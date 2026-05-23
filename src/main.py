import sys
import asyncio
import signal
import argparse
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils.logger import logger
from src.config import config
from src.browser import browser_manager
from src.reader import reader
from src.scheduler import scheduler
from src.notifier import notifier, NotificationType
from src.cookie_manager import cookie_manager
from src.session_manager import session_manager
from src.daemon import daemon_manager
from src.history_manager import history_manager
import uvicorn


def parse_args():
    parser = argparse.ArgumentParser(description="weread-auto-reader")
    parser.add_argument(
        "--mode",
        choices=["immediate", "scheduled", "daemon"],
        default=None,
        help="运行模式: immediate(立即执行), scheduled(定时任务), daemon(守护进程)"
    )
    parser.add_argument(
        "--user",
        type=str,
        default=None,
        help="指定用户执行（单用户模式）"
    )
    parser.add_argument(
        "--validate-config",
        action="store_true",
        help="仅校验配置不执行"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="干跑模式，不发起阅读请求"
    )
    parser.add_argument(
        "--show-last-run",
        action="store_true",
        help="显示上次运行结果"
    )
    return parser.parse_args()


async def api_reading_task():
    """API 阅读任务（多用户）"""
    try:
        results = await session_manager.run_multi_user()

        for result in results:
            if result.status == "completed":
                logger.info(f"用户 {result.user_name} 阅读完成")
            else:
                logger.warning(f"用户 {result.user_name} 阅读失败: {result.message}")

        return results

    except Exception as e:
        logger.error(f"API 阅读任务异常: {e}")
        await notifier.notify_runtime_error(str(e))
        return []


async def reading_with_fallback():
    """带故障切换的阅读任务：API模式失败自动切模拟模式，反之亦然"""
    try:
        if not cookie_manager.is_valid():
            logger.warning("Cookie无效，跳过阅读")
            return

        await notifier.notify_reading_start(duration=config.get("reading.target_duration"))
        results = await api_reading_task()

        fallback_needed = False
        fallback_reason = ""
        if not results:
            fallback_needed = True
            fallback_reason = "API任务异常退出"
        else:
            for r in results:
                rr = r.result
                if rr and rr.errors and rr.errors.get("fail_reason"):
                    fallback_needed = True
                    fallback_reason = f"API请求异常: {rr.errors['fail_reason']}"
                    break
                if rr and rr.total_reads == 0 and rr.failed_reads > 0:
                    fallback_needed = True
                    fallback_reason = f"API请求全部失败（{rr.failed_reads}次）"
                    break
                if r.status != "completed":
                    fallback_needed = True
                    fallback_reason = f"API阅读失败: {r.message}"
                    break

        if not fallback_needed:
            for r in results:
                if r.status == "completed" and r.result:
                    await notifier.notify_reading_complete(
                        elapsed_minutes=r.result.elapsed_minutes,
                        target_minutes=r.result.target_minutes,
                        books_read=r.result.books_read
                    )

        if fallback_needed:
            logger.warning(f"API模式失效 - {fallback_reason}，切换到模拟模式")
            await notifier.send(
                f"API模式失效: {fallback_reason}\n已自动切换到模拟模式",
                NotificationType.READING_FAILED
            )
            try:
                await notifier.notify_reading_start(duration=config.get("reading.target_duration"))
                reader_start = datetime.now()
                browser_result = await reader.start_reading()
                elapsed = (datetime.now() - reader_start).total_seconds() / 60
                from src.api_reader import ReadingResult
                br = ReadingResult(
                    status=browser_result.get("status", "error"),
                    elapsed_seconds=browser_result.get("elapsed_seconds", 0),
                    elapsed_minutes=browser_result.get("elapsed_minutes", elapsed),
                    target_minutes=browser_result.get("target_minutes", int(elapsed)),
                    total_reads=0,
                    failed_reads=0,
                    books_read=browser_result.get("books_read", 1),
                )
                user_name = "default"
                for u in config.get("users", []):
                    if u.get("name") == "default":
                        user_name = u.get("display_name", "") or "default"
                        break
                history_manager.add_reading_entry(br, user_name, execution_type="browser")
                if browser_result["status"] == "completed":
                    logger.info(f"模拟模式阅读完成 ({elapsed:.1f}分钟)")
                    session_manager.add_log(f"✅ 模拟阅读成功 ({elapsed:.0f}分钟)")
                    await notifier.notify_reading_complete(
                        elapsed_minutes=elapsed,
                        target_minutes=browser_result.get("target_minutes", int(elapsed)),
                        books_read=browser_result.get("books_read", 1)
                    )
                else:
                    error_msg = browser_result.get("error", "未知错误")
                    logger.error(f"模拟模式也失败: {error_msg}")
                    await notifier.send(
                        f"两个模式均失效\nAPI: {fallback_reason}\n模拟: {error_msg}",
                        NotificationType.READING_FAILED
                    )
            except Exception as e:
                logger.error(f"模拟模式异常: {e}")
        else:
            logger.info("API模式阅读成功，无需切换")

    except Exception as e:
        logger.error(f"阅读任务异常: {e}")


