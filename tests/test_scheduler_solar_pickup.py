"""Tests: find_next_solar_pickup() — price-independent ride-out endpoint."""

from datetime import datetime, timedelta, timezone, UTC

from custom_components.anker_x1_smartgrid.models import ForecastInterval
from custom_components.anker_x1_smartgrid.scheduler import find_next_solar_pickup

NOW = datetime(2026, 6, 26, 22, 0, tzinfo=UTC)  # 22:00 — night


def _ivs(pv_loads, start=NOW, dt_h=1.0):
    return [ForecastInterval(start + timedelta(hours=i), pv, load, dt_h) for i, (pv, load) in enumerate(pv_loads)]


def test_returns_first_pv_surplus_hour():
    # surplus (pv-load >= 200W) first appears at +8h (tomorrow's ramp).
    ivs = _ivs([(0.0, 400.0)] * 8 + [(2000.0, 300.0)] + [(0.0, 400.0)] * 3)
    assert find_next_solar_pickup(NOW, ivs) == NOW + timedelta(hours=8)


def test_cheap_night_is_not_a_pickup():
    # No interval ever clears the surplus threshold -> None (reserve spans full horizon).
    ivs = _ivs([(0.0, 400.0)] * 12)
    assert find_next_solar_pickup(NOW, ivs) is None


def test_threshold_boundary_exact_200w_counts():
    ivs = _ivs([(0.0, 400.0)] * 3 + [(600.0, 400.0)])  # surplus exactly 200W at +3h
    assert find_next_solar_pickup(NOW, ivs) == NOW + timedelta(hours=3)


def test_just_below_threshold_skipped():
    ivs = _ivs([(599.0, 400.0)] * 4)  # surplus 199W < 200W everywhere -> None
    assert find_next_solar_pickup(NOW, ivs) is None


def test_ignores_past_hours():
    later = NOW + timedelta(hours=1)
    ivs = _ivs([(5000.0, 100.0)])  # surplus at NOW, but we query from NOW+1h
    assert find_next_solar_pickup(later, ivs) is None
