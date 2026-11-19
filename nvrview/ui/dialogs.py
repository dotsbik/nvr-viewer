"""Окна входа и списка регистраторов."""
from __future__ import annotations

from gi.repository import GLib, Gtk

from ..devices import Device
from ..isapi import fingerprint_text, probe
from .common import bg, err_text


def _password_entry() -> Gtk.Entry:
    e = Gtk.Entry(visibility=False, input_purpose=Gtk.InputPurpose.PASSWORD, activates_default=True, hexpand=True)
    e.set_icon_from_icon_name(Gtk.EntryIconPosition.SECONDARY, "view-reveal-symbolic")
    e.set_icon_tooltip_text(Gtk.EntryIconPosition.SECONDARY, "Показать / скрыть пароль")
    e.connect("icon-press", lambda en, *_: en.set_visibility(not en.get_visibility()))
    return e


def _note(text: str, cls: str = "hint") -> Gtk.Label:
    lab = Gtk.Label(label=text, xalign=0, wrap=True, max_width_chars=52)
    lab.get_style_context().add_class(cls)
    return lab


# ---------- вход ----------
class LoginDialog(Gtk.Dialog):
    """Вход в регистратор своей учётной записью. Одна попытка на нажатие «Войти»."""

    def __init__(self, win, session, on_done, message: str = ""):
        super().__init__(title=f"Вход: {session.device.name}", transient_for=win, modal=True)
        self.session, self.on_done = session, on_done
        self.set_default_size(420, -1)
        self.add_button("Отмена", Gtk.ResponseType.CANCEL)
        self.ok = self.add_button("Войти", Gtk.ResponseType.OK)
        self.ok.get_style_context().add_class("suggested-action")
        self.set_default_response(Gtk.ResponseType.OK)
        dev = session.device

        g = Gtk.Grid(row_spacing=8, column_spacing=10, margin=16)
        title = Gtk.Label(xalign=0)
        title.set_markup(f"<b>{GLib.markup_escape_text(dev.name)}</b>\n"
                         f"<small>{GLib.markup_escape_text(dev.address)}</small>")
        g.attach(title, 0, 0, 2, 1)
        g.attach(Gtk.Label(label="Логин", xalign=1), 0, 1, 1, 1)
        self.user = Gtk.Entry(text=dev.user, activates_default=True, hexpand=True, max_length=64)
        g.attach(self.user, 1, 1, 1, 1)
        g.attach(Gtk.Label(label="Пароль", xalign=1), 0, 2, 1, 1)
        self.pw = _password_entry()
        g.attach(self.pw, 1, 2, 1, 1)
        self.remember = Gtk.CheckButton(label="Запомнить пароль", active=dev.remember)
        if session.secrets.persistent:
            self.remember.set_tooltip_text("Пароль сохранится в брелоке паролей Ubuntu (зашифрован, "
                                           "открывается входом в систему)")
        else:
            self.remember.set_sensitive(False)
            self.remember.set_active(False)
            self.remember.set_tooltip_text("Брелок паролей недоступен: пароль будет спрашиваться при каждом входе")
        g.attach(self.remember, 1, 3, 1, 1)
        if not dev.https:
            g.attach(_note("Соединение без шифрования (HTTP). Пароль передаётся только в виде Digest, "
                           "но видео и списки записей идут открыто по сети.", "warn"), 0, 4, 2, 1)
        self.msg = _note(message, "err")
        g.attach(self.msg, 0, 5, 2, 1)
        self.spinner = Gtk.Spinner()
        g.attach(self.spinner, 0, 6, 2, 1)
        self.get_content_area().add(g)
        self.connect("response", self._response)
        self._timer = 0
        self.show_all()
        (self.pw if dev.user else self.user).grab_focus()
        self._update_lock()

    def _update_lock(self):
        g, dev_id = self.session.guard, self.session.device.id
        wait = g.wait_left(dev_id)
        if wait:
            self.ok.set_sensitive(False)
            m, s = divmod(wait, 60)
            if g.failures(dev_id) >= g.MAX_FAILS:
                self.msg.set_text(f"Слишком много неудачных входов. Вход откроется через {m}:{s:02d}. "
                                  "Так регистратор не заблокирует этот компьютер.")
            else:
                self.msg.set_text(f"Регистратор временно заблокировал вход с этого компьютера. "
                                  f"Вход откроется через {m}:{s:02d}.")
            if not self._timer:
                self._timer = GLib.timeout_add_seconds(1, self._tick)
        else:
            self.ok.set_sensitive(True)
        return wait

    def _tick(self):
        if self._update_lock():
            return True
        self._timer = 0
        self.msg.set_text("")
        return False

    def _response(self, _d, resp):
        if resp != Gtk.ResponseType.OK:
            self._close(None)
            return
        user, pw = self.user.get_text().strip(), self.pw.get_text()
        if not user or not pw:
            self.msg.set_text("Введите логин и пароль")
            return
        self.ok.set_sensitive(False)
        self.spinner.start()
        self.msg.set_text("")

        def fail(e):
            self.spinner.stop()
            self.pw.set_text("")
            self.msg.set_text(err_text(e))
            if not self._update_lock():
                self.ok.set_sensitive(True)
            self.pw.grab_focus()
        bg(self.session.login, user, pw, self.remember.get_active(),
           done=lambda cams: self._close(cams), fail=fail)

    def _close(self, cams):
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        self.pw.set_text("")
        self.destroy()
        self.on_done(cams, None)


