"""Клиент регистратора Hikvision/HiWatch по ISAPI.

Правила безопасности, которые здесь соблюдаются:
* Только чтение. Каждый запрос проверяется по белому списку (метод + путь).
  Всё остальное отвергается PolicyError до отправки в сеть.
* Одна попытка входа на запрос. Неверный пароль блокирует клиент до нового входа,
  поэтому программа не доводит регистратор до блокировки адреса.
* Пароль уходит в сеть только через Digest. Basic (пароль открытым текстом)
  разрешён лишь поверх HTTPS.
* HTTPS: сертификат закрепляется по отпечатку SHA-256 (как в SSH, при первом подключении).
* Без перенаправлений и без системного прокси: запросы идут только на указанный адрес.
* Пароль не попадает ни в адреса, ни в тексты ошибок.
"""
from __future__ import annotations

import datetime as dt
import errno
import hashlib
import hmac
import http.client
import ipaddress
import re
import socket
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from xml.sax.saxutils import escape

from .log import log

try:  # защита от XML-бомб, если пакет установлен
    from defusedxml.ElementTree import fromstring as _xml_fromstring
except ImportError:  # pragma: no cover
    _xml_fromstring = ET.fromstring

MAX_XML = 16 * 1024 * 1024


# ---------- ошибки ----------
class NvrError(Exception):
    """Ошибка работы с регистратором. Текст можно показывать пользователю."""


class AuthError(NvrError):
    def __init__(self, msg: str | None = None, *, locked=False, unlock_in: int | None = None,
                 retries_left: int | None = None):
        self.locked = locked
        self.unlock_in = unlock_in
        self.retries_left = retries_left
        if msg is None:
            if locked:
                mins = max(1, round((unlock_in or 1800) / 60))
                msg = f"Регистратор временно заблокировал вход: подождите около {mins} мин."
            else:
                msg = "Неверный логин или пароль."
                if retries_left is not None:
                    msg += f" Осталось попыток: {retries_left}."
        super().__init__(msg)


class ForbiddenError(NvrError):
    OPS = {"live": "онлайн-просмотр", "archive": "архив", "playback": "просмотр архива", "download": "скачивание",
           "snapshot": "снимки", "cameras": "список камер", "info": "сведения об устройстве"}

    def __init__(self, op: str):
        self.op = op
        super().__init__(f"У этой учётной записи нет прав на {self.OPS.get(op, op)}.")


class NotSupportedError(NvrError):
    pass


class PolicyError(NvrError):
    """Запрос не из белого списка: программа не меняет ничего на регистраторе."""


class CertError(NvrError):
    pass


class InsecureAuthError(NvrError):
    pass


# ---------- белый список запросов ----------
_ALLOWED = [(m, re.compile(p)) for m, p in (
    ("GET", r"/ISAPI/System/deviceInfo"),
    ("GET", r"/ISAPI/Security/userCheck"),
    ("GET", r"/ISAPI/ContentMgmt/InputProxy/channels"),
    ("GET", r"/ISAPI/ContentMgmt/InputProxy/channels/status"),
    ("GET", r"/ISAPI/Streaming/channels"),
    ("GET", r"/ISAPI/Streaming/channels/\d{3,5}/picture"),
    ("POST", r"/ISAPI/ContentMgmt/search"),
    ("POST", r"/ISAPI/ContentMgmt/download"),
    ("POST", r"/ISAPI/ContentMgmt/record/tracks/\d{3,5}/dailyDistribution"),
)]


def check_policy(method: str, path: str):
    p = urllib.parse.urlsplit(path).path
    if not any(method == m and rx.fullmatch(p) for m, rx in _ALLOWED):
        raise PolicyError(f"Запрос {method} {p} запрещён: программа работает только на чтение")


# ---------- проверка ввода ----------
_HOST_LABEL = re.compile(r"(?!-)[A-Za-z0-9-]{1,63}(?<!-)")


def validate_host(host: str) -> str:
    h = (host or "").strip()
    h = re.sub(r"^\w+://", "", h).rstrip("/")
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    if not h:
        raise ValueError("Укажите адрес регистратора")
    try:
        return str(ipaddress.ip_address(h))
    except ValueError:
        pass
    if len(h) > 253 or not all(_HOST_LABEL.fullmatch(x) for x in h.rstrip(".").split(".")):
        raise ValueError("Адрес должен быть IP-адресом или именем хоста, например 192.168.1.64")
    if re.fullmatch(r"[\d.]+", h):
        raise ValueError("Неверный IP-адрес")
    return h.lower()


