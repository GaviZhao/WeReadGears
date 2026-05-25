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

    DEFAULT_HEADERS = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        "content-type": "application/json;charset=UTF-8",
        "origin": "https://weread.qq.com",
        "referer": "https://weread.qq.com/",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }

    def __init__(self, credentials: UserCredentials):
        self.credentials = credentials
        self.is_reading = False
        self.should_stop = False
        self.start_time: Optional[datetime] = None
        self.elapsed_seconds = 0
        self.total_break_seconds = 0
        self.break_count = 0
        self.total_reads = 0
        self.failed_reads = 0
        self.books_read = 0
        self.errors: Dict[str, int] = {}
        self.last_book_id = ""
        self.last_book_name = ""
        self.last_chapter_id = ""
        self.last_chapter_index: Optional[int] = None
        self._last_progress_time = 0
        self._needs_refresh = False
        self._last_logged_book = ""
        self._last_logged_chapter = ""
        self._fail_reason = ""

        self.http_client: Optional[HttpClient] = None
        self._init_http_client()

        self.data: Dict[str, Any] = {}
        self.cookies: Dict[str, str] = {}
        self.headers: Dict[str, str] = {}
        self._init_from_credentials()

    def _init_from_credentials(self):
        """从凭证初始化请求数据和cookie/header"""
        ui = self.credentials.user_info or {}
        cp = ui.get("captured_payload", {}) or {}
        if cp and isinstance(cp, dict):
            self.data = dict(cp)
            self.data.pop("s", None)
        else:
            self.data = {}
        cc = ui.get("captured_cookies", {}) or {}
        if cc and isinstance(cc, dict):
            self.cookies = dict(cc)
        else:
            self.cookies = {"wr_skey": self.credentials.wr_skey, "wr_vid": self.credentials.wr_vid}
        ch = ui.get("captured_headers", {}) or {}
        if ch and isinstance(ch, dict):
            self.headers = dict(self.DEFAULT_HEADERS)
            for k, v in ch.items():
                kl = k.lower()
                if ":" in k or kl in ("https", "cookie", "host", "content-length", "connection", "baggage", "sentry"):
                    continue
                if kl.startswith("sec-"):
                    continue
                self.headers[k] = v
        else:
            self.headers = dict(self.DEFAULT_HEADERS)

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
        """重新注入ps/pc/appId（同 weread-bot _apply_user_identity_to_payload）"""
        ui = self.credentials.user_info or {}
        cp = ui.get("captured_payload", {}) or {}
        if cp.get("ps"):
            self.data["ps"] = cp["ps"]
        if cp.get("pc"):
            self.data["pc"] = cp["pc"]
        if cp.get("appId"):
            self.data["appId"] = cp["appId"]

    def _prepare_payload(self, last_time: int):
        """准备单次阅读请求的payload（同 weread-bot _prepare_read_payload）"""
        self.data.pop("s", None)

        if self.last_book_id:
            self.data["b"] = self.last_book_id
        if self.last_chapter_id:
            self.data["c"] = self.last_chapter_id
        if self.last_chapter_index is not None:
            self.data["ci"] = self.last_chapter_index

        self._apply_user_identity()

        current_time = int(time.time())
        self.data["ct"] = current_time
        self.data["rt"] = current_time - last_time if last_time else 0
        self.data["ts"] = int(current_time * 1000) + random.randint(0, 1000)
        self.data["rn"] = random.randint(0, 1000)
        self.data["sg"] = hashlib.sha256(f"{self.data['ts']}{self.data['rn']}{KEY}".encode()).hexdigest()
        self.data["s"] = self._calculate_hash(self._encode_data(self.data))

    async def _refresh_cookie(self) -> bool:
        """刷新cookie（同 weread-bot _refresh_cookie）"""
        logger.info("刷新cookie...")
        try:
            cookie_data = {"rq": "/web/book/read", "ql": config.get("hack.cookie_refresh_ql", False)}
            response, _ = await self.http_client.post(
                self.RENEW_URL,
                headers=self.headers,
                cookies=self.cookies,
                json_data=cookie_data,
            )
            new_skey = self._extract_skey(response)
            if not new_skey:
                logger.warning("Cookie刷新失败：未找到新wr_skey")
                return False
            self.cookies["wr_skey"] = new_skey
            logger.info(f"Cookie刷新成功: {new_skey[:8]}***")
            return True
        except Exception as e:
            logger.warning(f"Cookie刷新请求异常: {e}")
            return False

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

    def _check_response(self, response: httpx.Response) -> bool:
        """检查响应（同 weread-bot _handle_protocol_response）"""
        if response.status_code != 200:
            logger.warning(f"API非200响应: {response.status_code}")
            return False
        try:
            data = response.json()
        except Exception:
            logger.warning(f"API响应JSON解析失败: {response.text[:200]}")
            return False

        if "succ" in data and "synckey" in data:
            return True

        if "succ" in data:
            logger.warning(f"API响应缺少synckey: {str(data)[:200]}")
            return False

        logger.warning(f"API响应无succ字段，触发cookie刷新: keys={list(data.keys())}")
        self._needs_refresh = True
        return False

    async def start_reading(self, on_progress=None) -> ReadingResult:
        if self.is_reading:
            logger.warning("已在阅读中，跳过")
            return ReadingResult(status="already_reading")

        self.is_reading = True
        self.should_stop = False
        self.start_time = datetime.now()
        self.elapsed_seconds = 0
        self.total_break_seconds = 0
        self.break_count = 0
        self.total_reads = 0
        self.failed_reads = 0
        self.books_read = 0
        self.errors = {}
        self._last_progress_time = 0
        self._needs_refresh = False
        self._last_logged_book = ""
        self._last_logged_chapter = ""

        self._fail_reason = ""

        reading_config = config.get("reading", {})
        target_duration_str = reading_config.get("target_duration", "60-90")
        if isinstance(target_duration_str, str) and "-" in target_duration_str:
            parts = target_duration_str.split("-")
            target_minutes = random.randint(int(parts[0]), int(parts[1]))
        else:
            target_minutes = int(target_duration_str)
        target_seconds = target_minutes * 60

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
        chapter_continuity = reading_config.get("chapter_continuity", 0.7)

        logger.info(f"API 阅读开始，目标时长: {target_minutes} 分钟")

        book_id, chapter_id, chapter_index, book_name, _ = self._select_book_and_chapter()
        self.last_book_id = book_id or self.last_book_id
        self.last_book_name = book_name or self.last_book_name
        self.last_chapter_id = chapter_id or self.last_chapter_id
        self.last_chapter_index = chapter_index
        logger.info(f"初始选择书籍: {self.last_book_name or self.last_book_id}")

        if not self.data.get("ps"):
            self.data.update({
                "appId": self.credentials.user_info.get("captured_appId", "wb115321887466h953405538") if self.credentials.user_info else "wb115321887466h953405538",
                "ps": self.credentials.user_id or self.credentials.wr_vid or "",
                "pc": self.credentials.user_id or self.credentials.wr_vid or "",
            })

        _logged_first = False
        last_time = int(time.time()) - 30
        last_book_switch_time = 0
        refresh_attempted = False

        try:
            while not self.should_stop:
                if mode == "smart_random":
                    if self.elapsed_seconds - last_book_switch_time > 300 and random.random() > book_continuity:
                        bid, cid, ci, bname, _ = self._select_book_and_chapter()
                        if bid != self.last_book_id:
                            self.last_book_id = bid
                            self.last_book_name = bname or self.last_book_name
                            self.books_read += 1
                            last_book_switch_time = self.elapsed_seconds
                            logger.info(f"切换书籍: {self.last_book_name or bid}")

                self._prepare_payload(last_time)

                try:
                    if not _logged_first:
                        _logged_first = True
                        logger.info(f"API首次请求: ps={self.data.get('ps','')[:16]} pc={self.data.get('pc','')[:16]} appId={self.data.get('appId','')[:20]}")

                    response, _ = await self.http_client.post(
                        self.READ_URL,
                        headers=self.headers,
                        cookies=self.cookies,
                        json_data=self.data,
                    )

                    if self._check_response(response):
                        self.total_reads += 1
                        last_time = int(time.time())
                        refresh_attempted = False
                    else:
                        if self._needs_refresh and not refresh_attempted:
                            refresh_attempted = True
                            self._needs_refresh = False
                            if await self._refresh_cookie():
                                logger.info("cookie刷新后重试请求...")
                                response, _ = await self.http_client.post(
                                    self.READ_URL,
                                    headers=self.headers,
                                    cookies=self.cookies,
                                    json_data=self.data,
                                )
                                if self._check_response(response):
                                    self.total_reads += 1
                                    last_time = int(time.time())

                except Exception as e:
                    self.failed_reads += 1
                    self.errors[str(e)[:60]] = self.errors.get(str(e)[:60], 0) + 1
                    logger.warning(f"API 请求异常，立即切换模拟模式: {e}")
                    self.should_stop = True
                    self._fail_reason = str(e)[:100]

                interval = random.uniform(interval_min, interval_max)

                if random.random() < break_prob:
                    break_time = random.randint(break_min, break_max)
                    self.break_count += 1
                    logger.info(f"模拟休息 #{self.break_count}：{break_time} 秒 (累计休息 {self.total_break_seconds} 秒)")
                    await asyncio.sleep(break_time)
                    self.total_break_seconds += break_time

                await asyncio.sleep(interval)
                self.elapsed_seconds = int((datetime.now() - self.start_time).total_seconds())
                active_seconds = self.elapsed_seconds - self.total_break_seconds

                if active_seconds >= target_seconds:
                    break

                if on_progress and active_seconds - (self._last_progress_time or 0) >= 10:
                    active_min = active_seconds // 60
                    active_sec = active_seconds % 60
                    pct = min(int((active_seconds / target_seconds) * 100), 100)
                    log_msg = f"进度 {pct}% ({active_min}分{active_sec}秒)"
                    if self.total_reads > 0 or self.failed_reads > 0:
                        log_msg += f" | 成功 {self.total_reads} 失败 {self.failed_reads}"
                    if self.break_count > 0:
                        log_msg += f" | 休息{self.break_count}次/{self.total_break_seconds}秒"
                    progress = {
                        "elapsed": active_seconds,
                        "target": target_seconds,
                        "progress": pct,
                        "current_book": self.last_book_id,
                        "book_name": self.last_book_name,
                        "total_reads": self.total_reads,
                        "failed_reads": self.failed_reads,
                        "log": log_msg,
                    }
                    book_key = f"{self.last_book_name or self.last_book_id}:{self.last_chapter_index or 0}"
                    if book_key != self._last_logged_book:
                        self._last_logged_book = book_key
                        ch_info = f" 第{self.last_chapter_index}章" if self.last_chapter_index is not None else ""
                        progress["log"] = f"阅读: {self.last_book_name or self.last_book_id}{ch_info}"
                    await on_progress(progress)
                    self._last_progress_time = active_seconds

            actual_minutes = active_seconds / 60

            if self.should_stop and self._fail_reason:
                logger.warning(f"API 异常终止: {self._fail_reason}")
                return ReadingResult(
                    status="error",
                    elapsed_seconds=active_seconds,
                    elapsed_minutes=actual_minutes,
                    target_minutes=target_minutes,
                    total_reads=self.total_reads,
                    failed_reads=self.failed_reads,
                    books_read=0,
                    errors={**self.errors, "fail_reason": self._fail_reason},
                    start_time=self.start_time.isoformat(),
                    end_time=datetime.now().isoformat(),
                )

            logger.info(f"阅读完成，实际: {actual_minutes:.1f}分钟 成功:{self.total_reads} 失败:{self.failed_reads} 休息{self.break_count}次/累计{self.total_break_seconds}秒")
            return ReadingResult(
                status="completed",
                elapsed_seconds=active_seconds,
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

    def _select_book_and_chapter(self) -> tuple:
        books = config.get("reading.books", [])
        if not books:
            return self.last_book_id or "", self.last_chapter_id or "", self.last_chapter_index, "", ""
        book = random.choice(books)
        book_id = book.get("book_id", "")
        book_name = book.get("name", "")
        raw_chapters = book.get("chapters", [])
        chapter_id = ""
        chapter_index = None
        chapter_name = ""
        if raw_chapters:
            ch = random.choice(raw_chapters)
            if isinstance(ch, dict):
                chapter_id = ch.get("chapter_id") or ch.get("id") or ""
                chapter_index = ch.get("chapter_index") or ch.get("index")
                chapter_name = ch.get("name", "") or ""
            else:
                chapter_id = str(ch)
        return book_id, chapter_id, chapter_index, book_name, chapter_name

    async def stop_reading(self):
        self.should_stop = True
