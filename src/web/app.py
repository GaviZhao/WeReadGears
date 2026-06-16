import sys
import base64
import io
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
from src.notifier import (
    BarkChannel, PushPlusChannel, TelegramChannel, WxPusherChannel,
    NtfyChannel, FeishuChannel, WeWorkChannel, DingTalkChannel,
    GotifyChannel, ServerChan3Channel, PushDeerChannel,
)
from src.cookie_manager import cookie_manager
from src.credential_manager import credential_manager
from src.history_manager import history_manager
from src.session_manager import session_manager


@asynccontextmanager
async def lifespan(app: FastAPI):
    log_file = config.get("log.file")
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        logger.setup("WeReadGears", log_file=str(log_path), level=config.get("log.level", "INFO"))
    logger.info("Web 服务启动")
    from src.user_data_manager import user_data_manager
    migrated = user_data_manager.migrate_from_old_structure()
    if migrated:
        logger.info(f"已迁移用户数据: {migrated}")
    await notifier.notify_startup()
    yield
    logger.info("Web 服务关闭")


app = FastAPI(title="WeReadGears", lifespan=lifespan)


class NoCacheMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response


app.add_middleware(NoCacheMiddleware)

BASE_DIR = Path(__file__).parent.parent
templates = Jinja2Templates(
    directory=str(BASE_DIR / "web" / "templates"),
    auto_reload=True,  # 模板磁盘变化时自动重载,改 HTML 不用重启服务
)


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
    # 先按 status 决定,但还要校验 user 是不是 config 里真实存在的(避免 _current_user 残留)
    raw_browser_user = browser_status.get("user") if browser_status.get("status") == "success" else None
    config_user_names = {u.get("name") for u in config.get_users()}
    if raw_browser_user and raw_browser_user in config_user_names:
        logged_in_user = raw_browser_user
    else:
        # 浏览器残留的 _current_user(已删用户) → 当作未登录
        logged_in_user = None
    cookies_valid = logged_in_user is not None
    if not cookies_valid:
        # 降级:用 config 里第一个有凭证的用户
        for cred_user in credential_manager.get_all_users():
            if cred_user not in config_user_names:
                # 凭证目录存在但 config 已删 → 跳过
                continue
            cred = credential_manager.load(cred_user)
            if cred and cred.is_valid():
                cookies_valid = True
                logged_in_user = cred_user
                break
    valid_users = [u for u in cookie_manager.get_all_valid_users() if u in config_user_names]
    cookies_info = cookie_manager.get_expiry_info(valid_users[0]) if valid_users else None
    cron = schedule_config.get("cron_expression", "0 9,12,18 * * *")
    times = schedule_config.get("times", _cron_to_times(cron))

    users = config.get_users()
    statistics = history_manager.get_statistics()
    # 自动进位分钟→小时(给首页模板用)
    statistics["today_fmt"] = history_manager.format_minutes(statistics["today"]["total_minutes"])
    statistics["week_fmt"] = history_manager.format_minutes(statistics["week"]["total_minutes"])
    statistics["total_fmt"] = history_manager.format_minutes(statistics["total"]["total_minutes"])

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
            "weekly_reward_day": notification_config.get("weekly_reward_day", 6),
            "weekly_reward_time": notification_config.get("weekly_reward_time", "10:00"),
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
            "weekly_reward_day": int(data.get("weekly_reward_day", 6) or 6),
            "weekly_reward_time": str(data.get("weekly_reward_time", "10:00") or "10:00"),
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
    # 通知通道配置变了 → 重建 notifier.channels,免重启就能用上新通道
    if "notification" in updates:
        try:
            notifier.reload_channels()
        except Exception as e:
            logger.error(f"重建通知通道失败: {e}")
    # 如果每周奖励配置变了,更新调度器触发器(避免重启服务)
    try:
        notif = updates.get("notification", {})
        if "weekly_reward_day" in notif or "weekly_reward_time" in notif or "weekly_reward_reminder" in notif:
            if scheduler and scheduler.scheduler:
                enabled = config.get("notification.weekly_reward_reminder", True)
                if enabled and scheduler._weekly_reminder_func is not None:
                    scheduler.scheduler.add_job(
                        scheduler._run_weekly_reminder,
                        trigger=scheduler._build_weekly_trigger(),
                        id="weekly_reward_reminder",
                        replace_existing=True,
                    )
                    job = scheduler.scheduler.get_job("weekly_reward_reminder")
                    next_str = job.next_run_time.strftime('%Y-%m-%d %H:%M:%S') if job and job.next_run_time else '?'
                    logger.info(f"每周奖励触发器已更新 → 每周 {scheduler._next_weekly_text()},下次: {next_str}")
                elif not enabled and scheduler.scheduler.get_job("weekly_reward_reminder"):
                    scheduler.scheduler.remove_job("weekly_reward_reminder")
                    logger.info("每周奖励提醒已禁用,移除调度")
    except Exception as e:
        logger.error(f"更新每周奖励触发器失败: {e}")
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


@app.post("/api/restart")
async def api_restart():
    """触发 Python 进程退出,容器会自动 restart(用于 dev 改代码后快速生效)"""
    import os
    import asyncio
    async def _kill():
        await asyncio.sleep(0.5)
        os._exit(0)
    asyncio.create_task(_kill())
    return JSONResponse({"status": "ok", "message": "正在重启..."})


@app.post("/api/reload")
async def api_reload():
    """热重载 history_manager / config / reader / scheduler 等模块(无需重启容器)"""
    import importlib
    reloaded = []
    for mod_name in [
        "src.history_manager",
        "src.config",
        "src.reader",
        "src.scheduler",
        "src.api_reader",
    ]:
        try:
            m = importlib.import_module(mod_name)
            importlib.reload(m)
            reloaded.append(mod_name)
        except Exception as e:
            reloaded.append(f"{mod_name}(err:{e})")
    return JSONResponse({"status": "ok", "reloaded": reloaded})


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


