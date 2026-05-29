import os
import json
from pathlib import Path
from typing import Optional, Dict, Any, Callable, List
from datetime import datetime, timedelta

from src.utils.logger import logger
from src.config import config
from src.user_data_manager import user_data_manager


class CookieManager:
    def __init__(self, cookie_file: str = "shared/cookies.json"):
        self.cookie_file = Path(cookie_file)
        self.cookie_file.parent.mkdir(parents=True, exist_ok=True)
        self.notify_callback: Optional[Callable] = None

    def set_notify_callback(self, callback: Callable):
        self.notify_callback = callback

    def save(self, cookies: list, user_name: str = None, user_info: Dict[str, Any] = None):
        if user_name:
            return user_data_manager.save_cookies(cookies, user_name)
        data = {
            "cookies": cookies,
            "user_info": user_info or {},
            "saved_at": datetime.now().isoformat(),
            "expires_at": (datetime.now() + timedelta(days=7)).isoformat()
        }
        with open(self.cookie_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info(f"Cookies 已保存，有效期至 {data['expires_at']}")

    def load(self, user_name: str = None) -> Optional[list]:
        if user_name:
            return user_data_manager.load_cookies(user_name)
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

    def clear(self, user_name: str = None):
        """清除cookies文件"""
        if user_name:
            try:
                user_dir = user_data_manager.get_user_dir(user_name)
                cookies_file = user_dir / "cookies.json"
                if cookies_file.exists():
                    cookies_file.unlink()
                    logger.info(f"用户Cookies已清除: {user_name}")
            except Exception as e:
                logger.error(f"清除用户Cookies失败: {e}")
            return
        try:
            if self.cookie_file.exists():
                self.cookie_file.unlink()
                logger.info("Cookies 已清除")
        except Exception as e:
            logger.error(f"清除Cookies失败: {e}")

    def is_valid(self, user_name: str = None) -> bool:
        if user_name:
            cookies = self.load(user_name)
            if not cookies:
                return False
            cookie_dict = {c.get("name"): c for c in cookies}
            required_cookies = ["wr_skey", "wr_vid"]
            return all(c in cookie_dict for c in required_cookies)
        users = user_data_manager.get_all_users()
        if users:
            for u in users:
                if self.is_valid(u):
                    return True
        cookies = self.load()
        if not cookies:
            return False
        cookie_dict = {c.get("name"): c for c in cookies}
        required_cookies = ["wr_skey", "wr_vid"]
        return all(c in cookie_dict for c in required_cookies)

    def is_expiring_soon(self, hours: int = 24, user_name: str = None) -> bool:
        if user_name:
            cookies = self.load(user_name)
            if not cookies:
                return False
            cookie_dict = {c.get("name"): c for c in cookies}
            required_cookies = ["wr_skey", "wr_vid"]
            if not all(c in cookie_dict for c in required_cookies):
                return False
            try:
                user_dir = user_data_manager.get_user_dir(user_name)
                cookies_file = user_dir / "cookies.json"
                if not cookies_file.exists():
                    return False
                with open(cookies_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                expires_at = datetime.fromisoformat(data.get("expires_at", datetime.now().isoformat()))
                return datetime.now() + timedelta(hours=hours) > expires_at
            except (json.JSONDecodeError, KeyError, ValueError):
                return False
        if not self.cookie_file.exists():
            return False
        try:
            with open(self.cookie_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            expires_at = datetime.fromisoformat(data.get("expires_at", datetime.now().isoformat()))
            return datetime.now() + timedelta(hours=hours) > expires_at
        except (json.JSONDecodeError, KeyError, ValueError):
            return False

    def get_all_valid_users(self) -> List[str]:
        """获取所有有效用户"""
        users = user_data_manager.get_all_users()
        return [u for u in users if self.is_valid(u)]

    def get_expiry_info(self, user_name: str = None) -> Optional[Dict[str, str]]:
        if user_name:
            cookies = self.load(user_name)
            if not cookies:
                return None
            try:
                user_dir = user_data_manager.get_user_dir(user_name)
                cookies_file = user_dir / "cookies.json"
                if not cookies_file.exists():
                    return None
                with open(cookies_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return {
                    "saved_at": data.get("saved_at", "未知"),
                    "expires_at": data.get("expires_at", "未知"),
                    "user_info": str(data.get("user_info", {}))
                }
            except (json.JSONDecodeError, KeyError, ValueError):
                return None
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

    def get_cookie_dict(self, user_name: str = None) -> Dict[str, str]:
        cookies = self.load(user_name)
        if not cookies:
            return {}
        return {c.get("name"): c.get("value") for c in cookies}


cookie_manager = CookieManager()
