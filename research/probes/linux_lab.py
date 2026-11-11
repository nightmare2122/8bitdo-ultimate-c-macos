#!/usr/bin/env python3
"""Linux lab for the 8BitDo Ultimate C (2dc8:2027): run the Mac handshake code on Linux.

Unloads xpad, then runs several handshake variants. Before each one the controller is
USB-reset, so it starts from the same state as a fresh plug-in. All USB traffic is recorded
with usbmon so every step can be compared with the Mac.

Usage (controller plugged in, light in any state):
    sudo python3 linux_lab.py
Needs: python3-pyusb (Fedora: sudo dnf install python3-pyusb), tcpdump.
Restores xpad at the end.
"""
import os
import subprocess
import sys
import threading
import time

import usb.core
import usb.util

VID, PID = 0x2DC8, 0x2027
# Temporary rule so udev cannot auto-load xpad when the controller re-enumerates after a reset.
# /run is cleared on reboot, and the script deletes the file when it finishes.
BLOCK_XPAD = "/run/modprobe.d/zz-linux-lab-block-xpad.conf"
INIT = [bytes([5, 0x20, 0, 1, 0]), bytes([0x0A, 0x20, 1, 3, 0, 1, 0x14]), bytes([6, 0x20, 2, 2, 1, 0])]
LISTEN_S = 6


def find():
    return usb.core.find(idVendor=VID, idProduct=PID)


def fresh_device():
    """USB-reset the controller and wait until it is back and configured by the kernel."""
    dev = find()
    if dev is None:
        sys.exit("controller not found")
    try:
        dev.reset()
    except usb.core.USBError:
        pass  # the device can vanish mid-reset; we just wait for it below
    usb.util.dispose_resources(dev)
    t0 = time.monotonic()
    while time.monotonic() - t0 < 10:
        time.sleep(0.05)
        dev = find()
        if dev is None:
            continue
        try:
            if dev.get_active_configuration().bConfigurationValue == 1:
                return dev, time.monotonic()
        except usb.core.USBError:
            continue
    sys.exit("controller did not come back after reset")


def bound_drivers():
    """Kernel drivers currently bound to the controller's interfaces (should be none)."""
    found = []
    for d in os.listdir("/sys/bus/usb/devices"):
        base = f"/sys/bus/usb/devices/{d}"
        try:
            with open(f"{base}/idVendor") as f:
                if f.read().strip() != f"{VID:04x}":
                    continue
        except OSError:
            continue
        for i in os.listdir("/sys/bus/usb/devices"):
            if i.startswith(d + ":") and os.path.exists(f"/sys/bus/usb/devices/{i}/driver"):
                found.append(f"{i}->{os.path.basename(os.readlink(f'/sys/bus/usb/devices/{i}/driver'))}")
    return found


def listen(dev, seconds):
    """Collect IN packets with a read that never times out (like xpad / the Mac test)."""
    got = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                got.append(bytes(dev.read(0x81, 64, timeout=500)))
            except usb.core.USBTimeoutError:
                continue
            except usb.core.USBError as e:
                got.append(f"read error: {e}")
                return

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    time.sleep(seconds)
    stop.set()
    th.join(1)
    return got


def send_init(dev):
    for p in INIT:
        dev.write(0x02, p, timeout=1000)


def set_alt_raw(dev):
    # Same raw request the Linux kernel sent: SET_INTERFACE(interface 1, alt 0).
    dev.ctrl_transfer(0x01, 0x0B, 0, 1, None, timeout=1000)


def exp_linux_like(dev):
    usb.util.claim_interface(dev, 0)
    set_alt_raw(dev)
    send_init(dev)


def exp_mac_like(dev):
    usb.util.claim_interface(dev, 0)
    usb.util.claim_interface(dev, 1)
    dev.set_interface_altsetting(interface=1, alternate_setting=0)
    send_init(dev)


def exp_no_altset(dev):
    usb.util.claim_interface(dev, 0)
    send_init(dev)


