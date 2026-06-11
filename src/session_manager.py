import asyncio
from collections import deque
from typing import List, Dict, Any, Optional
from dataclasses import dataclass

from src.utils.logger import logger
from src.config import config
from src.api_reader import ApiReader, UserCredentials, ReadingResult
from src.credential_manager import credential_manager
from src.history_manager import history_manager
from src.notifier import notifier, NotificationType
from src.reader import reader


@dataclass
class SessionResult:
    """会话结果"""
    user_name: str
    status: str
    message: str = ""
    result: Optional[ReadingResult] = None


class UserSession:
    """单个用户会话"""

    def __init__(self, user_config: Dict[str, Any]):
        self.user_config = user_config
        self.user_name = user_config.get("name", "default")
        self.api_reader: Optional[ApiReader] = None

    async def execute(self, progress_callback=None) -> SessionResult:
        """执行用户阅读会话"""
        credentials = credential_manager.load(self.user_name)

        if not credentials or not credentials.is_valid():
            return SessionResult(
                user_name=self.user_name,
                status="need_login",
                message="请先登录"
            )

        # 关键:API 模式前先远程 ping 一下 wr_skey,避免 wr_skey 已失效导致 -2012
        # (-2012 出现在主循环中途很难看,前置 ping 失败直接转 need_login)
        if not await self._precheck_credentials(credentials):
            return SessionResult(
                user_name=self.user_name,
                status="need_login",
                message="登录已失效,请重新扫码(API 模式前置检测失败,避免运行中 -2012 中断)"
            )

        reading_overrides = self.user_config.get("reading_overrides", {})
        target_duration = reading_overrides.get(
            "target_duration",
            config.get("reading.target_duration", "30-60")
        )
        mode = reading_overrides.get(
            "mode",
            config.get("reading.mode", "smart_random")
        )

        logger.info(f"开始阅读会话: {self.user_name}, 时长: {target_duration}, 模式: {mode}")

        try:
            self.api_reader = ApiReader(credentials, user_name=self.user_name)
            result = await self.api_reader.start_reading(on_progress=progress_callback)

            real_name = self.user_config.get("display_name", "") or credentials.user_name or self.user_name
            history_manager.add_reading_entry(result, real_name)

            return SessionResult(
                user_name=self.user_name,
                status=result.status,
                message="阅读完成" if result.status == "completed" else "阅读失败",
                result=result
            )

        except Exception as e:
            logger.error(f"阅读会话异常: {e}")
            return SessionResult(
                user_name=self.user_name,
                status="error",
                message=str(e)
            )

    async def _precheck_credentials(self, credentials: UserCredentials, timeout: float = 8.0) -> bool:
        """API 模式前置检测:用 wr_skey + wr_vid 发一个轻量请求,确认还活着。

        返回:
          True  → 凭证有效,可以进入主循环
          False → 凭证失效,需要重新登录(避免主循环中途 -2012 中断)

        检测策略(关键修复):
          之前调 /web/shelf 会得到 200,但真实阅读接口 /web/book/read 会 -2012。
          原因:/web/shelf 是 SPA 路由,cookie scope 容差大;/web/book/read 是真实后端 API。
          现在改用跟主阅读相同的接口路径 + 空 payload,直接验证后端 API 的 cookie scope。
        """
        try:
            import httpx
            cookies = {
                "wr_skey": credentials.wr_skey,
                "wr_vid": credentials.wr_vid,
            }
            user_info = credentials.user_info if isinstance(credentials.user_info, dict) else {}
            for k, v in (user_info.get("cookies") or {}).items():
                if k in ("wr_gid", "wr_fp", "wr_ql", "wr_rt") and v:
                    cookies[k] = v
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://weread.qq.com/web/reader",
                "Accept": "application/json, text/plain, */*",
            }
            # 用和真实阅读完全相同的接口路径 /web/book/read(关键修复)
            # 空 payload → 后端会判 -2012/参数错,但 -2012 表示 cookie scope 失效
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                resp = await client.post(
                    "https://weread.qq.com/web/book/read",
                    headers=headers,
                    cookies=cookies,
                    json={"data": {}},  # 空数据 → 后端必返回错误,但能从 errCode 判断 cookie 状态
                )
            # 200 = 凭证有效(即使 data 空也能正常处理)
            if resp.status_code == 200:
                logger.info(f"[precheck] {self.user_name} 凭证有效 (200)")
                return True
            # 关键:检查 errCode=-2012(登录超时)
            try:
                body = resp.json()
                if isinstance(body, dict):
                    err = body.get("errCode") or body.get("errcode") or body.get("code")
                    if err in (-2012, "-2012", 2012, "2012"):
                        logger.warning(f"[precheck] {self.user_name} 凭证已失效 (errCode={err}, 真实阅读接口确认)")
                        return False
                    # 其他 errCode(参数错之类)→ 凭证本身是活的,只是 payload 问题
                    logger.debug(f"[precheck] {self.user_name} 非 -2012 错误 (errCode={err}),凭证视为有效")
                    return True
            except Exception:
                pass
            if resp.status_code in (401, 403):
                logger.warning(f"[precheck] {self.user_name} 凭证已失效 (HTTP {resp.status_code})")
                return False
            logger.debug(f"[precheck] {self.user_name} 状态码 {resp.status_code} 视为未知,放行")
            return True
        except Exception as e:
            logger.debug(f"[precheck] {self.user_name} 检测异常(放行): {e}")
            return True


