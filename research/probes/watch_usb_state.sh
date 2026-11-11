#!/bin/zsh
# Passive: prints the controller's config and power state from the IORegistry every 100 ms. Touches nothing.
last=""
while true; do
  s=$(ioreg -p IOService -c IOUSBHostDevice -w0 -l -r 2>/dev/null | awk '/"idProduct" = 8231/{f=1} f&&/kUSBCurrentConfiguration|"CurrentPowerState"|IOPowerManagement/{print} /^ *\}$/{f=0}' | tr -s ' ' | tr '\n' ' ')
  present=$(ioreg -p IOUSB -w0 | grep -c "Ultimate C")
  cur="present=$present $s"
  [[ "$cur" != "$last" ]] && { print "$(date +%T.%3N 2>/dev/null || date +%T) $cur"; last=$cur; }
  sleep 0.1
done
