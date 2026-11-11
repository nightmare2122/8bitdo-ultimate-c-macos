#!/usr/bin/env python3
"""Linux side of the 8BitDo bridge: read the controller through xpad, stream it to the Mac.

The 8BitDo Ultimate C (2dc8:2027) works on Linux with xpad but goes dark on macOS. This script
reads the controller's evdev device on the Fedora PC and sends its state over UDP to
bridge_mac.py on the Mac. Any IP link works: Wi-Fi, LAN, or an Ethernet cable straight
between the two machines.

Usage (controller plugged into the Fedora PC, xpad loaded as normal):
    sudo python3 bridge_linux.py MAC_ADDRESS_OR_IP          e.g.  sudo python3 bridge_linux.py 192.168.1.20
    sudo python3 bridge_linux.py --broadcast                 send to every machine on the link
Options:  --port 47800   --no-grab (also let the Linux desktop see the controller)   --verbose
Python standard library only. Needs read access to /dev/input/event* (root, or the 'input' group).
"""
import argparse
import errno
import fcntl
import os
import select
import socket
import struct
import sys
import time

VID = "2dc8"
PORT = 47800
MAGIC = b"8BDB"
VERSION = 1
# magic, version, seq, buttons, lx, ly, rx, ry, lt, rt  (sticks -32767..32767, y down is positive;
# triggers 0..65535). Must match bridge_mac.py.
PACKET = struct.Struct("<4sBIIhhhhHH")
# seq 0 means "everything released"; the Mac always accepts it, whatever the last seq was.
RELEASE = PACKET.pack(MAGIC, VERSION, 0, 0, 0, 0, 0, 0, 0, 0)
KEEPALIVE_S = 0.05  # resend the state this often even when nothing changes, so a lost packet heals

BUTTONS = ["A", "B", "X", "Y", "LB", "RB", "View", "Menu", "Guide", "LS", "RS",
           "Up", "Down", "Left", "Right", "Share"]
BIT = {name: 1 << i for i, name in enumerate(BUTTONS)}

EV_SYN, EV_KEY, EV_ABS = 0x00, 0x01, 0x03
KEY_CODES = {
    304: "A", 305: "B", 307: "X", 308: "Y", 310: "LB", 311: "RB",
    314: "View", 315: "Menu", 316: "Guide", 317: "LS", 318: "RS",
    167: "Share",  # KEY_RECORD, used by newer xpad for the share button
    # xpad with dpad_to_buttons=1 reports the D-pad as BTN_TRIGGER_HAPPY1..4
    704: "Left", 705: "Right", 706: "Up", 707: "Down",
}
ABS_X, ABS_Y, ABS_Z, ABS_RX, ABS_RY, ABS_RZ, ABS_HAT0X, ABS_HAT0Y = 0, 1, 2, 3, 4, 5, 16, 17
STICKS = {ABS_X: "lx", ABS_Y: "ly", ABS_RX: "rx", ABS_RY: "ry"}
TRIGGERS = {ABS_Z: "lt", ABS_RZ: "rt"}

INPUT_EVENT = struct.Struct("@llHHi")  # struct input_event on 64-bit Linux
ABSINFO = struct.Struct("@iiiiii")     # value, minimum, maximum, fuzz, flat, resolution


def _ioc(direction, nr, size):
    return (direction << 30) | (size << 16) | (ord("E") << 8) | nr


def EVIOCGABS(axis):
    return _ioc(2, 0x40 + axis, ABSINFO.size)


EVIOCGRAB = _ioc(1, 0x90, 4)


class State:
    """Controller state built from evdev events, scaled to the wire format."""

    def __init__(self, ranges):
        self.ranges = ranges  # axis code -> (minimum, maximum)
        self.buttons = 0
        self.axes = {"lx": 0, "ly": 0, "rx": 0, "ry": 0, "lt": 0, "rt": 0}

    def _scale_stick(self, code, value):
        lo, hi = self.ranges.get(code, (-32768, 32767))
        if hi <= lo:
            return 0
        v = (2 * (value - lo) / (hi - lo)) - 1
        return max(-32767, min(32767, round(v * 32767)))

    def _scale_trigger(self, code, value):
        lo, hi = self.ranges.get(code, (0, 1023))
        if hi <= lo:
            return 0
        return max(0, min(65535, round((value - lo) * 65535 / (hi - lo))))

    def _set(self, name, pressed):
        if pressed:
            self.buttons |= BIT[name]
        else:
            self.buttons &= ~BIT[name]

    def apply(self, ev_type, code, value):
        if ev_type == EV_KEY and code in KEY_CODES:
            self._set(KEY_CODES[code], value != 0)
        elif ev_type == EV_ABS:
            if code in STICKS:
                self.axes[STICKS[code]] = self._scale_stick(code, value)
            elif code in TRIGGERS:
                self.axes[TRIGGERS[code]] = self._scale_trigger(code, value)
            elif code == ABS_HAT0X:
                self._set("Left", value < 0)
                self._set("Right", value > 0)
            elif code == ABS_HAT0Y:
                self._set("Up", value < 0)
                self._set("Down", value > 0)

    def pack(self, seq):
        a = self.axes
        return PACKET.pack(MAGIC, VERSION, seq, self.buttons,
                           a["lx"], a["ly"], a["rx"], a["ry"], a["lt"], a["rt"])

    def describe(self):
        held = [n for n in BUTTONS if self.buttons & BIT[n]]
        a = self.axes
        return (f"L({a['lx']:6d},{a['ly']:6d}) R({a['rx']:6d},{a['ry']:6d}) "
                f"LT {a['lt']:5d} RT {a['rt']:5d}  {' '.join(held)}")


