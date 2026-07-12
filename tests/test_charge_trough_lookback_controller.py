"""TDD: _trough_by_hour reaches the day's past trough; per-hour avoids over-block."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, UTC

from custom_components.anker_x1_smartgrid.controller import _trough_by_hour
from custom_components.anker_x1_smartgrid.models import PriceSlot


def _slots(base, prices_by_hour):
    return [PriceSlot(base.replace(hour=h), p) for h, p in prices_by_hour.items()]


def test_evening_hour_sees_past_noon_trough():
    base = datetime(2026, 6, 27, 0, 0, tzinfo=UTC)
    prices = {9: 0.20, 10: 0.15, 11: 0.13, 12: 0.13, 13: 0.15, 14: 0.19, 15: 0.25, 16: 0.28, 17: 0.34}
    slots = _slots(base, prices)
    now_h = base.replace(hour=15)  # 15:00 — evening
    horizon_edge = base.replace(hour=18)
    trough = _trough_by_hour(slots, now_h, horizon_edge, lookback_slots=8, slot_minutes=60)
    # [15:00 − 8h = 07:00, 18:00) ∩ real = hours 09..17 → min 0.13
    assert trough[base.replace(hour=15)] == 0.13  # 0.25 current hour is NOT within 0.13+band


def test_future_secondary_trough_not_overblocked():
    base = datetime(2026, 6, 28, 0, 0, tzinfo=UTC)
    prices = {
        1: 0.12,
        2: 0.10,
        3: 0.12,
        4: 0.15,
        5: 0.18,
        6: 0.20,
        7: 0.19,
        8: 0.17,
        9: 0.16,
        10: 0.15,
        11: 0.14,
        12: 0.13,
        13: 0.13,
        14: 0.18,
        15: 0.25,
        16: 0.30,
    }
    slots = _slots(base, prices)
    now_h = base.replace(hour=9)  # 09:00 decision
    horizon_edge = base.replace(hour=17)
    trough = _trough_by_hour(slots, now_h, horizon_edge, lookback_slots=8, slot_minutes=60)
    assert trough[base.replace(hour=9)] == 0.10  # 09:00 sees overnight 0.10 → won't charge
    assert trough[base.replace(hour=13)] == 0.13  # 13:00 window [05:00,17:00) excludes 0.10 → 0.13 chargeable


def test_lookback_zero_is_forward_only():
    base = datetime(2026, 6, 27, 0, 0, tzinfo=UTC)
    prices = {11: 0.13, 12: 0.13, 15: 0.25, 16: 0.28, 17: 0.34}
    slots = _slots(base, prices)
    now_h = base.replace(hour=15)
    horizon_edge = base.replace(hour=18)
    trough = _trough_by_hour(slots, now_h, horizon_edge, lookback_slots=0, slot_minutes=60)
    # forward window [15:00,18:00) only → min 0.25 (no look-back)
    assert trough[base.replace(hour=15)] == 0.25


def test_trough_by_hour_per_day_does_not_leak_across_midnight():
    """A day-1 hour must be judged vs day-1's own trough, not a cheaper day-2 trough."""
    base = datetime(2026, 6, 27, 0, 0, tzinfo=UTC)
    slots = []
    for h in range(48):
        price = 0.30
        if h == 3:
            price = 0.10  # day1 trough
        if h == 27:
            price = 0.05  # day2 (cheaper) trough
        slots.append(PriceSlot(base + timedelta(hours=h), price))
    now_h = base
    horizon_edge = base + timedelta(hours=48)
    trough = _trough_by_hour(slots, now_h, horizon_edge, lookback_slots=4, slot_minutes=60)
    assert trough[base] == 0.10  # day1 vs day1 trough
    assert trough[base + timedelta(hours=24)] == 0.05  # day2 vs day2 trough
