import asyncio
import random
import time
import hashlib
import urllib.parse
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
from datetime import datetime
import httpx

from src.utils.logger import logger
from src.config import config
from src.http_client import http_client, HttpClient


KEY = "3c5c8717f3daf09iop3423zafeqoi"


@dataclass
class UserCredentials:
    user_id: str = ""
    user_name: str = ""
    wr_skey: str = ""
    wr_vid: str = ""
    sign_key: str = ""
    user_info: Dict[str, Any] = field(default_factory=dict)
    expires_at: Optional[str] = None
    saved_at: Optional[str] = None

    def is_valid(self) -> bool:
        return bool(self.wr_skey and self.wr_vid)


@dataclass
class ReadingResult:
    status: str = "unknown"
    elapsed_seconds: int = 0
    elapsed_minutes: float = 0
    target_minutes: int = 0
    total_reads: int = 0
    failed_reads: int = 0
    books_read: int = 0
    errors: Dict[str, int] = field(default_factory=dict)
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    user_name: str = ""


class ApiReader:
    """HTTP API 阅读引擎 - 与 weread-bot 一致"""

    READ_URL = "https://weread.qq.com/web/book/read"
    RENEW_URL = "https://weread.qq.com/web/login/renewal"
    FIX_SYNCKEY_URL = "https://weread.qq.com/web/book/chapterInfos"

    DEFAULT_HEADERS = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        "content-type": "application/json;charset=UTF-8",
        "origin": "https://weread.qq.com",
        "referer": "https://weread.qq.com/",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }

    def __init__(self, credentials: UserCredentials, user_name: str = "default"):
        self.credentials = credentials
        self.user_name = user_name
        self.is_reading = False
        self.should_stop = False
        self.start_time: Optional[datetime] = None
        self.elapsed_seconds = 0
        self.total_reads = 0
        self.failed_reads = 0
        self.books_read = 0
        self.errors: Dict[str, int] = {}
        self.last_book_id = ""
        self.last_book_name = ""
        self.last_chapter_id = ""
        self.last_chapter_index: Optional[int] = None
        self._last_progress_time = 0
        self._last_progress_log_time = 0
        self._needs_refresh = False
        self._last_logged_book = ""
        self._last_logged_chapter = ""
        self._fail_reason = ""
        # 连续空 dict / 无 succ 字段 计数(用于熔断 + 噪音日志降频)
        self._consecutive_none = 0

        # 懒加载章节缓存 + 失败记录(避免 retry 循环无限重抓同一本)
        self._book_chapter_cache: Dict[str, Dict[str, Any]] = {}
        self._lazy_load_failed: set = set()
        # 每本书独立的 ci 计数器(跨书切换不互相污染,模拟真实翻页)
        self._book_ci_counters: Dict[str, int] = {}

        self.http_client: Optional[HttpClient] = None
        self._init_http_client()

        self.data: Dict[str, Any] = {}
        self.cookies: Dict[str, str] = {}
        self.headers: Dict[str, str] = {}
        self._init_from_credentials()

    def _init_from_credentials(self):
        """从凭证初始化请求数据和cookie/header

        关键设计:
          - captured_payload 的 b/c/r/st/sc/ct/ps/pc 全部保留作为身份上下文
          - b/c 兜底用 reading_progress.json,但 captured 有时不覆盖(避免 b/c 错配)
          - cookies 缺字段时回退到 credentials 上的 wr_skey/wr_vid
        """
        ui = self.credentials.user_info or {}
        cp = ui.get("captured_payload", {}) or {}
        # 1) payload:完整保留 captured 的关键字段,只丢 s(下面重算)
        if cp and isinstance(cp, dict):
            self.data = dict(cp)
            self.data.pop("s", None)
            # 关键:运行时兜底,自动修复历史脏数据里的 mojibake sm 字段
            # (双层防御:browser.capture_and_save_curl 写盘前也修,但旧数据可能没经过那里)
            self._heal_payload_mojibake()
        else:
            self.data = {}

        # 兜底:如果 captured_payload 里 ps/pc 缺失,用 user_info 顶层或 credentials 补
        if not self.data.get("ps"):
            self.data["ps"] = ui.get("ps") or (self.credentials.user_id or self.credentials.wr_vid or "")
        if not self.data.get("pc"):
            self.data["pc"] = ui.get("pc") or (self.credentials.user_id or self.credentials.wr_vid or "")
        # b/c 兜底:只在 captured 完全没有 b/c 时用进度文件补
        # (避免 captured 的 b/c 被错误覆盖,造成 b/c 错配被腾讯服务端静默丢弃)
        if not self.data.get("b"):
            self.data["b"] = self.last_book_id or ""
        if not self.data.get("c"):
            self.data["c"] = self.last_chapter_id or ""
        # 兜底 r/st/sc:首次或 captured 缺失
        self.data.setdefault("r", 0)
        self.data.setdefault("st", 0)
        self.data.setdefault("sc", 0)

        # 2) cookies
        cc = ui.get("captured_cookies", {}) or {}
        if cc and isinstance(cc, dict):
            self.cookies = dict(cc)
        else:
            self.cookies = {"wr_skey": self.credentials.wr_skey, "wr_vid": self.credentials.wr_vid}
        # 始终确保 wr_skey / wr_vid 在 cookies 里(可能 captured_cookies 缺)
        if not self.cookies.get("wr_skey"):
            self.cookies["wr_skey"] = self.credentials.wr_skey
        if not self.cookies.get("wr_vid"):
            self.cookies["wr_vid"] = self.credentials.wr_vid

        # 3) headers:跟 upstream funnyzak/weread-bot 对齐,pass-through 风格
        # 关键原则:**用户 curl 抓了什么 header,就发什么 header**,腾讯反爬才会认
        # upstream 不做任何过滤(只过滤 Cookie,因为我们用 httpx cookies= kwarg 单独传)
        # 我们之前过滤 x-wrpa-0 / sec-* 是错的,导致腾讯拿不到反爬 token,稳定返回 {}
        ch = ui.get("captured_headers", {}) or {}
        if ch and isinstance(ch, dict):
            # 基础:用 captured_headers 作为起点(完全照搬 upstream)
            # 不再用 DEFAULT_HEADERS 兜底——避免被默认 header 覆盖关键指纹
            self.headers = dict(ch)
            # 只过滤 4 类:
            #   1) Cookie:httpx 走 cookies= kwarg,不能同时放 header 里
            #   2) host / content-length / connection:httpx 自己处理
            #   3) baggage / sentry* / sentry-trace:sentry 上报头,业务请求不该带
            # 其余(x-wrpa-0, sec-ch-ua*, user-agent, referer, accept, content-type 等)全部保留
            for k in list(self.headers.keys()):
                kl = k.lower()
                if kl == "cookie":
                    del self.headers[k]
                elif kl in ("host", "content-length", "connection",
                            "baggage", "sentry", "sentry-trace"):
                    del self.headers[k]
        else:
            # 没有 captured_headers 才用 DEFAULT_HEADERS 兜底
            # (curl_parser 失败或老数据没存 header 才会到这里)
            self.headers = dict(self.DEFAULT_HEADERS)

    def _heal_payload_mojibake(self):
        """兜底:如果 self.data["sm"] 看起来是双重 UTF-8 编码(被 Latin-1 解读过),
        还原为正确的中文。腾讯服务端对乱码 sm 直接拒收,返回 {} → 空跑。
        """
        sm = self.data.get("sm", "")
        if not isinstance(sm, str) or not sm:
            return
        # 已经包含 CJK 字符 → 不用修
        if any("\u4e00" <= c <= "\u9fff" for c in sm):
            return
        try:
            healed = sm.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return
        if any("\u4e00" <= c <= "\u9fff" for c in healed) and "\ufffd" not in healed:
            from src.utils.logger import logger
            logger.info(
                f"sm 字段运行时 mojibake 自愈: {sm[:24]!r}... -> {healed[:24]!r}..."
            )
            self.data["sm"] = healed
        # co 字段偶尔也含中文(章节名),同样兜底
        co = self.data.get("co", "")
        if isinstance(co, str) and co and not any("\u4e00" <= c <= "\u9fff" for c in co):
            try:
                healed_co = co.encode("latin-1").decode("utf-8")
                if any("\u4e00" <= c <= "\u9fff" for c in healed_co) and "\ufffd" not in healed_co:
                    self.data["co"] = healed_co
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass

    def _init_http_client(self):
        network_config = config.get("network", {})
        self.http_client = HttpClient(
            timeout=network_config.get("timeout", 30),
            retry_times=network_config.get("retry_times", 3),
            retry_delay=network_config.get("retry_delay", "5-15"),
            rate_limit=network_config.get("rate_limit", 10),
        )

    def _encode_data(self, data: dict) -> str:
        pairs = [f"{k}={urllib.parse.quote(str(data[k]), safe='')}" for k in sorted(data.keys())]
        return "&".join(pairs)

    def _calculate_hash(self, input_string: str) -> str:
        _7032f5 = 0x15051505
        _cc1055 = _7032f5
        length = len(input_string)
        _19094e = length - 1
        while _19094e > 0:
            char_code = ord(input_string[_19094e])
            shift_amount = (length - _19094e) % 30
            _7032f5 = 0x7fffffff & (_7032f5 ^ char_code << shift_amount)
            prev_char_code = ord(input_string[_19094e - 1])
            prev_shift_amount = _19094e % 30
            _cc1055 = 0x7fffffff & (_cc1055 ^ prev_char_code << prev_shift_amount)
            _19094e -= 2
        return hex(_7032f5 + _cc1055)[2:].lower()

    def _apply_user_identity(self):
        """重新注入 ps/pc(以及有的话 appId)。

        关键设计:
          - ps/pc 是会话级身份,captured 时绑定,后续每轮都从 captured 注入确保身份一致
          - appId 只在 captured 有时才设置,不强制(很多 curl 抓包没有 appId 字段也能跑)
          - 不触碰 b/c:ps/pc 必须跟 b/c 同源(同一次抓包),否则腾讯服务端判定异常

        对齐 upstream funnyzak/weread-bot:在 startup 时校验 ps/pc/appId 不是
        placeholder("app_id" 或空字符串),避免带着垃圾值跑 20 次空响应
        """
        ui = self.credentials.user_info or {}
        cp = ui.get("captured_payload", {}) or {}
        if cp.get("ps"):
            self.data["ps"] = cp["ps"]
        if cp.get("pc"):
            self.data["pc"] = cp["pc"]
        if cp.get("appId"):
            self.data["appId"] = cp["appId"]
        # 兜底:确保 ps/pc 永远非空
        if not self.data.get("ps"):
            self.data["ps"] = self.credentials.user_id or self.credentials.wr_vid or ""
        if not self.data.get("pc"):
            self.data["pc"] = self.credentials.user_id or self.credentials.wr_vid or ""

    def _validate_identity(self) -> Optional[str]:
        """对齐 upstream 启动时的占位符校验。

        Returns:
            None → 校验通过
            str  → 错误信息(具体哪个字段是 placeholder / 空)

        历史坑:curl_parser 解析失败时,captured_payload 可能是空 dict,
        appId/ps/pc 全是空字符串,腾讯会稳定返回 {}
        """
        ui = self.credentials.user_info or {}
        cp = ui.get("captured_payload", {}) or {}
        # upstream DEFAULT_DATA placeholder 是 "app_id",我们额外检查空字符串
        placeholders = {"app_id", ""}
        errors = []
        for f in ("appId", "ps", "pc"):
            v = (cp.get(f) or self.data.get(f) or "").strip()
            if not v or v in placeholders:
                errors.append(f"{f}={v!r}")
        if errors:
            return f"凭证关键字段缺失或为占位符: {', '.join(errors)} (请重新捕获 curl)"
        return None

    def _prepare_payload(self, last_time: int):
        """准备单次阅读请求的payload(对齐 weread-bot 协议)

        关键修复:
          - 每轮重新计算 ts/rn/sg + 校验 _apply_user_identity
          - 注入 captured 里的 r/st/sc(浏览器真实数据)
          - ci 在主循环每轮被 _advance_chapter_index 推进过,这里直接用 self.last_chapter_index
        """
        self.data.pop("s", None)

        # 同步进度:书/章节 (支持随机切书到任意已配置的书)
        if self.last_book_id:
            self.data["b"] = self.last_book_id
        if self.last_chapter_id:
            self.data["c"] = self.last_chapter_id

        # 注入 captured 里的 r/st/sc(如有)
        ui = self.credentials.user_info or {}
        cp = ui.get("captured_payload", {}) or {}
        for k in ("r", "st", "sc"):
            v = cp.get(k)
            if v is not None and (k not in self.data or self.data.get(k) in (None, 0, "")):
                self.data[k] = v

        # r/st/sc 兜底
        if "r" not in self.data or self.data.get("r") in (None,):
            self.data["r"] = random.randint(10000000, 99999999)
        if "st" not in self.data or self.data.get("st") is None:
            self.data["st"] = 0
        if "sc" not in self.data or self.data.get("sc") is None:
            self.data["sc"] = 0

        # 章节索引 ci (关键 P1 修复:腾讯以此判断是否在真实推进阅读)
        # 优先级:每本书独立的 ci 计数器 > captured.ci > 0
        # 注意:_advance_chapter_index 已经在主循环每轮调用过,last_chapter_index 已经是最新值
        if self.last_chapter_index is not None:
            self.data["ci"] = self.last_chapter_index
        elif "ci" not in self.data or self.data.get("ci") is None:
            self.data["ci"] = 0

        # 关键:应用 captured 的 ps/pc,确保会话身份一致
        self._apply_user_identity()

        current_time = int(time.time())
        self.data["ct"] = current_time
        self.data["rt"] = current_time - last_time if last_time else 0
        self.data["ts"] = int(current_time * 1000) + random.randint(0, 1000)
        self.data["rn"] = random.randint(0, 1000)
        self.data["sg"] = hashlib.sha256(f"{self.data['ts']}{self.data['rn']}{KEY}".encode()).hexdigest()
        self.data["s"] = self._calculate_hash(self._encode_data(self.data))

    async def _refresh_cookie(self) -> bool:
        """刷新cookie(同 weread-bot _refresh_cookie)

        关键修复:
          - 成功后同时同步到 credentials.wr_skey、cookies.json、Playwright context
            (否则懒加载章节 fetch_chapter_info 拿不到 wr_skey)
        """
        logger.info("刷新cookie...")
        try:
            # 关键:对齐 upstream funnyzak/weread-bot,RENEW_URL 的 rq 必须是 URL 编码
            # 之前用 "/web/book/read" (raw),腾讯服务端可能直接拒收
            # upstream 用 "%2Fweb%2Fbook%2Fread",我们也照做
            cookie_data = {
                "rq": "%2Fweb%2Fbook%2Fread",
                "ql": config.get("hack.cookie_refresh_ql", False),
            }
            response, _ = await self.http_client.post(
                self.RENEW_URL,
                headers=self.headers,
                cookies=self.cookies,
                json_data=cookie_data,
            )
            new_skey = self._extract_skey(response)
            if not new_skey:
                logger.warning("Cookie刷新失败:未找到新wr_skey")
                return False
            old_skey = self.cookies.get("wr_skey", "")
            self.cookies["wr_skey"] = new_skey
            # 同步到内存 credentials
            if self.credentials:
                self.credentials.wr_skey = new_skey
            # 同步到 Playwright context(否则懒加载 fetch 拿不到 wr_skey)
            try:
                from src.browser import browser_manager
                if browser_manager and browser_manager.context:
                    try:
                        await browser_manager.context.clear_cookies()
                    except Exception:
                        pass
                    await browser_manager._ensure_fresh_cookies()
                    logger.debug("Playwright context cookies 已同步")
            except Exception as e:
                logger.debug(f"同步 wr_skey 到 Playwright context 失败(非致命): {e}")
            logger.info(f"Cookie刷新成功: {old_skey[:8] if old_skey else 'N/A'}*** -> {new_skey[:8]}***")
            # 持久化回写(同步 cookies.json)
            await self._persist_wr_skey(new_skey)
            return True
        except Exception as e:
            logger.warning(f"Cookie刷新请求异常: {e}")
            return False

    async def _persist_wr_skey(self, new_skey: str) -> None:
        """把新 wr_skey 写回 default.json 和 cookies.json。"""
        try:
            from src.credential_manager import credential_manager
            if not self.credentials or not self.credentials.user_name:
                return
            self.credentials.wr_skey = new_skey
            # 更新 user_info 里的 captured_cookies
            ui = self.credentials.user_info or {}
            cc = ui.get("captured_cookies", {}) or {}
            if isinstance(cc, dict):
                cc["wr_skey"] = new_skey
                ui["captured_cookies"] = cc
            credential_manager.save(self.credentials)
            logger.debug(f"wr_skey 已回写 default.json (user={self.credentials.user_name})")

            # 同时更新 cookies.json(否则 Playwright context 拿不到新 wr_skey)
            try:
                from src.cookie_manager import cookie_manager
                cookies = cookie_manager.load(self.credentials.user_name)
                if cookies:
                    updated = []
                    for c in cookies:
                        c2 = dict(c)
                        if c2.get("name") == "wr_skey":
                            c2["value"] = new_skey
                        updated.append(c2)
                    cookie_manager.save(updated, self.credentials.user_name)
                    logger.debug(f"wr_skey 已回写 cookies.json (user={self.credentials.user_name})")
            except Exception as e:
                logger.warning(f"回写 cookies.json 失败(非致命): {e}")
        except Exception as e:
            logger.warning(f"回写 wr_skey 失败(非致命): {e}")

    @staticmethod
    def _extract_skey(response: httpx.Response) -> Optional[str]:
        new_skey = response.cookies.get("wr_skey")
        if new_skey:
            return new_skey
        set_cookie = response.headers.get("set-cookie", "")
        for part in set_cookie.split(";"):
            p = part.strip()
            if p.startswith("wr_skey="):
                return p.split("=", 1)[1]
        return None

    async def _check_response(self, response: httpx.Response) -> Optional[bool]:
        """检查响应(对齐 weread-bot _handle_protocol_response + errCode 硬性错误检测)

        返回:
          - True:响应有效
          - False:响应无效(计入失败计数)
          - None:响应模糊(如空 dict、临时风控),不计入成功也不计入失败

        成功判定:
          - 新版 API:succ=1
          - 老版 API:有 succ + synckey
        硬性错误(errCode=-2012/-2010)→ _fail_reason,主循环看到立刻停止
        """
        if response.status_code != 200:
            logger.warning(f"API非200响应: {response.status_code}")
            return False
        try:
            data = response.json()
        except Exception:
            logger.warning(f"API响应JSON解析失败: {response.text[:200]}")
            return False

        # 优先检测:登录超时 (errCode=-2012/-2010) → 立刻判定 API 不可用
        err_code = data.get("errCode")
        if err_code in (-2012, -2010):
            self._fail_reason = f"errCode={err_code} {data.get('errMsg','')}: 登录已失效,需要重新扫码"
            self._needs_refresh = False
            logger.warning(f"检测到 {self._fail_reason},立刻切到模拟模式")
            return False

        # 老版 API:有 succ + synckey
        if "succ" in data and "synckey" in data:
            return True

        # 新版 API:succ=1 就行
        if data.get("succ") == 1:
            return True

        # 只有 succ 没 synckey:旧版兼容问题(调 chapterInfos 修 synckey)
        if "succ" in data:
            logger.warning(f"API响应 succ={data.get('succ')},尝试修复: {str(data)[:200]}")
            await self._fix_no_synckey()
            return False

        # 关键(对齐 upstream funnyzak/weread-bot _handle_protocol_response):
        # 空 dict {} / 没有 succ 字段 → 都视为"cookie 失效"信号,触发 cookie 刷新
        # 历史坑:我们之前把空 dict 走 None 分支"宽容",结果 20 次空响应之间 cookie 一直
        # 没刷新,腾讯一直拒收。upstream 是无条件刷新的——我们也照做。
        if not data:
            logger.warning(
                f"API响应空 dict(无任何字段),触发cookie刷新: "
                f"可能 wr_skey 已过期或 x-wrpa-0 token 失效"
            )
            self._needs_refresh = True
            return False

        # 走到这里:有数据但没有 succ 字段 → 真正的异常,WARNING + 触发 cookie 刷新
        logger.warning(f"API响应无succ字段,触发cookie刷新: keys={list(data.keys())}")
        self._needs_refresh = True
        return False

    async def _fix_no_synckey(self):
        """修复 synckey 问题(同 weread-bot _fix_no_synckey)"""
        try:
            await self.http_client.post(
                self.FIX_SYNCKEY_URL,
                headers=self.headers,
                cookies=self.cookies,
                json_data={"bookIds": ["3300060341"]},
            )
        except Exception as e:
            logger.warning(f"synckey修复失败: {e}")

    async def start_reading(self, on_progress=None) -> ReadingResult:
        if self.is_reading:
            logger.warning("已在阅读中，跳过")
            return ReadingResult(status="already_reading")

        self.is_reading = True
        self.should_stop = False
        self.start_time = datetime.now()
        self.elapsed_seconds = 0
        self.total_reads = 0
        self.failed_reads = 0
        self.books_read = 0
        self.errors = {}
        self._last_progress_time = 0
        self._last_progress_log_time = 0
        self._needs_refresh = False
        self._last_logged_book = ""
        self._last_logged_chapter = ""

        self._fail_reason = ""

        # 延迟 import browser_manager (避免循环引用),且不依赖名字查找
        try:
            from src.browser import browser_manager as _bm
            self._browser_manager = _bm
        except Exception as _e:
            logger.debug(f"browser_manager 不可用(将在 fallback 路径忽略): {_e}")
            self._browser_manager = None

        def _mark_relogin(reason: str):
            """标记需要重新登录(安全调用)"""
            bm = self._browser_manager
            if bm is None:
                return
            try:
                bm._needs_relogin = True
                bm._relogin_reason = reason
            except Exception as e:
                logger.debug(f"标记重新登录失败(非致命): {e}")

        reading_config = config.get("reading", {})
        target_duration_str = reading_config.get("target_duration", "60-90")
        if isinstance(target_duration_str, str) and "-" in target_duration_str:
            parts = target_duration_str.split("-")
            target_minutes = random.randint(int(parts[0]), int(parts[1]))
        else:
            target_minutes = int(target_duration_str)
        target_seconds = target_minutes * 60

        # 对齐 upstream funnyzak/weread-bot:启动时校验 appId/ps/pc 不是 placeholder
        # 防止带着"app_id"这种垃圾值跑 20 次空响应
        identity_err = self._validate_identity()
        if identity_err:
            logger.error(f"❌ {identity_err}")
            return ReadingResult(
                status="error",
                elapsed_seconds=0,
                errors={"identity_invalid": identity_err},
            )

        mode = reading_config.get("mode", "smart_random")
        interval_str = reading_config.get("reading_interval", "30-48")
        if isinstance(interval_str, str) and "-" in interval_str:
            parts = interval_str.split("-")
            interval_min, interval_max = int(parts[0]), int(parts[1])
        else:
            interval_min = interval_max = int(interval_str)

        break_prob = reading_config.get("break_probability", 0.15)
        break_str = reading_config.get("break_duration", "30-180")
        if isinstance(break_str, str) and "-" in break_str:
            parts = break_str.split("-")
            break_min, break_max = int(parts[0]), int(parts[1])
        else:
            break_min = break_max = int(break_str)

        book_continuity = reading_config.get("book_continuity", 0.8)

        logger.info(f"API 阅读开始，目标时长: {target_minutes} 分钟")

        # 初始选书:重试直到选到一本有章节信息的书(避免 b/c 错配)
        skip_books = set()
        for _try in range(10):
            book_id, chapter_id, chapter_index, book_name, _ = await self._select_book_and_chapter(
                advance_ci=False, skip_books=skip_books
            )
            if book_id and chapter_id:
                break
            if book_id:
                skip_books.add(book_id)
            logger.debug(f"初始选书重试 ({_try + 1}/10)")
        if not book_id or not chapter_id:
            # 区分两种情况给精准错误:
            # - books 配置为空 → 提示去添加书
            # - books 有书但所有书 chapters 都拿不到 → 提示去扫码登录
            books_cfg = config.get("reading.books", [])
            if not books_cfg:
                err_msg = (
                    "书籍配置为空:请在 Web UI '书籍配置' 中至少添加一本书 "
                    "(chapters 可留空,会自动从微信读书接口懒加载)"
                )
                logger.error(f"❌ {err_msg}")
            else:
                err_msg = (
                    f"书籍配置共 {len(books_cfg)} 本,但所有书的章节都拿不到。"
                    f"通常是登录态失效(浏览器未扫码登录 或 wr_skey 过期)。"
                    f"请到 Web UI 右上角扫码登录,然后重试。"
                )
                logger.error(f"❌ {err_msg}")
                # 标记浏览器需要重新登录
                try:
                    from src.browser import browser_manager
                    if browser_manager:
                        browser_manager._needs_relogin = True
                        browser_manager._relogin_reason = "懒加载全部失败(Playwright+httpx 都拿不到章节),可能是登录失效"
                except Exception:
                    pass
            return ReadingResult(
                status="error",
                elapsed_seconds=0,
                errors={"no_chapter_info": err_msg},
            )
        self.last_book_id = book_id
        self.last_book_name = book_name or self.last_book_name
        self.last_chapter_id = chapter_id
        self.last_chapter_index = chapter_index
        logger.info(f"初始选择书籍: {self.last_book_name or self.last_book_id}")

        _logged_first = False
        last_time = int(time.time()) - 30
        last_book_switch_time = 0
        refresh_attempted = False
        # Circuit breaker:连续失败 N 次立即切到浏览器模式
        self._consecutive_failures = 0
        circuit_breaker_threshold = 8
        # 空 dict 熔断:连续 N 次模糊响应(没有 succ 也没有明确失败)→ 判定 API 死透
        # 历史坑:抓包数据损坏时 API 会稳定返回 {},不计入失败 → 熔断永远不触发
        # 这里单独算账,阈值约 = circuit_breaker_threshold * 2 (给风控临时响应一点容差)
        self._consecutive_none = 0
        none_breaker_threshold = 20

        try:
            while self.elapsed_seconds < target_seconds and not self.should_stop:
                # 每轮请求前推进 ci(模拟翻页),这是腾讯"阅读时长"入账的关键
                if mode == "smart_random":
                    if self.elapsed_seconds - last_book_switch_time > 300 and random.random() > book_continuity:
                        # 尝试切书;如果候选书缺章节,重试直到选到有的
                        switch_skip = set()
                        for _retry in range(10):
                            bid, cid, ci, bname, _ = await self._select_book_and_chapter(
                                advance_ci=False, skip_books=switch_skip
                            )
                            if bid and cid:
                                break
                            if bid:
                                switch_skip.add(bid)
                        if bid and cid and bid != self.last_book_id:
                            self.last_book_id = bid
                            self.last_book_name = bname or self.last_book_name
                            self.last_chapter_id = cid
                            self.last_chapter_index = ci or 0
                            self.books_read += 1
                            last_book_switch_time = self.elapsed_seconds
                            logger.info(f"切换书籍: {self.last_book_name or bid} ci={ci}")
                    else:
                        # 不切书 → 每轮 +1 ci(模拟翻页)
                        self._advance_chapter_index()
                else:
                    # 其他模式(sequential/pure_random)也每轮推进 ci
                    self._advance_chapter_index()

                self._prepare_payload(last_time)

                try:
                    if not _logged_first:
                        _logged_first = True
                        logger.info(f"API首次请求: ps={self.data.get('ps','')[:16]} pc={self.data.get('pc','')[:16]} appId={self.data.get('appId','')[:20]}")
                        # 诊断:首次请求时 dump 完整 payload + headers,后续再出问题可以对比
                        # 不会泄露 secrets(都是自己抓包的,不是用户的)
                        try:
                            import json as _json
                            _payload_dump = {k: v for k, v in self.data.items() if k != "s"}
                            logger.info(
                                f"[DIAG] 首次请求 payload (signed s 隐藏):\n"
                                f"{_json.dumps(_payload_dump, ensure_ascii=False, indent=2)}"
                            )
                            _TRUNC = "..."
                            _hdr_lines = []
                            for _k, _v in self.headers.items():
                                _v_str = str(_v)
                                _v_show = _v_str[:60] + _TRUNC if len(_v_str) > 60 else _v_str
                                _hdr_lines.append(f"  {_k}: {_v_show}")
                            logger.info(
                                f"[DIAG] 首次请求 headers ({len(self.headers)} 个):\n"
                                f"{chr(10).join(_hdr_lines)}"
                            )
                            _ck_lines = []
                            for _k, _v in self.cookies.items():
                                _v_str = str(_v)
                                _v_show = _v_str[:30] + _TRUNC if len(_v_str) > 30 else _v_str
                                _ck_lines.append(f"  {_k}={_v_show}")
                            logger.info(
                                f"[DIAG] 首次请求 cookies ({len(self.cookies)} 个):\n"
                                f"{chr(10).join(_ck_lines)}"
                            )
                        except Exception as _e:
                            logger.debug(f"payload dump 失败(非致命): {_e}")

                    response, _ = await self.http_client.post(
                        self.READ_URL,
                        headers=self.headers,
                        cookies=self.cookies,
                        json_data=self.data,
                    )

                    check_result = await self._check_response(response)
                    if check_result is True:
                        self.total_reads += 1
                        last_time = int(time.time())
                        refresh_attempted = False
                        self._consecutive_failures = 0
                        self._consecutive_none = 0
                    elif check_result is None:
                        # 模糊响应(空 dict)——既不成功也不失败,但**不要 continue**
                        # 之前的 continue 会跳过 on_progress 推送,导致:
                        #   - 前端 /api-reading-progress 永远拿不到新数据
                        #   - 前端 /api-reading-logs 永远收不到新条目
                        #   - 进度条卡在 0%,但实际请求一直在发
                        # 修复:让流程继续走到下面的 await on_progress() 推送进度
                        self._consecutive_none += 1
                        # 降频:只在第 1、5、10、15、20 次时报 debug,避免日志刷屏
                        if self._consecutive_none in (1, 5, 10, 15) or self._consecutive_none % 20 == 0:
                            logger.debug(
                                f"API响应模糊已连续 {self._consecutive_none} 次"
                                f"(空 dict/风控临时响应/抓包数据损坏)"
                            )
                        # 熔断:连续 N 次空响应 → 判定 API 不可用
                        if self._consecutive_none >= none_breaker_threshold:
                            self._fail_reason = (
                                f"api_consecutive_none: API 连续 {self._consecutive_none} 次"
                                f"返回空响应(无 succ 字段),疑似抓包数据损坏或登录态异常"
                            )
                            self.should_stop = True
                            logger.warning(
                                f"API 模式连续 {self._consecutive_none} 次空响应,"
                                f"触发 none breaker,立即停止 API 切换到模拟模式"
                            )
                            break
                    else:
                        # 硬性登录错误(-2012/-2010)
                        # 对齐 funnyzak/weread-bot 行为:ps/pc 是会话级常量(从一次性 curl 抓包来),
                        # 不会运行时刷新。-2012 = 整个登录态(wr_skey + 绑定的 ps/pc)失效。
                        # 真正的解决路径是:用户在 Web UI 扫码重新登录,触发浏览器内
                        # capture_and_save_curl 重新抓一份 ps/pc 并存盘 —— **不能**运行时自动重抓。
                        # 这里最多打日志 + 标记需要重登,然后 break 让上层切模拟模式兜底。
                        if self._fail_reason and ("errCode" in self._fail_reason):
                            logger.warning(
                                f"检测到 {self._fail_reason}:ps/pc 绑定的会话已失效。"
                                f"请在 Web UI 右上角扫码重新登录(会重新抓 curl 拿新 ps/pc)。"
                            )
                            self.failed_reads += 1
                            self.should_stop = True
                            _mark_relogin("登录已失效,需要在右上角扫码重新登录")
                            break
                        # 检查是否需要先 refresh cookie 重试一次
                        did_retry = False
                        if self._needs_refresh and not refresh_attempted:
                            refresh_attempted = True
                            refresh_ok = await self._refresh_cookie()
                            if refresh_ok:
                                logger.info("cookie刷新后重试请求...")
                                response, _ = await self.http_client.post(
                                    self.READ_URL,
                                    headers=self.headers,
                                    cookies=self.cookies,
                                    json_data=self.data,
                                )
                                retry_check = await self._check_response(response)
                                if retry_check is True:
                                    self.total_reads += 1
                                    last_time = int(time.time())
                                    self._consecutive_failures = 0
                                    did_retry = True
                                elif retry_check is None:
                                    # 重试时也遇到空 dict——同样不计入失败
                                    logger.debug("重试响应模糊(空 dict),跳过")
                                    did_retry = True  # 算"已尝试",不触发 _consecutive_failures 自增
                                else:
                                    self.failed_reads += 1
                                    self._needs_refresh = False
                                    self._consecutive_failures += 1
                                    logger.warning(f"API重试失败: {str(response.json())[:100]}")
                            else:
                                self.failed_reads += 1
                                self._needs_refresh = False
                                logger.warning("Cookie刷新失败,记录为失败请求")
                                self._fail_reason = "cookie_refresh_failed: wr_skey 失效,请重新扫码登录"
                                self.should_stop = True
                                _mark_relogin("wr_skey 失效,请在右上角扫码重新登录")
                                break
                        if not did_retry:
                            self.failed_reads += 1
                            self._consecutive_failures += 1
                            logger.warning(f"API响应无效 (连续失败 {self._consecutive_failures}/{circuit_breaker_threshold})")

                        # Circuit breaker:连续失败达到阈值,立刻切到浏览器模式
                        if self._consecutive_failures >= circuit_breaker_threshold:
                            self._fail_reason = f"api_consecutive_failures: API 连续失败 {self._consecutive_failures} 次,立刻切到模拟模式"
                            self.should_stop = True
                            logger.warning(f"API 模式连续失败 {self._consecutive_failures} 次,触发 circuit breaker,立即停止 API 切换到模拟模式")
                            break

                except Exception as e:
                    self.failed_reads += 1
                    self.errors[str(e)[:60]] = self.errors.get(str(e)[:60], 0) + 1
                    self._consecutive_failures += 1
                    err_msg = str(e)[:100]
                    logger.warning(f"API 请求异常 (连续失败 {self._consecutive_failures}/{circuit_breaker_threshold}): {e}")
                    # 连续网络异常也立即切到浏览器模式
                    if self._consecutive_failures >= circuit_breaker_threshold:
                        self._fail_reason = f"api_network_consecutive_failures: {err_msg}"
                        self.should_stop = True
                        logger.warning(f"API 模式网络连续失败 {self._consecutive_failures} 次,触发 circuit breaker,立即停止 API 切换到模拟模式")
                        break

                interval = random.uniform(interval_min, interval_max)

                if random.random() < break_prob:
                    break_time = random.randint(break_min, break_max)
                    logger.info(f"模拟休息 {break_time} 秒")
                    await asyncio.sleep(break_time)
                    self.elapsed_seconds += break_time

                await asyncio.sleep(interval)
                self.elapsed_seconds = int((datetime.now() - self.start_time).total_seconds())

                if on_progress and self.elapsed_seconds - (self._last_progress_time or 0) >= 10:
                    pct = min(int((self.elapsed_seconds / target_seconds) * 100), 100)
                    elapsed_m = self.elapsed_seconds // 60
                    elapsed_s = self.elapsed_seconds % 60
                    log_msg = f"进度 {pct}% ({elapsed_m}分{elapsed_s}秒)"
                    if self.total_reads > 0 or self.failed_reads > 0:
                        log_msg += f" | 成功 {self.total_reads} 失败 {self.failed_reads}"
                    progress = {
                        "elapsed": self.elapsed_seconds,
                        "target": target_seconds,
                        "progress": pct,
                        "current_book": self.last_book_id,
                        "book_name": self.last_book_name,
                        "total_reads": self.total_reads,
                        "failed_reads": self.failed_reads,
                        "log": log_msg,
                    }
                    # 关键修复:每个 N 秒主动给前端推一条心跳 log,避免前端 logConsole 一直空白
                    if int(self.elapsed_seconds) - self._last_progress_log_time >= 30:
                        self._last_progress_log_time = int(self.elapsed_seconds)
                        progress["log"] = f"⏱️ 进度 {pct}% | 成功 {self.total_reads} 失败 {self.failed_reads}"
                    book_key = f"{self.last_book_name or self.last_book_id}:{self.last_chapter_index or 0}"
                    if book_key != self._last_logged_book:
                        self._last_logged_book = book_key
                        ch_info = f" 第{self.last_chapter_index}章" if self.last_chapter_index is not None else ""
                        progress["log"] = f"阅读: {self.last_book_name or self.last_book_id}{ch_info}"
                    await on_progress(progress)
                    self._last_progress_time = self.elapsed_seconds

            actual_minutes = self.elapsed_seconds / 60

            if self.should_stop and self._fail_reason:
                logger.warning(f"API 异常终止: {self._fail_reason}")
                return ReadingResult(
                    status="error",
                    elapsed_seconds=self.elapsed_seconds,
                    elapsed_minutes=actual_minutes,
                    target_minutes=target_minutes,
                    total_reads=self.total_reads,
                    failed_reads=self.failed_reads,
                    books_read=0,
                    errors={**self.errors, "fail_reason": self._fail_reason},
                    start_time=self.start_time.isoformat(),
                    end_time=datetime.now().isoformat(),
                )

            logger.info(f"阅读完成，实际: {actual_minutes:.1f}分钟 成功:{self.total_reads} 失败:{self.failed_reads}")
            return ReadingResult(
                status="completed",
                elapsed_seconds=self.elapsed_seconds,
                elapsed_minutes=actual_minutes,
                target_minutes=target_minutes,
                total_reads=self.total_reads,
                failed_reads=self.failed_reads,
                books_read=self.books_read if self.books_read > 0 else 1,
                errors=self.errors,
                start_time=self.start_time.isoformat(),
                end_time=datetime.now().isoformat(),
            )

        except Exception as e:
            logger.error(f"阅读过程异常: {e}")
            return ReadingResult(
                status="error",
                elapsed_seconds=self.elapsed_seconds,
                errors={"exception": str(e)},
            )
        finally:
            self.is_reading = False

    async def _select_book_and_chapter(self, advance_ci: bool = True, skip_books: set = None) -> tuple:
        """选书 + 选章节(支持懒加载章节)。

        Args:
            advance_ci: 是否推进 ci 计数(切书时传 False,因为已经通过 _advance_chapter_index 推进)
            skip_books: 本次调用要跳过的 book_id 集合(由 retry 循环传,避免无限重试同一本)

        关键修复:
        - 第一本书必须从 books 配置里 random.choice(不读 last_book_id 兑底)
        - books 为空 → 返回空元组,让主循环报 "请在书籍配置加书"
        - chapters=[] → 走 _ensure_chapter_for_book 懒加载路径(内存→磁盘→Playwright→httpx)
        """
        books = config.get("reading.books", [])

        # 推进 ci(主循环每轮调用时由调用方负责,这里 advance_ci=False 时跳过)
        if advance_ci:
            self._advance_chapter_index()

        # 关键:books 配置为空时,不要用 last_book_id 兑底
        # (兑底会跳过"从书籍配置随机"的关键测试点 + 跳过懒加载触发)
        if not books:
            logger.warning(
                "书籍配置为空,请在 Web UI '书籍配置' 中至少添加一本书 "
                "(chapters 可空,会走懒加载从微信读书接口自动拉)"
            )
            return "", "", None, "", ""

        # 过滤掉本次不可用的书
        candidates = [b for b in books if b.get("book_id") not in (skip_books or set())]
        if not candidates:
            logger.warning(
                f"所有候选书都被跳过 (skip_books={len(skip_books)}),"
                f"books 配置共 {len(books)} 本但无可用候选"
            )
            return "", "", None, "", ""

        # 关键:第一本从 books 配置 random.choice,不读 last_book_id
        book = random.choice(candidates)
        logger.info(
            f"随机选书: {book.get('name') or book.get('book_id','')[:12]} "
            f"(共 {len(candidates)} 本候选,本次随机到第 {candidates.index(book)+1} 本)"
        )
        book_id = book.get("book_id", "")
        book_name = book.get("name", "")
        raw_chapters = book.get("chapters", [])
        chapter_id = ""
        chapter_index = None
        chapter_name = ""

        if raw_chapters:
            ch = random.choice(raw_chapters)
            if isinstance(ch, dict):
                _cid = ch.get("chapter_id") or ch.get("id") or ""
                _cix = ch.get("chapter_index") or ch.get("index")
                _cname = ch.get("name", "") or ""
            else:
                _cid = str(ch)
                _cix = None
                _cname = ""

            # 防御性检查:chapter_id 必须是腾讯后端用的 24 字符 hex UID
            # 真实格式形如 "154325b0318b1543843a9ca"(24 字符,小写 hex 为主)
            # 如果是纯数字(< 6 字符)或太短,基本是用户/老数据错填的"章节顺序号"
            # 这种值发给腾讯会被静默拒收(返回 {})
            # 修复:视为无效,走懒加载拿真 chapter UID
            if (
                not _cid
                or not isinstance(_cid, str)
                or len(_cid) < 6
                or _cid.isdigit()  # 纯数字(如 "50")
            ):
                logger.warning(
                    f"检测到无效的 chapter_id {_cid!r}(< 6 字符或纯数字),"
                    f"通常是误填的章节顺序号而非真 UID。改走懒加载路径获取真章节 UID..."
                )
                # 不使用 raw_chapters,直接跳到懒加载分支
                # 留 chapter_id="" 让下面的 else 分支跑懒加载
                _cid = ""
                _cix = None
                _cname = ""

            chapter_id = _cid
            chapter_index = _cix
            chapter_name = _cname

            # 如果 chapter_id 被上面的检查清空,改走懒加载
            if not chapter_id:
                # 复用下面的 else 分支逻辑(用 fetched dict 填充)
                raw_chapters = []  # 强制走 else 分支
                # 注意:不 return,让下面的 else 分支跑

        if not raw_chapters:  # 合并:既支持原本空,又支持"被防御清空"
            # chapters 为空 —— 按以下顺序获取章节信息:
            # 1) 内存缓存 _book_chapter_cache
            # 2) captured_payload(仅当 captured 时刚好在这本书上)
            # 3) reading_progress.json(仅当进度文件记录的就是这本书)
            # 4) 浏览器懒加载 fetch_chapter_info(调 i.weread.qq.com/book/chapterInfos)
            cached = self._book_chapter_cache.get(book_id)
            if cached:
                chapter_id = cached.get("first_chapter_uid", "")
                chapter_index = cached.get("first_chapter_idx", 0)
                logger.debug(f"chapters 为空,使用内存缓存章节: {book_id[:12]} c={chapter_id[:16]}")
            else:
                ui = self.credentials.user_info or {} if self.credentials else {}
                cp = ui.get("captured_payload", {}) or {}
                captured_c = cp.get("c", "")
                captured_b = cp.get("b", "")

                if captured_c and captured_b == book_id:
                    chapter_id = captured_c
                    logger.debug(f"chapters 为空,使用 captured_payload.c (同书)")
                elif self.last_chapter_id and self.last_book_id == book_id:
                    chapter_id = self.last_chapter_id
                    chapter_index = self.last_chapter_index
                    logger.debug(f"chapters 为空,使用 reading_progress 的 last_chapter_id (同书)")
                else:
                    # 关键:浏览器懒加载 —— 第一次切到这本书时,自动从微信读书接口抓章节列表
                    fetched = await self._ensure_chapter_for_book(book_id)
                    if fetched:
                        chapter_id = fetched.get("first_chapter_uid", "")
                        chapter_index = fetched.get("first_chapter_idx", 0)
                        logger.info(
                            f"懒加载章节成功: {book_name or book_id[:12]} "
                            f"→ c={chapter_id[:16]} idx={chapter_index}"
                        )
                    else:
                        # 抓不到 —— 这本书暂时无法用 API 模式
                        logger.debug(
                            f"书 '{book_name}' ({book_id[:12]}) 懒加载章节失败,跳过"
                        )
                        return "", "", None, book_name, ""

        # 章节索引兜底:chapters 为空时,使用每本书独立的 ci 计数器
        if chapter_index is None and chapter_id:
            base_ci = self._book_ci_counters.get(book_id, 0)
            chapter_index = base_ci
            self.last_chapter_index = chapter_index
            logger.debug(f"章节索引 ci={chapter_index} (book={book_id[:12]})")

        return book_id, chapter_id, chapter_index, book_name, chapter_name

    async def _ensure_chapter_for_book(self, book_id: str) -> Optional[Dict[str, Any]]:
        """懒加载:三级缓存策略拿指定书的章节列表。

        查找顺序:
          1) 内存缓存 self._book_chapter_cache(本次会话有效)
          2) 磁盘缓存 shared/credentials/{user}/chapters/{bookId}.json(跨会话)
          3) Playwright 真实打开 reader 页截获 chapterInfos 响应(主方案)
          4) httpx 直调 i.weread.qq.com(兜底,i.weread.qq.com 反爬严时会被 401)

        写入:
          - 1) 内存缓存
          - 2) 磁盘缓存(下次启动秒开)
          - 3) config.yaml 回填(兼容旧路径)

        失败:返回 None,且把 book_id 加入 _lazy_load_failed,避免 retry 循环无限重试同一本
        """
        # 1) 内存缓存
        if book_id in self._book_chapter_cache:
            return self._book_chapter_cache[book_id]

        # 已知失败过 → 不要再试
        if book_id in getattr(self, "_lazy_load_failed", set()):
            return None

        # 2) 磁盘缓存(按用户分目录) — 这是新方案的核心
        try:
            from src.user_data_manager import user_data_manager
            disk_cached = user_data_manager.load_chapters(self.user_name, book_id)
            if disk_cached:
                chapters = disk_cached.get("chapters", [])
                if chapters:
                    # 关键校验:磁盘缓存里的 chapterUid 必须是真 UID(>= 6 字符且非纯数字)
                    # 老 bug 写入了纯数字 itemId(误以为 chapterUid),这种脏数据腾讯会拒收
                    # 检测到污染 → 视为不可用,fall through 到下一路重新 fetch
                    first_uid = disk_cached.get("first_chapter_uid", "") or ""
                    is_polluted = (
                        not first_uid
                        or len(first_uid) < 6
                        or first_uid.isdigit()
                    )
                    if is_polluted:
                        logger.warning(
                            f"磁盘章节缓存疑似污染(first_chapter_uid={first_uid!r} 看起来不是真 UID),"
                            f"删除并重新 fetch..."
                        )
                        try:
                            user_data_manager.delete_chapters(self.user_name, book_id)
                        except Exception as _e:
                            logger.debug(f"删磁盘脏数据失败(非致命): {_e}")
                    else:
                        logger.info(
                            f"📦 命中磁盘章节缓存: {book_id[:12]} ({len(chapters)}章, "
                            f"fetched_at={disk_cached.get('fetched_at', '?')})"
                        )
                        self._book_chapter_cache[book_id] = {
                            "chapters": chapters,
                            "first_chapter_uid": disk_cached.get("first_chapter_uid", ""),
                            "first_chapter_idx": disk_cached.get("first_chapter_idx", 0),
                        }
                        return self._book_chapter_cache[book_id]
        except Exception as e:
            logger.debug(f"磁盘章节缓存查询失败(非致命): {e}")

        # 3) Playwright 截获(主方案,反爬抗性强)
        info = await self._fetch_chapter_via_playwright(book_id)
        if info:
            self._book_chapter_cache[book_id] = info
            # 持久化到磁盘
            try:
                from src.user_data_manager import user_data_manager
                user_data_manager.save_chapters(self.user_name, book_id, info)
            except Exception as e:
                logger.debug(f"保存磁盘章节缓存失败(非致命): {e}")
            # 兼容旧路径写回 config.yaml
            try:
                self._persist_chapter_to_config(book_id, info["chapters"])
            except Exception as e:
                logger.debug(f"持久化章节到 config.yaml 失败(非致命): {e}")
            return self._book_chapter_cache[book_id]

        # 4) httpx 兜底(原方案) — i.weread.qq.com 反爬严时 401
        info = await self._fetch_chapter_via_httpx(book_id)
        if info:
            self._book_chapter_cache[book_id] = info
            try:
                from src.user_data_manager import user_data_manager
                user_data_manager.save_chapters(self.user_name, book_id, info)
            except Exception as e:
                logger.debug(f"保存磁盘章节缓存失败(非致命): {e}")
            try:
                self._persist_chapter_to_config(book_id, info["chapters"])
            except Exception as e:
                logger.debug(f"持久化章节到 config.yaml 失败(非致命): {e}")
            return self._book_chapter_cache[book_id]

        # 5) 全部失败 → 标记失败
        self._lazy_load_failed.add(book_id)
        logger.warning(
            f"懒加载章节最终失败: {book_id[:12]} (Playwright + httpx 都不可用)"
        )
        return None

    async def _fetch_chapter_via_playwright(self, book_id: str) -> Optional[Dict[str, Any]]:
        """【主方案】调 browser_manager.fetch_chapter_info_via_page 拿章节。"""
        try:
            from src.browser import browser_manager
            if not browser_manager or not getattr(browser_manager, "page", None):
                # 内置浏览器未启动(daemon 没起或单测场景),跳过
                logger.debug("Playwright 浏览器未启动,跳过 Playwright 抓章节")
                return None
            info = await browser_manager.fetch_chapter_info_via_page(
                book_id, user_name=self.user_name
            )
            return info
        except Exception as e:
            logger.debug(f"Playwright 抓章节异常(非致命): {e}")
            return None

    async def _fetch_chapter_via_httpx(self, book_id: str) -> Optional[Dict[str, Any]]:
        """【兜底方案】用 httpx 直调 i.weread.qq.com/book/chapterInfos。
        保留原懒加载逻辑,在 Playwright 不可用时回退。
        注意:本方法不修改 self._lazy_load_failed,失败标记由 _ensure_chapter_for_book 统一处理。
        """
        # 优先用 self.cookies(self._refresh_cookie 更新过的最新 skey)
        wr_skey = self.cookies.get("wr_skey", "")
        wr_vid = self.cookies.get("wr_vid", "")
        if not wr_skey or not wr_vid:
            try:
                from src.cookie_manager import cookie_manager
                cookies = cookie_manager.load()
                for c in cookies or []:
                    if c.get("name") == "wr_skey":
                        wr_skey = c.get("value", "")
                    if c.get("name") == "wr_vid":
                        wr_vid = c.get("value", "")
            except Exception:
                pass

        # 关键:wr_skey/wr_vid 都为空(或之前 -2012 被主动清掉)→ 不发请求,直接返回
        # (避免 401 干扰日志,也避免 -2012 后还在用空 cookie 反复尝试)
        if not wr_skey or not wr_vid:
            logger.debug(
                f"懒加载 httpx: wr_skey/wr_vid 都为空,跳过 (book={book_id[:12]})"
            )
            return None

        try:
            import httpx as _httpx
            url = f"https://i.weread.qq.com/book/chapterInfos?bookIds={book_id}&synckeys=0"
            headers = {
                "accept": "application/json, text/plain, */*",
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "referer": f"https://weread.qq.com/web/reader/{book_id}",
            }
            cookies_dict = {}
            for k, v in (self.cookies or {}).items():
                if k in ("wr_skey", "wr_vid", "wr_gid", "wr_fp", "wr_ql", "wr_rt"):
                    cookies_dict[k] = v
            if not cookies_dict.get("wr_skey") or not cookies_dict.get("wr_vid"):
                try:
                    from src.cookie_manager import cookie_manager as _cm
                    disk_cookies = _cm.load() or []
                    for c in disk_cookies:
                        if isinstance(c, dict):
                            n = c.get("name")
                            v = c.get("value", "")
                            if n in ("wr_skey", "wr_vid", "wr_gid", "wr_fp", "wr_ql", "wr_rt") and n not in cookies_dict and v:
                                cookies_dict[n] = v
                except Exception:
                    pass
            async with _httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                resp = await client.get(url, headers=headers, cookies=cookies_dict)

            if resp.status_code != 200:
                logger.warning(f"懒加载 httpx: {book_id} 状态码 {resp.status_code} body[:200]={resp.text[:200]}")
                return None

            try:
                data = resp.json()
            except Exception as e:
                logger.warning(f"懒加载 httpx:解析失败 {e}")
                return None

            items = None
            if isinstance(data, dict):
                if "data" in data and isinstance(data["data"], list):
                    items = data["data"]
                elif "results" in data and isinstance(data["results"], list):
                    items = data["results"]
                elif "chapters" in data and isinstance(data["chapters"], list):
                    items = [{"bookId": book_id, "chapters": data["chapters"]}]
            elif isinstance(data, list):
                items = data

            if not items:
                logger.warning(f"懒加载 httpx: {book_id} 响应无 items")
                return None

            target = None
            for it in items:
                if not isinstance(it, dict):
                    continue
                bid = it.get("bookId") or it.get("book_id") or it.get("bookid")
                if str(bid) == str(book_id):
                    target = it
                    break
            if target is None:
                target = items[0]

            chapters = target.get("chapters") or []
            if not isinstance(chapters, list) or not chapters:
                logger.warning(f"懒加载 httpx: {book_id} 没有 chapters 字段")
                return None

            norm = []
            for c in chapters:
                if not isinstance(c, dict):
                    continue
                uid = c.get("chapterUid") or c.get("chapter_uid") or c.get("uid") or c.get("id")
                if uid is None:
                    continue
                uid_str = str(uid)
                idx = c.get("chapterIdx") or c.get("chapter_idx") or c.get("idx") or c.get("order") or 0
                try:
                    idx_int = int(idx)
                except Exception:
                    idx_int = 0
                norm.append({
                    "chapterUid": uid_str,
                    "chapterIdx": idx_int,
                    "title": c.get("title") or c.get("chapterTitle") or "",
                })

            if not norm:
                return None

            norm.sort(key=lambda x: x["chapterIdx"])
            first = norm[0]
            logger.info(
                f"📡 httpx 抓章节成功: {book_id[:12]} 拿到 {len(norm)} 章,首章 c={first['chapterUid'][:16]}"
            )
            return {
                "chapters": norm,
                "first_chapter_uid": first["chapterUid"],
                "first_chapter_idx": first["chapterIdx"],
            }
        except Exception as e:
            logger.warning(f"懒加载 httpx 异常: {e}")
            return None

    @staticmethod
    def _persist_chapter_to_config(book_id: str, chapters: List[Dict[str, Any]]) -> None:
        """把 chapters 列表写回 shared/config.yaml 中对应 book_id 的条目。
        只补 chapters 字段,不动其他书籍配置。
        """
        try:
            import yaml
            from pathlib import Path
            cfg_path = Path(config.get("__config_file__", "shared/config.yaml"))
            if not cfg_path.exists():
                # 退而求其次:用路径猜
                alt = Path("shared/config.yaml")
                if alt.exists():
                    cfg_path = alt
                else:
                    return
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            books = (cfg.get("reading") or {}).get("books") or []
            target = None
            for b in books:
                if str(b.get("book_id", "")) == str(book_id):
                    target = b
                    break
            if target is None:
                logger.debug(f"config.yaml 里没找到 book_id={book_id},不写回")
                return
            # 写入 chapters(转成 config 里的格式:{chapter_id, chapter_index})
            target["chapters"] = [
                {
                    "chapter_id": str(c["chapterUid"]),
                    "chapter_index": int(c.get("chapterIdx") or 0),
                    "name": c.get("title", ""),
                }
                for c in chapters[:50]  # 只写前 50 章,避免 yaml 太大
            ]
            with open(cfg_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
            logger.info(
                f"已写回 {len(target['chapters'])} 个章节到 config.yaml (book_id={book_id[:12]})"
            )
        except ImportError:
            logger.warning("缺少 PyYAML,跳过持久化章节到 config.yaml")
        except Exception as e:
            logger.warning(f"持久化章节异常: {e}")

    def _advance_chapter_index(self) -> None:
        """每轮请求前推进当前书的 ci 计数(模拟翻页)。

        关键修复:这是腾讯服务端识别"真实阅读 vs 原地不动"的核心信号。
        同一 chapter_id 长时间不变 + ci 不变 → 服务端静默丢弃,阅读时长不入账。
        每本书独立的计数器,跨书切换时从0开始(避免污染)。
        """
        if not hasattr(self, "_book_ci_counters"):
            self._book_ci_counters = {}

        book_id = self.last_book_id
        if not book_id:
            return

        cur = self._book_ci_counters.get(book_id, 0) + 1
        self._book_ci_counters[book_id] = cur
        self.last_chapter_index = cur

    async def stop_reading(self):
        self.should_stop = True