# === 书籍阅读进度 ===
# 优先用 api_reader 实例的 _book_ci_counters(每本书独立的 ci 计数)
# 进程重启后会丢(但下次跑起来又会自动重计),够用
#
# 2026-06-12 优化:
#   - chapters_total 写死 = 58(用户要求每本书都默认 58)
#   - 缺失 json 时自动生成一个(chapters_total=58 的占位)
#   - 用户可在 web 端书籍卡手动改 chapters_total (POST 端点),
#     改值存磁盘 chapters json 的 chapters_total 字段,优先级最高
@app.get("/api/book-progress")
async def book_progress(book_id: str = ""):
    """返回指定书的当前 ci(章节顺序号)+ 总章节数(默认 58)"""
    # 2026-06-12: 总章节数写死 58
    CHAPTERS_TOTAL_DEFAULT = 58

    if not book_id.strip():
        return JSONResponse({"book_id": "", "ci": 0, "chapters_total": CHAPTERS_TOTAL_DEFAULT})
    ci = 0
    chapters_total = CHAPTERS_TOTAL_DEFAULT  # 默认 58
    chapters_total_user_set = None  # 用户手动改的值
    chapters_json_path = None
    cached_for_response = None  # 整份磁盘缓存,POST 端点写回时复用
    try:
        api = session_manager.api_reader if hasattr(session_manager, 'api_reader') else None
        if api and getattr(api, '_book_ci_counters', None):
            ci = api._book_ci_counters.get(book_id, 0)
        # 找磁盘缓存(任意用户的 chapters 缓存即可)
        import glob as _glob, json as _json
        from pathlib import Path as _Path
        from datetime import datetime as _dt
        # 优先拿第一个用户的(任意用户都行)
        # 2026-06-12 修:Python glob.glob 顺序不稳定(可能拿到 default/ 占位 json 而不是 Gavi/ 真数据),
        # 改用:1) 排序确保 Gavi 优先 2) 优先选 chapters 非空的 json
        _candidates = sorted(_glob.glob("shared/credentials/*/chapters/" + book_id + ".json"))
        # 优先级:chapters 非空 + 含真 uid 的 json(说明是用户真实数据,不是占位)
        # 用 (has_data, -uid_len) 排序,真数据排前
        def _rank(p: str) -> tuple:
            try:
                with open(p, "r", encoding="utf-8") as f:
                    _c = _json.load(f)
                if _c and isinstance(_c.get("chapters"), list) and _c["chapters"]:
                    first_uid = _c["chapters"][0].get("chapterUid", "") or ""
                    return (0, -len(first_uid), p)
            except Exception:
                pass
            return (1, 0, p)
        _ordered = sorted(_candidates, key=_rank)
        for p in _ordered:
            chapters_json_path = p
            try:
                with open(p, "r", encoding="utf-8") as f:
                    cached = _json.load(f)
            except Exception:
                continue
            cached_for_response = cached
            if cached and isinstance(cached.get("chapters"), list) and cached.get("chapters"):
                # 2026-06-12: 磁盘有真实 chapters
                # 新格式(参考 1d5322805cfd751d5aff1ea.json)只有 1 条 chapterIdx=N,
                # 旧逻辑 len(chapters) < 20 触发回退会误伤 → 改为:
                #   - chapters_total 字段已设(用户明示 N) → 不再判 len,信任字段
                #   - chapters_total 字段没设(原始抓的全量列表) → 用 len,兜底 <20 当脏数据
                has_user_total = isinstance(cached.get("chapters_total"), int) and cached["chapters_total"] > 0
                if has_user_total:
                    # 用户已明示 N → 跳过 len 校验,直接 trust chapters_total
                    pass
                else:
                    if len(cached["chapters"]) < 20:
                        logger.warning(
                            f"⚠️ 磁盘 chapters 数 <20 视为脏数据,回退默认 58: "
                            f"{book_id[:12]} (len={len(cached['chapters'])})"
                        )
                        chapters_total = CHAPTERS_TOTAL_DEFAULT
                    else:
                        chapters_total = len(cached["chapters"])
            break

        # 用户手动改的 chapters_total 优先级最高(任何 disk 缓存里读到的)
        if cached_for_response and isinstance(cached_for_response.get("chapters_total"), int):
            ut = cached_for_response["chapters_total"]
            if ut > 0:
                chapters_total_user_set = ut
                chapters_total = ut

        # 2026-06-12: 缺失 json 时**不再自动生成占位**(用户严禁创建 default 目录)
        # 真实数据由 _ensure_chapter_json 在 start_reading 入口用 Playwright 抓取并落盘
        # GET 端点只读,查不到就返回 N=58 默认值(chapters_total_user_set=False)
        # 真正想生成 json,得在 Web UI 触发阅读开始
        if chapters_json_path is None:
            logger.debug(
                f"[chapters] 缺失 json(不自动生成): {book_id[:12]},默认 N={CHAPTERS_TOTAL_DEFAULT}"
            )
    except Exception:
        pass
    return JSONResponse({
        "book_id": book_id,
        "ci": ci,
        "chapters_total": chapters_total,
    })


