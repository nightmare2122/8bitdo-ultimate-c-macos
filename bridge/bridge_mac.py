#!/usr/bin/env python3
"""Mac side of the 8BitDo bridge: receive controller state from bridge_linux.py, turn it into input.

macOS does not let an unsigned program create a virtual gamepad (IOHIDUserDevice needs an
entitlement only Apple grants), so this maps the controller to keyboard keys and the mouse,
using the layout in bridge_mapping.json. Edit that file to change the layout.

Usage:
    python3 bridge_mac.py                 map the controller to keyboard/mouse
    python3 bridge_mac.py --monitor       only show what arrives (no input is sent)
    python3 bridge_mac.py --dry-run       show the keys/clicks it would send, without sending them
Options:  --port 47800   --map other_mapping.json
Python standard library only. Posting input needs Accessibility permission for the app running
this script (System Settings > Privacy & Security > Accessibility > Terminal, or your terminal app).
"""
import argparse
import ctypes
import ctypes.util
import json
import math
import os
import select
import socket
import struct
import sys
import time

PORT = 47800
MAGIC = b"8BDB"
VERSION = 1
PACKET = struct.Struct("<4sBIIhhhhHH")  # must match bridge_linux.py
BUTTONS = ["A", "B", "X", "Y", "LB", "RB", "View", "Menu", "Guide", "LS", "RS",
           "Up", "Down", "Left", "Right", "Share"]
BIT = {name: 1 << i for i, name in enumerate(BUTTONS)}
TICK_S = 1 / 120     # mouse movement / scrolling update rate
LINK_TIMEOUT_S = 0.5  # sender sends at least every 50 ms; after this long, let go of everything
RESYNC_S = 1.0        # after this long without an accepted packet, accept any sequence number

DEFAULT_MAP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge_mapping.json")
DEFAULT_MAP = {
    "buttons": {"A": "space", "B": "escape", "X": "r", "Y": "e", "LB": "q", "RB": "f",
                "View": "tab", "Menu": "return", "Guide": None, "LS": "shift", "RS": "mouse:middle",
                "Up": "up", "Down": "down", "Left": "left", "Right": "right", "Share": None},
    "triggers": {"LT": "mouse:right", "RT": "mouse:left"},
    "left_stick": "wasd",
    "right_stick": "mouse",
    "mouse_speed": 1500,
    "scroll_speed": 1200,
    "deadzone": 0.15,
    "stick_key_threshold": 0.5,
    "trigger_threshold": 0.35,
}

# macOS virtual key codes (US layout positions).
KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11,
    "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21,
    "6": 22, "5": 23, "=": 24, "9": 25, "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31,
    "u": 32, "[": 33, "i": 34, "p": 35, "return": 36, "l": 37, "j": 38, "'": 39, "k": 40,
    ";": 41, "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46, ".": 47, "tab": 48, "space": 49,
    "`": 50, "delete": 51, "escape": 53, "command": 55, "shift": 56, "capslock": 57,
    "option": 58, "control": 59, "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96,
    "f6": 97, "f7": 98, "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
    "home": 115, "pageup": 116, "forwarddelete": 117, "end": 119, "pagedown": 121,
    "left": 123, "right": 124, "down": 125, "up": 126,
}
MODIFIER_FLAGS = {"shift": 0x20000, "control": 0x40000, "option": 0x80000, "command": 0x100000}
STICK_KEYS = {"wasd": ("w", "s", "a", "d"), "arrows": ("up", "down", "left", "right")}
MOUSE_BUTTONS = {  # name: (button number, down type, up type, dragged type)
    "left": (0, 1, 2, 6), "right": (1, 3, 4, 7), "middle": (2, 25, 26, 27),
}
kCGEventMouseMoved, kCGEventFlagsChanged = 5, 12
kCGMouseEventClickState, kCGMouseEventDeltaX, kCGMouseEventDeltaY = 1, 4, 5
kCGHIDEventTap, kCGScrollEventUnitPixel = 0, 0


class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class CGRect(ctypes.Structure):
    _fields_ = [("origin", CGPoint), ("size", CGPoint)]


