"""PadBridge GUI entry point (GTK 4 + libadwaita)."""
import os
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Adw, Gdk, Gio, Gtk  # noqa: E402

from .. import APP_ID, VERSION  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIRS = [os.path.join(os.path.dirname(os.path.dirname(HERE)), "data"), "/opt/padbridge/data"]


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.window = None
        for name, cb, accels in (("about", self.on_about, []), ("quit", lambda *_: self.quit(), ["<Control>q"])):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", cb)
            self.add_action(action)
            if accels:
                self.set_accels_for_action(f"app.{name}", accels)

    def do_startup(self):
        Adw.Application.do_startup(self)
        display = Gdk.Display.get_default()
        css = Gtk.CssProvider()
        css.load_from_path(os.path.join(HERE, "style.css"))
        Gtk.StyleContext.add_provider_for_display(display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        theme = Gtk.IconTheme.get_for_display(display)
        for d in DATA_DIRS:
            if os.path.isdir(d):
                theme.add_search_path(d)
        self.set_accels_for_action("window.close", ["<Control>w"])

    def do_activate(self):
        from .window import Window
        if not self.window:
            self.window = Window(self)
        self.window.present()

    def on_about(self, *_):
        about = Adw.AboutDialog(
            application_name="PadBridge",
            application_icon=APP_ID,
            developer_name="PadBridge contributors",
            version=VERSION,
            comments="Use this laptop as a Bluetooth game controller for your Mac. Plug a wired controller "
                     "into the laptop and your Mac sees an Xbox Wireless Controller or a DualShock 4.",
            license_type=Gtk.License.CUSTOM,
            license="Personal project. No warranty.",
        )
        if self.window:
            about.set_debug_info(self.window.debug_info())
            about.set_debug_info_filename("padbridge-status.json")
        about.present(self.window)


def main():
    return App().run(sys.argv)
