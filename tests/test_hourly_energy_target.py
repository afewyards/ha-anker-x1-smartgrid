"""TDD tests for featureset.hourly_load_w — energy-integral hourly load helper.

Public API under test:
    hourly_load_w(row: dict) -> float | None

Prefers house_load_kwh_sum (energy integral, scaled x1000 to W) over the
count-weighted house_load_mean, falling back to house_load_mean when the
kwh_sum column is missing/None.
"""

from __future__ import annotations

from custom_components.anker_x1_smartgrid.featureset import hourly_load_w


def test_prefers_kwh_sum_scaled_to_w():
    assert hourly_load_w({"house_load_kwh_sum": 0.5, "house_load_mean": 700.0}) == 500.0


def test_falls_back_to_mean_when_kwh_null():
    assert hourly_load_w({"house_load_kwh_sum": None, "house_load_mean": 700.0}) == 700.0


def test_none_when_both_null():
    assert hourly_load_w({}) is None


# ---------------------------------------------------------------------------
# Partial-coverage rescaling (F1) — recorder gaps (HA restart) leave
# house_load_count < 60, so kwh_sum only integrates the observed ticks but is
# implicitly treated as spanning a full clock-hour. Rescale by
# expected/observed rows to recover the time-weighted average W over the
# observed window instead of diluting the target toward zero.
# ---------------------------------------------------------------------------


def test_partial_coverage_rescaled_to_observed_window():
    # 20 of 60 expected ticks observed: 0.167 kWh * 1000 * 60/20 ≈ 501 W.
    row = {"house_load_kwh_sum": 0.167, "house_load_count": 20, "house_load_mean": 480.0}
    v = hourly_load_w(row)
    assert v is not None
    assert abs(v - 501.0) < 1e-6


def test_full_coverage_unscaled():
    # count == 60 (full hour) → no rescaling, exactly kwh * 1000.
    row = {"house_load_kwh_sum": 0.5, "house_load_count": 60}
    assert hourly_load_w(row) == 500.0


def test_count_none_unscaled_backfilled_rows():
    # Backfilled v10 rows have no house_load_count → unscaled kwh * 1000.
    row = {"house_load_kwh_sum": 0.5, "house_load_count": None}
    assert hourly_load_w(row) == 500.0


def test_count_zero_unscaled_no_div_by_zero():
    # count == 0 must not raise ZeroDivisionError and must not scale.
    row = {"house_load_kwh_sum": 0.5, "house_load_count": 0}
    assert hourly_load_w(row) == 500.0
