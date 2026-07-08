# tests/test_scheduler_trough.py
from datetime import datetime, timedelta, timezone

from custom_components.anker_x1_smartgrid.models import Config, PriceSlot
from custom_components.anker_x1_smartgrid.scheduler import find_next_trough

NOW = datetime(2026, 6, 23, 12, 0, tzinfo=timezone.utc)


def _slots(prices, start=NOW):
    return [PriceSlot(start + timedelta(hours=i), p) for i, p in enumerate(prices)]


def test_picks_local_min_below_percentile_at_or_after_min_horizon():
    # Dip at +8h is the only sub-P30 local min, comfortably past min_horizon_h=6.
    prices = [0.30] * 8 + [0.10] + [0.30] * 3
    trough_dt, ref = find_next_trough(NOW, _slots(prices), Config())
    assert trough_dt == NOW + timedelta(hours=8)
    assert ref == 0.10


def test_skips_near_min_for_later_qualifier():
    # Dip at +2h is inside min_horizon_h and must be skipped; +10h qualifies.
    prices = [0.30, 0.30, 0.10, 0.30, 0.30, 0.30, 0.30, 0.30, 0.30, 0.30, 0.10, 0.30]
    trough_dt, _ = find_next_trough(NOW, _slots(prices), Config())
    assert trough_dt == NOW + timedelta(hours=10)


def test_no_qualifier_falls_back_to_end_of_known_prices():
    # Flat 4-slot window: no slot is >= min_horizon_h out -> fallback.
    prices = [0.20, 0.20, 0.20, 0.20]
    trough_dt, ref = find_next_trough(NOW, _slots(prices), Config())
    assert trough_dt == NOW + timedelta(hours=3)
    assert ref == 0.20


def test_descending_window_endpoint_is_local_min():
    # Strictly descending: only the last slot is a local min (right neighbor = +inf).
    prices = [0.30 - 0.02 * i for i in range(12)]
    trough_dt, ref = find_next_trough(NOW, _slots(prices), Config())
    assert trough_dt == NOW + timedelta(hours=11)
    assert abs(ref - prices[11]) < 1e-9


def test_negative_trough_price_passes_through():
    prices = [0.30] * 8 + [-0.05] + [0.30] * 3
    trough_dt, ref = find_next_trough(NOW, _slots(prices), Config())
    assert trough_dt == NOW + timedelta(hours=8)
    assert ref == -0.05
