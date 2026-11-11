#!/bin/bash
# Prepare (or undo) the Linux laptop's Bluetooth so ds4_bt_bridge.py can pose as a game controller.
#
#   sudo bash ds4_bt_setup.sh [xbox-one]  apply, as an Xbox Wireless Controller (Xbox One S, 045e:02fd), Bluetooth
#                                         Classic - default. "xbox" is the old name for xbox-one.
#   (xbox-series, Xbox Series X|S over Bluetooth LE, is paused: see paused/ds4_bt_setup_le.sh)
#   sudo bash ds4_bt_setup.sh ds4         apply, as a Sony DualShock 4 (054c:09cc), Bluetooth Classic
#   sudo bash ds4_bt_setup.sh --undo      restore the original Bluetooth setup
# After switching identity, remove the controller on the Mac (Bluetooth settings) and pair it again.
#
# What "apply" changes, all reverted by --undo:
#   1. bluetoothd runs with --noplugin=input,hostname,a2dp,avrcp. Without "input" it stops claiming the HID
#      channels (L2CAP PSM 17 and 19), so the bridge can own them; while applied, Bluetooth keyboards and mice
#      on THIS laptop will not work. Without "hostname" it stops forcing the device class to computer/laptop.
#      Without "a2dp"/"avrcp" the laptop no longer offers itself as a Bluetooth speaker.
#   2. WirePlumber's Bluetooth audio is switched off for the desktop user (no A2DP/HFP endpoints), so the Mac
#      sees a game controller and nothing else. Bluetooth headphones on THIS laptop will not work while applied.
#   3. /etc/bluetooth/main.conf [General] gets:
#        Name     = Xbox Wireless Controller     (or "Wireless Controller" for ds4)
#        Class    = 0x002508                     (peripheral / gamepad)
#        DeviceID = usb:045E:02FD:0903           (or usb:054C:09CC:0100 for ds4: the vendor/product ID macOS matches on)
#        JustWorksRepairing = always             (re-pairing after "Forget This Device" on the Mac is not refused)
#      plus a marker line "#PadBridgeIdentity = <id>" the engine reads as its default --mode.
#      Leftovers of the paused LE identity (ControllerMode, [LE] connection intervals) are removed.
#      The original file is saved as main.conf.ds4bak.
set -euo pipefail

CONF=/etc/bluetooth/main.conf
BAK=/etc/bluetooth/main.conf.ds4bak
DROPIN_DIR=/etc/systemd/system/bluetooth.service.d
DROPIN=$DROPIN_DIR/ds4-bridge.conf
USER_NAME=${SUDO_USER:-}
WP_DROPIN=""
if [[ -n "$USER_NAME" ]]; then
  USER_HOME=$(getent passwd "$USER_NAME" | cut -d: -f6)
  WP_DROPIN=$USER_HOME/.config/wireplumber/wireplumber.conf.d/80-ds4-bridge-no-bt-audio.conf
fi
restart_wireplumber() {
  [[ -n "$USER_NAME" ]] || return 0
  runuser -u "$USER_NAME" -- env XDG_RUNTIME_DIR=/run/user/$(id -u "$USER_NAME") \
    systemctl --user restart wireplumber 2>/dev/null || echo "(could not restart WirePlumber; log out and in once)"
}

[[ $EUID -eq 0 ]] || { echo "Run with sudo."; exit 1; }

