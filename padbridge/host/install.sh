#!/bin/bash
# Install PadBridge Host on Fedora (GNOME). Run from the PadBridge folder:
#
#   sudo ./install.sh [--user NAME] [--identity xbox-one|ds4] [--setup|--no-setup] [--force]
#                     [--migrate-from DIR]
#
#   --user NAME      desktop user who gets access to PadBridge (default: the user who ran sudo)
#   --identity ID    what the Mac sees (default: keep the current one, else xbox-one)
#   --setup          always run the Bluetooth identity setup (default: only if not already applied as ID)
#   --no-setup       never run it now (the app offers it later)
#   --force          stop an engine that was started by hand (ctl.sh, sudo python3 ds4_bt_bridge.py)
#   --migrate-from DIR  take the paired Mac (ds4_bt_host.txt) and saved link keys (ds4_bt_keys.json) from a
#                    hand-run engine folder (default: ~USER/ds4-bt-bridge), so the Mac doesn't need to pair again
#
# What it does (uninstall.sh reverses all of it):
#   /opt/padbridge/{padbridge,bin,data,engine}   the app, the service and a vendored copy of the engine
#   /etc/padbridge/config.json                   settings (kept on reinstall)
#   /etc/systemd/system/padbridged.service       the root service, enabled and started
#   group "padbridge", with the desktop user in it (log out and in once afterwards)
#   /usr/share/applications + hicolor icons, /usr/local/bin/padbridge-host and padbridgectl
#   runs the engine's ds4_bt_setup.sh for the chosen identity if needed (Bluetooth restarts)
# Upgrades keep /opt/padbridge/engine/ds4_bt_host.txt and ds4_bt_keys.json (the pairing) and the settings.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PREFIX=/opt/padbridge
CONF_DIR=/etc/padbridge
UNIT=/etc/systemd/system/padbridged.service
GROUP=padbridge

USER_NAME=${SUDO_USER:-}
IDENTITY=""
SETUP=auto
FORCE=0
MIGRATE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --user) USER_NAME=${2:?--user needs a name}; shift 2 ;;
    --identity) IDENTITY=${2:?--identity needs xbox-one or ds4}; shift 2 ;;
    --setup) SETUP=1; shift ;;
    --no-setup) SETUP=0; shift ;;
    --migrate-from) MIGRATE=${2:?--migrate-from needs a folder}; shift 2 ;;
    --force) FORCE=1; shift ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1 (see --help)"; exit 1 ;;
  esac
done

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run with sudo: sudo $0"
[[ -n "$USER_NAME" && "$USER_NAME" != root ]] || die "Run with sudo from your own account, or pass --user NAME."
id "$USER_NAME" >/dev/null 2>&1 || die "No such user: $USER_NAME"
case "$IDENTITY" in
  ""|xbox-one|ds4) ;;
  xbox) IDENTITY=xbox-one ;;
  xbox-series) die "Xbox Series X|S is paused for now. Use --identity xbox-one or ds4." ;;
  *) die "Unknown identity: $IDENTITY (xbox-one or ds4)" ;;
esac

# ---- the engine to vendor: ./engine in a release tarball, ../../ds4-bt-bridge in the source tree
if [[ -f "$HERE/engine/ds4_bt_bridge.py" ]]; then
  ENGINE_SRC="$HERE/engine"
elif [[ -f "$HERE/../../ds4-bt-bridge/ds4_bt_bridge.py" ]]; then
  ENGINE_SRC=$(cd "$HERE/../../ds4-bt-bridge" && pwd)
else
  die "Engine not found. Expected $HERE/engine/ or ../../ds4-bt-bridge/ next to this folder."
fi
for f in ds4_bt_bridge.py bridge_linux.py ds4_bt_setup.sh; do
  [[ -f "$ENGINE_SRC/$f" ]] || die "Engine file missing: $ENGINE_SRC/$f"
done

# ---- stop what we are replacing; refuse to fight a hand-started engine
if systemctl is-active --quiet padbridged.service 2>/dev/null; then
  say "Stopping the running PadBridge service"
  systemctl stop padbridged.service
fi
if pgrep -f "ds4_bt_bridge.py" >/dev/null; then
  if (( FORCE )); then
    say "Stopping an engine that was started by hand"
    pkill -f "ds4_bt_bridge.py" || true
    sleep 1
  else
    die "An engine is already running (started by hand, e.g. ctl.sh). Stop it first (~/ds4-bt-bridge/ctl.sh stop) or rerun with --force."
  fi
fi

