#!/usr/bin/env python3
"""Stand-in for ds4_bt_bridge.py: same command line, same status-file JSON, no Bluetooth or controller.

Lets padbridged and the GUI run anywhere (including macOS) without touching hardware. It prints log lines
in the engine's format and rewrites the status file every 0.5 s like the real engine.

Scenario (env MOCK_SCENARIO):
    normal        controller plugged in; connects to the saved Mac, or "pairs" one after MOCK_PAIR_AFTER s
    nocontroller  no controller ever appears
    nomac         controller present, the Mac never connects
    bindfail      exits at once with the engine's "cannot bind HID channels" error
    crash         runs for a few seconds, then dies with a traceback
    hang          writes one status, then stops updating it (looks unresponsive)
MOCK_REPLAY=<status.json> replays a captured real engine status instead.
The saved Mac lives in ds4_bt_host.txt in $PADBRIDGE_ENGINE_DIR (or next to this file), like the engine.
"""
import argparse
import collections
import json
import math
import os
import sys
import time

MAC = os.environ.get("MOCK_MAC", "AA:BB:CC:DD:EE:01")
ADAPTER = "AA:BB:CC:DD:EE:02"
# Contract Addendum A: identities, the physical controller, and the Bluetooth name the Mac sees.
IDENTITIES = [                       # xbox-series is paused in the real engine and not listed
    {"id": "xbox-one", "label": "Xbox Wireless Controller (Xbox One S)", "transport": "classic",
     "vid": "045e", "pid": "02fd"},
    {"id": "ds4", "label": "DualShock 4", "transport": "classic", "vid": "054c", "pid": "09cc"},
]
NAMES = {"xbox-one": "Xbox Wireless Controller", "ds4": "Wireless Controller"}
PAD = {"name": "8BitDo Ultimate C Wired Controller for Xbox", "vendor": "8BitDo", "vid": "2dc8", "pid": "2027",
       "bus": "usb", "phys": "usb-0000:00:14.0-2/input0", "uniq": "", "usb_product": "8BitDo Ultimate C Wired Controller for Xbox",
       "evdev_name": "Generic X-Box pad", "short": "8BitDo Ultimate C"}
LOG = collections.deque(maxlen=200)


