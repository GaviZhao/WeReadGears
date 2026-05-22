import asyncio
import random
import time
from typing import Optional, Dict, Any, Callable, List

import httpx

from src.utils.logger import logger
from src.config import config


class RateLimiter:
    """异步速率限制器，按请求/分钟限制"""

    def __init__(self, rate_limit: int):
        self.rate_limit = max(0, rate_limit)
        self._interval = (60.0 / self.rate_limit) if self.rate_limit > 0 else 0
        self._lock = asyncio.Lock()
        self._last_acquire = 0.0

    async def acquire(self):
        """按需等待确保不超过速率"""
        if self.rate_limit <= 0:
            return

        async with self._lock:
            now = time.monotonic()
            wait_time = self._interval - (now - self._last_acquire)
            if wait_time > 0:
                await asyncio.sleep(wait_time)
                now = time.monotonic()
            self._last_acquire = now


class HttpClient:
    """HTTP 客户端，支持重试和速率限制"""

    def __init__(
        self,
        timeout: int = 30,
        retry_times: int = 3,
        retry_delay: str = "5-15",
        rate_limit: int = 10,
    ):
        self.timeout = timeout
        self.retry_times = retry_times
        self.retry_delay = retry_delay
        self.rate_limiter = RateLimiter(rate_limit)
        self._client: Optional[httpx.AsyncClient] = None
        self.request_times: List[float] = []

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout),
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=20),
            )
        return self._client

    def _parse_delay_range(self) -> tuple[float, float]:
        """解析延迟范围字符串 '5-15' -> (5, 15)"""
        parts = str(self.retry_delay).split("-")
        if len(parts) == 2:
            try:
                return float(parts[0]), float(parts[1])
            except ValueError:
                pass
        return 5.0, 15.0

    def _get_random_delay(self) -> float:
        """获取随机延迟"""
        min_delay, max_delay = self._parse_delay_range()
        return random.uniform(min_delay, max_delay)

    async def post(
        self,
        url: str,
        headers: Dict[str, str] = None,
        cookies: Dict[str, str] = None,
        json_data: Dict[str, Any] = None,
        data: Any = None,
        progress_callback: Callable = None,
    ) -> tuple[httpx.Response, float]:
        """发送 POST 请求，支持重试和速率限制"""
        client = await self._get_client()
        last_error = None

        for attempt in range(max(1, self.retry_times)):
            start_time = time.time()
            try:
                await self.rate_limiter.acquire()

                response = await client.post(
                    url,
                    headers=headers,
                    cookies=cookies,
                    json=json_data,
                    data=data,
                )
                elapsed = time.time() - start_time
                self.request_times.append(elapsed)

                if response.status_code < 400:
                    return response, elapsed

                last_error = Exception(f"HTTP {response.status_code}")
                if attempt < self.retry_times - 1:
                    delay = self._get_random_delay()
                    logger.warning(
                        f"请求失败 ({attempt + 1}/{self.retry_times}), "
                        f"{delay:.1f}s 后重试: {url}"
                    )
                    await asyncio.sleep(delay)

            except httpx.TimeoutException as e:
                elapsed = time.time() - start_time
                self.request_times.append(elapsed)
                last_error = e
                logger.warning(f"请求超时 ({attempt + 1}/{self.retry_times}): {url}")
                if attempt < self.retry_times - 1:
                    await asyncio.sleep(self._get_random_delay())

            except Exception as e:
                elapsed = time.time() - start_time
                self.request_times.append(elapsed)
                last_error = e
                logger.warning(f"请求异常 ({attempt + 1}/{self.retry_times}): {e}")
                if attempt < self.retry_times - 1:
                    await asyncio.sleep(self._get_random_delay())

        raise last_error or Exception("请求失败")

    async def get(
        self,
        url: str,
        headers: Dict[str, str] = None,
        cookies: Dict[str, str] = None,
    ) -> httpx.Response:
        """发送 GET 请求"""
        client = await self._get_client()
        response = await client.get(url, headers=headers, cookies=cookies)
        return response

    async def close(self):
        """关闭客户端"""
        if self._client:
            await self._client.aclose()
            self._client = None

    def get_average_response_time(self) -> float:
        """获取平均响应时间"""
        if not self.request_times:
            return 0
        return sum(self.request_times) / len(self.request_times)


http_client = HttpClient()
