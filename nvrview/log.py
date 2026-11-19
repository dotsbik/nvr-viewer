"""Журнал программы с вырезанием секретов.

Пароли текущих сеансов регистрируются через register_secret() и заменяются на ***
в каждой строке журнала и в сообщениях, которые видит пользователь (redact()).
Дополнительно вырезаются заголовки Authorization и пары вида password=...
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import re
import threading

from .paths import state_dir

_secrets: set[str] = set()
_lock = threading.Lock()
_PATTERNS = [
    (re.compile(r"(?i)(authorization\s*[:=]\s*)(digest|basic)\s+[^\r\n]*"), r"\1\2 ***"),
    (re.compile(r"(?i)((?:pass(?:word)?|pwd|user-pw|user_pw)\s*[=:]\s*)[^\s&;,'\"]+"), r"\1***"),
    (re.compile(r"(?i)(\w+://[^/\s:@]+:)[^@\s/]+@"), r"\1***@"),
]


def register_secret(s: str | None):
    if s and len(s) >= 3:
        with _lock:
            _secrets.add(s)


def unregister_secret(s: str | None):
    with _lock:
        _secrets.discard(s or "")


def redact(text) -> str:
    text = str(text)
    with _lock:
        secrets = sorted(_secrets, key=len, reverse=True)
    for s in secrets:
        text = text.replace(s, "***")
    for rx, rep in _PATTERNS:
        text = rx.sub(rep, text)
    return text


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if record.exc_info:
            msg += "\n" + logging.Formatter().formatException(record.exc_info)
            record.exc_info = None
            record.exc_text = None
        record.msg = redact(msg)
        record.args = None
        return True


log = logging.getLogger("nvrview")


def log_path():
    return state_dir() / "nvrview.log"


def setup(level=logging.INFO) -> str:
    path = str(log_path())
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.close(fd)
    os.chmod(path, 0o600)
    fh = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s: %(message)s"))
    fh.addFilter(RedactFilter())
    sh = logging.StreamHandler()
    sh.setLevel(logging.WARNING)
    sh.setFormatter(logging.Formatter("nvrview: %(levelname)s: %(message)s"))
    sh.addFilter(RedactFilter())
    log.handlers[:] = [fh, sh]
    log.setLevel(level)
    log.propagate = False
    return path
