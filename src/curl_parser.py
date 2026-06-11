import re
import json
import logging
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, List
from urllib.parse import quote

logger = logging.getLogger(__name__)

CRITICAL_FIELDS = ["appId", "ps", "pc", "wr_skey", "wr_vid"]


def parse_curl_file(file_path: str = "shared/curl_command.txt") -> Optional[Dict[str, Any]]:
    """解析curl_command.txt文件，提取headers、cookies和payload"""
    path = Path(file_path)
    if not path.exists():
        return None
    curl_text = path.read_text(encoding="utf-8").strip()
    if not curl_text:
        return None
    return parse_curl_text(curl_text)


def parse_curl_text(curl_text: str) -> Dict[str, Any]:
    """解析curl命令文本，返回结构化数据（兼容旧接口）"""
    result = robust_parse(curl_text)
    return {
        "url": result.get("url", ""),
        "headers": result.get("headers", {}),
        "cookies": result.get("cookies", {}),
        "payload": result.get("payload", {}),
    }


def robust_parse(curl_text: str) -> Dict[str, Any]:
    """健壮的多策略解析curl命令文本"""
    result = {
        "url": "",
        "headers": {},
        "cookies": {},
        "payload": {},
        "_raw_curl": curl_text,
        "_parse_errors": [],
    }

    if not curl_text or not curl_text.strip():
        result["_parse_errors"].append("curl_text is empty")
        return result

    text = curl_text.replace("\\\r\n", "\\\n").replace("\\\n", " ")
    lines = [line.rstrip() for line in text.split("\n") if line.strip()]

    url = _extract_url(lines)
    result["url"] = url

    headers = _extract_headers(lines)
    result["headers"] = headers

    cookies = _extract_cookies(headers)
    result["cookies"] = cookies

    payload = _extract_payload(lines)
    result["payload"] = payload

    return result


def _extract_url(lines: List[str]) -> str:
    """提取 curl 命令里的 URL,过滤掉明显的非 API 路径。

    微信读书有几条 URL 容易被误抓:
    - /web/book/read            ✓ 这才是真正的"阅读心跳"API(计费时长)
    - /web/book/chapter/e_0     ✗ 章节详情页初始化(不计时)
    - /web/reader/{bookId}      ✗ 阅读器页面(GET,不计时)

    如果抓到的是非 /web/book/read,返回空字符串让上层提示用户重新抓。
    """
    url = ""
    for line in lines:
        line = line.strip()
        if not line.startswith("curl "):
            continue
        m = re.search(r"curl\s+['\"]([^'\"]+)['\"]", line)
        if m:
            url = m.group(1)
            break
        m = re.search(r"curl\s+(\S+)", line)
        if m:
            url = m.group(1).strip("'\"")
            break

    # 过滤掉明显的非 /web/book/read 请求
    if url:
        u_lower = url.lower()
        if "/web/book/read" not in u_lower:
            # 不是阅读心跳 API,记下来供上层判断
            logger.warning(f"提取到非 /web/book/read URL: {url} (这不会计入阅读时长)")
            # 仍然返回,让上层根据 URL 决定是否重抓
    return url


def _extract_headers(lines: List[str]) -> Dict[str, str]:
    """从所有行里提取 header。

    兼容两类 curl 格式:
      1) 标准多行格式:每行 `  -H 'xxx: yyy' \\`
      2) 单行连续格式(DevTools 偶尔会输出无 \\ 续行的):整条命令挤在 line[0]

    先按 -H 拆分,再逐个解析。注意:cookie / host / content-length / connection
    也在字典里保留 —— _extract_cookies 会从这里取 cookie,而不是再扫一遍原文。
    """
    headers: Dict[str, str] = {}

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # 先按 -H / --header 拆分整行(支持一个行多个 -H,例如单行连续格式)
        if "-H " in line or "--header " in line:
            parts = re.split(r"(?:-H\s+|--header\s+)", line)
            for idx, part in enumerate(parts):
                part = part.strip()
                if not part or idx == 0:
                    continue
                # 每段可能本身是 "key: val" 形式,也可能段首还带引号
                header_val = _parse_single_header(part)
                if header_val:
                    key, val = header_val
                    if _is_valid_header_key(key):
                        # host/content-length/connection 不参与业务请求,丢掉
                        # 但 cookie 必须保留,供 _extract_cookies 解析
                        if key.lower() in ("host", "content-length", "connection"):
                            continue
                        headers[key] = val

        # 单引号/双引号开头的行(整段就是一个 header,常见于多行格式)
        if line.startswith("'") or line.startswith('"'):
            header_val = _parse_single_header(line)
            if header_val:
                key, val = header_val
                if _is_valid_header_key(key):
                    if key.lower() in ("host", "content-length", "connection"):
                        continue
                    headers[key] = val

    return headers


