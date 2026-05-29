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


credential_manager = CredentialManager()