def exp_delayed(seconds):
    def run(dev):
        time.sleep(seconds)
        exp_linux_like(dev)
    return run


def exp_reconfigure(dev):
    # Select configuration 1 again from userspace, as every Mac test did.
    dev.set_configuration(1)
    exp_linux_like(dev)


def exp_nothing(dev):
    # Kernel has selected configuration 1; send nothing. Compare the light with the Mac.
    usb.util.claim_interface(dev, 0)


EXPERIMENTS = [
    ("0 config_only     (no GIP packets at all)", exp_nothing),
    ("1 linux_like     (claim, raw SET_INTERFACE, init)", exp_linux_like),
    ("2 mac_like        (claim both, alt via API, init)", exp_mac_like),
    ("3 no_altset       (init without SET_INTERFACE)", exp_no_altset),
    ("4 delayed_1s      (linux_like after 1 s)", exp_delayed(1)),
    ("5 delayed_10s     (linux_like after 10 s)", exp_delayed(10)),
    ("6 reconfigure     (SET_CONFIGURATION again, then linux_like)", exp_reconfigure),
]


def main():
    if os.geteuid() != 0:
        sys.exit("run with sudo")
    out_dir = f"linux_lab_{time.strftime('%Y%m%d_%H%M%S')}"
    os.makedirs(out_dir)
    os.makedirs(os.path.dirname(BLOCK_XPAD), exist_ok=True)
    with open(BLOCK_XPAD, "w") as f:
        f.write("blacklist xpad\ninstall xpad /bin/true\n")
    r = subprocess.run(["modprobe", "-r", "xpad"], capture_output=True, text=True)
    if r.returncode != 0:
        os.remove(BLOCK_XPAD)
        sys.exit(f"could not unload xpad (close games/Steam using the controller): {r.stderr.strip()}")
    subprocess.run(["modprobe", "usbmon"], check=False)
    cap = subprocess.Popen(["tcpdump", "-i", "usbmon0", "-s", "0", "-w", f"{out_dir}/lab.pcap"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)
    report = []
    try:
        for name, fn in EXPERIMENTS:
            print(f"\n=== {name}")
            input("    Press ENTER, then press buttons / move sticks for ~6 s ... ")
            dev, _ = fresh_device()
            drivers = bound_drivers()
            if drivers:
                print(f"    WARNING: kernel driver still bound: {drivers}; result is not valid")
            t_start = time.monotonic()
            try:
                fn(dev)
                err = ""
            except usb.core.USBError as e:
                err = f"USB error: {e}"
            pkts = listen(dev, LISTEN_S)
            inputs = [p for p in pkts if isinstance(p, bytes) and p[:1] == b"\x20"]
            errors = [p for p in pkts if isinstance(p, str)]
            line = (f"{name}: {len(pkts)} packets, {len(inputs)} input; drivers={drivers or 'none'}; "
                    f"{err} {' '.join(errors)}")
            print("   ", line)
            for p in [p for p in pkts if isinstance(p, bytes)][:5]:
                print("     <-", p.hex(" "))
            light = input("    What is the controller light doing now? (solid/blinking/off): ").strip()
            report.append(f"{line} | light: {light}")
            try:
                usb.util.release_interface(dev, 0)
                usb.util.release_interface(dev, 1)
            except usb.core.USBError:
                pass
            usb.util.dispose_resources(dev)
    finally:
        cap.terminate()
        cap.wait()
        if os.path.exists(BLOCK_XPAD):
            os.remove(BLOCK_XPAD)
        subprocess.run(["modprobe", "xpad"], check=False)
        with open(f"{out_dir}/report.txt", "w") as f:
            f.write("\n".join(report) + "\n")
        subprocess.run(["tar", "czf", f"{out_dir}.tar.gz", out_dir], check=False)
        os.chmod(f"{out_dir}.tar.gz", 0o644)
        print(f"\nDone. Copy {os.path.abspath(out_dir)}.tar.gz to the Mac project folder.")


if __name__ == "__main__":
    main()
