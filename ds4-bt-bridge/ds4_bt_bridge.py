#!/usr/bin/env python3
"""Make a Linux laptop pose as a Sony DualShock 4 over Bluetooth, driven by the 8BitDo plugged into it.

    8BitDo ──USB──▶ Linux laptop (xpad) ──Bluetooth HID──▶ Mac sees "Wireless Controller" (DualShock 4)

macOS supports the DualShock 4 natively, so games, Steam and the Game Controller framework see a real
controller, not keyboard and mouse.

One-time setup:   sudo ./ds4_bt_setup.sh        (undo with: sudo ./ds4_bt_setup.sh --undo)
Run:              sudo python3 ds4_bt_bridge.py [-v]
Then on the Mac:  System Settings > Bluetooth > "Wireless Controller" > Connect.
After the first pairing the bridge reconnects to that Mac by itself.

Needs bridge_linux.py in the same folder (it reads the controller), plus python3-dbus and python3-gobject.
Status: experimental. Every request the Mac makes on the HID control channel is printed, so if macOS
asks for something this script does not answer yet, the log shows what.
"""
import argparse
import binascii
import collections
import json
import os
import select
import socket
import struct
import sys
import threading
import time

try:
    import dbus
    import dbus.service
    import dbus.mainloop.glib
    from gi.repository import GLib
except ImportError:
    sys.exit("Missing python3-dbus or python3-gobject. Run: sudo ./ds4_bt_setup.sh")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bridge_linux import (BIT, EV_SYN, EVIOCGRAB, INPUT_EVENT, State,  # noqa: E402
                          find_event_device, read_ranges)

import fcntl  # noqa: E402
import termios  # noqa: E402

EV_FF, FF_RUMBLE = 0x15, 0x50
FF_EFFECT = struct.Struct("=HhHHHHH2xHH28x")  # struct ff_effect (48 bytes) with the rumble member of the union
EVIOCSFF = (1 << 30) | (FF_EFFECT.size << 16) | (ord("E") << 8) | 0x80
KEEPALIVE_S = 0.004      # 250 Hz continuous stream, like a real DualShock 4: a quiet link lets the Mac drop it
                         # into sniff mode (15 ms interval), and every exit from sniff adds latency
SNDBUF = 4096            # tiny socket buffer: a stale backlog can never build up behind a button press
STATS_WINDOW_S = 0.5     # stats window, matches the status file's write interval
MAX_INFLIGHT = 2         # --inflight: our packets allowed inside the Bluetooth controller at once (0 = no limit).
                         # Measured (input -> radio-confirmed, p50/p99): 0 = 11.4/51 ms (radio buffers 6 stale
                         # reports), 1 = 7.9/58 ms (half the report rate), 2 = 7.4/48 ms: best of the three.

PSM_CTRL, PSM_INTR = 0x11, 0x13
HOST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ds4_bt_host.txt")
HID_UUID = "00001124-0000-1000-8000-00805f9b34fb"
AGENT_PATH = "/ds4bridge/agent"
PROFILE_PATH = "/ds4bridge/profile"
VERBOSE = False

# Live state for the host app's background service (see --status-file). Keys are part of the app contract.
STATUS = {
    "version": 1, "pid": os.getpid(), "started_at": time.time(), "updated_at": 0.0,
    "mode": None, "name": None, "adapter": None,
    "controller": {"connected": False, "path": None},
    "host": {"address": None, "connected": False, "since": None},
    "link": {"handle": None, "sniff_disabled": False},
    "stats": {"reports_per_s": 0.0, "max_queue_bytes": 0, "deferred": 0, "input_age_ms": None, "pings": 0},
    "rumble": {"strong": 0, "weak": 0},
    "input": {"lx": 0, "ly": 0, "rx": 0, "ry": 0, "lt": 0, "rt": 0, "buttons": []},
    "log": [],
    # Addendum A: the emulated identity, the ones available, and the Bluetooth name macOS is given
    "identity": None, "identities": [], "bt_name": None,
}
LOG_RING = collections.deque(maxlen=200)


def log(msg):
    line = time.strftime("%H:%M:%S ") + msg
    LOG_RING.append(line)
    print(line, flush=True)


HCI_INDEX = 0  # adapter index for raw HCI sockets, set by setup_bluez
PAD = None   # the Pad being read (set by the worker), so every status write carries the live input


def input_snapshot(st):
    """STATUS["input"] for a controller State (contract keys)."""
    a = st.axes
    return {**{k: a[k] for k in ("lx", "ly", "rx", "ry", "lt", "rt")},
            "buttons": [n for n, bit in BIT.items() if st.buttons & bit]}


def status_writer(path):
    """Write STATUS as JSON to `path` twice a second (atomic rename), for the host app's service.
    The input view is refreshed on every write; the stats are a rolling 0.5 s window (see run_session)."""
    def loop():
        while True:
            STATUS["updated_at"] = time.time()
            if PAD is not None:
                STATUS["input"] = input_snapshot(PAD.state)
            STATUS["log"] = list(LOG_RING)[-50:]
            tmp = path + ".tmp"
            try:
                with open(tmp, "w") as f:
                    json.dump(STATUS, f)
                os.chmod(tmp, 0o644)
                os.replace(tmp, path)
            except OSError:
                pass
            time.sleep(0.5)
    threading.Thread(target=loop, daemon=True).start()


# ---------------------------------------------------------------- HID report descriptor
def _vendor(report_id, usage, count, kind):
    # kind: 0x81 input, 0x91 output, 0xB1 feature
    return [0x85, report_id, 0x09, usage, 0x15, 0x00, 0x26, 0xFF, 0x00, 0x75, 0x08, 0x95, count, kind, 0x02]


# Feature reports a DualShock 4 answers over Bluetooth: report ID -> payload length (without the ID).
FEATURE_SIZES = {0x02: 36, 0x05: 40, 0x08: 3, 0x12: 15, 0x13: 22, 0xA3: 48}

REPORT_DESCRIPTOR = bytes(
    [0x05, 0x01, 0x09, 0x05, 0xA1, 0x01,                  # Generic Desktop / Game Pad / Application
     0x85, 0x01,                                            # report 0x01 ("basic" mode, 9 bytes)
     0x09, 0x30, 0x09, 0x31, 0x09, 0x32, 0x09, 0x35,        #   X Y Z Rz  (left stick, right stick)
     0x15, 0x00, 0x26, 0xFF, 0x00, 0x75, 0x08, 0x95, 0x04, 0x81, 0x02,
     0x09, 0x39, 0x15, 0x00, 0x25, 0x07, 0x35, 0x00, 0x46, 0x3B, 0x01, 0x65, 0x14,
     0x75, 0x04, 0x95, 0x01, 0x81, 0x42,                    #   hat switch, null state = 8
     0x65, 0x00,
     0x05, 0x09, 0x19, 0x01, 0x29, 0x0E, 0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x0E, 0x81, 0x02,  # 14 buttons
     0x06, 0x00, 0xFF, 0x09, 0x20, 0x75, 0x06, 0x95, 0x01, 0x81, 0x02,                                # counter
     0x05, 0x01, 0x09, 0x33, 0x09, 0x34, 0x15, 0x00, 0x26, 0xFF, 0x00, 0x75, 0x08, 0x95, 0x02, 0x81, 0x02,  # L2 R2
     0x06, 0x00, 0xFF]                                      # vendor page for the rest
    + _vendor(0x11, 0x21, 77, 0x81)                         # report 0x11 in: full Bluetooth input report
    + _vendor(0x11, 0x22, 77, 0x91)                         # report 0x11 out: rumble / light bar
    + sum((_vendor(rid, 0x24 + i, n, 0xB1) for i, (rid, n) in enumerate(FEATURE_SIZES.items())), [])
    + [0xC0])


