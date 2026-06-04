import asyncio
import random
from typing import Optional, Dict, Any, Callable, List
from datetime import datetime

from src.utils.logger import logger
from src.config import config
from src.browser import browser_manager


class Reader:
    def __init__(self):
        self.is_reading = False
        self.should_stop = False
        self.current_book: Optional[Dict[str, Any]] = None
        self.start_time: Optional[datetime] = None
        self.elapsed_seconds = 0
        self.progress_callback: Optional[Callable] = None
        self.books_read = 0
        self.last_book_switch_time = 0
        self.breaks_taken = 0
        self.total_break_time = 0

    def set_progress_callback(self, callback: Callable):
        self.progress_callback = callback

    def _parse_duration(self, duration_str: str) -> tuple[int, int]:
        if isinstance(duration_str, int):
            return duration_str, duration_str
        if "-" in str(duration_str):
            parts = str(duration_str).split("-")
            return int(parts[0]), int(parts[1])
        return int(duration_str), int(duration_str)

    def _get_books_config(self) -> List[Dict[str, Any]]:
        return config.get("reading.books", [])

    def _select_book(self) -> Optional[Dict[str, Any]]:
        books = self._get_books_config()
        if not books:
            logger.info("未配置书籍，将浏览书架选择")
            return None

        book_continuity = config.get("reading.book_continuity", 0.8)
        if self.current_book and random.random() < book_continuity:
            book_in_config = any(b.get("book_id") == self.current_book.get("book_id") for b in books)
            if book_in_config:
                logger.info(f"继续阅读当前书籍: {self.current_book.get('name', 'Unknown')}")
                return self.current_book

        selected = random.choice(books)
        logger.info(f"选择书籍: {selected.get('name', 'Unknown')}")
        return selected

    def _get_chapter_url(self, book: Dict[str, Any]) -> Optional[str]:
        book_id = book.get("book_id")
        if not book_id:
            return None

        chapters = book.get("chapters", [])
        if chapters:
            chapter = random.choice(chapters)
            chapter_id = chapter.get("chapter_id") or chapter.get("id") or chapter
            if isinstance(chapter_id, str):
                return f"https://weread.qq.com/web/reader/{chapter_id}"
            return None

        return f"https://weread.qq.com/web/reader/{book_id}"

    async def _navigate_to_book(self, page, book: Optional[Dict[str, Any]] = None) -> bool:
        try:
            if book:
                url = self._get_chapter_url(book)
                if url:
                    logger.info(f"导航到书籍: {book.get('name', 'Unknown')}, URL: {url}")
                    await page.goto(url, timeout=30000)
                    await asyncio.sleep(random.uniform(2, 4))
                    return True
            else:
                logger.info("导航到书架")
                await page.goto("https://weread.qq.com/web/shelf", timeout=30000)
                await asyncio.sleep(random.uniform(1.5, 3))
                return True
        except Exception as e:
            logger.error(f"导航到书籍失败: {e}")
            return False

    def _should_switch_book(self, current_time: int) -> bool:
        book_switch_cooldown = config.get("reading.smart_random.book_switch_cooldown", 300)
        if self.last_book_switch_time > 0 and (current_time - self.last_book_switch_time) < book_switch_cooldown:
            return False
        return True

    async def _human_like_scroll(self, page) -> int:
        """拟真滚动:慢速 + 短间隔(配合 _try_turn_page 做整体翻页模拟)"""
        scroll_amount = random.randint(80, 180)
        current_scroll = await page.evaluate("window.pageYOffset")
        max_scroll = await page.evaluate("document.documentElement.scrollHeight - window.innerHeight")

        if max_scroll < 100:
            return -1  # 标记需要翻页(短页)

        if current_scroll >= max_scroll * 0.85:
            # 滚到底就回到顶部,下一轮再翻页(模拟翻到下一页后继续滚)
            await page.evaluate("window.scrollTo(0, 0)")
            return -2  # 特殊值:已回到顶部,等会儿用 click 翻页

        await page.evaluate(f"window.scrollBy(0, {scroll_amount})")
        return scroll_amount

    async def _try_turn_page(self, page) -> dict:
        """模拟真人翻页:点击右半部分 + 慢节奏。返回 {ok, moved}。"""
        out = {"ok": False, "moved": False}
        try:
            # 记录点击前的 URL + scrollY
            try:
                url_before = page.url
                y_before = await page.evaluate("window.pageYOffset || 0")
            except Exception:
                url_before, y_before = "", 0

            # 微信读书 Web 版翻下一页:点页面右侧
            vp = page.viewport_size or {"width": 1280, "height": 800}
            x = vp["width"] * 0.85
            y = vp["height"] * 0.5
            try:
                await page.mouse.click(x, y, delay=random.randint(80, 200))
                out["ok"] = True
            except Exception:
                # 兜底:键盘翻页
                try:
                    await page.keyboard.press("PageDown")
                    out["ok"] = True
                except Exception:
                    pass
            await asyncio.sleep(random.uniform(1.5, 2.5))

            # 检测是否真的翻页了(URL 变 / scrollY 变 / 内容变)
            try:
                url_after = page.url
                y_after = await page.evaluate("window.pageYOffset || 0")
                if url_after != url_before or abs(y_after - y_before) > 30:
                    out["moved"] = True
            except Exception:
                pass
            return out
        except Exception:
            return out

    async def _try_next_chapter(self, page) -> bool:
        """点击'下一章'按钮(各种 selector 兼容)"""
        try:
            for sel in [
                '.readerChapter_next', '.nextChapter', '.chapter-next',
                'button[class*=next]', 'a[class*=next]',
                'button:has-text("下一章")', 'button:has-text("下一节")',
                '[class*=nextChapter]', '[class*=nextPage]',
            ]:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0:
                        await el.click(timeout=2500)
                        await asyncio.sleep(2)
                        return True
                except Exception:
                    pass
        except Exception:
            pass
        return False

    async def _check_page_visible(self, page) -> bool:
        try:
            is_visible = await page.evaluate("""
                () => {
                    const rect = document.body.getBoundingClientRect();
                    return rect.top < window.innerHeight && rect.bottom > 0;
                }
            """)
            return is_visible
        except:
            return True

    async def start_reading(self, on_progress: Callable = None) -> Dict[str, Any]:
        if self.is_reading:
            logger.warning("已经在阅读中，跳过")
            return {"status": "already_reading"}

        self.is_reading = True
        self.should_stop = False
        self.start_time = datetime.now()
        self.elapsed_seconds = 0
        self.books_read = 0
        self.last_book_switch_time = 0
        self.breaks_taken = 0
        self.total_break_time = 0

        if on_progress:
            self.progress_callback = on_progress

        min_duration, max_duration = self._parse_duration(config.get("reading.target_duration", "60-90"))
        target_minutes = random.randint(min_duration, max_duration)
        target_seconds = target_minutes * 60

        logger.info(f"开始阅读，目标时长: {target_minutes} 分钟")

        # 收集可用的书籍列表(优先 config.books,空就 fetch 书架)
        books = self._get_books_config()
        if not books:
            logger.info("无配置书籍，自动从书架获取...")
            shelf_books = await browser_manager.fetch_shelf_books()
            if shelf_books:
                for b in shelf_books:
                    if b.get("book_id"):
                        books.append({"name": b.get("name",""), "book_id": b["book_id"], "chapters": []})
                logger.info(f"从书架获取 {len(books)} 本书")
        if books:
            logger.info(f"已配置 {len(books)} 本书籍")
        else:
            logger.error("无可用书籍(config 和书架都没有),模拟模式无法开始")
            self.is_reading = False
            return {"status": "error", "error": "无可用书籍,请先配置书籍或确保登录态有效"}

        try:
            page = await browser_manager.get_page()

            # 选书并导航;若失败,持续重试直到选到一本能开的
            book = self._select_book()
            for attempt in range(3):
                if not await self._navigate_to_book(page, book):
                    logger.warning(f"第 {attempt+1} 次导航失败,重选书籍")
                    book = self._select_book()
                    continue
                break
            else:
                # 3 次都失败:fallback 到书架,选能开的
                logger.warning("导航失败 3 次,fallback 到书架")
                await page.goto("https://weread.qq.com/web/shelf", timeout=30000)
                await asyncio.sleep(random.uniform(2, 3))
                book = self._select_book()  # 再选一次,可能是 None

            if book is None:
                logger.error("多次尝试后仍无 book,模拟模式无法继续")
                self.is_reading = False
                return {"status": "error", "error": "选不到可用的书籍,fallback 到书架页也无 book"}

            self.current_book = book
            logger.info(f"模拟模式选定书籍: {self.current_book.get('name')} ({self.current_book.get('book_id')})")

            reading_mode = config.get("reading.mode", "smart_random")
            reading_interval_min, reading_interval_max = self._parse_duration(config.get("reading.reading_interval", "10-20"))
            break_prob = config.get("reading.break_probability", 0.02)
            break_min, break_max = self._parse_duration(config.get("reading.break_duration", "2"))
            chapter_continuity = config.get("reading.chapter_continuity", 0.7)

            no_turn_streak = 0
            # 拟真翻页节奏:每 N 次滚动,click 翻页一次
            scroll_count_since_turn = 0
            turns_per_cycle = random.randint(3, 6)  # 每次翻页前滚 3-6 次
            max_consecutive_errors = 0
            while not self.should_stop:
                try:
                    # 1) 滚一屏(短间隔,模拟人眼扫字)
                    scroll_result = await self._human_like_scroll(page)
                    scroll_count_since_turn += 1

                    # 2) 模拟阅读时间(每滚一屏等 2-5s)
                    read_pause = random.uniform(2.0, 5.0)
                    await asyncio.sleep(read_pause)

                    # 3) 累计到次数 → 主动 click 翻页(模拟真人翻页)
                    if scroll_result == -2 or scroll_count_since_turn >= turns_per_cycle:
                        scroll_count_since_turn = 0
                        turns_per_cycle = random.randint(3, 6)
                        # 记录翻页前的状态,用于判断是否真的翻了
                        turn_res = await self._try_turn_page(page)
                        if not turn_res.get("moved"):
                            # 没翻动,先试下一章按钮
                            if await self._try_next_chapter(page):
                                no_turn_streak = 0
                            else:
                                no_turn_streak += 1
                        else:
                            no_turn_streak = 0
                        # 连续翻不动 = 这章/书 完了,换书
                        if no_turn_streak >= 3:
                            logger.info("连续翻页失败,切换到下一本书")
                            new_book = self._select_book()
                            if new_book and (not self.current_book or new_book.get("book_id") != self.current_book.get("book_id")):
                                if await self._navigate_to_book(page, new_book):
                                    self.current_book = new_book
                                    self.books_read += 1
                                    no_turn_streak = 0
                            elif new_book:
                                await page.evaluate("window.scrollTo(0, 0)")
                                no_turn_streak = 0
                    else:
                        no_turn_streak = 0

                    # 4) 休息概率(0.02)
                    if random.random() < break_prob:
                        break_time = random.randint(break_min, break_max)
                        self.breaks_taken += 1
                        self.total_break_time += break_time
                        logger.info(f"模拟休息 #{self.breaks_taken}：{break_time} 秒 (累计休息 {self.total_break_time} 秒)")
                        await asyncio.sleep(break_time)

                    max_consecutive_errors = 0  # 一次成功循环就重置错误计数
                except Exception as loop_err:
                    # 循环内单次异常:记录 + 重试,不退出整个阅读
                    max_consecutive_errors += 1
                    logger.warning(f"阅读循环单次异常 ({max_consecutive_errors}/5): {loop_err}")
                    if max_consecutive_errors >= 5:
                        logger.error(f"连续 5 次循环异常,放弃模拟模式: {loop_err}")
                        self.is_reading = False
                        return {"status": "error", "error": f"loop_consecutive_errors: {loop_err}"}
                    await asyncio.sleep(2)
                    continue

                self.elapsed_seconds = int((datetime.now() - self.start_time).total_seconds())
                active_seconds = self.elapsed_seconds - self.total_break_time
                if active_seconds >= target_seconds:
                    break

                # smart_random 模式偶尔切书
                if reading_mode == "smart_random":
                    if self._should_switch_book(active_seconds) and random.random() < (1 - chapter_continuity):
                        new_book = self._select_book()
                        if new_book and new_book.get("book_id") != self.current_book.get("book_id"):
                            logger.info(f"智能切换到书籍: {new_book.get('name', 'Unknown')}")
                            try:
                                if await self._navigate_to_book(page, new_book):
                                    self.current_book = new_book
                                    self.books_read += 1
                                    self.last_book_switch_time = active_seconds
                            except Exception as e:
                                logger.warning(f"切书失败: {e}")

                if self.progress_callback and active_seconds % 60 < 5:
                    progress = int((active_seconds / target_seconds) * 100)
                    await self.progress_callback({
                        "elapsed": active_seconds,
                        "target": target_seconds,
                        "progress": min(progress, 100),
                        "current_book": self.current_book.get("name") if self.current_book else None
                    })

            active_seconds = self.elapsed_seconds - self.total_break_time
            actual_minutes = active_seconds / 60
            logger.info(f"阅读完成，实际阅读: {actual_minutes:.1f} 分钟，休息 {self.breaks_taken} 次/累计 {self.total_break_time} 秒")

            return {
                "status": "completed",
                "elapsed_seconds": active_seconds,
                "elapsed_minutes": actual_minutes,
                "target_minutes": target_minutes,
                "books_read": self.books_read if self.books_read > 0 else 1,
                "breaks_taken": self.breaks_taken,
                "total_break_time": self.total_break_time
            }

        except Exception as e:
            logger.error(f"阅读过程出错: {e}")
            return {
                "status": "error",
                "error": str(e),
                "elapsed_seconds": max(0, self.elapsed_seconds - self.total_break_time)
            }
        finally:
            self.is_reading = False

    async def stop_reading(self):
        logger.info("停止阅读")
        self.should_stop = True

    def get_status(self) -> Dict[str, Any]:
        active_seconds = max(0, self.elapsed_seconds - self.total_break_time)
        return {
            "is_reading": self.is_reading,
            "elapsed_seconds": active_seconds,
            "total_elapsed": self.elapsed_seconds,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "current_book": self.current_book.get("name") if self.current_book else None
        }


reader = Reader()
