"""Архив: дата, список и шкала дня, просмотр потоком (RTSP) или файлом, скачивание в MP4.

Просмотр с обычной скоростью идёт потоком RTSP прямо с регистратора, как в iVMS:
начинается за пару секунд и не требует права на скачивание. Для ускоренного
просмотра запись скачивается в кэш (регистратор не умеет ускорять поток).
"""
from __future__ import annotations

import datetime as dt
import os
import shutil
import threading

from gi.repository import GLib, Gst, Gtk, Pango

from ..isapi import AuthError, Camera, ForbiddenError, InsecureAuthError, NvrError, Record
from ..log import log
from ..media import Archive
from .common import (bg, black_box, ellipsize_combo, set_status, err_text, fmt_dur, fmt_size, gst_text, icon_button, is_double,
                     make_sink, open_folder, safe_name, ui, unique_path)
from .timeline import Timeline

WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
          "ноября", "декабря")
GAP = 2.0  # секунд: меньше этого считаем записи идущими подряд
E1900 = dt.datetime(1900, 1, 1)  # ONVIF: время в потоке считается от 1900 года
ABS = 10 ** 18                   # больше этого: метка времени абсолютная (ONVIF), а не от начала потока


def onvif_ns(t: dt.datetime) -> int:
    """Момент архива для ONVIF (Range: clock). Регистратор хранит местное время и называет его UTC."""
    return int((t - E1900).total_seconds() * Gst.SECOND)


def day_overlap(recs: list[Record], day: dt.date) -> float:
    """Сколько секунд записей приходится на сам день (запись с вечера накануне считается частично)."""
    a = dt.datetime.combine(day, dt.time())
    b = a + dt.timedelta(days=1)
    return sum(max(0.0, (min(r.end, b) - max(r.start, a)).total_seconds()) for r in recs)


def gaps(recs: list[Record]) -> list[tuple[dt.datetime, dt.datetime]]:
    """Разрывы между записями (по порядку): [(конец предыдущей, начало следующей)]."""
    return [(a.end, b.start) for a, b in zip(recs, recs[1:]) if (b.start - a.end).total_seconds() > GAP]


def fmt_day(d: dt.date, today: dt.date | None = None) -> str:
    today = today or dt.date.today()
    year = "" if d.year == today.year else f" {d.year}"
    base = f"{d.day} {MONTHS[d.month - 1]}{year}, {WEEKDAYS[d.weekday()]}"
    if d == today:
        return "Сегодня, " + base
    if d == today - dt.timedelta(days=1):
        return "Вчера, " + base
    return base