@app.post("/api/book-progress")
async def update_book_progress(request: Request):
    """
    更新书籍的 chapters_total(用户在 web 端书籍卡手动改)

    Body JSON: {"book_id": "<id>", "chapters_total": <int>}
    - chapters_total < 1 → 当作"清空用户设置",回退到默认/磁盘
    - 任意用户的 chapters 缓存均可写(优先用第一个找到的)
    - 缺失时自动创建占位 json
    """
    CHAPTERS_TOTAL_DEFAULT = 58
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON body"}, status_code=400)
    book_id = str(body.get("book_id") or "").strip()
    if not book_id:
        return JSONResponse({"ok": False, "error": "missing book_id"}, status_code=400)
    raw_total = body.get("chapters_total", None)
    if raw_total is None or raw_total == "":
        return JSONResponse({"ok": False, "error": "missing chapters_total"}, status_code=400)
    try:
        new_total = int(raw_total)
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "chapters_total must be int"}, status_code=400)
    if new_total < 1:
        new_total = 0  # 0 / 负数 = 清空用户设置

    try:
        from src.user_data_manager import user_data_manager as _udm
        import glob as _glob, json as _json
        from datetime import datetime as _dt

        # 找现存 chapters json(任意用户都行)
        # 2026-06-12 修:Python glob.glob 顺序不稳定(Windows/Linux 都可能 default 优先),
        # 必须显式排序,避免 web 端改章节数时把已有 Gavi/<bid>.json 跳过,创建空 default/<bid>.json
        existing_path = None
        existing_payload = None
        candidates = sorted(_glob.glob("shared/credentials/*/chapters/" + book_id + ".json"))
        # 优先级:当前 api_reader.user_name 匹配的目录 > 字母序最前的 > 其余
        try:
            _current_user = getattr(session_manager.api_reader, "user_name", "") or ""
        except Exception:
            _current_user = ""
        ordered = sorted(
            candidates,
            key=lambda p: (f"/{_current_user}/" not in p.replace("\\", "/"), p),
        )
        for p in ordered:
            existing_path = p
            try:
                with open(p, "r", encoding="utf-8") as f:
                    existing_payload = _json.load(f)
            except Exception:
                existing_payload = None
            break

        # 决定写哪个 user 的目录 + payload
        # 2026-06-12:严禁创建 "default" 目录。user_name 必须从 existing_path 解析,
        # 或从当前 api_reader.user_name 拿 —— 两个都拿不到就 400 报错,绝不写。
        user_name = ""
        if existing_path and existing_payload is not None:
            # 从 path 提取 user_name
            # 形如 shared/credentials/<user>/chapters/<book_id>.json
            parts = existing_path.replace("\\", "/").split("/")
            # ['shared', 'credentials', '<user>', 'chapters', '<book_id>.json']
            if len(parts) >= 5 and parts[2] and parts[2] != "default":
                user_name = parts[2]
            payload = dict(existing_payload)  # 复制
        else:
            # 不存在 → 尝试用当前 api_reader 的 user
            api = session_manager.api_reader if hasattr(session_manager, 'api_reader') else None
            try:
                if api and getattr(api, 'user_name', None):
                    user_name = api.user_name
            except Exception:
                pass
            payload = {
                "bookId": str(book_id),
                "chapters": [],
                "first_chapter_uid": "",
                "first_chapter_idx": 0,
                "current_ci": 0,
            }

        # 2026-06-12: 守卫 —— user_name 仍为空 / "default" → 400 拒绝
        # 原因:配置没用户、磁盘没该书、session 没跑 → 都不该猜
        if not user_name or user_name == "default":
            return JSONResponse(
                {"ok": False, "error": "无法确定写入用户,请先扫码登录或先在 Web UI 添加用户"},
                status_code=400,
            )

        # 应用新值
        if new_total > 0:
            payload["chapters_total"] = new_total
            payload["user_set"] = True
            # 2026-06-12: 同步把 chapters 数组里那条(唯一一条)的 chapterIdx 改成 N
            # 格式参考 1d5322805cfd751d5aff1ea.json
            # (主循环算 N = max(min) - min(min) + 1 = N-1+1 = N,正确)
            existing_chapters = payload.get("chapters") or []
            preserved_uid = payload.get("first_chapter_uid") or ""
            if existing_chapters and isinstance(existing_chapters[0], dict):
                existing_chapters[0]["chapterIdx"] = new_total
            else:
                # chapters 数组空(或不存在)→ 补一条
                existing_chapters = [{
                    "chapterUid": preserved_uid,
                    "chapterIdx": new_total,
                    "title": "",
                }]
            payload["chapters"] = existing_chapters
            payload["first_chapter_idx"] = new_total
        else:
            # 清空用户设置:回退到默认 58(或磁盘 chapters 实际长度,跟 GET 逻辑一致)
            payload.pop("chapters_total", None)
            payload.pop("user_set", None)
            # 不动 chapters 数组本身(让 _advance_chapter_index 重新算 N)
        payload["fetched_at"] = _dt.now().isoformat()
        payload["updated_at"] = _dt.now().isoformat()

        ok = _udm.save_chapters(user_name, book_id, payload)
        if not ok:
            return JSONResponse({"ok": False, "error": "save_chapters failed"}, status_code=500)

        logger.info(
            f"[chapters] web 端更新: {book_id[:12]} N={new_total or '默认'}"
        )
        return JSONResponse({
            "ok": True,
            "book_id": book_id,
            "chapters_total": new_total if new_total > 0 else CHAPTERS_TOTAL_DEFAULT,
            "user_set": new_total > 0,
        })
    except Exception as e:
        logger.exception(f"[chapters] web 端更新异常: book_id={book_id[:12]} err={e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api-reading-logs")
async def get_api_reading_logs():
    return JSONResponse(session_manager.get_logs())


@app.post("/logout")
async def logout():
    browser_manager._current_user = ""
    browser_manager._login_status = "idle"
    browser_manager.reset_login_status()
    # 1. 清浏览器 context 的 cookies
    try:
        if browser_manager.context:
            await browser_manager.context.clear_cookies()
        if browser_manager.page:
            await browser_manager.page.goto("https://weread.qq.com/", timeout=10000)
    except Exception:
        pass
    # 2. 清磁盘上的 cookies + credentials(否则 reload 后 cookies_valid 还是 True)
    cleared = []
    try:
        from src.cookie_manager import cookie_manager
        from src.credential_manager import credential_manager
        for u in cookie_manager.get_all_valid_users():
            cookie_manager.clear(u)
            try:
                credential_manager.delete(u)
            except Exception:
                pass
            cleared.append(u)
        # 兼容旧结构:根目录 default.json
        try:
            old_default = Path("shared/credentials/default.json")
            if old_default.exists():
                old_default.unlink()
                cleared.append("default(old)")
            old_cookies = Path("shared/cookies.json")
            if old_cookies.exists():
                old_cookies.unlink()
        except Exception:
            pass
    except Exception as e:
        logger.warning(f"清磁盘凭证时异常: {e}")
    return JSONResponse({
        "status": "ok",
        "message": f"已退出登录(清掉 {len(cleared)} 个用户凭证: {cleared})"
    })


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
    # 先把磁盘 cookie 同步到 context(初始化时只 load 一次,后续不同步)
    try:
        await browser_manager._ensure_fresh_cookies()
    except Exception:
        pass
    # 再看登录态,没 wr_skey 直接提示
    has_login = False
    try:
        if browser_manager.context:
            cookies = await browser_manager.context.cookies()
            has_login = any(c.get("name") == "wr_skey" and c.get("value") for c in cookies)
    except Exception:
        pass
    if not has_login:
        return JSONResponse(
            {"books": [], "error": "not_logged_in", "message": "未登录或 cookie 已过期,请先在右上角扫码登录"},
            status_code=200,
        )
    books = await browser_manager.fetch_shelf_books()
    return JSONResponse({"books": books, "count": len(books)})


@app.get("/api/shelf-debug")
async def shelf_debug():
    """调试：拦截书架页所有JSON响应"""
    try:
        page = await browser_manager.get_page()
        jsons = []

        async def on_response(response):
            try:
                body = await response.json()
                if isinstance(body, dict):
                    ks = list(body.keys())[:20]
                    jsons.append({"url": response.url[:150], "keys": ks})
            except:
                pass

        page.on("response", on_response)
        try:
            await page.goto("https://weread.qq.com/web/shelf", timeout=30000, wait_until="networkidle")
            await asyncio.sleep(4)
        finally:
            try:
                page.remove_listener("response", on_response)
            except:
                pass

        return JSONResponse({"url": page.url, "json_responses": jsons})
    except Exception as e:
        return JSONResponse({"error": str(e)})


