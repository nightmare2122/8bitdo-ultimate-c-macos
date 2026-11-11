#!/usr/bin/env python3
"""Linux lab 2: imitate things macOS may do when it connects/configures a USB device,
one at a time, and see which one makes the 8BitDo Ultimate C go dark / stop sending input.

Needs linux_lab.py in the same folder (reuses its helpers).
Usage:  sudo python3 linux_lab2.py
"""
import os
import subprocess
import sys
import time

import usb.core
import usb.util

import linux_lab as lab

GET_STATUS, CLEAR_FEATURE, SET_FEATURE = 0x00, 0x01, 0x03
GET_DESCRIPTOR, GET_CONFIGURATION, SET_CONFIGURATION = 0x06, 0x08, 0x09
GET_INTERFACE, SET_INTERFACE = 0x0A, 0x0B


def ctrl(dev, req_type, req, value, index, length_or_data=None):
    """Send one control request; a STALL is a normal answer for unsupported requests."""
    try:
        r = dev.ctrl_transfer(req_type, req, value, index, length_or_data, timeout=1000)
        return bytes(r).hex(" ") if not isinstance(r, int) else f"ok({r})"
    except usb.core.USBError as e:
        return f"error({e})"


def m_remote_wakeup(dev):
    return ctrl(dev, 0x00, SET_FEATURE, 1, 0)  # DEVICE_REMOTE_WAKEUP


def m_deconfigure(dev):
    a = ctrl(dev, 0x00, SET_CONFIGURATION, 0, 0)
    time.sleep(0.3)
    b = ctrl(dev, 0x00, SET_CONFIGURATION, 1, 0)
    return f"cfg0 {a}, cfg1 {b}"


def m_suspend_resume(dev):
    """Put the controller into USB suspend for 3 s, then resume it."""
    path = None
    for d in os.listdir("/sys/bus/usb/devices"):
        p = f"/sys/bus/usb/devices/{d}"
        try:
            if open(f"{p}/idVendor").read().strip() == "2dc8" and ":" not in d:
                path = p
        except OSError:
            pass
    if path is None:
        return "sysfs path not found"
    usb.util.dispose_resources(dev)  # an open usbfs handle would keep it awake
    open(f"{path}/power/autosuspend_delay_ms", "w").write("0")
    open(f"{path}/power/control", "w").write("auto")
    time.sleep(3)
    state = open(f"{path}/power/runtime_status").read().strip()
    open(f"{path}/power/control", "w").write("on")
    time.sleep(0.3)
    return f"runtime_status during wait: {state}"


def m_strings_short_first(dev):
    out = []
    for idx, lang in ((0, 0), (1, 0x0409), (2, 0x0409), (3, 0x0409)):
        out.append(ctrl(dev, 0x80, GET_DESCRIPTOR, 0x0300 | idx, lang, 2))
        out.append(ctrl(dev, 0x80, GET_DESCRIPTOR, 0x0300 | idx, lang, 255)[:20])
    return " | ".join(out)


def m_ms_os_descriptor(dev):
    return ctrl(dev, 0x80, GET_DESCRIPTOR, 0x03EE, 0, 18)


def m_status_queries(dev):
    return " | ".join([
        "qualifier " + ctrl(dev, 0x80, GET_DESCRIPTOR, 0x0600, 0, 10),
        "other_speed " + ctrl(dev, 0x80, GET_DESCRIPTOR, 0x0700, 0, 9),
        "status " + ctrl(dev, 0x80, GET_STATUS, 0, 0, 2),
        "get_config " + ctrl(dev, 0x80, GET_CONFIGURATION, 0, 0, 1),
        "get_intf0 " + ctrl(dev, 0x81, GET_INTERFACE, 0, 0, 1),
        "get_intf1 " + ctrl(dev, 0x81, GET_INTERFACE, 0, 1, 1),
    ])


def m_clear_halt(dev):
    return " | ".join(ctrl(dev, 0x02, CLEAR_FEATURE, 0, ep) for ep in (0x81, 0x02))


def m_set_interface0(dev):
    return ctrl(dev, 0x01, SET_INTERFACE, 0, 0)


