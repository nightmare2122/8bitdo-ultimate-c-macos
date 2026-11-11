#!/bin/bash
# ctl.sh start|stop|status|log|trace|sniff [secs]|sniffed  -- helper so remote commands never contain the bridge's own name
D=/home/USER/ds4-bt-bridge
B=ds4_bt_bridge.py
stop() { sudo -n pkill -f "python3 $D/$B"; sudo -n pkill -x btmon; sleep 1; }
case "$1" in
  start)
    stop
    rm -f $D/btmon.log $D/bridge.log
    nohup sudo -n btmon -w $D/btmon.log >/dev/null 2>&1 &
    nohup sudo -n python3 $D/$B -v "${@:2}" > $D/bridge.log 2>&1 &
    sleep 3; cat $D/bridge.log ;;
  stop) stop; echo stopped ;;
  status) pgrep -af "btmon|$B" | grep -v -e pgrep -e ctl.sh ;;
  log) cat $D/bridge.log ;;
  sniff) nohup sudo -n python3 $D/$B --usb-sniff "${2:-40}" > /tmp/sniff.log 2>&1 & sleep 1; echo "sniffing ${2:-40}s -> /tmp/sniff.log" ;;
  sniffed) cat /tmp/sniff.log ;;
  trace) btmon -r $D/btmon.log 2>/dev/null | tail -n "${2:-200}" ;;
esac