@app.get("/api/search-debug")
async def search_debug(q: str = ""):
    """调试：搜索截图+拦截JSON"""
    try:
        from urllib.parse import quote
        import base64 as b64
        page = await browser_manager.get_page()
        jsons = []

        async def on_response(response):
            try:
                body = await response.json()
                if isinstance(body, dict):
                    ks = list(body.keys())[:20]
                    jsons.append({"url": response.url[:150], "keys": ks})
            except:
                pass

        page.on("response", on_response)
        try:
            keyword = q or "明朝那些事儿"
            await page.goto(f"https://weread.qq.com/web/search/global?keyword={quote(keyword)}", timeout=30000, wait_until="networkidle")
            await asyncio.sleep(3)
            screenshot = await page.screenshot(type="png")
            screenshot_b64 = b64.b64encode(screenshot).decode()
            init_keys = await page.evaluate("""() => {
                try {
                    var st = window.__INITIAL_STATE__ || {};
                    return Object.keys(st);
                } catch(e) { return []; }
            }""")
            body_text = await page.evaluate("document.body ? document.body.innerText.substring(0, 500) : ''")
        finally:
            try:
                page.remove_listener("response", on_response)
            except:
                pass

        return JSONResponse({
            "url": page.url,
            "title": await page.evaluate("document.title"),
            "init_state_keys": init_keys,
            "body_preview": body_text,
            "json_responses": jsons,
            "screenshot": "data:image/png;base64," + screenshot_b64,
        })
    except Exception as e:
        return JSONResponse({"error": str(e)})


@app.get("/browser/search-preview")
async def search_preview(q: str = ""):
    """直接返回搜索页截图"""
    try:
        from urllib.parse import quote
        page = await browser_manager.get_page()
        keyword = q or "明朝那些事儿"
        await page.goto(f"https://weread.qq.com/web/search/global?keyword={quote(keyword)}", timeout=30000, wait_until="networkidle")
        await asyncio.sleep(3)
        screenshot = await page.screenshot(type="png")
        return Response(content=screenshot, media_type="image/png")
    except Exception as e:
        return JSONResponse({"error": str(e)})


@app.get("/api/search-books")
async def search_books(q: str = "", limit: int = 8):
    """按书名搜索,返回多条候选 + 数据来源(api/ssr/dom/empty)。
    用于书籍配置弹窗,前端从候选列表里挑一本填回。
    """
    if not q.strip():
        return JSONResponse({"query": q, "source": "empty", "count": 0, "results": []})
    out = await browser_manager.search_books_candidates(q.strip(), limit=limit)
    return JSONResponse(out)


# === 书籍封面代理 ===
# 微信读书封面 URL 形如 https://cdn.wered.qq.com/weread/cover/{...}/{bookId}/xx.jpg
# 第三方页面直链会被防盗链(403),我们做服务端代理 + 显式 UA + Referer
@app.get("/api/book-cover")
async def book_cover(book_id: str = ""):
    """根据 book_id 拿微信读书封面 URL(返回 URL 不下载图片,前端 <img src> 直接用)。
    实现思路:
      1) 浏览器内 fetch /web/bookDetail/{bookId} 拿 SSR cover
      2) 失败 → 用 bookId 当 q 调 search_books_candidates 兜底
      3) 还失败 → 返回空 cover_url,前端展示"暂无封面"占位
    """
    if not book_id.strip():
        return JSONResponse({"error": "missing book_id"}, status_code=400)
    cover_url = ""

    # 1) 浏览器内 fetch bookDetail 页(SSR)
    try:
        if browser_manager and getattr(browser_manager, "page", None):
            js = (
                "(async () => {"
                "  try {"
                "    const r = await fetch(location.origin + '/web/bookDetail/" + book_id + "', {credentials: 'include'});"
                "    const html = await r.text();"
                "    let m = html.match(/\"cover\"\\s*:\\s*\"(https?:\\\\/\\\\/[^\"]+)\"/);"
                "    if (!m) m = html.match(/\"coverUrl\"\\s*:\\s*\"(https?:\\\\/\\\\/[^\"]+)\"/);"
                "    if (m) return m[1].replace(/\\\\\\\\u002F/g, '/').replace(/\\\\\\\\/g, '/');"
                "    return '';"
                "  } catch(e) { return ''; }"
                "})()"
            )
            try:
                cover_url = await asyncio.wait_for(
                    browser_manager.page.evaluate(js),
                    timeout=5.0
                )
            except Exception:
                cover_url = ""
    except Exception:
        cover_url = ""

    # 2) SSR 没拿到 → 用 bookId 调 search 兜底
    if not cover_url or "http" not in cover_url:
        try:
            r = await browser_manager.search_books_candidates(book_id, limit=3)
            for cand in (r.get("results") or []):
                if cand.get("bookId") == book_id and cand.get("cover"):
                    cover_url = cand["cover"]
                    break
            # 没精确匹配就用第一条
            if not cover_url and r.get("results"):
                first = r["results"][0]
                if first.get("cover"):
                    cover_url = first["cover"]
        except Exception:
            pass

    return JSONResponse({
        "book_id": book_id,
        "cover_url": cover_url or "",
    })


