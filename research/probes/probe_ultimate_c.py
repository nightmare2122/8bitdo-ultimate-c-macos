#!/usr/bin/env python3
"""Read-only-ish probe for the 8BitDo Ultimate C Wired for Xbox (2dc8:2027).

Claims the vendor-specific GIP interface (class 0xFF / subclass 0x47 / protocol 0xD0),
sends the same init packets Linux xpad sends to generic XTYPE_XBOXONE pads
(power on, LED on, auth done), then prints raw + decoded packets.

It writes nothing persistent to the controller (no firmware / config commands).

Run:  .venv/bin/python probe_ultimate_c.py [seconds]
"""
import sys
import time

import usb.backend.libusb1
import usb.core
import usb.util

VID, PID = 0x2DC8, 0x2027
GIP_CLASS, GIP_SUBCLASS, GIP_PROTOCOL = 0xFF, 0x47, 0xD0

GIP_CMD_ACK, GIP_CMD_ANNOUNCE, GIP_CMD_VIRTUAL_KEY, GIP_CMD_INPUT = 0x01, 0x02, 0x07, 0x20
GIP_OPT_ACK, GIP_OPT_INTERNAL = 0x10, 0x20

# Same packets, same order as xpad's xboxone_init_packets[] for an unlisted 0x2dc8 pad.
# Byte 2 is the sequence number and gets overwritten before sending.
INIT_PACKETS = [
    ("power_on", [0x05, 0x20, 0x00, 0x01, 0x00]),
    ("led_on", [0x0A, 0x20, 0x00, 0x03, 0x00, 0x01, 0x14]),
    ("auth_done", [0x06, 0x20, 0x00, 0x02, 0x01, 0x00]),
]

LIBUSB_PATHS = ["/opt/homebrew/lib/libusb-1.0.dylib", "/usr/local/lib/libusb-1.0.dylib"]


def backend():
    for path in LIBUSB_PATHS:
        be = usb.backend.libusb1.get_backend(find_library=lambda _x, p=path: p)
        if be is not None:
            return be
    sys.exit("libusb not found. Run: brew install libusb")


def hexs(data):
    return " ".join(f"{b:02x}" for b in data)


def s16(data, off):
    return int.from_bytes(bytes(data[off:off + 2]), "little", signed=True)


def u16(data, off):
    return int.from_bytes(bytes(data[off:off + 2]), "little", signed=False)


def decode_input(d):
    """Decode a GIP_CMD_INPUT packet using xpadone_process_packet's layout."""
    if len(d) < 18:
        return f"(short input packet, {len(d)} bytes)"
    names4 = {2: "MENU", 3: "VIEW", 4: "A", 5: "B", 6: "X", 7: "Y"}
    names5 = {0: "UP", 1: "DOWN", 2: "LEFT", 3: "RIGHT", 4: "LB", 5: "RB", 6: "LS", 7: "RS"}
    pressed = [n for b, n in names4.items() if d[4] & (1 << b)]
    pressed += [n for b, n in names5.items() if d[5] & (1 << b)]
    # xpad negates Y (~value) so up is negative for evdev; here we keep raw (up = positive).
    return (f"LT={u16(d, 6):4d} RT={u16(d, 8):4d} "
            f"LX={s16(d, 10):6d} LY={s16(d, 12):6d} RX={s16(d, 14):6d} RY={s16(d, 16):6d} "
            f"btn=[{' '.join(pressed)}]")


