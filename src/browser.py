import asyncio
import json
import re
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Playwright

from src.utils.logger import logger
from src.config import config
from src.cookie_manager import cookie_manager
from src.credential_manager import credential_manager, UserCredentials


class BrowserManager:
    def __init__(self):
        self.playwright: Optional[Playwright] = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self._lock = asyncio.Lock()
        self._login_page: Optional[Page] = None
        self._login_status: str = "idle"
        self._login_error: str = ""
        self._current_user: str = "default"
        self.last_captured = {}
        self.last_captured_at = ""
        self._pending_login_data: Optional[Dict[str, Any]] = None

    def get_captured_info(self) -> Dict[str, Any]:
        return {
            "has_captured": bool(self.last_captured.get("full_payload")),
            "captured_at": self.last_captured_at,
            "book_id": self.last_captured.get("b", ""),
            "appId": str(self.last_captured.get("appId", ""))[:20],
            "ps": str(self.last_captured.get("ps", ""))[:12] + "...",
            "pc": str(self.last_captured.get("pc", ""))[:12] + "...",
            "has_sg": bool(self.last_captured.get("sg", "")),
            "has_s": bool(self.last_captured.get("s", "")),
        }

    async def initialize(self, headless: bool = None):
        if self.browser:
            return

        async with self._lock:
            if self.browser:
                return

            logger.info("初始化 Playwright 浏览器...")
            self.playwright = await async_playwright().start()

            if headless is None:
                headless = config.get("browser.headless", True)
            user_agent = config.get("browser.user_agent", "")

            self.browser = await self.playwright.chromium.launch(
                headless=headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                    "--disable-setuid-sandbox"
                ]
            )

            storage_state = None
            cookies = cookie_manager.load()
            if cookies:
                storage_state = {"cookies": cookies}

            default_user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            self.context = await self.browser.new_context(
                user_agent=user_agent if user_agent else default_user_agent,
                viewport={"width": 1920, "height": 1080},
                storage_state=storage_state
            )

            self.page = await self.context.new_page()
            try:
                await self.page.goto("https://weread.qq.com/", timeout=15000, wait_until="domcontentloaded")
                await asyncio.sleep(2)
            except:
                pass
            logger.info("浏览器初始化完成")

    def set_current_user(self, user_name: str):
        """设置当前登录用户"""
        self._current_user = user_name

    async def capture_api_params(self) -> Dict[str, Any]:
        """通过Playwright拦截真实API请求，提取有效签名参数 and 完整curl"""
        captured = {}
        if not self.page:
            return captured

        async def on_request(request):
            if "/web/book/read" in request.url and not captured:
                try:
                    body = request.post_data
                    if body:
                        data = json.loads(body)
                        captured["appId"] = data.get("appId", "")
                        captured["b"] = data.get("b", "")
                        captured["c"] = data.get("c", "")
                        captured["ps"] = data.get("ps", "")
                        captured["pc"] = data.get("pc", "")
                        captured["ci"] = data.get("ci", 0)
                        captured["co"] = data.get("co", 0)
                        captured["sm"] = data.get("sm", "")
                        captured["pr"] = data.get("pr", 0)
                        captured["full_payload"] = data
                        captured["url"] = request.url
                        captured["headers"] = dict(request.headers)
                        try:
                            captured["_raw_cookies"] = await self.context.cookies()
                        except:
                            captured["_raw_cookies"] = []
                        logger.info(f"捕获API请求: appId={captured.get('appId','')[:20]} book={captured.get('b','')[:20]} ps={captured.get('ps','')[:16]}...")
                except Exception as e:
                    logger.warning(f"API请求捕获失败: {e}")

        self.page.on("request", on_request)
        try:
            saved_url = self.page.url
            books = config.get("reading.books", [])
            book_id = books[0].get("book_id", "") if books else ""
            if not book_id:
                book_id = captured.get("b", "bc432df0813ab6be3g011169")
            reader_url = f"https://weread.qq.com/web/reader/{book_id}"
            await self.page.goto(reader_url, timeout=15000, wait_until="domcontentloaded")
            await asyncio.sleep(4)
            if not captured:
                await self.page.evaluate("window.scrollBy(0, 200)")
                await asyncio.sleep(3)
        except Exception as e:
            logger.warning(f"API捕获导航失败: {e}")
        try:
            self.page.remove_listener("request", on_request)
        except:
            pass

        if captured.get("full_payload"):
            self.last_captured = captured
            self.last_captured_at = datetime.now().strftime("%m-%d %H:%M:%S")

        return captured

    async def capture_and_save_curl(self, user_name: str = "default", retry: int = 1) -> bool:
        """捕获完整curl命令并保存到文件，然后解析文件填入凭证
        
        Args:
            user_name: 用户名
            retry: 解析失败时自动重试捕获的次数
        """
        capt = await self.capture_api_params()
        if not capt.get("ps") or not capt.get("pc"):
            logger.warning("捕获curl失败: ps/pc为空")
            return False

        url = capt.get("url", "https://weread.qq.com/web/book/read")
        headers = capt.get("headers", {})
        body_json = json.dumps(capt.get("full_payload", {}), ensure_ascii=False, separators=(",", ":"))

        lines = [f"curl '{url}' \\"]
        for key, value in headers.items():
            k = key.lower()
            if k in ("host", "content-length", "sec-", "accept-encoding", "connection", "cookie"):
                continue
            escaped = value.replace("'", "'\\''")
            lines.append(f"  -H '{key}: {escaped}' \\")

        raw_cookies = []
        for c in capt.get("_raw_cookies", []):
            raw_cookies.append(f"{c.get('name','')}={c.get('value','')}")
        if raw_cookies:
            ck_str = "; ".join(raw_cookies)
            lines.append(f"  -H 'cookie: {ck_str.replace(chr(39), chr(39)+chr(92)+chr(39)+chr(39))}' \\")

        user_agent = headers.get("user-agent", "Mozilla/5.0")
        lines.append(f"  -H 'user-agent: {user_agent.replace(chr(39), chr(39)+chr(92)+chr(39)+chr(39))}' \\")
        lines.append(f"  --data-raw '{body_json.replace(chr(39), chr(39)+chr(92)+chr(39)+chr(39))}'")
        curl_text = "\n".join(lines)

        from src.user_data_manager import user_data_manager
        user_data_manager.ensure_user_dir(user_name)
        curl_file = user_data_manager.get_user_dir(user_name) / "curl_command.txt"
        with open(curl_file, "w", encoding="utf-8") as f:
            f.write(curl_text)
        logger.info(f"完整curl已保存到 {curl_file}")

        from src.curl_parser import parse_curl_with_fallback, validate_parsed_data
        parsed, was_valid, missing, errors = parse_curl_with_fallback(curl_text, capt, strict=True)
        
        cp = parsed.get("payload", {})
        ch = parsed.get("headers", {})
        cc = parsed.get("cookies", {})
        
        logger.info(f"curl解析结果: appId={cp.get('appId','')[:20] if cp.get('appId') else 'MISSING'} "
                   f"ps={cp.get('ps','')[:16] if cp.get('ps') else 'MISSING'}... "
                   f"共{len(ch)}个header {len(cc)}个cookie")
        if errors:
            logger.warning(f"解析警告: {'; '.join(errors)}")
        
        if not was_valid and missing:
            logger.warning(f"首次解析缺失字段: {missing}，尝试重新捕获...")
            if retry > 0:
                await asyncio.sleep(2)
                capt_retry = await self.capture_api_params()
                if capt_retry.get("ps") and capt_retry.get("pc"):
                    capt = capt_retry
                    logger.info("重新捕获成功，使用新数据")

        cred = credential_manager.load(user_name)
        if not cred:
            from src.api_reader import UserCredentials
            cred = UserCredentials(user_name=user_name)
        
        cred.user_info["captured_appId"] = cp.get("appId", "")
        cred.user_info["captured_payload"] = cp
        safe_headers = {}
        for k, v in ch.items():
            k_str = str(k)
            v_str = str(v)
            if k_str.lower() in ("cookie", "host", "content-length", "connection", "https"):
                continue
            if ":" in k_str:
                continue
            try:
                k_str.encode("ascii")
                v_str.encode("ascii")
                safe_headers[k_str] = v_str
            except UnicodeEncodeError:
                continue
        cred.user_info["captured_headers"] = safe_headers
        safe_cookies = {}
        for k, v in cc.items():
            k_str = str(k)
            v_str = str(v)
            try:
                k_str.encode("ascii")
                v_str.encode("ascii")
                safe_cookies[k_str] = v_str
            except UnicodeEncodeError:
                continue
        cred.user_info["captured_cookies"] = safe_cookies
        
        if cc.get("wr_skey"):
            cred.wr_skey = cc["wr_skey"]
        elif capt.get("_raw_cookies"):
            for c in capt["_raw_cookies"]:
                if c.get("name") == "wr_skey":
                    cred.wr_skey = c.get("value", "")
                    break
        if cc.get("wr_vid"):
            cred.wr_vid = cc["wr_vid"]
        elif capt.get("_raw_cookies"):
            for c in capt["_raw_cookies"]:
                if c.get("name") == "wr_vid":
                    cred.wr_vid = c.get("value", "")
                    break
        
        is_valid, valid_missing, _ = validate_parsed_data(
            {"payload": cp, "cookies": cc, "headers": ch}, strict=True
        )
        if not is_valid:
            logger.error(f"凭证字段仍然缺失: {valid_missing}，保存可能不完整")
        
        credential_manager.save(cred)

        logger.info(f"curl数据已保存到凭证: appId={cp.get('appId','')[:20] if cp.get('appId') else 'MISSING'} ps={cp.get('ps','')[:16] if cp.get('ps') else 'MISSING'}...")
        return True

    async def start_login_with_qr(self, user_name: str = "default") -> Optional[bytes]:
        self._current_user = user_name
        self._login_status = "waiting"
        self._login_error = ""

        try:
            page = await self.context.new_page()
            logger.info(f"打开微信读书登录页面... (用户: {user_name})")
            await page.goto("https://weread.qq.com/", timeout=30000, wait_until="networkidle")
            await page.wait_for_timeout(3000)

            if "shelf" in page.url:
                logger.info("已登录微信读书")
                cookies = await self.context.cookies()
                cookie_manager.save(cookies)
                await self._extract_and_save_credentials(page, cookies, user_name)
                self._login_status = "success"
                await page.close()
                capt = await self.capture_api_params()
                if capt.get("appId"):
                    cred = credential_manager.load(user_name)
                    if cred:
                        cred.sign_key = capt.get("sg", cred.sign_key)
                        cred.user_info["captured_appId"] = capt.get("appId", "")
                        cred.user_info["captured_payload"] = capt.get("full_payload", {})
                        credential_manager.save(cred)
                        logger.info(f"已保存捕获的API参数: appId={capt['appId'][:20]}")
                return None

            login_clicked = False
            login_btn = page.locator("button, a, .login_btn, .login_button, [class*='login']")
            btns = await login_btn.all()
            for btn in btns:
                text = await btn.text_content()
                if text and "登录" in text:
                    if await btn.is_visible(timeout=2000):
                        await btn.click()
                        login_clicked = True
                        await page.wait_for_timeout(3000)
                        logger.info("已点击登录按钮")
                        break

            qr_info = await page.evaluate("""() => {
                function findQR() {
                    const imgs = document.querySelectorAll('img');
                    for (const img of imgs) {
                        const src = (img.src || '').toLowerCase();
                        const alt = (img.alt || '').toLowerCase();
                        if (src.includes('qrcode') || alt.includes('二维码') || alt.includes('qrcode') || src.includes('qrc')) {
                            const r = img.getBoundingClientRect();
                            if (r.width > 30) return { method: 'img_keyword', x: r.x, y: r.y, w: r.width, h: r.height };
                        }
                    }
                    const qrDivs = document.querySelectorAll('[class*="qrcode"], [class*="qrc"], [id*="qrcode"], [class*="login"]');
                    for (const el of qrDivs) {
                        const img = el.querySelector('img');
                        if (img) {
                            const r = img.getBoundingClientRect();
                            if (r.width > 30) return { method: 'parent_img', x: r.x, y: r.y, w: r.width, h: r.height };
                        }
                        const r = el.getBoundingClientRect();
                        if (r.width > 50 && r.width < 400 && r.height > 50 && r.height < 400) {
                            return { method: 'parent_div', x: r.x, y: r.y, w: r.width, h: r.height };
                        }
                    }
                    for (const img of imgs) {
                        if (img.naturalWidth > 80 && img.naturalWidth < 400 &&
                            Math.abs(img.naturalWidth - img.naturalHeight) < 30) {
                            const r = img.getBoundingClientRect();
                            if (r.width > 50 && r.height > 50) return { method: 'img_square', x: r.x, y: r.y, w: r.width, h: r.height };
                        }
                    }
                    const canvases = document.querySelectorAll('canvas');
                    for (const c of canvases) {
                        const r = c.getBoundingClientRect();
                        if (r.width > 80 && r.width < 500) return { method: 'canvas', x: r.x, y: r.y, w: r.width, h: r.height };
                    }
                    return null;
                }
                let result = findQR();
                if (result) {
                    const pad = 10;
                    result.x = Math.max(0, result.x - pad);
                    result.y = Math.max(0, result.y - pad);
                    result.w = result.w + pad * 2;
                    result.h = result.h + pad * 2;
                    result.found = true;
                } else {
                    result = { found: false };
                }
                return result;
            }""")

            if qr_info.get("found"):
                screenshot = await page.screenshot(type="png", clip={
                    "x": qr_info["x"], "y": qr_info["y"],
                    "width": qr_info["w"], "height": qr_info["h"]
                })
                logger.info(f"二维码截图成功 (方法: {qr_info['method']})")
            else:
                screenshot = await page.screenshot(type="png")
                logger.info("未定位到二维码，回退整页截图")

            self._login_page = page
            self._login_status = "scanning"
            asyncio.create_task(self._wait_login_complete(page, user_name))
            return screenshot

        except Exception as e:
            logger.error(f"启动扫码登录失败: {e}")
            self._login_status = "failed"
            self._login_error = str(e)
            return None

    async def _extract_and_save_credentials(self, page: Page, cookies: list, user_name: str):
        """从登录页面提取凭证"""
        try:
            cookie_dict = {c["name"]: c.get("value", "") for c in cookies}
            wr_skey = cookie_dict.get("wr_skey", "")
            wr_vid = cookie_dict.get("wr_vid", "")

            user_info = {}
            sign_key = ""

            try:
                user_info_str = await page.evaluate("""() => {
                    try {
                        const data = window.__INITIAL_STATE__ || {};
                        if (data.userInfo) return JSON.stringify(data.userInfo);
                        const scripts = document.querySelectorAll('script');
                        for (const s of scripts) {
                            const text = s.textContent || '';
                            if (text.includes('userInfo') || text.includes('nickname')) {
                                const match = text.match(/userInfo["\']?\s*[:=]\s*({[^}]+})/);
                                if (match) return match[1];
                            }
                        }
                        return '{}';
                    } catch(e) { return '{}'; }
                }""")

                if user_info_str and user_info_str != '{}':
                    import json
                    user_info = json.loads(user_info_str)
            except Exception as e:
                logger.warning(f"提取用户信息失败: {e}")

            try:
                sign_key = await page.evaluate("""() => {
                    try {
                        const scripts = document.querySelectorAll('script');
                        for (const s of scripts) {
                            const text = s.textContent || '';
                            const patterns = [
                                /signKey["']?\s*[:=]\s*["']([^"']{16,})["']/,
                                /KEY\s*=\s*["']([^"']{16,})["']/,
                                /signatureKey["']?\s*[:=]\s*["']([^"']{16,})["']/,
                                /secretKey["']?\s*[:=]\s*["']([^"']{16,})["']/,
                                /skey["']?\s*[:=]\s*["']([a-zA-Z0-9]{16,})["']/,
                                /["']([a-z0-9]{24,})["']\s*[:=]\s*["']([a-z0-9]{24,})["']/g
                            ];
                            for (const p of patterns) {
                                const m = text.match(p);
                                if (m) {
                                    const v = m[2] || m[1];
                                    if (v && v.length >= 16) return v;
                                }
                            }
                        }
                        return '';
                    } catch(e) { return ''; }
                }""")
            except Exception as e:
                logger.warning(f"提取签名KEY失败: {e}")

            if not sign_key:
                sign_key = "3c5c8717f3daf09iop3423zafeqoi"

            credentials = UserCredentials(
                user_id=user_info.get("userId", "") or user_info.get("user_id", "") or wr_vid,
                user_name=user_name,
                wr_skey=wr_skey,
                wr_vid=wr_vid,
                sign_key=sign_key,
                user_info=user_info,
                expires_at=(datetime.now() + timedelta(days=7)).isoformat()
            )

            credential_manager.save(credentials)
            logger.info(f"凭证已提取并保存: {user_name}, skey: {wr_skey[:8] if wr_skey else 'N/A'}***")

        except Exception as e:
            logger.error(f"提取凭证失败: {e}")

    async def _wait_login_complete(self, page: Page, user_name: str):
        try:
            for i in range(60):
                try:
                    url = page.url
                except:
                    url = "__closed__"

                cookies = await self.context.cookies()
                cookie_dict = {c["name"]: c.get("value", "") for c in cookies}
                has_key = bool(cookie_dict.get("wr_skey"))

                logger.info(f"登录检测 [{i+1}/60]  wr_skey={'有' if has_key else '无'}  url={url[:80]}")

                if has_key:
                    logger.info(f"检测到 wr_skey cookie，登录成功，URL: {url}")
                    capt = await self.capture_api_params()
                    self._pending_login_data = {
                        "cookies": cookies,
                        "user_info": {},
                        "captured": capt,
                        "page_url": url,
                    }
                    try:
                        user_info_str = await page.evaluate("""() => {
                            try {
                                const data = window.__INITIAL_STATE__ || {};
                                if (data.userInfo) return JSON.stringify(data.userInfo);
                                return '{}';
                            } catch(e) { return '{}'; }
                        }""")
                        if user_info_str and user_info_str != '{}':
                            self._pending_login_data["user_info"] = json.loads(user_info_str)
                    except Exception as e:
                        logger.warning(f"提取用户信息失败: {e}")
                    self._login_status = "need_username"
                    await page.close()
                    return

                if url == "__closed__":
                    logger.warning(f"登录页面异常关闭 (第{i+1}次检测)")
                    break

                await asyncio.sleep(2)

            if self._login_status != "success":
                self._login_status = "failed"
                self._login_error = "登录超时，cookie中未检测到 wr_skey"
        except Exception as e:
            logger.warning(f"登录等待异常: {e}")
            self._login_status = "failed"
            self._login_error = str(e)
        finally:
            if self._login_page:
                try:
                    await self._login_page.close()
                except:
                    pass
                self._login_page = None

    def get_login_status(self) -> dict:
        return {"status": self._login_status, "error": self._login_error, "user": self._current_user}

    def get_login_debug(self) -> dict:
        return {
            "login_status": self._login_status,
            "browser_ok": self.browser is not None,
            "context_ok": self.context is not None,
            "has_main_page": self.page is not None,
            "has_login_page": self._login_page is not None,
            "current_user": self._current_user
        }

    def reset_login_status(self):
        self._login_status = "idle"
        self._login_error = ""
        self._pending_login_data = None

    async def complete_login_with_username(self, user_name: str) -> dict:
        """使用用户名完成登录保存"""
        if not self._pending_login_data:
            return {"status": "error", "message": "没有待保存的登录数据"}
        try:
            from src.user_data_manager import user_data_manager
            data = self._pending_login_data
            cookies = data.get("cookies", [])
            user_info = data.get("user_info", {})
            capt = data.get("captured", {})
            cookie_dict = {c["name"]: c.get("value", "") for c in cookies}
            wr_skey = cookie_dict.get("wr_skey", "")
            wr_vid = cookie_dict.get("wr_vid", "")
            sign_key = capt.get("sg", "") or "3c5c8717f3daf09iop3423zafeqoi"
            credentials = UserCredentials(
                user_id=user_info.get("userId", "") or user_info.get("user_id", "") or wr_vid,
                user_name=user_name,
                wr_skey=wr_skey,
                wr_vid=wr_vid,
                sign_key=sign_key,
                user_info=user_info,
                expires_at=(datetime.now() + timedelta(days=7)).isoformat()
            )
            if capt.get("appId"):
                credentials.user_info["captured_appId"] = capt.get("appId", "")
                credentials.user_info["captured_payload"] = capt.get("full_payload", {})
            user_data_manager.ensure_user_dir(user_name)
            user_data_manager.save_credentials(credentials, user_name)
            user_data_manager.save_cookies(cookies, user_name)
            self._current_user = user_name
            self._login_status = "success"
            self._pending_login_data = None
            logger.info(f"登录完成，用户: {user_name}，开始自动捕获CURL...")
            await self.capture_and_save_curl(user_name)
            logger.info(f"CURL捕获完成")
            return {"status": "ok", "user": user_name}
        except Exception as e:
            logger.error(f"完成登录失败: {e}")
            self._login_error = str(e)
            self._login_status = "failed"
            return {"status": "error", "message": str(e)}

    async def fetch_shelf_books(self) -> list:
        """从书架页面获取用户所有书籍信息"""
        try:
            page = await self.get_page()
            await page.goto("https://weread.qq.com/web/shelf", timeout=15000, wait_until="domcontentloaded")
            await asyncio.sleep(3)
            books = await page.evaluate("""() => {
                try {
                    const data = window.__INITIAL_STATE__ || {};
                    if (data.shelf && data.shelf.bookInfoMap) {
                        const map = data.shelf.bookInfoMap;
                        return Object.values(map).map(function(b) {
                            return {
                                book_id: b.bookId || '',
                                name: b.title || b.book?.title || '',
                                author: b.author || b.book?.author || '',
                                cover: b.cover || b.book?.cover || ''
                            };
                        });
                    }
                } catch(e) {}
                const cards = document.querySelectorAll('[data-bookid], .shelfBookItem, .bookItem, [class*="bookCard"]');
                if (cards.length > 0) {
                    var result = [];
                    cards.forEach(function(c) {
                        var id = c.getAttribute('data-bookid');
                        if (id) result.push({ book_id: id, name: '', author: '', cover: '' });
                    });
                    return result;
                }
                var links = document.querySelectorAll('a[href*="/web/reader/"]');
                var result = [];
                links.forEach(function(a) {
                    var m = a.href.match(/\\/web\\/reader\\/([a-zA-Z0-9_]+)/);
                    if (m) result.push({ book_id: m[1], name: '', author: '', cover: '' });
                });
                return result;
            }""")
            if books:
                logger.info(f"书架获取: {len(books)} 本书")
                for b in books:
                    logger.info(f"  书名: {b.get('name','')}  ID: {b.get('book_id','')}")
            return books or []
        except Exception as e:
            logger.warning(f"获取书架失败: {e}")
            return []

    async def search_book_by_name(self, name: str) -> list:
        """按书名搜索并返回匹配的书籍ID列表"""
        try:
            page = await self.get_page()
            search_url = f"https://weread.qq.com/web/search/global?keyword={urllib.parse.quote(name)}"
            await page.goto(search_url, timeout=15000, wait_until="domcontentloaded")
            await asyncio.sleep(3)
            results = await page.evaluate("""() => {
                try {
                    const data = window.__INITIAL_STATE__ || {};
                    if (data.search && data.search.bookInfoMap) {
                        const map = data.search.bookInfoMap;
                        return Object.values(map).map(function(b) {
                            return {
                                book_id: b.bookId || '',
                                name: b.title || b.book?.title || '',
                                author: b.author || b.book?.author || '',
                                cover: b.cover || b.book?.cover || ''
                            };
                        });
                    }
                } catch(e) {}
                var links = document.querySelectorAll('a[href*="/web/reader/"]');
                var result = [];
                var seen = {};
                links.forEach(function(a) {
                    var m = a.href.match(/\\/web\\/reader\\/([a-zA-Z0-9_]+)/);
                    var id = m ? m[1] : null;
                    if (id && !seen[id]) {
                        seen[id] = true;
                        var title = a.textContent.trim() || a.closest('[class*="item"]')?.textContent?.trim() || '';
                        result.push({ book_id: id, name: title.substring(0, 50), author: '' });
                    }
                });
                return result.slice(0, 10);
            }""")
            if results:
                logger.info(f"搜索 '{name}': 找到 {len(results)} 个结果")
            return results or []
        except Exception as e:
            logger.warning(f"搜索书籍失败: {e}")
            return []

    async def get_preview_screenshot(self) -> Optional[bytes]:
        try:
            p = self.page or self._login_page
            if not p or p.is_closed():
                return None
            return await p.screenshot(type="png")
        except:
            return None

    async def get_page(self) -> Page:
        if not self.page:
            await self.initialize()
        return self.page

    async def close(self):
        async with self._lock:
            if self.page:
                await self.page.close()
                self.page = None
            if self.context:
                await self.context.close()
                self.context = None
            if self.browser:
                await self.browser.close()
                self.browser = None
            if self.playwright:
                await self.playwright.stop()
                self.playwright = None
            logger.info("浏览器已关闭")

    async def restart(self):
        await self.close()
        await asyncio.sleep(1)
        await self.initialize()


browser_manager = BrowserManager()