@app.get("/api/cover-proxy")
async def cover_proxy(url: str = ""):
    """封面代理:从微信读书 CDN 拉图(带 Referer 头)流回前端,绕过防盗链。
    前端 <img src="/api/cover-proxy?url=..."> 即可,失败时浏览器原生 onerror。
    """
    if not url.startswith("http"):
        return JSONResponse({"error": "bad url"}, status_code=400)
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as cli:
            r = await cli.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://weread.qq.com/",
            })
        if r.status_code != 200:
            return JSONResponse({"error": f"upstream {r.status_code}"}, status_code=r.status_code)
        ct = r.headers.get("content-type", "image/jpeg")
        return Response(content=r.content, media_type=ct, headers={"Cache-Control": "public, max-age=86400"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/cover-screenshot")
async def cover_screenshot(book_id: str = ""):
    """获取书籍封面(磁盘缓存 + 单本原图)。
    优先级:
      1) shared/covers/{bookId}.png → 有就直返
      2) 用 Playwright 打开 /web/bookDetail/{bookId},从 og:image meta 拿到"单本封面"真 URL
         (这个 URL 才是真正的 2:3 单本书封,不是 DOM 里的整张大图)
      3) 服务端用 httpx 拉原图 + 防盗链 Referer → PIL 后处理(统一尺寸、加白底)
      4) 写盘 → 返回
    缓存触发重新获取的时机:
      - 文件不存在(新加书 / 用户主动删)
      - 文件存在但 size=0 / 不可读
      - DELETE /api/cover-screenshot?book_id=X
    """
    if not book_id.strip():
        return JSONResponse({"error": "missing book_id"}, status_code=400)
    import re as _re
    if not _re.match(r"^[A-Za-z0-9_-]+$", book_id):
        return JSONResponse({"error": "bad book_id"}, status_code=400)
    cache_dir = Path("shared/covers")
    cache_path = cache_dir / f"{book_id}.jpg"

    # 1) 磁盘有缓存 → 直返
    try:
        if cache_path.exists() and cache_path.stat().st_size > 0:
            data = cache_path.read_bytes()
            return Response(
                content=data,
                media_type="image/jpeg",
                headers={"Cache-Control": "public, max-age=86400", "X-Cover-Cache": "hit"},
            )
    except Exception:
        pass

    # 2) 从 og:image 拿真封面 URL
    cover_url = ""
    try:
        if not browser_manager or not getattr(browser_manager, "page", None):
            return JSONResponse({"error": "browser not ready"}, status_code=503)
        from urllib.parse import quote
        page = browser_manager.page
        if "weread.qq.com" not in (page.url or ""):
            await page.goto("https://weread.qq.com/", wait_until="domcontentloaded", timeout=15000)
        detail_page = await page.context.new_page()
        try:
            url = f"https://weread.qq.com/web/bookDetail/{quote(book_id, safe='')}"
            await detail_page.goto(url, wait_until="domcontentloaded", timeout=20000)
            try:
                await detail_page.wait_for_selector("meta[property='og:image']", timeout=6000)
            except Exception:
                pass
            js = """
            (() => {
              const og = document.querySelector('meta[property="og:image"]');
              if (og && og.content && og.content.startsWith('http')) return og.content;
              // 备选:.wr_bookCover_img
              const img = document.querySelector('img.wr_bookCover_img, .wr_bookCover img, img[alt="书籍封面"]');
              if (img && img.src) return img.src;
              return '';
            })()
            """
            cover_url = (await detail_page.evaluate(js) or "").strip()
        finally:
            try:
                await detail_page.close()
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"og:image 提取失败,降级到空: {e}")

    if not cover_url or "http" not in cover_url:
        return JSONResponse({"error": "cover image not found"}, status_code=404)

    # 3) 服务端拉原图(带防盗链)
    try:
        import httpx
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as cli:
            r = await cli.get(cover_url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://weread.qq.com/",
            })
        if r.status_code != 200:
            return JSONResponse({"error": f"upstream {r.status_code}"}, status_code=r.status_code)
        raw = r.content
    except Exception as e:
        return JSONResponse({"error": f"fetch failed: {e}"}, status_code=502)

    # 4) PIL 后处理:统一到 240×320(3:4 比例) + 白底居中 + 轻微阴影
    out_bytes = _postprocess_cover(raw, target_w=240, target_h=320)
    if out_bytes is None:
        return JSONResponse({"error": "image decode failed"}, status_code=500)

    # 5) 写盘(原子写:tmp → rename)
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        # 顺便清理老的 .png 缓存(从截图方案迁过来)
        old_png = cache_path.with_suffix(".png")
        if old_png.exists():
            try: old_png.unlink()
            except Exception: pass
        tmp_path = cache_path.with_suffix(".jpg.tmp")
        tmp_path.write_bytes(out_bytes)
        tmp_path.replace(cache_path)
    except Exception as e:
        logger.warning(f"封面缓存写盘失败(非致命,直接返回): {e}")

    return Response(
        content=out_bytes,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400", "X-Cover-Cache": "miss"},
    )


@app.delete("/api/cover-screenshot")
async def cover_screenshot_invalidate(book_id: str = ""):
    """删除指定 book_id 的封面缓存(新 .jpg + 兼容老 .png),下次访问会重新获取。
    用法:用户在 UI 上点"重新截图"或"刷新封面"时调用。
    """
    import re as _re
    if not _re.match(r"^[A-Za-z0-9_-]+$", book_id):
        return JSONResponse({"error": "bad book_id"}, status_code=400)
    cache_dir = Path("shared/covers")
    removed = False
    # 删新 .jpg + 兼容老 .png
    for ext in (".jpg", ".png"):
        p = cache_dir / f"{book_id}{ext}"
        try:
            if p.exists():
                p.unlink()
                removed = True
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse({"status": "ok", "removed": removed, "book_id": book_id})


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
    raw = history_manager.get_statistics()
    # 自动进位分钟→小时
    raw["today_fmt"] = history_manager.format_minutes(raw["today"]["total_minutes"])
    raw["week_fmt"] = history_manager.format_minutes(raw["week"]["total_minutes"])
    raw["total_fmt"] = history_manager.format_minutes(raw["total"]["total_minutes"])
    return JSONResponse(raw)


@app.get("/api/heatmap")
async def get_heatmap(weeks: int = 9):
    """GitHub contributions 风格阅读热力图(默认 9 周 ≈ 2 个月)"""
    try:
        data = history_manager.get_heatmap_data(weeks=weeks)
        return JSONResponse(data)
    except Exception as e:
        return JSONResponse({"error": str(e)})


@app.post("/history/clear")
async def clear_history():
    history_manager.clear_history()
    return JSONResponse({"status": "ok", "message": "历史记录已清除"})


