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