def validate_port(p, name="Порт") -> int:
    try:
        v = int(str(p).strip())
    except ValueError:
        raise ValueError(f"{name}: нужно число") from None
    if not 1 <= v <= 65535:
        raise ValueError(f"{name}: число от 1 до 65535")
    return v


# ---------- данные ----------
@dataclass
class Camera:
    id: int
    name: str
    online: bool = True


@dataclass
class Record:
    channel: int
    start: dt.datetime
    end: dt.datetime
    uri: str          # playbackURI как есть (без XML-экранирования)
    size: int = 0

    @property
    def seconds(self) -> float:
        return max(0.0, (self.end - self.start).total_seconds())


def _strip_ns(text: str) -> str:
    text = re.sub(r'\sxmlns(:\w+)?="[^"]*"', "", text)
    return re.sub(r"<(/?)[\w.-]+:", r"<\1", text)


HTTPS_HINT = ("Регистратор ответил веб-страницей вместо данных. Обычно так бывает, когда в нём включено "
              "перенаправление на HTTPS: откройте «Регистраторы…», включите галочку HTTPS (порт 443) и войдите снова.")


def _looks_html(text: str) -> bool:
    head = text.lstrip()[:200].lower()
    return head.startswith("<!doctype html") or head.startswith("<html") or "<html" in head


def parse_xml(data: bytes, content_type: str = "") -> ET.Element:
    if len(data) > MAX_XML:
        raise NvrError("Слишком большой ответ регистратора")
    text = data.decode("utf-8", "replace")
    if content_type == "text/html" or _looks_html(text):
        raise NvrError(HTTPS_HINT)
    if re.search(r"<!\s*(DOCTYPE|ENTITY)", text, re.I):
        raise NvrError("Регистратор прислал XML с недопустимыми объявлениями (DOCTYPE/ENTITY), ответ отклонён")
    try:
        return _xml_fromstring(_strip_ns(text).encode("utf-8"))
    except Exception:  # noqa: BLE001
        raise NvrError("Не удалось разобрать ответ регистратора") from None


def _parse_time(s: str) -> dt.datetime:
    # Регистратор отдаёт своё местное время с буквой Z на конце.
    s = (s or "").strip().rstrip("Z")
    s = re.sub(r"[+-]\d\d:\d\d$", "", s)
    return dt.datetime.fromisoformat(s)


def _int(s, default=None):
    try:
        return int(str(s).strip())
    except (TypeError, ValueError):
        return default


def _auth_error_from(body: bytes) -> NvrError:
    """Разбор ответа 401: неверный пароль, блокировка или нет прав."""
    try:
        x = parse_xml(body) if body else None
    except NvrError:
        x = None
    if x is None:
        return AuthError()
    sub = (x.findtext(".//subStatusCode") or "").lower()
    if sub in ("nopermission", "lowprivilege"):
        return ForbiddenError("info")
    lock = (x.findtext(".//lockStatus") or "").lower() == "lock"
    return AuthError(locked=lock, unlock_in=_int(x.findtext(".//unlockTime")),
                     retries_left=_int(x.findtext(".//retryLoginTime")))


def net_text(reason, host: str = "", port: int | None = None) -> str:
    """Понятный текст сетевой ошибки. Подробности (errno) остаются в журнале."""
    where = f"{host}:{port}" if host and port else (host or "регистратор")
    if isinstance(reason, NvrError):
        return str(reason)
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return f"Регистратор {where} не отвечает. Проверьте адрес и что компьютер в той же сети."
    if isinstance(reason, socket.gaierror):
        return f"Не удалось найти адрес «{host}». Проверьте, правильно ли он написан."
    if isinstance(reason, ssl.SSLError):
        return (f"Не удалось установить защищённое соединение с {where}. "
                "Возможно, регистратор не поддерживает HTTPS на этом порту.")
    code = getattr(reason, "errno", None)
    if code == errno.ECONNREFUSED:
        return f"По адресу {where} никто не отвечает. Проверьте адрес и порт."
    if code in (errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EHOSTDOWN):
        return f"Нет связи с {host or 'регистратором'}: адрес недоступен из этой сети."
    if code == errno.ECONNRESET:
        return f"Регистратор {where} оборвал соединение. Попробуйте ещё раз."
    if isinstance(reason, OSError) and getattr(reason, "strerror", None):
        return f"Нет связи с {where}: {reason.strerror}."
    return f"Нет связи с {where}."