def log(msg):
    line = time.strftime("%H:%M:%S ") + msg
    LOG.append(line)
    print(line, flush=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--adapter", default="")
    p.add_argument("--status-file")
    p.add_argument("--mode", choices=["xbox-series", "xbox-one", "ds4", "xbox"], default="xbox-one")
    p.add_argument("--name")
    p.add_argument("--inflight", type=int, default=2)
    p.add_argument("--master", action="store_true")
    p.add_argument("--rate", type=int, default=250)
    p.add_argument("--no-grab", action="store_true")
    p.add_argument("--forget", action="store_true")
    args = p.parse_args()
    if args.mode == "xbox-series":
        sys.exit("--mode xbox-series: xbox-series (Xbox Series X|S over Bluetooth LE) is paused; use xbox-one or ds4.")
    args.mode = {"xbox": "xbox-one"}.get(args.mode, args.mode)
    legacy = os.environ.get("MOCK_LEGACY") == "1"     # behave like today's engine: no Addendum A keys

    scenario = os.environ.get("MOCK_SCENARIO", "normal")
    pair_after = float(os.environ.get("MOCK_PAIR_AFTER", "6"))
    engine_dir = os.environ.get("PADBRIDGE_ENGINE_DIR") or os.path.dirname(os.path.abspath(__file__))
    host_file = os.path.join(engine_dir, "ds4_bt_host.txt")
    keys_file = os.path.join(engine_dir, "ds4_bt_keys.json")
    t0 = time.time()

    replay = os.environ.get("MOCK_REPLAY")     # a real engine status file, replayed as-is (fresh pid/time)
    if replay and args.status_file:
        with open(replay) as f:
            snap = json.load(f)
        for line in snap.get("log", [])[-20:]:
            print(line, flush=True)
        while True:
            snap.update(pid=os.getpid(), updated_at=time.time())
            tmp = args.status_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(snap, f)
            os.replace(tmp, args.status_file)
            time.sleep(0.5)

    log(f"mode {args.mode}: emulating '{NAMES[args.mode]}', HID report descriptor 307 bytes (mock)")
    if scenario == "bindfail":
        log("cannot bind HID channels ([Errno 98] Address already in use). Run ds4_bt_setup.sh first "
            "(bluetoothd must run with --noplugin=input) and use sudo.")
        sys.exit(1)

    status = {
        "version": 1, "pid": os.getpid(), "started_at": t0, "updated_at": t0,
        "mode": "ds4" if args.mode == "ds4" else "xbox", "name": NAMES[args.mode], "adapter": ADAPTER,
        "controller": {"connected": False, "path": None},
        "host": {"address": None, "connected": False, "since": None},
        "link": {"handle": None, "sniff_disabled": False},
        "stats": {"reports_per_s": 0.0, "max_queue_bytes": 0, "deferred": 0, "input_age_ms": None, "pings": 0},
        "rumble": {"strong": 0, "weak": 0},
        "input": {"lx": 0, "ly": 0, "rx": 0, "ry": 0, "lt": 0, "rt": 0, "buttons": []},
        "log": [],
    }
    if not legacy:
        status["identity"] = next(i for i in IDENTITIES if i["id"] == args.mode)
        status["identities"] = IDENTITIES
        status["bt_name"] = None
    log(f"adapter /org/bluez/hci0 {ADAPTER} class 0x002508")
    log("pairing agent registered as default (NoInputNoOutput)")
    log(f"HID service record published ({args.mode}); the laptop is discoverable as '{NAMES[args.mode]}'")

    def write():
        if not args.status_file:
            return
        status["updated_at"] = time.time()
        status["log"] = list(LOG)[-50:]
        tmp = args.status_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(status, f)
        os.replace(tmp, args.status_file)

    buttons_cycle = ["A", "B", "X", "Y", "LB", "RB", "Up", "Right", "Down", "Left", "Menu", "View", "LS", "RS"]
    paired_logged = False
    while True:
        now = time.time()
        el = now - t0
        if scenario == "crash" and el > 3:
            print("Traceback (most recent call last):", flush=True)
            print('  File "ds4_bt_bridge.py", line 754, in _worker', flush=True)
            print("OSError: [Errno 104] Connection reset by peer", flush=True)
            log("bridge crashed; please send me the error above")
            sys.exit(1)
        if scenario != "nocontroller" and el > 0.5 and not status["controller"]["connected"]:
            status["controller"] = {"connected": True, "path": "/dev/input/event19"}
            if not legacy:
                status["controller"].update({k: v for k, v in PAD.items() if k != "short"})
                status["bt_name"] = args.name or PAD["short"]
            log("controller: reading /dev/input/event19")
            log(f"waiting for the Mac: System Settings > Bluetooth > '{NAMES[args.mode]}' > Connect")
        saved = open(host_file).read().strip() if os.path.exists(host_file) else None
        status["host"]["address"] = saved or status["host"]["address"]
        if scenario not in ("nomac",) and not status["host"]["connected"]:
            if not saved and el > pair_after:
                with open(host_file, "w") as f:
                    f.write(MAC)
                with open(keys_file, "w") as f:
                    json.dump({MAC: {"key": "00" * 16, "type": 4}}, f)
                saved = MAC
                if not paired_logged:
                    log(f"pairing: authorized /org/bluez/hci0/dev_{MAC.replace(':', '_')} (Just Works)")
                    log(f"paired with {MAC}; connecting to it")
                    paired_logged = True
            if saved and el > 2:
                status["host"] = {"address": saved, "connected": True, "since": now}
                status["link"] = {"handle": 3, "sniff_disabled": True}
                log(f"Mac {saved} connected")
                log("link 0x0003: sniff mode disabled, staying active")
                log(f"connected: streaming {NAMES[args.mode]} reports")
        if status["host"]["connected"] and status["controller"]["connected"]:
            t = el
            status["stats"] = {
                "reports_per_s": round(args.rate * (0.92 + 0.04 * math.sin(t / 3)), 1),
                "max_queue_bytes": int(480 + 480 * abs(math.sin(t / 5))),
                "deferred": int(30 + 15 * abs(math.sin(t / 7))),
                "input_age_ms": round(1.1 + 0.3 * abs(math.sin(t / 4)), 2),
                "input_age_max_ms": round(3.0 + 2 * abs(math.sin(t / 3)), 2),
                "link_ms": round(7.6 + 0.8 * abs(math.sin(t / 5)), 2),
                "link_p99_ms": round(24 + 10 * abs(math.sin(t / 9)), 1),
                "max_inflight": min(2, args.inflight) if args.inflight else 6,
                "pings": int(t * 2),
            }
            status["input"] = {
                "lx": int(26000 * math.cos(t * 1.3)), "ly": int(26000 * math.sin(t * 1.3)),
                "rx": int(14000 * math.sin(t * 0.7)), "ry": int(-9000 * math.cos(t * 0.9)),
                "lt": int(65535 * max(0.0, math.sin(t * 0.8))), "rt": int(65535 * max(0.0, math.cos(t * 0.6)) ** 2),
                "buttons": [buttons_cycle[int(t) % len(buttons_cycle)]] + (["RB"] if int(t) % 5 == 0 else []),
            }
        elif status["host"]["connected"]:
            status["stats"] = {**status["stats"], "reports_per_s": float(args.rate), "input_age_ms": None}
        if scenario == "hang" and el > 1.5:
            time.sleep(0.5)
            continue
        write()
        time.sleep(0.25)


if __name__ == "__main__":
    main()