class SessionManager:
    """会话管理器"""

    def __init__(self, max_concurrent: int = 1):
        self.max_concurrent = max_concurrent
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self._is_running = False
        self._progress = {}
        self._logs = deque(maxlen=100)
        self._active_session: Optional[UserSession] = None
        self._has_failed = False
        self._fail_reason = ""

    def get_progress(self) -> Dict[str, Any]:
        return self._progress

    def get_logs(self) -> List[Dict[str, str]]:
        return list(self._logs)

    def add_log(self, msg: str):
        from datetime import datetime, timezone, timedelta
        import os
        tz_env = os.environ.get("TZ", "")
        offset_h = 8
        if "+" in tz_env:
            try:
                offset_h = int(tz_env.split("+")[-1].split(":")[0])
            except: pass
        elif "-" in tz_env:
            try:
                offset_h = -int(tz_env.split("-")[-1].split(":")[0])
            except: pass
        tz = timezone(timedelta(hours=offset_h))
        ts = datetime.now(tz).strftime("%H:%M:%S")
        self._logs.append({"time": ts, "msg": msg})

    async def _progress_callback(self, data: Dict[str, Any]):
        pct = data.get('progress', 0)
        reads = data.get('total_reads', 0)
        failed = data.get('failed_reads', 0)
        total = reads + failed
        parts = [f"进度 {pct}%"]
        if total > 0:
            parts.append(f"请求 {total}次 成功 {reads}")
        if failed > 0:
            parts.append(f"失败 {failed}次")
        data["message"] = " | ".join(parts)
        self._progress.update(data)
        log_msg = data.get("log", "")
        if log_msg:
            self.add_log(log_msg)
        if self._active_session and self._active_session.api_reader:
            api = self._active_session.api_reader
            if api.should_stop and api._fail_reason:
                self._has_failed = True
                self._fail_reason = api._fail_reason
                # 关键修复:把失败原因推到前端 logConsole(避免前端看不到失败详情)
                self.add_log(f"❌ API失败: {api._fail_reason}")
                # 同时记录到容器日志(双保险)
                logger.warning(f"[session_manager] API失败原因: {api._fail_reason}")

    async def _run_user_with_semaphore(self, user_config: Dict[str, Any]) -> SessionResult:
        """使用信号量运行用户会话"""
        async with self.semaphore:
            session = UserSession(user_config)
            self._active_session = session
            try:
                return await session.execute(self._progress_callback)
            finally:
                self._active_session = None

    def stop(self):
        """停止当前正在运行的阅读"""
        if self._active_session and self._active_session.api_reader:
            self._active_session.api_reader.should_stop = True
            logger.info("已请求停止API阅读")
        reader.should_stop = True
        logger.info("已请求停止模拟阅读")

    async def run_multi_user(self) -> List[SessionResult]:
        """运行多用户会话"""
        self._logs = deque(maxlen=100)
        self._is_running = True
        self._has_failed = False
        self._fail_reason = ""
        users = config.get_users()
        if not users:
            user_name = "default"
            self.add_log(f"开始阅读: {user_name}")
            self._progress = {"status": "running", "user": user_name, "message": "初始化..."}
            result = [await self._run_single_user()]
            self._is_running = False
            self._progress = {"status": "idle"}
            r = result[0]
            status_str = "✅ 成功" if r.status == "completed" else f"❌ 失败: {r.message}"
            self.add_log(f"阅读{status_str}")
            return result

        self._is_running = True
        user_names = [u.get("name","?") for u in users]
        self.add_log(f"多用户阅读开始: {', '.join(user_names)}")
        self._progress = {"status": "running", "message": "初始化..."}
        logger.info(f"开始多用户会话，用户数: {len(users)}")

        tasks = [self._run_user_with_semaphore(u) for u in users]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        session_results = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                session_results.append(SessionResult(
                    user_name=users[i].get("name", f"用户{i+1}"),
                    status="error",
                    message=str(result)
                ))
            else:
                session_results.append(result)

        for r in session_results:
            status_str = "✅ 成功" if r.status == "completed" else f"❌ 失败: {r.message}"
            self.add_log(f"用户 {r.user_name}: {status_str}")

        # 关键修复:session 结束如果 _fail_reason 还在,推到前端 + 容器日志
        if self._fail_reason and not any("❌" in log.get("msg", "") for log in self._logs):
            self.add_log(f"❌ 失败原因: {self._fail_reason}")
            logger.warning(f"[session_manager] 失败原因: {self._fail_reason}")

        self._is_running = False
        self._progress = {"status": "idle"}
        self.add_log("阅读任务全部完成")
        await self._send_summary_notification(session_results)

        return session_results

    async def _run_single_user(self) -> SessionResult:
        """运行单用户会话(使用默认 reading 配置)

        查找顺序:
          1) config.users 里第一个有有效凭证的 name(优先)
          2) 磁盘上 shared/credentials/ 里第一个有有效凭证的目录
          3) config.users 里第一个(即便凭证失效,保留名字)
          4) 真的没有任何用户 → 返回 need_login,**不**自动创建 default 文件夹
             (用户需在 Web UI 扫码登录,登录成功后才会创建用户目录)

        关键:不允许在 config.users 空时自动用 "default" 跑阅读
        历史坑:会触发 capture_and_save_curl / credential_manager.save 创建 default/ 目录
        """
        from src.cookie_manager import cookie_manager
        from src.credential_manager import credential_manager

        # 1) config 优先(避免 fallback 覆盖)
        config_users = config.get_users()  # 已去重
        if config_users:
            for u in config_users:
                name = u.get("name", "")
                if name and credential_manager.is_valid(name):
                    logger.info(f"单用户模式: 使用 config 里的有效用户 {name}")
                    return await UserSession({"name": name}).execute(self._progress_callback)
            # config 有用户但凭证都失效,记住第一个名字兜底
            fallback_name = config_users[0].get("name", "default")
            if credential_manager.is_valid(fallback_name):
                logger.info(f"单用户模式: 使用 config 里第一个有凭证的用户 {fallback_name}")
                return await UserSession({"name": fallback_name}).execute(self._progress_callback)
            logger.warning(
                f"单用户模式: config 有用户 {fallback_name} 但凭证失效,"
                f"先保留该名字不重建 default(避免覆盖原始用户名)"
            )
            return await UserSession({"name": fallback_name}).execute(self._progress_callback)

        # 2) config 空,看磁盘目录
        disk_valid = cookie_manager.get_all_valid_users()
        if disk_valid:
            user_name = disk_valid[0]
            logger.info(f"单用户模式: config 空,从磁盘找到有效用户 {user_name}")
            return await UserSession({"name": user_name}).execute(self._progress_callback)

        # 3) 真的没有用户 → need_login,不再自动创建 default 文件夹
        logger.info(
            "单用户模式: config.users 为空且磁盘无有效用户,"
            "等待用户在 Web UI 扫码登录后才会创建用户目录"
        )
        return SessionResult(
            user_name="",
            status="need_login",
            message="暂无用户,请在 Web UI 添加用户后扫码登录",
        )

    async def _send_summary_notification(self, results: List[SessionResult]):
        """发送多用户汇总通知"""
        total = len(results)
        successful = sum(1 for r in results if r.status == "completed")
        failed = total - successful

        if total == 1:
            return

        total_minutes = sum(
            r.result.elapsed_minutes if r.result else 0
            for r in results
        )

        msg = f"多用户阅读完成\n"
        msg += f"总用户: {total}\n"
        msg += f"成功: {successful}\n"
        msg += f"失败: {failed}\n"
        msg += f"总时长: {total_minutes:.0f} 分钟"

        await notifier.send(msg, NotificationType.MULTI_USER_SUMMARY)

    def is_running(self) -> bool:
        return self._is_running


session_manager = SessionManager()
