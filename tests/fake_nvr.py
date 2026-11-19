"""Поддельный регистратор Hikvision для тестов: Digest/Basic, права, блокировка, HTTPS."""
from __future__ import annotations

import base64
import hashlib
import http.server
import re
import secrets
import ssl
import threading
import urllib.parse

ALL = {"proxy", "live", "archive", "download", "snapshot"}
NS = 'xmlns="http://www.hikvision.com/ver20/XMLSchema"'


def md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


class FakeNvr:
    def __init__(self, users=None, *, basic_only=False, lock_after=5, n_records=250,
                 deny_401=None, certfile=None, mode=""):
        self.users = users or {"admin": ("Secr3t!pw", ALL)}
        self.basic_only = basic_only
        self.lock_after = lock_after
        self.n_records = n_records
        self.deny_401 = deny_401   # None: нет прав -> 403; "plain" / "noPermission": -> 401
        self.mode = mode           # "doctype", "redirect"
        self.realm = "DS-FAKE"
        self.failed_logins = 0
        self.requests: list[tuple[str, str, str | None]] = []  # (method, path, auth scheme)
        self.lock = threading.Lock()
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                fake._handle(self)

            do_POST = do_PUT = do_DELETE = do_GET

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.https = bool(certfile)
        if certfile:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(certfile)
            self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()

    # ---------- авторизация ----------
    def _send(self, h, code, body: bytes = b"", ctype="application/xml", headers=()):
        h.send_response(code)
        for k, v in headers:
            h.send_header(k, v)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        if h.command != "HEAD":
            h.wfile.write(body)

    def _challenge(self, h, body=b""):
        if self.basic_only:
            hdr = ("WWW-Authenticate", f'Basic realm="{self.realm}"')
        else:
            hdr = ("WWW-Authenticate", f'Digest realm="{self.realm}", qop="auth", nonce="{secrets.token_hex(16)}"')
        extra = [("Location", f"https://127.0.0.1:{self.port}{h.path}")] if self.mode == "https_only" else []
        self._send(h, 401, body, headers=[hdr] + extra)

    def _fail_body(self):
        left = max(0, self.lock_after - self.failed_logins)
        lock = "lock" if left == 0 else "unlock"
        return (f'<?xml version="1.0"?><userCheck {NS}><statusValue>401</statusValue>'
                f"<statusString>Unauthorized</statusString><lockStatus>{lock}</lockStatus>"
                f"<unlockTime>{1800 if left == 0 else 0}</unlockTime><retryLoginTime>{left}</retryLoginTime>"
                "</userCheck>").encode()

    def _auth(self, h) -> str | None:
        a = h.headers.get("Authorization")
        if not a:
            return None
        scheme, _, rest = a.partition(" ")
        if scheme.lower() == "basic" and self.basic_only:
            user, _, pw = base64.b64decode(rest).decode().partition(":")
            return user if user in self.users and self.users[user][0] == pw else ""
        if scheme.lower() != "digest" or self.basic_only:
            return ""
        p = {m[0]: m[1] or m[2] for m in re.findall(r'(\w+)=(?:"([^"]*)"|([^,\s]*))', rest)}
        user = p.get("username", "")
        if user not in self.users:
            return ""
        ha1 = md5(f"{user}:{self.realm}:{self.users[user][0]}")
        ha2 = md5(f"{h.command}:{p.get('uri', '')}")
        exp = md5(f"{ha1}:{p.get('nonce')}:{p.get('nc')}:{p.get('cnonce')}:{p.get('qop')}:{ha2}")
        return user if exp == p.get("response") else ""

    # ---------- маршруты ----------
    def _handle(self, h):
        path = urllib.parse.urlsplit(h.path).path
        auth = h.headers.get("Authorization")
        with self.lock:
            self.requests.append((h.command, path, auth.split(" ")[0] if auth else None))
        n = int(h.headers.get("Content-Length") or 0)
        body = h.rfile.read(n).decode() if n else ""
        user = self._auth(h)
        if user is None:
            return self._challenge(h)
        if user == "":
            with self.lock:
                self.failed_logins += 1
            return self._challenge(h, self._fail_body())
        rights = self.users[user][1]

        def need(right):
            if right in rights:
                return True
            if self.deny_401 == "noPermission":
                self._send(h, 401, f"<ResponseStatus {NS}><subStatusCode>noPermission</subStatusCode>"
                                   "</ResponseStatus>".encode(),
                           headers=[("WWW-Authenticate", f'Digest realm="{self.realm}", qop="auth", '
                                                         f'nonce="{secrets.token_hex(16)}"')])
            elif self.deny_401 == "plain":
                self._challenge(h)
            else:
                self._send(h, 403, f"<ResponseStatus {NS}><statusCode>4</statusCode></ResponseStatus>".encode())
            return False

        if self.mode == "https_only":
            return self._send(h, 200, b"<!DOCTYPE html><html><head><title>Redirect</title></head></html>",
                              ctype="text/html")
        if self.mode == "redirect" and path == "/ISAPI/System/deviceInfo":
            return self._send(h, 302, headers=[("Location", "/elsewhere")])
        if path == "/ISAPI/Security/userCheck":
            return self._send(h, 200, f"<userCheck {NS}><statusValue>200</statusValue></userCheck>".encode())
        if path == "/ISAPI/System/deviceInfo":
            if self.mode == "doctype":
                return self._send(h, 200, b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>')
            return self._send(h, 200, f"<DeviceInfo {NS}><model>FAKE-NVR</model><firmwareVersion>V1</firmwareVersion>"
                                      "</DeviceInfo>".encode())
        if path == "/ISAPI/ContentMgmt/InputProxy/channels":
            if not need("proxy"):
                return
            xs = "".join(f"<InputProxyChannel><id>{i}</id><name>Cam{i}</name></InputProxyChannel>" for i in (1, 2, 3))
            return self._send(h, 200, f"<InputProxyChannelList {NS}>{xs}</InputProxyChannelList>".encode())
        if path == "/ISAPI/ContentMgmt/InputProxy/channels/status":
            xs = "".join(f"<InputProxyChannelStatus><id>{i}</id><online>{'false' if i == 3 else 'true'}</online>"
                         "</InputProxyChannelStatus>" for i in (1, 2, 3))
            return self._send(h, 200, f"<InputProxyChannelStatusList {NS}>{xs}</InputProxyChannelStatusList>".encode())
        if path == "/ISAPI/Streaming/channels":
            xs = "".join(f"<StreamingChannel><id>{i}0{s}</id><channelName>S{i}</channelName></StreamingChannel>"
                         for i in (1, 2) for s in (1, 2))
            return self._send(h, 200, f"<StreamingChannelList {NS}>{xs}</StreamingChannelList>".encode())
        if path == "/ISAPI/ContentMgmt/search":
            if not need("archive"):
                return
            pos = int(re.search(r"<searchResultPostion>(\d+)<", body).group(1))
            mx = int(re.search(r"<maxResults>(\d+)<", body).group(1))
            items = []
            for i in range(pos, min(self.n_records, pos + mx)):
                hh, mm = divmod(i, 60)
                items.append(
                    f"<searchMatchItem><timeSpan><startTime>2026-11-18T{hh:02d}:{mm:02d}:00Z</startTime>"
                    f"<endTime>2026-11-18T{hh:02d}:{mm:02d}:30Z</endTime></timeSpan><mediaSegmentDescriptor>"
                    f"<playbackURI>rtsp://x/Streaming/tracks/101/?starttime=a&amp;name={i}&amp;size=1000"
                    "</playbackURI></mediaSegmentDescriptor></searchMatchItem>")
            more = pos + mx < self.n_records
            return self._send(h, 200, (f"<CMSearchResult {NS}><responseStatusStrg>{'MORE' if more else 'OK'}"
                                       f"</responseStatusStrg><numOfMatches>{len(items)}</numOfMatches>"
                                       f"<matchList>{''.join(items)}</matchList></CMSearchResult>").encode())
        if path == "/ISAPI/ContentMgmt/download":
            if not need("download"):
                return
            return self._send(h, 200, b"IMKH" + b"\x00" * 36 + b"\x00\x00\x01\xba" + b"payload",
                              ctype="Opaque/data")
        if re.fullmatch(r"/ISAPI/Streaming/channels/\d+/picture", path):
            if not need("snapshot"):
                return
            return self._send(h, 200, b"\xff\xd8\xff\xe0fakejpeg", ctype="image/jpeg")
        return self._send(h, 404, b"<ResponseStatus/>")