def m_config_desc_long(dev):
    return ctrl(dev, 0x80, GET_DESCRIPTOR, 0x0200, 0, 0xFFFF)[:30]


MACISMS = [
    ("A remote_wakeup     SET_FEATURE(DEVICE_REMOTE_WAKEUP)", m_remote_wakeup),
    ("B deconfigure       SET_CONFIGURATION 0 then 1", m_deconfigure),
    ("C suspend_resume    USB suspend 3 s, then resume", m_suspend_resume),
    ("D strings_short     string descriptors read 2 bytes first", m_strings_short_first),
    ("E ms_os_descriptor  string 0xEE (Microsoft OS descriptor)", m_ms_os_descriptor),
    ("F status_queries    qualifier/other-speed/GET_STATUS/GET_CONFIG/GET_INTERFACE", m_status_queries),
    ("G clear_halt        CLEAR_FEATURE(ENDPOINT_HALT) on 0x81 and 0x02", m_clear_halt),
    ("H set_interface0    SET_INTERFACE(interface 0, alt 0)", m_set_interface0),
    ("I config_desc_long  config descriptor with wLength 0xFFFF", m_config_desc_long),
]


def main():
    if os.geteuid() != 0:
        sys.exit("run with sudo")
    out_dir = f"linux_lab2_{time.strftime('%Y%m%d_%H%M%S')}"
    os.makedirs(out_dir)
    os.makedirs(os.path.dirname(lab.BLOCK_XPAD), exist_ok=True)
    with open(lab.BLOCK_XPAD, "w") as f:
        f.write("blacklist xpad\ninstall xpad /bin/true\n")
    r = subprocess.run(["modprobe", "-r", "xpad"], capture_output=True, text=True)
    if r.returncode != 0:
        os.remove(lab.BLOCK_XPAD)
        sys.exit(f"could not unload xpad (close games/Steam using the controller): {r.stderr.strip()}")
    subprocess.run(["modprobe", "usbmon"], check=False)
    cap = subprocess.Popen(["tcpdump", "-i", "usbmon0", "-s", "0", "-w", f"{out_dir}/lab2.pcap"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)
    report = []
    try:
        for name, fn in MACISMS:
            print(f"\n=== {name}")
            input("    Press ENTER to start ... ")
            dev, _ = lab.fresh_device()
            drivers = lab.bound_drivers()
            if drivers:
                print(f"    WARNING: kernel driver still bound: {drivers}; result is not valid")
            result = fn(dev)
            print(f"    request result: {result}")
            light_before = input("    Light right now, BEFORE the startup packets? (solid/blinking/off): ").strip()
            dev = lab.find() or dev  # suspend test released the old handle
            print("    Sending the startup that works on Linux. Press buttons / move sticks for ~6 s ...")
            try:
                lab.exp_linux_like(dev)
                err = ""
            except usb.core.USBError as e:
                err = f"USB error: {e}"
            pkts = lab.listen(dev, lab.LISTEN_S)
            inputs = [p for p in pkts if isinstance(p, bytes) and p[:1] == b"\x20"]
            light_after = input("    Light now, AFTER the startup? (solid/blinking/off): ").strip()
            line = (f"{name}: result=[{result}] light_before={light_before} | "
                    f"{len(pkts)} packets, {len(inputs)} input, light_after={light_after} "
                    f"drivers={drivers or 'none'} {err}")
            print("   ", line)
            report.append(line)
            try:
                usb.util.release_interface(dev, 0)
            except usb.core.USBError:
                pass
            usb.util.dispose_resources(dev)
    finally:
        cap.terminate()
        cap.wait()
        if os.path.exists(lab.BLOCK_XPAD):
            os.remove(lab.BLOCK_XPAD)
        subprocess.run(["modprobe", "xpad"], check=False)
        with open(f"{out_dir}/report.txt", "w") as f:
            f.write("\n".join(report) + "\n")
        subprocess.run(["tar", "czf", f"{out_dir}.tar.gz", out_dir], check=False)
        os.chmod(f"{out_dir}.tar.gz", 0o644)
        print(f"\nDone. Copy {os.path.abspath(out_dir)}.tar.gz to the Mac project folder.")


if __name__ == "__main__":
    main()
