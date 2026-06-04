import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional, Callable
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from src.utils.logger import logger
from src.config import config
from src.utils.logger import logger
from src.config import config


class TaskScheduler:
    def __init__(self):
        self.scheduler: Optional[AsyncIOScheduler] = None
        self.task_id: Optional[str] = None
        self.is_running = False
        self._task_func: Optional[Callable] = None
        self._weekly_reminder_func: Optional[Callable] = None

    def _get_now(self) -> datetime:
        tz_name = config.get("schedule.timezone", "Asia/Shanghai")
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo(tz_name))
        except Exception:
            return datetime.now()

    def _get_weekly_day(self) -> int:
        """读取用户在 config 里配置的星期几 (0=周一 … 6=周日),越界或缺失则默认周日"""
        try:
            d = int(config.get("notification.weekly_reward_day", 6))
            if 0 <= d <= 6:
                return d
        except Exception:
            pass
        return 6

    def _get_weekly_time(self) -> tuple:
        """读取用户在 config 里配置的时间 (HH:MM),越界或缺失则默认 10:00"""
        try:
            t = str(config.get("notification.weekly_reward_time", "10:00")).strip()
            h, m = t.split(":", 1)
            h, m = int(h), int(m)
            if 0 <= h <= 23 and 0 <= m <= 59:
                return h, m
        except Exception:
            pass
        return 10, 0

    def _next_weekly_text(self) -> str:
        """给人类读的'下次提醒时间'字符串"""
        day = self._get_weekly_day()
        h, m = self._get_weekly_time()
        day_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        return f"{day_names[day]} {h:02d}:{m:02d}"

    def _build_weekly_trigger(self) -> CronTrigger:
        """构造每周奖励的 CronTrigger(根据用户配置 day_of_week + hour + minute)"""
        tz_name = config.get("schedule.timezone", "Asia/Shanghai")
        day = self._get_weekly_day()
        h, m = self._get_weekly_time()
        return CronTrigger(
            day_of_week=str(day),  # APScheduler 接受 0-6
            hour=h,
            minute=m,
            second=0,
            timezone=tz_name,
        )

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

        # 如果之前 register_weekly_reminder 在 scheduler 还是 None 时被调用过,
        # 现在 self.scheduler 已经创建,自动注册每周奖励任务
        if self._weekly_reminder_func is not None and config.get("notification.weekly_reward_reminder", True):
            self.scheduler.add_job(
                self._run_weekly_reminder,
                trigger=self._build_weekly_trigger(),
                id="weekly_reward_reminder",
                replace_existing=True,
            )
            try:
                job = self.scheduler.get_job("weekly_reward_reminder")
                if job and job.next_run_time:
                    logger.info(f"每周奖励提醒已自动注册 → 每周 {self._next_weekly_text()},下次: {job.next_run_time.strftime('%Y-%m-%d %H:%M:%S')}")
            except Exception:
                pass

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
        if not config.get("notification.weekly_reward_reminder", True):
            logger.info("每周奖励提醒已关闭，跳过")
            return
        if not self._weekly_reminder_func:
            return
        logger.info(f"发送每周阅读奖励提醒 (时间: {self._next_weekly_text()})")
        try:
            await self._weekly_reminder_func()
        except Exception as e:
            logger.error(f"每周提醒发送失败: {e}")

    def register_weekly_reminder(self, func):
        self._weekly_reminder_func = func
        if not config.get("notification.weekly_reward_reminder", True):
            logger.info("每周奖励提醒未启用，不注册")
            return
        if not self.scheduler:
            # 调度器还没启动,延迟到 start() 时统一注册
            return
        trigger = self._build_weekly_trigger()
        self.scheduler.add_job(
            self._run_weekly_reminder,
            trigger=trigger,
            id="weekly_reward_reminder",
            replace_existing=True,
        )
        # 读取并打印下次触发时间(用 jodatime 输出)
        try:
            job = self.scheduler.get_job("weekly_reward_reminder")
            if job and job.next_run_time:
                logger.info(f"每周阅读奖励提醒已注册 → 每周 {self._next_weekly_text()},下次: {job.next_run_time.strftime('%Y-%m-%d %H:%M:%S')}")
        except Exception as e:
            logger.debug(f"读取每周奖励下次触发时间失败: {e}")

    def get_weekly_status(self) -> dict:
        """返回每周奖励调度状态(供前端展示)"""
        enabled = config.get("notification.weekly_reward_reminder", True)
        out = {
            "enabled": enabled,
            "day": self._get_weekly_day(),
            "time": f"{self._get_weekly_time()[0]:02d}:{self._get_weekly_time()[1]:02d}",
            "next_run": None,
        }
        if enabled and self.scheduler:
            try:
                job = self.scheduler.get_job("weekly_reward_reminder")
                if job and job.next_run_time:
                    out["next_run"] = job.next_run_time.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass
        return out

    async def trigger_weekly_now(self) -> bool:
        """手动触发每周奖励提醒(用于前端'测试'按钮)"""
        if not self._weekly_reminder_func:
            return False
        try:
            await self._weekly_reminder_func()
            return True
        except Exception as e:
            logger.error(f"手动触发每周奖励提醒失败: {e}")
            return False

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
