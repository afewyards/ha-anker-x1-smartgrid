"""Task 2 — controller._retrain_sync Tier-2 (Bucketed) rewired onto hourly

energy rollups (samples_hourly) instead of per-tick W samples.
"""

from datetime import datetime, timedelta, timezone, UTC

import pytest

from custom_components.anker_x1_smartgrid import forecast
from custom_components.anker_x1_smartgrid.controller import Controller
from custom_components.anker_x1_smartgrid.models import Config


class _HourlyRec:
    """Recorder stub: returns fixed hourly rollup rows regardless of since_iso.

    Mirrors both call sites in _retrain_sync — Tier-1's own unfiltered
    ``read_hourly_rows()`` and the new Tier-2 filtered
    ``read_hourly_rows(since_iso=...)`` — with the same synthetic data.
    """

    def __init__(self, hourly_rows):
        self._hourly_rows = hourly_rows

    def read_hourly_rows(self, since_iso=None):
        return self._hourly_rows

    def append(self, row):
        pass

    def purge_older_than(self, *a):
        return 0


def _make_ctl(rec, **cfg_overrides) -> Controller:
    """Bare controller via __new__ (mirrors _make_retrain_ctl in test_controller_phase2.py)."""
    data = {
        "use_learned_model": True,
        "train_days": 14,
        "backtest_test_days": 3,
        **cfg_overrides,
    }
    ctl = Controller.__new__(Controller)
    ctl._hass = None
    ctl._data = data
    ctl._recorder = rec
    ctl.cfg = Config.from_dict(data)
    ctl.profile = {}
    ctl.predictor = forecast.LoadPredictor.from_profile({})
    ctl._profile_predictor = forecast.LoadPredictor.from_profile({})
    ctl.backtest_result = None
    ctl.active_model_name = "profile"
    return ctl


_BASE = datetime(2026, 6, 1, tzinfo=UTC)


def _hourly_rows(n, *, kwh_sum=0.5, temp=15.0):
    return [
        {
            "hour_ts": (_BASE + timedelta(hours=i)).isoformat(),
            "house_load_kwh_sum": kwh_sum,
            "temp_mean": temp,
        }
        for i in range(n)
    ]


def test_retrain_selects_bucketed_from_hourly_rollups_when_hgbr_unavailable():
    """60 hourly rows (~2.5 days) sits far below HGBR's ~28-day coverage gate
    (is_ready naturally returns False — no lag-complete rows at a 168h offset),
    so Tier-1 falls through. It clears DEFAULT_MIN_TRAIN_HOURS (48) for Tier-2,
    so the bucketed model wins and, since every training row carries
    house_load_kwh_sum=0.5 (-> 500 W) at temp_mean=15.0, predicts ~500 W for a
    matching hour/temp.
    """
    rec = _HourlyRec(_hourly_rows(60))
    ctl = _make_ctl(rec)

    ctl._retrain_sync("2026-06-01T00:00:00+00:00")

    assert ctl.active_model_name == "bucketed"
    when = _BASE + timedelta(hours=5)  # an hour actually present in the training rows
    predicted = ctl.predictor.predict(when, 15.0, 400.0)
    assert predicted == pytest.approx(500.0)


def test_retrain_falls_to_profile_when_too_few_hourly_rows():
    """10 hourly rows is below DEFAULT_MIN_TRAIN_HOURS (48) -> falls through to profile."""
    rec = _HourlyRec(_hourly_rows(10))
    ctl = _make_ctl(rec)

    ctl._retrain_sync("2026-06-01T00:00:00+00:00")

    assert ctl.active_model_name == "profile"
