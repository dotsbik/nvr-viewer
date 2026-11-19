"""Архив: скачивание записей в приватный кэш и перепаковка в MP4 без перекодирования видео.

ffmpeg работает только с локальными файлами: ни адреса регистратора, ни пароля
в его командной строке нет.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .isapi import NvrError, Nvr, Record
from .log import log
from .paths import cache_dir

CACHE_DAYS = 3
CACHE_MAX = 5 * 1024 ** 3


def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise NvrError("Для архива нужен ffmpeg: sudo apt install ffmpeg")
    return exe


def _run_ffmpeg(args: list[str]) -> str:
    p = subprocess.run([_ffmpeg(), "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args],
                       capture_output=True, text=True, timeout=3600)
    return "" if p.returncode == 0 else ((p.stderr or "").strip().splitlines() or ["ошибка ffmpeg"])[-1]


class Archive:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else cache_dir()
        self._locks: dict[str, threading.Lock] = {}
        self._glock = threading.Lock()

    def cache_path(self, dev_id: str, r: Record) -> str:
        return str(self.root / f"{dev_id[:12]}_ch{r.channel}_{r.start:%Y%m%d%H%M%S}_{r.end:%H%M%S}.mp4")

    def _lock(self, key: str) -> threading.Lock:
        with self._glock:
            return self._locks.setdefault(key, threading.Lock())

    def ensure_mp4(self, nvr: Nvr, dev_id: str, r: Record, progress=None, cancel=None) -> str:
        out = self.cache_path(dev_id, r)
        with self._lock(out):
            if os.path.exists(out):
                os.utime(out)
                return out
            ps = out[:-4] + ".ps.part"
            try:
                nvr.download(r, ps, progress, cancel)
                self.remux(ps, out)
            finally:
                if os.path.exists(ps):
                    os.remove(ps)
            return out

    @staticmethod
    def remux(src: str, dst: str):
        part = dst + ".part"
        head = ["-fflags", "+genpts", "-f", "mpeg", "-i", src, "-map", "0:v:0"]
        tail = ["-movflags", "+faststart", "-f", "mp4", part]
        err = ""
        for v in (["-map", "0:a:0?", "-c:v", "copy", "-c:a", "aac", "-b:a", "64k"], ["-c:v", "copy", "-an"]):
            err = _run_ffmpeg(head + v + tail)
            if not err and os.path.exists(part) and os.path.getsize(part) > 0:
                os.chmod(part, 0o600)
                os.replace(part, dst)
                return
        if os.path.exists(part):
            os.remove(part)
        log.warning("ffmpeg (перепаковка): %s", err)
        raise NvrError("Не удалось подготовить запись: файл с регистратора повреждён или в неизвестном формате. "
                       "Подробности в журнале программы.")

    @staticmethod
    def concat(files: list[str], dst: str):
        lst, part = dst + ".txt", dst + ".part"
        try:
            with open(lst, "w", encoding="utf-8") as f:
                for p in files:
                    f.write("file '" + p.replace("'", "'\\''") + "'\n")
            err = _run_ffmpeg(["-f", "concat", "-safe", "0", "-i", lst, "-c", "copy",
                               "-movflags", "+faststart", "-f", "mp4", part])
            if err:
                log.warning("ffmpeg (склейка): %s", err)
                raise NvrError("Не удалось склеить записи в один файл. Попробуйте без галочки «Склеить» "
                               "или посмотрите журнал программы.")
            os.replace(part, dst)
        finally:
            for x in (lst, part):
                if os.path.exists(x):
                    os.remove(x)

    def clean(self, max_age_days: float = CACHE_DAYS, max_bytes: int = CACHE_MAX):
        now = time.time()
        files = []
        for p in self.root.iterdir():
            try:
                st = p.stat()
            except OSError:
                continue
            if now - st.st_mtime > max_age_days * 86400 or p.name.endswith(".part"):
                p.unlink(missing_ok=True)
            else:
                if st.st_mode & 0o077:  # файлы старых версий: закрываем доступ остальным
                    os.chmod(p, 0o600)
                files.append((st.st_mtime, st.st_size, p))
        total = sum(f[1] for f in files)
        for _, size, p in sorted(files):
            if total <= max_bytes:
                break
            p.unlink(missing_ok=True)
            total -= size

    def clear(self, dev_id: str | None = None) -> int:
        n = 0
        for p in self.root.iterdir():
            if dev_id is None or p.name.startswith(dev_id[:12] + "_"):
                p.unlink(missing_ok=True)
                n += 1
        return n

    def size(self) -> int:
        return sum(p.stat().st_size for p in self.root.iterdir() if p.is_file())