# Xbox One S controller (model 1708, 045e:02fd, firmware 4.8) over Bluetooth Classic, "Windows mode" descriptor,
# as dumped by the xpadneo project (docs/descriptors/xb1s_windows.md). macOS supports this controller natively.
#   report 1 in:  LX LY RX RY (16 bit), LT RT (10 bit), hat (1-8, 0 = centred), 10 buttons
#   report 2 in:  Xbox button       report 3 out: rumble        report 4 in: battery
XBOX_DESCRIPTOR = bytes.fromhex("""
05 01 09 05 a1 01 85 01 09 01 a1 00 09 30 09 31 15 00 27 ff ff 00 00 95 02 75 10 81 02 c0 09 01 a1 00 09 33 09 34 15 00
27 ff ff 00 00 95 02 75 10 81 02 c0 05 01 09 32 15 00 26 ff 03 95 01 75 0a 81 02 15 00 25 00 75 06 95 01 81 03 05 01 09
35 15 00 26 ff 03 95 01 75 0a 81 02 15 00 25 00 75 06 95 01 81 03 05 01 09 39 15 01 25 08 35 00 46 3b 01 66 14 00 75 04
95 01 81 42 75 04 95 01 15 00 25 00 35 00 45 00 65 00 81 03 05 09 19 01 29 0a 15 00 25 01 75 01 95 0a 81 02 15 00 25 00
75 06 95 01 81 03 05 01 09 80 85 02 a1 00 09 85 15 00 25 01 95 01 75 01 81 02 15 00 25 00 75 07 95 01 81 03 c0 05 0f 09
21 85 03 a1 02 09 97 15 00 25 01 75 04 95 01 91 02 15 00 25 00 75 04 95 01 91 03 09 70 15 00 25 64 75 08 95 04 91 02 09
50 66 01 10 55 0e 15 00 26 ff 00 75 08 95 01 91 02 09 a7 15 00 26 ff 00 75 08 95 01 91 02 65 00 55 00 09 7c 15 00 26 ff
00 75 08 95 01 91 02 c0 85 04 05 06 09 20 15 00 26 ff 00 75 08 95 01 81 02 c0 00""")

# What macOS can be shown (contract Addendum A). "transport": classic = Bluetooth BR/EDR HID, le = HID over GATT.
# xbox-series (Xbox Series X|S over Bluetooth LE) is paused: see paused/ and the contract, Addendum A.
IDENTITIES = [
    {"id": "xbox-one", "label": "Xbox Wireless Controller (Xbox One S)", "transport": "classic",
     "vid": "045e", "pid": "02fd"},
    {"id": "ds4", "label": "DualShock 4", "transport": "classic", "vid": "054c", "pid": "09cc"},
]
MODE_ALIASES = {"xbox": "xbox-one"}
PROFILES = {
    "ds4": {"name": "Wireless Controller", "provider": "Sony Interactive Entertainment",
            "descriptor": REPORT_DESCRIPTOR, "vid": 0x054C, "pid": 0x09CC, "version": 0x0100},
    "xbox-one": {"name": "Xbox Wireless Controller", "provider": "Microsoft",
                 "descriptor": XBOX_DESCRIPTOR, "vid": 0x045E, "pid": 0x02FD, "version": 0x0903},
}
PAUSED_MODES = {"xbox-series": "xbox-series (Xbox Series X|S over Bluetooth LE) is paused; use xbox-one or ds4"}
MODE = "xbox-one"


def is_xbox():
    return MODE == "xbox-one"


def identity(mode):
    return next(i for i in IDENTITIES if i["id"] == mode)


def mode_from_main_conf():
    """Follow whatever identity ds4_bt_setup.sh gave the Bluetooth adapter (marker line, else DeviceID)."""
    try:
        lines = open("/etc/bluetooth/main.conf").read().splitlines()
    except OSError:
        return "xbox-one"
    for line in lines:
        if line.startswith("#PadBridgeIdentity") and "=" in line:
            m = line.split("=", 1)[1].strip()
            m = MODE_ALIASES.get(m, m)
            if m in PROFILES:
                return m
    for line in lines:
        s = line.strip().upper()
        if s.startswith("DEVICEID") and "054C" in s:
            return "ds4"
    return "xbox-one"


def sdp_record():
    prof = PROFILES[MODE]
    desc = prof["descriptor"].hex()
    return f"""<?xml version="1.0" encoding="UTF-8" ?>
<record>
  <attribute id="0x0001"><sequence><uuid value="0x1124"/></sequence></attribute>
  <attribute id="0x0004"><sequence>
    <sequence><uuid value="0x0100"/><uint16 value="0x0011"/></sequence>
    <sequence><uuid value="0x0011"/></sequence></sequence></attribute>
  <attribute id="0x0005"><sequence><uuid value="0x1002"/></sequence></attribute>
  <attribute id="0x0006"><sequence><uint16 value="0x656e"/><uint16 value="0x006a"/><uint16 value="0x0100"/></sequence></attribute>
  <attribute id="0x0009"><sequence><sequence><uuid value="0x1124"/><uint16 value="0x0100"/></sequence></sequence></attribute>
  <attribute id="0x000d"><sequence><sequence>
    <sequence><uuid value="0x0100"/><uint16 value="0x0013"/></sequence>
    <sequence><uuid value="0x0011"/></sequence></sequence></sequence></attribute>
  <attribute id="0x0100"><text value="{prof['name']}"/></attribute>
  <attribute id="0x0101"><text value="Game Controller"/></attribute>
  <attribute id="0x0102"><text value="{prof['provider']}"/></attribute>
  <attribute id="0x0200"><uint16 value="0x0100"/></attribute>
  <attribute id="0x0201"><uint16 value="0x0111"/></attribute>
  <attribute id="0x0202"><uint8 value="0x08"/></attribute>
  <attribute id="0x0203"><uint8 value="0x00"/></attribute>
  <attribute id="0x0204"><boolean value="false"/></attribute>
  <attribute id="0x0205"><boolean value="true"/></attribute>
  <attribute id="0x0206"><sequence><sequence><uint8 value="0x22"/><text encoding="hex" value="{desc}"/></sequence></sequence></attribute>
  <attribute id="0x0207"><sequence><sequence><uint16 value="0x0409"/><uint16 value="0x0100"/></sequence></sequence></attribute>
  <attribute id="0x0209"><boolean value="true"/></attribute>
  <attribute id="0x020a"><boolean value="true"/></attribute>
  <attribute id="0x020c"><uint16 value="0x0c80"/></attribute>
  <attribute id="0x020d"><boolean value="false"/></attribute>
  <attribute id="0x020e"><boolean value="false"/></attribute>
</record>"""


# ---------------------------------------------------------------- DualShock 4 reports
def crc32(seed_byte, data):
    return struct.pack("<I", binascii.crc32(bytes([seed_byte]) + bytes(data)) & 0xFFFFFFFF)


def calibration(bt_layout):
    """Plausible IMU calibration. Bluetooth (0x05) orders gyro limits plus,plus,plus,minus,minus,minus;
    USB-style (0x02) orders them plus,minus per axis."""
    bias = [0, 0, 0]
    gyro = [8000, 8000, 8000, -8000, -8000, -8000] if bt_layout else [8000, -8000, 8000, -8000, 8000, -8000]
    rest = [540, 540, 8192, -8192, 8192, -8192, 8192, -8192]
    return struct.pack("<17h", *(bias + gyro + rest)) + b"\x00\x00"  # 36 bytes


def feature_report(rid):
    """Payload for GET_REPORT(feature, rid), including the report ID, or None if unknown."""
    if rid == 0x02:
        return bytes([0x02]) + calibration(False)
    if rid == 0x05:
        body = bytes([0x05]) + calibration(True)
        return body + crc32(0xA3, body)                      # 1 + 36 + 4 = 41
    if rid == 0xA3:
        body = bytearray(48)
        body[0:11] = b"Sep 21 2018"
        body[16:24] = b"04:50:51"
        return bytes([0xA3]) + bytes(body)
    if rid in FEATURE_SIZES:
        return bytes([rid]) + bytes(FEATURE_SIZES[rid])
    return None


HAT = {(0, -1): 0, (1, -1): 1, (1, 0): 2, (1, 1): 3, (0, 1): 4, (-1, 1): 5, (-1, 0): 6, (-1, -1): 7}