@app.get("/users")
async def get_users():
    """返回用户列表(严格按 config.users,不再虚拟显示 default)。

    修复:之前 config.users 为空时会虚拟塞一个 default 用户进列表,
    导致用户清空账号后前端还显示一个"幽灵 default",体验上是 bug。
    现在:config.users 空 → 返回空数组,前端展示"暂无用户"引导扫码登录。

    旧 default 残留清理:用户可手动调 DELETE /users/default(后端有特例放行)
    """
    users = config.get_users()
    user_status = []
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
    """删除用户(配置 + 凭证 + cookies + 章节缓存 + 浏览器状态 全清)。

    返回:
      - status=ok: 删除成功,data 包含 config_removed(可能>1,如果原本有同名重复)
      - status=error: 不存在该用户

    特例:user_name="default" 且 config.users 为空时,允许删除磁盘上的 default 目录
    (因为 /users API 在 config 空时虚拟显示了 default 用来占位)
    """
    # 先确认 config 里真的有这个用户
    config_users = config.get_users()
    exists = any(u.get("name") == user_name for u in config_users)
    # 特例:default 虚拟用户不在 config 里,但磁盘上可能有遗留目录
    if not exists:
        if user_name == "default" and not config_users:
            # 允许删除磁盘上残留的 default 目录
            logger.info(f"[delete_user] 删除虚拟 default 用户(磁盘残留清理)")
        else:
            return JSONResponse(
                {"status": "error", "message": f"用户 {user_name} 不存在"},
                status_code=404,
            )
    config_removed = config.remove_user(user_name)
    credential_manager.delete(user_name)
    # 顺便清掉章节缓存目录
    try:
        from src.user_data_manager import user_data_manager
        # 拿这个用户的所有 bookId 缓存逐一删
        for book_id in user_data_manager.list_cached_books(user_name):
            user_data_manager.delete_chapters(user_name, book_id)
    except Exception as e:
        logger.debug(f"清理 {user_name} 章节缓存异常(非致命): {e}")
    # 同步清浏览器内部残留状态(_current_user 经常忘记更新,导致状态显示已删用户)
    try:
        bs = browser_manager.get_login_status()
        if bs.get("user") == user_name:
            # 切到 config 里下一个有效用户,或清空
            next_user = config_users[0].get("name") if config_users and config_users[0].get("name") != user_name else None
            if next_user is None and len(config_users) >= 2:
                next_user = config_users[1].get("name")
            if next_user:
                browser_manager.set_current_user(next_user)
                logger.info(f"[delete_user] 浏览器 _current_user 从 {user_name} 切到 {next_user}")
            else:
                # 没有任何其他用户,清掉状态
                browser_manager.set_current_user("")
                browser_manager._login_status = "idle"
                browser_manager._login_error = None
                browser_manager._needs_relogin = False
                logger.info(f"[delete_user] 浏览器 _current_user 清空(无其他用户)")
    except Exception as e:
        logger.debug(f"同步浏览器状态失败(非致命): {e}")
    return JSONResponse(
        {
            "status": "ok",
            "message": f"用户 {user_name} 已删除(清理了 {config_removed} 条 config 记录 + 凭证 + 章节缓存 + 浏览器状态)",
            "data": {"config_removed": config_removed},
        }
    )


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
    return JSONResponse({"status": "error", "message": "用户不存在"})


@app.post("/users/{user_name}/login")
async def login_user(user_name: str):
    # 关键:扫码前自动把用户加进 config.users(如果还没在的话)
    # 解决单用户首次扫码的两步操作问题:
    #   之前: 用户必须先点 "添加用户" → 再点 "登录" 扫码
    #   现在: 用户直接点 "扫码登录" 即可,系统自动创建用户记录
    # config.users 允许为空;空的时候扫码就建,单用户也支持删除后再扫码重建
    if not any(u.get("name") == user_name for u in config.get_users()):
        added = config.add_user({
            "name": user_name,
            "display_name": user_name,
            "books": [],
            "reading_overrides": {},
        })
        if added:
            logger.info(f"[login_user] 首次扫码,自动加入 config: {user_name}")
        else:
            logger.warning(f"[login_user] 自动加入 config 失败: {user_name}")

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
    # 兼容:浏览器返回字段是 user;前端 header 右上角读 user_name + 状态卡读 user
    # 优先用浏览器当前用户,否则回退到第一个有效用户(磁盘凭证)
    # 2026-06-16: 兜底 —— "default" / 空字符串都视作"未设",严禁泄漏到前端(用户原话)
    raw_user = status.get("user") or ""
    if raw_user.strip().lower() == "default":
        raw_user = ""
    user_name = raw_user
    if not user_name:
        try:
            valid_users = cookie_manager.get_all_valid_users()
            # 同样过滤 "default"
            valid_users = [u for u in valid_users if u and u.strip().lower() != "default"]
            if valid_users:
                user_name = valid_users[0]
        except Exception:
            pass
    if user_name:
        status["user_name"] = user_name
        # 没设 user 时也回填,保持 user 字段存在
        if not status.get("user") or status.get("user", "").strip().lower() == "default":
            status["user"] = user_name
    else:
        # 都没拿到 → 显式清掉 "default" 残留
        status.pop("user_name", None)
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
    vp = page.viewport_size
    logger.info(f"点击: 接收坐标=({x},{y}) 视口={vp}")
    try:
        await page.mouse.click(x, y, force=True)
        logger.info(f"点击成功: ({x},{y})")
    except Exception as e:
        logger.warning(f"点击失败: {e}")
    return JSONResponse({"status": "ok", "clicked": {"x": x, "y": y}, "viewport": vp})


@app.post("/browser/back")
async def browser_back():
    page = await browser_manager.get_page()
    await page.go_back()
    await asyncio.sleep(1)
    return JSONResponse({"status": "ok", "url": page.url})


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
async def test_notification(request: Request):
    """测试通知。query/body 参数:
    - type=general|weekly_reward    通用 vs 周奖励(标题/正文不同)
    - channels=["bark","feishu",..] 只测开关打开的通道;不传/空=测所有 enabled 的(向后兼容)

    重要:不走 notifier.channels 缓存(那是模块启动时按 YAML 构建的,UI 改了保存不重建),
    而是每次按当前 config + 通道名单临时构造通道实例,这样 UI 勾选状态能立刻生效。
    """
    try:
        body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    except Exception:
        body = {}
    ntype = (body.get("type") or "general").lower()

    # 解析 channels 字段
    raw_channels = body.get("channels")
    if raw_channels is None:
        # 旧版前端没传:测所有 enabled+keys 完整的通道
        requested = None
    else:
        requested = raw_channels if isinstance(raw_channels, list) else [raw_channels]
        # 过滤非字符串 / 不识别的名字
        requested = [n for n in requested if isinstance(n, str) and n in CHANNEL_LABELS]

    # 按名单(或不传=全部)构造通道
    targets = _build_test_channels(requested)

    if not targets:
        if requested:
            # 全部缺失配置 → 单独提示
            missing = "、".join(CHANNEL_LABELS.get(n, n) for n in requested)
            msg = f"未发送:通道 {missing} 配置不完整或未启用(请确认已开启开关 + 填好 Token/Webhook,并保存配置)"
        else:
            msg = "未配置任何通知通道,请先开启开关并保存"
        return JSONResponse({"status": "error", "message": msg, "type": ntype})

    # 标题/正文(简化,与 notifier._format_body 保持一致)
    if ntype == "weekly_reward":
        title = "微信读书 · 周奖励待领"
        body_text = "(测试) 周奖励提醒 - 如果你收到此消息,说明通知配置正确。"
    else:
        title = "微信读书自动阅读 - 测试通知"
        body_text = "如果你收到此消息,说明通知配置正确。"

    # 并发发送(每个通道独立,互不影响)
    results = await asyncio.gather(
        *[ch.send(title, body_text) for ch, _ in targets],
        return_exceptions=True,
    )

    # 汇总
    success_labels = []
    failed_items = []  # [(label, reason)]
    for (ch, name), r in zip(targets, results):
        label = CHANNEL_LABELS.get(name, name)
        if r is True:
            success_labels.append(label)
        elif isinstance(r, Exception):
            failed_items.append((label, type(r).__name__))
        else:
            # channel.send 内部已经吞掉异常 → 跑一次小诊断把真正原因带回 toast
            diag = await _quick_diag(name)
            if not diag["ok"]:
                failed_items.append((label, f"网络不通:{diag['stage']}({diag.get('reason','')})"))
            else:
                failed_items.append((label, "服务端未确认成功(HTTP 异常/被限流)"))

    if not failed_items:
        msg = f"已发送 ({len(success_labels)}/{len(targets)} 通道):" + "、".join(success_labels)
        return JSONResponse({
            "status": "ok", "message": msg, "type": ntype,
            "success": success_labels, "failed": []
        })

    if success_labels:
        fail_str = "、".join(f"{n}({r})" for n, r in failed_items)
        msg = f"部分成功:{('、'.join(success_labels))} 已发送;失败:{fail_str}"
        return JSONResponse({
            "status": "partial", "message": msg, "type": ntype,
            "success": success_labels, "failed": [n for n, _ in failed_items]
        })

    # 全部失败
    fail_str = "、".join(f"{n}({r})" for n, r in failed_items)
    msg = f"全部发送失败:{fail_str}"
    return JSONResponse({
        "status": "error", "message": msg, "type": ntype,
        "success": [], "failed": [n for n, _ in failed_items]
    })


