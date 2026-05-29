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
            self.api_reader = ApiReader(credentials)
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

        self._is_running = False
        self._progress = {"status": "idle"}
        self.add_log("阅读任务全部完成")
        await self._send_summary_notification(session_results)

        return session_results

    async def _run_single_user(self) -> SessionResult:
        """运行单用户会话（使用默认 reading 配置）"""
        return await UserSession({"name": "default"}).execute(self._progress_callback)

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