def load_quartz():
    cg = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
    cf = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
    vp = ctypes.c_void_p
    sigs = {
        "CGEventCreate": (vp, [vp]),
        "CGEventGetLocation": (CGPoint, [vp]),
        "CGEventCreateKeyboardEvent": (vp, [vp, ctypes.c_uint16, ctypes.c_bool]),
        "CGEventCreateMouseEvent": (vp, [vp, ctypes.c_uint32, CGPoint, ctypes.c_uint32]),
        "CGEventCreateScrollWheelEvent2": (vp, [vp, ctypes.c_uint32, ctypes.c_uint32,
                                                ctypes.c_int32, ctypes.c_int32, ctypes.c_int32]),
        "CGEventSetIntegerValueField": (None, [vp, ctypes.c_uint32, ctypes.c_int64]),
        "CGEventSetType": (None, [vp, ctypes.c_uint32]),
        "CGEventSetFlags": (None, [vp, ctypes.c_uint64]),
        "CGEventPost": (None, [ctypes.c_uint32, vp]),
        "CGGetActiveDisplayList": (ctypes.c_int32, [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
                                                    ctypes.POINTER(ctypes.c_uint32)]),
        "CGDisplayBounds": (CGRect, [ctypes.c_uint32]),
        "AXIsProcessTrusted": (ctypes.c_bool, []),
    }
    for name, (res, args) in sigs.items():
        fn = getattr(cg, name)
        fn.restype, fn.argtypes = res, args
    cf.CFRelease.argtypes = [vp]
    return cg, cf


class QuartzOutput:
    """Posts real keyboard and mouse events."""

    def __init__(self):
        self.cg, self.cf = load_quartz()
        self.flags = 0
        self.mouse_held = []  # names of mouse buttons currently down, oldest first

    def accessibility_ok(self):
        return self.cg.AXIsProcessTrusted()

    def _post(self, ev):
        self.cg.CGEventSetFlags(ev, self.flags)
        self.cg.CGEventPost(kCGHIDEventTap, ev)
        self.cf.CFRelease(ev)

    def _cursor(self):
        ev = self.cg.CGEventCreate(None)
        p = self.cg.CGEventGetLocation(ev)
        self.cf.CFRelease(ev)
        return p

    def _desktop(self):
        ids = (ctypes.c_uint32 * 16)()
        n = ctypes.c_uint32()
        self.cg.CGGetActiveDisplayList(16, ids, ctypes.byref(n))
        rects = [self.cg.CGDisplayBounds(ids[i]) for i in range(n.value)]
        return [(r.origin.x, r.origin.y, r.origin.x + r.size.x - 1, r.origin.y + r.size.y - 1) for r in rects]

    def key(self, name, down):
        ev = self.cg.CGEventCreateKeyboardEvent(None, KEYCODES[name], down)
        if name in MODIFIER_FLAGS:
            if down:
                self.flags |= MODIFIER_FLAGS[name]
            else:
                self.flags &= ~MODIFIER_FLAGS[name]
            self.cg.CGEventSetType(ev, kCGEventFlagsChanged)
        self._post(ev)

    def mouse_button(self, name, down):
        number, down_type, up_type, _ = MOUSE_BUTTONS[name]
        if down:
            self.mouse_held.append(name)
        elif name in self.mouse_held:
            self.mouse_held.remove(name)
        ev = self.cg.CGEventCreateMouseEvent(None, down_type if down else up_type, self._cursor(), number)
        self.cg.CGEventSetIntegerValueField(ev, kCGMouseEventClickState, 1)
        self._post(ev)

    def move(self, dx, dy):
        p = self._cursor()
        x, y = p.x + dx, p.y + dy
        # Keep the cursor on a screen: clamp to the display it is closest to.
        best = None
        for left, top, right, bottom in self._desktop():
            cx, cy = min(max(x, left), right), min(max(y, top), bottom)
            d = (cx - x) ** 2 + (cy - y) ** 2
            if best is None or d < best[0]:
                best = (d, cx, cy)
        if best:
            x, y = best[1], best[2]
        if self.mouse_held:
            number, _, _, ev_type = MOUSE_BUTTONS[self.mouse_held[0]]
        else:
            number, ev_type = 0, kCGEventMouseMoved
        ev = self.cg.CGEventCreateMouseEvent(None, ev_type, CGPoint(x, y), number)
        # Games that read relative motion use the delta fields, not the position.
        self.cg.CGEventSetIntegerValueField(ev, kCGMouseEventDeltaX, int(dx))
        self.cg.CGEventSetIntegerValueField(ev, kCGMouseEventDeltaY, int(dy))
        self._post(ev)

    def scroll(self, vertical, horizontal):
        self._post(self.cg.CGEventCreateScrollWheelEvent2(
            None, kCGScrollEventUnitPixel, 2, vertical, horizontal, 0))


