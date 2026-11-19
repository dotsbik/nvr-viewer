"""Сеанс работы с одним регистратором: вход, выход, права, защита от подбора пароля."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from .devices import Device
from .isapi import AuthError, Camera, Nvr
from .log import log, register_secret, unregister_secret
from .paths import state_dir, write_private


class LoginGuard:
    """Счётчик неудачных входов, переживает перезапуск программы.

    После MAX_FAILS неудач подряд вход к этому регистратору закрывается на PAUSE секунд.
    Регистратор сам блокирует адрес после нескольких неудач; мы останавливаемся раньше.
    """

    MAX_FAILS = 3
    PAUSE = 30 * 60

    def __init__(self, path: Path | None = None, clock=time.time):
        self.path = Path(path) if path else state_dir() / "login_guard.json"
        self.clock = clock
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, d: dict):
        write_private(self.path, json.dumps(d).encode("utf-8"))

    def wait_left(self, dev_id: str) -> int:
        with self._lock:
            e = self._load().get(dev_id) or {}
        return max(0, int(float(e.get("until", 0)) - self.clock()))

    def failures(self, dev_id: str) -> int:
        with self._lock:
            return int((self._load().get(dev_id) or {}).get("fails", 0))

    def failed(self, dev_id: str, nvr_unlock_in: int | None = None) -> int:
        with self._lock:
            d = self._load()
            e = d.get(dev_id) or {"fails": 0, "until": 0}
            e["fails"] = int(e.get("fails", 0)) + 1
            now = self.clock()
            until = float(e.get("until", 0))
            if e["fails"] >= self.MAX_FAILS:
                until = max(until, now + self.PAUSE)
            if nvr_unlock_in:
                until = max(until, now + int(nvr_unlock_in))
            e["until"] = until
            d[dev_id] = e
            self._save(d)
            return max(0, int(until - now))

    def succeeded(self, dev_id: str):
        with self._lock:
            d = self._load()
            if dev_id in d:
                del d[dev_id]
                self._save(d)


class Session:
    OFF, CONNECTING, ON, AUTH_FAILED, LOCKED, ERROR = "off", "connecting", "on", "auth", "locked", "error"

    def __init__(self, device: Device, secrets, guard: LoginGuard, on_device_changed=None):
        self.device = device
        self.secrets = secrets
        self.guard = guard
        self.on_device_changed = on_device_changed
        self.nvr: Nvr | None = None
        self.cameras: list[Camera] = []
        self.state = self.OFF
        self.error = ""
        self._forbidden: set[tuple[str, int | None]] = set()
        self.rtsp_ok: set[str] = set()   # адреса потоков, к которым доступ уже проверен
        self.no_onvif = False            # регистратор не умеет перематывать поток архива

    # ----- вход -----
    def saved_password(self) -> str | None:
        if not (self.device.remember and self.device.user):
            return None
        try:
            return self.secrets.lookup(self.device.id, self.device.user)
        except Exception as e:  # noqa: BLE001
            log.warning("брелок недоступен: %s", e)
            return None

    def login(self, user: str, password: str, remember: bool) -> list[Camera]:
        """Вход (блокирующий: вызывать в фоне). Одна попытка."""
        dev = self.device
        user = (user or "").strip()
        if not user or not password:
            raise AuthError("Введите логин и пароль")
        wait = self.guard.wait_left(dev.id)
        if wait:
            self.state = self.LOCKED
            raise AuthError(locked=True, unlock_in=wait,
                            msg=f"Слишком много неудачных входов. Следующая попытка через {max(1, round(wait / 60))} мин.")
        self.state = self.CONNECTING
        nvr = Nvr(dev.host, user, password, dev.http_port, dev.rtsp_port, dev.https,
                  dev.cert_sha256, on_new_pin=self._new_pin)
        try:
            nvr.user_check()
        except AuthError as e:
            wait = self.guard.failed(dev.id, e.unlock_in if e.locked else None)
            self.state = self.LOCKED if (e.locked or wait) else self.AUTH_FAILED
            log.info("вход на %s (%s): отклонён", dev.name, user)
            if wait and not e.locked:
                raise AuthError(f"Неверный логин или пароль. Вход закрыт на {max(1, round(wait / 60))} мин., "
                                "чтобы регистратор не заблокировал компьютер.", unlock_in=wait) from None
            if not e.locked:
                # Честное число попыток: меньшее из того, что разрешает регистратор, и нашего предела.
                left = self.guard.MAX_FAILS - self.guard.failures(dev.id)
                if e.retries_left is not None:
                    left = min(left, e.retries_left)
                mins = max(1, round(self.guard.PAUSE / 60))
                raise AuthError(f"Неверный логин или пароль. Осталось попыток: {left}, потом вход "
                                f"закроется на {mins} мин.", retries_left=left) from None
            raise
        except Exception:
            self.state = self.ERROR
            raise
        self.guard.succeeded(dev.id)
        register_secret(password)
        try:
            cams = nvr.cameras()
        except Exception:
            unregister_secret(password)
            self.state = self.ERROR
            raise
        self._forbidden.clear()
        self.rtsp_ok.clear()
        self.nvr, self.cameras, self.state, self.error = nvr, cams, self.ON, ""
        dev.user = user
        dev.remember = bool(remember and self.secrets.persistent)
        try:
            if dev.remember:
                self.secrets.store(dev.id, user, password, dev.name)
            else:
                self.secrets.clear(dev.id)
        except Exception as e:  # noqa: BLE001
            dev.remember = False
            log.warning("не удалось сохранить пароль в брелоке: %s", e)
        log.info("вход на %s (%s): успешно, камер %d", dev.name, user, len(cams))
        return cams

    def login_saved(self) -> list[Camera] | None:
        pw = self.saved_password()
        if not pw:
            return None
        try:
            return self.login(self.device.user, pw, True)
        except AuthError as e:
            if not e.locked and self.state == self.AUTH_FAILED:
                # Пароль на регистраторе сменили: сохранённый больше не годится.
                self.forget_password()
            raise

    def logout(self, forget: bool = False):
        if self.nvr is not None:
            unregister_secret(self.nvr.password)
        self.nvr = None
        self.cameras = []
        self._forbidden.clear()
        self.rtsp_ok.clear()
        self.state = self.OFF
        if forget:
            self.forget_password()

    def forget_password(self):
        try:
            self.secrets.clear(self.device.id)
        except Exception as e:  # noqa: BLE001
            log.warning("брелок: %s", e)
        self.device.remember = False

    def _new_pin(self, fp: str):
        self.device.cert_sha256 = fp
        log.info("закреплён сертификат %s: %s", self.device.name, fp)
        if self.on_device_changed:
            self.on_device_changed(self.device)

    # ----- права -----
    def mark_forbidden(self, op: str, cam: int | None = None):
        self._forbidden.add((op, cam))

    def is_forbidden(self, op: str, cam: int | None = None) -> bool:
        return (op, None) in self._forbidden or (op, cam) in self._forbidden

    def check_rtsp(self, url: str, op: str):
        """Один раз за сеанс на поток: доступ есть (иначе ForbiddenError и т.п.). Блокирующий."""
        key = url.split("?", 1)[0]
        if key in self.rtsp_ok:
            return
        self.nvr.rtsp_check(url, op)
        self.rtsp_ok.add(key)

    @property
    def connected(self) -> bool:
        return self.state == self.ON and self.nvr is not None and self.nvr.blocked is None
