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
    url = ""
    for line in lines:
        line = line.strip()
        if line.startswith("curl "):
            m = re.search(r"curl\s+['\"]([^'\"]+)['\"]", line)
            if m:
                url = m.group(1)
                break
            m = re.search(r"curl\s+(\S+)", line)
            if m:
                url = m.group(1).strip("'\"")
                break
    return url


def _extract_headers(lines: List[str]) -> Dict[str, str]:
    headers = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if "-H " in line or "--header " in line:
            parts = re.split(r"(?:-H\s+|--header\s+)", line)
            for part in parts:
                part = part.strip()
                if not part:
                    continue
                header_val = _parse_single_header(part)
                if header_val:
                    key, val = header_val
                    if key.lower() not in ("cookie", "host", "content-length", "connection", "https") and ":" not in key:
                        headers[key] = val
        if line.startswith("'"):
            header_val = _parse_single_header(line)
            if header_val:
                key, val = header_val
                if key.lower() not in ("cookie", "host", "content-length", "connection", "https") and ":" not in key:
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


def _extract_cookies(headers: Dict[str, str]) -> Dict[str, str]:
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
        if "--data-raw " in line or "-d " in line or line.startswith("-d"):
            data_match = re.search(r"--data-raw\s+['\"](.*?)['\"]", line, re.DOTALL)
            if not data_match:
                data_match = re.search(r"-d\s+['\"](.*?)['\"]", line, re.DOTALL)
            if not data_match:
                data_match = re.search(r"'([^']*?\\?'?)$", line)
            if data_match:
                candidate = data_match.group(1).strip()
                candidate = candidate.replace("\\'", "'").replace('\\"', '"')
                if len(candidate) > len(raw_data):
                    raw_data = candidate

    if not raw_data:
        for line in lines:
            if "{" in line and "}" in line:
                m = re.search(r"\{.*\}", line, re.DOTALL)
                if m:
                    raw_data = m.group(0)
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
            else:
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