def ds4_fields(st, counter):
    """The 9 shared bytes: LX LY RX RY, hat+face, shoulders/menu/sticks, PS/touchpad+counter, L2, R2."""
    b = st.buttons
    a = st.axes

    def stick(v):  # -32767..32767 -> 0..255
        return max(0, min(255, (v + 32768) * 255 // 65535))

    def on(name):
        return bool(b & BIT[name])

    dx = on("Right") - on("Left")
    dy = on("Down") - on("Up")
    hat = HAT.get((dx, dy), 8)
    face = (on("X") << 4) | (on("A") << 5) | (on("B") << 6) | (on("Y") << 7)  # square cross circle triangle
    l2, r2 = a["lt"] >> 8, a["rt"] >> 8
    b2 = (on("LB") | on("RB") << 1 | (l2 > 0) << 2 | (r2 > 0) << 3 | on("View") << 4 | on("Menu") << 5
          | on("LS") << 6 | on("RS") << 7)
    b3 = on("Guide") | on("Share") << 1 | (counter & 0x3F) << 2  # PS, touchpad click, counter
    return bytes([stick(a["lx"]), stick(a["ly"]), stick(a["rx"]), stick(a["ry"]), hat | face, b2, b3, l2, r2])


def report_01(st, counter):
    return bytes([0x01]) + ds4_fields(st, counter)


def report_11(st, counter, timestamp):
    r = bytearray(78)
    r[0], r[1], r[2] = 0x11, 0xC0, 0x00
    r[3:12] = ds4_fields(st, counter)
    struct.pack_into("<H", r, 12, timestamp & 0xFFFF)
    r[14] = 0x00                                   # temperature, unused
    struct.pack_into("<3h", r, 15, 0, 0, 0)        # gyro: still
    struct.pack_into("<3h", r, 21, 0, 8192, 0)     # accel: lying flat, 1 g
    r[32] = 0x1B                                   # cable connected, battery full
    r[35] = 1                                      # one touch packet
    r[36] = counter & 0xFF
    r[37] = 0x80                                   # finger 1 not touching
    r[41] = 0x80                                   # finger 2 not touching
    r[74:78] = crc32(0xA1, r[0:74])
    return bytes(r)


XBOX_BUTTONS = ["A", "B", "X", "Y", "LB", "RB", "View", "Menu", "LS", "RS"]


def xbox_report_1(st):
    a, b = st.axes, st.buttons

    def stick(v):  # -32767..32767 -> 0..65535, centre 32768
        return max(0, min(65535, v + 32768))

    dx = bool(b & BIT["Right"]) - bool(b & BIT["Left"])
    dy = bool(b & BIT["Down"]) - bool(b & BIT["Up"])
    hat = HAT[(dx, dy)] + 1 if (dx, dy) in HAT else 0       # 1 = up ... 8 = up-left, 0 = centred
    buttons = sum(1 << i for i, n in enumerate(XBOX_BUTTONS) if b & BIT[n])
    return struct.pack("<BHHHHHHBH", 0x01, stick(a["lx"]), stick(a["ly"]), stick(a["rx"]), stick(a["ry"]),
                       a["lt"] * 1023 // 65535, a["rt"] * 1023 // 65535, hat, buttons)


def xbox_report_2(st):
    return bytes([0x02, 1 if st.buttons & BIT["Guide"] else 0])


# ---------------------------------------------------------------- the real controller (what is plugged in)
# Short names for controllers we know (vid, pid) -> name shown on the Mac. Everything else: cleaned USB strings.
KNOWN_PADS = {
    ("2dc8", "2027"): "8BitDo Ultimate C", ("2dc8", "3106"): "8BitDo Ultimate", ("2dc8", "310a"): "8BitDo Ultimate 2C",
    ("2dc8", "3109"): "8BitDo Ultimate", ("2dc8", "6012"): "8BitDo Ultimate 2C", ("2dc8", "301a"): "8BitDo Ultimate 2C",
    ("045e", "028e"): "Xbox 360 Controller", ("045e", "02d1"): "Xbox One Controller",
    ("045e", "02dd"): "Xbox One Controller", ("045e", "02ea"): "Xbox One S Controller",
    ("045e", "0b00"): "Xbox Elite Series 2", ("045e", "0b12"): "Xbox Series X|S Controller",
    ("054c", "05c4"): "DualShock 4", ("054c", "09cc"): "DualShock 4", ("054c", "0ce6"): "DualSense",
    ("054c", "0df2"): "DualSense Edge", ("057e", "2009"): "Switch Pro Controller",
}
VENDOR_NAMES = {"2dc8": "8BitDo", "045e": "Microsoft", "054c": "Sony", "057e": "Nintendo", "0e6f": "PDP",
                "0f0d": "Hori", "046d": "Logitech", "24c6": "PowerA", "20d6": "PowerA", "0738": "Mad Catz",
                "1532": "Razer", "3537": "GameSir", "2563": "ShanWan", "1949": "Amazon", "28de": "Valve"}
BUS_NAMES = {0x03: "usb", 0x05: "bluetooth", 0x06: "virtual", 0x19: "host", 0x1F: "spi"}
GENERIC_NAMES = ("generic x-box pad", "xbox 360 wireless receiver", "microsoft x-box", "x-box pad")
NAME_NOISE = ("wired controller for xbox", "wireless controller for xbox", "controller for xbox", "for xbox",
              "wired controller", "wireless controller", "game controller", "controller", "gamepad", "game pad",
              "joystick", "(r)", "(tm)", "®", "™")
BT_NAME_MAX = 29     # short enough for every place the Mac shows it
BT_NAME_OVERRIDE = None  # --name


def _read(path):
    try:
        return open(path).read().strip()
    except OSError:
        return ""


def find_controller():
    """The game controller's /dev/input/eventN: a joystick (jsN) node with BTN_GAMEPAD, not virtual.
    Prefers USB, then the 8BitDo the bridge was built for. Returns the path or None."""
    try:
        blocks = open("/proc/bus/input/devices").read().split("\n\n")
    except OSError:
        return None
    best = None
    for block in blocks:
        info = {}
        for line in block.splitlines():
            if line.startswith("I:"):
                info.update(kv.split("=", 1) for kv in line[3:].split())
            elif line.startswith("H: Handlers="):
                info["handlers"] = line.split("=", 1)[1].split()
            elif line.startswith("B: KEY="):
                words = line.split("=", 1)[1].split()
                info["keys"] = int("".join(w.zfill(16) for w in words), 16)
        handlers = info.get("handlers", [])
        events = [h for h in handlers if h.startswith("event")]
        if not events or not any(h.startswith("js") for h in handlers) or info.get("Bus") == "0006":
            continue
        if not (info.get("keys", 0) >> 0x130) & 1:          # BTN_GAMEPAD / BTN_SOUTH
            continue
        score = (info.get("Bus") == "0003") * 2 + (info.get("Vendor") == "2dc8")
        if best is None or score > best[0]:
            best = (score, f"/dev/input/{events[0]}")
    return best[1] if best else None


def controller_info(path):
    """Contract `controller` fields for an evdev node, from sysfs (+ the USB device's strings)."""
    ev = os.path.basename(path)
    d = f"/sys/class/input/{ev}/device"
    bus = int(_read(f"{d}/id/bustype") or "0", 16)
    info = {"connected": True, "path": path, "name": _read(f"{d}/name"),
            "vid": _read(f"{d}/id/vendor").lower(), "pid": _read(f"{d}/id/product").lower(),
            "bus": BUS_NAMES.get(bus, f"0x{bus:02x}"), "phys": _read(f"{d}/phys"), "uniq": _read(f"{d}/uniq")}
    usb_manu = usb_prod = ""
    p = os.path.realpath(d)
    while p not in ("/", "/sys"):
        if os.path.exists(f"{p}/idVendor"):
            usb_manu, usb_prod = _read(f"{p}/manufacturer"), _read(f"{p}/product")
            break
        p = os.path.dirname(p)
    info["vendor"] = usb_manu or VENDOR_NAMES.get(info["vid"], "")
    info["usb_product"] = usb_prod
    info["evdev_name"] = info["name"]                     # e.g. xpad's "Generic X-Box pad"
    if usb_prod and info["name"].lower() in GENERIC_NAMES:
        info["name"] = usb_prod                           # the contract wants the real product name
    info["short_name"] = short_name(info)
    return info


def short_name(info):
    """"8BitDo Ultimate C Wired Controller for Xbox" -> "8BitDo Ultimate C"."""
    known = KNOWN_PADS.get((info["vid"], info["pid"]))
    if known:
        return known
    prod = info.get("usb_product") or info.get("name") or ""
    if prod.lower() in GENERIC_NAMES:
        prod = ""
    low = prod.lower()
    for noise in NAME_NOISE:
        i = low.find(noise)
        while i >= 0:
            prod, low = prod[:i] + " " + prod[i + len(noise):], low[:i] + " " + low[i + len(noise):]
            i = low.find(noise)
    prod = " ".join(prod.split()).strip(" -_,")
    if prod.lower() in ("wireless", "wired", "usb", "usb wired", "usb wireless"):
        prod = ""
    vendor = (info.get("vendor") or "").split(" ")[0]
    if not prod:
        prod = f"{vendor} Controller" if vendor else "Game Controller"
    elif vendor and not prod.lower().startswith(vendor.lower()):
        prod = f"{vendor} {prod}"
    return prod[:BT_NAME_MAX].strip()


# ---------------------------------------------------------------- D-Bus: agent, profile, adapter
class Agent(dbus.service.Object):
    """Accept pairing from the Mac with no PIN (Just Works) and trust it."""

    def __init__(self, bus):
        super().__init__(bus, AGENT_PATH)
        self.bus = bus

    def _trust(self, device):
        addr = str(device).rsplit("dev_", 1)[-1].replace("_", ":")
        try:
            with open(HOST_FILE, "w") as f:
                f.write(addr)  # the bridge connects to this Mac by itself from now on
        except OSError:
            pass
        try:
            props = dbus.Interface(self.bus.get_object("org.bluez", device), "org.freedesktop.DBus.Properties")
            props.Set("org.bluez.Device1", "Trusted", True)
        except dbus.DBusException as e:
            log(f"could not mark {device} trusted: {e}")

    @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
    def Release(self):
        pass

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="s")
    def RequestPinCode(self, device):
        log(f"pairing: PIN requested by {device}, answering 0000")
        self._trust(device)
        return "0000"

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="u")
    def RequestPasskey(self, device):
        self._trust(device)
        return dbus.UInt32(0)

    @dbus.service.method("org.bluez.Agent1", in_signature="ouq", out_signature="")
    def DisplayPasskey(self, device, passkey, entered):
        log(f"pairing: passkey {passkey:06d}")

    @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
    def DisplayPinCode(self, device, pincode):
        log(f"pairing: PIN {pincode}")

    @dbus.service.method("org.bluez.Agent1", in_signature="ou", out_signature="")
    def RequestConfirmation(self, device, passkey):
        log(f"pairing: accepted {device}")
        self._trust(device)

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="")
    def RequestAuthorization(self, device):
        log(f"pairing: authorized {device} (Just Works)")
        self._trust(device)

    @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
    def AuthorizeService(self, device, uuid):
        log(f"pairing: service {uuid} authorized for {device}")

    @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
    def Cancel(self):
        log("pairing cancelled")