def _parse_single_header(part: str) -> Optional[Tuple[str, str]]:
    part = part.strip().strip("'\"")
    if not part:
        return None
    colon_pos = -1
    for i, c in enumerate(part):
        if c == ":":
            colon_pos = i
            break
    if colon_pos > 0:
        key = part[:colon_pos].strip()
        val = part[colon_pos + 1:].strip()
        if key and val:
            return (key, val)
    return None


def _is_valid_header_key(key: str) -> bool:
    if not key:
        return False
    if " " in key:
        return False
    if "//" in key:
        return False
    if "{" in key or "}" in key:
        return False
    if "--" in key:
        return False
    return True


def _extract_cookies(headers: Dict[str, str]) -> Dict[str, str]:
    """从 headers 里提取 cookies。

    兼容:
      - 'Cookie: foo=bar; baz=qux' 这种标准 header
      - 有时 cookie 行被截到 --data-raw 残片(已由 _sanitize_header_value 清洗过)
      - 还会从每行原文里尝试直接抓 "cookie: ..." 段,避免漏掉 (headers 已清洗掉的情况)
    """
    cookies = {}
    cookie_header = headers.get("cookie", "")
    if not cookie_header:
        for key, val in headers.items():
            if key.lower() == "cookie":
                cookie_header = val
                break
    if cookie_header:
        for part in cookie_header.split(";"):
            part = part.strip()
            if "=" in part:
                idx = part.index("=")
                k = part[:idx].strip()
                v = part[idx + 1:].strip()
                if k:
                    cookies[k] = v
    return cookies


def _extract_payload(lines: List[str]) -> Dict[str, Any]:
        payload = {}
        raw_data = ""

        for line in lines:
            line = line.strip()
            if not line:
                continue
            if "--data-raw " in line or " -d " in line or "--data " in line:
                # Find substring after --data-raw / -d / --data
                idx = -1
                for marker in ["--data-raw ", "--data ", "-d "]:
                    j = line.find(marker)
                    if j >= 0 and (idx < 0 or j < idx):
                        idx = j + len(marker)
                if idx < 0:
                  continue
                rest = line[idx:].strip()
                # Skip leading quote char
                if not rest:
                  continue
                quote = rest[0]
                if quote not in ("'", ""):
                  # No leading quote, treat rest as raw JSON up to first whitespace or end
                  end = len(rest)
                  for k in range(len(rest)):
                    if rest[k] in (" ", "\t"):
                      end = k
                      break
                  candidate = rest[:end]
                else:
                  # Find matching closing quote, respecting backslash escapes
                  end = -1
                  k = 1
                  while k < len(rest):
                    if rest[k] == "\\":
                      k += 2  # skip escaped char
                      continue
                    if rest[k] == quote:
                      end = k
                      break
                    k += 1
                  if end < 0:
                    continue
                  candidate = rest[1:end]
                # Unescape common sequences
                # 关键:这里只处理"原始 curl 里就把转义字面写出来了"的情况
                # (例如有些工具导出的 curl 里会带 \n、\u4e2d\u6587 这种字面转义)。
                # 如果 candidate 已经是合法 UTF-8 中文,这一步会把它破坏掉:
                #   encode("utf-8") 把"英"变成 b"\xe5\x9b\xbd"
                #   decode("unicode_escape") 把 \xe5 当 Latin-1 字符解读 → 乱码
                # 因此:**先尝试 json.loads 一次**,成功就直接用,跳过 unicode_escape
                try:
                    _test_parsed = json.loads(candidate)
                    # json.loads 自身就处理 \uXXXX / \n / \t 等转义,无需额外 unescape
                except Exception:
                    # 真解析不动时,才尝试 unicode_escape 兜底(很可能是字面转义)
                    try:
                        candidate = candidate.encode("utf-8").decode("unicode_escape")
                    except Exception:
                        pass
                candidate = candidate.strip()
                if len(candidate) > len(raw_data):
                  raw_data = candidate

        if not raw_data:
            for line in lines:
                if "{" in line and "}" in line:
                    start_b = line.find("{")
                    end_b = line.rfind("}")
                    if start_b >= 0 and end_b > start_b:
                        raw_data = line[start_b:end_b + 1]
                    break

        if raw_data:
            try:
                parsed = json.loads(raw_data)
                if isinstance(parsed, dict):
                    payload = parsed
                elif isinstance(parsed, list) and parsed:
                    if isinstance(parsed[0], dict):
                        payload = parsed[0]
            except json.JSONDecodeError:
                clean = _try_fix_json(raw_data)
                if clean:
                    try:
                        payload = json.loads(clean)
                    except:
                        payload = {"_raw": raw_data}

        return payload