# ---- dependencies
if command -v rpm >/dev/null; then
  missing=()
  for pkg in python3 python3-dbus python3-gobject gtk4 libadwaita bluez; do
    rpm -q "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
  done
  if (( ${#missing[@]} )); then
    say "Installing: ${missing[*]}"
    dnf install -y "${missing[@]}"
  fi
fi

# ---- files
say "Installing to $PREFIX"
install -d -m 755 "$PREFIX" "$PREFIX/bin" "$PREFIX/data" "$PREFIX/engine" "$PREFIX/padbridge"
rm -rf "$PREFIX/padbridge"
cp -r "$HERE/padbridge" "$PREFIX/padbridge"
find "$PREFIX/padbridge" -name __pycache__ -prune -exec rm -rf {} +
install -m 755 "$HERE/bin/padbridged" "$HERE/bin/padbridge-host" "$HERE/bin/padbridgectl" "$PREFIX/bin/"
install -m 644 "$HERE/data/"* "$PREFIX/data/"
install -m 644 "$HERE/README.md" "$PREFIX/README.md"
install -m 755 "$HERE/uninstall.sh" "$PREFIX/uninstall.sh"
# The engine keeps the paired Mac in engine/ds4_bt_host.txt and link keys in engine/ds4_bt_keys.json; the
# folder is never emptied, so upgrades keep the pairing.
install -m 755 "$ENGINE_SRC/ds4_bt_bridge.py" "$ENGINE_SRC/ds4_bt_setup.sh" "$PREFIX/engine/"
install -m 644 "$ENGINE_SRC/bridge_linux.py" "$PREFIX/engine/"
# Carry over the pairing of a hand-run engine, so the Mac it is paired with keeps working.
MIGRATE=${MIGRATE:-$(getent passwd "$USER_NAME" | cut -d: -f6)/ds4-bt-bridge}
for f in ds4_bt_host.txt ds4_bt_keys.json; do
  if [[ -s "$MIGRATE/$f" && ! -e "$PREFIX/engine/$f" ]]; then
    say "Keeping the existing pairing: $MIGRATE/$f"
    install -m 600 "$MIGRATE/$f" "$PREFIX/engine/$f"
  fi
done
chown -R root:root "$PREFIX"
chmod -R go-w "$PREFIX"
[[ -e "$PREFIX/engine/ds4_bt_keys.json" ]] && chmod 600 "$PREFIX/engine/ds4_bt_keys.json"

# ---- group
getent group "$GROUP" >/dev/null || { say "Creating group $GROUP"; groupadd --system "$GROUP"; }
NEW_MEMBER=0
if ! id -nG "$USER_NAME" | tr ' ' '\n' | grep -qx "$GROUP"; then
  say "Adding $USER_NAME to $GROUP"
  usermod -aG "$GROUP" "$USER_NAME"
  NEW_MEMBER=1
fi

# ---- identity: keep the configured one, else what Bluetooth is already set up as, else xbox-one
applied_identity() {
  local m
  m=$(sed -nE 's/^#PadBridgeIdentity[[:space:]]*=[[:space:]]*([a-z0-9-]+).*/\1/p' /etc/bluetooth/main.conf 2>/dev/null | head -1)
  [[ "$m" == xbox ]] && m=xbox-one
  if [[ -z "$m" ]] && grep -qiE '^\s*DeviceID\s*=.*054C' /etc/bluetooth/main.conf 2>/dev/null; then m=ds4; fi
  if [[ -z "$m" ]] && grep -qiE '^\s*DeviceID\s*=.*045E:02FD' /etc/bluetooth/main.conf 2>/dev/null; then m=xbox-one; fi
  echo "$m"
}
APPLIED=$(applied_identity)
current_conf_identity() {
  PYTHONPATH="$PREFIX" python3 -c 'import sys; from padbridge import config; print(config.load(sys.argv[1])["identity"])' "$CONF_DIR/config.json"
}
if [[ -z "$IDENTITY" ]]; then
  if [[ -f "$CONF_DIR/config.json" ]]; then
    IDENTITY=$(current_conf_identity)
  else
    IDENTITY=${APPLIED:-xbox-one}
  fi
fi

# ---- settings
install -d -m 755 "$CONF_DIR"
PYTHONPATH="$PREFIX" python3 - "$CONF_DIR/config.json" "$USER_NAME" "$IDENTITY" <<'PY'
import sys
from padbridge import config
path, user, identity = sys.argv[1:4]
s = config.load(path)
s["desktop_user"] = user
s["identity"] = config.normalize_identity(identity)
config.save(path, s)
PY

# ---- Bluetooth identity setup (restarts bluetoothd). Skipped when it is already applied as this identity.
if [[ "$SETUP" == auto ]]; then
  if [[ "$APPLIED" == "$IDENTITY" && -f /etc/systemd/system/bluetooth.service.d/ds4-bridge.conf ]]; then
    say "Bluetooth is already set up as $IDENTITY; not restarting it"
    SETUP=0
  else
    SETUP=1
  fi
fi
if (( SETUP )); then
  say "Setting up Bluetooth as $IDENTITY (Bluetooth restarts)"
  SUDO_USER="$USER_NAME" bash "$PREFIX/engine/ds4_bt_setup.sh" "$IDENTITY"
fi

# ---- service
say "Installing the service"
install -m 644 "$HERE/data/padbridged.service" "$UNIT"
command -v restorecon >/dev/null && restorecon -RF "$PREFIX" "$CONF_DIR" "$UNIT" 2>/dev/null || true
systemctl daemon-reload
systemctl enable --now padbridged.service

# ---- desktop integration
say "Adding PadBridge to the app grid"
install -D -m 644 "$HERE/data/io.padbridge.Host.desktop" /usr/share/applications/io.padbridge.Host.desktop
install -D -m 644 "$HERE/data/io.padbridge.Host.svg" /usr/share/icons/hicolor/scalable/apps/io.padbridge.Host.svg
install -D -m 644 "$HERE/data/io.padbridge.Host-symbolic.svg" \
  /usr/share/icons/hicolor/symbolic/apps/io.padbridge.Host-symbolic.svg
ln -sf "$PREFIX/bin/padbridge-host" /usr/local/bin/padbridge-host
ln -sf "$PREFIX/bin/padbridgectl" /usr/local/bin/padbridgectl
gtk-update-icon-cache -q -t /usr/share/icons/hicolor 2>/dev/null || true
update-desktop-database -q /usr/share/applications 2>/dev/null || true

sleep 1
systemctl is-active --quiet padbridged.service || die "The service didn't start. See: journalctl -u padbridged -b"
say "PadBridge is installed and running."
if (( NEW_MEMBER )); then
  echo "    Log out and back in once, then open PadBridge from the app grid."
else
  echo "    Open PadBridge from the app grid, or run: padbridgectl status"
fi
