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


def _heal_mojibake(s: str) -> str:
    """如果字符串是"被双重 UTF-8 编码"的(UTF-8 字节被错当成 Latin-1 字符再编码),
    还原为正确的中文。

    典型症状(对比):
      正常 UTF-8: '徐达'         → bytes 0xe5 0xbe 0x90
      双重编码:   'å¾'         → chars U+00E5 U+00BE → bytes 0xc3 0xa5 0xc2 0xbe

    还原公式: encode("latin-1").decode("utf-8")
    触发条件: 还原后必须包含 CJK 字符(U+4E00-U+9FFF),避免误改其他字段
    """
    if not isinstance(s, str) or not s:
        return s
    # 已经包含 CJK 字符:不需要修复
    if any("\u4e00" <= c <= "\u9fff" for c in s):
        return s
    try:
        healed = s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s
    # 还原后必须含 CJK,且不含替换字符
    if any("\u4e00" <= c <= "\u9fff" for c in healed) and "\ufffd" not in healed:
        return healed
    return s


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
        # 标记 cookie 失效 / 需要重新登录(API 或页面访问 401 时设 true)
        self._needs_relogin: bool = False
        self._relogin_reason: str = ""

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

    async def _ensure_fresh_cookies(self):
        """从磁盘 cookie_manager 同步最新 cookies 到当前 context(只增不删)。
        在登录流程完成后,context 不会自动 reload;调用这个方法保证 context 用最新的 wr_skey。
        """
        if not self.context:
            return False
        try:
            user = self._current_user or "default"
            # 默认用户用 "default",其他用户用其名字
            disk_cookies = cookie_manager.load(user)
            if not disk_cookies:
                # 尝试 valid_users[0]
                users = cookie_manager.get_all_valid_users()
                if users:
                    disk_cookies = cookie_manager.load(users[0])
                    if disk_cookies:
                        user = users[0]
            if not disk_cookies:
                return False
            # 转成 playwright cookie 格式
            for c in disk_cookies:
                pc = {
                    "name": c.get("name", ""),
                    "value": c.get("value", ""),
                    "domain": c.get("domain", "weread.qq.com"),
                    "path": c.get("path", "/"),
                }
                if c.get("expires"):
                    try:
                        pc["expires"] = float(c["expires"])
                    except Exception:
                        pass
                if c.get("httpOnly") is not None:
                    pc["httpOnly"] = bool(c["httpOnly"])
                if c.get("secure") is not None:
                    pc["secure"] = bool(c["secure"])
                if c.get("sameSite"):
                    pc["sameSite"] = c["sameSite"]
                try:
                    await self.context.add_cookies([pc])
                except Exception as e:
                    logger.debug(f"add_cookie {pc.get('name')} 失败: {e}")
            return True
        except Exception as e:
            logger.debug(f"同步 cookies 失败: {e}")
            return False

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
            valid_users = cookie_manager.get_all_valid_users()
            if valid_users:
                cookies = cookie_manager.load(valid_users[0])
                if cookies:
                    storage_state = {"cookies": cookies}
                    logger.info(f"加载用户 {valid_users[0]} 的 cookies ({len(cookies)} 个)")
            if not storage_state:
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
        """通过Playwright拦截真实API请求，提取有效签名参数 and 完整curl

        关键修复:只接受 `/web/book/read` 这种"阅读心跳"POST(腾讯用它入账阅读时长)。
        排除常见的误抓请求:
        - /web/book/chapter/* (章节详情页初始化)
        - /web/book/chapterInfos
        - /web/shelf/* (书架)
        - /web/login/* (登录)
        """
        captured = {}
        if not self.page:
            return captured

        # 必须严格匹配 /web/book/read —— 这是计阅读时长的真正 endpoint
        TARGET_URL_PATTERNS = ("/web/book/read",)

        async def on_request(request):
            # 必须是 POST,且 URL 命中 /web/book/read
            if request.method != "POST":
                return
            url = request.url or ""
            if not any(p in url for p in TARGET_URL_PATTERNS):
                return  # 不是阅读心跳 API,静默忽略
            if not request.post_data:
                return
            try:
                data = json.loads(request.post_data)
            except Exception:
                return
            # body 里有 ps/pc 才算有效 API 请求
            if not (data.get("ps") or data.get("pc")):
                return
            # 第一次命中即用 —— 之后的"其它" read 请求忽略(避免被不同章节的请求污染 captured)
            if captured.get("full_payload"):
                return
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
            except Exception:
                captured["_raw_cookies"] = []
            logger.info(f"捕获 /web/book/read 请求: appId={captured.get('appId','')[:20]} book={captured.get('b','')[:20]} ps={captured.get('ps','')[:16]}...")

        self.page.on("request", on_request)
        try:
            books = config.get("reading.books", [])
            book_id = books[0].get("book_id", "") if books else ""
            if not book_id:
                book_id = captured.get("b", "bc432df0813ab6be3g011169")
            # 先尝试阅读页;让页面加载几秒后让前端 JS 发出 /web/book/read 心跳
            reader_url = f"https://weread.qq.com/web/reader/{book_id}"
            try:
                await self.page.goto(reader_url, timeout=12000, wait_until="domcontentloaded")
            except Exception:
                pass
            await asyncio.sleep(5)
            # 触发翻页:点击右半部分,让前端发出 /web/book/read
            if not captured:
                try:
                    vp = self.page.viewport_size or {"width": 1280, "height": 800}
                    await self.page.mouse.click(vp["width"] * 0.85, vp["height"] * 0.5)
                    await asyncio.sleep(3)
                except Exception:
                    pass
            if not captured:
                # fallback:打开书架页
                try:
                    await self.page.goto("https://weread.qq.com/web/shelf", timeout=12000, wait_until="domcontentloaded")
                    await asyncio.sleep(4)
                except Exception as e:
                    logger.warning(f"书架 fallback 导航失败: {e}")
            if not captured:
                try:
                    await self.page.evaluate("window.scrollBy(0, 200)")
                except Exception:
                    pass
                await asyncio.sleep(3)
        except Exception as e:
            logger.warning(f"API捕获导航失败: {e}")
        try:
            self.page.remove_listener("request", on_request)
        except:
            pass

        if not captured.get("full_payload"):
            logger.warning("未捕获到 /web/book/read 请求(可能用户没在阅读页或未滚动翻页)")

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

        # 关键:对 payload 里的中文 string 字段做 mojibake 自愈
        # (抓包链路中可能被双重 UTF-8 编码:sm/co/其它带 CJK 内容的字段)
        for _k in ("sm", "co"):  # sm 必含中文,co 偶尔含(章节名)
            if isinstance(cp.get(_k), str) and cp[_k]:
                _healed = _heal_mojibake(cp[_k])
                if _healed != cp[_k]:
                    logger.info(
                        f"payload.{_k} 字段经 mojibake 修复: "
                        f"{cp[_k][:24]!r}... -> {_healed[:24]!r}..."
                    )
                    cp[_k] = _healed
        
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
        return {
            "status": self._login_status,
            "error": self._login_error,
            "user": self._current_user,
            "needs_relogin": self._needs_relogin,
            "relogin_reason": self._relogin_reason,
        }

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
            self._needs_relogin = False
            self._relogin_reason = ""
            return {"status": "ok", "user": user_name}
        except Exception as e:
            logger.error(f"完成登录失败: {e}")
            self._login_error = str(e)
            self._login_status = "failed"
            return {"status": "error", "message": str(e)}

    async def fetch_shelf_books(self) -> list:
        """从书架页面获取用户所有书籍。三阶段:1) 拦截 shelf API;2) 从 body JSON 抓;
        3) __INITIAL_STATE__ 抓;都失败才回退 DOM 抓 <a href="/reader/...">。"""
        try:
            # 先把磁盘上的最新 cookie 同步到 context,免得用着旧的 wr_skey
            await self._ensure_fresh_cookies()
            page = await self.get_page()
            logger.info(f"书架: 正在加载书架页面...")
            books: list = []
            source = "empty"
            api_event = asyncio.Event()

            async def on_response(response):
                try:
                    url = response.url or ""
                    if not any(p in url for p in (
                        "/web/shelf",
                        "/api/shelf",
                        "/shelf/sync",
                        "/shelf/books",
                    )):
                        return
                    rt = response.request.resource_type
                    if rt in ("image", "stylesheet", "font", "media"):
                        return
                    body = await response.json()
                except Exception:
                    return
                if not isinstance(body, dict):
                    return
                items = (
                    body.get("books")
                    or body.get("records")
                    or (body.get("data") or {}).get("books")
                )
                if not isinstance(items, list):
                    return
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    inner = it.get("bookInfo") if isinstance(it.get("bookInfo"), dict) else it
                    bid = (
                        inner.get("bookId")
                        or inner.get("book_id")
                        or inner.get("id")
                        or it.get("bookId")
                        or it.get("book_id")
                        or it.get("id")
                    )
                    if not bid:
                        continue
                    books.append({
                        "book_id": str(bid),
                        "name": (inner.get("title") or it.get("title") or inner.get("name") or it.get("name") or ""),
                        "author": (inner.get("author") or it.get("author") or inner.get("authorName") or it.get("authorName") or ""),
                    })
                if books:
                    api_event.set()

            async def safe_evaluate(script: str, default=None):
                """evaluate 包装,页面 navigation 中断时不会让整个函数崩"""
                try:
                    return await page.evaluate(script)
                except Exception as e:
                    logger.debug(f"evaluate 中断(可能 navigation): {e}")
                    return default

            page.on("response", on_response)
            try:
                # 用 networkidle 等稳定,但放宽超时避免被 login 跳转搞挂
                try:
                    await page.goto("https://weread.qq.com/web/shelf", timeout=30000, wait_until="domcontentloaded")
                except Exception as e:
                    logger.warning(f"书架: 页面 goto 异常: {e}")
                # 看是不是被踢到登录页
                try:
                    cur_url = page.url
                except Exception:
                    cur_url = ""
                if "login" in cur_url.lower() or "wr_skey" not in (cur_url or ""):
                    # 书架页通常不需要 cookie 也能打开(SSR 失败),但 API 需要
                    logger.warning(f"书架: 当前 url={cur_url[:80]} 可能未登录")
                try:
                    await asyncio.wait_for(api_event.wait(), timeout=6)
                except asyncio.TimeoutError:
                    pass
                await asyncio.sleep(1)
                if books:
                    source = "api"

                # 阶段 2: body innerText 抓 JSON(SSR 情况)
                if not books:
                    body_json = await safe_evaluate("""() => {
                        function tryParse(s) {
                            if (!s) return null;
                            const t = s.trim();
                            if (t.startsWith('{') && t.endsWith('}')) {
                                try { return JSON.parse(t); } catch(e) {}
                            }
                            return null;
                        }
                        const cands = document.querySelectorAll('pre, code, script[type="application/json"]');
                        for (const el of cands) {
                            const r = tryParse(el.textContent || '');
                            if (r) return r;
                        }
                        const bt = document.body ? (document.body.innerText || '') : '';
                        return tryParse(bt);
                    }""")
                    if isinstance(body_json, dict):
                        items = (
                            body_json.get("books")
                            or body_json.get("records")
                            or (body_json.get("data") or {}).get("books")
                        )
                        if isinstance(items, list):
                            for it in items:
                                if not isinstance(it, dict):
                                    continue
                                inner = it.get("bookInfo") if isinstance(it.get("bookInfo"), dict) else it
                                bid = (
                                    inner.get("bookId")
                                    or inner.get("book_id")
                                    or inner.get("id")
                                    or it.get("bookId")
                                )
                                if not bid:
                                    continue
                                books.append({
                                    "book_id": str(bid),
                                    "name": (inner.get("title") or it.get("title") or inner.get("name") or it.get("name") or ""),
                                    "author": (inner.get("author") or it.get("author") or inner.get("authorName") or it.get("authorName") or ""),
                                })
                            if books:
                                source = "body-json"

                # 阶段 3: __INITIAL_STATE__ 兜底
                if not books:
                    ssr = await safe_evaluate("""() => {
                        const st = window.__INITIAL_STATE__ || {};
                        function pickBookInfoMap(n, d) {
                            if (d > 8 || !n || typeof n !== 'object') return null;
                            if (n.bookInfoMap && typeof n.bookInfoMap === 'object') return n.bookInfoMap;
                            for (const k of Object.keys(n)) {
                                const r = pickBookInfoMap(n[k], d + 1);
                                if (r) return r;
                            }
                            return null;
                        }
                        const m = pickBookInfoMap(st, 0);
                        if (!m) return [];
                        return Object.keys(m).map(function(id) {
                            const b = m[id] || {};
                            const inner = (b.bookInfo && typeof b.bookInfo === 'object') ? b.bookInfo : b;
                            return {
                                book_id: inner.bookId || b.bookId || id,
                                name: inner.title || b.title || (b.book && b.book.title) || '',
                                author: inner.author || b.author || (b.book && b.book.author) || ''
                            };
                        });
                    }""")
                    if isinstance(ssr, list) and ssr:
                        books = ssr
                        source = "ssr"

                # 阶段 4: 都没有,滚动 + DOM 抓 <a href="/reader/...">
                if not books:
                    for _ in range(5):
                        await safe_evaluate("window.scrollBy(0, 800)")
                        await asyncio.sleep(1)
                    dom = await safe_evaluate("""() => {
                        const result = [];
                        const seen = new Set();
                        for (const a of document.querySelectorAll('a[href]')) {
                            const h = a.href || '';
                            const m = h.match(/\\/reader\\/([^/?#]+)/);
                            if (m && !seen.has(m[1])) {
                                seen.add(m[1]);
                                const txt = (a.textContent || a.innerText || '').trim().substring(0, 60);
                                result.push({ book_id: m[1], name: txt, author: '' });
                            }
                        }
                        return result;
                    }""")
                    if dom:
                        books = dom
                        source = "dom"
            finally:
                try:
                    page.remove_listener("response", on_response)
                except Exception:
                    pass

            if books:
                # 去重
                seen = set()
                unique = []
                for b in books:
                    bid = b.get("book_id", "")
                    if bid and bid not in seen:
                        seen.add(bid)
                        unique.append(b)
                books = unique
                logger.info(f"书架: 获取到{len(books)}本书 (源={source})")
                for b in books[:5]:
                    logger.info(f"  [{b['book_id']}] {b['name']}")
            else:
                try:
                    cur = page.url
                except Exception:
                    cur = "<unavailable>"
                logger.warning(f"书架: 无结果 url={cur[:80]} (可能未登录或 cookie 过期)")
            return books or []
        except Exception as e:
            logger.warning(f"获取书架失败: {e}")
            return []

    async def search_book_by_name(self, name: str) -> list:
        """向后兼容:按书名搜,只返回最佳 1 条。
        新 UI 应该用 search_books_candidates() 拿多条候选 + 数据来源。
        """
        out = await self.search_books_candidates(name, limit=1)
        return out.get("results", [])

    async def search_books_candidates(self, name: str, limit: int = 8) -> dict:
        """按书名搜,返回多条候选 + 数据来源标注。
        返回:{"query": str, "source": "api"|"ssr"|"dom"|"empty", "count": int, "results": [{book_id, name, author}]}
        三阶段:1) 拦截搜索 API 拿结构化 JSON;2) 读 __INITIAL_STATE__ 的 SSR 数据;
        3) 都失败才回退到 DOM 抓链接 + 跳转 + 二次兜底。
        """
        out = {"query": name, "source": "empty", "count": 0, "results": []}
        if not (name or "").strip():
            return out
        name = name.strip()
        try:
            page = await self.get_page()
            encoded = urllib.parse.quote(name)
            # /web/search/books 是带 DOM 渲染的搜索结果页(有 <a href="/reader/..."> 卡片)
            # /web/search/global 是全局搜索(不渲染 DOM 卡片,纯 API 拉取) — 拿不到路由 ID 元素
            search_url = f"https://weread.qq.com/web/search/books?keyword={encoded}"
            logger.info(f"搜索: {search_url}")

            results: list = []
            source = "empty"
            api_event = asyncio.Event()

            async def on_response(response):
                # 阶段 1: 拦截搜索 API 的 JSON 响应
                try:
                    url = response.url or ""
                    # 微信读书搜索相关路径(放宽,涵盖 SSR 直接渲染 JSON 的情况)
                    if not any(p in url for p in (
                        "/web/search/books",
                        "/web/search/global",
                        "/web/search/result",
                        "/web/search",
                        "/api/search",
                        "/search/books",
                    )):
                        return
                    # 排除静态资源(image/stylesheet/font/media)
                    rt = response.request.resource_type
                    if rt in ("image", "stylesheet", "font", "media"):
                        return
                    # document/fetch/xhr/other 都接受 —— 微信读书把 JSON 直接渲染到 body 里,
                    # 这时 response 的 resource_type 是 "document",不能漏
                    body = await response.json()
                except Exception:
                    return
                if not isinstance(body, dict):
                    return
                # 微信读书搜索接口的字段路径可能改,挨个试
                items = (
                    body.get("books")
                    or body.get("records")
                    or body.get("searchBooks")
                    or (body.get("data") or {}).get("books")
                )
                if not isinstance(items, list):
                    return
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    # 兼容嵌套结构:{bookInfo: {bookId, title, author}} 或 {bookId, title, author}
                    inner = it.get("bookInfo") if isinstance(it.get("bookInfo"), dict) else it
                    bid = (
                        inner.get("bookId")
                        or inner.get("book_id")
                        or inner.get("id")
                        or it.get("bookId")
                        or it.get("book_id")
                        or it.get("id")
                    )
                    if not bid:
                        continue
                    results.append({
                        "book_id": str(bid),
                        "name": (inner.get("title") or it.get("title") or inner.get("name") or it.get("name") or ""),
                        "author": (inner.get("author") or it.get("author") or inner.get("authorName") or it.get("authorName") or ""),
                    })
                if results:
                    api_event.set()

            page.on("response", on_response)
            try:
                await page.goto(search_url, timeout=30000, wait_until="domcontentloaded")
                # 等搜索 API,通常 1-2s 就到,6s 上限
                try:
                    await asyncio.wait_for(api_event.wait(), timeout=6)
                except asyncio.TimeoutError:
                    pass
                # 再给 1s 让并发接口补一刀
                await asyncio.sleep(1)

                if results:
                    source = "api"
                else:
                    # 阶段 2: API 没拿到 —— 微信读书搜索页把 JSON 渲染到 body,先试这个
                    body_json = await page.evaluate("""() => {
                        // 微信读书 SSR 搜索页:body 里直接是 JSON 文本(包在 <pre> 之类容器里)
                        function tryParse(s) {
                            if (!s) return null;
                            const t = s.trim();
                            if (t.startsWith('{') && t.endsWith('}')) {
                                try { return JSON.parse(t); } catch(e) {}
                            }
                            return null;
                        }
                        // 1) 找包含 JSON 的 pre/code/script
                        const cands = document.querySelectorAll('pre, code, script[type="application/json"]');
                        for (const el of cands) {
                            const r = tryParse(el.textContent || '');
                            if (r) return r;
                        }
                        // 2) 退化到 body.innerText
                        const bt = document.body ? (document.body.innerText || '') : '';
                        const r = tryParse(bt);
                        if (r) return r;
                        return null;
                    }""")
                    if isinstance(body_json, dict):
                        items = (
                            body_json.get("books")
                            or body_json.get("records")
                            or body_json.get("searchBooks")
                            or (body_json.get("data") or {}).get("books")
                        )
                        if isinstance(items, list):
                            for it in items:
                                if not isinstance(it, dict):
                                    continue
                                inner = it.get("bookInfo") if isinstance(it.get("bookInfo"), dict) else it
                                bid = (
                                    inner.get("bookId")
                                    or inner.get("book_id")
                                    or inner.get("id")
                                    or it.get("bookId")
                                    or it.get("book_id")
                                    or it.get("id")
                                )
                                if not bid:
                                    continue
                                results.append({
                                    "book_id": str(bid),
                                    "name": (inner.get("title") or it.get("title") or inner.get("name") or it.get("name") or ""),
                                    "author": (inner.get("author") or it.get("author") or inner.get("authorName") or it.get("authorName") or ""),
                                })
                            if results:
                                source = "body-json"

                    # 阶段 3: __INITIAL_STATE__ 兜底
                    if not results:
                        ssr = await page.evaluate("""() => {
                            const st = window.__INITIAL_STATE__ || {};
                            const direct = [
                                st.search && st.search.books,
                                st.search && st.search.records,
                                st.searchBooks,
                                st.globalSearch && st.globalSearch.books,
                                st.bookSearch && st.bookSearch.books,
                            ];
                            for (const c of direct) {
                                if (Array.isArray(c) && c.length) return c;
                            }
                            function walk(node, depth) {
                                if (depth > 6 || !node || typeof node !== 'object') return null;
                                if (Array.isArray(node)) {
                                    const hit = node.find(x => x && (x.bookId || x.book_id || x.id || (x.bookInfo && x.bookInfo.bookId)));
                                    if (hit) return node;
                                    for (const c of node) {
                                        const r = walk(c, depth + 1);
                                        if (r) return r;
                                    }
                                } else {
                                    for (const k of Object.keys(node)) {
                                        const r = walk(node[k], depth + 1);
                                        if (r) return r;
                                    }
                                }
                                return null;
                            }
                            return walk(st, 0) || [];
                        }""")
                        if isinstance(ssr, list):
                            for it in ssr:
                                if not isinstance(it, dict):
                                    continue
                                inner = it.get("bookInfo") if isinstance(it.get("bookInfo"), dict) else it
                                bid = (
                                    inner.get("bookId")
                                    or inner.get("book_id")
                                    or inner.get("id")
                                    or it.get("bookId")
                                    or it.get("book_id")
                                    or it.get("id")
                                )
                                if not bid:
                                    continue
                                results.append({
                                    "book_id": str(bid),
                                    "name": (inner.get("title") or it.get("title") or inner.get("name") or it.get("name") or ""),
                                    "author": (inner.get("author") or it.get("author") or inner.get("authorName") or it.get("authorName") or ""),
                                })
                            if results:
                                source = "ssr"

                    # 阶段 4: 都拿不到才回退到 DOM 抓取
                    if not results:
                        dom = await self._fallback_dom_extract(page, name)
                        if dom:
                            results = dom
                            source = "dom"
            finally:
                try:
                    page.remove_listener("response", on_response)
                except Exception:
                    pass

            # 排序 + 截断
            if results:
                from difflib import SequenceMatcher
                target = name.lower()
                def score(r):
                    t = (r.get("name") or "").lower()
                    if t == target:
                        return 1.0
                    if target and (target in t or (t and t in target)):
                        return 0.8
                    return SequenceMatcher(None, target, t).ratio()
                results.sort(key=score, reverse=True)
                # 去重 bookId
                seen = set()
                unique = []
                for r in results:
                    bid = r.get("book_id", "")
                    if bid and bid not in seen:
                        seen.add(bid)
                        unique.append(r)
                results = unique[:max(1, int(limit))]

            # 阶段 5: 强制走 DOM 扫描拿路由 ID(跟书架导入同一种元素)
            # 这样 book_id 永远是路由 ID 格式(/web/reader/<routeId>),
            # 浏览器模式 / API 模式都能用,跟书架导入拿到的格式一致。
            # bookId 是纯数字不能拼 /web/reader/{id} 用,需要路由 ID。
            #
            # 注意:阶段 4 fallback 可能跳转到了阅读页,所以这里要**强制重新打开**搜索结果页,
            # 确保扫到的是搜索结果 DOM(包含所有候选)。
            # 这一步**总是跑**,即使前面 4 阶段都失败,DOM 拿到的候选也能补全 results。
            try:
                # 强制重新打开搜索结果页(忽略阶段 4 跳转过的页面)
                search_url = f"https://weread.qq.com/web/search/books?keyword={encoded}"
                if "/search/books" not in (page.url or "") or "keyword=" not in (page.url or ""):
                    await page.goto(search_url, timeout=15000, wait_until="domcontentloaded")
                    await page.wait_for_timeout(2500)
                dom_candidates = await self._dom_scan_route_candidates(page, scroll_times=2)

                if dom_candidates:
                    # 5a. 用书名+作者配对,覆盖已有 results 里的纯数字 bookId
                    mapped = 0
                    for r in (results or []):
                        bid = str(r.get("book_id", ""))
                        if bid and not (bid.isdigit() and len(bid) < 12):
                            r.setdefault("id_type", "route")
                            continue
                        route_id = self._match_route_for_book(
                            dom_candidates, r.get("name", ""), r.get("author", "")
                        )
                        if route_id:
                            r["api_book_id"] = bid
                            r["book_id"] = route_id
                            r["id_type"] = "route"
                            mapped += 1
                        else:
                            r["api_book_id"] = bid
                            r["id_type"] = "numeric"

                    # 5b. 如果 results 数 < limit, 用 DOM 候选**补全**(独立加候选)
                    # 这样即使阶段 1/2/3/4 没拿到数据,也能返多个路由 ID 候选
                    if not results:
                        results = []
                    existing_routes = {r.get("book_id") for r in results if r.get("id_type") == "route"}
                    added_from_dom = 0
                    for c in dom_candidates:
                        if len(results) >= int(limit):
                            break
                        rid = c.get("routeId", "")
                        if not rid or rid in existing_routes:
                            continue
                        # 从 text 里抽"前几行"作为书名(text 含书名+作者+介绍)
                        text = (c.get("text") or "").strip()
                        # 取前 30 字符作为简短书名
                        short_name = text[:30].split("\n")[0].strip() if text else "(未知书名)"
                        results.append({
                            "book_id": rid,
                            "name": short_name,
                            "author": "",
                            "id_type": "route",
                        })
                        existing_routes.add(rid)
                        added_from_dom += 1

                    logger.info(
                        f"  DOM 配对: 覆盖 {mapped} 条 + 补全 {added_from_dom} 条 "
                        f"(DOM 候选={len(dom_candidates)}, 现共 {len(results)} 条)"
                    )
                    if source != "empty":
                        source = source + "+dom"
                else:
                    # DOM 也没候选,标记 numeric
                    for r in (results or []):
                        bid = str(r.get("book_id", ""))
                        if bid and bid.isdigit() and len(bid) < 12 and "id_type" not in r:
                            r["api_book_id"] = bid
                            r["id_type"] = "numeric"
            except Exception as e:
                logger.debug(f"DOM 配对失败: {e}")

            if results:
                top = results[0]
                logger.info(
                    f"搜索 '{name}': 命中 {len(results)} 条 (源={source}), "
                    f"最佳={top['name']!r} id={top['book_id']} type={top.get('id_type','?')}"
                )
            else:
                logger.warning(f"搜索 '{name}': 所有阶段都没拿到 bookId")

            out["source"] = source
            out["count"] = len(results)
            out["results"] = results
            return out
        except Exception as e:
            logger.warning(f"搜索书籍失败: {e}")
            return out

    async def _resolve_route_id_by_click(self, name: str, api_book_id: str) -> Optional[str]:
        """登录态下:在搜索结果页点第一条匹配项,等跳转,从 page.url 提取路由 ID。
        没登录态(页面 errCode=-2012)或找不到链接时返回 None。
        假定搜索结果页已经打开。
        """
        try:
            page = await self.get_page()
            # 登录态检查
            body0 = await page.evaluate("() => (document.body && document.body.innerText || '').slice(0, 300)")
            if "errCode" in body0 or "登录超时" in body0:
                return None
            # 找含 api_book_id 的元素,点击(优先 data-book-id 匹配;fallback 文本匹配)
            clicked = False
            try:
                # 微信读书 React 应用,元素可能是 div onClick;用 data-book-id 属性定位
                loc = page.locator(f'[data-book-id="{api_book_id}"]').first
                if await loc.count() > 0:
                    await loc.click(timeout=3000)
                    clicked = True
            except Exception:
                pass
            if not clicked:
                # 退化:用文本匹配
                try:
                    loc = page.get_by_text(name, exact=False).first
                    if await loc.count() > 0:
                        await loc.click(timeout=3000)
                        clicked = True
                except Exception:
                    pass
            if not clicked:
                return None
            # 等跳转
            try:
                await page.wait_for_url(re.compile(r"/reader/|/bookDetail/"), timeout=6000)
            except Exception:
                pass
            await page.wait_for_timeout(2000)
            m = re.search(r"/reader/([^/?#]+)", page.url or "")
            if m:
                route_id = m.group(1)
                # 跟原 api_book_id 相同就不是真路由 ID
                if route_id != str(api_book_id):
                    return route_id
            return None
        except Exception:
            return None

    async def _resolve_route_ids_for_search(self, name: str, book_ids: list) -> dict:
        """在搜索结果页打开后,逐条 click+back 拿每条 bookId 的路由 ID。
        返回 {api_book_id: route_id}。失败/未登录的 bookId 不会出现在结果里。
        总时间控制在 ~6s 内(每条最多 2s)。
        """
        result = {}
        if not book_ids:
            return result
        try:
            page = await self.get_page()
            # 先确保在搜索结果页(/web/search/books 有 DOM 渲染的搜索结果卡片)
            search_url = f"https://weread.qq.com/web/search/books?keyword={urllib.parse.quote(name)}"
            cur = page.url or ""
            if "/search/books" not in cur or "keyword=" not in cur:
                await page.goto(search_url, timeout=15000, wait_until="domcontentloaded")
                await page.wait_for_timeout(2500)
            # 登录态检查
            body0 = await page.evaluate("() => (document.body && document.body.innerText || '').slice(0, 300)")
            if "errCode" in body0 or "登录超时" in body0:
                logger.debug("未登录,跳过路由 ID 解析")
                return result
            for bid in book_ids[:3]:  # 最多解析前 3 条
                bid = str(bid)
                if not bid.isdigit() or len(bid) >= 12:
                    continue
                try:
                    route_id = await asyncio.wait_for(
                        self._click_one_and_get_route_id(bid, name),
                        timeout=2.5
                    )
                    if route_id:
                        result[bid] = route_id
                        logger.info(f"  路由 ID: {bid} -> {route_id}")
                except asyncio.TimeoutError:
                    pass
                except Exception as e:
                    logger.debug(f"  解析 {bid} 失败: {e}")
                # 回到搜索结果页
                try:
                    await page.go_back()
                    await page.wait_for_timeout(800)
                except Exception:
                    # go_back 失败 → 重新 goto
                    try:
                        await page.goto(search_url, timeout=10000, wait_until="domcontentloaded")
                        await page.wait_for_timeout(1500)
                    except Exception:
                        pass
        except Exception as e:
            logger.debug(f"批量解析路由 ID 失败: {e}")
        return result

    async def _click_one_and_get_route_id(self, api_book_id: str, name: str) -> Optional[str]:
        """点击搜索结果中 bookId 匹配的那条,等 URL 变化,返回路由 ID。
        登录态下走 SPA 跳转;登录态失效时,fallback 用 goto /web/reader/<bookId> 让 SPA 重定向到 /web/reader/<routeId>。
        """
        page = await self.get_page()

        # 优先:走"书架导入"思路 — goto 纯数字 bookId 的阅读页,SPA 会重定向到路由 ID
        try:
            target_url = f"https://weread.qq.com/web/reader/{api_book_id}"
            cur_url = page.url or ""
            # 已经在阅读页就不用再 goto
            if api_book_id not in cur_url:
                await page.goto(target_url, timeout=15000, wait_until="domcontentloaded")
                await page.wait_for_timeout(2500)
            # 如果是登录超时页(登录态失效),直接返回 None
            body0 = await page.evaluate("() => (document.body && document.body.innerText || '').slice(0, 300)")
            if "errCode" in body0 or "登录超时" in body0:
                return None
            # 从最终 URL 拿路由 ID
            m = re.search(r"/reader/([^/?#]+)", page.url or "")
            if m:
                route_id = m.group(1)
                if route_id != str(api_book_id):
                    return route_id
        except Exception as e:
            logger.debug(f"goto 拿路由 ID 失败: {e}")

        # 退化:在搜索结果页里 click 元素(React Router 兼容)
        try:
            search_url = f"https://weread.qq.com/web/search/books?keyword={urllib.parse.quote(name)}"
            await page.goto(search_url, timeout=15000, wait_until="domcontentloaded")
            await page.wait_for_timeout(2500)
            # 登录态检查
            body0 = await page.evaluate("() => (document.body && document.body.innerText || '').slice(0, 300)")
            if "errCode" in body0 or "登录超时" in body0:
                return None
            clicked = False
            try:
                loc = page.locator(f'[data-book-id="{api_book_id}"]').first
                if await loc.count() > 0:
                    await loc.click(timeout=2500)
                    clicked = True
            except Exception:
                pass
            if not clicked:
                try:
                    loc = page.get_by_text(name, exact=False).first
                    if await loc.count() > 0:
                        await loc.click(timeout=2500)
                        clicked = True
                except Exception:
                    pass
            if not clicked:
                return None
            try:
                await page.wait_for_url(re.compile(r"/reader/|/bookDetail/"), timeout=4000)
            except Exception:
                pass
            await page.wait_for_timeout(1200)
            m = re.search(r"/reader/([^/?#]+)", page.url or "")
            if m:
                route_id = m.group(1)
                if route_id != str(api_book_id):
                    return route_id
        except Exception:
            pass
        return None

    async def _fallback_dom_extract(self, page, name: str) -> list:
        """DOM 抓取兜底:扫描 a 标签 + data-book-id,跳转后从 URL/INITIAL_STATE 二次兜底"""
        try:
            for _ in range(5):
                try:
                    await page.evaluate("window.scrollBy(0, 500)")
                except Exception:
                    break
                await asyncio.sleep(1)

            candidates = await page.evaluate("""() => {
                const out = [];
                for (const a of document.querySelectorAll('a[href]')) {
                    const h = a.href || '';
                    if (!h.includes('weread.qq.com')) continue;
                    if (!/reader|bookDetail|book\\//.test(h)) continue;
                    out.push({
                        href: h,
                        text: (a.textContent || '').trim().substring(0, 80),
                        book_id: a.getAttribute('data-book-id')
                            || (a.dataset && a.dataset.bookId)
                            || ''
                    });
                }
                return out;
            }""")

            # 优先 data-book-id 直接拿到 bookId 的(不用跳转)
            for c in candidates:
                if c.get("book_id"):
                    logger.info(f"Fallback: data-book-id 命中 {c['book_id']}")
                    return [{"book_id": str(c["book_id"]), "name": name, "author": ""}]

            if not candidates:
                return []

            # 用编辑距离排序,而不是粗暴的子串
            from difflib import SequenceMatcher
            target = name.lower()
            candidates.sort(
                key=lambda c: SequenceMatcher(None, target, (c["text"] or "").lower()).ratio(),
                reverse=True,
            )
            best = candidates[0]
            await page.goto(best["href"], timeout=30000, wait_until="domcontentloaded")
            await asyncio.sleep(3)
            final_url = page.url

            # 放宽容错:bookId 可能是纯数字、可能含特殊字符
            m = re.search(r"/reader/([^/?#]+)", final_url)
            if m:
                book_id = m.group(1)
                logger.info(f"Fallback: 从 URL 提取 bookId={book_id}")
                return [{"book_id": book_id, "name": name, "author": ""}]

            # 还不行,从跳转后页面的 __INITIAL_STATE__ 抢救
            bid = await page.evaluate("""() => {
                const st = window.__INITIAL_STATE__ || {};
                function walk(n, d) {
                    if (d > 8 || !n || typeof n !== 'object') return null;
                    for (const k of Object.keys(n)) {
                        const v = n[k];
                        if ((k === 'bookId' || k === 'book_id') && v) return v;
                        const r = walk(v, d + 1);
                        if (r) return r;
                    }
                    return null;
                }
                return walk(st, 0) || '';
            }""")
            if bid:
                logger.info(f"Fallback: 从跳转后 INITIAL_STATE 提取 bookId={bid}")
                return [{"book_id": str(bid), "name": name, "author": ""}]

            logger.warning(f"Fallback: DOM 也无法提取 bookId, 候选={candidates[:2]}")
            return []
        except Exception as e:
            logger.warning(f"Fallback DOM 提取失败: {e}")
            return []

    async def _dom_scan_route_candidates(self, page, scroll_times: int = 6) -> list:
        """扫描搜索结果/书架页 DOM,提取所有 /web/reader/<routeId> 候选。
        每个候选带:routeId(从 href 拿)、name(父容器 textContent,含书名+作者+介绍)、author(从 text 抽)。

        跟书架导入用的是同一种 DOM 元素,所以拿到的 book_id 格式一致(路由 ID,浏览器模式能用)。
        """
        try:
            # 滚几次让懒加载的卡片渲染出来
            for _ in range(scroll_times):
                try:
                    await page.evaluate("window.scrollBy(0, 500)")
                except Exception:
                    break
                await asyncio.sleep(0.6)

            candidates = await page.evaluate("""() => {
                const out = [];
                const seen = new Set();
                for (const a of document.querySelectorAll('a[href]')) {
                    const h = a.href || '';
                    const m = h.match(/\\/reader\\/([^/?#]+)/);
                    if (!m) continue;
                    const routeId = m[1];
                    if (seen.has(routeId)) continue;
                    seen.add(routeId);
                    // <a> 自己 textContent 通常空(React 懒加载子元素)
                    // 往上找 1-5 层父容器,取第一个 textContent 长度 >= 3 的
                    let parent = a;
                    let text = '';
                    for (let i = 0; i < 5; i++) {
                        parent = parent.parentElement || parent;
                        if (!parent) break;
                        const t = (parent.textContent || '').replace(/\\s+/g, ' ').trim();
                        if (t.length >= 3) { text = t; break; }
                    }
                    out.push({ routeId, text });
                }
                return out;
            }""")
            return candidates or []
        except Exception as e:
            logger.warning(f"DOM 扫描路由 ID 候选失败: {e}")
            return []

    def _match_route_for_book(self, dom_candidates: list, api_name: str, api_author: str = "") -> Optional[str]:
        """把 API 拿到的 (书名+作者) 跟 DOM 候选 text 配对,返回最接近的 routeId。"""
        if not dom_candidates or not api_name:
            return None
        from difflib import SequenceMatcher
        target = (api_name or "").lower()
        target_with_author = ((api_name or "") + (api_author or "")).lower()
        best_route = None
        best_score = 0.0
        for c in dom_candidates:
            ct = (c.get("text") or "").lower()
            if not ct:
                continue
            # 综合书名匹配 + 包含作者名
            s_name = SequenceMatcher(None, target, ct).ratio()
            s_full = SequenceMatcher(None, target_with_author, ct).ratio() if api_author else 0
            score = max(s_name, s_full)
            if score > best_score:
                best_score = score
                best_route = c.get("routeId")
        # 相似度阈值 0.3,太低算没匹配上
        if best_score >= 0.3:
            return best_route
        return None

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

    async def is_browser_logged_in(self) -> bool:
        """检查当前浏览器 context 是否已登录微信读书(有 wr_skey cookie)。"""
        try:
            if not self.context:
                return False
            cookies = await self.context.cookies()
            for c in cookies:
                if c.get("name") == "wr_skey" and c.get("value"):
                    return True
            return False
        except Exception:
            return False

    async def fetch_chapter_info(self, book_id: str) -> Optional[Dict[str, Any]]:
        """用 httpx 直接调 i.weread.qq.com/book/chapterInfos 拿章节列表。

        不用 Playwright fetch(避免 CORS 限制 / cookie 过期被丢弃)。
        返回:{"bookId": str, "chapters": [...], "first_chapter_uid": str}
        失败返回 None(网络/超时/未登录)。
        """
        # 优先从 disk 拿 wr_skey(避免 Playwright context 过期 cookie 被丢弃)
        wr_skey = ""
        wr_vid = ""
        try:
            from src.cookie_manager import cookie_manager
            cookies = cookie_manager.load()
            for c in cookies or []:
                if c.get("name") == "wr_skey":
                    wr_skey = c.get("value", "")
                if c.get("name") == "wr_vid":
                    wr_vid = c.get("value", "")
        except Exception:
            pass
        if not wr_skey or not wr_vid:
            try:
                if self.context:
                    ctx_cookies = await self.context.cookies()
                    for c in ctx_cookies:
                        if c.get("name") == "wr_skey":
                            wr_skey = c.get("value", "")
                        if c.get("name") == "wr_vid":
                            wr_vid = c.get("value", "")
            except Exception:
                pass

        if not wr_skey or not wr_vid:
            logger.warning("fetch_chapter_info: 缺少 wr_skey/wr_vid")
            return None

        try:
            import httpx
            url = f"https://i.weread.qq.com/book/chapterInfos?bookIds={book_id}&synckeys=0"
            headers = {
                "accept": "application/json, text/plain, */*",
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "referer": f"https://weread.qq.com/web/reader/{book_id}",
            }
            cookies_dict = {"wr_skey": wr_skey, "wr_vid": wr_vid}
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                resp = await client.get(url, headers=headers, cookies=cookies_dict)

            if resp.status_code != 200:
                logger.warning(f"fetch_chapter_info: {book_id} 状态码 {resp.status_code} body[:200]={resp.text[:200]}")
                return None
            try:
                data = resp.json()
            except Exception as e:
                logger.warning(f"fetch_chapter_info: 解析失败 {e}")
                return None

            items = None
            if isinstance(data, dict):
                if "data" in data and isinstance(data["data"], list):
                    items = data["data"]
                elif "results" in data and isinstance(data["results"], list):
                    items = data["results"]
                elif "chapters" in data and isinstance(data["chapters"], list):
                    items = [{"bookId": book_id, "chapters": data["chapters"]}]
            elif isinstance(data, list):
                items = data

            if not items:
                logger.warning(f"fetch_chapter_info: {book_id} 响应无 items")
                return None

            target = None
            for it in items:
                if not isinstance(it, dict):
                    continue
                bid = it.get("bookId") or it.get("book_id") or it.get("bookid")
                if str(bid) == str(book_id):
                    target = it
                    break
            if target is None:
                target = items[0]

            chapters = target.get("chapters") or []
            if not isinstance(chapters, list) or not chapters:
                logger.warning(f"fetch_chapter_info: {book_id} 没有 chapters 字段")
                return None

            norm = []
            for c in chapters:
                if not isinstance(c, dict):
                    continue
                uid = c.get("chapterUid") or c.get("chapter_uid") or c.get("uid") or c.get("id")
                if uid is None:
                    continue
                uid_str = str(uid)
                idx = c.get("chapterIdx") or c.get("chapter_idx") or c.get("idx") or c.get("order") or 0
                try:
                    idx_int = int(idx)
                except Exception:
                    idx_int = 0
                norm.append({
                    "chapterUid": uid_str,
                    "chapterIdx": idx_int,
                    "title": c.get("title") or c.get("chapterTitle") or "",
                })

            if not norm:
                logger.warning(f"fetch_chapter_info: {book_id} 规范化后无章节")
                return None

            norm.sort(key=lambda x: x["chapterIdx"])
            first = norm[0]
            logger.info(f"fetch_chapter_info: {book_id[:12]} 拿到 {len(norm)} 章,首章 c={first['chapterUid'][:16]}")
            return {
                "bookId": book_id,
                "chapters": norm,
                "first_chapter_uid": first["chapterUid"],
                "first_chapter_idx": first["chapterIdx"],
            }
        except Exception as e:
            logger.warning(f"fetch_chapter_info 异常: {e}")
            return None
    async def fetch_chapter_info_via_page(self, book_id: str, user_name: str = "default") -> Optional[Dict[str, Any]]:
        """【新方案】用 Playwright 真实打开阅读视图,通过多路收集章节信息。

        关键修复(基于真实运行日志 - 关键发现):
        - 真实接口路径:**/web/book/outline** 返回完整章节列表
          (用户日志显示 200 https://weread.qq.com/web/book/outline)
        - /web/book/chapter/e_N 是单章节内容接口(也含 chapterUid)
        - chapterInfos 根本不在 reader 视图被调用
        - 之前 page.on("response") 漏响应 → 改用 page.context.on("response")(context 级别更可靠)

        多路收集策略:
          ① goto 阅读视图(带 ?from=bookshelf_ba)
          ② context.on("response") 截获 outline(完整章节列表) + chapter/e_N(单章节)
          ③ 兜底:从 page DOM 读章节目录
        """
        try:
            page = await self.get_page()
        except Exception as e:
            logger.warning(f"fetch_chapter_info_via_page: 获取 page 失败 {e}")
            return None

        if not await self.is_browser_logged_in():
            logger.warning(
                f"fetch_chapter_info_via_page: 浏览器 context 无 wr_skey,"
                f"跳过 (book={book_id[:12]})。请在 Web UI 扫码登录后再触发"
            )
            return None

        captured_chapters: Dict[str, Dict[str, Any]] = {}
        captured_outline: List[Dict[str, Any]] = []
        all_seen_urls: list = []

        async def _on_response(resp):
            try:
                url = resp.url
                all_seen_urls.append((url[:150], resp.status))
                if resp.status != 200:
                    return
                # 关键:也截 /web/book/outline(它就是章节列表)
                if not any(pat in url for pat in (
                    "/web/book/chapter",
                    "/web/book/outline",
                    "/chapterInfo",
                    "chapterInfos",
                )):
                    return
                try:
                    body = await resp.json()
                except Exception as e:
                    # chapter/e_N 的内容接口是加密文本,非 JSON 是正常的
                    # 但响应 body 里可能包含 chapterUid 元数据(微信读书加密格式)
                    try:
                        raw_text = await resp.text()
                    except Exception:
                        raw_text = ""
                    logger.warning(
                        f"🔍 截到非 JSON 响应 {url[:80]} status={resp.status} "
                        f"text前150字符={repr(raw_text[:150])}"
                    )
                    # 尝试从文本里解析 chapterUid(微信读书加密格式通常包含元数据)
                    import re
                    m = re.search(r'"chapterUid"\s*:\s*"?(\w+)"?', raw_text)
                    if not m:
                        m = re.search(r'"chapterId"\s*:\s*"?(\w+)"?', raw_text)
                    if m:
                        uid_str = m.group(1)
                        if uid_str not in captured_chapters:
                            captured_chapters[uid_str] = {
                                "chapterUid": uid_str,
                                "chapterIdx": len(captured_chapters),
                                "title": "",  # 加密文本里没有标题
                            }
                            logger.info(f"📌 从加密响应提取 chapterUid={uid_str[:16]}")
                    return
                # 诊断:每次都打 body 类型 + keys(打 INFO 级别,不再静默)
                logger.info(
                    f"🔍 截到响应 {url[:80]} status={resp.status} "
                    f"type={type(body).__name__} "
                    f"keys={list(body.keys())[:15] if isinstance(body, dict) else 'N/A'}"
                )
                if not isinstance(body, dict):
                    return

                # === outline 路径 ===
                if "/web/book/outline" in url:
                    # 真实响应形如:{itemsArray: [...], bookId: ..., updated: [...], chapterCount: ...}
                    # itemsArray 里每个元素形如:{itemId, itemType, title, ...}
                    # itemType==1 是章节(还有目录/书签/笔记等其他类型)
                    # 关键:outline 的 itemId 是前端展示用的章节 ID(可能是纯数字 1,2,3...),
                    # **不是**腾讯 API 的真 chapterUid(24 字符 hex)。
                    # 真 chapterUid 通常嵌套在 chapterInfo 子对象里,或者由
                    # i.weread.qq.com/book/chapterInfos 单独接口返回。
                    # 这里只接受看起来像真 UID 的值(< 6 字符或纯数字视为展示用,丢弃)
                    items_raw = body.get("itemsArray") or body.get("chapters") or body.get("data") or body.get("results") or []
                    logger.info(f"outline items_raw 数量={len(items_raw) if isinstance(items_raw, list) else 'N/A'}")
                    if isinstance(items_raw, list):
                        idx_counter = 0
                        dropped_short_or_digit = 0
                        for c in items_raw:
                            if not isinstance(c, dict):
                                continue
                            # 只取 itemType==1 (普通章节)
                            item_type = c.get("itemType") if "itemType" in c else None
                            if item_type is not None and item_type != 1:
                                continue
                            # 优先级:嵌套真 UID > 顶层 chapterUid > itemId
                            _nested_info = c.get("chapterInfo") if isinstance(c.get("chapterInfo"), dict) else None
                            _nested_uid = (_nested_info or {}).get("chapterUid")
                            _top_uid = c.get("chapterUid") or c.get("chapterId")
                            _item_id = c.get("itemId")
                            _real_uid = None
                            for _candidate in (_nested_uid, _top_uid, _item_id):
                                if _candidate and isinstance(_candidate, str):
                                    if len(_candidate) >= 6 and not _candidate.isdigit():
                                        _real_uid = _candidate
                                        break
                            if _real_uid is None:
                                # 全部字段都是纯数字/< 6 字符 → 这是展示用,丢弃
                                dropped_short_or_digit += 1
                                idx_counter += 1
                                continue
                            uid_str = str(_real_uid)
                            # 真实字段是 level 不是 chapterIdx;level=0 是顶级章节,1是子章节
                            idx = (
                                c.get("chapterIdx")
                                or c.get("chapter_idx")
                                or c.get("idx")
                                or c.get("order")
                                or c.get("level")
                                or idx_counter
                            )
                            try:
                                idx_int = int(idx)
                            except Exception:
                                idx_int = idx_counter
                            title = (
                                c.get("title")
                                or c.get("chapterTitle")
                                or c.get("name")
                                or ""
                            )
                            captured_outline.append({
                                "chapterUid": uid_str,
                                "chapterIdx": idx_int,
                                "title": title,
                            })
                            idx_counter += 1
                        if dropped_short_or_digit:
                            logger.info(
                                f"outline 解析过滤掉 {dropped_short_or_digit} 个展示用 itemId "
                                f"(< 6 字符或纯数字),保留 {len(captured_outline)} 个真 chapterUid"
                            )
                        logger.info(f"outline 解析后拿到 {len(captured_outline)} 个 itemType==1 章节")
                    return

                # === chapter/e_N 单章节路径 ===
                uid = body.get("chapterUid") or body.get("chapter_uid") or body.get("uid") or body.get("chapterId") or body.get("chapter_id")
                if not uid:
                    return
                uid_str = str(uid)
                idx = body.get("chapterIdx") or body.get("chapter_idx") or body.get("idx") or body.get("order") or 0
                try:
                    idx_int = int(idx)
                except Exception:
                    idx_int = 0
                title = body.get("title") or body.get("chapterTitle") or body.get("name") or ""
                if uid_str not in captured_chapters:
                    captured_chapters[uid_str] = {
                        "chapterUid": uid_str,
                        "chapterIdx": idx_int,
                        "title": title,
                    }
                    logger.debug(f"截获 chapter 响应: uid={uid_str[:16]} idx={idx_int} title={title[:30]}")
            except Exception:
                pass

        # 关键:用 context.on 而不是 page.on(context 级别更可靠,不会漏)
        listener_target = self.context if self.context else page
        listener_target.on("response", _on_response)

        try:
            # ① goto ?from=bookshelf_ba 进阅读视图
            try:
                await page.goto(
                    f"https://weread.qq.com/web/reader/{book_id}?from=bookshelf_ba",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
            except Exception as e:
                logger.warning(f"fetch_chapter_info_via_page: goto 失败 {e},再试 load")
                try:
                    await page.goto(
                        f"https://weread.qq.com/web/reader/{book_id}?from=bookshelf_ba",
                        wait_until="load",
                        timeout=30000,
                    )
                except Exception as e2:
                    logger.warning(f"fetch_chapter_info_via_page: 二次 goto 也失败 {e2}")
                    return None

            # 等 10s 拿 outline + chapter 响应
            for _ in range(20):
                if captured_outline or len(captured_chapters) >= 2:
                    break
                await page.wait_for_timeout(500)

            # 如果还没拿到 outline,在 page 上下文里 fetch(用浏览器里的 cookie/referer,避免 404/401)
            if not captured_outline:
                logger.info("未截获 outline,在 page 里 fetch /web/book/outline")
                try:
                    # 用 page.evaluate 在浏览器内 fetch,带 SPA 该有的 referer/credentials
                    result = await page.evaluate(
                        """async (bookId) => {
                            const resp = await fetch(`/web/book/outline?bookId=${bookId}`, {
                                credentials: 'include',
                                headers: { 'accept': 'application/json, text/plain, */*' }
                            });
                            const text = await resp.text();
                            return { status: resp.status, body: text.slice(0, 5000) };
                        }""",
                        book_id,
                    )
                    status = result.get("status")
                    body_text = result.get("body", "")
                    logger.info(f"page 内 fetch outline status={status} body[:300]={body_text[:300]}")
                    if status == 200:
                        try:
                            import json as _json
                            body = _json.loads(body_text)
                            items_raw = body.get("itemsArray") or body.get("chapters") or []
                            if isinstance(items_raw, list):
                                idx_counter = 0
                                for c in items_raw:
                                    if not isinstance(c, dict):
                                        continue
                                    item_type = c.get("itemType")
                                    if item_type is not None and item_type != 1:
                                        continue
                                    # 关键:outline 的 itemId 是前端展示用的章节 ID(可能是纯数字 1,2,3...),
                                    # **不是**腾讯 API 的真 chapterUid(24 字符 hex)。
                                    # 真 chapterUid 通常嵌套在 chapterInfo 子对象里,或者由
                                    # i.weread.qq.com/book/chapterInfos 单独接口返回。
                                    # 这里只接受看起来像真 UID 的值(< 6 字符或纯数字视为展示用,丢弃)
                                    _item_id = c.get("itemId")
                                    _chapter_uid_nested = c.get("chapterUid") or c.get("chapterId")
                                    _nested_info = c.get("chapterInfo") if isinstance(c.get("chapterInfo"), dict) else None
                                    _nested_uid = (_nested_info or {}).get("chapterUid")

                                    _real_uid = None
                                    # 优先级:嵌套真 UID > 顶层 chapterUid (非纯数字) > itemId (非纯数字)
                                    for _candidate in (_nested_uid, _chapter_uid_nested, _item_id):
                                        if _candidate and isinstance(_candidate, str):
                                            if len(_candidate) >= 6 and not _candidate.isdigit():
                                                _real_uid = _candidate
                                                break
                                    if _real_uid is None:
                                        # 全部字段都是纯数字/< 6 字符 → 这是展示用,丢弃
                                        idx_counter += 1
                                        continue
                                    uid = _real_uid
                                    idx = c.get("chapterIdx") or c.get("level") or idx_counter
                                    try:
                                        idx_int = int(idx)
                                    except Exception:
                                        idx_int = idx_counter
                                    title = c.get("title") or c.get("chapterTitle") or ""
                                    captured_outline.append({
                                        "chapterUid": str(uid),
                                        "chapterIdx": idx_int,
                                        "title": title,
                                    })
                                    idx_counter += 1
                                logger.info(f"page 内 fetch outline 拿到 {len(captured_outline)} 章 (原始 itemsRaw {len(items_raw)} 项,过滤掉纯数字展示用字段)")
                        except Exception as e:
                            logger.warning(f"page 内 fetch outline 解析失败: {e}")
                    else:
                        logger.warning(f"page 内 fetch outline 状态码 {status}")
                except Exception as e:
                    logger.warning(f"page 内 fetch outline 异常: {e}")

            # 路 D: 在 page 里调 i.weread.qq.com/book/chapterInfos(浏览器内 fetch 带正确 cookie scope,绕过 401)
            if not captured_outline:
                logger.info("page 内 fetch i.weread.qq.com/book/chapterInfos")
                try:
                    result2 = await page.evaluate(
                        """async (bookId) => {
                            const url = `https://i.weread.qq.com/book/chapterInfos?bookIds=${bookId}&synckeys=0`;
                            const resp = await fetch(url, {
                                credentials: 'include',
                                headers: {
                                    'accept': 'application/json, text/plain, */*',
                                    'referer': `https://weread.qq.com/web/reader/${bookId}`
                                }
                            });
                            const text = await resp.text();
                            return { status: resp.status, body: text.slice(0, 8000) };
                        }""",
                        book_id,
                    )
                    status2 = result2.get("status")
                    body_text2 = result2.get("body", "")
                    logger.info(f"i.weread chapterInfos status={status2} body[:300]={body_text2[:300]}")
                    if status2 == 200:
                        try:
                            import json as _json
                            body2 = _json.loads(body_text2)
                            # 真实响应:{"data":[{"bookId":"...","chapters":[...]}]}
                            items_raw2 = (
                                (body2.get("data") or [{}])[0].get("chapters")
                                if isinstance(body2.get("data"), list) and body2["data"]
                                else None
                            ) or body2.get("chapters") or body2.get("itemsArray") or []
                            if isinstance(items_raw2, list) and items_raw2:
                                idx_counter = 0
                                for c in items_raw2:
                                    if not isinstance(c, dict):
                                        continue
                                    uid = (
                                        c.get("chapterUid")
                                        or c.get("chapter_uid")
                                        or c.get("uid")
                                        or c.get("chapterId")
                                        or c.get("itemId")
                                    )
                                    if uid is None:
                                        continue
                                    try:
                                        idx_int = int(c.get("chapterIdx") or c.get("level") or idx_counter)
                                    except Exception:
                                        idx_int = idx_counter
                                    title = c.get("title") or c.get("chapterTitle") or ""
                                    captured_outline.append({
                                        "chapterUid": str(uid),
                                        "chapterIdx": idx_int,
                                        "title": title,
                                    })
                                    idx_counter += 1
                                logger.info(f"i.weread chapterInfos 拿到 {len(captured_outline)} 章")
                        except Exception as e:
                            logger.warning(f"i.weread chapterInfos 解析失败: {e}")
                    else:
                        logger.warning(f"i.weread chapterInfos 状态码 {status2}")
                except Exception as e:
                    logger.warning(f"i.weread chapterInfos 异常: {e}")
        finally:
            try:
                listener_target.remove_listener("response", _on_response)
            except Exception:
                pass

        # 兜底:从 page DOM 读章节列表
        dom_chapters: List[Dict[str, Any]] = []
        try:
            dom_selectors = [
                "[data-chapter-uid]",
                "[data-uid]",
                "[data-chapterid]",
                ".catalogChapterItem",
                ".chapterItem",
                "[class*='chapterItem']",
                "[class*='ChapterItem']",
                ".readerChapterList li",
                "[class*='chapter']",
                "[class*='Chapter']",
            ]
            for sel in dom_selectors:
                try:
                    items = await page.locator(sel).all()
                    for item in items:
                        try:
                            uid = (
                                await item.get_attribute("data-chapter-uid")
                                or await item.get_attribute("data-uid")
                                or await item.get_attribute("data-chapterid")
                                or await item.get_attribute("data-id")
                            )
                            if not uid:
                                continue
                            title = await item.text_content() or ""
                            title = title.strip()[:80]
                            dom_chapters.append({
                                "chapterUid": str(uid),
                                "chapterIdx": len(dom_chapters),
                                "title": title,
                            })
                        except Exception:
                            continue
                    if dom_chapters:
                        logger.info(f"DOM 拿章节: {sel} → {len(dom_chapters)} 章")
                        break
                except Exception:
                    continue
            # 终极兜底:在 page 里抓 window 上的 React 全局状态/初始 state
            if not dom_chapters and not captured_outline:
                try:
                    result = await page.evaluate(
                        """() => {
                            // 微信读书 SPA 把整本书数据挂 window 上,常见位置:
                            // window.__INITIAL_STATE__, window.ebook, window.__NEXT_DATA__,
                            // 或者 redux/mobx 的全局 store
                            const candidates = ['__INITIAL_STATE__', '__NEXT_DATA__', '__NUXT__', 'ebook', '__INITIAL_DATA__'];
                            const found = {};
                            for (const k of candidates) {
                                if (typeof window[k] === 'object' && window[k] !== null) {
                                    found[k] = Object.keys(window[k]).slice(0, 30);
                                }
                            }
                            // 收集 window 上所有非 React/非 console 的对象 key
                            const allKeys = Object.keys(window).filter(k =>
                                !k.startsWith('chrome') && !k.startsWith('webkit') &&
                                !k.startsWith('on') && k.length < 40 &&
                                typeof window[k] === 'object' && window[k] !== null
                            ).slice(0, 50);
                            return { found, allKeys, url: location.href, title: document.title };
                        }"""
                    )
                    logger.info(
                        f"page window 探索: candidates={result.get('found')}, "
                        f"allKeys(前30)={result.get('allKeys', [])[:30]}"
                    )
                except Exception as e:
                    logger.warning(f"page window 探索失败: {e}")
        except Exception as e:
            logger.debug(f"DOM 读章节异常(非致命): {e}")

        # 合并:outline 优先(完整列表) > chapter/e_N > DOM
        merged: Dict[str, Dict[str, Any]] = {}
        for c in captured_outline:
            merged[c["chapterUid"]] = c
        for uid, c in captured_chapters.items():
            if uid not in merged:
                merged[uid] = c
            else:
                if c.get("chapterIdx") and not merged[uid].get("chapterIdx"):
                    merged[uid] = c
        for c in dom_chapters:
            if c["chapterUid"] not in merged:
                merged[c["chapterUid"]] = c

        if not merged:
            seen_api = [
                (u, s) for (u, s) in all_seen_urls
                if any(api in u for api in ("chapter", "/book/"))
            ]
            current_url = ""
            try:
                current_url = page.url
            except Exception:
                pass
            page_title = ""
            try:
                page_title = await page.title()
            except Exception:
                pass
            logger.warning(
                f"fetch_chapter_info_via_page: {book_id[:12]} 全部 4 路都未拿到章节\n"
                f"  当前 page URL: {current_url}\n"
                f"  page title: {page_title}\n"
                f"  截获的 outline 数量: {len(captured_outline)}\n"
                f"  截获的 chapter 数量: {len(captured_chapters)}\n"
                f"  DOM 读出的章节数量: {len(dom_chapters)}\n"
                f"  期间走过的 chapter/book API URL (前 10 个):\n"
                + "\n".join(f"    {s} {u}" for (u, s) in seen_api[:10])
            )
            return None

        norm = sorted(merged.values(), key=lambda x: x.get("chapterIdx", 0))
        first = norm[0]
        logger.info(
            f"🌐 Playwright 抓章节成功: {book_id[:12]} 拿到 {len(norm)} 章"
            f"(outline {len(captured_outline)} + chapter {len(captured_chapters)} + DOM {len(dom_chapters)}),"
            f"首章 c={first['chapterUid'][:16]} idx={first.get('chapterIdx', 0)}"
        )
        return {
            "bookId": book_id,
            "chapters": norm,
            "first_chapter_uid": first["chapterUid"],
            "first_chapter_idx": first.get("chapterIdx", 0),
        }

        # 解析最后一次响应(章节列表通常在最后一次请求里)
        data = captured[-1]["data"]
        items = None
        if isinstance(data, dict):
            if "data" in data and isinstance(data["data"], list):
                items = data["data"]
            elif "results" in data and isinstance(data["results"], list):
                items = data["results"]
            elif "chapters" in data and isinstance(data["chapters"], list):
                items = [{"bookId": book_id, "chapters": data["chapters"]}]
        elif isinstance(data, list):
            items = data

        if not items:
            logger.warning(f"fetch_chapter_info_via_page: {book_id[:12]} 响应无 items")
            return None

        target = None
        for it in items:
            if not isinstance(it, dict):
                continue
            bid = it.get("bookId") or it.get("book_id") or it.get("bookid")
            if str(bid) == str(book_id):
                target = it
                break
        if target is None:
            target = items[0]

        chapters = target.get("chapters") or []
        if not isinstance(chapters, list) or not chapters:
            logger.warning(f"fetch_chapter_info_via_page: {book_id[:12]} 没有 chapters 字段")
            return None

        norm = []
        for c in chapters:
            if not isinstance(c, dict):
                continue
            uid = c.get("chapterUid") or c.get("chapter_uid") or c.get("uid") or c.get("id")
            if uid is None:
                continue
            uid_str = str(uid)
            idx = c.get("chapterIdx") or c.get("chapter_idx") or c.get("idx") or c.get("order") or 0
            try:
                idx_int = int(idx)
            except Exception:
                idx_int = 0
            norm.append({
                "chapterUid": uid_str,
                "chapterIdx": idx_int,
                "title": c.get("title") or c.get("chapterTitle") or "",
            })

        if not norm:
            logger.warning(f"fetch_chapter_info_via_page: {book_id[:12]} 规范化后无章节")
            return None

        norm.sort(key=lambda x: x["chapterIdx"])
        first = norm[0]
        logger.info(
            f"🌐 Playwright 抓章节成功: {book_id[:12]} 拿到 {len(norm)} 章,首章 c={first['chapterUid'][:16]}"
        )
        return {
            "bookId": book_id,
            "chapters": norm,
            "first_chapter_uid": first["chapterUid"],
            "first_chapter_idx": first["chapterIdx"],
        }

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