def _try_fix_json(raw: str) -> Optional[str]:
    fixes = [
        raw.replace("\\", "\\\\"),
        raw.replace("'", "\\'"),
        raw.replace('\\"', '"'),
        raw.replace('""', '"'),
        raw.strip(),
    ]
    for f in fixes:
        try:
            json.loads(f)
            return f
        except:
            continue
    return None


def validate_parsed_data(parsed: Dict[str, Any], strict: bool = True) -> Tuple[bool, List[str], List[str]]:
    """校验解析结果，返回 (是否有效, 缺失字段列表, 错误列表)"""
    is_valid = True
    missing = []
    errors = []

    payload = parsed.get("payload", {})
    cookies = parsed.get("cookies", {})
    headers = parsed.get("headers", {})

    if strict:
        required = {
            "appId": payload.get("appId"),
            "ps": payload.get("ps"),
            "pc": payload.get("pc"),
            "wr_skey": cookies.get("wr_skey") or headers.get("wr_skey"),
            "wr_vid": cookies.get("wr_vid") or headers.get("wr_vid"),
        }
        for field, value in required.items():
            if not value or str(value).strip() == "":
                missing.append(field)
                is_valid = False

        if missing:
            errors.append(f"关键字段缺失: {', '.join(missing)}")
    else:
        if not cookies.get("wr_skey") and not headers.get("wr_skey"):
            missing.append("wr_skey")
            is_valid = False
        if not cookies.get("wr_vid") and not headers.get("wr_vid"):
            missing.append("wr_vid")
            is_valid = False

    if not parsed.get("url"):
        errors.append("URL为空")
        is_valid = False

    return is_valid, missing, errors


