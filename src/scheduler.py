import asyncio
from typing import Callable, Optional
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from datetime import datetime, timedelta

from src.utils.logger import logger
from src.config import config


class TaskScheduler:
    def __init__(self):
        self.scheduler: Optional[AsyncIOScheduler] = None
        self.task_id: Optional[str] = None
        self.is_running = False
        self._task_func: Optional[Callable] = None
        self._weekly_reminder_func: Optional[Callable] = None

    def set_task(self, task_func: Callable):
        self._task_func = task_func

    async def start(self):
        if self.scheduler:
            logger.warning("调度器已在运行")
            return

        if not config.get("schedule.enabled", True):
            logger.info("定时任务未启用")
            return

        self.scheduler = AsyncIOScheduler(timezone=config.get("schedule.timezone", "Asia/Shanghai"))

        cron_expr = config.get("schedule.cron_expression", "0 */2 * * *")
        parts = cron_expr.split()
        if len(parts) == 5:
            trigger = CronTrigger(
                minute=parts[0],
                hour=parts[1],
                day=parts[2],
                month=parts[3],
                day_of_week=parts[4],
                timezone=config.get("schedule.timezone", "Asia/Shanghai")
            )
        else:
            trigger = IntervalTrigger(hours=2, timezone=config.get("schedule.timezone", "Asia/Shanghai"))

        self.task_id = "weread_reading_task"
        self.scheduler.add_job(
            self._run_task,
            trigger=trigger,
            id=self.task_id,
            replace_existing=True
        )

        self.scheduler.start()
        self.is_running = True

        next_run = self.scheduler.get_job(self.task_id)
        if next_run:
            logger.info(f"调度器已启动，下次运行时间: {next_run.next_run_time}")

    async def _run_task(self):
        if self._task_func:
            logger.info("触发定时阅读任务")
            try:
                await self._task_func()
            except Exception as e:
                logger.error(f"定时任务执行失败: {e}")

    async def _run_weekly_reminder(self):
        if config.get("notification.weekly_reward_reminder", True):
            logger.info("发送每周阅读奖励提醒")
            try:
                await self._weekly_reminder_func()
            except Exception as e:
                logger.error(f"每周提醒发送失败: {e}")

    def register_weekly_reminder(self, func):
        self._weekly_reminder_func = func
        if self.scheduler:
            self.scheduler.add_job(
                self._run_weekly_reminder,
                trigger=CronTrigger(day_of_week="sun", hour=10, minute=0,
                                    timezone=config.get("schedule.timezone", "Asia/Shanghai")),
                id="weekly_reward_reminder",
                replace_existing=True
            )
            logger.info("每周阅读奖励提醒已注册（周日 10:00）")

    async def trigger_now(self):
        logger.info("手动触发立即执行")
        if self._task_func:
            await self._run_task()

    async def stop(self):
        if self.scheduler:
            self.scheduler.shutdown(wait=False)
            self.scheduler = None
            self.is_running = False
            logger.info("调度器已停止")

    def get_next_run_time(self) -> Optional[datetime]:
        if self.scheduler and self.task_id:
            job = self.scheduler.get_job(self.task_id)
            if job:
                return job.next_run_time
        return None

    def get_status(self) -> dict:
        return {
            "is_running": self.is_running,
            "next_run": self.get_next_run_time().isoformat() if self.get_next_run_time() else None,
            "task_id": self.task_id
        }


scheduler = TaskScheduler()
