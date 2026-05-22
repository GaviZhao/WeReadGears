import asyncio
import httpx
from typing import Optional, Dict, Any, List
from enum import Enum
from datetime import datetime

from src.utils.logger import logger
from src.config import config


class NotificationType(Enum):
    STARTUP = "startup"
    LOGIN_SUCCESS = "login_success"
    LOGIN_FAILED = "login_failed"
    READING_START = "reading_start"
    READING_PROGRESS = "reading_progress"
    READING_COMPLETE = "reading_complete"
    READING_INTERRUPTED = "reading_interrupted"
    READING_FAILED = "reading_failed"
    SESSION_SUCCESS = "session_success"
    SESSION_FAILURE = "session_failure"
    MULTI_USER_SUMMARY = "multi_user_summary"
    RUNTIME_ERROR = "runtime_error"
    NETWORK_DISCONNECT = "network_disconnect"
    NETWORK_RECOVER = "network_recover"
    DAILY_REPORT = "daily_report"
    COOKIES_EXPIRED = "cookies_expired"
    CONTAINER_EXIT = "container_exit"
    GENERAL = "general"


class NotificationChannel:
    async def send(self, title: str, content: str) -> bool:
        raise NotImplementedError


class BarkChannel(NotificationChannel):
    def __init__(self, server: str, device_key: str, sound: str = ""):
        self.server = server
        self.device_key = device_key
        self.sound = sound

    async def send(self, title: str, content: str) -> bool:
        if not self.device_key:
            return False
        url = f"{self.server.rstrip('/')}/{self.device_key}"
        try:
            params = {"title": title, "body": content}
            if self.sound:
                params["sound"] = self.sound
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url, params=params)
                return response.status_code == 200
        except Exception as e:
            logger.error(f"Bark通知失败: {e}")
            return False


class PushPlusChannel(NotificationChannel):
    def __init__(self, token: str):
        self.token = token
        self.url = "http://www.pushplus.plus/send"

    async def send(self, title: str, content: str) -> bool:
        if not self.token:
            return False
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    self.url,
                    json={
                        "token": self.token,
                        "title": title,
                        "content": content,
                        "template": "txt"
                    }
                )
                if response.status_code == 200:
                    result = response.json()
                    return result.get("code") == 200
                return False
        except Exception as e:
            logger.error(f"PushPlus通知失败: {e}")
            return False


class TelegramChannel(NotificationChannel):
    def __init__(self, bot_token: str, chat_id: str, proxy_http: str = "", proxy_https: str = ""):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.proxy_http = proxy_http
        self.proxy_https = proxy_https
        self.url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    async def send(self, title: str, content: str) -> bool:
        if not self.bot_token or not self.chat_id:
            return False
        try:
            text = f"*{title}*\n\n{content}"
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    self.url,
                    json={"chat_id": self.chat_id, "text": text, "parse_mode": "Markdown"}
                )
                return response.status_code == 200
        except Exception as e:
            logger.error(f"Telegram通知失败: {e}")
            return False


class WxPusherChannel(NotificationChannel):
    def __init__(self, spt: str):
        self.spt = spt
        self.url = "https://wxpusher.zjiecode.com/api/send/message"

    async def send(self, title: str, content: str) -> bool:
        if not self.spt:
            return False
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    self.url,
                    json={
                        "appToken": "",
                        "content": f"{title}\n\n{content}",
                        "summary": title,
                        "contentType": 1,
                        "topicIds": [self.spt]
                    }
                )
                return response.status_code == 200
        except Exception as e:
            logger.error(f"WxPusher通知失败: {e}")
            return False


class NtfyChannel(NotificationChannel):
    def __init__(self, server: str, topic: str, token: str = ""):
        self.server = server
        self.topic = topic
        self.token = token

    async def send(self, title: str, content: str) -> bool:
        if not self.topic:
            return False
        try:
            url = f"{self.server.rstrip('/')}/{self.topic}"
            headers = {}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    url,
                    headers=headers,
                    content=f"{title}\n\n{content}"
                )
                return response.status_code in (200, 201)
        except Exception as e:
            logger.error(f"Ntfy通知失败: {e}")
            return False


