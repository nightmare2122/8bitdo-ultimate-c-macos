#!/usr/bin/env python3
"""For each TX completion gap > 12 ms in a btmon text trace (stdin), count Mac->laptop ACL packets received
inside it. Packets arriving during a gap mean the Mac was polling us but our data was not getting through."""
import re, sys
nocp = re.compile(r"^> HCI Event: Number of Comp.*\[hci0\] ([\d.]+)")
rx = re.compile(r"^> (?:BR-)?ACL.*\[hci0\] ([\d.]+)")
ev, last = [], None
for line in sys.stdin:
    m = nocp.match(line)
    if m: ev.append(("c", float(m.group(1)))); continue
    m = rx.match(line)
    if m: ev.append(("r", float(m.group(1))))
comps = [t for k, t in ev if k == "c"]; rxs = [t for k, t in ev if k == "r"]
gaps = [(a, b) for a, b in zip(comps, comps[1:]) if b - a > 0.012]
inside = [sum(a < r < b for r in rxs) for a, b in gaps]
gaptime = sum(b - a for a, b in gaps); total = comps[-1] - comps[0]
rx_in = sum(inside)
print(f"{len(gaps)} gaps covering {100*gaptime/total:.0f}% of {total:.1f}s; rx packets inside gaps {rx_in} of {len(rxs)} "
      f"(expected if independent {len(rxs)*gaptime/total:.0f})")
