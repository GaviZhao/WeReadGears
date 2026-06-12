import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List

from src.utils.logger import logger
from src.api_reader import UserCredentials


# 2026-06-12: 严禁创建/使用 "default" 目录(用户原话)
# 任何 "default" 或空字符串 user_name 都视为"无用户" → 抛异常 / 返回 False / 跳过
# 不再 fallback 到 default 文件夹(历史坑:会创建脏数据目录污染)
DEFAULT_USER_FORBIDDEN_MSG = (
    "拒绝操作: user_name 为空或 'default'(严禁创建 default 目录)。"
    "请先在 Web UI 扫码登录真实用户。"
)


def _assert_valid_user_name(user_name: str) -> str:
    """统一守卫:user_name 必须非空且非 'default',否则抛 ValueError。

    任何写用户数据的入口(get_user_dir / ensure_user_dir / save_* / load_*)
    都应先调这个,杜绝 default 目录再生。
    """
    if not user_name or not isinstance(user_name, str) or user_name.strip() == "":
        raise ValueError(DEFAULT_USER_FORBIDDEN_MSG)
    if user_name.strip().lower() == "default":
        raise ValueError(DEFAULT_USER_FORBIDDEN_MSG)
    return user_name.strip()


class UserDataManager:
    """用户数据管理器 - 按用户分目录存储在 shared/credentials/{用户名}/"""

    def __init__(self, base_dir: str = "shared/credentials"):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def get_user_dir(self, user_name: str) -> Path:
        """获取用户数据目录(目录名做了 safe_name 转换)"""
        _assert_valid_user_name(user_name)
        safe_name = self.safe_user_name(user_name)
        return self.base_dir / safe_name

    def safe_user_name(self, user_name: str) -> str:
        """对外暴露:把任意 user_name 转成目录安全名。
        供 config 等模块同步使用,避免前后名字不一致导致目录找不到。
        """
        return "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in user_name)

    def ensure_user_dir(self, user_name: str) -> Path:
        """确保用户目录存在

        2026-06-12: 加 user_name 守卫 —— 严禁创建 "default" / 空 user_name 目录
        """
        user_dir = self.get_user_dir(user_name)  # 内含 _assert_valid_user_name
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
        """从独立目录加载用户凭证

        2026-06-12:user_name 为空 / "default" 时返回 None(不抛),保持"查不到就 None"语义
        """
        try:
            user_dir = self.get_user_dir(user_name)  # 守卫会抛
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
        except ValueError:
            # 守卫拒绝的 user_name(空 / "default") → 视作"无此用户"
            return None
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
        """获取所有用户列�?"""
        users = []
        if not self.base_dir.exists():
            return users
        for item in self.base_dir.iterdir():
            if item.is_dir() and (item / "default.json").exists():
                users.append(item.name)
        return sorted(users)

    # ========================================================================
    # 章节缓存 (按用户/书双层目录) - 用于 api_reader 懒加载切书
    # ========================================================================

    def get_chapters_dir(self, user_name: str) -> Path:
        """获取用户下的章节缓存目录 shared/credentials/{user}/chapters/"""
        user_dir = self.ensure_user_dir(user_name)
        chapters_dir = user_dir / "chapters"
        chapters_dir.mkdir(parents=True, exist_ok=True)
        return chapters_dir

    def _safe_book_id(self, book_id: str) -> str:
        """bookId 转安全文件名(去除路径分隔符和特殊字符)"""
        return "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in book_id)

    def get_chapters_path(self, user_name: str, book_id: str) -> Path:
        """获取某本书的章节缓存文件路径"""
        return self.get_chapters_dir(user_name) / f"{self._safe_book_id(book_id)}.json"

    def save_chapters(self, user_name: str, book_id: str, info: Dict[str, Any]) -> bool:
        """保存章节列表到磁盘缓存。

        info 必填字段:
          - bookId: str
          - chapters: list[dict]
          - first_chapter_uid: str
          - first_chapter_idx: int

        info 可选字段(全部透传,不被丢弃):
          - chapters_total: int(用户手动设的总章节数,GET 端点用它覆盖默认)
          - user_set: bool(标记 chapters_total 是用户手动设的)
          - current_ci: int(当前阅读到的 ci 序号)
          - created_by: str(谁创建的,如 "ensure_chapter_json_fast")
          - auto_generated: bool(自动生成 vs 浏览器抓的)
          - updated_at: str(更新时间)

        2026-06-12 修复:之前只白名单 4 个字段,web 改 chapters_total 时 chapters_total/user_set
        字段丢失,导致 GET 永远拿不到用户设的值。改为"以 info 为主,只补必填默认"
        """
        try:
            chapters_path = self.get_chapters_path(user_name, book_id)
            # 透传 info 全部字段,只补必填默认(避免缺字段导致下游崩)
            payload = dict(info)  # 浅拷贝,保留所有传入字段
            payload["bookId"] = str(book_id)
            payload["chapters"] = info.get("chapters", [])
            payload["first_chapter_uid"] = info.get("first_chapter_uid", "")
            payload["first_chapter_idx"] = info.get("first_chapter_idx", 0)
            payload.setdefault("fetched_at", datetime.now().isoformat())
            # 原子写:先写 .tmp 再 rename,避免读到半截文件
            tmp_path = chapters_path.with_suffix(".json.tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            tmp_path.replace(chapters_path)
            logger.info(f"📦 章节缓存已保存: {chapters_path} ({len(payload['chapters'])}章)")
            return True
        except Exception as e:
            logger.error(f"保存章节缓存失败 {user_name}/{book_id}: {e}")
            return False

    def load_chapters(self, user_name: str, book_id: str) -> Optional[Dict[str, Any]]:
        """从磁盘加载章节缓存,失败/不存在/格式错 都返回 None。

        2026-06-12:user_name 为空 / "default" 时返回 None(不抛),保持"查不到就 None"语义
        """
        try:
            chapters_path = self.get_chapters_path(user_name, book_id)  # 守卫会抛
            if not chapters_path.exists():
                return None
            with open(chapters_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                logger.warning(f"章节缓存格式错(非 dict): {chapters_path}")
                return None
            chapters = data.get("chapters")
            if not isinstance(chapters, list) or not chapters:
                logger.warning(f"章节缓存无 chapters: {chapters_path}")
                return None
            # 校验:至少要有 chapterUid
            for c in chapters[:3]:
                if not isinstance(c, dict) or "chapterUid" not in c:
                    logger.warning(f"章节缓存字段异常: {chapters_path}")
                    return None
            return data
        except ValueError:
            # 守卫拒绝的 user_name(空 / "default") → 视作"无此用户"
            return None
        except Exception as e:
            logger.error(f"加载章节缓存失败 {user_name}/{book_id}: {e}")
            return None

    def list_cached_books(self, user_name: str) -> List[str]:
        """列出该用户下所有有章节缓存的 bookId。"""
        books: List[str] = []
        chapters_dir = self.get_user_dir(user_name) / "chapters"
        if not chapters_dir.exists():
            return books
        for f in chapters_dir.glob("*.json"):
            if f.suffix == ".json":
                books.append(f.stem)
        return sorted(books)

    def delete_chapters(self, user_name: str, book_id: str) -> bool:
        """删除单本书的章节缓存(用于强制刷新)。"""
        try:
            chapters_path = self.get_chapters_path(user_name, book_id)
            if chapters_path.exists():
                chapters_path.unlink()
                logger.info(f"🗑️ 章节缓存已删除: {chapters_path}")
            return True
        except Exception as e:
            logger.error(f"删除章节缓存失败 {user_name}/{book_id}: {e}")
            return False

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
        """加载用户的curl命令

        2026-06-12:user_name 为空 / "default" 时返回 None(不抛)
        """
        try:
            user_dir = self.get_user_dir(user_name)
            curl_file = user_dir / "curl_command.txt"
            if not curl_file.exists():
                return None
            return curl_file.read_text(encoding="utf-8").strip()
        except ValueError:
            return None
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
        """加载用户的cookies

        2026-06-12:user_name 为空 / "default" 时返回 None(不抛)
        """
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
        except ValueError:
            return None
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