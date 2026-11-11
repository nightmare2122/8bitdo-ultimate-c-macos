#!/usr/bin/env python3
"""Summarise a btmon text trace (btmon -r btmon.log | btstats.py): per-packet controller latency of our
outgoing ACL packets (sent to controller -> Number Of Completed Packets), gaps between completions,
and how many packets were waiting in the controller. Read stdin."""
import re, sys
tx = re.compile(r"^< (?:BR-)?ACL.*#(\d+) \[hci0\] ([\d.]+)")
rx = re.compile(r"^> (?:BR-)?ACL.*#(\d+) \[hci0\] ([\d.]+)")
nocp = re.compile(r"^> HCI Event: Number of Comp.*\[hci0\] ([\d.]+)")
cnt = re.compile(r"^\s+Count: (\d+)")
other = re.compile(r"^[<>@=] (.*?)\s+#\d+ \[hci0\] ([\d.]+)")
pending, lat, gaps, depth, lastc, cur, rxs, evs = [], [], [], [], None, None, 0, {}
for line in sys.stdin:
    m = tx.match(line)
    if m:
        pending.append(float(m.group(2))); depth.append(len(pending)); continue
    m = nocp.match(line)
    if m:
        cur = float(m.group(1))
        if lastc is not None: gaps.append((cur - lastc) * 1000)
        lastc = cur; continue
    m = cnt.match(line)
    if m and cur is not None:
        for _ in range(int(m.group(1))):
            if pending: lat.append((cur - pending.pop(0)) * 1000)
        cur = None; continue
    if rx.match(line): rxs += 1; continue
    m = other.match(line)
    if m:
        k = m.group(1)[:60]; evs[k] = evs.get(k, 0) + 1
def pct(a, p):
    a = sorted(a); return a[min(len(a) - 1, int((len(a) - 1) * p + .5))] if a else float("nan")
def show(n, a):
    print(f"{n}: n={len(a)} p50 {pct(a,.5):.2f} p90 {pct(a,.9):.2f} p99 {pct(a,.99):.2f} max {max(a) if a else 0:.2f} ms")
show("controller latency (tx->completed)", lat)
show("gap between completion events   ", gaps)
print("packets in controller at send (1..): ", {k: depth.count(k) for k in sorted(set(depth))})
print("big completion gaps >15 ms:", sum(g > 15 for g in gaps), " >30 ms:", sum(g > 30 for g in gaps))
print("incoming ACL from Mac:", rxs, "  other events:", dict(sorted(evs.items(), key=lambda x: -x[1])[:12]))
