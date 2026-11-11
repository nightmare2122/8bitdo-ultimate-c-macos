"""padbridgectl: command-line client for the PadBridge service.

    padbridgectl status [--json]       padbridgectl start | stop | restart | forget
    padbridgectl identity xbox-one|ds4 padbridgectl boot on|off
    padbridgectl set rate=250 lid_closed=true grab=false inflight=2 name="My Pad" (name=null: default)
    padbridgectl log [-f]
"""
import argparse
import json
import sys
import time

from .client import Client, ConnectError, ServiceError
from .config import IDENTITY_NAMES

HINTS = {
    "permission": "Permission denied. Log out and back in so your account's new padbridge group membership "
                  "takes effect.",
    "not_running": "The PadBridge service isn't running. Start it with: sudo systemctl start padbridged",
    "not_installed": "PadBridge isn't installed. Run: sudo ./install.sh",
}


def parse_value(text):
    try:
        return json.loads(text)
    except ValueError:
        return text


def fmt_log(entry):
    return time.strftime("%H:%M:%S", time.localtime(entry["time"])) + "  " + entry["text"]


def print_status(st):
    svc, eng, cfg = st["service"], st.get("engine") or {}, st["settings"]
    ctrl = (eng.get("controller") or {})
    host = (eng.get("host") or {})
    stats = (eng.get("stats") or {})
    print(f"Service      {svc['state']}{' (fake system)' if svc.get('fake_system') else ''}")
    if svc.get("hold"):
        print("             waiting: forget the controller on the Mac, then run padbridgectl start")
    print(f"Identity     {IDENTITY_NAMES.get(svc['setup']['identity'] or cfg['identity'])}"
          f"{'' if svc['setup']['applied'] else '  (Bluetooth not set up)'}")
    name = ctrl.get("name") or ""
    ids = f" {ctrl['vid']}:{ctrl['pid']}" if ctrl.get("vid") else ""
    print(f"Controller   {'plugged in: ' + name + ids + ' ' + (ctrl.get('path') or '') if ctrl.get('connected') else 'not found'}")
    if eng.get("bt_name"):
        print(f"Mac sees     {eng['bt_name']}, as {(eng.get('identity') or {}).get('label', '')}")
    paired = svc["paired"]
    mac = (paired.get("name") or "") + (f" ({paired['address']})" if paired.get("address") else "")
    print(f"Mac          {'connected ' if host.get('connected') else 'not connected '}{mac.strip()}")
    if host.get("connected"):
        print(f"Reports      {stats.get('reports_per_s')} /s, input age {stats.get('input_age_ms')} ms, "
              f"Bluetooth delivery {stats.get('link_ms')} ms (p99 {stats.get('link_p99_ms')})")
    print(f"Lid closed   {'keep bridging' if cfg['lid_closed'] else 'suspend'}"
          f"{' (inhibitor held)' if svc['inhibit']['active'] else ''}")
    print(f"Start at boot {svc['start_at_boot']}")
    if svc.get("last_exit") and svc["state"] in ("waiting", "starting"):
        print(f"Last error   {svc['last_exit'].get('message')}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="padbridgectl", description="Control the PadBridge service.")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status")
    s.add_argument("--json", action="store_true")
    for name in ("start", "stop", "restart", "forget"):
        sub.add_parser(name)
    s = sub.add_parser("identity")
    s.add_argument("identity", choices=["xbox-one", "ds4"])
    s = sub.add_parser("boot")
    s.add_argument("state", choices=["on", "off"])
    s = sub.add_parser("set")
    s.add_argument("pairs", nargs="+", metavar="key=value")
    s = sub.add_parser("log")
    s.add_argument("-f", "--follow", action="store_true")
    args = p.parse_args(argv)

    c = Client()
    try:
        if args.cmd == "status":
            st = c.call("status")
            if args.json:
                st.pop("log", None)
                print(json.dumps(st, indent=2))
            else:
                print_status(st)
        elif args.cmd in ("start", "stop", "restart", "forget"):
            print(json.dumps(c.call(args.cmd)))
        elif args.cmd == "identity":
            c.call("apply_identity", identity=args.identity)
            print(f"Switching to {IDENTITY_NAMES[args.identity]}…", flush=True)
            while c.call("status")["service"]["busy"]:
                time.sleep(0.5)
            svc = c.call("status")["service"]
            act = svc.get("last_action") or {}
            print(act.get("message", "Done"))
            if svc.get("hold"):
                print("The bridge stays stopped so the Mac can't reconnect to the old device.\n"
                      "On the Mac: System Settings > Bluetooth > (i) next to the controller > Forget This Device.\n"
                      "Then run: padbridgectl start   and connect the controller again from the Mac.")
            return 0 if act.get("ok", True) else 1
        elif args.cmd == "boot":
            print(json.dumps(c.call("set_boot", enabled=args.state == "on")))
        elif args.cmd == "set":
            changes = {}
            for pair in args.pairs:
                key, _, value = pair.partition("=")
                changes[key] = parse_value(value)
            print(json.dumps(c.call("set_settings", settings=changes)))
        elif args.cmd == "log":
            since = 0
            while True:
                st = c.call("status", log_since=since)
                for e in st["log"]:
                    print(fmt_log(e), flush=True)
                since = st["log_seq"]
                if not args.follow:
                    break
                time.sleep(0.5)
    except ConnectError as e:
        print(HINTS.get(e.kind, str(e)), file=sys.stderr)
        return 2
    except ServiceError as e:
        print(str(e), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0
