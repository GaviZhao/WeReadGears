import asyncio
import random
import signal
from datetime import datetime, timedelta
from typing import Optional

from src.utils.logger import logger
from src.config import config
from src.session_manager import session_manager
from src.history_manager import history_manager


class DaemonManager:
    """守护进程管理器"""

    def __init__(self):
        self.enabled = config.get("daemon.enabled", False)
        self.session_interval = config.get("daemon.session_interval", "120-180")
        self.max_daily_sessions = config.get("daemon.max_daily_sessions", 12)
        self._daily_session_count = 0
        self._shutdown_requested = False
        self._last_reset_date: Optional[str] = None

    def _parse_interval(self) -> tuple[int, int]:
        """解析间隔范围"""
        parts = str(self.session_interval).split("-")
        if len(parts) == 2:
            try:
                return int(parts[0]), int(parts[1])
            except ValueError:
                pass
        return 120, 180

    def _get_random_interval(self) -> int:
        """获取随机间隔（秒）"""
        min_interval, max_interval = self._parse_interval()
        return random.randint(min_interval, max_interval) * 60

    def _reset_daily_count_if_needed(self):
        """如果到了新的一天，重置计数"""
        today = datetime.now().strftime("%Y-%m-%d")
        if self._last_reset_date != today:
            self._daily_session_count = 0
            self._last_reset_date = today
            logger.info("新的一天，重置会话计数")

    async def _wait_until_next_day(self):
        """等待到次日零点"""
        now = datetime.now()
        tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        wait_seconds = (tomorrow - now).total_seconds()

        logger.info(f"达到每日会话上限，等待到次日: {wait_seconds/3600:.1f} 小时")

        while wait_seconds > 0 and not self._shutdown_requested:
            await asyncio.sleep(min(60, wait_seconds))
            wait_seconds -= 60
            self._reset_daily_count_if_needed()

        if not self._shutdown_requested:
            self._daily_session_count = 0

    async def _wait_with_check(self, seconds: int):
        """可中断的等待"""
        while seconds > 0 and not self._shutdown_requested:
            await asyncio.sleep(min(10, seconds))
            seconds -= 10
            self._reset_daily_count_if_needed()

    async def _run_session(self):
        """运行单次阅读会话"""
        try:
            logger.info(f"开始守护进程会话 ({self._daily_session_count + 1}/{self.max_daily_sessions})")
            history_manager.add_startup_entry("daemon")
            results = await session_manager.run_multi_user()

            for result in results:
                if result.status == "completed":
                    logger.info(f"用户 {result.user_name} 阅读完成")
                else:
                    logger.warning(f"用户 {result.user_name} 阅读失败: {result.message}")

        except Exception as e:
            logger.error(f"守护进程会话异常: {e}")

    async def run(self):
        """主守护循环"""
        if not self.enabled:
            logger.info("守护进程未启用")
            return

        logger.info("守护进程启动")
        self._shutdown_requested = False

        while not self._shutdown_requested:
            self._reset_daily_count_if_needed()

            if self._daily_session_count >= self.max_daily_sessions:
                await self._wait_until_next_day()
                if self._shutdown_requested:
                    break

            await self._run_session()
            self._daily_session_count += 1

            interval = self._get_random_interval()
            logger.info(f"下次会话在 {interval / 60:.0f} 分钟后")
            await self._wait_with_check(interval)

        logger.info("守护进程已停止")

    def request_shutdown(self):
        """请求关闭"""
        logger.info("收到关闭请求")
        self._shutdown_requested = True


daemon_manager = DaemonManager()
