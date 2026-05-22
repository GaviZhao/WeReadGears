import hashlib
import random
import time
import urllib.parse
from typing import Optional


KEY = "3c5c8717f3daf09iop3423zafeqoi"


class SignatureCalculator:
    """签名计算器"""

    def __init__(self, sign_key: Optional[str] = None):
        self.sign_key = sign_key or KEY

    def set_sign_key(self, key: str):
        self.sign_key = key

    def calculate_signature(self, ts: int, rn: int) -> str:
        signature_string = f"{ts}{rn}{self.sign_key}"
        return hashlib.sha256(signature_string.encode()).hexdigest()

    @staticmethod
    def calculate_hash(input_string: str) -> str:
        """自定义哈希算法 - 与 weread-bot 完全一致"""
        _7032f5 = 0x15051505
        _cc1055 = _7032f5
        length = len(input_string)
        _19094e = length - 1

        while _19094e > 0:
            char_code = ord(input_string[_19094e])
            shift_amount = (length - _19094e) % 30
            _7032f5 = 0x7fffffff & (
                _7032f5 ^ char_code << shift_amount
            )

            prev_char_code = ord(input_string[_19094e - 1])
            prev_shift_amount = _19094e % 30
            _cc1055 = 0x7fffffff & (
                _cc1055 ^ prev_char_code << prev_shift_amount
            )
            _19094e -= 2

        return hex(_7032f5 + _cc1055)[2:].lower()

    @staticmethod
    def encode_data(data: dict) -> str:
        encoded_pairs = [
            f"{k}={urllib.parse.quote(str(data[k]), safe='')}"
            for k in sorted(data.keys())
        ]
        return "&".join(encoded_pairs)

    def build_read_payload(
        self,
        base_data: dict,
        last_time: int = 0,
        book_id: str = "",
        chapter_id: str = "",
        chapter_index: Optional[int] = None,
    ) -> dict:
        """构建完整阅读请求 payload"""
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
        payload["sg"] = self.calculate_signature(ts, rn)

        encoded = self.encode_data(payload)
        payload["s"] = self.calculate_hash(encoded)

        return payload


signature_calculator = SignatureCalculator()