class FeishuChannel(NotificationChannel):
    def __init__(self, webhook_url: str, msg_type: str = "text"):
        self.webhook_url = webhook_url
        self.msg_type = msg_type

    async def send(self, title: str, content: str) -> bool:
        if not self.webhook_url:
            return False
        try:
            if self.msg_type == "rich_text":
                payload = {
                    "msg_type": "interactive",
                    "card": {
                        "elements": [{"tag": "div", "text": {"content": f"{title}\n\n{content}", "tag": "lark_md"}}]
                    }
                }
            else:
                payload = {"msg_type": "text", "content": {"text": f"{title}\n\n{content}"}}
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(self.webhook_url, json=payload)
                return response.status_code == 200
        except Exception as e:
            logger.error(f"飞书通知失败: {e}")
            return False


class WeWorkChannel(NotificationChannel):
    def __init__(self, webhook_url: str, msg_type: str = "text"):
        self.webhook_url = webhook_url
        self.msg_type = msg_type

    async def send(self, title: str, content: str) -> bool:
        if not self.webhook_url:
            return False
        try:
            if self.msg_type == "markdown":
                payload = {"msgtype": "markdown", "markdown": {"content": f"**{title}**\n\n{content}"}}
            elif self.msg_type == "news":
                payload = {"msgtype": "news", "news": {"articles": [{"title": title, "description": content}]}}
            else:
                payload = {"msgtype": "text", "text": {"content": f"{title}\n\n{content}"}}
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(self.webhook_url, json=payload)
                return response.status_code == 200
        except Exception as e:
            logger.error(f"企业微信通知失败: {e}")
            return False


class DingTalkChannel(NotificationChannel):
    def __init__(self, webhook_url: str, msg_type: str = "text"):
        self.webhook_url = webhook_url
        self.msg_type = msg_type

    async def send(self, title: str, content: str) -> bool:
        if not self.webhook_url:
            return False
        try:
            if self.msg_type == "markdown":
                payload = {"msgtype": "markdown", "markdown": {"title": title, "text": f"**{title}**\n\n{content}"}}
            elif self.msg_type == "link":
                payload = {"msgtype": "link", "link": {"title": title, "text": content, "messageUrl": "https://weread.qq.com/"}}
            else:
                payload = {"msgtype": "text", "text": {"content": f"{title}\n\n{content}"}}
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(self.webhook_url, json=payload)
                return response.status_code == 200
        except Exception as e:
            logger.error(f"钉钉通知失败: {e}")
            return False


class GotifyChannel(NotificationChannel):
    def __init__(self, server: str, token: str, priority: int = 5, title: str = "weread"):
        self.server = server
        self.token = token
        self.priority = priority
        self.title = title

    async def send(self, title: str, content: str) -> bool:
        if not self.server or not self.token:
            return False
        try:
            url = f"{self.server.rstrip('/')}/message"
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    url,
                    params={"token": self.token},
                    json={"message": content, "title": title, "priority": self.priority}
                )
                return response.status_code == 200
        except Exception as e:
            logger.error(f"Gotify通知失败: {e}")
            return False


class ServerChan3Channel(NotificationChannel):
    def __init__(self, uid: str, sendkey: str):
        self.uid = uid
        self.sendkey = sendkey
        self.url = f"https://sc3.ft07.com/send"

    async def send(self, title: str, content: str) -> bool:
        if not self.uid or not self.sendkey:
            return False
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    self.url,
                    params={"uid": self.uid, "sendkey": self.sendkey, "title": title, "content": content}
                )
                return response.status_code == 200
        except Exception as e:
            logger.error(f"Server酱通知失败: {e}")
            return False


