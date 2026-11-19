"""Тесты ядра на поддельном регистраторе. Реальный регистратор не используется.

Запуск: python3 -m unittest discover -s tests -v
"""
import datetime as dt
import json
import logging
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_TMP = tempfile.mkdtemp(prefix="nvrview-test-")
for _v in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
    os.environ[_v] = os.path.join(_TMP, _v.lower())

from fake_nvr import ALL, FakeNvr  # noqa: E402

from nvrview import log as nlog  # noqa: E402
from nvrview.devices import Device, DeviceStore  # noqa: E402
from nvrview.isapi import (AuthError, CertError, ForbiddenError, InsecureAuthError, Nvr,  # noqa: E402
                           NvrError, PolicyError, validate_host)
from nvrview.secrets import MemoryStore  # noqa: E402
from nvrview.session import LoginGuard, Session  # noqa: E402

PW = "Secr3t!pw"


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


class IsapiTest(unittest.TestCase):
    def client(self, fake, user="admin", pw=PW, **kw):
        return Nvr("127.0.0.1", user, pw, http_port=fake.port, https=fake.https, **kw)

    def test_login_ok(self):
        with FakeNvr() as f:
            n = self.client(f)
            n.user_check()
            self.assertEqual(n.device_info()["model"], "FAKE-NVR")
            self.assertEqual(f.failed_logins, 0)

    def test_wrong_password_one_attempt_then_blocked(self):
        with FakeNvr() as f:
            n = self.client(f, pw="wrong")
            with self.assertRaises(AuthError) as cm:
                n.user_check()
            self.assertEqual(f.failed_logins, 1, "ровно одна неверная попытка")
            self.assertEqual(cm.exception.retries_left, 4)
            sent = len(f.requests)
            for call in (n.device_info, n.cameras, lambda: n.search(1, dt.date(2026, 11, 18))):
                with self.assertRaises(AuthError):
                    call()
            self.assertEqual(len(f.requests), sent, "после отказа запросы в сеть не уходят")
            self.assertNotIn("wrong", str(cm.exception))

    def test_nvr_lock_is_reported(self):
        with FakeNvr(lock_after=1) as f:
            with self.assertRaises(AuthError) as cm:
                self.client(f, pw="bad").user_check()
            self.assertTrue(cm.exception.locked)
            self.assertEqual(cm.exception.unlock_in, 1800)

    def test_basic_over_http_refused_password_never_sent(self):
        with FakeNvr(basic_only=True) as f:
            with self.assertRaises(InsecureAuthError):
                self.client(f).user_check()
            self.assertTrue(all(a is None for _m, _p, a in f.requests), "пароль не отправлялся")
            self.assertEqual(f.failed_logins, 0)

    def test_policy_blocks_writes_before_network(self):
        with FakeNvr() as f:
            n = self.client(f)
            for m, p in (("PUT", "/ISAPI/System/reboot"), ("DELETE", "/ISAPI/Security/users/1"),
                         ("POST", "/ISAPI/System/factoryReset"), ("GET", "/ISAPI/Security/users"),
                         ("PUT", "/ISAPI/ContentMgmt/search"), ("GET", "/ISAPI/System/deviceInfo/../reboot")):
                with self.assertRaises(PolicyError, msg=f"{m} {p}"):
                    n._request(m, p)
            self.assertEqual(f.requests, [])

    def test_forbidden_403(self):
        users = {"op": (PW, {"proxy", "live"})}
        with FakeNvr(users) as f:
            n = self.client(f, user="op")
            n.user_check()
            with self.assertRaises(ForbiddenError) as cm:
                n.search(1, dt.date(2026, 11, 18))
            self.assertEqual(cm.exception.op, "archive")
            with self.assertRaises(ForbiddenError):
                n.snapshot(1)
            self.assertIsNone(n.blocked, "нехватка прав не блокирует вход")
            self.assertEqual(len(n.cameras()), 3)

    def test_forbidden_as_401_is_not_treated_as_bad_password(self):
        for variant in ("noPermission", "plain"):
            with self.subTest(variant=variant), FakeNvr({"op": (PW, {"proxy"})}, deny_401=variant) as f:
                n = self.client(f, user="op")
                n.user_check()
                with self.assertRaises(ForbiddenError):
                    n.search(1, dt.date(2026, 11, 18))
                self.assertIsNone(n.blocked)
                self.assertEqual(f.failed_logins, 0)

    def test_cameras_fallback_without_proxy_rights(self):
        with FakeNvr({"op": (PW, {"live"})}) as f:
            n = self.client(f, user="op")
            cams = n.cameras()
            self.assertEqual([c.id for c in cams], [1, 2])

    def test_camera_status(self):
        with FakeNvr() as f:
            cams = self.client(f).cameras()
            self.assertEqual([(c.id, c.online) for c in cams], [(1, True), (2, True), (3, False)])

    def test_search_paging(self):
        with FakeNvr(n_records=250) as f:
            recs = self.client(f).search(1, dt.date(2026, 11, 18))
            self.assertEqual(len(recs), 250)
            self.assertEqual(recs[0].start, dt.datetime(2026, 11, 18, 0, 0))
            self.assertEqual(recs[0].size, 1000)
            self.assertIn("&name=0&", recs[0].uri)
            self.assertTrue(all(a.start <= b.start for a, b in zip(recs, recs[1:])))

    def test_download_strips_imkh(self):
        with FakeNvr() as f:
            n = self.client(f)
            rec = n.search(1, dt.date(2026, 11, 18))[0]
            out = os.path.join(_TMP, "clip.ps")
            n.download(rec, out)
            with open(out, "rb") as fh:
                self.assertEqual(fh.read(), b"\x00\x00\x01\xbapayload")

    def test_xml_with_doctype_rejected(self):
        with FakeNvr(mode="doctype") as f:
            with self.assertRaises(NvrError):
                self.client(f).device_info()

    def test_html_instead_of_data_suggests_https(self):
        from nvrview.isapi import probe
        with FakeNvr(mode="https_only") as f:
            with self.assertRaises(NvrError) as cm:
                self.client(f).user_check()
            self.assertNotIsInstance(cm.exception, AuthError)
            self.assertIn("HTTPS", str(cm.exception))
            self.assertEqual(f.failed_logins, 0)
            level, text, _fp = probe("127.0.0.1", f.port, False)
            self.assertEqual(level, "warn")
            self.assertIn("HTTPS", text)

    def test_redirect_not_followed(self):
        with FakeNvr(mode="redirect") as f:
            with self.assertRaises(NvrError):
                self.client(f).device_info()
            self.assertNotIn("/elsewhere", [p for _m, p, _a in f.requests])

    def test_probe_sends_no_credentials(self):
        from nvrview.isapi import probe
        with FakeNvr() as f:
            level, _text, _fp = probe("127.0.0.1", f.port, False)
            self.assertEqual(level, "ok")
            self.assertEqual(f.failed_logins, 0)
            self.assertTrue(all(a is None for _m, _p, a in f.requests))
        with FakeNvr(basic_only=True) as f:
            self.assertEqual(probe("127.0.0.1", f.port, False)[0], "err")

    def test_rtsp_url_has_no_credentials(self):
        n = Nvr("10.0.0.5", "admin", PW)
        url = n.rtsp_live(2, main=False)
        self.assertEqual(url, "rtsp://10.0.0.5:554/Streaming/Channels/202")
        self.assertNotIn(PW, url + repr(n))

    def test_rtsp_playback_url(self):
        n = Nvr("10.0.0.5", "admin", PW, rtsp_port=8554)
        url = n.rtsp_playback(3, dt.datetime(2026, 11, 18, 10, 5, 7))
        self.assertEqual(url, "rtsp://10.0.0.5:8554/Streaming/tracks/301"
                              "?starttime=20261118T100507Z&endtime=20261118T235959Z")
        self.assertNotIn(PW, url)

    def test_network_errors_are_human(self):
        import socket
        from nvrview.isapi import net_text, probe
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()  # порт заведомо закрыт
        level, text, _fp = probe("127.0.0.1", port, False)
        self.assertEqual(level, "err")
        self.assertIn("никто не отвечает", text)
        self.assertNotIn("Errno", text)
        self.assertIn("не отвечает", net_text(socket.timeout("timed out"), "h", 80))
        self.assertIn("Не удалось найти", net_text(socket.gaierror(-2, "Name"), "nvr.local", 80))
        with self.assertRaises(NvrError) as cm:
            Nvr("127.0.0.1", "admin", PW, http_port=port).user_check()
        self.assertNotIn("Errno", str(cm.exception))

    def test_rtsp_check_one_digest_attempt_never_basic(self):
        from fake_rtsp import FakeRtsp
        from nvrview.isapi import InsecureAuthError, NotSupportedError
        with FakeRtsp({"admin": PW}) as r:
            n = Nvr("127.0.0.1", "admin", PW, rtsp_port=r.port)
            n.rtsp_check(n.rtsp_live(1, False))
            self.assertEqual(r.auth_tries, 1)
        with FakeRtsp({"admin": PW}, allowed=set()) as r:
            n = Nvr("127.0.0.1", "admin", PW, rtsp_port=r.port)
            with self.assertRaises(ForbiddenError):
                n.rtsp_check(n.rtsp_playback(1, dt.datetime(2026, 11, 18, 9, 0)), "playback")
            self.assertEqual(r.auth_tries, 1, "при отказе пароль не повторяется")
        with FakeRtsp({"admin": PW}, basic=True) as r:
            n = Nvr("127.0.0.1", "admin", PW, rtsp_port=r.port)
            with self.assertRaises(InsecureAuthError):
                n.rtsp_check(n.rtsp_live(1, False))
            self.assertEqual(r.auth_tries, 0, "пароль открытым текстом не отправлен")
        with FakeRtsp({"admin": PW}, code=454) as r:
            n = Nvr("127.0.0.1", "admin", PW, rtsp_port=r.port)
            with self.assertRaises(NotSupportedError):
                n.rtsp_check(n.rtsp_live(1, False))
        with self.assertRaises(PolicyError):
            Nvr("127.0.0.1", "admin", PW).rtsp_check("rtsp://10.9.9.9/Streaming/Channels/101")

    def test_validate_host(self):
        self.assertEqual(validate_host("http://192.168.1.64/"), "192.168.1.64")
        self.assertEqual(validate_host("NVR.local"), "nvr.local")
        for bad in ("", "10.0.0.300", "a b", "host/../x", "-x.ru", "a@b"):
            with self.assertRaises(ValueError, msg=bad):
                validate_host(bad)


