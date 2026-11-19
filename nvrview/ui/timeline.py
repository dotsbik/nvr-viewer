"""Шкала дня: отрезки записей, курсор воспроизведения, масштаб колесом, сдвиг перетаскиванием."""
from __future__ import annotations

import datetime as dt
import math

from gi.repository import Gdk, Gtk

from ..isapi import Record

class Timeline(Gtk.DrawingArea):
    DAY = 86400

    def __init__(self, on_pick):
        super().__init__()
        self.on_pick = on_pick
        self.records: list[Record] = []
        self.day = dt.date.today()
        self.cur = -1
        self.selected: set[int] = set()
        self.cursor: float | None = None
        self.hover: float | None = None
        self.v0, self.span = 0.0, float(self.DAY)
        self._drag = None
        self.set_size_request(-1, 58)
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK
                        | Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.SCROLL_MASK
                        | Gdk.EventMask.SMOOTH_SCROLL_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.connect("draw", self._draw)
        self.connect("button-press-event", self._press)
        self.connect("button-release-event", self._release)
        self.connect("motion-notify-event", self._motion)
        self.connect("scroll-event", self._scroll)
        self.connect("leave-notify-event", self._leave)
        self.set_tooltip_text("Щелчок: смотреть с этого места. Колесо: масштаб. Перетаскивание: сдвиг.")

    def secs(self, t: dt.datetime) -> float:
        return (t - dt.datetime.combine(self.day, dt.time())).total_seconds()

    def set_records(self, day, records):
        self.day, self.records = day, records
        self.cur, self.selected, self.cursor = -1, set(), None
        self.v0, self.span = 0.0, float(self.DAY)
        self.queue_draw()

    def set_cursor(self, t: float | None, cur: int):
        self.cursor, self.cur = t, cur
        if t is not None and self._drag is None and not (self.v0 <= t <= self.v0 + self.span):
            self.v0 = min(max(0.0, t - self.span / 2), self.DAY - self.span)
        self.queue_draw()

    def _x2t(self, x):
        w = max(1, self.get_allocated_width())
        return self.v0 + x / w * self.span

    def _clamp(self):
        self.span = min(max(self.span, 600.0), float(self.DAY))
        self.v0 = min(max(self.v0, 0.0), self.DAY - self.span)

    def _draw(self, _w, cr):
        w, h = self.get_allocated_width(), self.get_allocated_height()
        cr.set_source_rgb(0.11, 0.11, 0.12)
        cr.paint()
        top, bh = 20, h - 26
        cr.set_source_rgb(0.18, 0.18, 0.2)
        cr.rectangle(0, top, w, bh)
        cr.fill()
        k = w / self.span
        for i, r in enumerate(self.records):
            s, e = self.secs(r.start), self.secs(r.end)
            if e < self.v0 or s > self.v0 + self.span:
                continue
            x0 = (s - self.v0) * k
            x1 = max(x0 + 1.0, (e - self.v0) * k)
            if i == self.cur:
                cr.set_source_rgb(1.0, 0.75, 0.25)
            elif i in self.selected:
                cr.set_source_rgb(0.55, 0.8, 1.0)
            else:
                cr.set_source_rgb(0.2, 0.5, 0.95)
            cr.rectangle(x0, top, x1 - x0, bh)
            cr.fill()
        # деления
        step = 7200
        for st in (60, 300, 600, 900, 1800, 3600, 7200, 10800):
            if self.span / st <= w / 64:
                step = st
                break
        cr.select_font_face("Sans")
        cr.set_font_size(11)
        t = math.ceil(self.v0 / step) * step
        while t <= self.v0 + self.span:
            x = (t - self.v0) * k
            cr.set_source_rgba(1, 1, 1, 0.25)
            cr.rectangle(int(x), 12, 1, h - 12)
            cr.fill()
            cr.set_source_rgba(1, 1, 1, 0.75)
            cr.move_to(x + 3, 12)
            tt = int(t) % self.DAY if t < self.DAY else self.DAY
            cr.show_text("24:00" if tt == self.DAY else f"{tt // 3600:02d}:{tt // 60 % 60:02d}")
            t += step
        for t, rgb in ((self.hover, (0.8, 0.8, 0.8)), (self.cursor, (1.0, 0.3, 0.3))):
            if t is None or not (self.v0 <= t <= self.v0 + self.span):
                continue
            x = (t - self.v0) * k
            cr.set_source_rgb(*rgb)
            cr.rectangle(x - 1, top - 4, 2, bh + 8)
            cr.fill()
            if rgb[0] < 1:
                it = int(t)
                label = f"{it // 3600:02d}:{it // 60 % 60:02d}:{it % 60:02d}"
                ext = cr.text_extents(label)
                tx = min(max(2, x - ext.width / 2), w - ext.width - 4)
                cr.set_source_rgba(0, 0, 0, 0.8)
                cr.rectangle(tx - 3, h - 15, ext.width + 6, 14)
                cr.fill()
                cr.set_source_rgb(1, 1, 1)
                cr.move_to(tx, h - 4)
                cr.show_text(label)
        return False

    def _press(self, _w, ev):
        if ev.button == 1:
            self._drag = (ev.x, self.v0, False)
        return True

    def _release(self, _w, ev):
        if ev.button == 1 and self._drag is not None:
            moved = self._drag[2]
            self._drag = None
            if not moved:
                self.on_pick(self._x2t(ev.x))
        return True

    def _motion(self, _w, ev):
        if self._drag is not None:
            x0, v0, moved = self._drag
            dx = ev.x - x0
            if abs(dx) > 4 or moved:
                self._drag = (x0, v0, True)
                self.v0 = v0 - dx / max(1, self.get_allocated_width()) * self.span
                self._clamp()
        self.hover = self._x2t(ev.x)
        self.queue_draw()
        return True

    def _leave(self, *_):
        self.hover = None
        self.queue_draw()

    def _scroll(self, _w, ev):
        dy = 0.0
        if ev.direction == Gdk.ScrollDirection.UP:
            dy = -1
        elif ev.direction == Gdk.ScrollDirection.DOWN:
            dy = 1
        elif ev.direction == Gdk.ScrollDirection.SMOOTH:
            dy = ev.delta_y
        if not dy:
            return True
        t = self._x2t(ev.x)
        f = 0.8 ** (-dy)
        frac = ev.x / max(1, self.get_allocated_width())
        self.span *= f
        self._clamp()
        self.v0 = t - frac * self.span
        self._clamp()
        self.queue_draw()
        return True
