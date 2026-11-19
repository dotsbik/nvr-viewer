"""Точка входа приложения."""
from __future__ import annotations

import os
import sys

# Переменные, через которые в процесс можно подмешать чужой код или вывести секреты в журнал.
_SNAP_VARS = ("GTK_PATH", "GTK_EXE_PREFIX", "GTK_IM_MODULE_FILE", "GIO_MODULE_DIR", "GDK_PIXBUF_MODULE_FILE",
              "GDK_PIXBUF_MODULEDIR", "GSETTINGS_SCHEMA_DIR", "LOCPATH")
_DROP_VARS = ("GST_DEBUG", "GST_DEBUG_FILE", "GST_DEBUG_DUMP_DOT_DIR", "GST_PLUGIN_PATH", "GST_PLUGIN_PATH_1_0",
              "GST_PLUGIN_SYSTEM_PATH", "GST_PLUGIN_SYSTEM_PATH_1_0", "PYTHONPATH", "PYTHONSTARTUP",
              "LD_PRELOAD", "LD_LIBRARY_PATH", "http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY")


def _clean_env_or_reexec(script: str):
    """Чистим окружение. Если запущены из snap (терминал VS Code), перезапускаемся с чистым."""
    if os.environ.get("SNAP") and not os.environ.get("NVRVIEW_CLEAN"):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("SNAP") and k not in _SNAP_VARS and k not in _DROP_VARS}
        for k in ("XDG_DATA_DIRS", "XDG_CONFIG_DIRS"):
            orig = os.environ.get(k + "_VSCODE_SNAP_ORIG")
            if orig is not None:
                env[k] = orig
        env["NVRVIEW_CLEAN"] = "1"
        os.execve(sys.executable, [sys.executable, "-I", script] + sys.argv[1:], env)
    for k in _DROP_VARS:
        os.environ.pop(k, None)


def main() -> int:
    os.umask(0o077)  # всё, что создаёт программа (кэш, журнал, видео), доступно только владельцу
    _clean_env_or_reexec(os.path.realpath(sys.argv[0]))

    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    gi.require_version("Gst", "1.0")
    from gi.repository import Gdk, Gst, Gtk

    from . import APP_ID, APP_NAME
    from .log import log, setup
    from .ui.common import CSS, bg

    path = setup()
    log.info("запуск, журнал %s", path)
    # Падение внутри системных библиотек (GStreamer, драйвер видеокарты) не даёт ошибки Python:
    # faulthandler допишет в журнал, где именно это случилось.
    import faulthandler
    global _fault_file
    _fault_file = open(path, "a", encoding="utf-8")
    faulthandler.enable(_fault_file, all_threads=True)
    GLib_set_name(APP_NAME)

    class App(Gtk.Application):
        def __init__(self):
            super().__init__(application_id=APP_ID)
            self.win = None

        def do_startup(self):
            Gtk.Application.do_startup(self)
            Gst.init(None)
            from .ui.common import software_decoding
            off = software_decoding()
            if off:
                log.info("аппаратные декодеры отключены: %s", ", ".join(off))
            prov = Gtk.CssProvider()
            prov.load_from_data(CSS)
            Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), prov,
                                                     Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        def do_activate(self):
            if self.win is None:
                from .media import Archive
                from .ui.window import Window
                self.win = Window(self)
                bg(Archive().clean)
            self.win.present()

    return App().run(sys.argv[:1])


_fault_file = None


def GLib_set_name(name: str):
    from gi.repository import GLib
    GLib.set_application_name(name)
    GLib.set_prgname("local.nvrview")
