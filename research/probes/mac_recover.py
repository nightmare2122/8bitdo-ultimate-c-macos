"""Try to recover the controller from the state macOS leaves it in, then send the GIP handshake."""
import os, sys, threading, time
import usb.core, usb.util, usb.backend.libusb1 as b

be = b.get_backend(find_library=lambda _x: "/opt/homebrew/lib/libusb-1.0.dylib")
INIT = [bytes([5, 0x20, 0, 1, 0]), bytes([0x0A, 0x20, 1, 3, 0, 1, 0x14]), bytes([6, 0x20, 2, 2, 1, 0])]


def get_dev(wait=30):
    t0 = time.monotonic()
    while time.monotonic() - t0 < wait:
        d = usb.core.find(idVendor=0x2DC8, idProduct=0x2027, backend=be)
        if d is not None:
            return d
        time.sleep(0.2)
    sys.exit("controller not found")


def ctrl(dev, rt, req, val, idx, data=None):
    try:
        r = dev.ctrl_transfer(rt, req, val, idx, data, timeout=1000)
        return bytes(r).hex(" ") if not isinstance(r, int) else "ok"
    except usb.core.USBError as e:
        return f"error({e})"


def handshake_and_listen(dev, seconds=6):
    usb.util.claim_interface(dev, 0)
    got = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                got.append(bytes(dev.read(0x81, 64, timeout=0)))
            except usb.core.USBError as e:
                got.append(str(e))
                return
    threading.Thread(target=reader, daemon=True).start()
    for p in INIT:
        dev.write(0x02, p, timeout=1000)
    time.sleep(seconds)
    stop.set()
    return got


def clear_remote_wakeup(dev):
    return "CLEAR_FEATURE(REMOTE_WAKEUP) " + ctrl(dev, 0x00, 0x01, 1, 0) + "; status " + ctrl(dev, 0x80, 0x00, 0, 0, 2)


def reconfigure(dev):
    a = ctrl(dev, 0x00, 0x09, 0, 0)
    time.sleep(0.5)
    try:
        dev.set_configuration(1)
        c = "ok"
    except usb.core.USBError as e:
        c = f"error({e})"
    return f"SET_CONFIGURATION(0) {a}; set_configuration(1) {c}"


def clear_halts(dev):
    usb.util.claim_interface(dev, 0)
    return " ".join(f"CLEAR_HALT {ep:#04x} " + ctrl(dev, 0x02, 0x01, 0, ep) for ep in (0x81, 0x02))


ATTEMPTS = [("disable remote wakeup", clear_remote_wakeup),
            ("unconfigure + reconfigure", reconfigure),
            ("clear endpoint halts", clear_halts),
            ("all three", lambda d: " / ".join(f(d) for f in (clear_remote_wakeup, reconfigure, clear_halts)))]

for name, fn in ATTEMPTS:
    dev = get_dev()
    try:
        dev.set_configuration(1)
    except usb.core.USBError:
        pass
    print(f"\n=== {name}\n    status before: {ctrl(dev, 0x80, 0x00, 0, 0, 2)}")
    print("    " + fn(dev))
    pkts = handshake_and_listen(dev)
    data = [p for p in pkts if isinstance(p, bytes)]
    errs = [p for p in pkts if isinstance(p, str)]
    print(f"    received {len(data)} packets {errs[:1]}")
    for p in data[:4]:
        print("     <-", p.hex(" "))
    try:
        usb.util.release_interface(dev, 0)
    except usb.core.USBError:
        pass
    usb.util.dispose_resources(dev)
    if data:
        print("\nHANDSHAKE WORKED with:", name)
        os._exit(0)
    time.sleep(1)
print("\nNo attempt produced input.")
os._exit(0)