class Profile(dbus.service.Object):
    """Only here to publish the HID SDP record; the L2CAP channels are handled by our own sockets."""

    @dbus.service.method("org.bluez.Profile1", in_signature="", out_signature="")
    def Release(self):
        pass

    @dbus.service.method("org.bluez.Profile1", in_signature="oha{sv}", out_signature="")
    def NewConnection(self, device, fd, props):
        os.close(fd.take())

    @dbus.service.method("org.bluez.Profile1", in_signature="o", out_signature="")
    def RequestDisconnection(self, device):
        pass


# ---------------------------------------------------------------- the Bluetooth name (bt_name)
BT = {"adapter": None}


def apply_bt_name(name):
    """Main (GLib) thread: give the adapter this name (Adapter1.Alias). The Mac reads it with a Remote Name Request
    when it pairs, and shows it as the device name, the HID Product and GCController.vendorName."""
    if not name or name == STATUS["bt_name"]:
        return False
    STATUS["bt_name"] = name
    if BT["adapter"] is not None:
        try:
            BT["adapter"].Set("org.bluez.Adapter1", "Alias", name)
        except dbus.DBusException as e:
            log(f"could not set the Bluetooth name: {e}")
        log(f"Bluetooth name is now '{name}' (the Mac keeps its cached name until the device is re-paired)")
    return False


def request_bt_name(name):
    """Any thread."""
    GLib.idle_add(apply_bt_name, name)


KEYS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ds4_bt_keys.json")


def _load_keys():
    try:
        return json.load(open(KEYS_FILE))
    except (OSError, ValueError):
        return {}


