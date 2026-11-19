"""Главное окно: выбор регистратора, вход/выход, вкладки «Онлайн» и «Архив»."""
from __future__ import annotations

import configparser
import datetime as dt
import os

from gi.repository import Gdk, GLib, Gtk, Pango

from .. import APP_NAME, __version__
from ..devices import Device, DeviceStore
from ..isapi import AuthError, CertError, ForbiddenError, InsecureAuthError, probe
from ..log import log
from ..media import Archive
from ..log import log_path
from ..paths import config_dir
from ..secrets import open_store
from ..session import LoginGuard, Session
from .archive import ArchivePage
from .common import bg, ellipsize_combo, err_text, fmt_size, open_folder, plural, ui
from .dialogs import DevicesDialog, LoginDialog
from .live import LivePage

R_LOGIN, R_FOLDER, R_DEVICES = 1, 2, 3


class Window(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=APP_NAME)
        self.set_default_size(1280, 800)
        self.set_icon_name("local.nvrview")
        self.store = DeviceStore()
        self.secrets = open_store()
        self.guard = LoginGuard()
        self.archive = Archive()
        self.sessions: dict[str, Session] = {}
        self.session: Session | None = None
        self.is_full = False
        self._login_open = False
        self._info_timer = 0

        # ----- заголовок -----
        hb = Gtk.HeaderBar(show_close_button=True)
        self.set_titlebar(hb)
        self.dev_combo = Gtk.ComboBoxText()
        ellipsize_combo(self.dev_combo, 20)
        self.dev_combo.set_tooltip_text("Регистратор")
        self._dev_handler = self.dev_combo.connect("changed", lambda *_: self._device_chosen())
        hb.pack_start(self.dev_combo)
        self.state_label = Gtk.Label(ellipsize=Pango.EllipsizeMode.END, max_width_chars=24)
        hb.pack_start(self.state_label)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        hb.set_custom_title(Gtk.StackSwitcher(stack=self.stack))
        menu = Gtk.MenuButton(image=Gtk.Image.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.BUTTON))
        menu.set_tooltip_text("Меню")
        menu.set_popover(self._build_menu())
        hb.pack_end(menu)
        full = Gtk.Button.new_from_icon_name("view-fullscreen-symbolic", Gtk.IconSize.BUTTON)
        full.set_tooltip_text("Во весь экран (F11)")
        full.connect("clicked", lambda *_: self.toggle_fullscreen())
        hb.pack_end(full)
        self.login_btn = Gtk.Button(label="Войти")
        self.login_btn.connect("clicked", lambda *_: self.login_or_logout())
        hb.pack_end(self.login_btn)

        # ----- содержимое -----
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.info = Gtk.InfoBar(show_close_button=True)
        self.info_label = Gtk.Label(xalign=0, wrap=True)
        self.info.get_content_area().add(self.info_label)
        self.info.connect("response", self._info_resp)
        self.info.set_no_show_all(True)
        self.info_label.show()
        box.pack_start(self.info, False, False, 0)
        box.pack_start(self.stack, True, True, 0)
        self.add(box)
        self.live = LivePage(self)
        self.stack.add_titled(self.live, "live", "Онлайн")
        self.arch = ArchivePage(self)
        self.stack.add_titled(self.arch, "archive", "Архив")
        self.stack.connect("notify::visible-child-name", lambda *_: self._page_changed())
        self.connect("key-press-event", self._key)
        self.connect("window-state-event", self._wstate)
        self.connect("destroy", lambda *_: self._shutdown())
        self.show_all()
        self.dev_combo.hide()
        self.login_btn.hide()
        GLib.idle_add(self._start)

    # ----- меню -----
    def _build_menu(self) -> Gtk.Popover:
        pop = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin=8)
        items = (("Регистраторы…", self.open_devices), None,
                 ("Забыть сохранённый пароль", self.forget_password),
                 ("Папка для видео…", self.choose_folder),
                 ("Очистить кэш архива", self.clear_cache), None,
                 ("Журнал программы", self.open_log),
                 ("О программе", self.about))
        for it in items:
            if it is None:
                box.pack_start(Gtk.Separator(margin_top=4, margin_bottom=4), False, False, 0)
                continue
            b = Gtk.ModelButton(text=it[0])
            b.connect("clicked", lambda *_, f=it[1]: f())
            box.pack_start(b, False, False, 0)
        box.show_all()
        pop.add(box)
        return pop

    # ----- запуск -----
    def _start(self):
        self._migrate_legacy()
        if not self.store.devices:
            self.show_info("Добавьте регистратор: меню → «Регистраторы…»", devices=True)
            self.open_devices()
        self._fill_devices()
        return False

    def _migrate_legacy(self):
        """Первая версия хранила пароль открытым текстом в config.ini: переносим в брелок и удаляем файл."""
        old = config_dir() / "config.ini"
        if not old.exists():
            return
        try:
            c = configparser.ConfigParser(interpolation=None)
            c.read(old, encoding="utf-8")
            s = c["nvr"] if "nvr" in c else {}
            if s.get("host") and not self.store.devices:
                dev = Device(name="Регистратор", host=s.get("host"), http_port=int(s.get("http_port", 80)),
                             rtsp_port=int(s.get("rtsp_port", 554)), user=s.get("user", "")).validate()
                pw = s.get("password", "")
                if pw and dev.user and self.secrets.persistent:
                    self.secrets.store(dev.id, dev.user, pw, dev.name)
                    dev.remember = True
                self.store.devices.append(dev)
                self.store.current = dev.id
                if s.get("download_dir") and os.path.isabs(s.get("download_dir")):
                    self.store.download_dir = s.get("download_dir")
                self.store.save()
            old.unlink()
            log.info("старые настройки перенесены, файл с открытым паролем удалён")
        except Exception:  # noqa: BLE001
            log.exception("перенос старых настроек")

    def _fill_devices(self):
        self.dev_combo.handler_block(self._dev_handler)
        self.dev_combo.remove_all()
        for d in self.store.devices:
            self.dev_combo.append(d.id, d.name)
        cur = self.store.current if self.store.get(self.store.current) else (
            self.store.devices[0].id if self.store.devices else None)
        if cur:
            self.dev_combo.set_active_id(cur)
        self.dev_combo.handler_unblock(self._dev_handler)
        self.dev_combo.set_visible(bool(self.store.devices))
        if cur and (self.session is None or self.session.device.id != cur):
            self.activate_device(self.store.get(cur))
        elif not cur:
            self.activate_device(None)
        else:
            self._update_header()

    def _device_chosen(self):
        dev = self.store.get(self.dev_combo.get_active_id() or "")
        if dev and (self.session is None or dev.id != self.session.device.id):
            self.activate_device(dev)

    # ----- сеансы -----
    def _session_for(self, dev: Device) -> Session:
        s = self.sessions.get(dev.id)
        if s is None:
            s = Session(dev, self.secrets, self.guard, on_device_changed=lambda _d: ui(self.save_devices))
            self.sessions[dev.id] = s
        else:
            s.device = dev
        return s

    def activate_device(self, dev: Device | None):
        self.live.set_active(False)
        if dev is None:
            self.session = None
            self._show_session()
            return
        self.store.current = dev.id
        self.save_devices()
        s = self._session_for(dev)
        self.session = s
        self.info.hide()
        if s.connected:
            self._show_session()
            return
        self._show_session()
        self.connect_session(s)

    def connect_session(self, s: Session):
        """Вход: сохранённым паролем без вопросов, иначе окно входа."""
        if s.saved_password() is None:
            if s.device.https and s.device.cert_sha256:
                # Сертификат мог смениться: проверяем без пароля, до окна входа.
                def checked(res):
                    level, text, _fp = res
                    if s is self.session and level == "err" and "ертификат" in text:
                        self.show_info(text, Gtk.MessageType.ERROR, devices=True)
                    else:
                        self.open_login(s)
                d = s.device
                bg(probe, d.host, d.http_port, d.https, d.cert_sha256, done=checked,
                   fail=lambda _e: self.open_login(s))
            else:
                self.open_login(s)
            return
        self.state_label.set_text("Подключение…")
        self.login_btn.set_sensitive(False)

        def fail(e):
            self.login_btn.set_sensitive(True)
            self._update_header()
            if s is self.session:
                self.handle_error(s, e)
                if isinstance(e, AuthError) and not e.locked and not s.guard.wait_left(s.device.id):
                    self.open_login(s, "Сохранённый пароль не подошёл и удалён. Введите пароль.")

        def done(_cams):
            self.login_btn.set_sensitive(True)
            self._logged_in(s)
        bg(s.login_saved, done=done, fail=fail)

    def _logged_in(self, s: Session):
        self.save_devices()
        if s is self.session:
            self.info.hide()
            self._show_session()

    def _show_session(self):
        s = self.session
        self.live.set_session(s)
        self.arch.set_session(s)
        self._update_header()
        self._page_changed()

    def _update_header(self):
        s = self.session
        if s is None:
            self.state_label.set_text("")
            self.login_btn.set_visible(False)
            return
        self.login_btn.set_visible(True)
        if s.connected:
            secure = "HTTPS" if s.device.https else "HTTP без шифрования"
            self.state_label.set_markup(f"<small>{GLib.markup_escape_text(s.nvr.user)} · {secure}</small>")
            self.login_btn.set_label("Выйти")
            self.login_btn.set_tooltip_text("Выйти из регистратора: потоки остановятся, пароль "
                                            "уберётся из памяти программы")
        else:
            self.state_label.set_markup("<small>не подключено</small>")
            self.login_btn.set_label("Войти")
            self.login_btn.set_tooltip_text("Войти в регистратор")

    def open_login(self, s: Session, message: str = ""):
        if self._login_open or s is not self.session:
            return
        self._login_open = True

        def done(cams, _err):
            self._login_open = False
            if cams is not None:
                self._logged_in(s)
            else:
                self._update_header()
                self.show_lock(s)
        LoginDialog(self, s, done, message)

    def show_lock(self, s: Session) -> bool:
        """Если вход закрыт после неудач, говорим об этом в главном окне, с временем открытия."""
        wait = s.guard.wait_left(s.device.id)
        if wait and s is self.session:
            at = dt.datetime.now() + dt.timedelta(seconds=wait)
            self.show_info(f"Вход в «{s.device.name}» закрыт после нескольких неудачных попыток. "
                           f"Он откроется в {at:%H:%M} (через {max(1, round(wait / 60))} мин.).",
                           Gtk.MessageType.WARNING)
        return bool(wait)

    def login_or_logout(self):
        s = self.session
        if s is None:
            return
        if s.connected:
            self.live.set_active(False)
            s.logout()
            self._show_session()
            note = (" Пароль остался сохранённым: «Войти» подключит без вопросов. Чтобы войти другим "
                    "пользователем, выберите в меню «Забыть сохранённый пароль».") if s.device.remember else ""
            self.show_info("Вы вышли из регистратора." + note, login=True)
        elif not self.show_lock(s):
            self.connect_session(s)

    def forget_password(self):
        s = self.session
        if s is None:
            return
        s.forget_password()
        self.save_devices()
        self.show_info(f"Сохранённый пароль для «{s.device.name}» удалён из брелока.")

    def save_devices(self):
        try:
            self.store.save()
        except OSError as e:
            self.show_info(f"Не удалось сохранить настройки: {e}", Gtk.MessageType.ERROR)

    # ----- регистраторы -----
    def open_devices(self):
        d = DevicesDialog(self)
        d.run()
        d.destroy()
        self._fill_devices()

    def device_saved(self, dev: Device, new: bool):
        old = self.store.get(dev.id)
        changed = old is not None and (old.host, old.http_port, old.rtsp_port, old.https) != \
            (dev.host, dev.http_port, dev.rtsp_port, dev.https)
        self.store.upsert(dev)
        if new and not self.store.current:
            self.store.current = dev.id
            self.save_devices()
        s = self.sessions.get(dev.id)
        if s is not None:
            if changed or old.cert_sha256 != dev.cert_sha256:
                if s is self.session:
                    self.live.set_active(False)
                s.logout()
            s.device = dev
            if s is self.session:
                self.session = None  # переподключим в _fill_devices

    def device_removed(self, dev: Device):
        s = self.sessions.pop(dev.id, None)
        if s is not None:
            if s is self.session:
                self.live.set_active(False)
                self.session = None
            s.logout(forget=True)
        else:
            try:
                self.secrets.clear(dev.id)
            except Exception:  # noqa: BLE001
                pass
        self.guard.succeeded(dev.id)
        self.archive.clear(dev.id)
        self.store.remove(dev.id)

    # ----- прочее меню -----
    def choose_folder(self):
        d = Gtk.FileChooserDialog(title="Папка для видео", transient_for=self, action=Gtk.FileChooserAction.SELECT_FOLDER)
        d.add_buttons("Отмена", Gtk.ResponseType.CANCEL, "Выбрать", Gtk.ResponseType.OK)
        os.makedirs(self.store.download_dir, exist_ok=True)
        d.set_filename(self.store.download_dir)
        if d.run() == Gtk.ResponseType.OK and d.get_filename():
            self.store.download_dir = d.get_filename()
            self.save_devices()
            self.arch.update_folder()
            self.show_info(f"Видео будет сохраняться в {self.store.download_dir}", autohide=True)
        d.destroy()

    def clear_cache(self):
        # Открытый сейчас файл продолжит играть: удалённый файл доступен, пока он открыт.
        size = self.archive.size()
        n = self.archive.clear()
        if n == 0:
            self.show_info("Кэш архива уже пуст", autohide=True)
        else:
            self.show_info(f"Кэш архива очищен: {plural(n, 'файл', 'файла', 'файлов')}, {fmt_size(size)}",
                           autohide=True)

    def open_log(self):
        p = log_path()
        if p.exists():
            Gtk.show_uri_on_window(self, GLib.filename_to_uri(str(p)), Gdk.CURRENT_TIME)
        else:
            open_folder(self, str(p.parent))

    def about(self):
        d = Gtk.AboutDialog(transient_for=self, modal=True, program_name=APP_NAME, version=__version__,
                            title="О программе",
                            logo_icon_name="local.nvrview",
                            comments="Просмотр камер и архива регистраторов Hikvision/HiWatch.\n"
                                     "Работает только на чтение: на регистраторе ничего не меняет.\n"
                                     "Пароли хранятся в системном брелоке.")
        d.run()
        d.destroy()

    # ----- ошибки и сообщения -----
    def handle_error(self, s: Session | None, e):
        text = err_text(e)
        if isinstance(e, AuthError):
            if s is not None:
                if s is self.session:
                    self.live.set_active(False)
                s.logout()
                if s is self.session:
                    self._show_session()
            self.show_info(text, Gtk.MessageType.ERROR, login=not e.locked)
        elif isinstance(e, ForbiddenError):
            self.show_info(text, Gtk.MessageType.WARNING)
        elif isinstance(e, (CertError, InsecureAuthError)):
            self.show_info(text, Gtk.MessageType.ERROR, devices=True)
        else:
            self.show_info(text, Gtk.MessageType.ERROR)

    def show_info(self, text, kind=Gtk.MessageType.INFO, login=False, folder=None, devices=False,
                  autohide=False):
        """Сообщение над видео. autohide: само исчезает через 8 с (для «сохранено» и т.п.)."""
        if self._info_timer:
            GLib.source_remove(self._info_timer)
            self._info_timer = 0
        if autohide:
            def hide():
                self._info_timer = 0
                self.info.hide()
                return False
            self._info_timer = GLib.timeout_add_seconds(8, hide)
        for b in self.info.get_action_area().get_children():
            b.destroy()
        self._info_folder = folder
        if login:
            self.info.add_button("Войти", R_LOGIN)
        if folder:
            self.info.add_button("Открыть папку", R_FOLDER)
        if devices:
            self.info.add_button("Регистраторы", R_DEVICES)
        self.info.set_message_type(kind)
        self.info_label.set_text(text)
        self.info.show()

    def _info_resp(self, _bar, resp):
        self.info.hide()
        if resp == R_LOGIN and self.session is not None:
            if not self.show_lock(self.session):
                self.connect_session(self.session)
        elif resp == R_FOLDER and self._info_folder:
            open_folder(self, self._info_folder)
        elif resp == R_DEVICES:
            self.open_devices()

    # ----- вкладки, экран, клавиши -----
    def _page_changed(self):
        self.live.set_active(self.stack.get_visible_child_name() == "live")
        self.arch.set_active(self.stack.get_visible_child_name() == "archive")

    def goto_archive(self, cam):
        self.arch.select_camera(cam.id)
        self.stack.set_visible_child_name("archive")

    def toggle_fullscreen(self):
        self.unfullscreen() if self.is_full else self.fullscreen()

    def _wstate(self, _w, ev):
        self.is_full = bool(ev.new_window_state & Gdk.WindowState.FULLSCREEN)

    def _key(self, _w, ev):
        k = ev.keyval
        if k == Gdk.KEY_F11:
            self.toggle_fullscreen()
            return True
        if k == Gdk.KEY_Escape:
            if not (self.stack.get_visible_child_name() == "live" and self.live.close_single()) and self.is_full:
                self.unfullscreen()
            return True
        if k == Gdk.KEY_space and self.stack.get_visible_child_name() == "archive" \
                and not isinstance(self.get_focus(), Gtk.Entry):
            self.arch.toggle()
            return True
        return False

    def _shutdown(self):
        self.live.set_active(False)
        for s in self.sessions.values():
            s.logout()
