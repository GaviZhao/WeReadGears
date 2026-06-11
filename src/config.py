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
            # 老数据修复:config.yaml 里可能有同名重复用户,启动时去重
            self._dedupe_users_on_load()
        else:
            self._config = self._get_default_config()
            self.save()

    def _dedupe_users_on_load(self):
        """启动时清理 config.yaml 里的同名重复用户记录。

        保留第一次出现的,后续同名记录删掉(避免 '两个 test1 删一个剩下另一个还是 test1' 的脏状态)。
        修复完写回磁盘(只在真的有重复时才写)。
        """
        try:
            users = self._config.get("users") or []
            if not isinstance(users, list):
                return
            seen: set = set()
            deduped: List[Dict[str, Any]] = []
            removed = 0
            for u in users:
                if not isinstance(u, dict):
                    continue
                name = u.get("name", "")
                if not name or name in seen:
                    removed += 1
                    continue
                seen.add(name)
                deduped.append(u)
            if removed > 0:
                self._config["users"] = deduped
                self.save()
                import logging
                logging.getLogger("src.config").warning(
                    f"[config] 启动清理: 删除了 {removed} 条同名重复用户记录"
                )
        except Exception:
            pass

    def _deep_merge(self, base: dict, override: dict):
        for k, v in override.items():
            if k in base and isinstance(base[k], dict) and isinstance(v, dict):
                self._deep_merge(base[k], v)
            else:
                base[k] = v

    def _get_default_config(self) -> Dict[str, Any]:
        return {
            "app": {
                "name": "WeReadGears",
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
                "weekly_reward_reminder": True,
                # 每周奖励提醒:星期几(0=周一 ... 6=周日)和时间(HH:MM,24h)
                # 默认 周日 10:00 — 微信读书周奖励在周日 23:59 截止,留出时间领取
                "weekly_reward_day": 6,
                "weekly_reward_time": "10:00",
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
        """获取用户列表(自动去重,按 name 字段保留第一次出现)。"""
        raw = self.get("users", [])
        seen: set = set()
        out: List[Dict[str, Any]] = []
        for u in raw:
            n = u.get("name", "")
            if not n or n in seen:
                # 同名重复的旧脏数据,跳过
                continue
            seen.add(n)
            out.append(u)
        return out

    def add_user(self, user: Dict[str, Any]) -> bool:
        """添加用户。

        - name 为空 → 拒绝
        - name 已存在 → 拒绝(返回 False),不静默添加重复记录
        """
        name = (user.get("name") or "").strip()
        if not name:
            return False
        users = self.get_users()
        if any(u.get("name") == name for u in users):
            # 已存在同名用户,拒绝添加(避免你描述的两个 test1 死循环)
            return False
        new_user = {**user, "name": name}
        users.append(new_user)
        self.set("users", users)
        self.save()
        return True

    def remove_user(self, user_name: str) -> int:
        """移除用户。返回被删除的记录条数(可能>1,如果旧数据有同名重复)。"""
        users_raw = self.get("users", [])  # 不走 get_users,免得去重掉要删的
        kept = [u for u in users_raw if u.get("name") != user_name]
        removed = len(users_raw) - len(kept)
        if removed > 0:
            self.set("users", kept)
            self.save()
        return removed

    def rename_user(self, old_name: str, new_name: str) -> bool:
        """重命名用户。重命名后 name 与其他用户冲突 → 拒绝。"""
        new_name = (new_name or "").strip()
        if not new_name:
            return False
        users = self.get_users()
        # 检查目标名是否已被占用
        if any(u.get("name") == new_name for u in users) and new_name != old_name:
            return False
        for u in users:
            if u.get("name") == old_name:
                u["name"] = new_name
                self.set("users", users)
                self.save()
                return True
        return False


config = Config()

# 早初始化 logger:任何模块 import config 后再 import utils.logger,就能拿到带 file handler 的 logger
# (关键修复:之前 logger.setup 在 lifespan 里调用,但更早模块的 logger.info() 会先触发无 file handler 的 setup)
try:
    _log_file = config.get("log.file")
    _log_level = config.get("log.level", "INFO")
    if _log_file:
        from src.utils.logger import logger as _logger
        _logger.setup("WeReadGears", log_file=_log_file, level=_log_level)
        import os as _os
        _logger.info(f"📝 logger 早初始化完成: file={_log_file} level={_log_level} cwd={_os.getcwd()}")
except Exception as _e:
    import sys as _sys
    print(f"logger 早初始化失败: {_e}", file=_sys.stderr)