@unittest.skipUnless(shutil.which("openssl"), "нет openssl")
class HttpsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pem = os.path.join(_TMP, "cert.pem")
        cls.pem2 = os.path.join(_TMP, "cert2.pem")
        for p in (cls.pem, cls.pem2):
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                            "-subj", "/CN=fake", "-keyout", p, "-out", p], check=True, capture_output=True)

    def test_pin_on_first_use_then_enforced(self):
        seen = []
        with FakeNvr(certfile=self.pem) as f:
            n = Nvr("127.0.0.1", "admin", PW, http_port=f.port, https=True, on_new_pin=seen.append)
            n.user_check()
            self.assertEqual(len(seen), 1)
            pin = seen[0]
            Nvr("127.0.0.1", "admin", PW, http_port=f.port, https=True, pin=pin).user_check()
            from nvrview.isapi import probe
            self.assertIn("совпадает с сохранённым", probe("127.0.0.1", f.port, True, pin)[1])
            self.assertIn("запомнится", probe("127.0.0.1", f.port, True, "")[1])
        with FakeNvr(certfile=self.pem2) as f:
            with self.assertRaises(CertError):
                Nvr("127.0.0.1", "admin", PW, http_port=f.port, https=True, pin=pin).user_check()
            self.assertTrue(all(a is None for _m, _p, a in f.requests), "пароль не отправлен подменному серверу")
            self.assertEqual(probe("127.0.0.1", f.port, True, pin)[0], "err")

    def test_basic_allowed_over_https(self):
        with FakeNvr(certfile=self.pem, basic_only=True) as f:
            Nvr("127.0.0.1", "admin", PW, http_port=f.port, https=True).user_check()


