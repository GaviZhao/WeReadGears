import json
import shutil
from pathlib import Path
from typing import Optional, Dict, Any, List

from src.utils.logger import logger
from src.api_reader import UserCredentials


class UserDataManager:
    """用户数据管理器 - 按用户分目录存储在 shared/credentials/{用户名}/"""

    def __init__(self, base_dir: str = "shared/credentials"):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def get_user_dir(self, user_name: str) -> Path:
        """获取用户数据目录"""
        safe_name = "".join(c if c.isalnum() else "_" for c in user_name)
        return self.base_dir / safe_name

    def ensure_user_dir(self, user_name: str) -> Path:
        """确保用户目录存在"""
        user_dir = self.get_user_dir(user_name)
        user_dir.mkdir(parents=True, exist_ok=True)
        return user_dir

    def save_credentials(self, credentials: UserCredentials, user_name: str) -> bool:
        """保存用户凭证到独立目录"""
        try:
            user_dir = self.ensure_user_dir(user_name)
            cred_file = user_dir / "default.json"
            data = {
                "user_id": credentials.user_id,
                "user_name": credentials.user_name or user_name,
                "wr_skey": credentials.wr_skey,
                "wr_vid": credentials.wr_vid,
                "sign_key": credentials.sign_key,
                "user_info": credentials.user_info,
                "expires_at": credentials.expires_at,
            }
            with open(cred_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.info(f"凭证已保存: {user_dir}")
            return True
        except Exception as e:
            logger.error(f"保存凭证失败: {e}")
            return False

    def load_credentials(self, user_name: str) -> Optional[UserCredentials]:
        """从独立目录加载用户凭证"""
        try:
            user_dir = self.get_user_dir(user_name)
            cred_file = user_dir / "default.json"
            if not cred_file.exists():
                return None
            with open(cred_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            cred = UserCredentials(
                user_id=data.get("user_id", ""),
                user_name=data.get("user_name", user_name),
                wr_skey=data.get("wr_skey", ""),
                wr_vid=data.get("wr_vid", ""),
                sign_key=data.get("sign_key", ""),
                user_info=data.get("user_info", {}),
                expires_at=data.get("expires_at"),
                saved_at=data.get("saved_at"),
            )
            if not cred.is_valid():
                logger.warning(f"凭证无效: {user_name}")
                return None
            return cred
        except Exception as e:
            logger.error(f"加载凭证失败: {e}")
            return None

    def is_valid(self, user_name: str) -> bool:
        """检查凭证是否有效"""
        cred = self.load_credentials(user_name)
        return cred is not None and cred.is_valid()

    def delete_user(self, user_name: str) -> bool:
        """删除用户目录"""
        try:
            user_dir = self.get_user_dir(user_name)
            if user_dir.exists():
                shutil.rmtree(user_dir)
                logger.info(f"用户目录已删除: {user_dir}")
            return True
        except Exception as e:
            logger.error(f"删除用户目录失败: {e}")
            return False

    def get_all_users(self) -> List[str]:
        """获取所有用户列表"""
        users = []
        if not self.base_dir.exists():
            return users
        for item in self.base_dir.iterdir():
            if item.is_dir() and (item / "default.json").exists():
                users.append(item.name)
        return sorted(users)

    def save_curl(self, curl_text: str, user_name: str) -> bool:
        """保存用户的curl命令"""
        try:
            user_dir = self.ensure_user_dir(user_name)
            curl_file = user_dir / "curl_command.txt"
            with open(curl_file, "w", encoding="utf-8") as f:
                f.write(curl_text)
            logger.info(f"CURL已保存: {curl_file}")
            return True
        except Exception as e:
            logger.error(f"保存CURL失败: {e}")
            return False

    def load_curl(self, user_name: str) -> Optional[str]:
        """加载用户的curl命令"""
        try:
            user_dir = self.get_user_dir(user_name)
            curl_file = user_dir / "curl_command.txt"
            if not curl_file.exists():
                return None
            return curl_file.read_text(encoding="utf-8").strip()
        except Exception as e:
            logger.error(f"加载CURL失败: {e}")
            return None

    def save_cookies(self, cookies: list, user_name: str) -> bool:
        """保存用户的cookies"""
        try:
            from datetime import datetime, timedelta
            user_dir = self.ensure_user_dir(user_name)
            cookies_file = user_dir / "cookies.json"
            data = {
                "cookies": cookies,
                "saved_at": datetime.now().isoformat(),
                "expires_at": (datetime.now() + timedelta(days=7)).isoformat(),
            }
            with open(cookies_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.info(f"Cookies已保存: {cookies_file}")
            return True
        except Exception as e:
            logger.error(f"保存Cookies失败: {e}")
            return False

    def load_cookies(self, user_name: str) -> Optional[list]:
        """加载用户的cookies"""
        try:
            from datetime import datetime
            user_dir = self.get_user_dir(user_name)
            cookies_file = user_dir / "cookies.json"
            if not cookies_file.exists():
                return None
            with open(cookies_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            expires_at = datetime.fromisoformat(data.get("expires_at", datetime.now().isoformat()))
            if datetime.now() > expires_at:
                logger.warning(f"Cookies已过期: {user_name}")
                return None
            return data.get("cookies")
        except Exception as e:
            logger.error(f"加载Cookies失败: {e}")
            return None

    def migrate_from_old_structure(self) -> List[str]:
        """从旧结构迁移数据到新结构（shared/credentials/{用户名}/）"""
        migrated = []
        old_base = Path("shared")
        old_cred_dir = old_base / "credentials"
        if not old_cred_dir.exists():
            return migrated
        old_curl = old_base / "curl_command.txt"
        for cred_file in old_cred_dir.glob("*.json"):
            if cred_file.name == "reading_progress.json":
                continue
            user_name = cred_file.stem
            if user_name == "_":
                user_name = "default"
            try:
                with open(cred_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                user_dir = self.ensure_user_dir(user_name)
                new_cred_file = user_dir / "default.json"
                with open(new_cred_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                migrated.append(user_name)
                logger.info(f"迁移凭证: {cred_file} -> {new_cred_file}")
            except Exception as e:
                logger.error(f"迁移凭证失败 {cred_file}: {e}")
        if old_curl.exists():
            try:
                default_dir = self.ensure_user_dir("default")
                new_curl = default_dir / "curl_command.txt"
                shutil.copy2(old_curl, new_curl)
                logger.info(f"迁移CURL: {old_curl} -> {new_curl}")
            except Exception as e:
                logger.error(f"迁移CURL失败: {e}")
        old_progress = old_cred_dir / "reading_progress.json"
        if old_progress.exists():
            try:
                default_dir = self.ensure_user_dir("default")
                new_progress = default_dir / "reading_progress.json"
                shutil.copy2(old_progress, new_progress)
                logger.info(f"迁移进度: {old_progress} -> {new_progress}")
            except Exception as e:
                logger.error(f"迁移进度失败: {e}")
        return migrated


user_data_manager = UserDataManager()