# ---------- HTTP-обработчики ----------
class _OnceDigest(urllib.request.HTTPDigestAuthHandler):
    """Digest ровно с одной попыткой на запрос."""

    def __init__(self, mgr, allow_basic: bool):
        super().__init__(mgr)
        self.allow_basic = allow_basic

    def http_error_401(self, req, fp, code, msg, headers):
        if getattr(req, "_nvr_auth_tried", False):
            try:
                body = fp.read(65536)
            except Exception:  # noqa: BLE001
                body = b""
            finally:
                fp.close()
            raise _auth_error_from(body)
        challenges = [c.strip().lower() for c in (headers.get_all("WWW-Authenticate") or [])]
        if not any(c.startswith("digest") for c in challenges):
            if self.allow_basic:
                return None  # пусть ответит обработчик Basic (только HTTPS)
            fp.close()
            raise InsecureAuthError("Регистратор предлагает вход без шифрования пароля (Basic) по HTTP. "
                                    "Пароль не отправлен. Включите Digest на регистраторе или HTTPS.")
        req._nvr_auth_tried = True
        self.reset_retry_count()
        return super().http_error_401(req, fp, code, msg, headers)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, *, pin_cb, **kw):
        self._pin_cb = pin_cb
        super().__init__(host, **kw)

    def connect(self):
        super().connect()
        der = self.sock.getpeercert(binary_form=True)
        self._pin_cb(hashlib.sha256(der or b"").hexdigest())


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, ctx, pin_cb):
        super().__init__(context=ctx)
        self._ctx, self._pin_cb = ctx, pin_cb

    def https_open(self, req):
        return self.do_open(lambda host, **kw: _PinnedHTTPSConnection(host, pin_cb=self._pin_cb, **kw),
                            req, context=self._ctx)


def _tls_context() -> ssl.SSLContext:
    # У регистраторов самоподписанные сертификаты: проверяем не цепочку, а отпечаток.
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def fingerprint_text(fp: str) -> str:
    return ":".join(fp[i:i + 2] for i in range(0, min(len(fp), 64), 2)).upper()


def probe(host: str, port: int, https: bool, pin: str = "", timeout: float = 8) -> tuple[str, str, str]:
    """Проверка связи БЕЗ логина и пароля: ни одной попытки входа на регистраторе.

    Возвращает (уровень, текст, отпечаток): уровень "ok", "warn" или "err"; отпечаток сертификата
    для HTTPS (для подсказки администратору, в тексте его нет).
    """
    host = validate_host(host)
    port = validate_port(port)
    seen = []

    def pin_cb(fp):
        seen.append(fp)
        if pin and not hmac.compare_digest(fp, pin):
            raise CertError("Сертификат регистратора не совпадает с сохранённым: возможна подмена. "
                            "Если сертификат меняли намеренно, нажмите ниже «Сбросить отпечаток сертификата».")
    scheme = "https" if https else "http"
    h = f"[{host}]" if ":" in host else host
    url = f"{scheme}://{h}:{port}/ISAPI/System/deviceInfo"
    handlers = [urllib.request.ProxyHandler({}), _NoRedirect()]
    if https:
        handlers.append(_PinnedHTTPSHandler(_tls_context(), pin_cb))
    op = urllib.request.build_opener(*handlers)
    res = _probe(op, url, https, pin, seen, timeout, host, port)
    return res[0], res[1], (seen[0] if seen else "")


