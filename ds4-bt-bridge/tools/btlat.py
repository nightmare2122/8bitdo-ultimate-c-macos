#!/usr/bin/env python3
"""Input-to-air latency from a btmon text trace (stdin): for a controller change at a random moment t, the
time until the radio confirms delivery (Number_Of_Completed_Packets) of the first report-1 packet handed to
the controller after t. Includes waiting for the send gate, the radio queue, and the air time. Packets are
matched to completions in order; only xbox report 1 (a1 01 ...) packets count as carrying state."""
import random, re, sys
tx = re.compile(r"^< (?:BR-)?ACL.*\[hci\d\] ([\d.]+)")
nocp = re.compile(r"^> HCI Event: Number of Comp.*\[hci\d\] ([\d.]+)")
cnt = re.compile(r"^\s+Count: (\d+)")
queue, pkts, want, cur = [], [], None, None   # queue: [tx_time, is_state]; pkts: (tx, done) of state packets
for line in sys.stdin:
    m = tx.match(line)
    if m:
        want = [float(m.group(1)), None]; queue.append(want); continue
    if want is not None and want[1] is None and line.strip().startswith(("a1", "a2", "00", "01")):
        if "Channel" not in line:
            want[1] = line.split()[:2] == ["a1", "01"]
    m = nocp.match(line)
    if m: cur = float(m.group(1)); continue
    m = cnt.match(line)
    if m and cur is not None:
        for _ in range(int(m.group(1))):
            if queue:
                t, st = queue.pop(0)
                if st: pkts.append((t, cur))
        cur = None
pkts.sort()
if len(pkts) < 100: sys.exit("not enough packets")
lo, hi = pkts[0][0], pkts[-1][0]
txs = [p[0] for p in pkts]
import bisect
lat = []
for _ in range(20000):
    t = random.uniform(lo, hi)
    i = bisect.bisect_left(txs, t)
    lat.append((min(d for _, d in pkts[i:i + 8]) - t) * 1000)
lat.sort()
q = lambda p: lat[int((len(lat) - 1) * p)]
print(f"input->air over {hi-lo:.1f}s, {len(pkts)/(hi-lo):.0f} state pkts/s: p50 {q(.5):.1f}  p90 {q(.9):.1f}  "
      f"p95 {q(.95):.1f}  p99 {q(.99):.1f}  max {lat[-1]:.1f}  mean {sum(lat)/len(lat):.1f} ms")
