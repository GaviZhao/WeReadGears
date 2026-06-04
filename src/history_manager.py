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
        """获取统计信息(今日 / 本周 / 总计,均按分钟数展示)"""
        history = self._load_history()
        # 包含"completed" 和"error"(失败也有部分阅读时长累计)
        all_with_minutes = [h for h in history if h.get("total_duration_minutes", 0) > 0]

        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        # 自然周(周一到周日),中国习惯
        weekday = now.weekday()  # 0=周一
        week_start = (now - timedelta(days=weekday)).strftime("%Y-%m-%d")

        today_entries = [h for h in all_with_minutes if h.get("recorded_at", "").startswith(today)]
        week_entries = [h for h in all_with_minutes if h.get("recorded_at", "")[:10] >= week_start]

        return {
            "today": {
                "total_minutes": round(sum(h.get("total_duration_minutes", 0) for h in today_entries), 1),
                "sessions": len(today_entries),
                "books": sum(h.get("books_read", 0) for h in today_entries),
            },
            "week": {
                "total_minutes": round(sum(h.get("total_duration_minutes", 0) for h in week_entries), 1),
                "sessions": len(week_entries),
                "books": sum(h.get("books_read", 0) for h in week_entries),
            },
            "total": {
                "total_minutes": round(sum(h.get("total_duration_minutes", 0) for h in all_with_minutes), 1),
                "sessions": len(all_with_minutes),
                "books": sum(h.get("books_read", 0) for h in all_with_minutes),
            }
        }

    @staticmethod
    def format_minutes(minutes: float) -> Dict[str, Any]:
        """把分钟数格式化为可读字符串 + 数值(>= 60 自动进位为小时)
        返回: {"value": 显示用数值, "unit": "分钟"/"小时", "text": "X 小时 Y 分钟"/"X 分钟"}
        """
        m = float(minutes or 0)
        if m < 60:
            return {
                "value": round(m, 1),
                "unit": "分钟",
                "text": f"{round(m, 1)} 分钟" if m != int(m) else f"{int(m)} 分钟",
            }
        h = int(m // 60)
        rest = m - h * 60
        if rest < 0.1:
            return {"value": h, "unit": "小时", "text": f"{h} 小时"}
        return {
            "value": round(m / 60, 1),
            "unit": "小时",
            "text": f"{h} 小时 {round(rest)} 分钟",
        }

    def get_heatmap_data(self, weeks: int = 9) -> Dict[str, Any]:
        """GitHub contributions 风格:weeks 列(默认 9 周 ≈ 2 个月) × 7 行(周一到周日)
        强度分 5 档:
            level 0 = 0 分钟(未打卡)
            level 1 = 1-29 分钟
            level 2 = 30-59 分钟
            level 3 = 60-89 分钟
            level 4 = 90-119 分钟
            level 5 = >= 120 分钟(2 小时以上)
        """
        history = self._load_history()
        # 按"日期"聚合分钟数
        per_day: Dict[str, float] = {}
        for h in history:
            ts = h.get("recorded_at", "")[:10]
            if not ts:
                continue
            mins = h.get("total_duration_minutes", 0) or 0
            if mins <= 0:
                continue
            per_day[ts] = per_day.get(ts, 0) + mins

        today = datetime.now().date()
        end_sunday = today + timedelta(days=(6 - today.weekday()))
        start_sunday = end_sunday - timedelta(weeks=weeks - 1)

        cells = []
        max_minutes = 0
        for w in range(weeks):
            col = []
            week_start = start_sunday + timedelta(weeks=w)
            for d in range(7):
                day = week_start + timedelta(days=d)
                iso = day.isoformat()
                mins = per_day.get(iso, 0)
                if mins == 0:
                    level = 0
                elif mins < 30:
                    level = 1
                elif mins < 60:
                    level = 2
                elif mins < 90:
                    level = 3
                elif mins < 120:
                    level = 4
                else:
                    level = 5
                col.append({
                    "date": iso,
                    "minutes": round(mins, 1),
                    "level": level,
                    "is_future": day > today,
                })
                if day <= today and mins > max_minutes:
                    max_minutes = mins
            cells.append(col)

        # 月份标签:精确计算每个 col 的"主导月份"(7 天里出现最多的),
        # 然后合并相同月份得到 col 范围,前端用 grid-column span 让 label 横跨该月所有列
        from collections import Counter
        col_month = []
        for col in cells:
            months = [datetime.fromisoformat(c["date"]).month for c in col]
            col_month.append(Counter(months).most_common(1)[0][0])
        month_labels = []
        i = 0
        while i < weeks:
            m = col_month[i]
            j = i
            while j + 1 < weeks and col_month[j + 1] == m:
                j += 1
            if i == 0 or col_month[i - 1] != m:
                month_labels.append({"col": i, "span": j - i + 1, "name": f"{m}月"})
            i = j + 1

        return {
            "weeks": weeks,
            "days": ["周一", "周二", "周三", "周四", "周五", "周六", "周日"],
            "month_labels": month_labels,
            "cells": cells,
            "max_minutes": round(max_minutes, 1),
            "total_days": len([c for col in cells for c in col if c["minutes"] > 0]),
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