# ---------- карточка регистратора ----------
class DeviceDialog(Gtk.Dialog):
    def __init__(self, parent, device: Device | None):
        super().__init__(title="Регистратор" if device else "Новый регистратор", transient_for=parent, modal=True)
        self.add_buttons("Отмена", Gtk.ResponseType.CANCEL, "Сохранить", Gtk.ResponseType.OK)
        self.set_default_response(Gtk.ResponseType.OK)
        self.device = device
        d = device or Device(name="", host="")
        g = Gtk.Grid(row_spacing=8, column_spacing=10, margin=16)
        self.name = Gtk.Entry(text=d.name, placeholder_text="например «Производство»", hexpand=True,
                              activates_default=True, max_length=80)
        self.host = Gtk.Entry(text=d.host, placeholder_text="например 192.168.1.64", activates_default=True)
        self.https = Gtk.CheckButton(label="HTTPS (шифрование, если регистратор поддерживает)", active=d.https)
        self.http_port = Gtk.SpinButton.new_with_range(1, 65535, 1)
        self.http_port.set_value(d.http_port)
        self.rtsp_port = Gtk.SpinButton.new_with_range(1, 65535, 1)
        self.rtsp_port.set_value(d.rtsp_port)
        for sp in (self.http_port, self.rtsp_port):
            sp.set_numeric(True)  # только цифры: опечатка не превратится молча в «1»
            sp.set_activates_default(True)
        self.https.connect("toggled", self._https_toggled)
        self.host.connect("changed", self._host_changed)
        rows = (("Название", self.name), ("Адрес", self.host), ("", self.https),
                ("Порт веб (HTTP/HTTPS)", self.http_port), ("Порт видео (RTSP)", self.rtsp_port))
        for i, (lab, w) in enumerate(rows):
            g.attach(Gtk.Label(label=lab, xalign=1), 0, i, 1, 1)
            g.attach(w, 1, i, 1, 1)
        n = len(rows)
        self.pin = d.cert_sha256
        self.pin_label = _note("")
        self.pin_reset = Gtk.Button(label="Сбросить отпечаток сертификата")
        self.pin_reset.connect("clicked", self._reset_pin)
        g.attach(self.pin_label, 0, n, 2, 1)
        g.attach(self.pin_reset, 1, n + 1, 1, 1)
        test = Gtk.Button(label="Проверить связь")
        test.set_tooltip_text("Проверка без логина и пароля: попытки входа не тратятся")
        test.connect("clicked", self._test)
        g.attach(test, 1, n + 2, 1, 1)
        self.result = _note("")
        g.attach(self.result, 0, n + 3, 2, 1)
        g.attach(_note("Логин и пароль спрашиваются при входе. Пароль в настройках не хранится."), 0, n + 4, 2, 1)
        self.get_content_area().add(g)
        self.show_all()
        self._show_pin()

    def _show_pin(self):
        has = bool(self.pin)
        self.pin_label.set_text("Сертификат регистратора запомнен при первом входе" if has else "")
        self.pin_label.set_tooltip_text(f"Отпечаток SHA-256 (для администратора):\n{fingerprint_text(self.pin)}"
                                        if has else None)
        self.pin_label.set_visible(has)
        self.pin_reset.set_visible(has)

    def _reset_pin(self, _b):
        self.pin = ""
        self._show_pin()

    def _host_changed(self, e):
        t = e.get_text().strip().lower()
        if t.startswith("https://") and not self.https.get_active():
            self.https.set_active(True)
        elif t.startswith("http://") and self.https.get_active():
            self.https.set_active(False)

    def _https_toggled(self, b):
        cur = int(self.http_port.get_value())
        if b.get_active() and cur == 80:
            self.http_port.set_value(443)
        elif not b.get_active() and cur == 443:
            self.http_port.set_value(80)

    def values(self) -> Device:
        for sp in (self.http_port, self.rtsp_port):
            sp.update()
        base = self.device or Device(name="", host="")
        dev = Device(**{**base.__dict__})
        dev.name, dev.host = self.name.get_text(), self.host.get_text()
        dev.https = self.https.get_active()
        dev.http_port, dev.rtsp_port = int(self.http_port.get_value()), int(self.rtsp_port.get_value())
        if base.host != dev.host or base.https != dev.https or base.http_port != dev.http_port:
            self.pin = "" if self.pin == base.cert_sha256 else self.pin
        dev.cert_sha256 = self.pin
        return dev.validate()

    def _test(self, btn):
        try:
            d = self.values()
        except ValueError as e:
            self._result("err", str(e))
            return
        btn.set_sensitive(False)
        self._result("hint", "Проверяю…")

        def done(res):
            btn.set_sensitive(True)
            level, text, fp = res
            self._result(level, text)
            self.result.set_tooltip_text(f"Отпечаток сертификата SHA-256:\n{fingerprint_text(fp)}" if fp else None)
        bg(probe, d.host, d.http_port, d.https, d.cert_sha256,
           done=done, fail=lambda e: (btn.set_sensitive(True), self._result("err", err_text(e))))

    def _result(self, level, text):
        ctx = self.result.get_style_context()
        for c in ("ok", "warn", "err", "hint"):
            ctx.remove_class(c)
        ctx.add_class(level)
        self.result.set_text(text)

    def run_edit(self) -> Device | None:
        while self.run() == Gtk.ResponseType.OK:
            try:
                dev = self.values()
            except ValueError as e:
                self._result("err", str(e))
                continue
            self.destroy()
            return dev
        self.destroy()
        return None


