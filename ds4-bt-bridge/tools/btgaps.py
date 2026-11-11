#!/usr/bin/env python3
"""Print times of completion gaps > 12 ms in a btmon text trace (stdin) and the spacing between them."""
import re, sys, collections
nocp = re.compile(r"^> HCI Event: Number of Comp.*\[hci0\] ([\d.]+)")
last, big = None, []
for line in sys.stdin:
    m = nocp.match(line)
    if m:
        t = float(m.group(1))
        if last is not None and t - last > 0.012: big.append((last, (t - last) * 1000))
        last = t
sp = [round((b[0] - a[0]) * 1000) for a, b in zip(big, big[1:])]
print("gaps (start s, len ms):", [(round(a, 3), round(b)) for a, b in big[:40]])
print("spacing ms (rounded to 5):", collections.Counter(5 * round(s / 5) for s in sp).most_common(15))
print("gap length ms:", collections.Counter(5 * round(b / 5) for _, b in big).most_common(10))
