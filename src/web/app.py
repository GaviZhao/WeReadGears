import sys
import base64
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from contextlib import asynccontextmanager
import asyncio
import json
import random

from datetime import datetime, timedelta
import locale

from src.config import config
from src.utils.logger import logger
from src.browser import browser_manager
from src.reader import reader
from src.scheduler import scheduler
from src.notifier import notifier, NotificationType
from src.cookie_manager import cookie_manager
from src.credential_manager import credential_manager
from src.history_manager import history_manager
from src.session_manager import session_manager


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Web 服务启动")
    from src.user_data_manager import user_data_manager
    migrated = user_data_manager.migrate_from_old_structure()
    if migrated:
        logger.info(f"已迁移用户数据: {migrated}")
    await notifier.notify_startup()
    yield
    logger.info("Web 服务关闭")


app = FastAPI(title="weread-auto-reader", lifespan=lifespan)


class NoCacheMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response


app.add_middleware(NoCacheMiddleware)

BASE_DIR = Path(__file__).parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "web" / "templates"))


@app.get("/favicon.ico")
async def favicon():
    return Response(content=b"", media_type="image/x-icon")


def _times_to_cron(times):
    if not times:
        return "0 9 * * *"
    hours = []
    minutes = set()
    for t in times:
        parts = t.strip().split(":")
        if len(parts) == 2:
            try:
                h = int(parts[0])
                m = int(parts[1])
                hours.append(h)
                minutes.add(m)
            except ValueError:
                continue
    if not hours:
        return "0 9 * * *"
    hours_str = ",".join(str(h) for h in sorted(set(hours)))
    if len(minutes) == 1:
        return f"{list(minutes)[0]} {hours_str} * * *"
    return f"{','.join(str(m) for m in sorted(minutes))} {hours_str} * * *"


def _get_weekly_info() -> dict:
    day_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    now = datetime.now()
    today_name = day_names[now.weekday()]
    days_until_sunday = (6 - now.weekday()) % 7
    if days_until_sunday == 0 and now.hour >= 10:
        days_until_sunday = 7
    next_sunday = now.replace(hour=10, minute=0, second=0, microsecond=0) + timedelta(days=days_until_sunday)
    return {
        "today_date": now.strftime("%m月%d日"),
        "today_weekday": today_name,
        "today_full": f"{now.strftime('%m月%d日')} {today_name}",
        "next_sunday": next_sunday.strftime("%m月%d日"),
        "next_sunday_full": f"{next_sunday.strftime('%m月%d日')} 周日 10:00",
    }

def _cron_to_times(cron):
    try:
        parts = cron.strip().split()
        minute_str = parts[0]
        hour_str = parts[1]
        minutes = [int(m) for m in minute_str.split(",")]
        hours = [int(h) for h in hour_str.split(",")]
        times = []
        for h in sorted(hours):
            for m in sorted(minutes):
                times.append(f"{h:02d}:{m:02d}")
        return times
    except (IndexError, ValueError):
        return ["09:00", "12:00", "18:00"]


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    reading_config = config.get("reading", {})
    schedule_config = config.get("schedule", {})
    notification_config = config.get("notification", {})
    network_config = config.get("network", {})
    daemon_config = config.get("daemon", {})
    history_config = config.get("history", {})

    browser_status = browser_manager.get_login_status()
    logged_in_user = browser_status.get("user") if browser_status.get("status") == "success" else None
    cookies_valid = logged_in_user is not None
    if not cookies_valid:
        for cred_user in credential_manager.get_all_users():
            cred = credential_manager.load(cred_user)
            if cred and cred.is_valid():
                cookies_valid = True
                logged_in_user = cred_user
                break
    valid_users = cookie_manager.get_all_valid_users()
    cookies_info = cookie_manager.get_expiry_info(valid_users[0]) if valid_users else None
    cron = schedule_config.get("cron_expression", "0 9,12,18 * * *")
    times = schedule_config.get("times", _cron_to_times(cron))

    users = config.get_users()
    statistics = history_manager.get_statistics()

    return templates.TemplateResponse("index.html", {
        "request": request,
        "reading": {
            "target_duration": reading_config.get("target_duration", "60-90"),
            "mode": reading_config.get("mode", "smart_random"),
            "reading_interval": reading_config.get("reading_interval", "10-20"),
            "books": reading_config.get("books", [])
        },
        "schedule": {
            "enabled": schedule_config.get("enabled", True),
            "times": times,
            "timezone": schedule_config.get("timezone", "Asia/Shanghai")
        },
        "notification": {
            "enabled": notification_config.get("enabled", False),
            "only_on_failure": notification_config.get("only_on_failure", False),
            "weekly_reward_reminder": notification_config.get("weekly_reward_reminder", True),
            "bark": notification_config.get("bark", {"enabled": False, "server": "https://api.day.app", "device_key": ""}),
            "pushplus": notification_config.get("pushplus", {"enabled": False, "token": ""}),
            "telegram": notification_config.get("telegram", {"enabled": False, "bot_token": "", "chat_id": ""}),
            "wxpusher": notification_config.get("wxpusher", {"enabled": False, "spt": ""}),
            "ntfy": notification_config.get("ntfy", {"enabled": False, "server": "https://ntfy.sh", "topic": ""}),
            "feishu": notification_config.get("feishu", {"enabled": False, "webhook_url": "", "msg_type": "text"}),
            "wework": notification_config.get("wework", {"enabled": False, "webhook_url": "", "msg_type": "text"}),
            "dingtalk": notification_config.get("dingtalk", {"enabled": False, "webhook_url": "", "msg_type": "text"}),
            "gotify": notification_config.get("gotify", {"enabled": False, "server": "", "token": "", "priority": 5}),
            "serverchan3": notification_config.get("serverchan3", {"enabled": False, "uid": "", "sendkey": ""}),
            "pushdeer": notification_config.get("pushdeer", {"enabled": False, "pushkey": ""}),
        },
        "network": network_config,
        "daemon": daemon_config,
        "history": {
            "enabled": history_config.get("enabled", True),
            "max_entries": history_config.get("max_entries", 50)
        },
        "cookies_valid": cookies_valid,
        "logged_in_user": logged_in_user,
        "cookies_info": cookies_info,
        "reader_status": reader.get_status(),
        "scheduler_status": scheduler.get_status(),
        "users": users,
        "statistics": statistics,
        "version": config.get("app.version", "1.0.0"),
        "weekly_info": _get_weekly_info()
    })


