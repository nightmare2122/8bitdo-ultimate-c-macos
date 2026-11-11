#!/usr/bin/env bash
# Run on the Linux machine where the 8BitDo Ultimate C (2dc8:2027) works.
# Captures the full USB conversation from plug-in, so the Mac side can replay the handshake.
# Usage: sudo bash collect_linux.sh      (controller UNPLUGGED when you start)
set -u
OUT="ultimate_c_linux_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$OUT"
cd "$OUT" || exit 1

if [ "$(id -u)" -ne 0 ]; then echo "Run with sudo."; exit 1; fi

{
  echo "== kernel"; uname -a
  echo "== modules"; lsmod | grep -Ei 'xpad|xone|gip|hid_generic|usbhid' || echo "(none matched)"
  echo "== modinfo xpad"; modinfo xpad 2>/dev/null | head -5
  echo "== xone (dkms)"; dkms status 2>/dev/null | grep -i xone || echo "(no xone dkms)"
} > system.txt 2>&1

modprobe usbmon || { echo "modprobe usbmon failed"; exit 1; }
mount | grep -q debugfs || mount -t debugfs none /sys/kernel/debug

dmesg -w > dmesg.txt 2>&1 &
DMESG_PID=$!

# usbmon0 = all buses, so we don't need to know the bus number in advance.
if command -v tcpdump >/dev/null; then
  tcpdump -i usbmon0 -s 0 -w capture.pcap >/dev/null 2>tcpdump.err &
  CAP_PID=$!
  echo "Capturing with tcpdump (full packets)."
fi
# Text capture as a fallback / easy-to-read copy (truncates payloads to 32 bytes).
cat /sys/kernel/debug/usb/usbmon/0u > usbmon_text.txt &
TXT_PID=$!

echo
echo ">>> Now PLUG IN the controller. Wait until the light is solid (connected)."
echo ">>> Then, slowly, one at a time: A, B, X, Y, LB, RB, LT, RT, View, Menu, Guide(Xbox),"
echo ">>>   D-pad up/down/left/right, click both sticks, move left stick full circle, right stick full circle."
echo ">>> Press ENTER here when done."
read -r _

kill "$TXT_PID" "$DMESG_PID" 2>/dev/null
[ -n "${CAP_PID:-}" ] && kill "$CAP_PID" 2>/dev/null
sleep 1

{
  echo "== lsusb -v"; lsusb -v -d 2dc8:2027 2>&1
  echo "== bound drivers"
  for d in /sys/bus/usb/devices/*; do
    [ -f "$d/idVendor" ] && [ "$(cat "$d/idVendor")" = "2dc8" ] || continue
    echo "$d"; for i in "$d":*; do echo "  $(basename "$i") -> $(basename "$(readlink -f "$i/driver" 2>/dev/null)" 2>/dev/null)"; done
  done
  echo "== input devices"; grep -A5 -i 8bitdo /proc/bus/input/devices
} > device.txt 2>&1

# Keep only the 8BitDo bus/device lines in the text capture to make it small.
BUSDEV=$(lsusb -d 2dc8:2027 | awk '{printf "%d:%03d", $2, $4}')
BUS=${BUSDEV%%:*}; DEV=${BUSDEV##*:}
[ -n "$BUS" ] && grep -E ":${BUS}:${DEV}:" usbmon_text.txt > usbmon_8bitdo.txt

cd ..
tar czf "$OUT.tar.gz" "$OUT"
chmod a+r "$OUT.tar.gz"
echo "Done: $(pwd)/$OUT.tar.gz  -- copy this file to the Mac project folder."