class ArchivePage(Gtk.Paned):
    RATES = (1.0, 2.0, 4.0, 8.0)

    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        self.win = win
        self.session = None
        self.cams: list[Camera] = []
        self.records: list[Record] = []
        self.day = dt.date.today()
        self.cur = -1
        self.token = 0
        self.play_token = 0
        self.pending = None
        self.cancel: threading.Event | None = None
        self._mkey = None
        self._loading = False
        self.active = False
        self._dirty = True
        # просмотр потоком
        self.mode: str | None = None       # "stream" | "file" | None
        self.s_base: dt.datetime | None = None
        self.s_first: int | None = None     # pts первого кадра потока
        self.s_last: int | None = None
        self._seek_id = 0
        self._s_retried = False
        self._s_seek_sent = False
        self._ever_abs = False
        self.s_target: dt.datetime | None = None  # куда перематываем поток (ONVIF)
        self.s_now: dt.datetime | None = None     # время кадра на экране (из потока)
        self._seg = None
        self._seg_abs = False
        self.s_onvif = False
        self._s_started = 0.0
        self.user_paused = False
        self.at_end = False                       # «Подряд» выключено и запись кончилась
        self.reconnect_at: dt.datetime | None = None
        self.resume: tuple[int, dt.datetime] | None = None  # продолжить после возврата на вкладку
        self._last_time: dt.datetime | None = None
        self._buf_mono = 0.0        # когда пришёл последний кадр (монотонные секунды)
        self._paused_mono = 0.0     # когда поставили на паузу
        self._dead_restarts = 0     # переподключения подряд без кадров

        # ----- левая панель -----
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=8)
        left.set_size_request(280, -1)
        self.cam_combo = Gtk.ComboBoxText()
        ellipsize_combo(self.cam_combo, 28)
        self.cam_combo.connect("changed", lambda *_: self._loading or self.refresh())
        left.pack_start(self.cam_combo, False, False, 0)

        # дата: строка со стрелками, календарь во всплывающем окне (экономит место на маленьком экране)
        dbox = Gtk.Box(spacing=2)
        dbox.get_style_context().add_class("linked")
        dbox.pack_start(icon_button("go-previous-symbolic", "Предыдущий день", lambda: self.shift_day(-1)),
                        False, False, 0)
        self.cal = Gtk.Calendar()
        self._month_nav = False
        self.cal.connect("day-selected", lambda *_: self._loading or self._day_clicked())
        self.cal.connect("month-changed", lambda *_: self._loading or self._month_changed())
        pop = Gtk.Popover()
        cbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin=6, spacing=4)
        cbox.pack_start(self.cal, False, False, 0)
        cbox.pack_start(self._hint("Жирным отмечены дни с записями"), False, False, 0)
        today = Gtk.Button(label="Сегодня")
        today.connect("clicked", lambda *_: self.set_day(dt.date.today()))
        cbox.pack_start(today, False, False, 0)
        cbox.show_all()
        pop.add(cbox)
        self.date_btn = Gtk.MenuButton(popover=pop)
        self.date_btn.set_tooltip_text("Выбрать день")
        self.date_label = Gtk.Label(ellipsize=Pango.EllipsizeMode.END)
        self.date_btn.add(self.date_label)
        dbox.pack_start(self.date_btn, True, True, 0)
        dbox.pack_start(icon_button("go-next-symbolic", "Следующий день", lambda: self.shift_day(1)),
                        False, False, 0)
        left.pack_start(dbox, False, False, 0)
        self.summary = self._hint("")
        left.pack_start(self.summary, False, False, 0)

        self.store = Gtk.ListStore(str, str, str, int)
        self.tree = Gtk.TreeView(model=self.store)
        for i, (title, xal) in enumerate((("Время", 0.0), ("Длит.", 1.0), ("Размер", 1.0))):
            col = Gtk.TreeViewColumn(title, Gtk.CellRendererText(xalign=xal), text=i)
            col.set_expand(i == 0)
            self.tree.append_column(col)
        self.tree.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        self.tree.get_selection().connect("changed", self._sel_changed)
        self.tree.connect("row-activated", lambda _t, path, _c: self.play_index(path.get_indices()[0]))
        sw = Gtk.ScrolledWindow()
        sw.set_min_content_height(80)
        sw.add(self.tree)
        left.pack_start(sw, True, True, 0)

        rng = Gtk.Box(spacing=4)
        rng.pack_start(Gtk.Label(label="С"), False, False, 0)
        self.t_from = Gtk.Entry(text="08:00", width_chars=6, max_length=5)
        rng.pack_start(self.t_from, False, False, 0)
        rng.pack_start(Gtk.Label(label="по"), False, False, 0)
        self.t_to = Gtk.Entry(text="18:00", width_chars=6, max_length=5)
        rng.pack_start(self.t_to, False, False, 0)
        self.sel_btn = Gtk.Button(label="Выделить")
        self.sel_btn.set_tooltip_text("Выделить записи в этом интервале")
        self.sel_btn.connect("clicked", lambda *_: self.select_range())
        rng.pack_start(self.sel_btn, True, True, 0)
        left.pack_start(rng, False, False, 0)
        self.merge = Gtk.CheckButton(label="Склеить в один файл", active=True)
        left.pack_start(self.merge, False, False, 0)
        self.dl_btn = Gtk.Button(label="Скачать выбранное")
        self.dl_btn.get_style_context().add_class("suggested-action")
        self.dl_btn.connect("clicked", lambda *_: self.download())
        left.pack_start(self.dl_btn, False, False, 0)
        self.pbox = Gtk.Box(spacing=4)
        self.progress = Gtk.ProgressBar(show_text=True, valign=Gtk.Align.CENTER)
        self.pbox.pack_start(self.progress, True, True, 0)
        self.pbox.pack_start(icon_button("process-stop-symbolic", "Отменить скачивание",
                                         lambda: self.cancel and self.cancel.set()), False, False, 0)
        self.pbox.set_no_show_all(True)
        left.pack_start(self.pbox, False, False, 0)
        fbox = Gtk.Box(spacing=4)
        self.folder_label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.folder_label.get_style_context().add_class("hint")
        fbox.pack_start(self.folder_label, True, True, 0)
        fbox.pack_start(icon_button("folder-open-symbolic", "Открыть папку с видео",
                                    lambda: open_folder(self.win, self.win.store.download_dir)), False, False, 0)
        left.pack_start(fbox, False, False, 0)
        self.actions = (self.sel_btn, self.t_from, self.t_to, self.merge)
        self.pack1(left, False, False)

        # ----- правая панель -----
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.timeline = Timeline(self.pick_time)
        right.pack_start(self.timeline, False, False, 0)
        self.player = Gst.ElementFactory.make("playbin")
        self.psink, pw = make_sink()
        self.player.props.video_sink = self.psink
        self.player.connect("source-setup", self._source_setup)
        self.psink.get_static_pad("sink").add_probe(
            Gst.PadProbeType.BUFFER | Gst.PadProbeType.EVENT_DOWNSTREAM, self._on_buf)
        bus = self.player.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_msg)
        vbox = Gtk.EventBox()
        vbox.get_style_context().add_class("video")
        vbox.set_above_child(True)
        ov = Gtk.Overlay()
        ov.add(pw)
        self.cover = black_box()  # прячет кадры до перемотки на нужное время
        self.cover.set_no_show_all(True)
        ov.add_overlay(self.cover)
        self.pstatus = Gtk.Label(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, wrap=True,
                                 justify=Gtk.Justification.CENTER)
        self.pstatus.get_style_context().add_class("status")
        set_status(self.pstatus, "Выберите запись в списке или щёлкните по шкале")
        self.pstatus.set_no_show_all(True)
        self.pstatus.show()
        ov.add_overlay(self.pstatus)
        vbox.add(ov)
        vbox.connect("button-press-event", self._video_click)
        right.pack_start(vbox, True, True, 0)

        ctl = Gtk.Box(spacing=4, margin=6)
        self.controls = [
            icon_button("media-skip-backward-symbolic", "Предыдущая запись", lambda: self.step(-1)),
            icon_button("media-seek-backward-symbolic", "Назад на 30 секунд", lambda: self.jump(-30)),
        ]
        self.play_btn = icon_button("media-playback-start-symbolic", "Пауза / воспроизведение (пробел)", self.toggle)
        self.controls += [self.play_btn,
                          icon_button("media-seek-forward-symbolic", "Вперёд на 30 секунд", lambda: self.jump(30)),
                          icon_button("media-skip-forward-symbolic", "Следующая запись", lambda: self.step(1))]
        for b in self.controls:
            ctl.pack_start(b, False, False, 0)
        self.clock = Gtk.Label(label="--:--:--", width_chars=10)
        ctl.pack_start(self.clock, False, False, 4)
        self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 0.1)
        self.scale.set_draw_value(False)
        self.scale.connect("change-value", self._scale_moved)
        ctl.pack_start(self.scale, True, True, 0)
        self.rate = Gtk.ComboBoxText()
        for r in self.RATES:
            self.rate.append_text(f"{r:g}×")
        self.rate.set_active(0)
        self.rate.set_tooltip_text("Скорость. 1× идёт потоком с регистратора; для ускорения запись сначала "
                                   "скачивается")
        self.rate.connect("changed", lambda *_: self.apply_rate())
        ctl.pack_start(self.rate, False, False, 0)
        self.cont = Gtk.CheckButton(label="Подряд", active=True)
        self.cont.set_tooltip_text("После записи сразу включать следующую")
        ctl.pack_start(self.cont, False, False, 0)
        right.pack_start(ctl, False, False, 0)
        self.pack2(right, True, True)

        self._show_day()
        self.update_folder()
        GLib.timeout_add(250, self._tick)
        self.connect("destroy", lambda *_: self.player.set_state(Gst.State.NULL))

    @staticmethod
    def _hint(text) -> Gtk.Label:
        lab = Gtk.Label(label=text, xalign=0, wrap=True, max_width_chars=36)
        lab.get_style_context().add_class("hint")
        return lab

    # ----- сеанс и камеры -----
    def set_session(self, session):
        self.stop_player()
        self.session = session
        self.records = []
        self.store.clear()
        self.timeline.set_records(self.cal_date(), [])
        cams = session.cameras if session is not None and session.connected else []
        cur = self.camera()
        self.cams = cams
        self._loading = True
        self.cam_combo.remove_all()
        for c in cams:
            self.cam_combo.append(str(c.id), c.name + ("" if c.online else " (не в сети)"))
        if cams:
            keep = cur and any(c.id == cur.id for c in cams)
            self.cam_combo.set_active_id(str(cur.id) if keep else str(cams[0].id))
        self._loading = False
        self._dirty = True
        if not cams:
            self._say("Нет подключения к регистратору" if session is None else "Войдите в регистратор")
            self._set_rights()
            return
        if self.active:
            self.refresh()

    def set_active(self, active: bool):
        """Архив запрашивается у регистратора, только когда вкладка открыта.

        Уходим с вкладки: просмотр останавливается (регистратор не отдаёт лишний поток),
        вернулись: продолжается с того же места.
        """
        was = self.active
        self.active = active
        if was and not active and self.mode is not None:
            t = self._current_time()
            playing = self.player.get_state(0)[1] == Gst.State.PLAYING
            self.resume = (self.cur, t) if (t and playing and 0 <= self.cur < len(self.records)) else None
            self.stop_player()
        if active and self._dirty:
            self.resume = None
            self.refresh()
        elif active and not was and self.resume:
            i, t = self.resume
            self.resume = None
            found = self._index_at(t)
            if found:
                self.play_at(*found)

    def select_camera(self, cid: int):
        self.cam_combo.set_active_id(str(cid))

    def camera(self) -> Camera | None:
        aid = self.cam_combo.get_active_id() if self.cams else None
        return next((c for c in self.cams if str(c.id) == aid), None)

    def cal_date(self) -> dt.date:
        y, m, d = self.cal.get_date()
        return dt.date(y, m + 1, d)

    def set_day(self, d: dt.date):
        self._loading = True
        self.cal.select_month(d.month - 1, d.year)
        self._loading = False
        self.cal.select_day(d.day)  # вызовет _day_changed
        self.mark_month()

    def shift_day(self, n: int):
        self.set_day(self.cal_date() + dt.timedelta(days=n))

    def _month_changed(self):
        self._month_nav = True  # стрелки месяца тоже выбирают день: окно календаря не закрываем
        self.mark_month()

    def _day_clicked(self):
        if self._month_nav:
            self._month_nav = False
        elif self.date_btn.get_active():
            self.date_btn.get_popover().popdown()
        self._day_changed()

    def _day_changed(self):
        self._show_day()
        self.refresh()

    def _show_day(self):
        d = self.cal_date()
        self.date_label.set_text(fmt_day(d))
        self.date_btn.set_tooltip_text(f"{d:%d.%m.%Y}. Выбрать другой день")

    def update_folder(self):
        d = self.win.store.download_dir
        home = os.path.expanduser("~")
        short = "~" + d[len(home):] if d.startswith(home + os.sep) else d
        self.folder_label.set_text(f"Видео сохраняется в {short}")
        self.folder_label.set_tooltip_text(d)

    def _say(self, text, summary=True):
        if summary:
            self.summary.set_text(text)
        set_status(self.pstatus, text)
        self.pstatus.show()

    def _ready(self) -> bool:
        return self.session is not None and self.session.connected and self.camera() is not None

    def _set_rights(self):
        """Кнопки по правам: без прав на архив всё неактивно, без прав на скачивание нет скачивания."""
        s, cam = self.session, self.camera()
        ok = self._ready()
        arch = ok and not s.is_forbidden("archive", cam.id)
        dl = arch and not s.is_forbidden("download", cam.id)
        view = arch and not (s.is_forbidden("playback", cam.id) and s.is_forbidden("download", cam.id))
        for w in self.actions:
            w.set_sensitive(arch)
        self.dl_btn.set_sensitive(dl and self.cancel is None)
        self.dl_btn.set_tooltip_text(None if dl or not arch else "У вашей учётной записи нет прав на скачивание")
        for w in self.controls + [self.scale, self.cont]:
            w.set_sensitive(view)
        self.rate.set_sensitive(dl)
        if arch and not dl:
            self.rate.set_active(0)
        self.date_btn.set_sensitive(arch)

    def _no_archive(self, cam):
        self._say(f"У вашей учётной записи нет доступа к архиву камеры «{cam.name}».")
        self._set_rights()

    def refresh(self):
        self._show_day()
        if not self._ready():
            self._set_rights()
            return
        if not self.active:
            self._dirty = True
            return
        self._dirty = False
        cam, s = self.camera(), self.session
        # Сменили камеру во время просмотра: продолжаем с того же времени, как в iVMS.
        keep = self._current_time() if (self.mode is not None and self.day == self.cal_date()) else None
        self.stop_player()
        self.token += 1
        tok, day = self.token, self.cal_date()
        self.store.clear()
        self.records = []
        self.timeline.set_records(day, [])
        self._set_rights()
        if s.is_forbidden("archive", cam.id):
            self._no_archive(cam)
            return
        self._say("Поиск записей…")

        def fail(e):
            if tok != self.token:
                return
            if isinstance(e, ForbiddenError):
                s.mark_forbidden("archive", cam.id)
                self._no_archive(cam)
                return
            self._say(err_text(e))
            self.win.handle_error(s, e)
        bg(s.nvr.search, cam.id, day, done=lambda recs: tok == self.token and self._got(day, recs, keep), fail=fail)
        self.mark_month()

    def _got(self, day, recs, keep=None):
        self.day, self.records, self.cur = day, recs, -1
        for i, r in enumerate(recs):
            self.store.append([f"{r.start:%H:%M:%S} – {r.end:%H:%M:%S}", fmt_dur(r.seconds),
                               fmt_size(r.size) if r.size else "", i])
        self.timeline.set_records(day, recs)
        if recs:
            self.summary.set_text(self._summary_text())
            set_status(self.pstatus, "Выберите запись в списке или щёлкните по шкале")
        else:
            self._say("За этот день записей нет")
        self.pstatus.show()
        found = self._index_at(keep) if (keep and recs) else None
        if found:
            self.play_at(*found)

    def _summary_text(self) -> str:
        recs = self.records
        total, size = day_overlap(recs, self.day), sum(r.size for r in recs)
        return f"Записей: {len(recs)}, видео {fmt_dur(total)}" + (f", {fmt_size(size)}" if size else "")

    def mark_month(self):
        if not self._ready() or not self.active:
            return
        cam, s = self.camera(), self.session
        if s.is_forbidden("archive", cam.id):
            return
        y, m, _ = self.cal.get_date()
        key = (id(self.session), cam.id, y, m)
        if self._mkey == key:
            return
        self._mkey = key
        self.cal.clear_marks()

        def done(days):
            if self._mkey == key:
                for d in days:
                    self.cal.mark_day(d)

        def fail(_e):
            if self._mkey == key:
                self._mkey = None
        bg(s.nvr.month_days, cam.id, y, m + 1, done=done, fail=fail)

    # ----- выделение -----
    def _sel_changed(self, sel):
        _m, paths = sel.get_selected_rows()
        self.timeline.selected = {p.get_indices()[0] for p in paths}
        self.timeline.queue_draw()

    def selected(self) -> list[int]:
        _m, paths = self.tree.get_selection().get_selected_rows()
        return sorted(p.get_indices()[0] for p in paths)

    def select_range(self):
        def parse(s):
            hh, _, mm = s.strip().replace(".", ":").partition(":")
            return dt.time(int(hh), int(mm or 0))
        try:
            a, b = parse(self.t_from.get_text()), parse(self.t_to.get_text())
        except ValueError:
            self.win.show_info("Время укажите как ЧЧ:ММ, например 08:30", Gtk.MessageType.WARNING)
            return
        ta = dt.datetime.combine(self.day, a)
        tb = dt.datetime.combine(self.day, b) if b > a else dt.datetime.combine(self.day, dt.time(23, 59, 59))
        sel = self.tree.get_selection()
        sel.unselect_all()
        first = None
        for i, r in enumerate(self.records):
            if r.end > ta and r.start < tb:
                sel.select_path(Gtk.TreePath(i))
                first = i if first is None else first
        if first is None:
            self.win.show_info("В этом интервале записей нет", autohide=True)
        else:
            self.tree.scroll_to_cell(Gtk.TreePath(first), None, False, 0, 0)

    # ----- плеер: общее -----
    def stop_player(self, text=None):
        t = self._current_time() if self.mode is not None else None
        if t is not None:
            self._last_time = t  # чтобы ±30 с работали и после остановки
        self.play_token += 1
        self.player.set_state(Gst.State.NULL)
        self.mode = None
        self.cur = -1
        self.s_base = self.s_first = self.s_last = None
        self.s_target = self.s_now = None
        self.user_paused = self.at_end = False
        self.reconnect_at = None
        self.cover.hide()
        self.clock.set_text("--:--:--")
        self.clock.set_tooltip_text(None)
        self.scale.get_adjustment().configure(0, 0, 1, 1, 5, 0)
        self.timeline.set_cursor(None, -1)
        self._set_play_icon(False)
        if text:
            self._say(text, summary=False)

    def _rate(self) -> float:
        return self.RATES[max(0, self.rate.get_active())]

    def _index_at(self, when: dt.datetime) -> tuple[int, dt.datetime] | None:
        """Запись, в которой находится момент when, или ближайшая следующая (и момент её начала)."""
        for i, r in enumerate(self.records):
            if r.start <= when < r.end:
                return i, when
            if r.start > when:
                return i, r.start
        return None

    def _index_back(self, when: dt.datetime, back: float) -> tuple[int, dt.datetime] | None:
        """Для перемотки назад: запись с моментом when, а если он в разрыве, конец предыдущей записи."""
        if not self.records:
            return None
        prev = None
        for i, r in enumerate(self.records):
            if r.start <= when < r.end:
                return i, when
            if r.end <= when:
                prev = i
        if prev is None:
            return 0, self.records[0].start
        r = self.records[prev]
        return prev, max(r.start, r.end - dt.timedelta(seconds=back))

    def pick_time(self, t: float):
        if not self.records:
            return
        when = dt.datetime.combine(self.day, dt.time()) + dt.timedelta(seconds=t)
        found = self._index_at(when)
        if found is None:
            self.win.show_info("После этого времени записей нет", autohide=True)
            return
        self.play_at(*found)

    def play_index(self, i: int, offset: float = 0.0):
        if 0 <= i < len(self.records):
            self.play_at(i, self.records[i].start + dt.timedelta(seconds=offset))

    def play_at(self, i: int, when: dt.datetime):
        """Смотреть запись i с момента when: потоком (1×) или файлом (ускорение / нет прав на поток)."""
        if not (0 <= i < len(self.records)) or not self._ready():
            return
        s, cam = self.session, self.camera()
        no_stream = s.is_forbidden("playback", cam.id)
        no_file = s.is_forbidden("download", cam.id)
        if no_stream and no_file:
            self._say("Нет прав на просмотр архива этой камеры", summary=False)
            return
        # Регистратор без перемотки потока (не ONVIF) начал бы с начала файла: тогда точнее файлом.
        use_file = not no_file and (self._rate() != 1.0 or no_stream or s.no_onvif)
        self.cur = i
        self.at_end = False
        self.reconnect_at = None
        path = Gtk.TreePath(i)
        self.tree.scroll_to_cell(path, None, False, 0, 0)
        self.tree.set_cursor(path, None, False)
        self.timeline.set_cursor(self.timeline.secs(when), i)
        self.clock.set_text(f"{when:%H:%M:%S}")
        if use_file:
            self._play_file_at(i, (when - self.records[i].start).total_seconds())
        elif self.mode == "stream" and self.s_onvif and self._seg_abs:
            self._stream_seek(when)  # тот же сеанс: просто перематываем, без переподключения
        else:
            self._play_stream(when)

    def _source_setup(self, _pb, src):
        nvr = self.session.nvr if self.session else None
        if nvr is None or self.mode != "stream":
            return
        # Логин и пароль передаются свойствами, а не в адресе потока.
        if src.find_property("user-id"):
            src.props.user_id = nvr.user
            src.props.user_pw = nvr.password
        if src.find_property("protocols"):
            Gst.util_set_object_arg(src, "protocols", "tcp")
        if src.find_property("latency"):
            src.props.latency = 500
        # ONVIF: перемотка по абсолютному времени (Range: clock) и время кадра в самом потоке.
        self.s_onvif = bool(src.find_property("onvif-mode")) and not self.session.no_onvif
        if self.s_onvif:
            src.props.onvif_mode = True

    def _on_buf(self, _pad, info):
        # поток GStreamer: запоминаем метки времени кадров и границы отрезков
        if info.type & Gst.PadProbeType.EVENT_DOWNSTREAM:
            ev = info.get_event()
            if ev.type == Gst.EventType.SEGMENT:
                seg = ev.parse_segment()
                self._seg = seg
                self._seg_abs = seg.start > ABS
                self._ever_abs = self._ever_abs or self._seg_abs
            return Gst.PadProbeReturn.OK
        pts = info.get_buffer().pts
        self._buf_mono = GLib.get_monotonic_time() / 1e6
        if pts != Gst.CLOCK_TIME_NONE:
            if self.s_first is None:
                self.s_first = pts
            self.s_last = pts
            # ONVIF: отрезок начинается с абсолютного времени, и метка кадра сама абсолютная
            # (поле time отрезка регистратор при первом запуске ставит в 0, поэтому берём pts).
            if self._seg_abs and pts > ABS:
                self.s_now = E1900 + dt.timedelta(microseconds=pts // 1000)
        return Gst.PadProbeReturn.OK

    # ----- поток RTSP -----
    def _play_stream(self, when: dt.datetime):
        s, cam = self.session, self.camera()
        nvr = s.nvr
        self.play_token += 1
        tok = self.play_token
        self.player.set_state(Gst.State.NULL)
        self.mode = "stream"
        self.pending = None
        self.user_paused = False
        # Без ONVIF регистратор начинает с начала файла записи: часы считаем от него.
        base = self.records[self.cur].start if (s.no_onvif and 0 <= self.cur < len(self.records)) else when
        self.s_base, self.s_first, self.s_last = base, None, None
        self.s_target, self.s_now, self._seg, self._seg_abs = when, None, None, False
        self._buf_mono = 0.0
        self._s_seek_sent = False
        self._ever_abs = False
        self._s_started = GLib.get_monotonic_time() / 1e6
        self.cover.show()
        self.clock.set_tooltip_text("Время приблизительное (± несколько секунд): регистратор начинает показ с "
                                    "ближайшего опорного кадра. Точное время напечатано на самом кадре.")
        set_status(self.pstatus, f"Подключение к архиву, {when:%H:%M:%S}…")
        self.pstatus.show()
        self._set_play_icon(True)
        url = nvr.rtsp_playback(cam.id, when)

        def go(_=None):
            if tok == self.play_token:
                self.player.props.uri = url
                self.player.set_state(Gst.State.PLAYING)

        def fail(e):
            if tok != self.play_token:
                return
            if isinstance(e, ForbiddenError):
                self._no_stream_rights(self.cur)
            elif not s.is_forbidden("download", cam.id) and 0 <= self.cur < len(self.records):
                # Поток недоступен (порт RTSP закрыт и т.п.), но скачивать можно: показываем файлом.
                self.win.show_info(f"Поток архива недоступен ({str(e).rstrip('.')}). Запись скачивается "
                                   "для просмотра.", Gtk.MessageType.WARNING, autohide=True)
                self._play_file_at(self.cur, (when - self.records[self.cur].start).total_seconds())
            elif isinstance(e, NvrError):
                self.stop_player(f"Не удалось открыть архив. {e}")
        if url.split("?", 1)[0] in s.rtsp_ok:
            go()
        else:
            # Одна своя проверка доступа: при отказе GStreamer повторяет пароль много раз.
            bg(s.check_rtsp, url, "playback", done=go, fail=fail)

    def _stream_seek(self, when: dt.datetime):
        self.s_target, self.s_now, self._seg_abs = when, None, False
        self.user_paused = False
        self._buf_mono = GLib.get_monotonic_time() / 1e6
        self.player.seek(1.0, Gst.Format.TIME, Gst.SeekFlags.FLUSH, Gst.SeekType.SET, onvif_ns(when),
                         Gst.SeekType.NONE, -1)
        self.player.set_state(Gst.State.PLAYING)
        self._set_play_icon(True)

    def stream_time(self) -> dt.datetime | None:
        if self.s_onvif:
            return self.s_now if (self.s_now is not None and self._seg_abs) else None
        if self.s_base is None or self.s_first is None or self.s_last is None:
            return None
        return self.s_base + dt.timedelta(microseconds=(self.s_last - self.s_first) // 1000)

    def _watchdog(self) -> bool:
        """Видео перестало идти (обрыв без сообщения об ошибке): переподключаемся с того же места."""
        if self.user_paused or self.at_end or self.reconnect_at is not None or self._seek_id:
            return False
        now = GLib.get_monotonic_time() / 1e6
        last = self._buf_mono or self._s_started
        if now - last < (20 if self.s_first is None else 10):
            return False
        when = self.stream_time() or self.s_target or self.s_base
        self._dead_restarts += 1
        if self._dead_restarts > 3 or when is None:
            self.stop_player("Регистратор перестал отдавать видео. Проверьте связь и нажмите ▶")
            return True
        log.info("архив: нет кадров %.0f с, переподключение", now - last)
        found = self._index_at(when)
        if found:
            self._seg_abs = False
            self.play_at(*found)
            set_status(self.pstatus, "Связь прервалась, подключаюсь заново…")
            self.pstatus.show()
        return True

    def _stream_tick(self):
        if self._watchdog():
            return
        t = self.stream_time()
        if t is None:
            waited = GLib.get_monotonic_time() / 1e6 - self._s_started
            if self.s_onvif and self.s_first is not None and waited > 8 and not self._ever_abs \
                    and not self.session.no_onvif:
                # Видео идёт, а абсолютного времени нет: регистратор не умеет ONVIF-перемотку.
                log.info("архив: регистратор не поддерживает перемотку ONVIF")
                self.session.no_onvif = True
                when = self.s_target or self.s_base
                found = self._index_at(when) if when else None
                self.stop_player()
                if found:
                    self.play_at(*found)
            return
        if self.pstatus.get_visible() or self.cover.get_visible():
            self.pstatus.hide()
            self.cover.hide()
            self._s_retried = False
        self._dead_restarts = 0
        self.s_target = None
        r = self.records[self.cur]
        if not (r.start - dt.timedelta(seconds=GAP) <= t < r.end):
            if not self.cont.get_active() and t >= r.end:
                self._end_of_record(r)
                return
            found = self._index_at(t)
            if found is None:
                self.stop_player("Записи за этот день закончились")
                return
            if self.s_onvif and (found[1] - t).total_seconds() > 1:
                # Время попало в разрыв между записями: регистратор его пропустил, а метки
                # кадров идут подряд. Перематываем на начало следующей записи, чтобы часы были верны.
                self.cur = found[0]
                self.tree.set_cursor(Gtk.TreePath(self.cur), None, False)
                self._stream_seek(found[1])
                return
            if found[0] != self.cur:
                if not self.s_onvif:
                    # Без ONVIF регистратор пропускает разрывы, а часы считаем сами: сдвигаем их.
                    gap = self.records[found[0]].start - r.end
                    if gap.total_seconds() > 0:
                        self.s_base += gap
                    t = self.stream_time()
                self.cur = found[0]
                self.tree.set_cursor(Gtk.TreePath(self.cur), None, False)
            r = self.records[self.cur]
        # Регистратор начинает показ с ближайшего опорного кадра, поэтому время приблизительное.
        self.clock.set_text(f"≈{t:%H:%M:%S}")
        self.scale.get_adjustment().configure(max(0.0, (t - r.start).total_seconds()), 0, max(1.0, r.seconds),
                                              1, 30, 0)
        self.timeline.set_cursor(self.timeline.secs(t), self.cur)

    def _end_of_record(self, r: Record):
        """«Подряд» выключено: стоп в конце записи; ▶ включит следующую."""
        self.player.set_state(Gst.State.PAUSED)
        self._set_play_icon(False)
        self.at_end = True
        self.clock.set_text(f"{r.end:%H:%M:%S}")
        self.timeline.set_cursor(self.timeline.secs(r.end), self.cur)
        set_status(self.pstatus, "Запись закончилась. ▶: следующая запись")
        self.pstatus.show()

    def _current_time(self) -> dt.datetime | None:
        if self.mode == "stream":
            return self.stream_time() or self.s_target or self.s_base
        if self.mode == "file" and 0 <= self.cur < len(self.records):
            pos = self._position()
            return self.records[self.cur].start + dt.timedelta(seconds=pos or 0)
        return None

    def jump(self, secs: int):
        t = self._current_time() or self._last_time
        if t is None or not self.records:
            return
        target = t + dt.timedelta(seconds=secs)
        found = self._index_back(target, -secs) if secs < 0 else self._index_at(target)
        if found is None:
            self.win.show_info("Дальше записей нет", autohide=True)
            return
        self.play_at(*found)

    # ----- файл -----
    def _play_file_at(self, i: int, offset: float):
        s, cam = self.session, self.camera()
        self.play_token += 1
        tok = self.play_token
        self.player.set_state(Gst.State.NULL)
        self.mode = "file"
        self.user_paused = False
        self.cover.hide()
        r = self.records[i]
        if not os.path.exists(self.win.archive.cache_path(s.device.id, r)):
            size = f" ({fmt_size(r.size)})" if r.size else ""
            set_status(self.pstatus, f"Загрузка записи {r.start:%H:%M:%S}{size} для ускоренного просмотра…"
                                  if self._rate() != 1.0 else f"Загрузка записи {r.start:%H:%M:%S}{size}…")
            self.pstatus.show()

        def prog(f):
            ui(lambda: tok == self.play_token and set_status(self.pstatus,
                f"Загрузка записи {r.start:%H:%M:%S}: {int(f * 100)}%"))

        def fail(e):
            if tok != self.play_token:
                return
            if isinstance(e, ForbiddenError):
                s.mark_forbidden("download", cam.id)
                self._set_rights()
                if not s.is_forbidden("playback", cam.id):
                    if s.no_onvif:
                        self.win.show_info(f"Этот регистратор не перематывает видео внутри записи, а скачивать "
                                           f"вашей учётной записи нельзя: запись показывается с начала, "
                                           f"с {r.start:%H:%M:%S}.", Gtk.MessageType.WARNING)
                    else:
                        self.win.show_info("Нет прав на скачивание: архив будет показан потоком, с обычной "
                                           "скоростью.", Gtk.MessageType.WARNING, autohide=True)
                    self.play_at(i, r.start + dt.timedelta(seconds=offset))
                else:
                    self._say("Нет прав на просмотр архива этой камеры", summary=False)
                return
            self._say(err_text(e), summary=False)
            if isinstance(e, AuthError):
                self.win.handle_error(s, e)
        bg(self.win.archive.ensure_mp4, s.nvr, s.device.id, r, prog,
           done=lambda p: tok == self.play_token and self._play_file(p, offset), fail=fail)

    def _play_file(self, path, offset):
        self.pstatus.hide()
        self.player.set_state(Gst.State.NULL)
        self.player.props.uri = Gst.filename_to_uri(path)
        rate = self._rate()
        self.pending = (offset, rate) if (offset > 0.5 or rate != 1.0) else None
        self.player.set_state(Gst.State.PLAYING)
        self._set_play_icon(True)

    def _on_msg(self, _bus, msg):
        t = msg.type
        if t == Gst.MessageType.ASYNC_DONE and self.mode == "stream" and self.s_onvif and \
                self.s_target is not None and not self._seg_abs and not self._s_seek_sent:
            # Регистратор начал с начала файла записи: перематываем на нужный момент (Range: clock).
            self._s_seek_sent = True
            self.player.seek(1.0, Gst.Format.TIME, Gst.SeekFlags.FLUSH, Gst.SeekType.SET,
                             onvif_ns(self.s_target), Gst.SeekType.NONE, -1)
        elif t == Gst.MessageType.ASYNC_DONE and self.pending:
            offset, rate = self.pending
            self.pending = None
            self._seek(offset, rate)
        elif t == Gst.MessageType.EOS:
            if self.mode == "stream":
                self.stop_player("Записи закончились")
            elif self.cont.get_active() and self.cur + 1 < len(self.records):
                self.play_index(self.cur + 1)
            else:
                self.player.set_state(Gst.State.PAUSED)
                self._set_play_icon(False)
                self.at_end = True
        elif t == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            log.info("архив (%s): %s (%s)", self.mode, err.message, (dbg or "").splitlines()[:1])
            mode, when = self.mode, self._current_time()
            self.player.set_state(Gst.State.NULL)
            self._set_play_icon(False)
            self._seg_abs = self._s_seek_sent = False  # старый сеанс закрыт: дальше только новое подключение
            text = f"{err.message or ''} {dbg or ''}"
            if mode == "stream" and (err.matches(Gst.ResourceError.quark(), Gst.ResourceError.NOT_AUTHORIZED)
                                     or "nauthorized" in text or "orbidden" in text):
                self._stream_unauthorized()
            elif mode == "stream" and when is not None and self.user_paused:
                # Пауза долгая, регистратор закрыл сеанс: сами не включаем, ждём ▶.
                self.reconnect_at = when
                self.mode = None
                set_status(self.pstatus, "Регистратор закрыл соединение во время паузы. ▶: продолжить")
                self.pstatus.show()
            elif mode == "stream" and when is not None and not self._s_retried:
                self._s_retried = True
                found = self._index_at(when)
                if found:
                    self.play_at(*found)
            else:
                why = gst_text(err)
                if mode == "stream" and why == "Камера не отвечает":
                    why = ("Регистратор не отдаёт видео архива. Проверьте порт видео (RTSP) в карточке "
                           "регистратора: меню → «Регистраторы…»")
                self.mode = None
                self._say(f"Не удалось воспроизвести. {why}.", summary=False)

    def _no_stream_rights(self, i: int):
        s, cam = self.session, self.camera()
        s.mark_forbidden("playback", cam.id)
        self._set_rights()
        if not s.is_forbidden("download", cam.id) and 0 <= i < len(self.records):
            self._say("Поток архива недоступен этой учётной записи: запись скачивается для просмотра…",
                      summary=False)
            self._play_file_at(i, 0)
        else:
            self.stop_player("Нет прав на просмотр архива этой камеры")

    def _stream_unauthorized(self):
        """RTSP 401 на архиве: одна контрольная проверка входа, затем «нет прав на просмотр потоком»."""
        s, cam, i = self.session, self.camera(), self.cur
        if s is None or not s.connected or cam is None:
            return
        bg(s.nvr.user_check, done=lambda _: self._no_stream_rights(i), fail=lambda e: self.win.handle_error(s, e))

    def _seek(self, pos: float, rate: float):
        self.player.seek(rate, Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
                         Gst.SeekType.SET, int(pos * Gst.SECOND), Gst.SeekType.NONE, -1)

    def _position(self):
        ok, pos = self.player.query_position(Gst.Format.TIME)
        return pos / Gst.SECOND if ok else None

    def apply_rate(self):
        rate = self._rate()
        if self.mode == "file":
            pos = self._position()
            if pos is not None:
                self._seek(pos, rate)
        elif self.mode == "stream" and rate != 1.0:
            # Поток регистратор не ускоряет: переходим на скачанный файл с того же места.
            t = self._current_time()
            found = self._index_at(t) if t else None
            if found:
                self.play_at(*found)

    def toggle(self):
        if self.at_end:
            self.at_end = False
            self.step(1)
            return
        if self.reconnect_at is not None:
            found = self._index_at(self.reconnect_at)
            self.reconnect_at = None
            if found:
                set_status(self.pstatus, "Подключаюсь заново…")
                self.play_at(*found)
            return
        _r, state, _p = self.player.get_state(0)
        if state == Gst.State.PLAYING:
            self.player.set_state(Gst.State.PAUSED)
            self.user_paused = True
            self._paused_mono = GLib.get_monotonic_time() / 1e6
            self._set_play_icon(False)
        elif self.mode == "stream" and self.user_paused and GLib.get_monotonic_time() / 1e6 - self._paused_mono > 15:
            # После долгой паузы регистратор мог молча закрыть сеанс: подключаемся заново с того же места.
            when = self._current_time()
            found = self._index_at(when) if when else None
            if found:
                self._seg_abs = False
                self.play_at(*found)
        elif self.mode is not None:
            self.player.set_state(Gst.State.PLAYING)
            self.user_paused = False
            self._set_play_icon(True)
        elif self.records:
            sel = self.selected()
            if self._last_time and not sel:
                found = self._index_at(self._last_time)
                if found:
                    self.play_at(*found)
                    return
            self.play_index(sel[0] if sel else 0)

    def step(self, d):
        if self.records:
            self.play_index(min(max(0, (self.cur if self.cur >= 0 else 0) + d), len(self.records) - 1))

    def _set_play_icon(self, playing):
        name = "media-playback-pause-symbolic" if playing else "media-playback-start-symbolic"
        self.play_btn.set_image(Gtk.Image.new_from_icon_name(name, Gtk.IconSize.BUTTON))

    def _scale_moved(self, _s, _scroll, value):
        if self.mode == "file":
            self._seek(max(0.0, value), self._rate())
        elif self.mode == "stream" and 0 <= self.cur < len(self.records):
            # Перематываем, когда ползунок отпустили (не на каждое движение).
            if self._seek_id:
                GLib.source_remove(self._seek_id)
            i, when = self.cur, self.records[self.cur].start + dt.timedelta(seconds=max(0.0, value))
            self.clock.set_text(f"{when:%H:%M:%S}")

            def go():
                self._seek_id = 0
                self.play_at(i, when)
                return False
            self._seek_id = GLib.timeout_add(400, go)
        return False

    def _tick(self):
        if self.mode == "stream" and 0 <= self.cur < len(self.records):
            if not self._seek_id and not self.at_end and self.reconnect_at is None:
                self._stream_tick()
        elif self.mode == "file" and 0 <= self.cur < len(self.records):
            pos = self._position()
            ok, dur = self.player.query_duration(Gst.Format.TIME)
            if pos is not None:
                r = self.records[self.cur]
                if ok and dur > 0:
                    self.scale.get_adjustment().configure(pos, 0, dur / Gst.SECOND, 1, 5, 0)
                self.clock.set_text(f"{r.start + dt.timedelta(seconds=pos):%H:%M:%S}")
                self.timeline.set_cursor(self.timeline.secs(r.start) + pos, self.cur)
        return True

    def _video_click(self, _w, ev):
        if is_double(ev):
            self.win.toggle_fullscreen()
        elif ev.button == 1:
            self.toggle()
        return True

    # ----- скачивание -----
    def download(self):
        if self.cancel is not None or not self._ready():
            return
        idx = self.selected()
        if not idx:
            self.win.show_info("Выделите записи в списке (Shift/Ctrl + щелчок) или задайте интервал "
                               "и нажмите «Выделить»")
            return
        if not shutil.which("ffmpeg"):
            self.win.show_info("Для скачивания нужен ffmpeg: sudo apt install ffmpeg", Gtk.MessageType.ERROR)
            return
        s, cam = self.session, self.camera()
        recs = [self.records[i] for i in idx]
        folder = self.win.store.download_dir
        os.makedirs(folder, exist_ok=True)
        self.cancel = threading.Event()
        self.dl_btn.set_sensitive(False)
        self.pbox.show_all()
        self.progress.set_fraction(0)
        self.progress.set_text("Скачивание…")

        def fail(e):
            self._dl_end()
            if isinstance(e, ForbiddenError):
                s.mark_forbidden("download", cam.id)
                self._set_rights()
            if str(e) == "Отменено":
                self.win.show_info("Скачивание отменено", autohide=True)
                return
            self.win.handle_error(s, e)
        merge = self.merge.get_active()
        holes = gaps(recs) if merge else []
        bg(self._dl_job, s.nvr, s.device.id, recs, merge, folder,
           safe_name(f"{s.device.name}_{cam.name}"), self.cancel,
           done=lambda outs: self._dl_done(outs, holes), fail=fail)

    def _dl_job(self, nvr, dev_id, recs, merge, folder, name, cancel):
        total = sum(r.size or 1 for r in recs) or 1
        done, files = 0, []
        for n, r in enumerate(recs, 1):
            text = f"Запись {n} из {len(recs)}"

            def prog(f, d=done, r=r, text=text):
                ui(self._dl_prog, (d + f * (r.size or 1)) / total, text)
            files.append(self.win.archive.ensure_mp4(nvr, dev_id, r, prog, cancel))
            done += r.size or 1
            ui(self._dl_prog, done / total, text)
            if cancel.is_set():
                raise NvrError("Отменено")
        if merge and len(files) > 1:
            ui(self._dl_prog, 1.0, "Склеиваю в один файл…")
            out = unique_path(os.path.join(
                folder, f"{name}_{recs[0].start:%Y-%m-%d_%H-%M-%S}_{recs[-1].end:%H-%M-%S}.mp4"))
            Archive.concat(files, out)
            return [out]
        outs = []
        for r, f in zip(recs, files):
            out = unique_path(os.path.join(folder, f"{name}_{r.start:%Y-%m-%d_%H-%M-%S}.mp4"))
            shutil.copyfile(f, out)
            outs.append(out)
        return outs

    def _dl_prog(self, frac, text):
        self.progress.set_fraction(min(1.0, frac))
        self.progress.set_text(f"{text}: {int(frac * 100)}%")

    def _dl_end(self):
        self.cancel = None
        self.pbox.hide()
        self._set_rights()

    def _dl_done(self, outs, holes):
        self._dl_end()
        what = os.path.basename(outs[0]) if len(outs) == 1 else f"{len(outs)} файлов"
        text = f"Сохранено: {what}"
        if holes:
            first = holes[0]
            text += (f"\nВ записи есть пропуски ({len(holes)}), например {first[0]:%H:%M:%S}–{first[1]:%H:%M:%S}: "
                     "в файле они идут встык, время на видео перескакивает.")
        self.win.show_info(text, folder=os.path.dirname(outs[0]))
