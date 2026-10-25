"""Fast "is this IP in any whitelisted range?" checks.

Ranges are merged and sorted once. Each lookup is a binary search,
so 30,000 IPs against thousands of ranges takes milliseconds.
"""

from __future__ import annotations

import bisect
import ipaddress
from typing import Iterable


class RangeSet:
    def __init__(self, networks: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network] = ()):
        spans: dict[int, list[tuple[int, int]]] = {4: [], 6: []}
        for net in networks:
            spans[net.version].append((int(net.network_address), int(net.broadcast_address)))
        self._starts: dict[int, list[int]] = {}
        self._ends: dict[int, list[int]] = {}
        for version, items in spans.items():
            merged: list[list[int]] = []
            for start, end in sorted(items):
                if merged and start <= merged[-1][1] + 1:
                    merged[-1][1] = max(merged[-1][1], end)
                else:
                    merged.append([start, end])
            self._starts[version] = [m[0] for m in merged]
            self._ends[version] = [m[1] for m in merged]

    def __contains__(self, ip: object) -> bool:
        if isinstance(ip, str):
            ip = ipaddress.ip_address(ip)
        assert isinstance(ip, (ipaddress.IPv4Address, ipaddress.IPv6Address))
        starts = self._starts[ip.version]
        value = int(ip)
        index = bisect.bisect_right(starts, value) - 1
        return index >= 0 and value <= self._ends[ip.version][index]

    def __len__(self) -> int:
        return len(self._starts[4]) + len(self._starts[6])
