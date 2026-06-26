import sys
import asyncio
import signal
import argparse
from datetime import datetime
from pathlib import Path
import httpx

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
from src.run_mode_manager import run_mode_manager
import uvicorn


def parse_args():
    parser = argparse.ArgumentParser(description="WeReadGears")
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
                    # 关键:如果是 -2012 登录失效,主动清掉磁盘 cookie(避免下次再试同样的死 key)
                    fr = rr.errors["fail_reason"]
                    if "errCode=-2012" in fr or "登录已失效" in fr or "登录超时" in fr:
                        try:
                            user_name = r.user_name
                            if user_name and user_name != "default":
                                cookie_manager.save([], user_name)
                                logger.warning(f"[main] {user_name} 登录失效,已清空其磁盘 cookie,下次会走 browser 模式或重新扫码")
                        except Exception as e:
                            logger.debug(f"清 cookie 异常(非致命): {e}")
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
    # 先启动调度器(让 weekly reminder 立即注册),再后台跑初始阅读
    scheduler.set_task(reading_with_fallback)
    await scheduler.start()
    logger.info("初始阅读后台执行中,调度器已就绪(每周奖励提醒已就位)")
    # 后台异步执行初始阅读,不阻塞调度器
    asyncio.create_task(reading_with_fallback())


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


async def periodic_cookie_refresh():
    """后台定时刷新 wr_skey,避免被服务器判为过期。

    微信读书的 wr_skey 1-2 小时会失效,即使续期后也可能再被服务器判过期。
    每 5 分钟主动调用 /web/login/renewal 一次,保持 wr_skey 一直有效。
    """
    refresh_interval = 300  # 5 分钟
    while True:
        try:
            await asyncio.sleep(refresh_interval)
            # 只在 cookie 有效时才续期;失效时让前端提示用户重新扫码
            if not cookie_manager.is_valid():
                logger.info("[定期续期] Cookie 无效,跳过")
                continue
            if browser_manager._needs_relogin:
                logger.info("[定期续期] 已标记需要重新登录,跳过")
                continue

            # 找出当前正在用的用户
            valid_users = cookie_manager.get_all_valid_users()
            user_name = valid_users[0] if valid_users else "default"
            logger.info(f"[定期续期] 触发 wr_skey 续期 (用户={user_name})")
            cookies = cookie_manager.load(user_name)
            wr_skey = next((c["value"] for c in cookies if c.get("name") == "wr_skey"), None)
            if not wr_skey:
                continue

            try:
                async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
                    resp = await client.post(
                        "https://weread.qq.com/web/login/renewal",
                        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36", "Content-Type": "application/json"},
                        cookies={"wr_skey": wr_skey},
                        json={"rq": "%2Fweb%2Fbook%2Fread", "ql": config.get("hack.cookie_refresh_ql", False)},
                    )
                # 提取新 wr_skey(可能通过 Set-Cookie 或 cookies 字段)
                new_skey = None
                for k, v in resp.cookies.items():
                    if k == "wr_skey":
                        new_skey = v
                        break
                if not new_skey:
                    set_cookie = resp.headers.get("set-cookie", "")
                    for part in set_cookie.split(";"):
                        p = part.strip()
                        if p.startswith("wr_skey="):
                            new_skey = p.split("=", 1)[1]
                            break
                if new_skey and new_skey != wr_skey:
                    # 更新磁盘 + Playwright context
                    updated = []
                    for c in cookies:
                        c2 = dict(c)
                        if c2.get("name") == "wr_skey":
                            c2["value"] = new_skey
                        updated.append(c2)
                    cookie_manager.save(updated, user_name)
                    await browser_manager._ensure_fresh_cookies()
                    logger.info(f"[定期续期] wr_skey 已更新: {wr_skey[:8]}*** -> {new_skey[:8]}***")
                else:
                    logger.info(f"[定期续期] wr_skey 未变化 (可能仍有效)状态码={resp.status_code},新skey={bool(new_skey)}")
            except Exception as e:
                logger.warning(f"[定期续期] 请求异常: {e}")
        except asyncio.CancelledError:
            logger.info("[定期续期] 任务取消")
            return
        except Exception as e:
            logger.warning(f"[定期续期] 循环异常: {e}")


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
    logger.info("WeReadGears 启动")
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

        # 注册模式启动回调:每次切换到任何自动模式时,都重新注册 weekly_reminder
        # (scheduler.stop() 后 job 会被清,这里保证新模式下提醒任务在位)
        run_mode_manager.register_on_start(
            lambda: scheduler.register_weekly_reminder(notifier.notify_weekly_reward)
        )

        # 启动模式(委托给 run_mode_manager,这样后续可通过 /api/run-mode 热切换)
        # 注意:这里用 switch_mode 而不是直接 await daemon_manager.run(),
        # 否则 await 会阻塞整个 main(),uvicorn 永远起不来。
        if mode in ("scheduled", "daemon", "immediate"):
            try:
                await run_mode_manager.switch_mode(mode)
            except Exception as e:
                logger.error(f"初始启动 {mode} 模式失败: {e}")
                # 降级到 immediate,不阻塞 uvicorn 启动
                if mode != "immediate":
                    await run_mode_manager.switch_mode("immediate")
        else:
            logger.warning(f"未知启动模式 '{mode}',默认按 immediate 处理")
            await run_mode_manager.switch_mode("immediate")

        # 启动后台定期 cookie 续期(独立于阅读任务,让 wr_skey 始终保持有效)
        asyncio.create_task(periodic_cookie_refresh())
        logger.info("已启动后台定期 cookie 续期任务 (5 分钟一次)")

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
