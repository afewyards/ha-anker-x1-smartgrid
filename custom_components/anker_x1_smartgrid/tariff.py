"""Pure synthetic price-slot generator for static tariff mode.

No Home Assistant imports — unit-testable in isolation.  ``synth_static_price_slots``
(added in a later task) turns a static tariff config (flat, or HP/HC with off-peak
wall-clock ranges) into UTC PriceSlots over a rolling top-of-current-hour →
tomorrow-local-midnight horizon.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import gcd

from .models import Config, PriceSlot


def _parse_hhmm(value: str) -> int:
    """Parse 'HH:MM' → minutes-of-day (0..1439). Raises ValueError if malformed."""
    s = value.strip()
    if s.count(":") != 1:
        raise ValueError(f"time {value!r} must be HH:MM")
    hh, mm = s.split(":")
    if not (hh.isdigit() and mm.isdigit()):
        raise ValueError(f"time {value!r} must be numeric HH:MM")
    h, m = int(hh), int(mm)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"time {value!r} out of range 00:00-23:59")
    return h * 60 + m


def parse_offpeak_ranges(spec: str | None) -> list[tuple[int, int]]:
    """Parse 'HH:MM-HH:MM,...' → list of (start_min, end_min) minutes-of-day.

    Empty/blank → []. start > end means the range wraps midnight (interpreted at
    membership time). Raises ValueError on any malformed token.
    """
    text = (spec or "").strip()
    if not text:
        return []
    ranges: list[tuple[int, int]] = []
    for token in text.split(","):
        part = token.strip()
        if part.count("-") != 1:
            raise ValueError(f"range {token!r} must be HH:MM-HH:MM")
        lo, hi = part.split("-")
        ranges.append((_parse_hhmm(lo), _parse_hhmm(hi)))
    return ranges


def _in_offpeak(minute_of_day: int, ranges: list[tuple[int, int]]) -> bool:
    """True when minute_of_day falls in any half-open (start, end) range.

    start < end: [start, end).  start > end: wraps midnight → [start, 1440) ∪
    [0, end).  start == end: empty.
    """
    for start, end in ranges:
        if start == end:
            continue
        if start < end:
            if start <= minute_of_day < end:
                return True
        elif minute_of_day >= start or minute_of_day < end:
            return True
    return False


def _resolution_minutes(ranges: list[tuple[int, int]]) -> int:
    """Slot width: 60 if every boundary is on the hour, else gcd of the boundary
    minute-of-hour offsets, floored at 15."""
    if not ranges:
        return 60
    g = 60
    for start, end in ranges:
        g = gcd(g, start % 60)
        g = gcd(g, end % 60)
    if g <= 0:
        g = 60
    return max(15, g)
