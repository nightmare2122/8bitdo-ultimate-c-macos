#!/usr/bin/env bash
# Captures how Linux brings up the 8BitDo Ultimate C Wired (2dc8:2027).
# Needs no extra packages: it reads the kernel's usbmon text interface.
#
# Usage:  sudo bash linux-capture.sh
# Output: ~/8bitdo-capture.tar.gz  (send this back)

set -u
VID=2dc8
PID=2027
OUT="$HOME/8bitdo-capture"
[ -n "${SUDO_USER:-}" ] && OUT="$(getent passwd "$SUDO_USER" | cut -d: -f6)/8bitdo-capture"

if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo:  sudo bash $0"
    exit 1
fi

rm -rf "$OUT"; mkdir -p "$OUT"
log() { echo "[*] $*"; }

# --- usbmon setup -----------------------------------------------------------
modprobe usbmon 2>/dev/null
mountpoint -q /sys/kernel/debug || mount -t debugfs none /sys/kernel/debug
if [ ! -r /sys/kernel/debug/usb/usbmon/0u ]; then
    echo "usbmon text interface not available at /sys/kernel/debug/usb/usbmon/0u"
    exit 1
fi

# --- state while the controller is working -----------------------------------
log "Plug the controller in and make sure it works, then press Enter."
read -r _

if ! lsusb -d "$VID:$PID" >/dev/null 2>&1; then
    echo "Controller $VID:$PID not found with lsusb. Is it plugged in?"
    exit 1
fi

log "Collecting device info..."
{
    echo "### uname";            uname -a
    echo "### lsusb";            lsusb -d "$VID:$PID"
    echo "### lsusb -v";         lsusb -d "$VID:$PID" -v 2>&1
    echo "### lsmod (xpad/xone)"; lsmod | grep -E 'xpad|xone|xbox|joydev|hid' || echo "(none)"
    echo "### modinfo xpad";     modinfo xpad 2>&1 | head -5
    echo "### modinfo xone";     modinfo xone_gip 2>&1 | head -5
    echo "### dmesg (usb/xpad/xone/2dc8)"
    dmesg | grep -iE 'xpad|xone|xbox|2dc8|input:.*(8bitdo|xbox)' | tail -40
    echo "### interface -> bound driver"
    for d in /sys/bus/usb/devices/*; do
        [ -f "$d/idVendor" ] || continue
        [ "$(cat "$d/idVendor")" = "$VID" ] || continue
        echo "$d  product=$(cat "$d/product" 2>/dev/null)"
        for i in "$d":*; do
            [ -e "$i" ] || continue
            drv=$(basename "$(readlink -f "$i/driver" 2>/dev/null)")
            echo "  $(basename "$i")  class=$(cat "$i/bInterfaceClass") sub=$(cat "$i/bInterfaceSubClass") proto=$(cat "$i/bInterfaceProtocol")  driver=$drv"
        done
    done
} > "$OUT/info.txt" 2>&1

# --- capture the cold-start handshake ----------------------------------------
log "Now UNPLUG the controller. Press Enter once it is unplugged."
read -r _

log "Capture starting (all USB buses). PLUG THE CONTROLLER BACK IN NOW,"
log "into the same port. Wait for it to connect, press a few buttons,"
log "then come back here and press Enter to stop."
cat /sys/kernel/debug/usb/usbmon/0u > "$OUT/usbmon-all.txt" &
CAP=$!
sleep 0.5
read -r _
sleep 1
kill "$CAP" 2>/dev/null
wait "$CAP" 2>/dev/null

# --- isolate the controller's traffic ----------------------------------------
LINE=$(lsusb -d "$VID:$PID" | head -1)
BUS=$(echo "$LINE" | sed -E 's/^Bus 0*([0-9]+) Device ([0-9]+).*/\1/')
DEV=$(echo "$LINE" | sed -E 's/^Bus 0*([0-9]+) Device ([0-9]+).*/\2/')
DEV3=$(printf '%03d' "$((10#$DEV))")
echo "bus=$BUS device=$DEV3" >> "$OUT/info.txt"

grep -E ":${BUS}:${DEV3}:" "$OUT/usbmon-all.txt" > "$OUT/usbmon-controller.txt"
log "Captured $(wc -l < "$OUT/usbmon-controller.txt") lines for bus $BUS device $DEV3."

if [ ! -s "$OUT/usbmon-controller.txt" ]; then
    echo "No traffic found for the controller. Did you replug it during capture?"
fi

chown -R "${SUDO_USER:-root}" "$OUT" 2>/dev/null
tar -czf "$OUT.tar.gz" -C "$(dirname "$OUT")" "$(basename "$OUT")"
chown "${SUDO_USER:-root}" "$OUT.tar.gz" 2>/dev/null
log "Done. Send back: $OUT.tar.gz"
log "Quick peek (first lines):"
head -30 "$OUT/usbmon-controller.txt"