@app.post("/save-config")
async def save_config(request: Request):
    data = await request.json()
    books = json.loads(data.get("books_json", "[]")) if data.get("books_json") else []

    schedule_times_list = data.get("schedule_times", ["09:00"])
    cron = _times_to_cron(schedule_times_list)

    updates = {
        "reading": {
            "target_duration": data.get("target_duration", "60-90"),
            "mode": data.get("reading_mode", "smart_random"),
            "reading_interval": data.get("reading_interval", "30-48"),
            "book_continuity": int(data.get("book_continuity", 80)) / 100,
            "break_probability": int(data.get("break_probability", 15)) / 100,
            "books": books
        },
        "schedule": {
            "enabled": data.get("schedule_enabled", True),
            "cron_expression": cron,
            "times": schedule_times_list,
            "timezone": data.get("timezone", "Asia/Shanghai")
        },
        "daemon": {
            "enabled": data.get("daemon_enabled", False),
            "session_interval": data.get("daemon_session_interval", "120-180"),
            "max_daily_sessions": int(data.get("daemon_max_daily", 12))
        },
        "network": {
            "timeout": int(data.get("network_timeout", 30)),
            "retry_times": int(data.get("network_retry_times", 3)),
            "retry_delay": data.get("network_retry_delay", "5-15"),
            "rate_limit": int(data.get("network_rate_limit", 10))
        },
        "notification": {
            "enabled": data.get("notification_enabled", True),
            "only_on_failure": data.get("notification_only_on_failure", False),
            "weekly_reward_reminder": data.get("weekly_reward_reminder", True),
            "bark": {
                "enabled": data.get("bark_enabled", False),
                "server": data.get("bark_server", "https://api.day.app"),
                "device_key": data.get("bark_device_key", "")
            },
            "pushplus": {
                "enabled": data.get("pushplus_enabled", False),
                "token": data.get("pushplus_token", "")
            },
            "telegram": {
                "enabled": data.get("telegram_enabled", False),
                "bot_token": data.get("telegram_bot_token", ""),
                "chat_id": data.get("telegram_chat_id", "")
            },
            "wxpusher": {
                "enabled": data.get("wxpusher_enabled", False),
                "spt": data.get("wxpusher_spt", "")
            },
            "ntfy": {
                "enabled": data.get("ntfy_enabled", False),
                "server": data.get("ntfy_server", "https://ntfy.sh"),
                "topic": data.get("ntfy_topic", "")
            },
            "feishu": {
                "enabled": data.get("feishu_enabled", False),
                "webhook_url": data.get("feishu_webhook_url", ""),
                "msg_type": data.get("feishu_msg_type", "text")
            },
            "wework": {
                "enabled": data.get("wework_enabled", False),
                "webhook_url": data.get("wework_webhook_url", ""),
                "msg_type": data.get("wework_msg_type", "text")
            },
            "dingtalk": {
                "enabled": data.get("dingtalk_enabled", False),
                "webhook_url": data.get("dingtalk_webhook_url", ""),
                "msg_type": data.get("dingtalk_msg_type", "text")
            },
            "gotify": {
                "enabled": data.get("gotify_enabled", False),
                "server": data.get("gotify_server", ""),
                "token": data.get("gotify_token", ""),
                "priority": int(data.get("gotify_priority", 5))
            },
            "serverchan3": {
                "enabled": data.get("serverchan3_enabled", False),
                "uid": data.get("serverchan3_uid", ""),
                "sendkey": data.get("serverchan3_sendkey", "")
            },
            "pushdeer": {
                "enabled": data.get("pushdeer_enabled", False),
                "pushkey": data.get("pushdeer_pushkey", "")
            }
        }
    }
    config.update(updates)
    return JSONResponse({"status": "ok", "message": "配置已保存"})