class PrintOutput:
    """Same interface as QuartzOutput, but only prints (for --dry-run)."""

    def __init__(self):
        self.moved = [0, 0]
        self.scrolled = [0, 0]
        self.last_report = time.monotonic()

    def accessibility_ok(self):
        return True

    def key(self, name, down):
        print(f"key {name} {'down' if down else 'up'}")

    def mouse_button(self, name, down):
        print(f"mouse {name} {'down' if down else 'up'}")

    def _report(self):
        now = time.monotonic()
        if now - self.last_report >= 0.5:
            if any(self.moved):
                print(f"mouse moved {self.moved[0]:+d},{self.moved[1]:+d} px in the last 0.5 s")
            if any(self.scrolled):
                print(f"scrolled {self.scrolled[0]:+d} vertical, {self.scrolled[1]:+d} horizontal px")
            self.moved, self.scrolled, self.last_report = [0, 0], [0, 0], now

    def move(self, dx, dy):
        self.moved[0] += dx
        self.moved[1] += dy
        self._report()

    def scroll(self, vertical, horizontal):
        self.scrolled[0] += vertical
        self.scrolled[1] += horizontal
        self._report()


class Mapper:
    """Turns controller state into key/mouse actions, sending only what changed."""

    def __init__(self, mapping, out):
        self.m = mapping
        self.out = out
        self.active = set()  # actions currently held down, e.g. "key:w", "mouse:left"
        self.buttons = 0
        self.axes = (0, 0, 0, 0, 0, 0)
        self.carry = [0.0, 0.0, 0.0, 0.0]  # sub-pixel remainders: mouse x/y, scroll v/h

    @staticmethod
    def _action(spec):
        if not spec:
            return None
        return spec if spec.startswith("mouse:") else "key:" + spec

    def _wanted(self):
        want = set()
        for name, spec in self.m["buttons"].items():
            if self.buttons & BIT[name] and self._action(spec):
                want.add(self._action(spec))
        lx, ly, rx, ry, lt, rt = self.axes
        thr = self.m["trigger_threshold"] * 65535
        for value, spec in ((lt, self.m["triggers"].get("LT")), (rt, self.m["triggers"].get("RT"))):
            if value > thr and self._action(spec):
                want.add(self._action(spec))
        for mode, x, y in ((self.m["left_stick"], lx, ly), (self.m["right_stick"], rx, ry)):
            if mode in STICK_KEYS:
                up, down, left, right = STICK_KEYS[mode]
                t = self.m["stick_key_threshold"] * 32767
                for pressed, k in ((y < -t, up), (y > t, down), (x < -t, left), (x > t, right)):
                    if pressed:
                        want.add("key:" + k)
        return want

    def _press(self, action, down):
        kind, name = action.split(":", 1)
        if kind == "mouse":
            self.out.mouse_button(name, down)
        else:
            self.out.key(name, down)

    def update(self, buttons, axes):
        self.buttons, self.axes = buttons, axes
        want = self._wanted()
        # Release first, and press modifiers before other keys, so combos come out in the right order.
        for action in sorted(self.active - want):
            self._press(action, False)
        for action in sorted(want - self.active, key=lambda a: a.split(":", 1)[1] not in MODIFIER_FLAGS):
            self._press(action, True)
        self.active = want

    def release_all(self):
        self.update(0, (0, 0, 0, 0, 0, 0))

    def _curve(self, x, y):
        """Stick position -> (-1..1, -1..1) with deadzone removed and a squared response."""
        x, y = x / 32767, y / 32767
        mag = math.hypot(x, y)
        dz = self.m["deadzone"]
        if mag <= dz:
            return 0.0, 0.0
        scaled = min(1.0, (mag - dz) / (1 - dz)) ** 2
        return x / mag * scaled, y / mag * scaled

    def tick(self, dt):
        lx, ly, rx, ry = self.axes[:4]
        for mode, x, y in ((self.m["left_stick"], lx, ly), (self.m["right_stick"], rx, ry)):
            if mode == "mouse":
                vx, vy = self._curve(x, y)
                self.carry[0] += vx * self.m["mouse_speed"] * dt
                self.carry[1] += vy * self.m["mouse_speed"] * dt
                dx, dy = int(self.carry[0]), int(self.carry[1])
                if dx or dy:
                    self.carry[0] -= dx
                    self.carry[1] -= dy
                    self.out.move(dx, dy)
            elif mode == "scroll":
                vx, vy = self._curve(x, y)
                # Stick up scrolls up: wheel values are positive away from the user.
                self.carry[2] += -vy * self.m["scroll_speed"] * dt
                self.carry[3] += -vx * self.m["scroll_speed"] * dt
                v, h = int(self.carry[2]), int(self.carry[3])
                if v or h:
                    self.carry[2] -= v
                    self.carry[3] -= h
                    self.out.scroll(v, h)