class GuardTest(unittest.TestCase):
    def test_pause_after_three_failures_persisted(self):
        now = [1000.0]
        path = os.path.join(_TMP, "guard.json")
        g = LoginGuard(path, clock=lambda: now[0])
        self.assertEqual(g.failed("d1"), 0)
        self.assertEqual(g.failed("d1"), 0)
        self.assertEqual(g.failed("d1"), LoginGuard.PAUSE)
        self.assertEqual(LoginGuard(path, clock=lambda: now[0]).wait_left("d1"), LoginGuard.PAUSE)
        self.assertEqual(g.wait_left("d2"), 0)
        self.assertEqual(mode(path), 0o600)
        now[0] += LoginGuard.PAUSE + 1
        self.assertEqual(g.wait_left("d1"), 0)
        g.succeeded("d1")
        self.assertEqual(g.failures("d1"), 0)


class DevicesTest(unittest.TestCase):
    def test_roundtrip_no_password_private(self):
        path = os.path.join(_TMP, "cfg", "devices.json")
        s = DeviceStore(path)
        d = Device.from_dict({"name": "Цех", "host": "192.168.1.64", "user": "op", "password": "LEAK"})
        s.upsert(d)
        s.current = d.id
        s.save()
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
        self.assertNotIn("LEAK", raw)
        self.assertNotIn("password", raw)
        self.assertEqual(mode(path), 0o600)
        self.assertEqual(mode(os.path.dirname(path)), 0o700)
        s2 = DeviceStore(path)
        self.assertEqual(s2.get(d.id).name, "Цех")
        self.assertEqual(s2.current, d.id)
        self.assertEqual(json.loads(raw)["version"], 1)

    def test_validation(self):
        with self.assertRaises(ValueError):
            Device(name="", host="10.0.0.1").validate()
        with self.assertRaises(ValueError):
            Device(name="x", host="10.0.0.1", http_port=70000).validate()

    def test_broken_file_kept_aside(self):
        path = os.path.join(_TMP, "broken", "devices.json")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as fh:
            fh.write("{oops")
        self.assertEqual(DeviceStore(path).devices, [])
        self.assertTrue(os.path.exists(path + ".broken"))