@app.post("/trigger-reading")
async def trigger_reading():
    if reader.is_reading:
        return JSONResponse({"status": "error", "message": "阅读任务正在进行中"})
    duration = config.get("reading.target_duration", "30-60")
    books = config.get("reading.books", [])
    book_name = books[0].get("name") if books else None
    asyncio.create_task(reader.start_reading())
    await notifier.notify_reading_start(book_name=book_name, duration=duration)
    return JSONResponse({"status": "ok", "message": "阅读任务已启动"})


@app.post("/trigger-api-reading")
async def trigger_api_reading():
    if session_manager.is_running():
        return JSONResponse({"status": "error", "message": "阅读任务正在进行中"})

    async def run_with_fallback():
        task = asyncio.create_task(session_manager.run_multi_user())
        while not task.done():
            await asyncio.sleep(2)
            if session_manager._has_failed:
                logger.warning(f"API异常检测到，立即切换模拟模式: {session_manager._fail_reason}")
                session_manager.stop()
                await notifier.send(
                    f"API请求异常: {session_manager._fail_reason}\n已自动切换到模拟模式",
                    NotificationType.READING_FAILED
                )
                await reader.start_reading()
                return
        results = task.result()
        fail_reason = session_manager._fail_reason
        if fail_reason:
            logger.warning(f"API异常，切换模拟模式: {fail_reason}")
            await notifier.send(
                f"API请求异常: {fail_reason}\n已自动切换到模拟模式",
                NotificationType.READING_FAILED
            )
            await reader.start_reading()
        else:
            all_failed = True
            for r in (results or []):
                if r.status == "completed":
                    all_failed = False
                    break
            if results and all_failed:
                logger.warning("API全部失败，切换模拟模式")
                await notifier.send(
                    "API模式全部请求失败\n已自动切换到模拟模式",
                    NotificationType.READING_FAILED
                )
                await reader.start_reading()

    asyncio.create_task(run_with_fallback())
    return JSONResponse({"status": "ok", "message": "API 阅读任务已启动"})


@app.post("/stop-reading")
async def stop_reading():
    session_manager.stop()
    return JSONResponse({"status": "ok", "message": "已发送停止信号"})


@app.get("/api-reading-progress")
async def get_api_reading_progress():
    progress = session_manager.get_progress()
    r_status = reader.get_status() if reader.is_reading else {}
    is_running = session_manager.is_running() or reader.is_reading
    mode = "browser" if reader.is_reading else ("api" if session_manager.is_running() else "idle")
    elapsed = progress.get("elapsed", r_status.get("elapsed_seconds", 0))
    target = progress.get("target", 0)
    if is_running and (not target or target <= 0):
        dur_str = config.get("reading.target_duration", "60-90")
        if isinstance(dur_str, str) and "-" in dur_str:
            parts = dur_str.split("-")
            target = random.randint(int(parts[0]), int(parts[1])) * 60
        else:
            target = int(dur_str) * 60
        if target > 0:
            progress["target"] = target
            session_manager._progress = progress
    return JSONResponse({
        "is_running": is_running,
        "mode": mode,
        "elapsed": elapsed,
        "target": target,
        "progress": min(int(elapsed / target * 100), 100) if target > 0 else 0,
        "message": progress.get("message", ""),
        "total_reads": progress.get("total_reads", 0),
        "failed_reads": progress.get("failed_reads", 0),
        "current_book": progress.get("current_book", ""),
        "book_name": progress.get("book_name", r_status.get("current_book", "")),
        "status": progress.get("status", "idle"),
    })