def keep_link_keys():
    """Remember every link key the Mac and this laptop agree on (Link Key Notification), in KEYS_FILE (root, 0600).

    Why: the Mac pairs with "No Bonding" when asked programmatically (IOBluetoothDevicePair, i.e. `blueutil
    --pair`, the name-refresh procedure). BlueZ then keeps the key in memory only and drops the device (and its
    storage) once it disconnects, while the Mac keeps the key: after a bluetoothd restart (ds4_bt_setup.sh,
    reboot) every reconnect fails with Authentication Failure until the Mac forgets the device.
    restore_link_keys() puts the key back at the next start."""
    def watch():
        try:
            s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI)
            s.bind((HCI_INDEX,))
            s.setsockopt(SOL_HCI, HCI_FILTER, struct.pack("<IIIHxx", 1 << 4, 1 << 0x18, 0, 0))
        except OSError as e:
            log(f"link key watcher unavailable ({e})")
            return
        while True:
            data = s.recv(64)
            if len(data) >= 26 and data[0] == 0x04 and data[1] == 0x18:      # Link Key Notification
                addr = ":".join(f"{b:02X}" for b in reversed(data[3:9]))
                keys = _load_keys()
                keys[addr] = {"key": data[9:25].hex().upper(), "type": data[25]}
                try:
                    fd = os.open(KEYS_FILE + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(fd, "w") as f:
                        json.dump(keys, f)
                    os.replace(KEYS_FILE + ".tmp", KEYS_FILE)
                    log(f"link key for {addr} saved (restored if bluetoothd forgets it)")
                except OSError as e:
                    log(f"could not save the link key for {addr}: {e}")
    threading.Thread(target=watch, daemon=True).start()


def restore_link_keys(bus, adapter_path, adapter_addr):
    """If BlueZ no longer has the pairing for a Mac we hold a key for, write BlueZ's device file (with the key)
    and restart bluetoothd once so it loads it. Returns True if bluetoothd was restarted."""
    keys = _load_keys()
    if not keys:
        return False
    objs = dbus.Interface(bus.get_object("org.bluez", "/"), "org.freedesktop.DBus.ObjectManager").GetManagedObjects()
    missing = []
    for addr, k in keys.items():
        dev = objs.get(f"{adapter_path}/dev_{addr.replace(':', '_')}", {}).get("org.bluez.Device1", {})
        if not dev.get("Paired"):
            missing.append((addr, k))
    if not missing:
        return False
    for addr, k in missing:
        d = f"/var/lib/bluetooth/{adapter_addr}/{addr}"
        try:
            os.makedirs(d, mode=0o700, exist_ok=True)
            with open(f"{d}/info", "w") as f:
                f.write(f"[General]\nName=Mac\nSupportedTechnologies=BR/EDR;\nTrusted=true\nBlocked=false\n\n"
                        f"[LinkKey]\nKey={k['key']}\nType={k['type']}\nPINLength=0\n")
            os.chmod(f"{d}/info", 0o600)
        except OSError as e:
            log(f"could not restore the pairing with {addr}: {e}")
            return False
    import subprocess
    log(f"BlueZ lost the pairing with {', '.join(a for a, _ in missing)}; restoring it (restarting bluetoothd)")
    subprocess.run(["systemctl", "restart", "bluetooth"], timeout=20)
    time.sleep(2)
    return True


def setup_bluez(adapter_name, _restored=False):
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    if not _restored:
        objs0 = dbus.Interface(bus.get_object("org.bluez", "/"),
                               "org.freedesktop.DBus.ObjectManager").GetManagedObjects()
        ad0 = [(p, i["org.bluez.Adapter1"]) for p, i in objs0.items()
               if "org.bluez.Adapter1" in i and (not adapter_name or p.endswith(adapter_name))]
        if ad0 and restore_link_keys(bus, ad0[0][0], str(ad0[0][1]["Address"])):
            for _ in range(20):                             # wait for bluetoothd to publish the adapter again
                try:
                    return setup_bluez(adapter_name, True)
                except (dbus.DBusException, SystemExit):
                    time.sleep(0.5)
    objs = dbus.Interface(bus.get_object("org.bluez", "/"), "org.freedesktop.DBus.ObjectManager").GetManagedObjects()
    adapters = [p for p, ifs in objs.items() if "org.bluez.Adapter1" in ifs and (not adapter_name or p.endswith(adapter_name))]
    if not adapters:
        sys.exit("No Bluetooth adapter found.")
    path = adapters[0]
    global HCI_INDEX
    HCI_INDEX = int(str(path).rsplit("hci", 1)[-1])  # raw HCI sockets need the index (hci1 after a resume)
    keep_link_keys()
    props = dbus.Interface(bus.get_object("org.bluez", path), "org.freedesktop.DBus.Properties")
    BT.update(adapter=props)
    # The Bluetooth name: the plugged-in controller's short name (Addendum A), else the identity's own name.
    path_now = find_controller()
    name = BT_NAME_OVERRIDE or (controller_info(path_now)["short_name"] if path_now else PROFILES[MODE]["name"])
    for k, v in [("Powered", True), ("Alias", name), ("Pairable", True),
                 ("PairableTimeout", dbus.UInt32(0)), ("DiscoverableTimeout", dbus.UInt32(0)), ("Discoverable", True)]:
        props.Set("org.bluez.Adapter1", k, v)
    STATUS["bt_name"] = name
    addr = props.Get("org.bluez.Adapter1", "Address")
    # `mode` keeps its old values for existing readers: "xbox" for both Xbox identities, "ds4"; `identity` is exact.
    STATUS.update(mode="ds4" if MODE == "ds4" else "xbox", name=PROFILES[MODE]["name"], adapter=str(addr),
                  identity=identity(MODE), identities=IDENTITIES)
    cls = int(props.Get("org.bluez.Adapter1", "Class"))
    warn = '' if (cls & 0x1FFF) == 0x0508 else \
        '  (expected ...508: rerun ds4_bt_setup.sh; the hostname plugin must be off)'
    log(f"adapter {path} {addr} class 0x{cls:06x}{warn}")

    agent = Agent(bus)
    mgr = dbus.Interface(bus.get_object("org.bluez", "/org/bluez"), "org.bluez.AgentManager1")
    mgr.RegisterAgent(AGENT_PATH, "NoInputNoOutput")
    mgr.RequestDefaultAgent(AGENT_PATH)
    log("pairing agent registered as default (NoInputNoOutput)")

    def reclaim():  # the desktop's own agent (GNOME, KDE) may take the default role back; keep it ours
        try:
            mgr.RequestDefaultAgent(AGENT_PATH)
        except dbus.DBusException:
            pass
        return True
    GLib.timeout_add_seconds(2, reclaim)

    def on_props(iface, changed, _invalidated, path=None):
        # Just Works pairing can complete without asking the agent, so also watch for "Paired" directly
        if iface == "org.bluez.Device1" and changed.get("Paired") and path:
            addr = path.rsplit("dev_", 1)[-1].replace("_", ":")
            with open(HOST_FILE, "w") as f:
                f.write(addr)
            log(f"paired with {addr}; connecting to it")
            try:
                dprops = dbus.Interface(bus.get_object("org.bluez", path), "org.freedesktop.DBus.Properties")
                dprops.Set("org.bluez.Device1", "Trusted", True)
            except dbus.DBusException:
                pass
    bus.add_signal_receiver(on_props, dbus_interface="org.freedesktop.DBus.Properties",
                            signal_name="PropertiesChanged", bus_name="org.bluez", path_keyword="path")

    profile = Profile(bus, PROFILE_PATH)
    pm = dbus.Interface(bus.get_object("org.bluez", "/org/bluez"), "org.bluez.ProfileManager1")
    pm.RegisterProfile(PROFILE_PATH, HID_UUID, {
        "ServiceRecord": sdp_record(), "Role": "server",
        "RequireAuthentication": False, "RequireAuthorization": False})
    log(f"HID service record published ({MODE}); the laptop is discoverable as '{name}'")
    return bus, agent, profile


# ---------------------------------------------------------------- controller (evdev)
class Pad:
    def __init__(self, grab):
        self.grab, self.fd, self.state, self.next_try = grab, None, State({}), 0.0

    def poll_open(self):
        if self.fd is not None or time.monotonic() < self.next_try:
            return
        self.next_try = time.monotonic() + 1.0
        path = find_controller()
        if not path:
            return
        try:
            self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        except OSError as e:
            log(f"controller: cannot open {path} ({e})")
            return
        self.effect_id, self.rumble_now, self.started = -1, (0, 0), False
        if self.grab:
            try:
                fcntl.ioctl(self.fd, EVIOCGRAB, 1)
            except OSError:
                pass
        self.state = State(read_ranges(self.fd))
        self.last_ev = None
        info = controller_info(path)
        STATUS["controller"] = {k: info[k] for k in ("connected", "path", "name", "vendor", "vid", "pid", "bus",
                                                     "phys", "uniq", "usb_product", "evdev_name")}
        log(f"controller: reading {path}: {info['usb_product'] or info['name']} ({info['vid']}:{info['pid']}, "
            f"{info['bus']}) -> Bluetooth name '{BT_NAME_OVERRIDE or info['short_name']}'")
        request_bt_name(BT_NAME_OVERRIDE or info["short_name"])

    def read(self):
        """Apply pending evdev events. Returns True if the state may have changed."""
        try:
            data = os.read(self.fd, INPUT_EVENT.size * 64)
        except BlockingIOError:
            return False
        except OSError:
            log("controller: disconnected")
            os.close(self.fd)
            self.fd, self.state = None, State({})
            STATUS["controller"] = {**STATUS["controller"], "connected": False, "path": None}
            return True
        changed = False
        for off in range(0, len(data) - INPUT_EVENT.size + 1, INPUT_EVENT.size):
            sec, usec, t, code, value = INPUT_EVENT.unpack_from(data, off)
            if t != EV_SYN:
                self.state.apply(t, code, value)
                changed = True
                self.last_ev = sec + usec / 1e6   # kernel timestamp (CLOCK_REALTIME) of the newest event
        return changed

    def rumble(self, strong, weak):
        """strong/weak 0..255 from the DualShock 4 output report -> xpad force feedback."""
        if self.fd is None or (strong, weak) == self.rumble_now:
            return
        self.rumble_now = (strong, weak)
        STATUS["rumble"] = {"strong": strong, "weak": weak}
        # One endless effect that is started once and then only re-uploaded with new strengths; "off" is
        # strength 0. Never send playback=0: ff-memless drops a stop that lands within a timer tick of an
        # upload (the effect is briefly not "playing"), and the motors then keep running at the old strength.
        try:
            # type, id, direction, trigger.button, trigger.interval, replay.length (0 = endless), replay.delay
            buf = bytearray(FF_EFFECT.pack(FF_RUMBLE, self.effect_id, 0, 0, 0, 0, 0, strong * 257, weak * 257))
            fcntl.ioctl(self.fd, EVIOCSFF, buf, True)
            self.effect_id = FF_EFFECT.unpack(buf)[1]
            if not self.started:
                os.write(self.fd, INPUT_EVENT.pack(0, 0, EV_FF, self.effect_id, 1))
                self.started = True
        except OSError as e:
            log(f"rumble failed: {e}")


# ---------------------------------------------------------------- HID session
def l2cap():
    return socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)


def handle_ctrl(ctrl, msg, session):
    """One message on the HID control channel. Returns False to end the session."""
    if not msg:
        return False
    kind, param = msg[0] >> 4, msg[0] & 0x0F
    if kind == 0x4:                                         # GET_REPORT
        rtype, rid = param & 0x3, msg[1] if len(msg) > 1 else 0
        if is_xbox():
            st = session["pad"].state
            rep = xbox_report_1(st) if (rtype, rid) == (1, 1) else xbox_report_2(st) if (rtype, rid) == (1, 2) else None
        else:
            rep = feature_report(rid) if rtype == 3 else (report_11(session["pad"].state, 0, 0) if rid == 0x11 else None)
        if rep is None:
            log(f"ctrl: GET_REPORT type {rtype} id 0x{rid:02x} -> not supported (please send me this line)")
            ctrl.send(bytes([0x02]))                        # HANDSHAKE: invalid report ID
        else:
            log(f"ctrl: GET_REPORT type {rtype} id 0x{rid:02x} -> {len(rep)} bytes")
            ctrl.send(bytes([0xA0 | rtype]) + rep)
        if rtype == 3 and rid in (0x02, 0x05) and not session["full"]:
            session["full"] = True
            log("mode: switching to full reports (0x11)")
    elif kind == 0x5:                                       # SET_REPORT
        log(f"ctrl: SET_REPORT {msg[1:].hex(' ')}")
        ctrl.send(bytes([0x00]))
    elif kind == 0x6:                                       # GET_PROTOCOL
        ctrl.send(bytes([0xA0, 0x01]))
    elif kind in (0x7, 0x9):                                # SET_PROTOCOL / SET_IDLE
        log(f"ctrl: {'SET_PROTOCOL' if kind == 0x7 else 'SET_IDLE'} {param}")
        ctrl.send(bytes([0x00]))
    elif kind == 0x1:                                       # HID_CONTROL
        log(f"ctrl: HID_CONTROL {param}{' (virtual cable unplug)' if param == 5 else ''}")
        return param != 5
    elif kind == 0xA:                                       # DATA on the control channel
        handle_output(msg, session)
    else:
        log(f"ctrl: unhandled {msg.hex(' ')}")
        ctrl.send(bytes([0x03]))                            # HANDSHAKE: unsupported request
    return True