def find_gip_interface(dev):
    for cfg in dev:
        for intf in cfg:
            if (intf.bInterfaceClass, intf.bInterfaceSubClass, intf.bInterfaceProtocol) == \
                    (GIP_CLASS, GIP_SUBCLASS, GIP_PROTOCOL):
                return cfg, intf
    return None, None


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    duration = float(args[0]) if args else 30.0
    dev = usb.core.find(idVendor=VID, idProduct=PID, backend=backend())
    if dev is None:
        sys.exit(f"Controller {VID:04x}:{PID:04x} not found. Is it plugged in and allowed "
                 "in the 'Allow accessory to connect' prompt?")

    if "--reset" in flags:
        # A port reset makes the pad re-enumerate, so we can catch its GIP announce packet.
        print("Resetting device")
        dev.reset()
        time.sleep(1.0)
        dev = usb.core.find(idVendor=VID, idProduct=PID, backend=backend())
        if dev is None:
            sys.exit("Controller disappeared after reset.")

    print(f"Found: {usb.util.get_string(dev, dev.iManufacturer)!r} "
          f"{usb.util.get_string(dev, dev.iProduct)!r} bcdDevice=0x{dev.bcdDevice:04x}")
    print(f"Device class/sub/proto: {dev.bDeviceClass:#04x}/{dev.bDeviceSubClass:#04x}/"
          f"{dev.bDeviceProtocol:#04x}, {dev.bNumConfigurations} config(s)")
    for cfg in dev:
        for intf in cfg:
            eps = ", ".join(f"ep{e.bEndpointAddress:#04x} "
                            f"{['ctrl', 'iso', 'bulk', 'intr'][e.bmAttributes & 3]} "
                            f"max{e.wMaxPacketSize}" for e in intf)
            print(f"  cfg{cfg.bConfigurationValue} intf{intf.bInterfaceNumber} "
                  f"alt{intf.bAlternateSetting} class {intf.bInterfaceClass:#04x}/"
                  f"{intf.bInterfaceSubClass:#04x}/{intf.bInterfaceProtocol:#04x}: {eps}")

    cfg, intf = find_gip_interface(dev)
    if intf is None:
        sys.exit("No 0xFF/0x47/0xD0 interface found; this is not a GIP device as configured.")

    # A class-0xFF device has no macOS driver, so it may still be unconfigured.
    try:
        active = dev.get_active_configuration()
    except usb.core.USBError:
        active = None
    if active is None or active.bConfigurationValue != cfg.bConfigurationValue:
        print(f"Setting configuration {cfg.bConfigurationValue}")
        dev.set_configuration(cfg.bConfigurationValue)

    num = intf.bInterfaceNumber
    try:
        usb.util.claim_interface(dev, num)
    except usb.core.USBError as e:
        sys.exit(f"claim_interface({num}) failed: {e}\n"
                 "Another driver (e.g. a leftover 360Controller / DriverKit extension) may own it.")
    if intf.bAlternateSetting:
        dev.set_interface_altsetting(num, intf.bAlternateSetting)

    ep_in = usb.util.find_descriptor(intf, custom_match=lambda e:
                                     usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN)
    ep_out = usb.util.find_descriptor(intf, custom_match=lambda e:
                                      usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT)
    if ep_in is None or ep_out is None:
        sys.exit("GIP interface lacks an IN or OUT endpoint.")
    print(f"Claimed intf{num}: IN {ep_in.bEndpointAddress:#04x}, OUT {ep_out.bEndpointAddress:#04x}")

    def drain(seconds, label):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                d = ep_in.read(ep_in.wMaxPacketSize, timeout=100)
                print(f"<- {label}: {hexs(d)}")
            except usb.core.USBError:
                pass

    drain(1.5, "before init")

    packets = list(INIT_PACKETS)
    if "--s-init" in flags:
        packets.insert(1, ("s_init", [0x05, 0x20, 0x00, 0x0F, 0x06]))
    seq = 0
    for name, pkt in packets:
        pkt = list(pkt)
        pkt[2] = seq
        seq = (seq + 1) & 0xFF
        try:
            n = ep_out.write(bytes(pkt), timeout=1000)
            print(f"-> {name:9s} {hexs(pkt)}  ({n} bytes written)")
        except usb.core.USBError as e:
            print(f"-> {name:9s} {hexs(pkt)}  WRITE FAILED: {e}")

    print(f"Listening {duration:.0f}s. Move sticks / press buttons. Ctrl+C to stop.\n")
    end = time.monotonic() + duration
    last_input = None
    counts = {}
    try:
        while time.monotonic() < end:
            try:
                d = ep_in.read(ep_in.wMaxPacketSize, timeout=250)
            except usb.core.USBTimeoutError:
                continue
            except usb.core.USBError as e:
                if e.errno == 60 or "timed out" in str(e).lower():
                    continue
                raise
            if not d:
                continue
            cmd = d[0]
            counts[cmd] = counts.get(cmd, 0) + 1
            if cmd == GIP_CMD_INPUT:
                # Only print when the payload changes, otherwise it floods.
                if bytes(d[4:]) != last_input:
                    last_input = bytes(d[4:])
                    print(f"INPUT  {hexs(d)}\n       {decode_input(d)}")
            elif cmd == GIP_CMD_VIRTUAL_KEY:
                print(f"GUIDE  {hexs(d)}  pressed={bool(d[4] & 0x03)}")
                if len(d) > 2 and d[1] == (GIP_OPT_ACK | GIP_OPT_INTERNAL):
                    ack = [GIP_CMD_ACK, GIP_OPT_INTERNAL, d[2], 0x09,
                           0x00, GIP_CMD_VIRTUAL_KEY, GIP_OPT_INTERNAL, 0x02, 0, 0, 0, 0, 0]
                    ep_out.write(bytes(ack), timeout=1000)
                    print(f"-> ack    {hexs(ack)}")
            elif cmd == GIP_CMD_ANNOUNCE:
                print(f"ANNOUNCE {hexs(d)}")
            else:
                print(f"OTHER cmd={cmd:#04x} {hexs(d)}")
    except KeyboardInterrupt:
        pass
    finally:
        usb.util.release_interface(dev, num)
        usb.util.dispose_resources(dev)

    print("\nPacket counts by command:", {f"{k:#04x}": v for k, v in counts.items()} or "NONE")
    if not counts:
        print("The controller sent nothing after init.")


if __name__ == "__main__":
    main()
