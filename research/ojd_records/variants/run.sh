#!/bin/zsh
# For each variant: install it, wait for the 8BitDo to be plugged in, capture 15 s, report, wait for unplug.
OJD=/Volumes/OpenJoystickDriver/OpenJoystickDriver.app/Contents/MacOS/OpenJoystickDriver
ojd(){ (exec -a ojd "$OJD" "$@"); }
LOG=~/Library/Logs/OpenJoystickDriver/OpenJoystickDriver.out.log
present(){ ioreg -p IOUSB -w0 | grep -q "8BitDo Ultimate C Wired"; }
cd "${0:A:h}"
for v in v1-setconfig v2-sinit v3-settle; do
  ojd record install --force $v.json </dev/null 2>&1 | grep -v "raw USB" | head -1
  print "$(date +%T) ##### $v installed. Waiting for unplug, then plug-in..."
  while present; do sleep 0.5; done
  while ! present; do sleep 0.3; done
  n=$(wc -l < $LOG)
  print "$(date +%T) plugged in"
  sleep 3
  ojd controller capture 2DC8:2027 --duration 15 --json </dev/null > $v.capture 2>&1
  print "$(date +%T) Rx packets: $(grep -c '"Rx"' $v.capture)  Tx: $(grep -c '"Tx"' $v.capture)"
  grep '"Rx"' $v.capture | head -3
  tail -n +$((n+1)) $LOG | grep -v -E "2563|0119" | sed 's/LogicalControllerID([^)]*)/8BitDo/' | tail -8
done
print "$(date +%T) ALL DONE"