def fix_missing_fields(parsed: Dict[str, Any], raw_captured: Dict[str, Any]) -> Dict[str, Any]:
    """尝试从原始捕获数据补全解析结果中缺失的字段"""
    fixed = dict(parsed)
    payload = dict(fixed.get("payload", {}))
    cookies = dict(fixed.get("cookies", {}))
    headers = dict(fixed.get("headers", {}))

    raw_payload = raw_captured.get("full_payload", {})
    raw_cookies_list = raw_captured.get("_raw_cookies", [])
    raw_headers = raw_captured.get("headers", {})

    if raw_payload:
        for key in ["appId", "ps", "pc", "b", "c", "ci", "co", "sm", "pr"]:
            if key not in payload or not str(payload.get(key, "")).strip():
                val = raw_payload.get(key)
                if val is not None and str(val).strip():
                    payload[key] = val
                    logger.info(f"从原始数据补全payload字段: {key}")

    if raw_cookies_list:
        for c in raw_cookies_list:
            name = c.get("name", "")
            value = c.get("value", "")
            if name and value:
                cookies[name] = value

    if raw_headers:
        for key in ["user-agent", "content-type", "accept", "accept-language", "origin", "referer"]:
            val = raw_headers.get(key) or raw_headers.get(key.title())
            if val and key not in headers:
                headers[key.title() if key.lower() == "user-agent" else key] = val

    for c in raw_cookies_list:
        name = c.get("name", "")
        value = c.get("value", "")
        if name == "wr_skey" and not cookies.get("wr_skey"):
            cookies["wr_skey"] = value
            logger.info("从原始cookies补全wr_skey")
        if name == "wr_vid" and not cookies.get("wr_vid"):
            cookies["wr_vid"] = value
            logger.info("从原始cookies补全wr_vid")

    fixed["payload"] = payload
    fixed["cookies"] = cookies
    fixed["headers"] = headers
    fixed["_fixed"] = True

    return fixed


def parse_curl_with_fallback(curl_text: str, raw_captured: Dict[str, Any] = None, strict: bool = True) -> Tuple[Dict[str, Any], bool, List[str], List[str]]:
    """带后备和修复的解析入口
    
    Returns: (parsed_data, was_valid_before_fix, missing_fields, errors)
    """
    parsed = robust_parse(curl_text)
    is_valid, missing, errors = validate_parsed_data(parsed, strict=strict)
    
    if is_valid:
        return parsed, True, [], []
    
    logger.warning(f"解析验证失败，缺失字段: {missing}，尝试修复...")
    
    if raw_captured:
        parsed = fix_missing_fields(parsed, raw_captured)
        is_valid, missing, errors = validate_parsed_data(parsed, strict=strict)
        if is_valid:
            logger.info("通过原始数据修复成功")
            return parsed, False, [], []
    
    return parsed, is_valid, missing, errors


def build_read_payload(base_data: dict, last_time: int = 0, sign_key: str = "",
                       book_id: str = "", chapter_id: str = "",
                       chapter_index=None) -> dict:
    """构建完整阅读请求 payload"""
    import time, random, hashlib
    payload = base_data.copy()
    payload.pop("s", None)
    if book_id:
        payload["b"] = book_id
    if chapter_id:
        payload["c"] = chapter_id
    if chapter_index is not None:
        payload["ci"] = chapter_index
    current_time = int(time.time())
    payload["ct"] = current_time
    payload["rt"] = current_time - last_time if last_time else 0
    ts = int(current_time * 1000) + random.randint(0, 1000)
    rn = random.randint(0, 1000)
    payload["ts"] = ts
    payload["rn"] = rn
    sig_str = f"{ts}{rn}{sign_key}"
    payload["sg"] = hashlib.sha256(sig_str.encode()).hexdigest()
    encoded = encode_data(payload)
    payload["s"] = calculate_hash(encoded)
    return payload


def encode_data(data: dict) -> str:
    """URL编码并按key排序"""
    pairs = [f"{k}={quote(str(data[k]), safe='')}" for k in sorted(data.keys())]
    return "&".join(pairs)


def calculate_hash(input_string: str) -> str:
    """自定义哈希算法 - 与 weread-bot 完全一致"""
    _7032f5 = 0x15051505
    _cc1055 = _7032f5
    length = len(input_string)
    _19094e = length - 1
    while _19094e > 0:
        char_code = ord(input_string[_19094e])
        shift_amount = (length - _19094e) % 30
        _7032f5 = 0x7fffffff & (_7032f5 ^ char_code << shift_amount)
        prev_char_code = ord(input_string[_19094e - 1])
        prev_shift_amount = _19094e % 30
        _cc1055 = 0x7fffffff & (_cc1055 ^ prev_char_code << prev_shift_amount)
        _19094e -= 2
    return hex(_7032f5 + _cc1055)[2:].lower()