@app.get("/api-reading-logs")
async def get_api_reading_logs():
    return JSONResponse(session_manager.get_logs())


@app.post("/logout")
async def logout():
    browser_manager._current_user = ""
    browser_manager._login_status = "idle"
    browser_manager.reset_login_status()
    try:
        if browser_manager.context:
            await browser_manager.context.clear_cookies()
        if browser_manager.page:
            await browser_manager.page.goto("https://weread.qq.com/", timeout=10000)
    except:
        pass
    return JSONResponse({"status": "ok", "message": "已退出登录"})


@app.post("/capture-curl")
async def capture_curl():
    try:
        browser_status = browser_manager.get_login_status()
        user_name = browser_status.get("user", "default")
        ok = await browser_manager.capture_and_save_curl(user_name)
        if ok:
            return JSONResponse({"status": "ok", "message": "CURL参数捕获成功"})
        return JSONResponse({"status": "error", "message": "捕获失败: 未获取到ps/pc"})
    except Exception as e:
        return JSONResponse({"status": "error", "message": f"捕获异常: {e}"})


@app.get("/api/captured-info")
async def get_captured_info():
    return JSONResponse(browser_manager.get_captured_info())


@app.get("/api/shelf-books")
async def get_shelf_books():
    books = await browser_manager.fetch_shelf_books()
    return JSONResponse({"books": books})


@app.get("/api/search-books")
async def search_books(q: str = ""):
    if not q.strip():
        return JSONResponse({"results": []})
    results = await browser_manager.search_book_by_name(q.strip())
    return JSONResponse({"results": results})


@app.get("/reader-status")
async def get_reader_status():
    return JSONResponse(reader.get_status())


@app.get("/scheduler-status")
async def get_scheduler_status():
    return JSONResponse(scheduler.get_status())


@app.get("/history")
async def get_history(limit: int = 10, offset: int = 0):
    history = history_manager.get_history(limit + offset)
    return JSONResponse({
        "history": history[offset:offset + limit],
        "total": len(history)
    })


@app.get("/statistics")
async def get_statistics():
    return JSONResponse(history_manager.get_statistics())


@app.post("/history/clear")
async def clear_history():
    history_manager.clear_history()
    return JSONResponse({"status": "ok", "message": "历史记录已清除"})


@app.get("/users")
async def get_users():
    users = config.get_users()
    user_status = []
    shown = set()
    for u in users:
        user_name = u.get("name", "")
        display_name = u.get("display_name", "") or user_name
        cred = credential_manager.load(user_name)
        has_credential = cred is not None
        real_name = cred.user_name if cred and cred.user_name else display_name
        user_status.append({
            **u,
            "logged_in": has_credential,
            "real_name": real_name,
            "display_name": display_name,
            "credential_info": credential_manager.get_expiry_info(user_name) if has_credential else None
        })
    if not users:
        cred = credential_manager.load("default")
        is_logged = cookie_manager.is_valid() or (cred is not None)
        real_name = (cred.user_name if cred and cred.user_name else None) or "default"
        user_status.append({
            "name": "default",
            "display_name": "default",
            "logged_in": is_logged,
            "real_name": real_name,
            "books": [],
            "reading_overrides": {},
            "credential_info": {"saved_at": cred.saved_at if cred and cred.saved_at else "未知", "expires_at": cred.expires_at if cred and cred.expires_at else "未知"} if cred else None
        })
    return JSONResponse({"users": user_status})


@app.post("/users")
async def add_user(request: Request):
    data = await request.json()
    user = {
        "name": data.get("name", ""),
        "books": data.get("books", []),
        "reading_overrides": data.get("reading_overrides", {})
    }
    if not user["name"]:
        return JSONResponse({"status": "error", "message": "用户名不能为空"})
    config.add_user(user)
    return JSONResponse({"status": "ok", "message": f"用户 {user['name']} 已添加"})


@app.delete("/users/{user_name}")
async def delete_user(user_name: str):
    config.remove_user(user_name)
    credential_manager.delete(user_name)
    return JSONResponse({"status": "ok", "message": f"用户 {user_name} 已删除"})


@app.put("/users/{user_name}/rename")
async def rename_user(user_name: str, request: Request):
    data = await request.json()
    new_name = data.get("new_name", "").strip()
    if not new_name:
        return JSONResponse({"status": "error", "message": "新用户名不能为空"})
    users = config.get_users()
    for u in users:
        if u.get("name") == user_name:
            u["display_name"] = new_name
            config.set("users", users)
            config.save()
            return JSONResponse({"status": "ok", "message": f"已重命名为 {new_name}"})
    if not users and user_name == "default":
        config.add_user({"name": "default", "display_name": new_name, "books": [], "reading_overrides": {}})
        return JSONResponse({"status": "ok", "message": f"已创建并命名为 {new_name}"})
    return JSONResponse({"status": "error", "message": "用户不存在"})