class LogTest(unittest.TestCase):
    def test_redaction(self):
        nlog.register_secret("Pa$$w0rd-xyz")
        try:
            t = nlog.redact("auth Pa$$w0rd-xyz; Authorization: Digest username=\"a\", response=\"f00\"\n"
                            "password=hunter22 rtsp://admin:topsecret@10.0.0.1/")
            self.assertNotIn("Pa$$w0rd-xyz", t)
            self.assertNotIn("f00", t)
            self.assertNotIn("hunter22", t)
            self.assertNotIn("topsecret", t)
            rec = logging.LogRecord("x", logging.INFO, "", 0, "pw is %s", ("Pa$$w0rd-xyz",), None)
            nlog.RedactFilter().filter(rec)
            self.assertNotIn("Pa$$w0rd-xyz", rec.getMessage())
        finally:
            nlog.unregister_secret("Pa$$w0rd-xyz")


class Keyring(MemoryStore):
    persistent = True


class SessionTest(unittest.TestCase):
    def make(self, fake, guard_name):
        dev = Device(name="Тест", host="127.0.0.1", http_port=fake.port).validate()
        return Session(dev, Keyring(), LoginGuard(os.path.join(_TMP, guard_name)))

    def test_login_remember_and_logout(self):
        with FakeNvr() as f:
            s = self.make(f, "g1.json")
            cams = s.login("admin", PW, remember=True)
            self.assertEqual(len(cams), 3)
            self.assertTrue(s.connected)
            self.assertEqual(s.secrets.lookup(s.device.id, "admin"), PW)
            self.assertEqual(nlog.redact(PW), "***")
            s.logout()
            self.assertFalse(s.connected)
            self.assertNotEqual(nlog.redact(PW), "***")
            self.assertEqual(s.login_saved()[0].name, "Cam1")
            s.logout(forget=True)
            self.assertIsNone(s.secrets.lookup(s.device.id, "admin"))

    def test_no_remember_keeps_nothing(self):
        with FakeNvr() as f:
            s = self.make(f, "g2.json")
            s.login("admin", PW, remember=False)
            self.assertIsNone(s.secrets.lookup(s.device.id, "admin"))
            self.assertIsNone(s.login_saved())

    def test_stale_saved_password_is_forgotten(self):
        with FakeNvr() as f:
            s = self.make(f, "g3.json")
            s.device.user, s.device.remember = "admin", True
            s.secrets.store(s.device.id, "admin", "old-password")
            with self.assertRaises(AuthError):
                s.login_saved()
            self.assertIsNone(s.secrets.lookup(s.device.id, "admin"))
            self.assertFalse(s.device.remember)
            self.assertIsNone(s.login_saved(), "повторной попытки со старым паролем нет")
            self.assertEqual(f.failed_logins, 1)

    def test_retries_message_is_honest(self):
        with FakeNvr(lock_after=5) as f:
            s = self.make(f, "g5.json")
            with self.assertRaises(AuthError) as cm:
                s.login("admin", "bad", False)
            self.assertEqual(cm.exception.retries_left, 2, "наш предел 3 меньше, чем 5 у регистратора")
            self.assertIn("Осталось попыток: 2", str(cm.exception))
        with FakeNvr(lock_after=2) as f:
            s = self.make(f, "g6.json")
            with self.assertRaises(AuthError) as cm:
                s.login("admin", "bad", False)
            self.assertEqual(cm.exception.retries_left, 1, "у регистратора осталась одна попытка")

    def test_guard_stops_before_nvr_locks(self):
        with FakeNvr(lock_after=5) as f:
            s = self.make(f, "g4.json")
            for _ in range(3):
                with self.assertRaises(AuthError):
                    s.login("admin", "bad", False)
            with self.assertRaises(AuthError) as cm:
                s.login("admin", PW, False)
            self.assertTrue(cm.exception.locked)
            self.assertEqual(f.failed_logins, 3, "до блокировки регистратора не дошли")
            self.assertEqual(s.state, Session.LOCKED)


