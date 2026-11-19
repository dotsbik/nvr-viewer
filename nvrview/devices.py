"""Список регистраторов и общие настройки.

Файл ~/.config/nvrview/devices.json (0600). Паролей в нём нет и быть не может:
поля проверяются по списку, лишние отбрасываются.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from .isapi import validate_host, validate_port
from .paths import config_dir, write_private

SCHEMA_VERSION = 1
LAYOUTS = (1, 4, 9, 16)


@dataclass
class Device:
    name: str
    host: str
    http_port: int = 80
    rtsp_port: int = 554
    https: bool = False
    user: str = ""
    remember: bool = False
    cert_sha256: str = ""
    layout: int = 9
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def validate(self) -> "Device":
        self.name = (self.name or "").strip()[:80]
        if not self.name:
            raise ValueError("Укажите название регистратора")
        self.host = validate_host(self.host)
        self.http_port = validate_port(self.http_port, "Порт HTTP" if not self.https else "Порт HTTPS")
        self.rtsp_port = validate_port(self.rtsp_port, "Порт RTSP")
        self.user = (self.user or "").strip()[:64]
        self.layout = self.layout if self.layout in LAYOUTS else 9
        self.cert_sha256 = "".join(c for c in (self.cert_sha256 or "").lower() if c in "0123456789abcdef")[:64]
        return self

    @property
    def address(self) -> str:
        scheme = "https" if self.https else "http"
        return f"{scheme}://{self.host}" + ("" if self.http_port in (80, 443) else f":{self.http_port}")

    @classmethod
    def from_dict(cls, d: dict) -> "Device":
        allowed = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in allowed}).validate()


def _default_video_dir() -> str:
    try:
        from gi.repository import GLib
        d = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_VIDEOS)
    except Exception:  # noqa: BLE001
        d = None
    return os.path.join(d or os.path.expanduser("~"), "Регистратор")


class DeviceStore:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else config_dir() / "devices.json"
        self.devices: list[Device] = []
        self.current: str = ""
        self.download_dir: str = _default_video_dir()
        self.load()

    def load(self):
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Повреждённый файл не затираем молча: сохраняем копию.
            try:
                os.replace(self.path, self.path.with_suffix(".json.broken"))
            except OSError:
                pass
            return
        self.devices = []
        for d in data.get("devices", []):
            try:
                self.devices.append(Device.from_dict(d))
            except (TypeError, ValueError):
                continue
        self.current = str(data.get("current", ""))
        dd = data.get("settings", {}).get("download_dir")
        if isinstance(dd, str) and os.path.isabs(dd):
            self.download_dir = dd

    def save(self):
        data = {"version": SCHEMA_VERSION, "current": self.current,
                "settings": {"download_dir": self.download_dir},
                "devices": [asdict(d) for d in self.devices]}
        write_private(self.path, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))

    def get(self, dev_id: str) -> Device | None:
        return next((d for d in self.devices if d.id == dev_id), None)

    def upsert(self, dev: Device):
        dev.validate()
        for i, d in enumerate(self.devices):
            if d.id == dev.id:
                self.devices[i] = dev
                break
        else:
            self.devices.append(dev)
        self.save()

    def remove(self, dev_id: str):
        self.devices = [d for d in self.devices if d.id != dev_id]
        if self.current == dev_id:
            self.current = self.devices[0].id if self.devices else ""
        self.save()
