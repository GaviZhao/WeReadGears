import logging
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler
from datetime import datetime


class Logger:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._logger = None

    def setup(self, name: str, log_file: str = None, level: str = "INFO"):
        self._logger = logging.getLogger(name)
        self._logger.setLevel(getattr(logging, level.upper(), logging.INFO))

        formatter = logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )

        # 始终确保 console handler 存在(可能 logger 已被别的模块提前 setup 过)
        has_console = any(
            isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
            and getattr(h, "stream", None) is sys.stdout
            for h in self._logger.handlers
        )
        if not has_console:
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setFormatter(formatter)
            self._logger.addHandler(console_handler)

        # 始终确保 file handler 存在(关键修复:即使 logger 已初始化,后续 setup 也要加 file handler)
        if log_file:
            # 转绝对路径:避免容器 cwd 不一致导致日志写错地方
            log_path = Path(log_file).resolve()
            log_path.parent.mkdir(parents=True, exist_ok=True)
            abs_log_file = str(log_path)
            has_file = any(
                isinstance(h, RotatingFileHandler)
                and getattr(h, "baseFilename", "") == abs_log_file
                for h in self._logger.handlers
            )
            if not has_file:
                file_handler = RotatingFileHandler(
                    str(log_path),
                    maxBytes=10 * 1024 * 1024,
                    backupCount=5,
                    encoding="utf-8"
                )
                file_handler.setFormatter(formatter)
                self._logger.addHandler(file_handler)
                # 让所有早期 logger.info() 也写到文件:通知 root logger 一次
                _root = logging.getLogger()
                if not any(getattr(h, "baseFilename", "") == abs_log_file for h in _root.handlers):
                    _root.addHandler(file_handler)

        return self._logger

    @property
    def logger(self):
        if self._logger is None:
            self.setup("WeReadGears")
        return self._logger

    def debug(self, msg, *args, **kwargs):
        self.logger.debug(msg, *args, **kwargs)

    def info(self, msg, *args, **kwargs):
        self.logger.info(msg, *args, **kwargs)

    def warning(self, msg, *args, **kwargs):
        self.logger.warning(msg, *args, **kwargs)

    def error(self, msg, *args, **kwargs):
        self.logger.error(msg, *args, **kwargs)

    def critical(self, msg, *args, **kwargs):
        self.logger.critical(msg, *args, **kwargs)


logger = Logger()
