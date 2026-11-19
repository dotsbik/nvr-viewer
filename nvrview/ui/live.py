"""Онлайн-просмотр: сетка камер 1/4/9/16 со страницами, одна камера в основном потоке."""
from __future__ import annotations

import datetime as dt
import math
import os
import time

from gi.repository import Gdk, GLib, Gst, Gtk, Pango

from ..devices import LAYOUTS
from ..isapi import AuthError, Camera, ForbiddenError, InsecureAuthError, NotSupportedError, NvrError
from ..log import log
from .common import (bg, black_box, gst_text, icon_button, is_double, make_sink, pictures_dir, safe_name, ui,
                     unique_path)

TILE_TIP = "Двойной щелчок: крупно, в лучшем качестве. Правый щелчок: меню камеры"


class LiveTile(Gtk.EventBox):
    def __init__(self, page: "LivePage", cam: Camera, main=False):
        super().__init__()
        self.page, self.cam, self.main = page, cam, main
        self.pipe = None
        self.running = False
        self.retry_id = 0
        self.delay = 3
        self.last_buf = 0.0
        self.got_frame = False
        self.dead = False  # нет прав: не переподключаемся
        self.checking = False
        self.get_style_context().add_class("tile")
        self.set_above_child(True)
        self.set_size_request(160, 90)

        ov = Gtk.Overlay()
        self.add(ov)
        self.sink = None
        if cam.online:
            self.sink, w = make_sink()
            ov.add(w)
            self.sink.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, self._on_buf)
        else:
            ov.add(black_box())
        name = Gtk.Label(label=cam.name, halign=Gtk.Align.START, valign=Gtk.Align.START,
                         ellipsize=Pango.EllipsizeMode.END, max_width_chars=40)
        name.get_style_context().add_class("camname")
        self.set_tooltip_text(f"{cam.name}\n{TILE_TIP}" if not main else cam.name)
        ov.add_overlay(name)
        self.status = Gtk.Label(label="Подключение…" if cam.online else "Камера не в сети",
                                halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, wrap=True, justify=Gtk.Justification.CENTER)
        self.status.get_style_context().add_class("status")
        self.status.set_no_show_all(True)
        self.status.show()
        ov.add_overlay(self.status)
        self.connect("button-press-event", self._on_click)
        self.connect("destroy", lambda *_: self._dispose())

    def _dispose(self):
        self.stop()
        if self.pipe is not None:
            self.pipe.get_bus().remove_signal_watch()
            self.pipe = None

    def _on_click(self, _w, ev):
        if ev.button == 3 and ev.type == Gdk.EventType.BUTTON_PRESS:
            self.page.camera_menu(self, ev)
            return True
        if is_double(ev):
            if self.main:
                self.page.close_single()
            else:
                self.page.open_single(self.cam)
        return True

    def _on_buf(self, _pad, _info):
        self.last_buf = time.monotonic()
        if not self.got_frame:
            self.got_frame = True
            ui(self._first_frame)
        return Gst.PadProbeReturn.OK

    def _first_frame(self):
        self.delay = 3
        self.status.hide()

    def show_status(self, text):
        self.status.set_text(text)
        self.status.show()

    def _build(self):
        p = Gst.ElementFactory.make("playbin")
        p.props.uri = self.page.session.nvr.rtsp_live(self.cam.id, self.main)
        p.props.video_sink = self.sink
        p.props.flags = 0x1  # только видео
        p.connect("source-setup", self._source_setup)
        bus = p.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_msg)
        self.pipe = p

    def _source_setup(self, _pb, src):
        nvr = self.page.session.nvr if self.page.session else None
        if nvr is None:
            return
        # Логин и пароль передаются свойствами, а не в адресе потока.
        if src.find_property("user-id"):
            src.props.user_id = nvr.user
            src.props.user_pw = nvr.password
        if src.find_property("protocols"):
            Gst.util_set_object_arg(src, "protocols", "tcp")
        if src.find_property("latency"):
            src.props.latency = 300
        if src.find_property("drop-on-latency"):
            src.props.drop_on_latency = True

    def _on_msg(self, _bus, msg):
        if msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            self._halt()
            text = f"{err.message or ''} {dbg or ''}"
            if "orbidden" in text:
                self.forbidden()
            elif err.matches(Gst.ResourceError.quark(), Gst.ResourceError.NOT_AUTHORIZED) or "nauthorized" in text:
                self.dead = True
                self.show_status("Нет доступа, проверяю…")
                self.page.rtsp_unauthorized(self)
            else:
                log.info("камера %s: %s (%s)", self.cam.name, err.message, (dbg or "").splitlines()[:1])
                self._retry(gst_text(err))
        elif msg.type == Gst.MessageType.EOS:
            self._halt()
            self._retry("Поток прервался")

    def forbidden(self):
        self.dead = True
        self.stop()
        self.show_status("Нет прав на онлайн-просмотр этой камеры")

    def _retry(self, why):
        if not self.running or self.dead:
            return
        self.show_status(f"{why}\nПовтор через {self.delay} с")
        self.retry_id = GLib.timeout_add_seconds(self.delay, self._do_retry)
        self.delay = min(self.delay * 2, 60)

    def _do_retry(self):
        self.retry_id = 0
        if self.running:
            self._play()
        return False

    def _halt(self):
        if self.pipe is not None:
            self.pipe.set_state(Gst.State.NULL)

    def _play(self):
        s = self.page.session
        if s is None or not s.connected or self.dead or self.checking:
            return
        url = s.nvr.rtsp_live(self.cam.id, self.main)
        if url.split("?", 1)[0] not in s.rtsp_ok:
            # Сначала одна своя проверка доступа: GStreamer при отказе повторяет пароль много раз.
            self.checking = True
            self.show_status("Подключение…")

            def ok(_):
                self.checking = False
                if self.running and self.page.session is s:
                    self._play()

            def fail(e):
                self.checking = False
                if not self.running or self.page.session is not s:
                    return
                if isinstance(e, ForbiddenError):
                    s.mark_forbidden("live", self.cam.id)
                    self.forbidden()
                elif isinstance(e, (InsecureAuthError, NotSupportedError)):
                    self.dead = True
                    self.show_status(str(e))
                elif isinstance(e, NvrError):
                    self._retry(str(e).rstrip("."))
            bg(s.check_rtsp, url, "live", done=ok, fail=fail)
            return
        if self.pipe is None:
            self._build()
        self.got_frame = False
        self.last_buf = time.monotonic()
        self.show_status("Подключение…")
        self.pipe.set_state(Gst.State.PLAYING)

    def start(self):
        if not self.cam.online or self.running:
            return
        s = self.page.session
        if s is not None and s.is_forbidden("live", self.cam.id):
            self.forbidden()
            return
        self.running = True
        self.delay = 3
        self._play()

    def stop(self):
        self.running = False
        if self.retry_id:
            GLib.source_remove(self.retry_id)
            self.retry_id = 0
        self._halt()

    def watchdog(self):
        """Поток молча застыл: перезапускаем."""
        if self.running and not self.dead and self.retry_id == 0 and time.monotonic() - self.last_buf > 20:
            self._halt()
            self._retry("Нет кадров")