def xbox_output(r, session):
    """Xbox output report 3 payload (8 bytes, no report ID), both identities: rumble, or the latency ping.
    enable bits (0x01 weak, 0x02 strong, 0x04/0x08 trigger motors), magnitudes 0-100 for left trigger, right
    trigger, strong, weak; then duration and start delay in 10 ms, loop count."""
    en, _lt, _rt, strong, weak, dur, delay, loop = r[:8]
    if en == 0 and dur == 0xA5 and loop == 0x5A:
        # Latency ping from the receiver app: echo the sequence number at once in input report 4.
        # Contract: out 03 [00 00 00 00 00 A5 <seq> 5A]  ->  in 04 [<seq>]
        session["ping"](delay)
        STATUS["stats"]["pings"] += 1
        return
    strong = strong * 255 // 100 if en & 0x02 else 0
    weak = weak * 255 // 100 if en & 0x01 else 0
    session["pad"].rumble(strong, weak)
    session["rumble_stop"] = (time.monotonic() + max(1, (dur + delay) * (loop + 1) - delay) * 0.01
                              if (strong or weak) else None)
    if VERBOSE and (strong, weak) != session.get("last_rumble"):
        session["last_rumble"] = (strong, weak)
        log(f"host: rumble strong {strong} weak {weak} for {dur * 10} ms x{loop + 1}")


def handle_output(msg, session):
    """Output report from the Mac (0xA2 header): rumble (and the DualShock 4 light bar)."""
    if is_xbox() and len(msg) >= 10 and msg[1] == 0x03:
        xbox_output(msg[2:10], session)
        return
    if len(msg) > 2 and msg[1] == 0x11:
        if not session["full"]:
            session["full"] = True
            log("mode: switching to full reports (0x11) after host output report")
        r = msg[1:]
        if len(r) > 10:
            if r[3] & 0x01:                                 # flags: rumble fields valid
                session["pad"].rumble(r[7], r[6])           # [6] weak/right motor, [7] strong/left motor
            if VERBOSE and (r[6], r[7]) != session.get("last_rumble"):
                session["last_rumble"] = (r[6], r[7])
                log(f"host: rumble weak {r[6]} strong {r[7]}  light bar #{r[8]:02x}{r[9]:02x}{r[10]:02x}")
    elif VERBOSE:
        log(f"intr: output {msg[:16].hex(' ')}")


SOL_L2CAP, L2CAP_CONNINFO = 6, 2
HCI_LP_RSWITCH = 0x0001                       # link policy: allow role switch only, so no sniff/hold/park
HCISETLINKPOL = (1 << 30) | (4 << 16) | (ord("H") << 8) | 222
HCISETLINKMODE = (1 << 30) | (4 << 16) | (ord("H") << 8) | 226
HCI_LM_ACCEPT, HCI_LM_MASTER = 0x8000, 0x0001
WANT_MASTER = False      # --master: try to be the piconet master (laptop polls the Mac) instead of the Mac


def set_link_mode():
    """--master: ask the kernel to take the master role on new connections (Create_Connection without role
    switch; Accept_Connection with a role switch). Experimental: the Mac may refuse or insist on master."""
    hci = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI)
    try:
        hci.bind((HCI_INDEX,))
        mode = HCI_LM_ACCEPT | (HCI_LM_MASTER if WANT_MASTER else 0)
        fcntl.ioctl(hci, HCISETLINKMODE, struct.pack("=HxxI", HCI_INDEX, mode))
    finally:
        hci.close()


def keep_link_active(sock):
    """Refuse sniff mode on this connection. The Mac asks for 15 ms sniff whenever the link is briefly quiet,
    and each exit back to active mode costs time, so we tell our radio to reject sniff requests outright.
    Returns the ACL connection handle (None if unknown)."""
    try:
        handle = struct.unpack_from("<H", sock.getsockopt(SOL_L2CAP, L2CAP_CONNINFO, 8))[0]
        hci = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI)
        hci.bind((HCI_INDEX,))
        pol = 0 if WANT_MASTER else HCI_LP_RSWITCH   # as master, also refuse the Mac's role switch requests
        fcntl.ioctl(hci, HCISETLINKPOL, struct.pack("=HxxI", HCI_INDEX, pol))      # default for new links
        opcode = (0x02 << 10) | 0x000D                                            # Write_Link_Policy_Settings
        hci.send(struct.pack("<BHBHH", 0x01, opcode, 4, handle, pol))
        hci.send(struct.pack("<BHBH", 0x01, (0x02 << 10) | 0x0004, 2, handle))    # Exit_Sniff_Mode (if in sniff)
        hci.close()
        STATUS["link"] = {"handle": handle, "sniff_disabled": True}
        log(f"link 0x{handle:04x}: sniff mode disabled, staying active")
        return handle
    except OSError as e:
        log(f"could not disable sniff mode ({e}); latency may jitter")
        return None


SOL_HCI, HCI_DATA_DIR, HCI_FILTER, HCI_TIME_STAMP = 0, 1, 2, 3
HCI_CMSG_DIR, HCI_CMSG_TSTAMP = 1, 2
EVT_NAMES = {0x0D: "QoS Setup Complete", 0x12: "Role Change", 0x14: "Mode Change", 0x1B: "Max Slots Change",
             0x1D: "Connection Packet Type Changed", 0x21: "Flow Specification Complete"}


class LinkMonitor:
    """Counts our ACL packets inside the Bluetooth controller, from a raw HCI socket.

    Why: the L2CAP socket queue is empty as soon as the kernel has handed a packet to the USB radio, but the
    radio itself buffers several packets (6 on the RTL8852BU) until the Mac polls us. During a radio stall
    (the Mac's radio is elsewhere for 20-40 ms at a time) those buffers fill with stale reports, and the
    newest state queues behind them. Here we see every ACL frame the kernel sends to the controller (raw HCI
    sockets get a copy, with a kernel timestamp) and every Number_Of_Completed_Packets event (the radio
    reporting a packet as delivered), so `inflight` is exact and the send gate can keep the radio's queue at
    one packet: the report that goes out after a stall is always the newest one.
    Also measures the radio delivery time (handed to controller -> completed) and logs link events."""

    def __init__(self, handle, adapter_index=0):
        self.handle, self.tx, self.done = handle, 0, 0
        self.sent_at = collections.deque()
        self.lat = []                       # ms, handed to controller -> completed, since the last stats tick
        self.last_change = time.monotonic()
        s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI)
        s.bind((adapter_index,))
        s.setsockopt(SOL_HCI, HCI_DATA_DIR, 1)
        s.setsockopt(SOL_HCI, HCI_TIME_STAMP, 1)
        events = [0x13] + list(EVT_NAMES)   # Number_Of_Completed_Packets + the link events we log
        mask = sum(1 << e for e in events)
        # struct hci_ufilter (16 bytes with padding): u32 type_mask (ACL=2, event=4), u32 event_mask[2], le16 opcode
        s.setsockopt(SOL_HCI, HCI_FILTER, struct.pack("<IIIHxx", (1 << 2) | (1 << 4),
                                                     mask & 0xFFFFFFFF, mask >> 32, 0))
        s.setblocking(False)
        self.sock = s

    def fileno(self):
        return self.sock.fileno()

    @property
    def inflight(self):
        return self.tx - self.done

    def read(self):
        """Drain the socket. Returns True if a packet of ours completed."""
        completed = False
        while True:
            try:
                data, anc, _, _ = self.sock.recvmsg(260, 64)
            except (BlockingIOError, InterruptedError):
                return completed
            incoming, ts = True, time.time()
            for level, kind, val in anc:
                if level == SOL_HCI and kind == HCI_CMSG_DIR:
                    incoming = struct.unpack("i", val[:4])[0] == 1
                elif level == SOL_HCI and kind == HCI_CMSG_TSTAMP:
                    sec, usec = struct.unpack("qq", val[:16])
                    ts = sec + usec / 1e6
            if not data:
                continue
            if data[0] == 0x02 and not incoming and len(data) >= 3:          # ACL out, to the controller
                if struct.unpack_from("<H", data, 1)[0] & 0x0FFF == self.handle:
                    self.tx += 1
                    self.sent_at.append(ts)
            elif data[0] == 0x04 and len(data) >= 3:
                evt = data[1]
                if evt == 0x13:                                                # Number_Of_Completed_Packets
                    for i in range(data[3]):
                        h, n = struct.unpack_from("<HH", data, 4 + 4 * i)
                        if h & 0x0FFF != self.handle:
                            continue
                        self.done += n
                        completed = True
                        for _ in range(n):
                            if self.sent_at:
                                self.lat.append((ts - self.sent_at.popleft()) * 1000)
                    if self.done > self.tx:   # a frame we missed (should not happen): resynchronise
                        self.done = self.tx
                        self.sent_at.clear()
                    self.last_change = time.monotonic()
                elif evt in EVT_NAMES:
                    log(f"link event: {EVT_NAMES[evt]} {data[3:].hex(' ')}")

    def reset_if_stuck(self, now, limit=0.5):
        """Safety net: if nothing completed for `limit` seconds while we think packets are in flight, assume
        we lost count (never seen in practice) and reopen the gate rather than stop streaming."""
        if self.inflight > 0 and now - self.last_change > limit:
            log(f"link monitor: {self.inflight} packets unconfirmed for {limit}s; resetting the count")
            self.done = self.tx
            self.sent_at.clear()
            self.last_change = now
            return True
        return False

    def close(self):
        self.sock.close()