class HelpersTest(unittest.TestCase):
    def test_keep_page_and_gaps(self):
        import importlib
        sys.modules.setdefault("gi", importlib.import_module("gi"))
        import gi
        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        gi.require_version("Gst", "1.0")
        from nvrview.isapi import Record
        from nvrview.ui.archive import gaps
        from nvrview.ui.common import safe_name
        from nvrview.ui.live import keep_page
        self.assertEqual(keep_page(1, 16, 4), 4)   # камеры 17-20 -> страница 5 из сетки на 4
        self.assertEqual(keep_page(4, 4, 16), 1)
        self.assertEqual(keep_page(0, 9, 1), 0)
        t = dt.datetime(2026, 11, 18, 1, 0, 0)
        recs = [Record(1, t, t + dt.timedelta(seconds=30), "rtsp://x"),
                Record(1, t + dt.timedelta(seconds=30), t + dt.timedelta(seconds=60), "rtsp://x"),
                Record(1, t + dt.timedelta(seconds=90), t + dt.timedelta(seconds=120), "rtsp://x")]
        self.assertEqual(gaps(recs), [(t + dt.timedelta(seconds=60), t + dt.timedelta(seconds=90))])
        self.assertEqual(safe_name("Склад №2 (рампа)"), "Склад №2 (рампа)")
        self.assertEqual(safe_name("../a/b"), "a_b")
        self.assertEqual(safe_name(""), "camera")
        from nvrview.ui.archive import day_overlap, onvif_ns
        from nvrview.ui.common import plural
        day = dt.date(2026, 11, 18)
        night = [Record(1, dt.datetime(2026, 11, 17, 23, 0), dt.datetime(2026, 11, 18, 1, 0), "rtsp://x")]
        self.assertEqual(day_overlap(night, day), 3600)
        self.assertEqual(onvif_ns(dt.datetime(1900, 1, 1, 0, 0, 1)), 10 ** 9)
        self.assertEqual([plural(n, "файл", "файла", "файлов") for n in (1, 3, 5, 11, 21, 22, 112)],
                         ["1 файл", "3 файла", "5 файлов", "11 файлов", "21 файл", "22 файла", "112 файлов"])


@unittest.skipUnless(shutil.which("ffmpeg"), "нет ffmpeg")
class MediaTest(unittest.TestCase):
    def test_remux_and_concat(self):
        from nvrview.media import Archive
        d = os.path.join(_TMP, "media")
        os.makedirs(d, exist_ok=True)
        srcs = []
        for i in range(2):
            ps = os.path.join(d, f"s{i}.ps")
            subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=d=1:s=320x240:r=10",
                            "-c:v", "libx264" if "libx264" in subprocess.run(
                                ["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
                            else "mpeg2video", "-f", "mpeg", ps], check=True)
            mp4 = os.path.join(d, f"s{i}.mp4")
            Archive.remux(ps, mp4)
            self.assertEqual(mode(mp4), 0o600)
            srcs.append(mp4)
        out = os.path.join(d, "joined.mp4")
        Archive.concat(srcs, out)
        self.assertGreater(os.path.getsize(out), 0)


if __name__ == "__main__":
    unittest.main()
