"""Pure price-slot resolution detection (no Home Assistant imports)."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from .models import PriceSlot

_ALLOWED = (15, 30, 60)
_MIN_GAP_MIN = 10  # ignore deltas below this as duplicate / DST artifacts


def _snap(minutes: float) -> int:
    """Snap a raw minute delta to the nearest allowed slot length."""
    return min(_ALLOWED, key=lambda a: abs(a - minutes))


def detect_slot_minutes(slots: list[PriceSlot]) -> int:
    """Finest (MIN) consecutive-start delta, snapped to {15,30,60}; fallback 60.

    MIN (not mode): during rollout the forecast is mixed (near-term 15-min,
    far-term 60-min).  MIN picks 15 so coarse entries can be forward-filled onto
    the fine grid; mode would pick 60 and drop the 15-min slots on the highest-
    value day.  A min-gap floor (_MIN_GAP_MIN) ignores sub-10-min duplicate/DST
    artifacts.  Fallback 60 when <2 slots or no delta clears the floor.
    """
    if not slots or len(slots) < 2:
        return 60
    starts = sorted(s.start for s in slots)
    deltas = [
        (b - a).total_seconds() / 60.0
        for a, b in zip(starts, starts[1:])
        if (b - a).total_seconds() / 60.0 >= _MIN_GAP_MIN
    ]
    if not deltas:
        return 60
    return _snap(min(deltas))


def resolve_slot_minutes(slots: list[PriceSlot], override: str) -> int:
    """`override` in {"15","30","60"} pins; anything else ("auto") -> detect."""
    if override in ("15", "30", "60"):
        return int(override)
    return detect_slot_minutes(slots)


def latch_finest(
    detected: int,
    now_utc: datetime,
    state: tuple[int, date] | None,
) -> tuple[int, tuple[int, date]]:
    """Latch the finest (smallest) slot_minutes seen so far this UTC day.

    Reset at UTC-day rollover.  A coarser tail cannot flip the latch back within
    a day (anti-flap): prevents 15<->60 thrash as the 15-min head is consumed and
    only the hourly tail remains, which would repeatedly clear committed state.
    Returns (effective_slot_minutes, new_state) where new_state = (latched, utc_date).
    """
    day = now_utc.date()
    if state is None or state[1] != day:
        return detected, (detected, day)
    latched = min(state[0], detected)
    return latched, (latched, day)


def floor_to_slot(dt: datetime, slot_minutes: int) -> datetime:
    """Floor `dt` to the `slot_minutes` grid.  At 60 == top-of-hour floor.

    `.replace()`-based (NOT epoch): at slot_minutes=60 reduces provably to
    dt.replace(minute=0, second=0, microsecond=0) in dt's own frame (any tz).
    """
    minute = (dt.minute // slot_minutes) * slot_minutes
    return dt.replace(minute=minute, second=0, microsecond=0)


def resample_price_map(
    slots: list[PriceSlot], slot_minutes: int, *, horizon_end: datetime | None = None
) -> dict[datetime, float]:
    """Fine-grid price map keyed by slot-start; coarse slots forward-filled.

    Each slot spans its own duration (delta to the next slot) expanded into
    `slot_minutes`-wide sub-slots, so a coarse (e.g. 60-min) entry among fine
    (15-min) entries is FORWARD-FILLED across its sub-slots.  At a uniform
    resolution == `slot_minutes` every slot yields one key; at 60 the map
    equals the legacy `{s.start.replace(minute=0,...): s.price}` dict (byte
    parity).

    The chronologically LAST slot has no successor to infer a span from.
    Default (`horizon_end=None`, byte-identical for every existing caller):
    it gets a single `slot_minutes`-wide span.  Callers that know the true
    horizon end (e.g. the real-price window boundary) can pass `horizon_end`
    so the last slot spans `[slot.start, horizon_end)` instead — the same
    successor-inferred rule interior slots already follow, just anchored to
    the horizon edge instead of a real next slot.
    """
    out: dict[datetime, float] = {}
    ordered = sorted(slots, key=lambda s: s.start)
    n = len(ordered)
    stride = timedelta(minutes=slot_minutes)
    for i, slot in enumerate(ordered):
        if i + 1 < n:
            infer_end = ordered[i + 1].start
        elif horizon_end is not None:
            infer_end = horizon_end
        else:
            infer_end = slot.start + stride
        infer_span = round((infer_end - slot.start).total_seconds() / 60.0)
        if slot.duration_min is None:
            span_min = max(slot_minutes, infer_span)
        elif i == n - 1 and horizon_end is not None:
            span_min = max(slot_minutes, round(slot.duration_min), infer_span)
        else:
            span_min = max(slot_minutes, round(slot.duration_min))
        n_sub = max(1, round(span_min / slot_minutes))
        base = floor_to_slot(slot.start, slot_minutes)
        for k in range(n_sub):
            out[base + k * stride] = slot.price
    return out