# === 测试通道构造(读当前 config,不依赖 notifier 缓存) ===
CHANNEL_LABELS = {
    "bark": "Bark",
    "pushplus": "PushPlus",
    "telegram": "Telegram",
    "wxpusher": "WxPusher",
    "ntfy": "Ntfy",
    "feishu": "飞书",
    "wework": "企业微信",
    "dingtalk": "钉钉",
    "gotify": "Gotify",
    "serverchan3": "Server酱",
    "pushdeer": "PushDeer",
}


def _build_channel(name: str):
    """根据当前 config 构造单个通道实例。返回 None 表示未启用或 key 不完整。
    与 notifier._init_channels 保持一致,但每次实时读 config(后者只在启动时读一次)。"""
    if not config.get(f"notification.{name}.enabled", False):
        return None
    if name == "bark":
        k = config.get("notification.bark.device_key", "")
        if not k:
            return None
        return BarkChannel(
            config.get("notification.bark.server", "https://api.day.app") or "https://api.day.app",
            k,
            config.get("notification.bark.sound", ""),
        )
    if name == "pushplus":
        k = config.get("notification.pushplus.token", "")
        return PushPlusChannel(k) if k else None
    if name == "telegram":
        bot = config.get("notification.telegram.bot_token", "")
        cid = config.get("notification.telegram.chat_id", "")
        return TelegramChannel(bot, cid) if (bot and cid) else None
    if name == "wxpusher":
        k = config.get("notification.wxpusher.spt", "")
        return WxPusherChannel(k) if k else None
    if name == "ntfy":
        topic = config.get("notification.ntfy.topic", "")
        if not topic:
            return None
        return NtfyChannel(
            config.get("notification.ntfy.server", "https://ntfy.sh") or "https://ntfy.sh",
            topic,
            config.get("notification.ntfy.token", ""),
        )
    if name == "feishu":
        u = config.get("notification.feishu.webhook_url", "")
        if not u:
            return None
        return FeishuChannel(u, config.get("notification.feishu.msg_type", "text") or "text")
    if name == "wework":
        u = config.get("notification.wework.webhook_url", "")
        if not u:
            return None
        return WeWorkChannel(u, config.get("notification.wework.msg_type", "text") or "text")
    if name == "dingtalk":
        u = config.get("notification.dingtalk.webhook_url", "")
        if not u:
            return None
        return DingTalkChannel(u, config.get("notification.dingtalk.msg_type", "text") or "text")
    if name == "gotify":
        s = config.get("notification.gotify.server", "")
        t = config.get("notification.gotify.token", "")
        if not (s and t):
            return None
        return GotifyChannel(s, t, int(config.get("notification.gotify.priority", 5)))
    if name == "serverchan3":
        u = config.get("notification.serverchan3.uid", "")
        k = config.get("notification.serverchan3.sendkey", "")
        return ServerChan3Channel(u, k) if (u and k) else None
    if name == "pushdeer":
        k = config.get("notification.pushdeer.pushkey", "")
        return PushDeerChannel(k) if k else None
    return None


def _build_test_channels(names):
    """names=None → 构造所有 enabled+keys 完整的(向后兼容);
    names=list → 只构造名单里且配置完整的。返回 [(channel, name), ...]"""
    if names is None:
        out = []
        for n in CHANNEL_LABELS:
            ch = _build_channel(n)
            if ch is not None:
                out.append((ch, n))
        return out
    out = []
    for n in names:
        ch = _build_channel(n)
        if ch is not None:
            out.append((ch, n))
    return out


async def _quick_diag(name: str) -> dict:
    """测试用的小型诊断:DNS + TCP + HTTPS,出错立刻返回真正失败原因。
    目的:channel.send 内部把异常吞了返回 False,这里把真实原因挖出来给用户看。
    返回 {ok, stage, reason} — stage 是 DNS/TCP/HTTPS/config/ok 之一。"""
    import socket
    from urllib.parse import urlparse
    server = None
    if name == "bark":
        server = config.get("notification.bark.server") or "https://api.day.app"
    elif name == "ntfy":
        server = config.get("notification.ntfy.server") or "https://ntfy.sh"
    elif name == "gotify":
        server = config.get("notification.gotify.server")
    elif name in ("feishu", "wework", "dingtalk"):
        server = config.get(f"notification.{name}.webhook_url")
    else:
        server_map = {
            "pushplus": "http://www.pushplus.plus",
            "telegram": "https://api.telegram.org",
            "wxpusher": "https://wxpusher.zjiecode.com",
            "serverchan3": "https://sc3.ft07.com",
            "pushdeer": "https://api.pushdeer.com",
        }
        server = server_map.get(name)
    if not server:
        return {"ok": False, "stage": "config", "reason": "缺少 server/webhook_url"}
    parsed = urlparse(server)
    host, port = parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        ip = socket.gethostbyname(host)
    except Exception as e:
        return {"ok": False, "stage": "DNS", "reason": f"{type(e).__name__}:{e}"}
    try:
        s = socket.create_connection((host, port), timeout=8)
        s.close()
    except Exception as e:
        return {"ok": False, "stage": "TCP", "reason": f"{type(e).__name__}:{e}"}
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as c:
            r = await c.get(server.rstrip("/"))
        if r.status_code >= 500:
            return {"ok": False, "stage": "HTTPS", "reason": f"5xx {r.status_code}"}
        return {"ok": True, "stage": "ok", "reason": f"{r.status_code}"}
    except Exception as e:
        cause = getattr(e, "__cause__", None) or getattr(e, "__context__", None)
        return {"ok": False, "stage": "HTTPS", "reason": f"{type(e).__name__}:{e} cause={cause!r}"}


