#!/bin/bash
# Remove PadBridge Host and give the laptop its normal Bluetooth back.
#
#   sudo ./uninstall.sh            (also works as: sudo bash /opt/padbridge/uninstall.sh)
#
# Stops and removes the service (which releases any lid-closed inhibitor), forgets the paired Mac,
# runs ds4_bt_setup.sh --undo (bluetoothd, main.conf and WirePlumber back to normal), and deletes
# /opt/padbridge, /etc/padbridge, the unit, the app entry, the icons, the launchers and the padbridge group.
set -uo pipefail

PREFIX=/opt/padbridge
say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
[[ $EUID -eq 0 ]] || { echo "Run with sudo: sudo $0"; exit 1; }

USER_NAME=""
if [[ -f /etc/padbridge/config.json ]]; then
  USER_NAME=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("desktop_user") or "")' \
              /etc/padbridge/config.json 2>/dev/null || true)
fi
USER_NAME=${USER_NAME:-${SUDO_USER:-}}

say "Stopping the service"
systemctl disable --now padbridged.service 2>/dev/null || true

ADDR=$(cat "$PREFIX/engine/ds4_bt_host.txt" 2>/dev/null || true)
if [[ -n "$ADDR" ]]; then
  say "Forgetting the paired Mac ($ADDR)"
  bluetoothctl remove "$ADDR" >/dev/null 2>&1 || true
fi

if [[ -f "$PREFIX/engine/ds4_bt_setup.sh" ]]; then
  say "Restoring the original Bluetooth setup"
  SUDO_USER="$USER_NAME" bash "$PREFIX/engine/ds4_bt_setup.sh" --undo || echo "    (undo reported a problem; check /etc/bluetooth/main.conf)"
fi

say "Removing files"
rm -f /etc/systemd/system/padbridged.service
systemctl daemon-reload
rm -rf "$PREFIX" /etc/padbridge /run/padbridge
rm -f /usr/share/applications/io.padbridge.Host.desktop \
      /usr/share/icons/hicolor/scalable/apps/io.padbridge.Host.svg \
      /usr/share/icons/hicolor/symbolic/apps/io.padbridge.Host-symbolic.svg \
      /usr/local/bin/padbridge-host /usr/local/bin/padbridgectl
gtk-update-icon-cache -q -t /usr/share/icons/hicolor 2>/dev/null || true
update-desktop-database -q /usr/share/applications 2>/dev/null || true

if getent group padbridge >/dev/null; then
  say "Removing the padbridge group"
  groupdel padbridge || true
fi
say "PadBridge is removed. Bluetooth keyboards, mice and headphones work on this laptop again."