def load_mapping(path):
    mapping = json.loads(json.dumps(DEFAULT_MAP))
    if os.path.exists(path):
        with open(path) as f:
            user = json.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(mapping.get(k), dict):
                mapping[k].update(v)
            elif not k.startswith("_"):
                mapping[k] = v
    elif path != DEFAULT_MAP_PATH:
        sys.exit(f"mapping file not found: {path}")
    problems = []
    for spec in list(mapping["buttons"].values()) + list(mapping["triggers"].values()):
        if not spec:
            continue
        if spec.startswith("mouse:"):
            if spec[6:] not in MOUSE_BUTTONS:
                problems.append(f"unknown mouse button '{spec}' (use mouse:left, mouse:right, mouse:middle)")
        elif spec not in KEYCODES:
            problems.append(f"unknown key '{spec}'")
    for side in ("left_stick", "right_stick"):
        if mapping[side] not in ("wasd", "arrows", "mouse", "scroll", "none"):
            problems.append(f"{side} must be wasd, arrows, mouse, scroll or none")
    unknown = set(mapping["buttons"]) - set(BUTTONS)
    if unknown:
        problems.append(f"unknown button names {sorted(unknown)}; valid: {BUTTONS}")
    if problems:
        sys.exit("Problems in the mapping:\n  " + "\n  ".join(problems))
    return mapping


def describe(buttons, axes):
    held = " ".join(n for n in BUTTONS if buttons & BIT[n])
    lx, ly, rx, ry, lt, rt = axes
    return f"L({lx:6d},{ly:6d}) R({rx:6d},{ry:6d}) LT {lt:5d} RT {rt:5d}  {held:<40}"


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=PORT)
    p.add_argument("--map", default=DEFAULT_MAP_PATH, help="mapping JSON file")
    p.add_argument("--monitor", action="store_true", help="only display incoming state")
    p.add_argument("--dry-run", action="store_true", help="print actions instead of sending them")
    args = p.parse_args()

    mapper = None
    if not args.monitor:
        out = PrintOutput() if args.dry_run else QuartzOutput()
        if not out.accessibility_ok():
            print("WARNING: this app has no Accessibility permission, so macOS will silently drop the input.\n"
                  "  Allow it in System Settings > Privacy & Security > Accessibility (add your terminal app),\n"
                  "  then restart this script.")
        mapper = Mapper(load_mapping(args.map), out)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("", args.port))
    sock.setblocking(False)
    addrs = sorted({a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)} - {"127.0.0.1"})
    print(f"Listening on UDP port {args.port}. This Mac: {', '.join(addrs) or socket.gethostname()}")
    print("On Fedora run:  sudo python3 bridge_linux.py <one of those addresses>   Ctrl-C to stop.")

    last_seq = None
    sender = None
    last_accept = 0.0
    connected = False
    next_tick = time.monotonic()
    try:
        while True:
            now = time.monotonic()
            ready, _, _ = select.select([sock], [], [], max(0.0, next_tick - now))
            if ready:
                while True:
                    try:
                        data, addr = sock.recvfrom(64)
                    except BlockingIOError:
                        break
                    if len(data) != PACKET.size or data[:4] != MAGIC:
                        continue
                    _, version, seq, buttons, *axes = PACKET.unpack(data)
                    if version != VERSION:
                        print(f"\nIgnoring packet with protocol version {version} (expected {VERSION}).")
                        continue
                    now = time.monotonic()
                    fresh = (seq == 0 or last_seq is None or addr[0] != sender
                             or now - last_accept > RESYNC_S
                             or 0 < (seq - last_seq) % 2**32 < 2**31)
                    if not fresh:
                        continue  # arrived out of order; a newer state was already applied
                    last_seq = None if seq == 0 else seq
                    last_accept = now
                    if not connected or addr[0] != sender:
                        print(f"\nReceiving from {addr[0]}")
                        connected = True
                    sender = addr[0]
                    axes = tuple(axes)
                    if mapper:
                        mapper.update(buttons, axes)
                    if args.monitor:
                        print("\r" + describe(buttons, axes), end="", flush=True)
            now = time.monotonic()
            if connected and now - last_accept > LINK_TIMEOUT_S:
                print(f"\nNo data from {sender} for {LINK_TIMEOUT_S} s; released all keys. Waiting ...")
                connected = False
                if mapper:
                    mapper.release_all()
            if now >= next_tick:
                if mapper:
                    mapper.tick(TICK_S)
                next_tick = max(next_tick + TICK_S, now)  # do not try to catch up after a stall
    except KeyboardInterrupt:
        print()
    finally:
        if mapper:
            mapper.release_all()


if __name__ == "__main__":
    main()
