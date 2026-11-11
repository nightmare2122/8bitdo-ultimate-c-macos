"""Wait for a fresh plug-in, then send the Linux handshake as fast as possible after SET_CONFIGURATION."""
import os, sys, threading, time
import usb.core, usb.util, usb.backend.libusb1 as b

be = b.get_backend(find_library=lambda _x: "/opt/homebrew/lib/libusb-1.0.dylib")
find = lambda: usb.core.find(idVendor=0x2DC8, idProduct=0x2027, backend=be)
t0 = time.monotonic()
T = lambda: f"{(time.monotonic() - t0) * 1000:9.1f}ms"
INIT = [bytes([5, 0x20, 0, 1, 0]), bytes([0x0A, 0x20, 1, 3, 0, 1, 0x14]), bytes([6, 0x20, 2, 2, 1, 0])]

while find() is not None:
    if time.monotonic() - t0 > 60: sys.exit("controller was never unplugged")
    time.sleep(0.02)
print(T(), "unplugged"); sys.stdout.flush()

dev = None
while True:
    if time.monotonic() - t0 > 90: sys.exit("controller never came back")
    d = find()
    if d is None: time.sleep(0.001); continue
    try:
        d.set_configuration(1); dev = d; break
    except usb.core.USBError:
        time.sleep(0.001)
tc = time.monotonic(); log = [("set_configuration", tc)]
usb.util.claim_interface(dev, 0); log.append(("claim0", time.monotonic()))
usb.util.claim_interface(dev, 1); log.append(("claim1", time.monotonic()))
dev.set_interface_altsetting(interface=1, alternate_setting=0); log.append(("set_interface(1,0)", time.monotonic()))
for p in INIT:
    dev.write(0x02, p, timeout=1000); log.append(("write " + p.hex(" "), time.monotonic()))
for name, t in log: print(f"  +{(t - tc) * 1000:6.1f}ms  {name}")
sys.stdout.flush()

n = [0]
def reader():
    last = None
    while True:
        try: x = bytes(dev.read(0x81, 64, timeout=0))
        except usb.core.USBError as e: print(T(), "read error:", e); sys.stdout.flush(); return
        n[0] += 1
        if x != last and n[0] < 60: print(T(), "<-", x.hex(" ")); sys.stdout.flush()
        last = x
threading.Thread(target=reader, daemon=True).start()
time.sleep(20)
print("packets:", n[0]); sys.stdout.flush()
os._exit(0)