# Diagnostics for latency work (read-only): Wi-Fi/Bluetooth coexistence state of the combo chip.
if [[ "${1:-}" == "diag" ]]; then
  mountpoint -q /sys/kernel/debug || mount -t debugfs none /sys/kernel/debug
  WIF=$(ls /sys/class/ieee80211/*/device/net 2>/dev/null | head -1)
  echo "== Wi-Fi $WIF"; iw dev "$WIF" link 2>/dev/null | grep -E "freq|signal|bitrate"; iw dev "$WIF" get power_save
  for f in /sys/kernel/debug/ieee80211/phy*/rtw89/btc_info; do [[ -f $f ]] && { echo "== $f"; cat "$f"; }; done
  exit 0
fi
# Latency experiments: Wi-Fi power save (wifi-ps on|off, not persistent), and a timed Wi-Fi outage
# (wifi-off-test SECS: rfkill-block Wi-Fi, wait, unblock; NetworkManager reconnects by itself).
if [[ "${1:-}" == "wifi-ps" ]]; then
  WIF=$(ls /sys/class/ieee80211/*/device/net 2>/dev/null | head -1)
  iw dev "$WIF" set power_save "${2:-off}"; iw dev "$WIF" get power_save; exit 0
fi
if [[ "${1:-}" == "wifi-off-test" ]]; then
  S=${2:-20}; (( S <= 60 )) || S=60
  ( rfkill block wlan; sleep "$S"; rfkill unblock wlan ) >/dev/null 2>&1 &
  disown; exit 0
fi

if [[ "${1:-}" == "--undo" ]]; then
  rm -f "$DROPIN"
  [[ -f "$BAK" ]] && mv -f "$BAK" "$CONF"
  [[ -n "$WP_DROPIN" ]] && rm -f "$WP_DROPIN"
  systemctl daemon-reload
  systemctl restart bluetooth
  restart_wireplumber
  echo "Restored. bluetoothd is back to normal and main.conf is the original."
  exit 0
fi

ID=${1:-xbox-one}
case "$ID" in
  ds4)            NAME="Wireless Controller";      DID="usb:054C:09CC:0100" ;;
  xbox|xbox-one)  ID=xbox-one; NAME="Xbox Wireless Controller"; DID="usb:045E:02FD:0903" ;;
  xbox-series)    echo "xbox-series (Xbox Series X|S over Bluetooth LE) is paused; use xbox-one or ds4."; exit 1 ;;
  *) echo "unknown identity '$ID' (xbox-one | xbox | ds4 | --undo)"; exit 1 ;;
esac

BTD=""
for p in /usr/libexec/bluetooth/bluetoothd /usr/lib/bluetooth/bluetoothd /usr/sbin/bluetoothd; do
  [[ -x "$p" ]] && { BTD=$p; break; }
done
[[ -n "$BTD" ]] || { echo "bluetoothd not found. Is bluez installed?"; exit 1; }

missing=()
python3 -c "import dbus" 2>/dev/null || missing+=(python3-dbus)
python3 -c "from gi.repository import GLib" 2>/dev/null || missing+=(python3-gobject)
if (( ${#missing[@]} )); then
  echo "Installing: ${missing[*]}"
  dnf install -y "${missing[@]}"
fi

mkdir -p "$DROPIN_DIR"
cat > "$DROPIN" <<EOF
[Service]
ExecStart=
ExecStart=$BTD --noplugin=input,hostname,a2dp,avrcp
EOF

[[ -f "$CONF" ]] || printf '[General]\n' > "$CONF"
[[ -f "$BAK" ]] || cp -p "$CONF" "$BAK"
# Drop any active lines we manage (incl. leftovers of the paused LE identity), then insert ours after [General].
sed -i -E '/^[[:space:]]*(Name|Class|DeviceID|JustWorksRepairing|ControllerMode|MinConnectionInterval|MaxConnectionInterval|ConnectionLatency)[[:space:]]*=/d; /^#PadBridgeIdentity/d' "$CONF"
SETTINGS="#PadBridgeIdentity = $ID\nName = $NAME\nClass = 0x002508\nJustWorksRepairing = always"
SETTINGS="$SETTINGS\nDeviceID = $DID"
if grep -q '^\[General\]' "$CONF"; then
  sed -i "0,/^\[General\]/s//[General]\n$SETTINGS/" "$CONF"
else
  printf "\n[General]\n$SETTINGS\n" >> "$CONF"
fi

if [[ -n "$WP_DROPIN" ]]; then
  runuser -u "$USER_NAME" -- mkdir -p "$(dirname "$WP_DROPIN")"
  cat > "$WP_DROPIN" <<'WPEOF'
# Written by ds4_bt_setup.sh: no Bluetooth audio while the laptop poses as a game controller. Removed by --undo.
wireplumber.profiles = {
  main = {
    monitor.bluez = disabled
  }
}
WPEOF
  chown "$USER_NAME": "$WP_DROPIN"
fi

systemctl daemon-reload
systemctl restart bluetooth
restart_wireplumber
sleep 1
echo "Applied as: $ID, $NAME ($DID)"
echo "bluetoothd: $(ps -o args= -C bluetoothd | head -1)"
echo "Now run:  sudo python3 ds4_bt_bridge.py"