@app.get("/api/notification-diag/{name}")
async def notification_diag_one(name: str):
    """单通道诊断:DNS 解析 + TCP 连接 + HTTPS GET,每个步骤返回耗时和结果"""
    import socket
    import time
    if name not in CHANNEL_LABELS:
        return JSONResponse({"ok": False, "stage": "input", "error": f"未知通道: {name}"})
    if not config.get(f"notification.{name}.enabled", False):
        return JSONResponse({"ok": False, "stage": "config", "error": "该通道未启用,请先在 UI 上打开开关并保存"})

    # 取 server URL(各通道字段名不同,挨个写)
    server = None
    if name == "bark":
        server = config.get("notification.bark.server") or "https://api.day.app"
    elif name == "ntfy":
        server = config.get("notification.ntfy.server") or "https://ntfy.sh"
    elif name == "gotify":
        server = config.get("notification.gotify.server")
    elif name in ("feishu", "wework", "dingtalk"):
        server = config.get(f"notification.{name}.webhook_url")
    else:
        # pushplus/telegram/wxpusher/serverchan3/pushdeer 是固定 server,直接测该 host
        server_map = {
            "pushplus": "http://www.pushplus.plus",
            "telegram": "https://api.telegram.org",
            "wxpusher": "https://wxpusher.zjiecode.com",
            "serverchan3": "https://sc3.ft07.com",
            "pushdeer": "https://api.pushdeer.com",
        }
        server = server_map.get(name)

    if not server:
        return JSONResponse({"ok": False, "stage": "config", "error": "缺少 server / webhook_url"})

    # 抽 host
    from urllib.parse import urlparse
    parsed = urlparse(server)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    steps = []
    # 1. DNS
    t0 = time.time()
    try:
        ip = socket.gethostbyname(host)
        steps.append({"stage": "dns", "ok": True, "ms": int((time.time() - t0) * 1000), "host": host, "ip": ip})
    except Exception as e:
        steps.append({"stage": "dns", "ok": False, "ms": int((time.time() - t0) * 1000), "host": host,
                      "type": type(e).__name__, "msg": str(e) or repr(e)})
        return JSONResponse({"ok": False, "stage": "dns", "steps": steps})

    # 2. TCP
    t0 = time.time()
    try:
        s = socket.create_connection((host, port), timeout=10)
        s.close()
        steps.append({"stage": "tcp", "ok": True, "ms": int((time.time() - t0) * 1000), "ip": ip, "port": port})
    except Exception as e:
        steps.append({"stage": "tcp", "ok": False, "ms": int((time.time() - t0) * 1000), "ip": ip, "port": port,
                      "type": type(e).__name__, "msg": str(e) or repr(e)})
        return JSONResponse({"ok": False, "stage": "tcp", "steps": steps})

    # 3. HTTPS(简单 GET 一次,验证 SSL + 协议层通)
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as c:
            r = await c.get(server if not server.endswith("/") else server[:-1])
        steps.append({"stage": "https", "ok": True, "ms": int((time.time() - t0) * 1000),
                      "status": r.status_code, "len": len(r.text)})
        return JSONResponse({"ok": True, "steps": steps})
    except Exception as e:
        cause = getattr(e, "__cause__", None) or getattr(e, "__context__", None)
        steps.append({"stage": "https", "ok": False, "ms": int((time.time() - t0) * 1000),
                      "type": type(e).__name__, "msg": str(e) or repr(e),
                      "cause": repr(cause) if cause else None})
        return JSONResponse({"ok": False, "stage": "https", "steps": steps})


@app.get("/api/weekly-status")
async def weekly_status():
    """返回每周奖励调度状态(供前端展示)"""
    try:
        st = scheduler.get_weekly_status()
        return JSONResponse(st)
    except Exception as e:
        return JSONResponse({"error": str(e)})


@app.get("/logs")
async def get_logs():
    """返回应用日志文件"""
    try:
        log_file = config.get("log.file", "shared/logs/weread.log")
        log_path = Path(log_file)
        if log_path.exists():
            content = log_path.read_text(encoding="utf-8")
            lines = content.split("\n")
            return JSONResponse({"total_lines": len(lines), "content": "\n".join(lines[-300:])})
        return JSONResponse({"error": "日志文件不存在"})
    except Exception as e:
        return JSONResponse({"error": str(e)})


@app.get("/health")
async def health_check():
    return JSONResponse({
        "status": "healthy",
        "reader_running": reader.is_reading
    })


def _postprocess_cover(raw: bytes, target_w: int = 240, target_h: int = 320) -> bytes | None:
    """封面后处理:统一到 3:4(target_w × target_h),白底居中,保留阴影。

    输入:任意格式的封面原图(jpg/webp 等)
    输出:PNG bytes(透明阴影 + 居中缩放)
    失败:None(调用方 fallback 到原图)
    """
    try:
        from PIL import Image
    except ImportError:
        # PIL 没装 → 直接返回原图 bytes(让浏览器自己渲染)
        return raw
    try:
        im = Image.open(io.BytesIO(raw)).convert("RGBA")
    except Exception:
        return None
    # 算缩放:等比 fit 到 target_w × target_h,留 8px padding
    pad = 8
    avail_w = target_w - pad * 2
    avail_h = target_h - pad * 2
    iw, ih = im.size
    if iw == 0 or ih == 0:
        return None
    scale = min(avail_w / iw, avail_h / ih)
    nw = max(1, int(iw * scale))
    nh = max(1, int(ih * scale))
    im_resized = im.resize((nw, nh), Image.LANCZOS)
    # 合成:白底 + 居中 + 阴影
    bg = Image.new("RGBA", (target_w, target_h), (255, 255, 255, 255))
    # 阴影:把 im_resized 转灰 alpha 模拟
    shadow = Image.new("RGBA", (target_w, target_h), (0, 0, 0, 0))
    from PIL import ImageDraw
    dr = ImageDraw.Draw(shadow)
    sx = (target_w - nw) // 2
    sy = (target_h - nh) // 2
    dr.rectangle([sx + 2, sy + 3, sx + nw + 2, sy + nh + 3], fill=(0, 0, 0, 28))
    # 合并阴影 → 白底 → 封面
    bg.alpha_composite(shadow)
    bg.alpha_composite(im_resized, (sx, sy))
    out = io.BytesIO()
    bg.convert("RGB").save(out, format="JPEG", quality=88, optimize=True)
    return out.getvalue()
