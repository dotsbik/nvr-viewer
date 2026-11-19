"""Общие помощники интерфейса."""
from __future__ import annotations

import os
import re
import threading

from gi.repository import Gdk, GLib, Gst, Gtk, Pango

from ..log import log, redact

CSS = b"""
.tile { background: #000; }
.camname { background: rgba(0,0,0,0.6); color: #fff; padding: 2px 8px; border-bottom-right-radius: 6px; }
.status { color: #bbb; background: rgba(0,0,0,0.65); padding: 4px 10px; border-radius: 6px; }
.video { background: #000; }
.hint { color: alpha(currentColor, 0.65); }
.warn { color: #c77c02; }
.err { color: #d93025; }
.ok { color: #1e8e3e; }
"""


def ui(fn, *args):
    """Выполнить fn в главном потоке GTK."""
    def once():
        try:
            fn(*args)
        except Exception:  # noqa: BLE001
            log.exception("ошибка в обработчике интерфейса")
        return False
    GLib.idle_add(once)


def bg(fn, *args, done=None, fail=None):
    """Выполнить fn в фоновом потоке, результат передать в главный поток."""
    def run():
        try:
            res = fn(*args)
        except Exception as e:  # noqa: BLE001
            if not hasattr(e, "op") and type(e).__module__ != "nvrview.isapi":
                log.exception("фоновая операция")
            if fail:
                ui(fail, e)
        else:
            if done:
                ui(done, res)
    threading.Thread(target=run, daemon=True).start()


def err_text(e) -> str:
    return redact(e) or type(e).__name__


def is_double(ev) -> bool:
    return ev.type == getattr(Gdk.EventType, "DOUBLE_BUTTON_PRESS", Gdk.EventType._2BUTTON_PRESS)


def fmt_size(n: int) -> str:
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.1f} ГБ".replace(".", ",")
    return f"{n / 1024 ** 2:.1f} МБ".replace(".", ",")


def fmt_dur(sec: float) -> str:
    sec = int(round(sec))
    h, m, s = sec // 3600, sec // 60 % 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def safe_name(s: str) -> str:
    """Имя файла из названия камеры: читаемое, с пробелами, но без опасных символов."""
    s = "".join("_" if (c in '/\\:*?"<>|' or ord(c) < 32) else c for c in s)
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"_+", "_", s).strip(" ._")
    return s[:100] or "camera"


def unique_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 2
    while os.path.exists(f"{base} ({i}){ext}"):
        i += 1
    return f"{base} ({i}){ext}"


def make_sink():
    sink = Gst.ElementFactory.make("gtksink")
    if sink is None:
        raise RuntimeError("Нет модуля gtksink: установите пакет gstreamer1.0-gtk3")
    return sink, sink.props.widget


def pictures_dir() -> str:
    d = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_PICTURES) or os.path.expanduser("~")
    return os.path.join(d, "Регистратор")


def open_folder(parent, folder: str):
    os.makedirs(folder, exist_ok=True)
    Gtk.show_uri_on_window(parent, GLib.filename_to_uri(folder), Gdk.CURRENT_TIME)


def black_box() -> Gtk.DrawingArea:
    """Чёрный прямоугольник: пустая ячейка сетки, камера без видео."""
    a = Gtk.DrawingArea()

    def draw(_w, cr):
        cr.set_source_rgb(0, 0, 0)
        cr.paint()
        return False
    a.connect("draw", draw)
    return a


def set_status(label: Gtk.Label, text: str):
    """Надпись поверх видео: светлый текст на тёмной подложке, читается на любом кадре."""
    if not text:
        label.set_text("")
        return
    label.set_markup('<span background="#000000" bgalpha="78%" foreground="#ffffff">'
                     + "\n".join(f" {GLib.markup_escape_text(x)} " for x in text.split("\n")) + "</span>")


def plural(n: int, one: str, few: str, many: str) -> str:
    """3 файла, 5 файлов, 21 файл."""
    m10, m100 = n % 10, n % 100
    w = one if m10 == 1 and m100 != 11 else few if 2 <= m10 <= 4 and not 12 <= m100 <= 14 else many
    return f"{n} {w}"


def ellipsize_combo(combo: Gtk.ComboBox, chars: int = 30):
    """Длинные названия в выпадающем списке обрезаются «…», а не растягивают окно."""
    for cell in combo.get_cells():
        if isinstance(cell, Gtk.CellRendererText):
            cell.set_property("ellipsize", Pango.EllipsizeMode.END)
            cell.set_property("max-width-chars", chars)


def gst_text(err) -> str:
    """Понятный текст ошибки видеопотока. Сырой текст GStreamer пишется в журнал."""
    msg = (getattr(err, "message", "") or "").lower()
    if err.matches(Gst.ResourceError.quark(), Gst.ResourceError.NOT_FOUND) or "not found" in msg:
        return "Регистратор не нашёл этот поток"
    if err.matches(Gst.ResourceError.quark(), Gst.ResourceError.OPEN_READ_WRITE) or \
            err.matches(Gst.ResourceError.quark(), Gst.ResourceError.OPEN_READ) or "timeout" in msg or \
            "could not" in msg or "connect" in msg:
        return "Камера не отвечает"
    if err.matches(Gst.StreamError.quark(), Gst.StreamError.DECODE) or \
            err.matches(Gst.StreamError.quark(), Gst.StreamError.CODEC_NOT_FOUND) or "decod" in msg:
        return "Не удалось показать видео: нет нужного декодера (пакет gstreamer1.0-libav)"
    return "Видео прервалось"


def icon_button(icon: str, tip: str, cb=None) -> Gtk.Button:
    b = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
    b.set_tooltip_text(tip)
    if cb:
        b.connect("clicked", lambda *_: cb())
    return b
