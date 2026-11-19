"""Хранение паролей в системном брелоке (Secret Service: GNOME Keyring, KWallet).

Пароль шифруется брелоком и открывается входом пользователя в систему.
В файлах программы паролей нет.
"""
from __future__ import annotations

import threading


class MemoryStore:
    """Хранилище в памяти: для тестов и для систем без брелока."""

    persistent = False

    def __init__(self):
        self._d: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()

    def store(self, device_id: str, user: str, password: str, label: str = ""):
        with self._lock:
            self._d[(device_id, user)] = password

    def lookup(self, device_id: str, user: str) -> str | None:
        with self._lock:
            return self._d.get((device_id, user))

    def clear(self, device_id: str, user: str | None = None):
        with self._lock:
            for k in [k for k in self._d if k[0] == device_id and (user is None or k[1] == user)]:
                del self._d[k]


class KeyringStore:
    persistent = True

    def __init__(self):
        import gi
        gi.require_version("Secret", "1")
        from gi.repository import Secret
        self.Secret = Secret
        self.schema = Secret.Schema.new(
            "local.nvrview.Device", Secret.SchemaFlags.NONE,
            {"device-id": Secret.SchemaAttributeType.STRING, "user": Secret.SchemaAttributeType.STRING})
        # Проверяем, что служба брелока доступна.
        Secret.Service.get_sync(Secret.ServiceFlags.NONE, None)

    def store(self, device_id: str, user: str, password: str, label: str = ""):
        ok = self.Secret.password_store_sync(
            self.schema, {"device-id": device_id, "user": user}, self.Secret.COLLECTION_DEFAULT,
            f"Видеорегистратор: {label or device_id} ({user})", password, None)
        if not ok:
            raise RuntimeError("Брелок паролей отказал в сохранении")

    def lookup(self, device_id: str, user: str) -> str | None:
        return self.Secret.password_lookup_sync(self.schema, {"device-id": device_id, "user": user}, None)

    def clear(self, device_id: str, user: str | None = None):
        attrs = {"device-id": device_id}
        if user is not None:
            attrs["user"] = user
        self.Secret.password_clear_sync(self.schema, attrs, None)


def open_store():
    """Брелок, если он есть; иначе пароли только в памяти (галочка «Запомнить» недоступна)."""
    try:
        return KeyringStore()
    except Exception:  # noqa: BLE001
        return MemoryStore()
