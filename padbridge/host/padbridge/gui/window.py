"""PadBridge main window."""
import json
import os
import time

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from .. import APP_ID
from ..config import (BLUETOOTH_NAMES, IDENTITY_INFO, IDENTITY_NAMES, IDENTITY_TABLE, RATE_MAX, RATE_MIN,
                      normalize_identity)
from .link import ServiceLink
from .visualizer import InputView

TRANSPORT = {"le": "Bluetooth Low Energy", "classic": "Bluetooth Classic"}
HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE_ROOT = os.path.dirname(os.path.dirname(HERE))       # padbridge/host when run from the source tree


def ago(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "less than a minute"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours = minutes // 60
    return f"{hours} hour{'s' if hours != 1 else ''} {minutes % 60} min"


class Chip(Gtk.Box):
    def __init__(self, icon):
        super().__init__(spacing=6, css_classes=["chip"])
        self.image = Gtk.Image(icon_name=icon, pixel_size=16)
        self.label = Gtk.Label()
        self.append(self.image)
        self.append(self.label)

    def set(self, text, ok):
        self.label.set_label(text)
        if ok:
            self.add_css_class("ok")
        else:
            self.remove_css_class("ok")


KNOWN_ERRORS = (
    ("cannot bind HID channels", "Bluetooth isn't set up for PadBridge, or another program holds the "
                                 "controller channels. Choose an identity under Appear as to set it up again."),
    ("No Bluetooth adapter", "This laptop's Bluetooth adapter isn't available. Check that Bluetooth is on."),
    ("Missing python3-dbus", "A system package is missing. Run the installer again."),
    ("Run with sudo", "The engine needs to run as root. Reinstall PadBridge."),
)


def friendly_error(message):
    """Plain words for errors we know; the raw line is always in Activity."""
    message = message or ""
    for needle, text in KNOWN_ERRORS:
        if needle in message:
            return text
    return (message.rstrip(".") + ".") if message else "The engine exited unexpectedly."


def steps_list(steps):
    """Numbered instructions for a dialog, left-aligned (dialog bodies are centred)."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    box.add_css_class("steps")
    for n, text in enumerate(steps, 1):
        row = Gtk.Box(spacing=12)
        num = Gtk.Label(label=str(n), valign=Gtk.Align.START, css_classes=["step-number", "numeric"])
        lab = Gtk.Label(label=text, wrap=True, xalign=0, hexpand=True, max_width_chars=40)
        row.append(num)
        row.append(lab)
        box.append(row)
    return box


class Stat(Gtk.Box):
    def __init__(self, caption):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        self.value = Gtk.Label(css_classes=["title-2", "numeric", "stat-value"], label="–")
        self.caption = Gtk.Label(label=caption, css_classes=["caption", "dim-label"], wrap=True,
                                 justify=Gtk.Justification.CENTER)
        self.append(self.value)
        self.append(self.caption)


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="PadBridge", icon_name=APP_ID)
        self.set_default_size(720, 920)
        self.set_size_request(360, 480)
        self.status = None
        self.syncing = False
        self.user_touched = {}          # widget key -> time of last user change (don't fight the poll)
        self.last_action_seen = None
        self.problem_kind = None
        self.narrow = False
        self.rate_timer = None
        self.log_lines = 0
        self.first_status = True

        self.toasts = Adw.ToastOverlay()
        self.toolbar = Adw.ToolbarView()
        self.toasts.set_child(self.toolbar)
        self.set_content(self.toasts)

        self.build_header()
        self.content = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=150)
        self.toolbar.set_content(self.content)

        loading = Adw.StatusPage(title="Connecting to PadBridge", paintable=Adw.SpinnerPaintable.new(None))
        loading.get_paintable().set_widget(loading)
        self.content.add_named(loading, "loading")
        self.problem = Adw.StatusPage()
        self.problem_button = Gtk.Button(halign=Gtk.Align.CENTER, css_classes=["pill", "suggested-action"])
        self.problem_button.connect("clicked", self.on_problem_action)
        self.problem_extra = Gtk.Label(selectable=True, wrap=True, css_classes=["monospace", "dim-label"],
                                       visible=False, margin_top=12, justify=Gtk.Justification.CENTER)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.append(self.problem_button)
        box.append(self.problem_extra)
        self.problem.set_child(box)
        self.content.add_named(self.problem, "problem")

        self.views = Adw.ViewStack()
        self.views.add_titled_with_icon(self.build_bridge_page(), "bridge", "Bridge", "input-gaming-symbolic")
        self.views.add_titled_with_icon(self.build_activity_page(), "activity", "Activity",
                                        "document-open-recent-symbolic")
        self.content.add_named(self.views, "main")
        self.switcher.set_stack(self.views)
        self.switcher_bar.set_stack(self.views)

        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 520sp"))
        bp.connect("apply", lambda *_: self.set_narrow(True))
        bp.connect("unapply", lambda *_: self.set_narrow(False))
        self.add_breakpoint(bp)

        self.install_actions()
        self.update_chrome()
        self.dev_hooks()
        self.link = ServiceLink(self.on_status, self.on_error)
        self.connect("close-request", self.on_close)
        GLib.timeout_add_seconds(1, self.tick_clock)

    # ================================================================== construction
    def build_header(self):
        header = Adw.HeaderBar()
        self.switcher = Adw.ViewSwitcher(policy=Adw.ViewSwitcherPolicy.WIDE)
        self.window_title = Adw.WindowTitle(title="PadBridge")
        self.title_stack = Gtk.Stack()
        self.title_stack.add_named(self.switcher, "switcher")
        self.title_stack.add_named(self.window_title, "title")
        header.set_title_widget(self.title_stack)

        self.menu_main = Gio.Menu()
        menu = Gio.Menu()
        menu.append_section(None, self.menu_main)
        about = Gio.Menu()
        about.append("About PadBridge", "app.about")
        menu.append_section(None, about)
        self.menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                                          tooltip_text="Main menu", primary=True)
        header.pack_end(self.menu_button)
        self.toolbar.add_top_bar(header)

        self.banner = Adw.Banner()
        self.banner.connect("button-clicked", self.on_banner_button)
        self.banner_action = None
        self.toolbar.add_top_bar(self.banner)

        self.switcher_bar = Adw.ViewSwitcherBar()
        self.toolbar.add_bottom_bar(self.switcher_bar)

    def install_actions(self):
        for name, cb in (("restart", lambda *_: self.command("restart", toast="Restarting the bridge")),
                         ("start", lambda *_: self.command("start")),
                         ("stop", lambda *_: self.command("stop", toast="Bridging stopped")),
                         ("pair", lambda *_: self.show_pair_dialog()),
                         ("forget", lambda *_: self.confirm_forget()),
                         ("copy-log", lambda *_: self.copy_log())):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", cb)
            self.add_action(action)
        self.get_application().set_accels_for_action("win.restart", ["<Control>r"])

    def page_box(self):
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=640, tightening_threshold=480)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24,
                      margin_top=24, margin_bottom=32, margin_start=12, margin_end=12)
        clamp.set_child(box)
        scroller.set_child(clamp)
        return scroller, box

    def build_bridge_page(self):
        scroller, box = self.page_box()
        self.bridge_scroller = scroller

        # ---- hero: the one big thing on the page
        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_bottom=6)
        self.hero_disc = Gtk.Stack(css_classes=["hero-icon"], halign=Gtk.Align.CENTER, margin_bottom=12,
                                   transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.hero_image = Gtk.Image(pixel_size=44)
        self.hero_spinner = Adw.Spinner(width_request=36, height_request=36)
        self.hero_disc.add_named(self.hero_image, "icon")
        self.hero_disc.add_named(self.hero_spinner, "spinner")
        self.hero_title = Gtk.Label(css_classes=["title-1"], wrap=True, justify=Gtk.Justification.CENTER)
        self.hero_text = Gtk.Label(css_classes=["body", "dim-label"], wrap=True, max_width_chars=52,
                                   justify=Gtk.Justification.CENTER)
        chips = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER, margin_top=12)
        self.chip_ctrl = Chip("input-gaming-symbolic")
        self.chip_mac = Chip("computer-symbolic")
        chips.append(self.chip_ctrl)
        chips.append(self.chip_mac)
        self.hero_button = Gtk.Button(halign=Gtk.Align.CENTER, css_classes=["pill", "suggested-action"],
                                      margin_top=18, visible=False)
        self.hero_button.connect("clicked", self.on_hero_button)
        for w in (self.hero_disc, self.hero_title, self.hero_text, chips, self.hero_button):
            hero.append(w)
        box.append(hero)

        # ---- live input
        live = self.live_group = Adw.PreferencesGroup(title="Live input")
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["card"])
        overlay = Gtk.Overlay()
        self.input_view = InputView()
        self.input_view.set_margin_top(12)
        self.input_view.set_margin_bottom(6)
        self.input_view.set_margin_start(12)
        self.input_view.set_margin_end(12)
        overlay.set_child(self.input_view)
        self.input_hint = Gtk.Label(css_classes=["input-hint"], halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
                                    wrap=True, justify=Gtk.Justification.CENTER)
        overlay.add_overlay(self.input_hint)
        card.append(overlay)
        card.append(Gtk.Separator())
        stats = Gtk.Box(homogeneous=True, margin_top=12, margin_bottom=12, margin_start=6, margin_end=6)
        self.stat_rate = Stat("Reports per second")
        self.stat_age = Stat("Delay on this laptop")
        self.stat_link = Stat("Bluetooth delivery")
        for s in (self.stat_rate, self.stat_age, self.stat_link):
            stats.append(s)
        card.append(stats)
        live.add(card)
        box.append(live)

        # ---- Mac
        mac = Adw.PreferencesGroup(title="Mac")
        self.mac_row = Adw.ActionRow(subtitle_lines=2)
        self.mac_row.add_prefix(Gtk.Image(icon_name="computer-symbolic"))
        self.mac_state = Gtk.Label(css_classes=["dim-label"])
        self.mac_row.add_suffix(self.mac_state)
        mac.add(self.mac_row)
        self.pair_row = Adw.ButtonRow(title="Pair a new Mac", start_icon_name="list-add-symbolic",
                                      action_name="win.pair")
        self.forget_row = Adw.ButtonRow(title="Forget this Mac", action_name="win.forget")
        self.forget_row.add_css_class("destructive-action")
        mac.add(self.pair_row)
        mac.add(self.forget_row)
        box.append(mac)

        # ---- controller and this laptop
        dev = Adw.PreferencesGroup(title="Controller and Bluetooth")
        self.ctrl_row = Adw.ActionRow(title="Controller", subtitle_lines=2)
        self.ctrl_row.add_prefix(Gtk.Image(icon_name="input-gaming-symbolic"))
        self.ctrl_state = Gtk.Label(css_classes=["dim-label"])
        self.ctrl_row.add_suffix(self.ctrl_state)
        self.bt_row = Adw.ActionRow(title="This laptop's Bluetooth", subtitle_lines=2)
        self.bt_row.add_prefix(Gtk.Image(icon_name="bluetooth-active-symbolic"))
        dev.add(self.ctrl_row)
        dev.add(self.bt_row)
        box.append(dev)

        # ---- identity: rows come from the service's identity list (rebuilt when it changes)
        self.ident_group = Adw.PreferencesGroup(
            title="Appear as",
            description="After a switch, your Mac has to forget the controller and pair again. "
                        "PadBridge walks you through it.")
        self.identity_rows = {}
        self.identity_ids = None
        self.rebuild_identity_rows([dict(i, supported=True) for i in IDENTITY_TABLE])
        box.append(self.ident_group)

        # ---- power
        power = Adw.PreferencesGroup(title="Power")
        self.lid_row = Adw.SwitchRow(title="Keep bridging with the lid closed",
                                     subtitle="This laptop won't sleep while it bridges. Keep it plugged in.")
        self.lid_row.connect("notify::active", self.on_lid_toggled)
        self.boot_row = Adw.SwitchRow(title="Start with this laptop",
                                      subtitle="Bridge automatically after the laptop starts, before anyone logs in.")
        self.boot_row.connect("notify::active", self.on_boot_toggled)
        power.add(self.lid_row)
        power.add(self.boot_row)
        box.append(power)

        # ---- advanced
        adv = Adw.PreferencesGroup(title="Advanced")
        self.rate_row = Adw.SpinRow.new_with_range(RATE_MIN, RATE_MAX, 10)
        self.rate_row.set_title("Reports per second")
        self.rate_row.set_subtitle("250 matches a real controller. Changing it restarts the bridge.")
        self.rate_row.connect("notify::value", self.on_rate_changed)
        self.share_row = Adw.SwitchRow(title="Let this laptop use the controller too",
                                       subtitle="Off keeps button presses from reaching apps on this laptop.")
        self.share_row.connect("notify::active", self.on_share_toggled)
        adv.add(self.rate_row)
        adv.add(self.share_row)
        box.append(adv)
        return scroller

    def build_activity_page(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        clamp = Adw.Clamp(maximum_size=880, vexpand=True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=18, margin_bottom=18,
                      margin_start=12, margin_end=12)
        head = Gtk.Box(spacing=12)
        title = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        title.append(Gtk.Label(label="Activity", xalign=0, css_classes=["heading"]))
        title.append(Gtk.Label(label="What the bridge and its service did, newest at the bottom.", xalign=0,
                               wrap=True, css_classes=["dim-label", "caption"]))
        head.append(title)
        copy = Gtk.Button(label="Copy all", valign=Gtk.Align.CENTER, action_name="win.copy-log")
        head.append(copy)
        box.append(head)

        self.log_stack = Gtk.Stack(vexpand=True)
        empty = Adw.StatusPage(icon_name="document-open-recent-symbolic", title="No activity yet",
                               description="Events appear here once PadBridge's service is running.")
        empty.add_css_class("compact")
        self.log_stack.add_named(empty, "empty")
        self.log_scroller = Gtk.ScrolledWindow(vexpand=True)
        self.log_scroller.add_css_class("card")
        self.log_scroller.add_css_class("log-card")
        self.log_view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True,
                                     wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=12, bottom_margin=12,
                                     left_margin=14, right_margin=14, css_classes=["log-view"])
        buf = self.log_view.get_buffer()
        dim = Gdk.RGBA()
        dim.parse("rgba(128, 128, 128, 0.95)")     # mid grey reads as secondary on light and dark
        buf.create_tag("time", foreground_rgba=dim)
        self.log_scroller.set_child(self.log_view)
        self.log_stack.add_named(self.log_scroller, "log")
        box.append(self.log_stack)
        clamp.set_child(box)
        outer.append(clamp)
        return outer

    def rebuild_identity_rows(self, identities):
        ids = [i["id"] for i in identities]
        if ids == self.identity_ids:
            return
        for row, *_ in self.identity_rows.values():
            self.ident_group.remove(row)
        self.identity_rows, self.identity_ids, group = {}, ids, None
        for item in identities:
            info = {**IDENTITY_INFO.get(item["id"], {}), **item}
            row = Adw.ActionRow(title=info.get("label") or info["id"], subtitle_lines=3)
            check = Gtk.CheckButton(valign=Gtk.Align.CENTER)
            if group:
                check.set_group(group)
            group = group or check
            row.add_prefix(check)
            row.set_activatable_widget(check)
            # Room for a measured latency figure per identity (status identities[].latency_ms, when known).
            latency = Gtk.Label(css_classes=["dim-label", "numeric"], visible=False)
            spinner = Adw.Spinner(visible=False)
            row.add_suffix(latency)
            row.add_suffix(spinner)
            check.connect("toggled", self.on_identity_toggled, info["id"])
            self.identity_rows[info["id"]] = (row, check, spinner, latency)
            self.ident_group.add(row)
        self.refresh_identity_rows(identities)

    def refresh_identity_rows(self, identities):
        for item in identities:
            entry = self.identity_rows.get(item["id"])
            if not entry:
                continue
            row, check, spinner, latency = entry
            info = {**IDENTITY_INFO.get(item["id"], {}), **item}
            transport = TRANSPORT.get(info.get("transport"), "")
            if not info.get("supported", True):
                row.set_subtitle(f"{transport}. Needs a newer PadBridge engine.")
            else:
                row.set_subtitle(" · ".join(x for x in (transport, info.get("note")) if x))
            row.set_sensitive(info.get("supported", True))
            ms = info.get("latency_ms")
            latency.set_visible(isinstance(ms, (int, float)))
            if isinstance(ms, (int, float)):
                latency.set_label(f"{ms:.0f} ms")

    def identities_of(self, st):
        return st["service"].get("identities") or [dict(i, supported=True) for i in IDENTITY_TABLE]

    def identity_of(self, st):
        return normalize_identity(st["service"]["setup"]["identity"] or st["settings"]["identity"])

    def label_of(self, st, identity):
        for item in self.identities_of(st):
            if item["id"] == identity and item.get("label"):
                return item["label"]
        return IDENTITY_NAMES.get(identity, identity)

    def bt_name_of(self, st, identity=None):
        """The name the Mac lists in Bluetooth settings: the engine's bt_name, else the identity's default."""
        eng = st.get("engine") or {}
        return (eng.get("bt_name") or st["service"].get("bt_name")
                or BLUETOOTH_NAMES.get(identity or self.identity_of(st), "PadBridge"))

    def dev_hooks(self):
        """Development only (screenshots): PADBRIDGE_DEV_PAGE=activity, PADBRIDGE_DEV_SCROLL=<px>,
        PADBRIDGE_DEV_SIZE=<w>x<h>."""
        size = os.environ.get("PADBRIDGE_DEV_SIZE")
        if size:
            w, h = (int(v) for v in size.split("x"))
            self.set_default_size(w, h)
        page = os.environ.get("PADBRIDGE_DEV_PAGE")
        if page:
            self.views.set_visible_child_name(page)
        dialog = os.environ.get("PADBRIDGE_DEV_DIALOG")      # switch:<id> | repair:<id> | pair | forget
        if dialog:
            def open_dialog():
                if not self.status:
                    return True
                kind, _, arg = dialog.partition(":")
                if kind == "switch":
                    self.identity_rows[arg][1].set_active(True)
                elif kind == "repair":
                    self.show_repair_dialog({"identity": arg, "old_identity": "xbox-one" if arg == "ds4" else "ds4"})
                elif kind == "pair":
                    self.show_pair_dialog()
                elif kind == "forget":
                    self.confirm_forget()
                return False
            GLib.timeout_add(2000, open_dialog)
        scroll = os.environ.get("PADBRIDGE_DEV_SCROLL")
        if scroll:
            def go():
                self.bridge_scroller.get_vadjustment().set_value(float(scroll))
                return False
            GLib.timeout_add(2500, go)

    # ================================================================== chrome
    def set_narrow(self, narrow):
        self.narrow = narrow
        self.update_chrome()

    def update_chrome(self):
        main = self.content.get_visible_child_name() == "main"
        self.title_stack.set_visible_child_name("switcher" if main and not self.narrow else "title")
        self.switcher_bar.set_reveal(main and self.narrow)
        self.menu_button.set_visible(main)

    def show_content(self, name):
        if self.content.get_visible_child_name() != name:
            self.content.set_visible_child_name(name)
        self.update_chrome()

    def set_menu(self, state):
        self.menu_main.remove_all()
        if state in ("stopped", "hold"):
            self.menu_main.append("Start bridging", "win.start")
        else:
            self.menu_main.append("Restart bridge", "win.restart")
            self.menu_main.append("Stop bridging", "win.stop")

    def toast(self, text, timeout=3):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=timeout))

    # ================================================================== service results
    def on_error(self, kind, message):
        self.status = None
        self.problem_kind = kind
        self.problem_extra.set_visible(False)
        self.problem_button.set_visible(True)
        self.problem_button.set_sensitive(True)
        if kind == "permission":
            self.problem.set_icon_name("system-lock-screen-symbolic")
            self.problem.set_title("Log out to finish setup")
            self.problem.set_description(
                "Your account was added to the padbridge group during installation. "
                "Log out and back in so PadBridge can talk to its service.")
            self.problem_button.set_label("Log out")
        elif kind == "not_installed":
            self.problem.set_icon_name(APP_ID)
            self.problem.set_title("Install PadBridge")
            installer = self.installer_path()
            if installer:
                self.problem.set_description(
                    "PadBridge runs a small background service that drives Bluetooth. "
                    "Installing it needs your password once.")
                self.problem_button.set_label("Install")
            else:
                self.problem.set_description(
                    "PadBridge runs a small background service that drives Bluetooth. "
                    "Open a terminal in the PadBridge folder and run:")
                self.problem_extra.set_label("sudo ./install.sh")
                self.problem_extra.set_visible(True)
                self.problem_button.set_visible(False)
        elif kind == "not_running":
            self.problem.set_icon_name(APP_ID)
            self.problem.set_title("PadBridge's service is stopped")
            self.problem.set_description("Start it to send your controller to your Mac. "
                                         "It may ask for your password.")
            self.problem_button.set_label("Start service")
        else:
            self.problem.set_icon_name("dialog-warning-symbolic")
            self.problem.set_title("Unable to reach PadBridge")
            self.problem.set_description("The service didn't answer. PadBridge keeps trying.")
            self.problem_extra.set_label(message)
            self.problem_extra.set_visible(True)
            self.problem_button.set_visible(False)
        self.show_content("problem")

    def installer_path(self):
        path = os.path.join(SOURCE_ROOT, "install.sh")
        return path if os.path.exists(path) else None

    def on_problem_action(self, _button):
        kind = self.problem_kind
        if kind == "permission":
            self.spawn(["gnome-session-quit", "--logout"], "Unable to log out from here. Use the system menu.")
        elif kind == "not_installed" and self.installer_path():
            self.problem_button.set_sensitive(False)
            self.problem_button.set_label("Installing…")
            user = GLib.get_user_name()
            self.spawn(["pkexec", "/usr/bin/bash", self.installer_path(), "--user", user],
                       "Installation didn't finish. Run sudo ./install.sh in a terminal to see why.")
        elif kind == "not_running":
            self.problem_button.set_sensitive(False)
            self.spawn(["systemctl", "start", "padbridged.service"],
                       "Unable to start the service. Run: sudo systemctl start padbridged")

    def spawn(self, argv, failure):
        try:
            proc = Gio.Subprocess.new(argv, Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_SILENCE)
        except GLib.Error:
            self.toast(failure, 6)
            self.problem_button.set_sensitive(True)
            return

        def done(p, res):
            try:
                ok = p.wait_check_finish(res)
            except GLib.Error:
                ok = False
            if not ok:
                self.toast(failure, 6)
            self.problem_button.set_sensitive(True)
        proc.wait_check_async(None, done)

    def on_status(self, st):
        self.status = st
        svc, cfg = st["service"], st["settings"]
        eng = st.get("engine") or {}
        fresh = svc.get("status_fresh")
        self.show_content("main")
        self.set_menu(svc["state"])
        self.append_log(st.get("log") or [], st.get("log_reset") or self.first_status)
        self.first_status = False

        identity = self.identity_of(st)
        identities = self.identities_of(st)
        self.rebuild_identity_rows(identities)
        self.refresh_identity_rows(identities)
        ctrl = (eng.get("controller") or {}) if fresh else {}
        host = (eng.get("host") or {}) if fresh else {}
        paired = svc["paired"]["address"]
        mac_name = svc["paired"]["name"] or "your Mac"
        mac_label = svc["paired"]["name"] or "Mac"
        bt_name = self.bt_name_of(st, identity)
        label = self.label_of(st, identity)
        pad = (eng.get("controller") or {}).get("name") if fresh else None
        self.live_group.set_visible(svc["state"] == "running" and bool((eng.get("host") or {}).get("connected")))
        connected = bool(host.get("connected"))
        plugged = bool(ctrl.get("connected"))
        running = svc["state"] == "running"

        self.update_hero(svc, identity, plugged, connected, paired, mac_name, bt_name, label,
                         eng.get("bt_name") if fresh else None)
        self.update_banner(svc, cfg)

        self.chip_ctrl.set("Controller plugged in" if plugged else "No controller", plugged)
        self.chip_mac.set("Mac connected" if connected else ("Mac not connected" if paired else "No Mac paired"),
                          connected)
        self.chip_ctrl.set_visible(running)
        self.chip_mac.set_visible(running)

        # live input
        live = connected and plugged and running
        self.input_view.set_input(eng.get("input"), live, "ds4" if identity == "ds4" else "xbox")
        if live:
            self.input_hint.set_visible(False)
        else:
            self.input_hint.set_visible(True)
            self.input_hint.set_label("Plug in your controller to see its input" if running and not plugged
                                      else "Live input appears once your Mac is connected")
        stats = eng.get("stats") or {}
        if connected and running:
            self.stat_rate.value.set_label(f"{stats.get('reports_per_s', 0):.0f}")
            age = stats.get("input_age_ms")
            self.stat_age.value.set_label(f"{age:.1f} ms" if isinstance(age, (int, float)) else "–")
            link = stats.get("link_ms")
            self.stat_link.value.set_label(f"{link:.1f} ms" if isinstance(link, (int, float)) else "–")
            p99 = stats.get("link_p99_ms")
            self.stat_link.set_tooltip_text(f"Slowest 1%: {p99:.0f} ms" if isinstance(p99, (int, float)) else None)
        else:
            for s in (self.stat_rate, self.stat_age, self.stat_link):
                s.value.set_label("–")

        # Mac
        if paired:
            self.mac_row.set_title(mac_label)
            detail = [paired]
            if connected and host.get("since"):
                detail.append(f"Connected for {ago(svc['now'] - host['since'])}")
            self.mac_row.set_subtitle(" · ".join(detail))
            self.mac_state.set_label("Connected" if connected else "Not connected")
        else:
            self.mac_row.set_title("No Mac paired")
            self.mac_row.set_subtitle(f"On your Mac, connect “{bt_name}” in Bluetooth settings.")
            self.mac_state.set_label("")
        self.forget_row.set_visible(bool(paired))
        self.pair_row.set_title("Pair a different Mac" if paired else "How to pair your Mac")
        self.pair_row.set_start_icon_name("list-add-symbolic" if paired else "help-about-symbolic")

        # controller and Bluetooth
        if plugged:
            self.ctrl_row.set_title(pad or "Controller")
            vid, pid = ctrl.get("vid"), ctrl.get("pid")
            facts = [ctrl.get("vendor"), f"{vid}:{pid}" if vid and pid else None,
                     (ctrl.get("bus") or "usb").upper(), None if vid else ctrl.get("path")]
            self.ctrl_row.set_subtitle(" · ".join(f for f in facts if f))
            self.ctrl_row.set_tooltip_text(ctrl.get("path"))
            self.ctrl_state.set_label("Plugged in")
        else:
            self.ctrl_row.set_title("Controller")
            self.ctrl_row.set_subtitle("Connect it to this laptop with a USB cable." if running
                                       else "Not checked while bridging is off" if svc["state"] == "stopped"
                                       else "Checked once the bridge is running")
            self.ctrl_state.set_label("Not found" if running else "")
        adapter = eng.get("adapter") if fresh else None
        bits = [f"Your Mac lists it as “{bt_name}”"] + ([adapter] if adapter else [])
        self.bt_row.set_subtitle(" · ".join(bits) if running else "Not visible while bridging is off"
                                 if svc["state"] == "stopped" else "Not visible until the bridge is running")

        # identity / power / advanced (don't fight a change the user just made)
        self.syncing = True
        try:
            busy = svc.get("busy") == "identity"
            for key, (row, check, spinner, _lat) in self.identity_rows.items():
                if not self.recently_touched("identity"):
                    check.set_active(key == identity)
                if busy:
                    row.set_sensitive(False)
                spinner.set_visible(busy and key == (self.pending_identity() or identity))
            if not self.recently_touched("lid"):
                self.lid_row.set_active(cfg["lid_closed"])
            inh = svc["inhibit"]
            if inh.get("error") and cfg["lid_closed"]:
                self.lid_row.set_subtitle("Unable to keep the laptop awake. See Activity for details.")
            else:
                self.lid_row.set_subtitle("This laptop won't sleep while it bridges. Keep it plugged in.")
            boot = svc.get("start_at_boot")
            self.boot_row.set_sensitive(boot is not None)
            if not self.recently_touched("boot") and boot is not None:
                self.boot_row.set_active(boot)
            if not self.recently_touched("rate") and self.rate_timer is None:
                self.rate_row.set_value(cfg["rate"])
            if not self.recently_touched("share"):
                self.share_row.set_active(not cfg["grab"])
        finally:
            self.syncing = False

        guide = getattr(self, "_repair_guide", None)
        if guide and svc["state"] == "running" and time.time() - guide.get("time", 0) > 5:
            self._repair_guide = None             # the switch is finished and the Mac is back
        act = svc.get("last_action")
        if act and act.get("time") != self.last_action_seen:
            guide = getattr(self, "_repair_guide", None)
            if act.get("ok") and guide:
                self.show_repair_dialog(guide)
            elif self.last_action_seen is not None or time.time() - act["time"] < 10:
                self.toast(act["message"] if act.get("ok") else f"Switch failed: {act['message']}", 5)
            if not act.get("ok"):
                self._repair_guide = None
            self.last_action_seen = act.get("time")
        elif self.last_action_seen is None:
            self.last_action_seen = (act or {}).get("time", 0)

    def pending_identity(self):
        return getattr(self, "_pending_identity", None)

    def recently_touched(self, key, window=2.0):
        return time.time() - self.user_touched.get(key, 0) < window

    def update_hero(self, svc, identity, plugged, connected, paired, mac_name, bt_name, label, pad_name):
        state = svc["state"]
        tone, icon, spinner, button = "neutral", "input-gaming-symbolic", False, None
        if state == "applying":
            target = IDENTITY_NAMES.get(self.pending_identity() or identity, label)
            title, text, spinner, tone = "Switching identity", f"Setting up Bluetooth as {target}. This takes a few seconds.", True, "accent"
        elif state == "hold":
            title, icon, tone = "Forget the controller on your Mac", "computer-symbolic", "accent"
            text = (f"PadBridge now appears as {label} and waits, so your Mac can't reconnect to the old device. "
                    f"On your Mac, open Bluetooth settings, click the Info button next to "
                    f"“{self.mac_side_name()}”, then Forget This Device.")
            button = ("I've forgotten it", "start")
        elif state == "stopped":
            title, text, icon = ("Bridging is off", "Your controller isn't sent to any Mac.",
                                 "media-playback-pause-symbolic")
            button = ("Start bridging", "start")
        elif not svc["setup"]["applied"]:
            title, text, icon, tone = ("Set up Bluetooth", "PadBridge needs to prepare this laptop's Bluetooth once. "
                                       "Bluetooth keyboards, mice and headphones won't work on this laptop while "
                                       "it's set up.", "bluetooth-active-symbolic", "warning")
            button = ("Set up Bluetooth", "setup")
        elif state == "waiting":
            exit_ = svc.get("last_exit") or {}
            wait = max(0, int((svc.get("retry_at") or 0) - svc["now"]))
            title, icon, tone = "The bridge stopped", "dialog-warning-symbolic", "error"
            text = friendly_error(exit_.get("message")) + (f" Trying again in {wait} s." if wait >= 1
                                                          else " Trying again now.")
            button = ("Try again now", "restart")
        elif state == "starting":
            title, text, spinner = "Starting", "Getting Bluetooth and the controller ready.", True
        elif state == "unresponsive":
            title, text, icon, tone = ("The bridge isn't responding", "It stopped reporting its status.",
                                       "dialog-warning-symbolic", "warning")
            button = ("Restart bridge", "restart")
        elif connected and plugged:
            text = f"{pad_name}, appearing as {label}." if pad_name else f"Appearing as {label}."
            title, text, icon, tone = (f"Bridging to {mac_name}", text,
                                       APP_ID + "-symbolic", "success")
        elif connected:
            title, text, icon, tone = ("Plug in your controller",
                                       "Your Mac is connected. Connect the controller to this laptop with a USB cable.",
                                       "input-gaming-symbolic", "warning")
        elif not paired:
            title, text, icon, tone = ("Ready to pair",
                                       f"On your Mac, open System Settings, then Bluetooth, and connect “{bt_name}”.",
                                       "bluetooth-active-symbolic", "accent")
            button = ("How to pair", "pair")
        else:
            title = f"Waiting for {mac_name}"
            text = "PadBridge reconnects by itself when the Mac is awake and its Bluetooth is on."
            icon = "computer-symbolic"
        self.hero_title.set_label(title)
        self.hero_text.set_label(text)
        self.hero_image.set_from_icon_name(icon)
        self.hero_disc.set_visible_child_name("spinner" if spinner else "icon")
        for t in ("neutral", "success", "warning", "error", "accent"):
            (self.hero_disc.add_css_class if t == tone else self.hero_disc.remove_css_class)(t)
        self.hero_button.set_visible(button is not None)
        if button:
            self.hero_button.set_label(button[0])
            self.hero_button_action = button[1]

    def update_banner(self, svc, cfg):
        banner = None
        inh = svc["inhibit"]
        if cfg["lid_closed"] and inh.get("wanted") and not inh.get("active") and inh.get("error"):
            banner = ("Closing the lid will suspend this laptop. PadBridge couldn't stop it.", None, None)
        elif (svc["setup"]["applied"] and svc["setup"]["identity"]
              and normalize_identity(svc["setup"]["identity"]) != normalize_identity(cfg["identity"])):
            banner = (f"Bluetooth is set up as {IDENTITY_NAMES.get(svc['setup']['identity'], '?')}, "
                      f"not {IDENTITY_NAMES.get(cfg['identity'], '?')}", "Fix", "fix-identity")
        if banner:
            self.banner.set_title(banner[0])
            self.banner.set_button_label(banner[1])
            self.banner_action = banner[2]
            self.banner.set_revealed(True)
        else:
            self.banner.set_revealed(False)

    def on_banner_button(self, _banner):
        if self.banner_action == "fix-identity" and self.status:
            self.apply_identity(self.status["settings"]["identity"])

    def on_hero_button(self, _button):
        action = getattr(self, "hero_button_action", None)
        if action == "start":
            self.command("start")
        elif action == "restart":
            self.command("restart")
        elif action == "pair":
            self.show_pair_dialog()
        elif action == "setup" and self.status:
            self.apply_identity(self.status["settings"]["identity"])

    def tick_clock(self):
        return True

    # ================================================================== log
    def append_log(self, entries, reset):
        buf = self.log_view.get_buffer()
        if reset:
            buf.set_text("")
            self.log_lines = 0
        if not entries and self.log_lines == 0:
            self.log_stack.set_visible_child_name("empty")
            return
        adj = self.log_scroller.get_vadjustment()
        at_bottom = adj.get_value() >= adj.get_upper() - adj.get_page_size() - 24
        for e in entries:
            stamp = time.strftime("%H:%M:%S", time.localtime(e["time"]))
            end = buf.get_end_iter()
            buf.insert_with_tags_by_name(end, stamp + "  ", "time")
            buf.insert(buf.get_end_iter(), e["text"] + "\n")
            self.log_lines += 1
        if self.log_lines > 1000:
            start = buf.get_start_iter()
            cut = buf.get_iter_at_line(self.log_lines - 800)[1]
            buf.delete(start, cut)
            self.log_lines = 800
        self.log_stack.set_visible_child_name("log")
        if entries and at_bottom:
            GLib.idle_add(lambda: (adj.set_value(adj.get_upper()), False)[1])

    def copy_log(self):
        buf = self.log_view.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
        self.get_clipboard().set(text)
        self.toast("Activity copied")

    # ================================================================== commands
    def command(self, cmd, toast=None, on_done=None, **args):
        def done(result, error):
            if error:
                self.toast(error, 5)
            elif toast:
                self.toast(toast)
            if on_done:
                on_done(result, error)
        self.link.call(cmd, done, **args)

    def set_setting(self, key, value, touched):
        self.user_touched[touched] = time.time()
        self.command("set_settings", settings={key: value})

    def on_lid_toggled(self, row, _pspec):
        if not self.syncing:
            self.set_setting("lid_closed", row.get_active(), "lid")

    def on_share_toggled(self, row, _pspec):
        if not self.syncing:
            self.set_setting("grab", not row.get_active(), "share")

    def on_rate_changed(self, row, _pspec):
        if self.syncing:
            return
        self.user_touched["rate"] = time.time()
        if self.rate_timer:
            GLib.source_remove(self.rate_timer)

        def apply():
            self.rate_timer = None
            self.set_setting("rate", int(row.get_value()), "rate")
            return False
        self.rate_timer = GLib.timeout_add(900, apply)

    def on_boot_toggled(self, row, _pspec):
        if self.syncing:
            return
        self.user_touched["boot"] = time.time()
        enabled = row.get_active()
        self.command("set_boot", enabled=enabled,
                     toast="PadBridge starts with this laptop" if enabled else "PadBridge won't start with this laptop")

    def on_identity_toggled(self, check, key):
        if self.syncing or not check.get_active() or not self.status:
            return
        st = self.status
        current = self.identity_of(st)
        if key == current:
            return
        self.user_touched["identity"] = time.time()
        paired = st["service"]["paired"]
        mac = paired["name"] or "your Mac"
        new_label = self.label_of(st, key)
        body = ("PadBridge stops, restarts Bluetooth on this laptop"
                + (f" and forgets {mac}" if paired["address"] else "")
                + ". Then your Mac has to forget the controller and connect again. PadBridge shows you how.")
        dialog = Adw.AlertDialog(heading=f"Appear as {new_label}?", body=body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("switch", "Switch")
        dialog.set_response_appearance("switch", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("switch")
        dialog.set_close_response("cancel")

        def response(_d, rid):
            if rid == "switch":
                self.apply_identity(key)
            else:
                self.user_touched.pop("identity", None)
                self.syncing = True
                if current in self.identity_rows:
                    self.identity_rows[current][1].set_active(True)
                self.syncing = False
        dialog.connect("response", response)
        dialog.present(self)

    def apply_identity(self, key):
        st = self.status
        # Remember what the Mac knows the device as now, to tell the user what to forget afterwards.
        self._repair_guide = {"identity": key, "old_name": self.bt_name_of(st) if st else None,
                              "old_identity": self.identity_of(st) if st else None, "time": time.time()}
        self._pending_identity = key
        self.user_touched["identity"] = time.time() + 3
        self.command("apply_identity", identity=key)

    def mac_side_name(self, old_identity=None):
        """How the Mac lists the device: the Bluetooth name it cached when it paired. macOS renames a DualShock 4
        to "DUALSHOCK 4 Wireless Controller" and keeps that until the device is forgotten."""
        st = self.status
        name = self.bt_name_of(st) if st else "the controller"
        guide = getattr(self, "_repair_guide", None) or {}
        old = old_identity or guide.get("old_identity")
        name = guide.get("old_name") or name
        if old == "ds4" or (st and self.identity_of(st) == "ds4" and not old):
            return f"{name}” or “DUALSHOCK 4 Wireless Controller"
        return name

    def show_repair_dialog(self, guide):
        st = self.status
        new_name = self.bt_name_of(st, guide["identity"])
        old_name = self.mac_side_name(guide.get("old_identity"))
        steps = ["On your Mac, open System Settings, then Bluetooth.",
                 f"Click the Info button next to “{old_name}”, then click Forget This Device.",
                 "Come back here and click Start bridging.",
                 f"On your Mac, find “{new_name}” under Nearby Devices and click Connect."]
        dialog = Adw.AlertDialog(
            heading="Forget the controller on your Mac",
            body=f"PadBridge now appears as {self.label_of(st, guide['identity'])}. It stays stopped until your "
                 "Mac forgets the old device, otherwise the Mac reconnects to it right away. "
                 "macOS keeps the old name until it forgets the device.")
        dialog.set_extra_child(steps_list(steps))
        dialog.add_response("later", "Not now")
        dialog.add_response("start", "Start bridging")
        dialog.set_response_appearance("start", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("start")
        dialog.set_close_response("later")
        dialog.connect("response", lambda _d, rid: rid == "start" and self.command(
            "start", toast=f"Now connect “{new_name}” from your Mac"))
        dialog.present(self)

    def confirm_forget(self):
        if not self.status:
            return
        paired = self.status["service"]["paired"]
        name = paired["name"] or paired["address"] or "this Mac"
        dialog = Adw.AlertDialog(
            heading="Forget this Mac?",
            body=f"{name} stops getting controller input until it pairs again. On the Mac, also forget "
                 f"“{self.bt_name_of(self.status)}” in Bluetooth settings.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("forget", "Forget Mac")
        dialog.set_response_appearance("forget", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, rid: rid == "forget" and self.command(
            "forget", toast=f"Forgot {name}"))
        dialog.present(self)

    def show_pair_dialog(self):
        if not self.status:
            return
        paired = self.status["service"]["paired"]
        steps = ["On the Mac, open System Settings, then Bluetooth.",
                 f"Under Nearby Devices, find “{self.bt_name_of(self.status)}” and click Connect.",
                 "PadBridge accepts the Mac on its own. No code is needed."]
        if paired["address"]:
            name = paired["name"] or paired["address"]
            dialog = Adw.AlertDialog(heading="Pair a different Mac?",
                                     body=f"PadBridge pairs with one Mac at a time, so it forgets {name} first.")
            dialog.set_extra_child(steps_list(steps))
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("pair", "Forget and pair")
            dialog.set_response_appearance("pair", Adw.ResponseAppearance.SUGGESTED)
            dialog.set_close_response("cancel")
            dialog.connect("response", lambda _d, rid: rid == "pair" and self.command(
                "forget", toast=f"Forgot {name}. Connect from the new Mac."))
        else:
            dialog = Adw.AlertDialog(heading="Pair your Mac")
            dialog.set_extra_child(steps_list(steps))
            dialog.add_response("close", "Done")
            dialog.set_close_response("close")
        dialog.present(self)

    def on_close(self, *_):
        self.link.stop()
        return False

    def debug_info(self):
        if not self.status:
            return "Not connected to the service."
        st = {k: v for k, v in self.status.items() if k != "log"}
        if st.get("engine"):
            st["engine"] = {k: v for k, v in st["engine"].items() if k != "log"}
        return json.dumps(st, indent=2)
