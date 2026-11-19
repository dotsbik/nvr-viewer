"""Поддельный RTSP-сервер для проверки доступа: DESCRIBE с Digest (или Basic) и права."""
from __future__ import annotations

import hashlib
import re
import secrets
import socket
import threading


def md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


class FakeRtsp:
    def __init__(self, users, allowed=None, basic=False, code=200):
        self.users, self.basic, self.code = users, basic, code
        self.allowed = set(users) if allowed is None else allowed
        self.realm, self.nonce = "FAKE-RTSP", secrets.token_hex(8)
        self.auth_tries = 0
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.stop = False
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.stop = True
        self.sock.close()

    def _serve(self):
        while not self.stop:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(c,), daemon=True).start()

    def _client(self, c):
        f = c.makefile("rb")
        with c:
            while True:
                first = f.readline().decode()
                if not first:
                    return
                method, uri = first.split()[:2]
                hdrs = {}
                while True:
                    line = f.readline().decode().strip()
                    if not line:
                        break
                    k, _, v = line.partition(":")
                    hdrs[k.strip().lower()] = v.strip()
                cseq = hdrs.get("cseq", "0")
                auth = hdrs.get("authorization")
                if auth:
                    self.auth_tries += 1
                ok_user = None
                if auth and auth.lower().startswith("digest") and not self.basic:
                    p = {m[0]: m[1] or m[2] for m in re.findall(r'(\w+)=(?:"([^"]*)"|([^,\s]*))', auth[6:])}
                    u = p.get("username", "")
                    if u in self.users:
                        ha1 = md5(f"{u}:{self.realm}:{self.users[u]}")
                        exp = md5(f"{ha1}:{self.nonce}:{md5(method + ':' + p.get('uri', ''))}")
                        if exp == p.get("response") and p.get("uri") == uri:
                            ok_user = u
                if ok_user is None or ok_user not in self.allowed:
                    ch = f'Basic realm="{self.realm}"' if self.basic else \
                        f'Digest realm="{self.realm}", nonce="{self.nonce}"'
                    c.sendall(f"RTSP/1.0 401 Unauthorized\r\nCSeq: {cseq}\r\nWWW-Authenticate: {ch}\r\n\r\n".encode())
                    continue
                body = b"v=0\r\n" if self.code == 200 else b""
                c.sendall(f"RTSP/1.0 {self.code} X\r\nCSeq: {cseq}\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