class LivePage(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.session = None
        self.tiles: list[LiveTile] = []
        self.single: LiveTile | None = None
        self.page_no = 0
        self.active = False
        self._verifying = False
        self._loading = False
        self._pending: list[LiveTile] = []

        # панель над сеткой
        bar = Gtk.Box(spacing=6, margin=4)
        bar.pack_start(Gtk.Label(label="Сетка"), False, False, 0)
        self.layout = Gtk.ComboBoxText()
        for n in LAYOUTS:
            self.layout.append(str(n), f"{n}")
        self.layout.set_active_id("9")
        self.layout.connect("changed", lambda *_: self._layout_changed())
        bar.pack_start(self.layout, False, False, 0)
        self.prev_btn = icon_button("go-previous-symbolic", "Предыдущая страница", lambda: self.turn(-1))
        self.next_btn = icon_button("go-next-symbolic", "Следующая страница", lambda: self.turn(1))
        self.page_label = Gtk.Label()
        for w in (self.prev_btn, self.page_label, self.next_btn):
            w.set_can_focus(False)  # случайный пробел/Enter не листает страницы
            bar.pack_start(w, False, False, 0)
        self.grid_bar = bar

        self.stack = Gtk.Stack()
        gbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        gbox.pack_start(bar, False, False, 0)
        self.grid = Gtk.Grid(row_homogeneous=True, column_homogeneous=True, row_spacing=2, column_spacing=2)
        gbox.pack_start(self.grid, True, True, 0)
        self.empty = Gtk.Label(label="Нет подключения к регистратору")
        self.empty.get_style_context().add_class("hint")
        self.stack.add_named(self.empty, "empty")
        self.stack.add_named(gbox, "grid")

        sbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sbar = Gtk.Box(spacing=6, margin=4)
        back = Gtk.Button(label="← Все камеры")
        back.connect("clicked", lambda *_: self.close_single())
        sbar.pack_start(back, False, False, 0)
        self.single_name = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        sbar.pack_start(self.single_name, True, True, 6)
        self.snap_btn = Gtk.Button(label="Снимок")
        self.snap_btn.set_tooltip_text("Сохранить кадр в «Изображения/Регистратор»")
        self.snap_btn.connect("clicked", lambda *_: self.snapshot())
        sbar.pack_end(self.snap_btn, False, False, 0)
        arch = Gtk.Button(label="Архив этой камеры")
        arch.connect("clicked", lambda *_: self.single and self.win.goto_archive(self.single.cam))
        sbar.pack_end(arch, False, False, 0)
        sbox.pack_start(sbar, False, False, 0)
        self.single_area = Gtk.Box()
        sbox.pack_start(self.single_area, True, True, 0)
        self.stack.add_named(sbox, "single")
        self.pack_start(self.stack, True, True, 0)
        GLib.timeout_add_seconds(5, self._watchdog)

    # ----- сеанс -----
    def set_session(self, session):
        self.close_single(restart=False)
        self._clear_tiles()
        self.session = session
        self.page_no = 0
        if session is None or not session.connected:
            self.empty.set_text("Нет подключения к регистратору" if session is None else
                                "Войдите в регистратор, чтобы смотреть камеры")
            self.stack.set_visible_child_name("empty")
            return
        self._loading = True
        self.layout.set_active_id(str(session.device.layout))
        self._loading = False
        self._build()
        self.stack.set_visible_child_name("grid")
        self.set_active(self.active)

    def _clear_tiles(self):
        for t in self.tiles:
            t.destroy()
        self.tiles = []
        for c in self.grid.get_children():
            c.destroy()

    def _per_page(self) -> int:
        return int(self.layout.get_active_id() or 9)

    def _build(self):
        self._clear_tiles()
        if self.session is None:
            return
        cams = self.session.cameras
        per = self._per_page()
        pages = max(1, math.ceil(len(cams) / per))
        self.page_no = min(self.page_no, pages - 1)
        shown = cams[self.page_no * per:(self.page_no + 1) * per]
        # Камер больше, чем ячеек: сетка ровно как выбрана. Меньше: подгоняем под их число.
        cells = per if len(cams) > per else len(shown)
        cols = max(1, math.ceil(math.sqrt(cells)))
        for i in range(cells):
            r, c = divmod(i, cols)
            if i < len(shown):
                t = LiveTile(self, shown[i])
                self.tiles.append(t)
                self.grid.attach(t, c, r, 1, 1)
            else:
                self.grid.attach(black_box(), c, r, 1, 1)
        self.page_label.set_text(f"Стр. {self.page_no + 1} из {pages}")
        for w in (self.prev_btn, self.page_label, self.next_btn):
            w.set_visible(pages > 1)
        self.prev_btn.set_sensitive(self.page_no > 0)
        self.next_btn.set_sensitive(self.page_no < pages - 1)
        self.grid.show_all()
        if self.active and self.single is None:
            for t in self.tiles:
                t.start()

    def _layout_changed(self):
        if self.session is None or self._loading:
            return
        n = self._per_page()
        old = self.session.device.layout
        if old != n:
            # Остаёмся на тех же камерах: страница, где оказалась первая из видимых.
            self.page_no = keep_page(self.page_no, old, n)
            self.session.device.layout = n
            self.win.save_devices()
        self._build()

    def turn(self, d):
        self.page_no += d
        self._build()

    # ----- запуск / остановка -----
    def set_active(self, active: bool):
        self.active = active
        if active and self.session is not None and self.session.connected:
            for t in ([self.single] if self.single else self.tiles):
                t.start()
        else:
            self.stop_all()

    def stop_all(self):
        for t in self.tiles:
            t.stop()
        if self.single is not None:
            self.single.stop()

    def _watchdog(self):
        for t in self.tiles + ([self.single] if self.single else []):
            t.watchdog()
        return True

    # ----- одна камера -----
    def open_single(self, cam: Camera):
        for t in self.tiles:
            t.stop()
        self.single = LiveTile(self, cam, main=True)
        self.single_area.pack_start(self.single, True, True, 0)
        self.single_name.set_markup(f"<b>{GLib.markup_escape_text(cam.name)}</b>  · основной поток · "
                                    "двойной щелчок или Esc: назад")
        self.single_name.set_tooltip_text(cam.name)
        self.snap_btn.set_sensitive(not (self.session and self.session.is_forbidden("snapshot", cam.id)))
        self.single_area.show_all()
        self.stack.set_visible_child_name("single")
        if self.active:
            self.single.start()

    def close_single(self, restart=True):
        if self.single is None:
            return False
        self.single.destroy()
        self.single = None
        self.stack.set_visible_child_name("grid")
        if restart and self.active:
            for t in self.tiles:
                t.start()
        return True

    def camera_menu(self, tile: LiveTile, ev):
        cam, s = tile.cam, self.session
        m = Gtk.Menu()
        items = [("Назад ко всем камерам" if tile.main else "Крупно", lambda: self.close_single() if tile.main
                  else self.open_single(cam))]
        if not (s and s.is_forbidden("snapshot", cam.id)):
            items.append(("Снимок кадра", lambda: self.snapshot(cam)))
        items.append(("Архив этой камеры", lambda: self.win.goto_archive(cam)))
        for label, fn in items:
            it = Gtk.MenuItem(label=label)
            it.connect("activate", lambda *_, f=fn: f())
            m.append(it)
        m.show_all()
        m.attach_to_widget(tile, None)
        m.popup_at_pointer(ev)

    def snapshot(self, cam: Camera | None = None):
        cam = cam or (self.single.cam if self.single else None)
        if cam is None or self.session is None or not self.session.connected:
            return
        nvr, s = self.session.nvr, self.session

        def take():
            data = nvr.snapshot(cam.id)
            d = pictures_dir()
            os.makedirs(d, exist_ok=True)
            p = unique_path(os.path.join(d, f"{safe_name(cam.name)}_{dt.datetime.now():%Y-%m-%d_%H-%M-%S}.jpg"))
            with open(p, "wb") as f:
                f.write(data)
            return p

        def fail(e):
            if isinstance(e, ForbiddenError):
                s.mark_forbidden("snapshot", cam.id)
                self.snap_btn.set_sensitive(False)
            self.win.handle_error(s, e)
        bg(take, done=lambda p: self.win.show_info(f"Снимок сохранён: {os.path.basename(p)}",
                                                   folder=os.path.dirname(p), autohide=True), fail=fail)

    # ----- отказ RTSP -----
    def rtsp_unauthorized(self, tile: LiveTile):
        """RTSP ответил 401. Это либо нет прав на камеру, либо сменился пароль.

        Делаем одну контрольную проверку входа по ISAPI, а не повторяем RTSP:
        иначе каждая камера сетки добавила бы по неудачной попытке.
        """
        s = self.session
        if s is None or not s.connected:
            return
        if self._verifying:
            self._pending.append(tile)
            return
        self._verifying = True
        self._pending = [tile]

        def ok(_):
            self._verifying = False
            for t in self._pending:
                s.mark_forbidden("live", t.cam.id)
                t.forbidden()

        def fail(e):
            self._verifying = False
            if isinstance(e, AuthError):
                self.stop_all()
            self.win.handle_error(s, e)
        bg(s.nvr.user_check, done=ok, fail=fail)


def keep_page(page_no: int, old_per: int, new_per: int) -> int:
    """Номер страницы при смене сетки: та, на которой окажется первая видимая камера."""
    return (page_no * max(1, old_per)) // max(1, new_per)