def _probe(op, url, https, pin, seen, timeout, host, port) -> tuple[str, str]:
    fp_note = ""
    try:
        with op.open(url, timeout=timeout):
            return "warn", "Регистратор отвечает, но пускает без пароля. Проверьте его настройки."
    except urllib.error.HTTPError as e:
        challenges = [c.strip().lower() for c in (e.headers.get_all("WWW-Authenticate") or [])]
        to_https = (e.headers.get("Location") or "").lower().startswith("https://")
        e.close()
        if seen:
            fp_note = ("\nСертификат совпадает с сохранённым ✓" if pin else
                       "\nСертификат регистратора запомнится при первом входе.")
        if not https and to_https:
            return "warn", ("Регистратор на связи, но требует HTTPS. Включите галочку HTTPS, "
                            "порт станет 443.")
        if e.code == 401 and any(c.startswith("digest") for c in challenges):
            return "ok", "Регистратор на связи, безопасный вход поддерживается." + fp_note
        if e.code == 401 and https:
            return "ok", "Регистратор на связи, соединение защищено (HTTPS)." + fp_note
        if e.code == 401:
            return "err", "Регистратор принимает пароль только открытым текстом (Basic по HTTP). Включите Digest."
        if e.code == 404:
            return "err", "По этому адресу отвечает не регистратор Hikvision/HiWatch (нет ISAPI)."
        return "warn", f"Регистратор ответил кодом {e.code}."
    except urllib.error.URLError as e:
        return "err", net_text(e.reason, host, port)
    except NvrError as e:
        return "err", str(e)
    except (OSError, http.client.HTTPException) as e:
        return "err", net_text(e, host, port)


