import os
import json
from pathlib import Path
from typing import Optional, Dict, Any, Callable
from datetime import datetime, timedelta

from src.utils.logger import logger
from src.config import config


class CookieManager:
    def __init__(self, cookie_file: str = "shared/cookies.json"):
        self.cookie_file = Path(cookie_file)
        self.cookie_file.parent.mkdir(parents=True, exist_ok=True)
        self.notify_callback: Optional[Callable] = None

    def set_notify_callback(self, callback: Callable):
        self.notify_callback = callback

    def save(self, cookies: list, user_info: Dict[str, Any] = None):
        data = {
            "cookies": cookies,
            "user_info": user_info or {},
            "saved_at": datetime.now().isoformat(),
            "expires_at": (datetime.now() + timedelta(days=7)).isoformat()
        }
        with open(self.cookie_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info(f"Cookies 已保存，有效期至 {data['expires_at']}")

    def load(self) -> Optional[list]:
        if not self.cookie_file.exists():
            return None

        try:
            with open(self.cookie_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            expires_at = datetime.fromisoformat(data.get("expires_at", datetime.now().isoformat()))
            if datetime.now() > expires_at:
                logger.warning("Cookies 已过期")
                return None

            return data.get("cookies")
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.error(f"Cookies 解析失败: {e}")
            return None

    def clear(self):
        """清除cookies文件"""
        try:
            if self.cookie_file.exists():
                self.cookie_file.unlink()
                logger.info("Cookies 已清除")
        except Exception as e:
            logger.error(f"清除Cookies失败: {e}")

    def is_valid(self) -> bool:
        cookies = self.load()
        if not cookies:
            return False

        cookie_dict = {c.get("name"): c for c in cookies}
        required_cookies = ["wr_skey", "wr_vid"]
        return all(c in cookie_dict for c in required_cookies)

    def is_expiring_soon(self, hours: int = 24) -> bool:
        if not self.cookie_file.exists():
            return False

        try:
            with open(self.cookie_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            expires_at = datetime.fromisoformat(data.get("expires_at", datetime.now().isoformat()))
            return datetime.now() + timedelta(hours=hours) > expires_at
        except (json.JSONDecodeError, KeyError, ValueError):
            return False

    async def validate_with_request(self, page) -> bool:
        try:
            cookies = self.load()
            if not cookies:
                return False

            cookie_dict = {c.get("name"): c for c in cookies}
            if "wr_skey" not in cookie_dict or "wr_vid" not in cookie_dict:
                logger.warning("Cookies 缺少必需的认证字段")
                return False

            response = await page.request.get("https://weread.qq.com/", timeout=10)
            if response.status_code == 200:
                logger.info("Cookies 验证成功")
                return True
            else:
                logger.warning(f"Cookies 验证失败，状态码: {response.status_code}")
                return False
        except Exception as e:
            logger.error(f"Cookies 验证异常: {e}")
            return False

    async def check_and_notify(self, page) -> bool:
        is_valid = await self.validate_with_request(page)

        if not is_valid:
            if self.notify_callback:
                await self.notify_callback()
            return False

        if self.is_expiring_soon(hours=24):
            logger.info("Cookies 即将在24小时内过期")
            if self.notify_callback:
                await self.notify_callback(expiring=True)

        return True

    def get_expiry_info(self) -> Optional[Dict[str, str]]:
        if not self.cookie_file.exists():
            return None

        try:
            with open(self.cookie_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {
                "saved_at": data.get("saved_at", "未知"),
                "expires_at": data.get("expires_at", "未知"),
                "user_info": str(data.get("user_info", {}))
            }
        except (json.JSONDecodeError, KeyError, ValueError):
            return None

    def get_cookie_dict(self) -> Dict[str, str]:
        cookies = self.load()
        if not cookies:
            return {}
        return {c.get("name"): c.get("value") for c in cookies}


cookie_manager = CookieManager()
