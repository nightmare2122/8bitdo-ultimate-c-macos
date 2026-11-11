"""Paths and persistent settings, shared by the service, the GUI and the CLI.

Every path can be moved with an environment variable so the service can run unprivileged in a temp dir
for development and tests:

    PADBRIDGE_RUN_DIR      default /run/padbridge         (control.sock, status.json)
    PADBRIDGE_CONFIG_DIR   default /etc/padbridge         (config.json)
    PADBRIDGE_ENGINE_DIR   default /opt/padbridge/engine  (vendored engine, ds4_bt_host.txt)
    PADBRIDGE_ENGINE       default $PADBRIDGE_ENGINE_DIR/ds4_bt_bridge.py
    PADBRIDGE_SOCKET       default $PADBRIDGE_RUN_DIR/control.sock   (client side only)
"""
import json
import os
from dataclasses import dataclass

DEFAULT_RUN_DIR = "/run/padbridge"
DEFAULT_CONFIG_DIR = "/etc/padbridge"
DEFAULT_ENGINE_DIR = "/opt/padbridge/engine"
INSTALL_DIR = "/opt/padbridge"
UNIT_PATH = "/etc/systemd/system/padbridged.service"

# What the Mac sees (contract Addendum A). The engine's status file sends its own list ("identities"); this
# table is the fallback and adds the GUI copy. Xbox Series X|S (Bluetooth LE) is paused and not offered.
IDENTITY_TABLE = [
    {"id": "xbox-one", "label": "Xbox Wireless Controller (Xbox One S)", "short": "Xbox One S",
     "transport": "classic", "vid": "045e", "pid": "02fd", "bt_default": "Xbox Wireless Controller",
     "note": "Recommended. Works with most Mac games, including rumble."},
    {"id": "ds4", "label": "DualShock 4", "short": "DualShock 4",
     "transport": "classic", "vid": "054c", "pid": "09cc", "bt_default": "Wireless Controller",
     "note": "For games that only support PlayStation controllers."},
]
IDENTITY_INFO = {i["id"]: i for i in IDENTITY_TABLE}
IDENTITIES = tuple(IDENTITY_INFO)
LEGACY_IDS = {"xbox": "xbox-one"}          # ids used before Addendum A
IDENTITY_NAMES = {i["id"]: i["label"] for i in IDENTITY_TABLE}
# Fallback for the name the Mac lists in Bluetooth settings when the engine doesn't report bt_name.
BLUETOOTH_NAMES = {i["id"]: i["bt_default"] for i in IDENTITY_TABLE}


def normalize_identity(value):
    return LEGACY_IDS.get(value, value)


RATE_MIN, RATE_MAX = 60, 1000
INFLIGHT_MAX = 8
NAME_MAX = 29            # the engine's limit for the Bluetooth name

DEFAULTS = {
    "identity": "xbox-one",  # which controller the Mac sees; changed only through apply_identity
    "rate": 250,             # engine keepalive reports per second
    "lid_closed": False,     # keep bridging with the lid closed (logind inhibitor)
    "bridging": True,        # False after the user pressed "Stop bridging"; survives reboots
    "grab": True,            # grab the controller so the Linux desktop does not also react to it
    "inflight": 2,           # engine --inflight: most reports inside the Bluetooth radio at once (0 = off)
    "name": None,            # engine --name: Bluetooth name for the Mac (None: the controller's short name)
    "desktop_user": None,    # user whose WirePlumber config ds4_bt_setup.sh adjusts (set by install.sh)
}


@dataclass
class Paths:
    run_dir: str
    config_dir: str
    engine_dir: str
    engine: str

    @property
    def socket(self):
        return os.path.join(self.run_dir, "control.sock")

    @property
    def status_file(self):
        return os.path.join(self.run_dir, "status.json")

    @property
    def config_file(self):
        return os.path.join(self.config_dir, "config.json")

    @property
    def host_file(self):
        # The engine keeps the paired Mac's address next to itself (engine dir); we never move it.
        # The mock engine honours $PADBRIDGE_ENGINE_DIR, which the service sets for its child.
        return os.path.join(self.engine_dir, "ds4_bt_host.txt")

    @property
    def keys_file(self):
        # Link keys the engine saved at pairing (root 0600); it restores lost pairings from it.
        return os.path.join(self.engine_dir, "ds4_bt_keys.json")

    @property
    def setup_script(self):
        return os.path.join(self.engine_dir, "ds4_bt_setup.sh")

    @classmethod
    def from_env(cls, run_dir=None, config_dir=None, engine_dir=None, engine=None):
        env = os.environ.get
        run_dir = run_dir or env("PADBRIDGE_RUN_DIR") or DEFAULT_RUN_DIR
        config_dir = config_dir or env("PADBRIDGE_CONFIG_DIR") or DEFAULT_CONFIG_DIR
        engine_dir = engine_dir or env("PADBRIDGE_ENGINE_DIR") or DEFAULT_ENGINE_DIR
        engine = engine or env("PADBRIDGE_ENGINE") or os.path.join(engine_dir, "ds4_bt_bridge.py")
        return cls(os.path.abspath(run_dir), os.path.abspath(config_dir), os.path.abspath(engine_dir),
                   os.path.abspath(engine))


def socket_path():
    """Where clients connect."""
    env = os.environ.get
    if env("PADBRIDGE_SOCKET"):
        return env("PADBRIDGE_SOCKET")
    return os.path.join(env("PADBRIDGE_RUN_DIR") or DEFAULT_RUN_DIR, "control.sock")


class SettingsError(ValueError):
    pass


def validate(changes, allow_identity=False):
    """Return a cleaned copy of `changes` or raise SettingsError with a message fit for the GUI."""
    out = {}
    for key, value in changes.items():
        if key == "identity":
            if not allow_identity:
                raise SettingsError("Change the identity with apply_identity; it reconfigures Bluetooth.")
            value = normalize_identity(value)
            if value == "xbox-series":
                raise SettingsError("Xbox Series X|S is paused for now. Use xbox-one or ds4.")
            if value not in IDENTITIES:
                raise SettingsError(f"Unknown identity {value!r}. Use one of: {', '.join(IDENTITIES)}.")
        elif key == "rate":
            if isinstance(value, bool) or not isinstance(value, int) or not RATE_MIN <= value <= RATE_MAX:
                raise SettingsError(f"Report rate must be a whole number from {RATE_MIN} to {RATE_MAX}.")
        elif key == "inflight":
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= INFLIGHT_MAX:
                raise SettingsError(f"inflight must be a whole number from 0 to {INFLIGHT_MAX}.")
        elif key == "name":
            if value is not None:
                if not isinstance(value, str) or not value.strip() or len(value.strip()) > NAME_MAX:
                    raise SettingsError(f"The Bluetooth name must be 1 to {NAME_MAX} characters.")
                value = value.strip()
        elif key in ("lid_closed", "bridging", "grab"):
            if not isinstance(value, bool):
                raise SettingsError(f"{key} must be true or false.")
        elif key == "desktop_user":
            if value is not None and not isinstance(value, str):
                raise SettingsError("desktop_user must be a user name.")
        else:
            raise SettingsError(f"Unknown setting {key!r}.")
        out[key] = value
    return out


def load(path):
    """Settings from `path`, falling back to defaults for anything missing or invalid."""
    settings = dict(DEFAULTS)
    try:
        with open(path) as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return settings
    if isinstance(raw, dict):
        for key, value in raw.items():
            try:
                settings.update(validate({key: value}, allow_identity=True))
            except SettingsError:
                pass
    return settings


def save(path, settings):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(settings, f, indent=2, sort_keys=True)
        f.write("\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
