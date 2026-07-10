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


def synth_static_price_slots(now: datetime, cfg: Config, tz) -> list[PriceSlot]:
    """Synthesize import PriceSlots for static tariff mode.

    Horizon: top of the current local hour → local midnight ending tomorrow
    (00:00 of now.date()+2 days), emitted as a contiguous UTC grid — a uniform
    UTC stride with a local-time price lookup is DST-safe by construction.

    Resolution: 60 min for a flat tariff or all-on-hour off-peak boundaries;
    otherwise gcd of the boundary minute offsets, floored at 15 min.

    Price: ``cfg.static_price_import`` (peak), or ``cfg.static_price_offpeak``
    when the slot's local start time is in an off-peak range.  Off-peak is
    active only when ranges are configured AND static_price_offpeak > 0 (0/unset
    ⇒ flat-only).  An invalid ranges string is treated as flat (config flow
    validates on entry; this guards direct/legacy edits).
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    try:
        ranges = parse_offpeak_ranges(cfg.static_offpeak_hours)
    except ValueError:
        ranges = []
    import_price = cfg.static_price_import
    offpeak_price = cfg.static_price_offpeak
    use_offpeak = bool(ranges) and offpeak_price > 0.0
    step_min = _resolution_minutes(ranges) if use_offpeak else 60

    now_local = now.astimezone(tz)
    start_local = now_local.replace(minute=0, second=0, microsecond=0)
    end_date = now_local.date() + timedelta(days=2)
    end_local = datetime(end_date.year, end_date.month, end_date.day, 0, 0, tzinfo=tz)
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)

    step = timedelta(minutes=step_min)
    slots: list[PriceSlot] = []
    t = start_utc
    while t < end_utc:
        local = t.astimezone(tz)
        minute_of_day = local.hour * 60 + local.minute
        price = (
            offpeak_price
            if use_offpeak and _in_offpeak(minute_of_day, ranges)
            else import_price
        )
        slots.append(PriceSlot(t, price, duration_min=float(step_min)))
        t += step
    return slots
