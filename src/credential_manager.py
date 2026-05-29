import json
import os
from pathlib import Path
from typing import Optional, Dict, Any, List
from datetime import datetime

from src.utils.logger import logger
from src.config import config
from src.api_reader import UserCredentials
from src.user_data_manager import user_data_manager


class CredentialManager:
    """凭证管理器 - 委托给UserDataManager处理"""

    def __init__(self):
        pass

    def save(self, credentials: UserCredentials) -> bool:
        """保存用户凭证"""
        return user_data_manager.save_credentials(credentials, credentials.user_name)

    def load(self, user_name: str) -> Optional[UserCredentials]:
        """加载用户凭证"""
        return user_data_manager.load_credentials(user_name)

    def is_valid(self, user_name: str) -> bool:
        """检查凭证是否有效"""
        return user_data_manager.is_valid(user_name)

    def delete(self, user_name: str) -> bool:
        """删除用户凭证"""
        return user_data_manager.delete_user(user_name)

    def get_all_users(self) -> List[str]:
        """获取所有用户"""
        return user_data_manager.get_all_users()

    def get_expiry_info(self, user_name: str) -> Optional[Dict[str, str]]:
        """获取过期信息"""
        cred = self.load(user_name)
        if not cred:
            return None
        return {
            "saved_at": cred.saved_at or "未知",
            "expires_at": cred.expires_at or "未知",
            "user_info": str(cred.user_info),
        }

    def load(self, user_name: str) -> Optional[UserCredentials]:
        """加载用户凭证"""
        try:
            user_file = self._get_user_file(user_name)
            if not user_file.exists():
                return None

            with open(user_file, "r", encoding="utf-8") as f:
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
        cred = self.load(user_name)
        return cred is not None and cred.is_valid()

    def delete(self, user_name: str) -> bool:
        """删除用户凭证"""
        try:
            user_file = self._get_user_file(user_name)
            if user_file.exists():
                user_file.unlink()
                logger.info(f"凭证已删除: {user_name}")
            return True
        except Exception as e:
            logger.error(f"删除凭证失败: {e}")
            return False

    def get_all_users(self) -> List[str]:
        """获取所有用户"""
        users = []
        for f in self.credentials_dir.glob("*.json"):
            users.append(f.stem.replace("_", ""))
        return users

    def get_expiry_info(self, user_name: str) -> Optional[Dict[str, str]]:
        """获取过期信息"""
        cred = self.load(user_name)
        if not cred:
            return None
        return {
            "saved_at": cred.saved_at or "未知",
            "expires_at": cred.expires_at or "未知",
            "user_info": str(cred.user_info),
        }


credential_manager = CredentialManager()