def find_event_device():
    """Path of the controller's /dev/input/eventN, or None. Picks the joystick node (has jsN)."""
    try:
        blocks = open("/proc/bus/input/devices").read().split("\n\n")
    except OSError:
        return None
    for block in blocks:
        if f"Vendor={VID}" not in block:
            continue
        for line in block.splitlines():
            if line.startswith("H: Handlers="):
                handlers = line.split("=", 1)[1].split()
                events = [h for h in handlers if h.startswith("event")]
                if events and any(h.startswith("js") for h in handlers):
                    return f"/dev/input/{events[0]}"
    return None


def read_ranges(fd):
    ranges = {}
    for code in (*STICKS, *TRIGGERS):
        try:
            info = ABSINFO.unpack(fcntl.ioctl(fd, EVIOCGABS(code), bytes(ABSINFO.size)))
            ranges[code] = (info[1], info[2])
        except OSError:
            pass
    return ranges


def wait_for_device():
    shown = False
    while True:
        path = find_event_device()
        if path:
            return path
        if not shown:
            print(f"Waiting for an 8BitDo controller (vendor {VID}) ... is it plugged in and is xpad loaded?")
            shown = True
        time.sleep(1)


def run(sock, target, grab, verbose):
    seq = 0
    while True:
        path = wait_for_device()
        try:
            fd = os.open(path, os.O_RDONLY)
        except PermissionError:
            sys.exit(f"No permission to read {path}. Run with sudo, or add yourself to the 'input' group.")
        name = open(f"/sys/class/input/{os.path.basename(path)}/device/name").read().strip()
        if grab:
            try:
                fcntl.ioctl(fd, EVIOCGRAB, 1)
            except OSError as e:
                print(f"Could not grab {path} ({e}); the Linux desktop will also see the input.")
        state = State(read_ranges(fd))
        print(f"Streaming '{name}' ({path}) to {target[0]}:{target[1]}. Ctrl-C to stop.")
        last_send = 0.0
        try:
            while True:
                ready, _, _ = select.select([fd], [], [], KEEPALIVE_S)
                changed = False
                if ready:
                    data = os.read(fd, INPUT_EVENT.size * 64)
                    for off in range(0, len(data) - INPUT_EVENT.size + 1, INPUT_EVENT.size):
                        _, _, ev_type, code, value = INPUT_EVENT.unpack_from(data, off)
                        if ev_type != EV_SYN:
                            state.apply(ev_type, code, value)
                            changed = True
                now = time.monotonic()
                if changed or now - last_send >= KEEPALIVE_S:
                    seq = seq % 0xFFFFFFFF + 1  # never 0, that is RELEASE
                    try:
                        sock.sendto(state.pack(seq), target)
                    except OSError as e:
                        print(f"send failed: {e}")  # e.g. network down; keep reading, retry next tick
                    last_send = now
                    if changed and verbose:
                        print(state.describe())
        except OSError as e:
            if e.errno != errno.ENODEV:
                raise
            print("Controller disconnected.")
            sock.sendto(RELEASE, target)
        finally:
            os.close(fd)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("host", nargs="?", help="the Mac's IP address or hostname")
    p.add_argument("--broadcast", action="store_true", help="send to 255.255.255.255 instead of one host")
    p.add_argument("--port", type=int, default=PORT)
    p.add_argument("--no-grab", action="store_true", help="do not take exclusive use of the controller")
    p.add_argument("--verbose", "-v", action="store_true", help="print every state change")
    args = p.parse_args()
    if not args.host and not args.broadcast:
        p.error("give the Mac's address, or --broadcast")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if args.broadcast:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        target = ("255.255.255.255", args.port)
    else:
        target = (socket.gethostbyname(args.host), args.port)
    try:
        run(sock, target, not args.no_grab, args.verbose)
    except KeyboardInterrupt:
        # Tell the Mac to let go of everything right away instead of waiting for its timeout.
        sock.sendto(RELEASE, target)
        print()


if __name__ == "__main__":
    main()
