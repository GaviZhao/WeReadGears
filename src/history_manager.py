import json
from pathlib import Path
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta, timezone

from src.utils.logger import logger
from src.config import config


class HistoryManager:
    """执行历史管理器"""

    def __init__(self):
        self.enabled = config.get("history.enabled", True)
        self.history_file = Path(config.get("history.file", "shared/logs/run-history.json"))
        self.max_entries = config.get("history.max_entries", 50)
        self.history_file.parent.mkdir(parents=True, exist_ok=True)

    def _load_history(self) -> List[Dict[str, Any]]:
        """加载历史记录"""
        if not self.history_file.exists():
            return []

        try:
            with open(self.history_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, Exception) as e:
            logger.warning(f"历史记录读取失败: {e}")
            return []

    def _save_history(self, history: List[Dict[str, Any]]):
        """保存历史记录"""
        try:
            with open(self.history_file, "w", encoding="utf-8") as f:
                json.dump(history, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"历史记录保存失败: {e}")

    def _append_entry(self, entry: Dict[str, Any]):
        """追加记录"""
        if not self.enabled:
            return

        history = self._load_history()
        history.insert(0, entry)

        if len(history) > self.max_entries:
            history = history[:self.max_entries]

        self._save_history(history)

    def add_reading_entry(self, result: Any, user: str = "default", execution_type: str = "api") -> bool:
        """添加阅读记录"""
        if not self.enabled:
            return False

        try:
            tz = timezone(timedelta(hours=8))
            entry = {
                "type": "reading",
                "recorded_at": datetime.now(tz).isoformat(),
                "execution_type": execution_type,
                "final_status": result.status,
                "user": user,
                "total_duration_seconds": result.elapsed_seconds,
                "total_duration_minutes": result.elapsed_minutes,
                "target_minutes": result.target_minutes,
                "total_reads": result.total_reads if hasattr(result, 'total_reads') else 0,
                "failed_reads": result.failed_reads if hasattr(result, 'failed_reads') else 0,
                "books_read": result.books_read if hasattr(result, 'books_read') else 0,
                "errors": result.errors if hasattr(result, 'errors') else {},
            }
            self._append_entry(entry)
            logger.info(f"历史记录已保存: {user} - {result.status}")
            return True
        except Exception as e:
            logger.error(f"保存历史记录失败: {e}")
            return False

    def add_startup_entry(self, mode: str = "immediate") -> bool:
        """添加启动记录"""
        if not self.enabled:
            return False

        tz = timezone(timedelta(hours=8))
        entry = {
            "type": "startup",
            "recorded_at": datetime.now(tz).isoformat(),
            "execution_type": mode,
            "final_status": "success",
        }
        self._append_entry(entry)
        return True

    def add_error_entry(self, error_type: str, message: str) -> bool:
        """添加错误记录"""
        if not self.enabled:
            return False

        tz = timezone(timedelta(hours=8))
        entry = {
            "type": "error",
            "recorded_at": datetime.now(tz).isoformat(),
            "error_type": error_type,
            "message": message,
            "final_status": "failed",
        }
        self._append_entry(entry)
        return True

    def get_history(self, limit: int = 10) -> List[Dict[str, Any]]:
        """获取历史记录"""
        history = self._load_history()
        return history[:limit]

    def get_last_entry(self) -> Optional[Dict[str, Any]]:
        """获取最新记录"""
        history = self._load_history()
        return history[0] if history else None

    def get_daily_summary(self, date: str = None) -> Dict[str, Any]:
        """获取日统计"""
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        history = self._load_history()
        daily_entries = [h for h in history if h.get("recorded_at", "").startswith(date)]

        total_minutes = sum(h.get("total_duration_minutes", 0) for h in daily_entries)
        sessions = len(daily_entries)
        books = sum(h.get("books_read", 0) for h in daily_entries)
        successful = sum(1 for h in daily_entries if h.get("final_status") == "completed")
        total_reads = sum(h.get("total_reads", 0) for h in daily_entries)

        return {
            "date": date,
            "total_minutes": total_minutes,
            "sessions": sessions,
            "books": books,
            "successful": successful,
            "failed": sessions - successful,
            "total_reads": total_reads,
        }

    def get_statistics(self) -> Dict[str, Any]:
        """获取统计信息"""
        history = self._load_history()
        completed = [h for h in history if h.get("final_status") == "completed"]

        today = datetime.now().strftime("%Y-%m-%d")
        week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

        today_entries = [h for h in completed if h.get("recorded_at", "").startswith(today)]
        week_entries = [h for h in completed if h.get("recorded_at", "") >= week_ago]

        return {
            "today": {
                "total_minutes": sum(h.get("total_duration_minutes", 0) for h in today_entries),
                "sessions": len(today_entries),
                "books": sum(h.get("books_read", 0) for h in today_entries),
                "successful": len(today_entries),
            },
            "week": {
                "total_minutes": sum(h.get("total_duration_minutes", 0) for h in week_entries),
                "sessions": len(week_entries),
                "books": sum(h.get("books_read", 0) for h in week_entries),
                "successful": len(week_entries),
            },
            "total": {
                "total_minutes": sum(h.get("total_duration_minutes", 0) for h in completed),
                "sessions": len(completed),
                "books": sum(h.get("books_read", 0) for h in completed),
                "successful": len(completed),
            }
        }

    def clear_history(self) -> bool:
        """清除历史记录"""
        try:
            if self.history_file.exists():
                self.history_file.unlink()
            logger.info("历史记录已清除")
            return True
        except Exception as e:
            logger.error(f"清除历史记录失败: {e}")
            return False


history_manager = HistoryManager()
