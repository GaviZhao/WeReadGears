import json
import os
from pathlib import Path
from typing import Optional, Dict, Any, List
from datetime import datetime

from src.utils.logger import logger
from src.config import config
from src.api_reader import UserCredentials


class CredentialManager:
    """凭证管理器"""

    def __init__(self):
        self.credentials_dir = Path("shared/credentials")
        self.credentials_dir.mkdir(parents=True, exist_ok=True)

    def _get_user_file(self, user_name: str) -> Path:
        """获取用户凭证文件路径"""
        safe_name = "".join(c if c.isalnum() else "_" for c in user_name)
        return self.credentials_dir / f"{safe_name}.json"

    def save(self, credentials: UserCredentials) -> bool:
        """保存用户凭证"""
        try:
            user_file = self._get_user_file(credentials.user_name)
            data = {
                "user_id": credentials.user_id,
                "user_name": credentials.user_name,
                "wr_skey": credentials.wr_skey,
                "wr_vid": credentials.wr_vid,
                "sign_key": credentials.sign_key,
                "user_info": credentials.user_info,
                "expires_at": credentials.expires_at,
                "saved_at": datetime.now().isoformat(),
            }
            with open(user_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.info(f"凭证已保存: {credentials.user_name}")
            return True
        except Exception as e:
            logger.error(f"保存凭证失败: {e}")
            return False

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