async def startup_reading_and_schedule():
    await reading_with_fallback()
    logger.info("初始阅读完成，切换到定时任务模式")
    scheduler.set_task(reading_with_fallback)
    await scheduler.start()


async def run_uvicorn():
    config_dict = config.all
    port = config_dict.get("app", {}).get("port", 8000)

    server_config = uvicorn.Config(
        "src.web.app:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
        lifespan="off"
    )
    server = uvicorn.Server(server_config)
    await server.serve()


async def run_immediate_mode():
    """立即执行模式"""
    logger.info("运行模式: 立即执行")
    history_manager.add_startup_entry("immediate")
    await api_reading_task()


async def run_scheduled_mode():
    """定时任务模式"""
    logger.info("运行模式: 定时任务")
    scheduler.set_task(reading_with_fallback)
    await scheduler.start()


async def run_daemon_mode():
    """守护进程模式"""
    logger.info("运行模式: 守护进程")
    await daemon_manager.run()


async def main():
    args = parse_args()

    mode = args.mode or config.get("app.startup_mode", "immediate")

    logger.info("=" * 50)
    logger.info("weread-auto-reader 启动")
    logger.info(f"运行模式: {mode}")
    logger.info("=" * 50)

    if args.validate_config:
        logger.info("配置校验模式")
        logger.info(f"用户数: {len(config.get_users())}")
        logger.info(f"通知通道: {len(notifier.channels)}")
        return

    if args.show_last_run:
        last = history_manager.get_last_entry()
        if last:
            logger.info(f"上次运行: {last}")
        else:
            logger.info("无运行记录")
        return

    if args.dry_run:
        logger.info("干跑模式")
        return

    try:
        await browser_manager.initialize()

        if mode == "scheduled":
            await run_scheduled_mode()
            scheduler.register_weekly_reminder(notifier.notify_weekly_reward)
        elif mode == "daemon":
            await run_daemon_mode()
            scheduler.register_weekly_reminder(notifier.notify_weekly_reward)
        elif mode == "immediate":
            scheduler.register_weekly_reminder(notifier.notify_weekly_reward)
            asyncio.create_task(startup_reading_and_schedule())

        port = config.get("app.port", 8000)
        logger.info(f"Web 服务启动在端口 {port}")

        await run_uvicorn()

    except Exception as e:
        logger.error(f"主程序异常: {e}")
        await notifier.notify_container_exit(str(e))
    finally:
        await cleanup()


async def cleanup():
    logger.info("开始清理资源...")
    try:
        await scheduler.stop()
        await browser_manager.close()
        logger.info("资源清理完成")
    except Exception as e:
        logger.error(f"清理异常: {e}")


async def signal_handler():
    logger.info("收到退出信号，开始清理...")
    daemon_manager.request_shutdown()
    await cleanup()
    sys.exit(0)


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(signal_handler()))

    try:
        loop.run_until_complete(main())
    finally:
        loop.close()
