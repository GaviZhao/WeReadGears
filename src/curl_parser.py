import re
import json
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, List
from urllib.parse import quote


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
    """解析curl命令文本，返回结构化数据"""
    result = CurlResult()

    text = curl_text.replace("\\\r\n", "\\\n")
    lines = [line.rstrip() for line in text.split("\n") if line.strip()]
    current = ""
    for line in lines:
        if line.endswith("\\"):
            current += line[:-1] + " "
        else:
            current += line
            result.process_line(current)
            current = ""
    if current.strip():
        result.process_line(current.strip())

    if result.headers.get("cookie"):
        for part in result.headers["cookie"].split(";"):
            part = part.strip()
            if "=" in part:
                kv = part.split("=", 1)
                result.cookies[kv[0].strip()] = kv[1].strip()
    return {"url": result.url, "headers": result.headers, "cookies": result.cookies, "payload": result.payload}


class CurlResult:
    def __init__(self):
        self.url = ""
        self.headers = {}
        self.cookies = {}
        self.payload = {}

    def process_line(self, line):
        line = line.strip()
        if not line:
            return
        if line.startswith("curl "):
            m = re.search(r"curl\s+'([^']+)'", line)
            if m:
                self.url = m.group(1)
        if "-H " in line:
            for part in line.split("-H "):
                part = part.strip()
                if not part:
                    continue
                m = re.search(r"""['\"]([^'\"]+):\s*([^'\"]+)['\"]""", part)
                if m:
                    self.headers[m.group(1).strip()] = m.group(2).strip()
        if "--data-raw " in line or line.startswith("-d ") or line.startswith("-d'"):
            m = re.search(r"""--data-raw\s+'([^']*)'""", line)
            if not m:
                m = re.search(r'--data-raw\s+"([^"]*)"', line)
            if not m:
                m = re.search(r"""-d\s+'([^']*)'""", line)
            if m:
                raw = m.group(1)
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        self.payload.update(parsed)
                except json.JSONDecodeError:
                    self.payload["_raw"] = raw


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