# ---------- клиент ----------
class Nvr:
    def __init__(self, host: str, user: str, password: str, http_port=80, rtsp_port=554,
                 https=False, pin: str = "", on_new_pin=None, timeout: float = 15):
        self.host = validate_host(host)
        self.http_port = validate_port(http_port, "Порт HTTP")
        self.rtsp_port = validate_port(rtsp_port, "Порт RTSP")
        self.https = bool(https)
        self.pin = (pin or "").lower()
        self.on_new_pin = on_new_pin
        self.timeout = timeout
        self._user = user or ""
        self._password = password or ""
        self.blocked: NvrError | None = None
        self.verified = False
        self._lock = threading.Lock()

    def __repr__(self):
        return f"Nvr({self.host!r}, user={self._user!r})"

    @property
    def user(self) -> str:
        return self._user

    @property
    def password(self) -> str:
        return self._password

    @property
    def base(self) -> str:
        scheme = "https" if self.https else "http"
        default = 443 if self.https else 80
        host = f"[{self.host}]" if ":" in self.host else self.host
        port = "" if self.http_port == default else f":{self.http_port}"
        return f"{scheme}://{host}{port}"

    # ----- сеть -----
    def _check_pin(self, fp: str):
        with self._lock:
            if not self.pin:
                self.pin = fp
                if self.on_new_pin:
                    self.on_new_pin(fp)
                return
            if not hmac.compare_digest(fp, self.pin):
                raise CertError("Сертификат регистратора изменился, соединение остановлено: это может быть "
                                "подмена. Пароль не отправлен. Если сертификат меняли намеренно (например, "
                                "после обновления прошивки), нажмите «Регистраторы», выберите этот регистратор, "
                                "«Изменить» и «Сбросить отпечаток сертификата».")

    def _opener(self):
        mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        mgr.add_password(None, self.base, self._user, self._password)
        handlers = [urllib.request.ProxyHandler({}), _NoRedirect(), _OnceDigest(mgr, self.https)]
        if self.https:
            handlers += [urllib.request.HTTPBasicAuthHandler(mgr), _PinnedHTTPSHandler(_tls_context(), self._check_pin)]
        return urllib.request.build_opener(*handlers)

    def _request(self, method: str, path: str, body: str | None = None, *, op="info",
                 timeout: float | None = None, _verify=True):
        check_policy(method, path)
        if self.blocked is not None:
            raise self.blocked
        headers = {"Content-Type": "application/xml; charset=UTF-8"} if body is not None else {}
        req = urllib.request.Request(self.base + path, method=method, headers=headers,
                                     data=body.encode("utf-8") if body is not None else None)
        try:
            return self._opener().open(req, timeout=timeout or self.timeout)
        except ForbiddenError:
            raise ForbiddenError(op) from None
        except AuthError as e:
            return self._on_auth_error(e, op, path, _verify)
        except InsecureAuthError:
            raise
        except urllib.error.HTTPError as e:
            e.close()
            if e.code == 401:
                if req.get_header("Authorization"):
                    return self._on_auth_error(AuthError(), op, path, _verify)
                raise InsecureAuthError("Регистратор не принял безопасный способ входа (Digest)") from None
            if e.code == 403:
                raise ForbiddenError(op) from None
            if e.code == 404:
                raise NotSupportedError(f"Регистратор не поддерживает {urllib.parse.urlsplit(path).path}") from None
            if 300 <= e.code < 400:
                raise NvrError("Регистратор перенаправил запрос на другой адрес; перенаправления запрещены") from None
            raise NvrError(f"Регистратор ответил ошибкой {e.code}") from None
        except urllib.error.URLError as e:
            if isinstance(e.reason, NvrError):
                raise e.reason from None
            log.info("сеть %s: %r", self.host, e.reason)
            raise NvrError(net_text(e.reason, self.host, self.http_port)) from None
        except (OSError, http.client.HTTPException) as e:
            log.info("сеть %s: %r", self.host, e)
            raise NvrError(net_text(e, self.host, self.http_port)) from None

    def _on_auth_error(self, e: AuthError, op: str, path: str, verify: bool):
        # После успешного входа 401 на отдельной операции бывает и из-за нехватки прав.
        # Одна контрольная проверка входа отличает это от сменившегося пароля.
        if verify and self.verified and "userCheck" not in path and not e.locked:
            self.user_check()
            raise ForbiddenError(op)
        self.blocked = e
        self.verified = False
        raise e

    def _xml(self, method, path, body=None, **kw) -> ET.Element:
        with self._request(method, path, body, **kw) as r:
            return parse_xml(r.read(MAX_XML + 1), r.headers.get_content_type())

    # ----- вход -----
    def user_check(self) -> None:
        """Проверка логина и пароля. Одна попытка; неверный пароль -> AuthError."""
        try:
            x = self._xml("GET", "/ISAPI/Security/userCheck", _verify=False)
            status = _int(x.findtext("statusValue"), 200)
            if status == 401:
                self.blocked = AuthError(locked=(x.findtext("lockStatus") or "") == "lock",
                                         unlock_in=_int(x.findtext("unlockTime")),
                                         retries_left=_int(x.findtext("retryLoginTime")))
                raise self.blocked
        except (NotSupportedError, ForbiddenError):
            # Старая прошивка без userCheck: проверяем через сведения об устройстве.
            try:
                self._xml("GET", "/ISAPI/System/deviceInfo", _verify=False)
            except ForbiddenError:
                pass  # вход принят, просто нет прав на сведения
        self.verified = True

    def device_info(self) -> dict:
        x = self._xml("GET", "/ISAPI/System/deviceInfo")
        return {k: (x.findtext(k) or "") for k in ("deviceName", "model", "serialNumber", "firmwareVersion")}

    # ----- камеры -----
    def cameras(self) -> list[Camera]:
        cams: dict[int, Camera] = {}
        try:
            x = self._xml("GET", "/ISAPI/ContentMgmt/InputProxy/channels", op="cameras")
            for ch in x.iter("InputProxyChannel"):
                cid = _int(ch.findtext("id"), 0)
                if cid > 0:
                    cams[cid] = Camera(cid, (ch.findtext("name") or f"Камера {cid}").strip())
            try:
                st = self._xml("GET", "/ISAPI/ContentMgmt/InputProxy/channels/status", op="cameras")
                for s in st.iter("InputProxyChannelStatus"):
                    cid = _int(s.findtext("id"), 0)
                    if cid in cams:
                        cams[cid].online = (s.findtext("online") or "true").lower() == "true"
            except (ForbiddenError, NotSupportedError):
                pass
        except (ForbiddenError, NotSupportedError):
            x = self._xml("GET", "/ISAPI/Streaming/channels", op="cameras")
            for ch in x.iter("StreamingChannel"):
                sid = _int(ch.findtext("id"), 0)
                if sid and sid % 100 == 1:
                    cid = sid // 100
                    cams[cid] = Camera(cid, (ch.findtext("channelName") or f"Камера {cid}").strip())
        return [cams[k] for k in sorted(cams)]

    # ----- архив -----
    def search(self, channel: int, day: dt.date, limit: int = 5000, page: int = 100) -> list[Record]:
        start = dt.datetime.combine(day, dt.time(0, 0, 0))
        end = dt.datetime.combine(day, dt.time(23, 59, 59))
        sid = str(uuid.uuid4()).upper()
        pos, out = 0, []
        while pos < limit:
            body = (
                '<?xml version="1.0" encoding="UTF-8"?><CMSearchDescription>'
                f"<searchID>{sid}</searchID>"
                f"<trackList><trackID>{int(channel)}01</trackID></trackList>"
                f"<timeSpanList><timeSpan><startTime>{start:%Y-%m-%dT%H:%M:%S}Z</startTime>"
                f"<endTime>{end:%Y-%m-%dT%H:%M:%S}Z</endTime></timeSpan></timeSpanList>"
                f"<maxResults>{page}</maxResults><searchResultPostion>{pos}</searchResultPostion>"
                "<metadataList><metadataDescriptor>//recordType.meta.std-cgi.com"
                "</metadataDescriptor></metadataList></CMSearchDescription>")
            x = self._xml("POST", "/ISAPI/ContentMgmt/search", body, op="archive", timeout=30)
            status = (x.findtext("responseStatusStrg") or "").upper()
            items = list(x.iter("searchMatchItem"))
            for it in items:
                uri = it.findtext("mediaSegmentDescriptor/playbackURI") or ""
                ts = it.find("timeSpan")
                if ts is None or not uri:
                    continue
                try:
                    a, b = _parse_time(ts.findtext("startTime")), _parse_time(ts.findtext("endTime"))
                except ValueError:
                    continue
                q = urllib.parse.parse_qs(urllib.parse.urlsplit(uri).query)
                out.append(Record(int(channel), a, b, uri, _int((q.get("size") or ["0"])[0], 0)))
            pos += len(items)
            if status != "MORE" or not items:
                break
        out.sort(key=lambda r: r.start)
        return out

    def month_days(self, channel: int, year: int, month: int) -> set[int]:
        body = ('<?xml version="1.0" encoding="UTF-8"?><trackDailyParam>'
                f"<year>{int(year)}</year><monthOfYear>{int(month)}</monthOfYear></trackDailyParam>")
        x = self._xml("POST", f"/ISAPI/ContentMgmt/record/tracks/{int(channel)}01/dailyDistribution",
                      body, op="archive")
        return {_int(d.findtext("dayOfMonth"), 0) for d in x.iter("day")
                if (d.findtext("record") or "").lower() == "true"}

    def download(self, rec: Record, dest: str, progress=None, cancel: threading.Event | None = None):
        """Скачивает запись в файл MPEG-PS (без служебного заголовка IMKH)."""
        if not rec.uri.lower().startswith("rtsp://"):
            raise NvrError("Неверная ссылка на запись")
        body = ('<?xml version="1.0" encoding="UTF-8"?><downloadRequest>'
                f"<playbackURI>{escape(rec.uri)}</playbackURI></downloadRequest>")
        got, head_done = 0, False
        with self._request("POST", "/ISAPI/ContentMgmt/download", body, op="download", timeout=60) as r, \
                open(dest, "wb") as f:
            total = _int(r.headers.get("Content-Length"), 0) or rec.size or 0
            while True:
                if cancel is not None and cancel.is_set():
                    raise NvrError("Отменено")
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                got += len(chunk)
                if not head_done:
                    head_done = True
                    if chunk.startswith(b"IMKH"):
                        i = chunk.find(b"\x00\x00\x01\xba", 0, 4096)
                        chunk = chunk[i:] if i >= 0 else chunk[40:]
                f.write(chunk)
                if progress and total:
                    progress(min(1.0, got / total))
        if got == 0:
            raise NvrError("Регистратор вернул пустой файл")

    def snapshot(self, channel: int) -> bytes:
        with self._request("GET", f"/ISAPI/Streaming/channels/{int(channel)}01/picture", op="snapshot") as r:
            data = r.read(32 * 1024 * 1024)
        if not data.startswith(b"\xff\xd8"):
            raise NvrError("Регистратор вернул не изображение")
        return data

    # ----- RTSP -----
    def rtsp_live(self, channel: int, main: bool) -> str:
        """Адрес потока без логина и пароля: их получает rtspsrc отдельными свойствами."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"rtsp://{host}:{self.rtsp_port}/Streaming/Channels/{int(channel)}0{1 if main else 2}"

    def rtsp_check(self, url: str, op: str = "live", timeout: float = 8) -> None:
        """Проверка доступа к потоку RTSP до запуска видео: DESCRIBE, Digest ровно с одной попыткой.

        GStreamer сам повторяет вход на каждый отказ и согласен на Basic (пароль открытым
        текстом), поэтому сначала спрашиваем сами. Basic не отправляем никогда.
        """
        u = urllib.parse.urlsplit(url)
        if u.scheme != "rtsp" or (u.hostname or "").lower() != self.host.strip("[]").lower():
            raise PolicyError("Поток RTSP не на этот регистратор")
        port = u.port or 554

        def ask(sock, f, cseq, auth=None):
            lines = [f"DESCRIBE {url} RTSP/1.0", f"CSeq: {cseq}", "Accept: application/sdp",
                     "User-Agent: nvrview"]
            if auth:
                lines.append("Authorization: " + auth)
            sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8"))
            parts = f.readline(4096).decode("latin-1").split()
            if len(parts) < 2 or not parts[0].startswith("RTSP/"):
                raise NvrError("Регистратор ответил на запрос видео непонятно")
            hdrs = []
            for _ in range(100):
                line = f.readline(8192).decode("latin-1").strip()
                if not line:
                    break
                k, _, v = line.partition(":")
                hdrs.append((k.strip().lower(), v.strip()))
            n = _int(next((v for k, v in hdrs if k == "content-length"), 0), 0)
            if n:
                f.read(min(n, 1 << 20))
            return _int(parts[1], 0), hdrs

        try:
            with socket.create_connection((self.host.strip("[]"), port), timeout) as sock:
                sock.settimeout(timeout)
                f = sock.makefile("rb")
                code, hdrs = ask(sock, f, 1)
                if code == 401:
                    chal = [v for k, v in hdrs if k == "www-authenticate"]
                    dig = next((c for c in chal if c.lower().startswith("digest")), None)
                    if dig is None:
                        raise InsecureAuthError("Регистратор просит пароль для видео без защиты, поэтому программа "
                                                "его не отправила. Обратитесь к администратору регистратора "
                                                "(нужно включить Digest для RTSP).")
                    prm = {m[0].lower(): m[1] or m[2]
                           for m in re.findall(r'(\w+)=(?:"([^"]*)"|([^,\s]*))', dig[6:])}
                    realm, nonce = prm.get("realm", ""), prm.get("nonce", "")
                    md5 = lambda x: hashlib.md5(x.encode("utf-8")).hexdigest()  # noqa: E731
                    ha1 = md5(f"{self._user}:{realm}:{self._password}")
                    ha2 = md5(f"DESCRIBE:{url}")
                    a = f'Digest username="{self._user}", realm="{realm}", nonce="{nonce}", uri="{url}"'
                    if "auth" in [q.strip() for q in prm.get("qop", "").split(",")]:
                        cnonce = uuid.uuid4().hex[:16]
                        resp = md5(f"{ha1}:{nonce}:00000001:{cnonce}:auth:{ha2}")
                        a += f', qop=auth, nc=00000001, cnonce="{cnonce}"'
                    else:
                        resp = md5(f"{ha1}:{nonce}:{ha2}")
                    a += f', response="{resp}"' + (f', opaque="{prm["opaque"]}"' if "opaque" in prm else "")
                    code, _ = ask(sock, f, 2, a)
        except NvrError:
            raise
        except (OSError, ValueError) as e:
            log.info("rtsp %s: %r", self.host, e)
            raise NvrError(net_text(e, self.host, port)) from None
        if code == 200:
            return
        if code in (401, 403):
            # Пароль уже проверен по ISAPI: отказ здесь означает нехватку прав.
            raise ForbiddenError(op)
        if code in (404, 454):
            raise NotSupportedError("Регистратор не нашёл этот поток")
        if code == 453:
            raise NvrError("Камера не в сети или регистратор перегружен")
        raise NvrError(f"Регистратор отказал в видео (код {code})")

    def rtsp_playback(self, channel: int, start: dt.datetime) -> str:
        """Поток архива с момента start (местное время регистратора), без логина и пароля в адресе.

        Регистратор отдаёт записи подряд, пропуская разрывы; endtime он не соблюдает,
        поэтому указываем конец суток только для порядка.
        """
        host = f"[{self.host}]" if ":" in self.host else self.host
        end = dt.datetime.combine(start.date(), dt.time(23, 59, 59))
        return (f"rtsp://{host}:{self.rtsp_port}/Streaming/tracks/{int(channel)}01"
                f"?starttime={start:%Y%m%dT%H%M%S}Z&endtime={end:%Y%m%dT%H%M%S}Z")
