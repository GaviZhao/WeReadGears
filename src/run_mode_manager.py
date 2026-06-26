"""运行时模式管理器 - 负责 immediate / scheduled / daemon 三种模式的热切换。

设计要点:
- 单一入口:Web UI 点守护/定时按钮,真正切换进程行为的是 switch_mode()
- 不重启进程:停止旧的 background task + 启动新的 background task
- 配置同步:切换时同步写 YAML,容器重启后也能保持
- 锁保护:防止并发切换导致状态错乱
"""
import asyncio
from typing import Optional, Callable

from src.utils.logger import logger
from src.config import config
from src.daemon import daemon_manager
from src.scheduler import scheduler
from src.notifier import notifier


# scheduled 模式下,scheduler 触发后实际跑的阅读任务
async def _scheduled_reading_task():
    """scheduled 模式触发的实际阅读任务(委托给 main 的 reading_with_fallback)"""
    try:
        from src.main import reading_with_fallback
        await reading_with_fallback()
    except Exception as e:
        logger.error(f"[scheduled] 阅读任务异常: {e}")


class RunModeManager:
    """运行时模式管理器"""

    # 合法模式
    VALID_MODES = ("immediate", "scheduled", "daemon", "idle")

    def __init__(self):
        self._current_mode: str = "idle"
        self._current_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._on_start_callbacks: list = []
        self._switch_in_progress: bool = False

    # ============ 公共 API ============

    def register_on_start(self, callback: Callable):
        """注册模式启动时的回调(用于注册 weekly_reminder 等)"""
        self._on_start_callbacks.append(callback)

    def get_status(self) -> dict:
        """获取当前模式状态(供 Web UI 展示)"""
        is_task_alive = (
            self._current_task is not None
            and not self._current_task.done()
        )
        return {
            "current_mode": self._current_mode,
            "is_running": is_task_alive,
            "switch_in_progress": self._switch_in_progress,
            "daemon_enabled": config.get("daemon.enabled", False),
            "daemon_session_interval": config.get("daemon.session_interval", "120-180"),
            "daemon_max_daily_sessions": config.get("daemon.max_daily_sessions", 12),
            "daemon_daily_session_count": getattr(daemon_manager, "_daily_session_count", 0),
            "scheduler_running": scheduler.is_running if scheduler else False,
            "scheduler_next_run": (
                scheduler.get_next_run_time().isoformat()
                if scheduler and scheduler.is_running and scheduler.get_next_run_time()
                else None
            ),
        }

    async def switch_mode(self, mode: str):
        """切换运行模式(主入口)"""
        mode = (mode or "").lower().strip()
        if mode not in self.VALID_MODES:
            raise ValueError(f"未知模式: {mode}(支持: {', '.join(self.VALID_MODES)})")

        async with self._lock:
            self._switch_in_progress = True
            try:
                if mode == self._current_mode and mode != "idle":
                    logger.info(f"[run-mode] 已在 {mode} 模式,跳过切换")
                    return {"status": "noop", "mode": mode}

                logger.info(f"[run-mode] 切换模式: {self._current_mode} → {mode}")
                await self._stop_current()

                if mode == "daemon":
                    await self._start_daemon()
                elif mode == "scheduled":
                    await self._start_scheduled()
                elif mode == "immediate":
                    await self._start_immediate()
                elif mode == "idle":
                    self._current_mode = "idle"
                    logger.info("[run-mode] 已停止所有自动任务(仅手动模式)")

                logger.info(f"[run-mode] 切换完成: current_mode={self._current_mode}")
                return {"status": "ok", "mode": self._current_mode}
            finally:
                self._switch_in_progress = False

    # ============ 内部:停止当前模式 ============

    async def _stop_current(self):
        """停止当前所有后台任务(守护/调度/立即)"""
        # 1. 通知 daemon manager 优雅停止
        try:
            daemon_manager.request_shutdown()
        except Exception as e:
            logger.warning(f"[run-mode] daemon.request_shutdown 异常: {e}")

        # 2. 停 scheduler(若有)
        try:
            if scheduler and scheduler.is_running:
                await scheduler.stop()
        except Exception as e:
            logger.warning(f"[run-mode] scheduler.stop 异常: {e}")

        # 3. 取消外层 task(daemon.run 的 wrapper)
        if self._current_task and not self._current_task.done():
            logger.info(f"[run-mode] 取消当前 task ({self._current_mode})")
            self._current_task.cancel()
            try:
                await asyncio.wait_for(self._current_task, timeout=5.0)
            except asyncio.CancelledError:
                pass
            except asyncio.TimeoutError:
                logger.warning("[run-mode] 取消 task 超时(5s),强制结束")
            except Exception as e:
                logger.warning(f"[run-mode] 取消 task 异常: {e}")
        self._current_task = None

        # 等一小段时间让旧 task 释放资源
        await asyncio.sleep(0.2)
        self._current_mode = "idle"

    # ============ 内部:启动各模式 ============

    async def _start_daemon(self):
        """启动 daemon 模式后台循环"""
        # 强制启用 daemon 配置(避免用户在 UI 点守护却 daemon.enabled=false 的尴尬)
        if not config.get("daemon.enabled", False):
            config.update({"daemon": {"enabled": True}})
            logger.info("[run-mode] 自动启用 daemon.enabled")

        # hot-reload daemon_manager 配置(它的字段是 __init__ 时缓存的)
        daemon_manager.enabled = config.get("daemon.enabled", False)
        daemon_manager.session_interval = config.get("daemon.session_interval", "120-180")
        daemon_manager.max_daily_sessions = int(config.get("daemon.max_daily_sessions", 12))
        # 重置 daily count 不太合适(否则跨模式切换会丢当天的进度),保留即可
        daemon_manager._shutdown_requested = False

        # 触发 on_start 回调(注册 weekly_reminder)
        for cb in self._on_start_callbacks:
            try:
                cb()
            except Exception as e:
                logger.warning(f"[run-mode] on_start 回调失败: {e}")

        # 启动后台循环
        self._current_mode = "daemon"
        self._current_task = asyncio.create_task(daemon_manager.run())
        logger.info(
            f"[run-mode] 守护进程已启动:interval={daemon_manager.session_interval}min,"
            f"max_daily={daemon_manager.max_daily_sessions}"
        )

    async def _start_scheduled(self):
        """启动 scheduled 模式(cron 调度)"""
        if not config.get("schedule.enabled", True):
            config.update({"schedule": {"enabled": True}})
            logger.info("[run-mode] 自动启用 schedule.enabled")

        # 触发 on_start 回调
        for cb in self._on_start_callbacks:
            try:
                cb()
            except Exception as e:
                logger.warning(f"[run-mode] on_start 回调失败: {e}")

        scheduler.set_task(_scheduled_reading_task)
        await scheduler.start()

        self._current_mode = "scheduled"
        # 占位 task:scheduler 自己负责触发,我们只需一个常驻 task 占位
        async def _idle():
            try:
                while True:
                    await asyncio.sleep(3600)
            except asyncio.CancelledError:
                return
        self._current_task = asyncio.create_task(_idle())
        logger.info("[run-mode] 定时任务模式已启动")

    async def _start_immediate(self):
        """启动 immediate 模式(立即跑一次 + cron 调度)"""
        from src.main import startup_reading_and_schedule

        # 触发 on_start 回调
        for cb in self._on_start_callbacks:
            try:
                cb()
            except Exception as e:
                logger.warning(f"[run-mode] on_start 回调失败: {e}")

        self._current_mode = "immediate"
        self._current_task = asyncio.create_task(startup_reading_and_schedule())
        logger.info("[run-mode] 立即模式已启动(立即跑一次 + cron 调度)")


# 全局单例
run_mode_manager = RunModeManager()