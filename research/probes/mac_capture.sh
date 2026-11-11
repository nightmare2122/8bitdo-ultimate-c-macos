#!/bin/zsh
# Capture the raw USB traffic of the 8BitDo handshake on macOS, for comparison with the Linux usbmon pcap.
# Run as root:  sudo ./mac_capture.sh
# It brings up macOS's USB capture interfaces (XHCn), starts tcpdump on each, runs probe3 (which waits for
# you to unplug and replug the controller), then stops the capture and brings the interfaces back down.
set -u
cd "${0:A:h}"
[[ $EUID -eq 0 ]] || { print "run with sudo"; exit 1; }

typeset -a ifs pids
for i in 0 1 2 3 4 5 20; do
  if ifconfig XHC$i up 2>/dev/null; then ifs+=XHC$i; fi
done
print "capture interfaces: ${ifs:-none}"
(( ${#ifs} )) || { print "macOS refused to create any XHC capture interface"; exit 1; }

for x in $ifs; do
  tcpdump -i $x -w mac_$x.pcap -s 0 >/dev/null 2>mac_$x.tcpdump.log &
  pids+=$!
done
sleep 1
./probe3 --seconds 6
sleep 1
kill $pids 2>/dev/null; wait 2>/dev/null
for x in $ifs; do ifconfig $x down 2>/dev/null; done
chown "${SUDO_USER:-$USER}" mac_*.pcap mac_*.tcpdump.log 2>/dev/null
ls -la mac_*.pcap
