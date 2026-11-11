"""Everything the service does to the host system, behind one interface so tests can swap in a fake.

RealSystem runs the commands for real (the service runs as root). FakeSystem only records what it would
have done, for development on machines without BlueZ/systemd and for the unit tests.
"""
import asyncio
import glob
import os
import re

from . import SERVICE_UNIT

MAIN_CONF = "/etc/bluetooth/main.conf"
BT_DROPIN = "/etc/systemd/system/bluetooth.service.d/ds4-bridge.conf"
MAC_RE = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$", re.I)


async def run(*argv, env=None, timeout=60):
    """Run a command, return (exit code, combined output). Never raises for a failing command."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env)
    except OSError as e:
        return 127, str(e)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, f"{argv[0]} timed out after {timeout} s"
    return proc.returncode, out.decode(errors="replace")


def identity_from_main_conf(path=MAIN_CONF):
    """'xbox-one' or 'ds4' as applied by ds4_bt_setup.sh, or None if the setup has not been applied.
    The setup writes a marker "#PadBridgeIdentity = <id>"; older setups only left DeviceID."""
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    for line in lines:
        if line.startswith("#PadBridgeIdentity") and "=" in line:
            value = line.split("=", 1)[1].strip()
            return {"xbox": "xbox-one"}.get(value, value) or None
    for line in lines:
        s = line.strip().upper()
        if s.startswith("DEVICEID") and "=" in s:
            if "054C" in s:
                return "ds4"
            if "0B13" in s:
                return "xbox-series"
            return "xbox-one" if "045E" in s else None
    return None


def usb_device_dir(sysfs_path):
    """Walk up from a sysfs path to the USB device directory (the one with idVendor and power/control)."""
    try:
        d = os.path.realpath(sysfs_path)
    except OSError:
        return None
    while d and d != "/" and d.startswith("/sys/"):
        if os.path.exists(os.path.join(d, "idVendor")) and os.path.exists(os.path.join(d, "power", "control")):
            return d
        d = os.path.dirname(d)
    return None


class RealSystem:
    fake = False

    async def run_setup(self, script, identity, desktop_user):
        env = dict(os.environ)
        env.pop("SUDO_USER", None)
        if desktop_user:
            env["SUDO_USER"] = desktop_user   # ds4_bt_setup.sh adjusts this user's WirePlumber config
        arg = "--undo" if identity == "undo" else identity
        return await run("/usr/bin/bash", script, arg, env=env, timeout=120)

    def setup_state(self):
        """(identity applied in main.conf or None, bluetoothd drop-in present)."""
        return identity_from_main_conf(), os.path.exists(BT_DROPIN)

    async def bt_remove(self, address):
        if not MAC_RE.match(address or ""):
            return 1, f"not a Bluetooth address: {address!r}"
        return await run("bluetoothctl", "remove", address, timeout=15)

    async def bt_alias(self, address):
        """The name BlueZ knows for this device (usually the Mac's name), or None."""
        if not MAC_RE.match(address or ""):
            return None
        code, out = await run("bluetoothctl", "info", address, timeout=5)
        if code != 0:
            return None
        for key in ("Alias:", "Name:"):
            for line in out.splitlines():
                s = line.strip()
                if s.startswith(key):
                    value = s[len(key):].strip()
                    if value and not MAC_RE.match(value.replace("-", ":")):
                        return value
        return None

    async def boot_enabled(self):
        code, out = await run("systemctl", "is-enabled", SERVICE_UNIT, timeout=10)
        state = out.strip().splitlines()[-1] if out.strip() else ""
        if state in ("enabled", "enabled-runtime", "alias", "static"):
            return True
        if state in ("disabled", "masked", "masked-runtime", "indirect", "linked", "linked-runtime"):
            return False
        return None   # unit not installed (dev mode) or systemd unavailable

    async def set_boot(self, enabled):
        return await run("systemctl", "enable" if enabled else "disable", SERVICE_UNIT, timeout=30)

    # ---- USB autosuspend: keep the Bluetooth radio and the controller awake while bridging
    def usb_dirs(self, controller_path):
        dirs = []
        for hci in sorted(glob.glob("/sys/class/bluetooth/hci*")):
            d = usb_device_dir(os.path.join(hci, "device"))
            if d:
                dirs.append(d)
        if controller_path:
            node = os.path.basename(controller_path)
            d = usb_device_dir(f"/sys/class/input/{node}/device")
            if d:
                dirs.append(d)
        return dirs

    def read_power(self, usb_dir):
        try:
            with open(os.path.join(usb_dir, "power", "control")) as f:
                return f.read().strip()
        except OSError:
            return None

    def write_power(self, usb_dir, value):
        try:
            with open(os.path.join(usb_dir, "power", "control"), "w") as f:
                f.write(value)
            return True
        except OSError:
            return False


class FakeSystem:
    """Records commands instead of running them. Setup takes a moment, like the real one."""
    fake = True

    def __init__(self, setup_delay=1.0, boot=False, identity="xbox-one"):
        self.calls = []
        self.setup_delay = setup_delay
        self.boot = boot
        self.identity = identity
        self.dropin = identity is not None
        self.power = {}
        self.fail_setup = False

    async def run_setup(self, script, identity, desktop_user):
        self.calls.append(("setup", identity, desktop_user))
        await asyncio.sleep(self.setup_delay)
        if self.fail_setup:
            return 1, "bluetoothd not found. Is bluez installed?"
        if identity == "undo":
            self.identity, self.dropin = None, False
        else:
            self.identity, self.dropin = identity, True
        return 0, f"Applied as: {identity}"

    def setup_state(self):
        return self.identity, self.dropin

    async def bt_remove(self, address):
        self.calls.append(("bt_remove", address))
        return 0, f"Device {address} removed"

    async def bt_alias(self, address):
        self.calls.append(("bt_alias", address))
        return "MacBook Pro" if address else None

    async def boot_enabled(self):
        return self.boot

    async def set_boot(self, enabled):
        self.calls.append(("set_boot", enabled))
        self.boot = enabled
        return 0, ""

    def usb_dirs(self, controller_path):
        return ["/fake/usb/bluetooth"] + (["/fake/usb/controller"] if controller_path else [])

    def read_power(self, usb_dir):
        return self.power.get(usb_dir, "auto")

    def write_power(self, usb_dir, value):
        self.calls.append(("power", usb_dir, value))
        self.power[usb_dir] = value
        return True

