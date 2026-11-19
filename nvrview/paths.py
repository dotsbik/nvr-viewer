"""Каталоги программы и запись приватных файлов.

Всё, что пишет программа, доступно только владельцу: каталоги 0700, файлы 0600.
"""
from __future__ import annotations

import os
from pathlib import Path


def _xdg(var: str, default: str) -> Path:
    v = os.environ.get(var)
    return Path(v if v and os.path.isabs(v) else os.path.expanduser(default))


def private_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    if p.stat().st_mode & 0o077:
        os.chmod(p, 0o700)
    return p


def config_dir() -> Path:
    return private_dir(_xdg("XDG_CONFIG_HOME", "~/.config") / "nvrview")


def state_dir() -> Path:
    return private_dir(_xdg("XDG_STATE_HOME", "~/.local/state") / "nvrview")


def cache_dir() -> Path:
    return private_dir(_xdg("XDG_CACHE_HOME", "~/.cache") / "nvrview")


def write_private(path: Path, data: bytes):
    """Атомарная запись: временный файл 0600, fsync, rename."""
    path = Path(path)
    private_dir(path.parent)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