# ---------- список регистраторов ----------
class DevicesDialog(Gtk.Dialog):
    def __init__(self, win):
        super().__init__(title="Регистраторы", transient_for=win, modal=True)
        self.win = win
        self.set_default_size(520, 380)
        self.add_button("Готово", Gtk.ResponseType.CLOSE)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=12)
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.list.connect("row-activated", lambda *_: self._edit())
        ph = Gtk.Label(label="Регистраторов пока нет.\nНажмите «Добавить» и укажите адрес регистратора.",
                       justify=Gtk.Justification.CENTER, margin=24)
        ph.get_style_context().add_class("hint")
        ph.show()
        self.list.set_placeholder(ph)
        sw = Gtk.ScrolledWindow(vexpand=True)
        sw.add(self.list)
        box.pack_start(sw, True, True, 0)
        bar = Gtk.Box(spacing=6)
        self.need_sel = []
        for label, cb in (("Добавить", self._add), ("Изменить", self._edit), ("Удалить", self._remove)):
            b = Gtk.Button(label=label)
            b.connect("clicked", lambda *_, f=cb: f())
            bar.pack_start(b, False, False, 0)
            if label != "Добавить":
                self.need_sel.append(b)
        self.list.connect("row-selected", lambda *_: self._sel_changed())
        box.pack_start(bar, False, False, 0)
        self.get_content_area().add(box)
        self._fill()
        self.show_all()
        self._sel_changed()

    def _sel_changed(self):
        for b in self.need_sel:
            b.set_sensitive(self.list.get_selected_row() is not None)

    def _fill(self):
        for r in self.list.get_children():
            r.destroy()
        for d in self.win.store.devices:
            row = Gtk.ListBoxRow()
            row.dev_id = d.id
            lab = Gtk.Label(xalign=0, margin=8)
            user = f" · {GLib.markup_escape_text(d.user)}" if d.user else ""
            saved = " · пароль сохранён" if d.remember else ""
            lab.set_markup(f"<b>{GLib.markup_escape_text(d.name)}</b>\n"
                           f"<small>{GLib.markup_escape_text(d.address)}{user}{saved}</small>")
            row.add(lab)
            self.list.add(row)
        self.list.show_all()

    def _selected(self) -> Device | None:
        r = self.list.get_selected_row()
        return self.win.store.get(r.dev_id) if r else None

    def _add(self):
        dev = DeviceDialog(self, None).run_edit()
        if dev:
            self.win.device_saved(dev, new=True)
            self._fill()

    def _edit(self):
        cur = self._selected()
        if cur is None:
            return
        dev = DeviceDialog(self, cur).run_edit()
        if dev:
            self.win.device_saved(dev, new=False)
            self._fill()

    def _remove(self):
        cur = self._selected()
        if cur is None:
            return
        q = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                              buttons=Gtk.ButtonsType.OK_CANCEL,
                              text=f"Удалить регистратор «{cur.name}»?")
        q.format_secondary_text("Сохранённый пароль и кэш архива этого регистратора тоже будут удалены. "
                                "На самом регистраторе ничего не меняется.")
        ok = q.run() == Gtk.ResponseType.OK
        q.destroy()
        if ok:
            self.win.device_removed(cur)
            self._fill()