def queued_bytes(sock):
    """Bytes still waiting in the kernel for this socket (Bluetooth's TIOCOUTQ reports free space)."""
    try:
        free = struct.unpack("i", fcntl.ioctl(sock, termios.TIOCOUTQ, b"\0\0\0\0"))[0]
        return max(0, sock.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF) - free)
    except OSError:
        return 0


def pct(values, p):
    """p-th fraction percentile of a list (nearest rank), or None for an empty list."""
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, int((len(v) - 1) * p + 0.5))]


def publish_stats(stats, ages, link, pad, now):
    """Roll the 0.5 s stats window into STATUS (contract keys) and, with -v, log it every 5 s."""
    el = now - stats["since"]
    lat = link.lat if link else []
    STATUS["stats"].update(
        reports_per_s=round(stats["sent"] / el, 1), max_queue_bytes=stats["maxq"],
        deferred=stats["deferred"],
        input_age_ms=round(sum(ages) / len(ages), 2) if ages else None,
        # additions (latency work stream): worst host-side age, radio delivery time, radio queue
        input_age_max_ms=round(max(ages), 2) if ages else None,
        link_ms=round(sum(lat) / len(lat), 2) if lat else None,
        link_p99_ms=round(pct(lat, 0.99), 2) if lat else None,
        max_inflight=stats["maxin"] if link else None)
    STATUS["input"] = input_snapshot(pad.state)
    if VERBOSE and now >= stats["next_log"]:
        stats["next_log"] = now + 5
        st = STATUS["stats"]
        age = f", input age {st['input_age_ms']} ms (max {st['input_age_max_ms']})" if ages else ""
        radio = (f", radio {st['link_ms']} ms (p99 {st['link_p99_ms']}), in radio max {stats['maxin']}"
                 if lat else "")
        log(f"{stats['sent'] / el:.0f} reports/s, max queue {stats['maxq']} B, deferred "
            f"{stats['deferred']}{radio}{age}; {pad.state.describe()}")
    ages.clear()
    if link:
        link.lat.clear()
    stats.update(sent=0, deferred=0, maxq=0, maxin=0, since=now)


def run_session(ctrl, intr, pad):
    """Send a report the moment the controller changes, but only when nothing of ours is waiting anywhere on
    the way out (kernel socket queue and, with the link monitor, the Bluetooth controller's own buffers), so a
    press never waits behind stale reports. While the way is busy the newest state is held and sent as soon
    as the previous packet is confirmed delivered. A keepalive (--rate, 250 Hz) keeps the link busy so the
    Mac never puts it into sniff mode."""
    try:
        intr.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SNDBUF)
    except OSError:
        pass
    # Start in the full 0x11 format: macOS never sends the feature request a real DS4 waits for.
    session = {"full": True, "pad": pad, "rumble_stop": None, "intr": intr}

    def ping(seq):
        try:
            intr.send(bytes([0xA1, 0x04, seq]), socket.MSG_DONTWAIT)
        except OSError:
            pass
    session["ping"] = ping
    ages, ev_sent = [], None
    guide_sent = None
    counter, ts, last = 0, 0, time.monotonic()
    pending, next_keep = True, 0.0
    stats = {"sent": 0, "deferred": 0, "maxq": 0, "maxin": 0, "since": time.monotonic(), "next_log": 0.0}
    handle = keep_link_active(intr)
    link = None
    if handle is not None:                  # always measure; gate only if MAX_INFLIGHT > 0
        try:
            link = LinkMonitor(handle, HCI_INDEX)
        except OSError as e:
            log(f"link monitor unavailable ({e}); gating on the socket queue only")
    log(f"connected: streaming {PROFILES[MODE]['name']} reports"
        + (f", at most {MAX_INFLIGHT} in the radio" if link and MAX_INFLIGHT else ""))
    try:
        while True:
            pad.poll_open()
            fds = [ctrl, intr] + ([pad.fd] if pad.fd is not None else []) + ([link] if link else [])
            # Waiting for a completion wakes us through the link socket; the 1 ms poll is only for the
            # (rare) case of a non-empty kernel socket queue, which has no wakeup of its own.
            if not pending:
                timeout = max(0.0, next_keep - time.monotonic())
            else:
                timeout = 0.005 if link and MAX_INFLIGHT and link.inflight >= MAX_INFLIGHT else 0.001
            ready, _, _ = select.select(fds, [], [], timeout)
            for s in ready:
                if s is pad.fd:
                    pending |= pad.read()
                elif s is link:
                    link.read()
                elif s is ctrl:
                    if not handle_ctrl(ctrl, ctrl.recv(1024), session):
                        return
                elif s is intr:
                    m = intr.recv(1024)
                    if not m:
                        return
                    handle_output(m, session)
            now = time.monotonic()
            if session["rumble_stop"] and now >= session["rumble_stop"]:
                pad.rumble(0, 0)                                # Xbox rumble has a duration; honour it
                session["rumble_stop"] = None
            if now >= next_keep:
                pending = True
            if not pending:
                continue
            if link:
                stats["maxin"] = max(stats["maxin"], link.inflight)
                if MAX_INFLIGHT and link.inflight >= MAX_INFLIGHT and not link.reset_if_stuck(now):
                    stats["deferred"] += 1
                    continue
            q = queued_bytes(intr)
            stats["maxq"] = max(stats["maxq"], q)
            if q:
                stats["deferred"] += 1
                continue
            ts += int((now - last) * 1e6 / 5.33)
            last = now
            if pad.fd is not None and pad.last_ev and pad.last_ev != ev_sent:
                ages.append((time.time() - pad.last_ev) * 1000)   # controller event -> report handed to Bluetooth
                ev_sent = pad.last_ev
            if is_xbox():
                guide = bool(pad.state.buttons & BIT["Guide"])
                if guide != guide_sent:
                    try:
                        intr.send(b"\xa1" + xbox_report_2(pad.state), socket.MSG_DONTWAIT)
                        guide_sent = guide
                    except BlockingIOError:
                        pass
                rep = xbox_report_1(pad.state)
            else:
                rep = report_11(pad.state, counter, ts) if session["full"] else report_01(pad.state, counter)
            try:
                intr.send(b"\xa1" + rep, socket.MSG_DONTWAIT)
            except BlockingIOError:
                stats["deferred"] += 1
                continue
            counter = (counter + 1) & 0xFF
            pending, next_keep = False, now + KEEPALIVE_S
            stats["sent"] += 1
            if now - stats["since"] >= STATS_WINDOW_S:
                publish_stats(stats, ages, link, pad, now)
    finally:
        if link:
            link.close()


def accept_or_reconnect(lc, li):
    """Wait for the Mac to connect, or connect to the last paired Mac ourselves every few seconds."""
    while True:
        ready, _, _ = select.select([lc], [], [], 2.0)
        host = open(HOST_FILE).read().strip() if os.path.exists(HOST_FILE) else None
        if ready:
            ctrl, peer = lc.accept()
            addr = peer[0]  # (bdaddr, psm) before Python 3.14; 3.14 adds more fields
            li.settimeout(10)
            try:
                intr, _ = li.accept()
            except socket.timeout:
                log(f"{addr} opened the control channel but not the interrupt channel")
                ctrl.close()
                continue
            return ctrl, intr, addr
        if host:
            try:
                ctrl, intr = l2cap(), l2cap()
                ctrl.settimeout(5)
                intr.settimeout(5)
                ctrl.connect((host, PSM_CTRL))
                intr.connect((host, PSM_INTR))
                ctrl.settimeout(None)
                intr.settimeout(None)
                return ctrl, intr, host
            except OSError:
                for s in (ctrl, intr):
                    s.close()


def worker(grab):
    try:
        _worker(grab)
    except Exception:
        import traceback
        traceback.print_exc()
        log("bridge crashed; please send me the error above")
        os._exit(1)