class PushDeerChannel(NotificationChannel):
    def __init__(self, pushkey: str):
        self.pushkey = pushkey
        self.url = "https://api.pushdeer.com/push"

    async def send(self, title: str, content: str) -> bool:
        if not self.pushkey:
            return False
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    self.url,
                    data={"pushkey": self.pushkey, "text": content, "desp": title, "type": "markdown"}
                )
                return response.status_code == 200
        except Exception as e:
            logger.error(f"PushDeer通知失败: {e}")
            return False


class Notifier:
    def __init__(self):
        self.enabled = config.get("notification.enabled", True)
        self.include_statistics = config.get("notification.include_statistics", True)
        self.only_on_failure = config.get("notification.only_on_failure", False)
        self.channels: List[NotificationChannel] = []
        self._init_channels()

    def _init_channels(self):
        if config.get("notification.bark.enabled", False):
            bark_key = config.get("notification.bark.device_key", "")
            if bark_key:
                bark_server = config.get("notification.bark.server", "https://api.day.app")
                bark_sound = config.get("notification.bark.sound", "")
                self.channels.append(BarkChannel(bark_server, bark_key, bark_sound))

        if config.get("notification.pushplus.enabled", False):
            pushplus_token = config.get("notification.pushplus.token", "")
            if pushplus_token:
                self.channels.append(PushPlusChannel(pushplus_token))

        if config.get("notification.telegram.enabled", False):
            bot_token = config.get("notification.telegram.bot_token", "")
            chat_id = config.get("notification.telegram.chat_id", "")
            if bot_token and chat_id:
                self.channels.append(TelegramChannel(bot_token, chat_id))

        if config.get("notification.wxpusher.enabled", False):
            spt = config.get("notification.wxpusher.spt", "")
            if spt:
                self.channels.append(WxPusherChannel(spt))

        if config.get("notification.ntfy.enabled", False):
            server = config.get("notification.ntfy.server", "https://ntfy.sh")
            topic = config.get("notification.ntfy.topic", "")
            token = config.get("notification.ntfy.token", "")
            if topic:
                self.channels.append(NtfyChannel(server, topic, token))

        if config.get("notification.feishu.enabled", False):
            webhook_url = config.get("notification.feishu.webhook_url", "")
            msg_type = config.get("notification.feishu.msg_type", "text")
            if webhook_url:
                self.channels.append(FeishuChannel(webhook_url, msg_type))

        if config.get("notification.wework.enabled", False):
            webhook_url = config.get("notification.wework.webhook_url", "")
            msg_type = config.get("notification.wework.msg_type", "text")
            if webhook_url:
                self.channels.append(WeWorkChannel(webhook_url, msg_type))

        if config.get("notification.dingtalk.enabled", False):
            webhook_url = config.get("notification.dingtalk.webhook_url", "")
            msg_type = config.get("notification.dingtalk.msg_type", "text")
            if webhook_url:
                self.channels.append(DingTalkChannel(webhook_url, msg_type))

        if config.get("notification.gotify.enabled", False):
            server = config.get("notification.gotify.server", "")
            token = config.get("notification.gotify.token", "")
            priority = config.get("notification.gotify.priority", 5)
            if server and token:
                self.channels.append(GotifyChannel(server, token, priority))

        if config.get("notification.serverchan3.enabled", False):
            uid = config.get("notification.serverchan3.uid", "")
            sendkey = config.get("notification.serverchan3.sendkey", "")
            if uid and sendkey:
                self.channels.append(ServerChan3Channel(uid, sendkey))

        if config.get("notification.pushdeer.enabled", False):
            pushkey = config.get("notification.pushdeer.pushkey", "")
            if pushkey:
                self.channels.append(PushDeerChannel(pushkey))

    def _should_notify(self, notif_type: NotificationType) -> bool:
        if not self.enabled:
            return False
        if self.only_on_failure:
            failure_types = {
                NotificationType.LOGIN_FAILED,
                NotificationType.READING_INTERRUPTED,
                NotificationType.READING_FAILED,
                NotificationType.SESSION_FAILURE,
                NotificationType.COOKIES_EXPIRED,
                NotificationType.CONTAINER_EXIT,
                NotificationType.RUNTIME_ERROR,
            }
            return notif_type in failure_types
        return True

    async def send(self, message: str, notif_type: NotificationType = NotificationType.GENERAL, **kwargs) -> bool:
        if not self._should_notify(notif_type):
            return True

        if not self.channels:
            logger.debug("未配置通知通道")
            return False

        title = self._get_title(notif_type)
        body = self._format_body(message, notif_type, **kwargs)

        results = await asyncio.gather(
            *[channel.send(title, body) for channel in self.channels],
            return_exceptions=True
        )

        success_count = sum(1 for r in results if r is True)
        if success_count > 0:
            logger.info(f"通知发送成功: {title} (成功: {success_count}/{len(self.channels)})")
            return True
        else:
            logger.error(f"所有通知通道发送失败")
            return False

    def _get_title(self, notif_type: NotificationType) -> str:
        return "微信读书自动阅读"

    def _format_body(self, message: str, notif_type: NotificationType, **kwargs) -> str:
        return message

    async def notify_startup(self):
        await self.send(f"weread-auto-reader 已启动", NotificationType.STARTUP)

    async def notify_login_success(self, user_info: Dict[str, Any] = None):
        msg = "微信读书登录成功"
        if user_info and user_info.get("nickname"):
            msg += f"\n用户: {user_info['nickname']}"
        await self.send(msg, NotificationType.LOGIN_SUCCESS)

    async def notify_login_failed(self, reason: str):
        await self.send(f"登录失败: {reason}\n请检查日志并重新扫码登录", NotificationType.LOGIN_FAILED)

    async def notify_reading_start(self, book_name: str = None, duration: str = None):
        msg = f"开始阅读"
        if book_name:
            msg += f"\n书籍: {book_name}"
        if duration:
            msg += f"\n目标: {duration} 分钟"
        await self.send(msg, NotificationType.READING_START)

    async def notify_reading_complete(self, elapsed_minutes: float, target_minutes: int, books_read: int = 1):
        await self.send(
            f"阅读完成！\n实际 {elapsed_minutes:.0f} 分钟\n读书 {books_read} 本",
            NotificationType.READING_COMPLETE
        )

    async def notify_reading_interrupted(self, reason: str, retry_count: int = 0):
        msg = f"阅读中断: {reason}"
        if retry_count > 0:
            msg += f"\n已自动重试 {retry_count} 次"
        await self.send(msg, NotificationType.READING_INTERRUPTED)

    async def notify_reading_failed(self, reason: str):
        await self.send(f"阅读失败: {reason}\n请检查日志", NotificationType.READING_FAILED)

    async def notify_session_success(self, user: str, duration: float, reads: int):
        await self.send(
            f"会话成功\n用户: {user}\n时长: {duration:.0f} 分钟\n请求: {reads} 次",
            NotificationType.SESSION_SUCCESS
        )

    async def notify_session_failure(self, user: str, reason: str):
        await self.send(f"会话失败\n用户: {user}\n原因: {reason}", NotificationType.SESSION_FAILURE)

    async def notify_multi_user_summary(self, total: int, success: int, failed: int, minutes: float):
        await self.send(
            f"多用户阅读完成\n总用户: {total}\n成功: {success}\n失败: {failed}\n总时长: {minutes:.0f} 分钟",
            NotificationType.MULTI_USER_SUMMARY
        )

    async def notify_runtime_error(self, error: str):
        await self.send(f"运行时错误: {error}", NotificationType.RUNTIME_ERROR)

    async def notify_cookies_expired(self):
        await self.send("Cookies 已过期\n请重新扫码登录", NotificationType.COOKIES_EXPIRED)

    async def notify_container_exit(self, reason: str):
        await self.send(f"容器异常退出\n原因: {reason}\n正在重启...", NotificationType.CONTAINER_EXIT)


notifier = Notifier()