@app.post("/users/{user_name}/login")
async def login_user(user_name: str):
    browser_manager.set_current_user(user_name)
    qr_bytes = await browser_manager.start_login_with_qr(user_name)
    if qr_bytes is None:
        status = browser_manager.get_login_status()
        if status["status"] == "success":
            return JSONResponse({"status": "already_logged_in", "message": "已登录"})
        return JSONResponse({"status": "error", "message": "登录失败：无法获取二维码"})
    qr_base64 = base64.b64encode(qr_bytes).decode("utf-8")
    return JSONResponse({
        "status": "ok",
        "qr_image": qr_base64,
        "message": "请使用微信扫描二维码登录"
    })


@app.post("/users/{user_name}/reading")
async def trigger_user_reading(user_name: str):
    user_cred = credential_manager.load(user_name)
    if not user_cred or not user_cred.is_valid():
        return JSONResponse({"status": "error", "message": f"用户 {user_name} 未登录或凭证已过期"})
    asyncio.create_task(session_manager.run_multi_user())
    return JSONResponse({"status": "ok", "message": f"用户 {user_name} 的阅读任务已启动"})


@app.get("/login/start")
async def login_start():
    browser_manager.reset_login_status()
    qr_bytes = await browser_manager.start_login_with_qr()

    if qr_bytes is None:
        status = browser_manager.get_login_status()
        if status["status"] == "success":
            return JSONResponse({"status": "already_logged_in", "message": "已登录"})

    qr_base64 = base64.b64encode(qr_bytes).decode("utf-8") if qr_bytes else None
    return JSONResponse({
        "status": "ok",
        "qr_image": qr_base64,
        "message": "请使用微信扫描二维码登录"
    })


@app.get("/login/status")
async def login_status():
    status = browser_manager.get_login_status()
    if status["status"] == "need_username":
        return JSONResponse({"status": "need_username", "message": "请输入用户名"})
    return JSONResponse(status)


@app.post("/login/complete-with-username")
async def login_complete_with_username(request: Request):
    data = await request.json()
    user_name = data.get("user_name", "").strip()
    if not user_name:
        return JSONResponse({"status": "error", "message": "用户名不能为空"})
    result = await browser_manager.complete_login_with_username(user_name)
    if result["status"] == "ok":
        config.add_user({"name": user_name, "display_name": user_name, "books": [], "reading_overrides": {}})
    return JSONResponse(result)


@app.get("/login/debug")
async def login_debug():
    return JSONResponse(browser_manager.get_login_debug())


@app.get("/browser/screenshot")
async def browser_screenshot():
    img = await browser_manager.get_preview_screenshot()
    if img:
        return Response(content=img, media_type="image/png")
    return Response(status_code=204)


@app.post("/browser/navigate")
async def browser_navigate(request: Request):
    data = await request.json()
    url = data.get("url", "https://weread.qq.com/")
    try:
        page = await browser_manager.get_page()
        await page.goto(url, timeout=30000)
        await asyncio.sleep(2)
        return JSONResponse({"status": "ok", "message": f"已导航到 {url}"})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)})


@app.post("/browser/click")
async def browser_click(request: Request):
    data = await request.json()
    x = data.get("x", 0)
    y = data.get("y", 0)
    page = await browser_manager.get_page()
    await page.mouse.click(x, y)
    return JSONResponse({"status": "ok"})


@app.post("/browser/scroll")
async def browser_scroll(request: Request):
    data = await request.json()
    dy = data.get("dy", 0)
    page = await browser_manager.get_page()
    await page.evaluate(f"window.scrollBy(0, {dy})")
    return JSONResponse({"status": "ok"})


@app.post("/restart-browser")
async def restart_browser():
    try:
        await browser_manager.restart()
        return JSONResponse({"status": "ok", "message": "浏览器已重启"})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)})


@app.post("/test-notification")
async def test_notification():
    success = await notifier.send(
        "微信读书自动阅读 - 测试通知\n\n如果你收到此消息，说明通知配置正确。",
        NotificationType.GENERAL
    )
    if success:
        return JSONResponse({"status": "ok", "message": "测试通知已发送"})
    return JSONResponse({"status": "error", "message": "通知发送失败，请检查 Token/Key 是否正确"})


@app.get("/health")
async def health_check():
    return JSONResponse({
        "status": "healthy",
        "reader_running": reader.is_reading
    })
