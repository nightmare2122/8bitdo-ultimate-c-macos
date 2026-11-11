"""Live drawing of the controller's input: sticks, triggers, bumpers, D-pad, face and menu buttons."""
import math

import cairo  # noqa: F401  (registers cairo types for append_cairo)
from gi.repository import Adw, Graphene, Gtk, Pango, PangoCairo

# Design canvas; the drawing scales to the widget and stays centred.
W, H = 520.0, 224.0
FACE = {"xbox": {"A": "A", "B": "B", "X": "X", "Y": "Y"},
        "ds4": {"A": "✕", "B": "○", "X": "□", "Y": "△"}}
SMOOTH_TAU = 0.08      # seconds; softens the engine's coarse updates without visible lag


class InputView(Gtk.Widget):
    __gtype_name__ = "PadBridgeInputView"

    def __init__(self):
        super().__init__()
        self.set_hexpand(True)
        self.target = {"lx": 0.0, "ly": 0.0, "rx": 0.0, "ry": 0.0, "lt": 0.0, "rt": 0.0}
        self.shown = dict(self.target)
        self.buttons = set()
        self.live = False
        self.mode = "xbox"
        self.last_frame = None
        self.tick_id = None
        self.update_property([Gtk.AccessibleProperty.LABEL], ["Live controller input"])

    def set_input(self, data, live, mode):
        data = data or {}
        self.live = live
        self.mode = mode if mode in FACE else "xbox"
        for k in ("lx", "ly", "rx", "ry"):
            self.target[k] = max(-1.0, min(1.0, (data.get(k) or 0) / 32767.0)) if live else 0.0
        for k in ("lt", "rt"):
            self.target[k] = max(0.0, min(1.0, (data.get(k) or 0) / 65535.0)) if live else 0.0
        self.buttons = set(data.get("buttons") or []) if live else set()
        animate = Gtk.Settings.get_default().get_property("gtk-enable-animations")
        if not animate:
            self.shown = dict(self.target)
            self.queue_draw()
        elif self.tick_id is None:
            self.last_frame = None
            self.tick_id = self.add_tick_callback(self._tick)

    def _tick(self, _widget, clock):
        t = clock.get_frame_time() / 1e6
        dt = 0.016 if self.last_frame is None else max(0.0, t - self.last_frame)
        self.last_frame = t
        a = 1 - math.exp(-dt / SMOOTH_TAU)
        done = True
        for k, v in self.target.items():
            cur = self.shown[k] + (v - self.shown[k]) * a
            if abs(cur - v) < 0.002:
                cur = v
            else:
                done = False
            self.shown[k] = cur
        self.queue_draw()
        if done:
            self.tick_id = None
            return False
        return True

    # ------------------------------------------------------------------ size: keep the canvas aspect
    def do_get_request_mode(self):
        return Gtk.SizeRequestMode.HEIGHT_FOR_WIDTH

    def do_measure(self, orientation, for_size):
        if orientation == Gtk.Orientation.HORIZONTAL:
            return 280, 560, -1, -1
        if for_size <= 0:
            return 150, 230, -1, -1
        h = int(max(150, min(260, for_size * H / W)))
        return h, h, -1, -1

    # ------------------------------------------------------------------ drawing
    def do_snapshot(self, snapshot):
        w, h = self.get_width(), self.get_height()
        rect = Graphene.Rect().init(0, 0, w, h)
        cr = snapshot.append_cairo(rect)
        self.draw(cr, w, h)

    def draw(self, cr, w, h):
        fg = self.get_color()
        try:
            acc = Adw.StyleManager.get_default().get_accent_color_rgba()
        except AttributeError:
            acc = None
        accent = (acc.red, acc.green, acc.blue) if acc else (0.21, 0.52, 0.89)
        f = (fg.red, fg.green, fg.blue)
        s = min(w / W, h / H)
        cr.translate((w - W * s) / 2, (h - H * s) / 2)
        cr.scale(s, s)
        dim = 1.0 if self.live else 0.55

        well = (*f, 0.07 * dim)
        edge = (*f, 0.16 * dim)
        label = (*f, 0.55 * dim)
        on_accent = (1, 1, 1, 1)

        def fill(rgba):
            cr.set_source_rgba(*rgba)

        def rounded(x, y, rw, rh, r):
            cr.new_path()
            cr.arc(x + rw - r, y + r, r, -math.pi / 2, 0)
            cr.arc(x + rw - r, y + rh - r, r, 0, math.pi / 2)
            cr.arc(x + r, y + rh - r, r, math.pi / 2, math.pi)
            cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
            cr.close_path()

        def text(t, cx, cy, size, rgba, bold=False):
            layout = PangoCairo.create_layout(cr)
            desc = Pango.FontDescription.from_string(f"Sans {'Bold ' if bold else ''}{size}")
            layout.set_font_description(desc)
            layout.set_text(t, -1)
            _ink, logical = layout.get_pixel_extents()
            fill(rgba)
            cr.new_path()
            cr.move_to(cx - logical.width / 2, cy - logical.height / 2)
            PangoCairo.show_layout(cr, layout)
            cr.new_path()

        def button_shape(pressed):
            if pressed:
                fill((*accent, 1))
                cr.fill()
            else:
                fill(well)
                cr.fill_preserve()
                fill(edge)
                cr.set_line_width(1)
                cr.stroke()

        def pill(name, x, y, pw, ph, caption=None):
            pressed = name in self.buttons
            rounded(x, y, pw, ph, ph / 2)
            button_shape(pressed)
            text(caption or name, x + pw / 2, y + ph / 2, 8.5, on_accent if pressed else label, bold=pressed)

        def trigger(name, value, x, y, tw):
            text(name, x - 14 if x < W / 2 else x + tw + 14, y + 4, 8.5, label)
            rounded(x, y, tw, 8, 4)
            fill(well)
            cr.fill_preserve()
            fill(edge)
            cr.set_line_width(1)
            cr.stroke()
            if value > 0.01:
                vw = max(8, tw * value)
                rounded(x if x < W / 2 else x + tw - vw, y, vw, 8, 4)
                fill((*accent, 1))
                cr.fill()

        def stick(name, cx, cy, r, vx, vy):
            cr.new_path()
            cr.arc(cx, cy, r, 0, 2 * math.pi)
            fill(well)
            cr.fill_preserve()
            fill(edge)
            cr.set_line_width(1)
            cr.stroke()
            # cross-hair
            fill((*f, 0.10 * dim))
            cr.new_path()
            cr.move_to(cx - r + 8, cy)
            cr.line_to(cx + r - 8, cy)
            cr.move_to(cx, cy - r + 8)
            cr.line_to(cx, cy + r - 8)
            cr.stroke()
            pressed = name in self.buttons
            travel = r - 14
            dx, dy = cx + vx * travel, cy + vy * travel
            cr.new_path()
            if self.live:
                cr.arc(dx, dy, 12, 0, 2 * math.pi)
                fill((*accent, 1) if pressed else (*accent, 0.85))
                cr.fill()
                if pressed:
                    cr.new_path()
                    cr.arc(dx, dy, 16, 0, 2 * math.pi)
                    fill((*accent, 0.35))
                    cr.set_line_width(3)
                    cr.stroke()
            else:
                cr.arc(cx, cy, 12, 0, 2 * math.pi)
                fill(edge)
                cr.fill()
            text(name, cx, cy + r + 13, 8.5, label)

        sh = self.shown
        # shoulders
        trigger("LT", sh["lt"], 40, 10, 124)
        trigger("RT", sh["rt"], W - 40 - 124, 10, 124)
        pill("LB", 40, 26, 124, 20)
        pill("RB", W - 40 - 124, 26, 124, 20)

        # Xbox layout: left stick high, D-pad low; face buttons high, right stick low
        stick("LS", 102, 104, 42, sh["lx"], sh["ly"])
        stick("RS", 332, 166, 36, sh["rx"], sh["ry"])
        dcx, dcy, arm = 188, 166, 16
        for name, ox, oy in (("Up", 0, -1), ("Down", 0, 1), ("Left", -1, 0), ("Right", 1, 0)):
            rounded(dcx + ox * arm - arm / 2, dcy + oy * arm - arm / 2, arm, arm, 4)
            button_shape(name in self.buttons)
        rounded(dcx - arm / 2, dcy - arm / 2, arm, arm, 2)
        fill(well)
        cr.fill()

        fcx, fcy, sp = 418, 104, 26
        letters = FACE[self.mode]
        for name, ox, oy in (("Y", 0, -1), ("B", 1, 0), ("A", 0, 1), ("X", -1, 0)):
            pressed = name in self.buttons
            cr.new_path()
            cr.arc(fcx + ox * sp, fcy + oy * sp, 13, 0, 2 * math.pi)
            button_shape(pressed)
            text(letters[name], fcx + ox * sp, fcy + oy * sp, 9, on_accent if pressed else label, bold=True)

        # centre buttons
        pressed = "Guide" in self.buttons
        cr.new_path()
        cr.arc(W / 2, 74, 15, 0, 2 * math.pi)
        button_shape(pressed)
        text("PS" if self.mode == "ds4" else "Guide", W / 2, 74, 7, on_accent if pressed else label, bold=pressed)
        pill("View", W / 2 - 50, 98, 40, 16, "Share" if self.mode == "ds4" else "View")
        pill("Menu", W / 2 + 10, 98, 40, 16, "Options" if self.mode == "ds4" else "Menu")
        pill("Share", W / 2 - 20, 124, 40, 16, "Pad" if self.mode == "ds4" else "Share")
