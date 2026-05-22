import os
import yaml
from pathlib import Path
from typing import Any, Dict, Optional, List
from copy import deepcopy


class Config:
    _instance = None
    _config: Dict[str, Any] = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._config_file = os.environ.get("CONFIG_FILE", "shared/config.yaml")
        self._load()

    def _load(self):
        config_path = Path(self._config_file)
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f) or {}
            defaults = self._get_default_config()
            self._deep_merge(defaults, loaded)
            self._config = defaults
        else:
            self._config = self._get_default_config()
            self.save()

    def _deep_merge(self, base: dict, override: dict):
        for k, v in override.items():
            if k in base and isinstance(base[k], dict) and isinstance(v, dict):
                self._deep_merge(base[k], v)
            else:
                base[k] = v

    def _get_default_config(self) -> Dict[str, Any]:
        return {
            "app": {
                "name": "weread-auto-reader",
                "version": "1.0.0",
                "host": "0.0.0.0",
                "port": 8000,
                "startup_mode": "immediate",
                "startup_delay": "1-10",
            },
            "reading": {
                "target_duration": "30-60",
                "mode": "smart_random",
                "reading_interval": "25-35",
                "book_continuity": 0.8,
                "chapter_continuity": 0.7,
                "break_probability": 0.1,
                "break_duration": "10-20",
                "books": []
            },
            "users": [],
            "schedule": {
                "enabled": True,
                "cron_expression": "0 */2 * * *",
                "timezone": "Asia/Shanghai"
            },
            "daemon": {
                "enabled": False,
                "session_interval": "120-180",
                "max_daily_sessions": 12
            },
            "notification": {
                "enabled": True,
                "include_statistics": True,
                "only_on_failure": False,
                "bark": {
                    "enabled": False,
                    "server": "https://api.day.app",
                    "device_key": ""
                },
                "pushplus": {
                    "enabled": False,
                    "token": ""
                },
                "telegram": {
                    "enabled": False,
                    "bot_token": "",
                    "chat_id": ""
                },
                "wxpusher": {
                    "enabled": False,
                    "spt": ""
                },
                "ntfy": {
                    "enabled": False,
                    "server": "https://ntfy.sh",
                    "topic": ""
                },
                "feishu": {
                    "enabled": False,
                    "webhook_url": "",
                    "msg_type": "text"
                },
                "wework": {
                    "enabled": False,
                    "webhook_url": "",
                    "msg_type": "text"
                },
                "dingtalk": {
                    "enabled": False,
                    "webhook_url": "",
                    "msg_type": "text"
                },
                "gotify": {
                    "enabled": False,
                    "server": "",
                    "token": "",
                    "priority": 5
                },
                "serverchan3": {
                    "enabled": False,
                    "uid": "",
                    "sendkey": ""
                },
                "pushdeer": {
                    "enabled": False,
                    "pushkey": ""
                },
            },
            "browser": {
                "headless": True,
                "user_agent": ""
            },
            "network": {
                "timeout": 30,
                "retry_times": 3,
                "retry_delay": "5-15",
                "rate_limit": 10
            },
            "human_simulation": {
                "enabled": True,
                "rotate_user_agent": False,
                "user_agents": [
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
                ]
            },
            "hack": {
                "cookie_refresh_ql": False
            },
            "history": {
                "enabled": True,
                "file": "shared/logs/run-history.json",
                "max_entries": 50
            },
            "log": {
                "level": "INFO",
                "file": "shared/logs/weread.log"
            }
        }

    def save(self):
        config_path = Path(self._config_file)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(self._config, f, allow_unicode=True, sort_keys=False)

    def get(self, key: str, default: Any = None) -> Any:
        keys = key.split(".")
        value = self._config
        for k in keys:
            if isinstance(value, dict):
                value = value.get(k)
            else:
                return default
            if value is None:
                return default
        return value

    def set(self, key: str, value: Any):
        keys = key.split(".")
        config = self._config
        for k in keys[:-1]:
            if k not in config:
                config[k] = {}
            config = config[k]
        config[keys[-1]] = value

    def update(self, updates: Dict[str, Any]):
        def deep_update(d, u):
            for k, v in u.items():
                if isinstance(v, dict) and k in d and isinstance(d[k], dict):
                    deep_update(d[k], v)
                else:
                    d[k] = v
        deep_update(self._config, updates)
        self.save()

    @property
    def all(self) -> Dict[str, Any]:
        return deepcopy(self._config)

    def get_users(self) -> List[Dict[str, Any]]:
        """获取用户列表"""
        return self.get("users", [])

    def add_user(self, user: Dict[str, Any]) -> bool:
        """添加用户"""
        users = self.get_users()
        users.append(user)
        self.set("users", users)
        self.save()
        return True

    def remove_user(self, user_name: str) -> bool:
        """移除用户"""
        users = self.get_users()
        users = [u for u in users if u.get("name") != user_name]
        self.set("users", users)
        self.save()
        return True

    def rename_user(self, old_name: str, new_name: str) -> bool:
        """重命名用户"""
        users = self.get_users()
        for u in users:
            if u.get("name") == old_name:
                u["name"] = new_name
                self.set("users", users)
                self.save()
                return True
        return False


config = Config()