def _worker(grab):
    global PAD
    lc, li = l2cap(), l2cap()
    try:
        lc.bind((socket.BDADDR_ANY, PSM_CTRL))
        li.bind((socket.BDADDR_ANY, PSM_INTR))
    except OSError as e:
        log(f"cannot bind HID channels ({e}). Run ds4_bt_setup.sh first (bluetoothd must run with --noplugin=input) and use sudo.")
        os._exit(1)
    lc.listen(1)
    li.listen(1)
    pad = PAD = Pad(grab)
    pad.poll_open()
    log(f"waiting for the Mac: System Settings > Bluetooth > '{STATUS['bt_name']}' > Connect")
    while True:
        ctrl, intr, addr = accept_or_reconnect(lc, li)
        log(f"Mac {addr} connected")
        STATUS["host"] = {"address": addr, "connected": True, "since": time.time()}
        with open(HOST_FILE, "w") as f:
            f.write(addr)
        try:
            run_session(ctrl, intr, pad)
        except OSError as e:
            log(f"session ended: {e}")
        pad.rumble(0, 0)  # never leave the motors running when the Mac disconnects
        for s in (ctrl, intr):
            s.close()
        STATUS["host"] = {**STATUS["host"], "connected": False, "since": None}
        STATUS["link"] = {"handle": None, "sniff_disabled": False}
        log("disconnected; waiting to reconnect")


EVIOCRMFF = (1 << 30) | (4 << 16) | (ord("E") << 8) | 0x81


def usb_sniff(seconds):
    """Print the USB packets Linux sends to the controller (rumble, LEDs), with timestamps."""
    path = find_event_device()
    if not path:
        sys.exit("controller not found")
    usb = os.path.dirname(os.path.realpath(f"/sys/class/input/{os.path.basename(path)}/device/device"))
    bus, dev = int(open(f"{usb}/busnum").read()), int(open(f"{usb}/devnum").read())
    os.system("modprobe usbmon 2>/dev/null; mount -t debugfs none /sys/kernel/debug 2>/dev/null")
    mon = open(f"/sys/kernel/debug/usb/usbmon/{bus}u")
    tag = f"Io:{bus}:{dev:03d}:"
    end = time.monotonic() + seconds

    def watch():
        for line in mon:
            f = line.split()
            if len(f) > 3 and f[2] == "S" and f[3].startswith(tag) and "=" in f:
                print(time.strftime("%H:%M:%S ") + "USB -> controller: " + " ".join(f[f.index("=") + 1:]), flush=True)
    threading.Thread(target=watch, daemon=True).start()
    while time.monotonic() < end:
        time.sleep(0.2)


def rumble_test():
    """Try the ways of stopping rumble, and record the USB packets xpad sends to the controller (usbmon)."""
    path = find_event_device()
    if not path:
        sys.exit("controller not found")
    fd = os.open(path, os.O_RDWR)
    usb = os.path.dirname(os.path.realpath(f"/sys/class/input/{os.path.basename(path)}/device/device"))
    bus, dev = int(open(f"{usb}/busnum").read()), int(open(f"{usb}/devnum").read())
    os.system("modprobe usbmon 2>/dev/null; mount -t debugfs none /sys/kernel/debug 2>/dev/null")
    mon = open(f"/sys/kernel/debug/usb/usbmon/{bus}u")
    tag = f":{bus}:{dev:03d}:"
    t0 = time.monotonic()

    def say(msg):
        print(f"{time.monotonic() - t0:6.2f}s  {msg}", flush=True)

    def sniff():
        for line in mon:
            f = line.split()
            if len(f) > 3 and f[2] == "S" and f[3].startswith("Io" + tag):      # interrupt OUT submitted
                say("   USB -> controller: " + " ".join(f[f.index("=") + 1:]) if "=" in f else line.strip())
    threading.Thread(target=sniff, daemon=True).start()

    def upload(eid, strong, weak):
        buf = bytearray(FF_EFFECT.pack(FF_RUMBLE, eid, 0, 0, 0, 0, 0, strong * 257, weak * 257))
        fcntl.ioctl(fd, EVIOCSFF, buf, True)
        return FF_EFFECT.unpack(buf)[1]

    def play(eid, on):
        os.write(fd, INPUT_EVENT.pack(0, 0, EV_FF, eid, 1 if on else 0))

    say(f"controller {path} on USB bus {bus} device {dev}. Hold it and note when it buzzes and stops.")
    time.sleep(1)
    say("TEST 1: rumble 40% ... then STOP (play off)")
    e = upload(-1, 100, 100); play(e, True); time.sleep(1.5)
    play(e, False); say("TEST 1: stop sent"); time.sleep(4)
    say("TEST 2: rumble 40% ... then set strength to 0 while playing")
    e = upload(e, 100, 100); play(e, True); time.sleep(1.5)
    upload(e, 0, 0); say("TEST 2: zero strength sent"); time.sleep(4)
    play(e, False)
    say("TEST 3: rumble 40% ... then ERASE the effect")
    e = upload(e, 100, 100); play(e, True); time.sleep(1.5)
    fcntl.ioctl(fd, EVIOCRMFF, e); say("TEST 3: erase sent"); time.sleep(4)
    say("TEST 4: 300 ms timed rumble (stops by itself)")
    e = upload(-1, 100, 100)
    buf = bytearray(FF_EFFECT.pack(FF_RUMBLE, e, 0, 0, 0, 300, 0, 100 * 257, 100 * 257))
    fcntl.ioctl(fd, EVIOCSFF, buf, True); play(e, True); time.sleep(4)
    say("done. If it is still buzzing, unplug and replug the controller.")
    os.close(fd)


def main():
    global VERBOSE
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true", help="log rumble changes, report rate and queue depth")
    p.add_argument("--adapter", default="", help="e.g. hci0 (default: first adapter)")
    p.add_argument("--status-file", help="write live status JSON here twice a second (for the host app service)")
    p.add_argument("--mode", choices=["xbox-series", "xbox-one", "xbox", "ds4"],
                   help="identity to emulate: xbox-one (= xbox) or ds4 (default: follow ds4_bt_setup.sh); "
                        "xbox-series is paused")
    p.add_argument("--name", help="Bluetooth name to give the Mac (default: the plugged-in controller's short name)")
    p.add_argument("--rate", type=int, default=250, help="keepalive reports per second (default 250)")
    p.add_argument("--inflight", type=int, default=2,
                   help="max reports waiting inside the Bluetooth radio (default 2; 0 = only gate on the socket)")
    p.add_argument("--master", action="store_true",
                   help="experimental: try to keep the master role on the Bluetooth link")
    p.add_argument("--no-grab", action="store_true", help="let the Linux desktop see the controller too")
    p.add_argument("--usb-sniff", type=int, metavar="SECONDS", help="print USB packets sent to the controller, then exit")
    p.add_argument("--rumble-test", action="store_true", help="test rumble start/stop on the controller and exit")
    p.add_argument("--forget", action="store_true", help="forget the saved Mac and wait for a fresh connection")
    args = p.parse_args()
    VERBOSE = args.verbose
    global MODE, BT_NAME_OVERRIDE
    if args.mode in PAUSED_MODES:
        sys.exit(f"--mode {args.mode}: {PAUSED_MODES[args.mode]}.")
    MODE = MODE_ALIASES.get(args.mode, args.mode) or mode_from_main_conf()
    BT_NAME_OVERRIDE = args.name
    global KEEPALIVE_S, MAX_INFLIGHT
    KEEPALIVE_S = 1.0 / max(1, args.rate)
    MAX_INFLIGHT = max(0, args.inflight)
    global WANT_MASTER
    WANT_MASTER = args.master
    if os.geteuid() != 0:
        sys.exit("Run with sudo (the HID channels and the controller need root).")
    if args.usb_sniff:
        usb_sniff(args.usb_sniff)
        return
    if args.rumble_test:
        rumble_test()
        return
    if args.forget and os.path.exists(HOST_FILE):
        os.remove(HOST_FILE)
    ident = identity(MODE)
    log(f"mode {MODE}: emulating {ident['label']} ({ident['vid']}:{ident['pid']}, Bluetooth "
        f"Classic), HID report descriptor "
        f"{len(PROFILES[MODE]['descriptor'])} bytes")
    if args.status_file:
        status_writer(args.status_file)
    if os.path.exists(HOST_FILE):
        STATUS["host"]["address"] = open(HOST_FILE).read().strip()
    _keep = setup_bluez(args.adapter)  # noqa: F841  (the D-Bus objects must stay alive)
    try:
        set_link_mode()
    except OSError as e:
        log(f"could not set link mode ({e})")
    threading.Thread(target=worker, args=(not args.no_grab,), daemon=True).start()
    try:
        GLib.MainLoop().run()
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
