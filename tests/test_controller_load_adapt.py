"""Controller wiring for the Layer A residual corrector."""

from datetime import datetime, timedelta, timezone, UTC

import pytest

from custom_components.anker_x1_smartgrid.controller import Controller
from custom_components.anker_x1_smartgrid.intra_hour import HourAccumulator
from custom_components.anker_x1_smartgrid.load_adapt import AdaptivePredictor, PredictionLog
from custom_components.anker_x1_smartgrid.models import Config

NOW = datetime(2026, 7, 4, 12, 30, tzinfo=UTC)
NOW_H = NOW.replace(minute=0, second=0, microsecond=0)


class _StubPredictor:
    def __init__(self, base_w=400.0):
        self.base_w = base_w
        self.calls = []

    def predict(self, when, temp, fallback_w, *, quantile=0.5):
        self.calls.append((when, temp, fallback_w, quantile))
        return self.base_w


def _make_ctl(**cfg_overrides) -> Controller:
    """Bare controller via __new__ (mimics tests/test_controller_phase2.py:_make_retrain_ctl)."""
    ctl = Controller.__new__(Controller)
    ctl.cfg = Config.from_dict(cfg_overrides)
    ctl.predictor = _StubPredictor()
    ctl._load_adapt_log = PredictionLog()
    ctl._load_adapt_ratio = None
    ctl._load_adapt_matched = 0
    ctl._hour_acc = HourAccumulator()
    return ctl


def _actuals(hours_back_to_w):
    return {NOW_H - timedelta(hours=b): {"load_w": w} for b, w in hours_back_to_w.items()}


def test_fraction_zero_returns_base_identity():
    ctl = _make_ctl(load_adapt_fraction=0.0)
    # Log/actuals would otherwise produce a ratio:
    for b in (1, 2, 3):
        ctl._load_adapt_log.record(NOW_H - timedelta(hours=b), 400.0)
    out = ctl._update_load_adapt(NOW, 20.0, _actuals({1: 520.0, 2: 520.0, 3: 520.0}))
    assert out is ctl.predictor  # byte-identical guarantee


def test_no_ratio_returns_base_identity_and_records_current_hour():
    ctl = _make_ctl()
    out = ctl._update_load_adapt(NOW, 20.0, {})
    assert out is ctl.predictor  # empty log → no ratio
    assert ctl._load_adapt_log.get(NOW_H) == 400.0  # current hour logged
    assert ctl._load_adapt_ratio is None


def test_ratio_produces_adaptive_wrapper():
    ctl = _make_ctl()
    for b in (1, 2, 3):
        ctl._load_adapt_log.record(NOW_H - timedelta(hours=b), 400.0)
    out = ctl._update_load_adapt(NOW, 20.0, _actuals({1: 520.0, 2: 520.0, 3: 520.0}))
    assert isinstance(out, AdaptivePredictor)
    assert ctl._load_adapt_ratio == pytest.approx(1.3)
    assert ctl._load_adapt_matched == 3
    # fraction=0.7 (default): 400 * (1 + 0.3*0.7) = 484
    assert out.predict(NOW, 20.0, 350.0) == pytest.approx(484.0)


def test_log_records_base_not_wrapped_prediction():
    ctl = _make_ctl()
    for b in (1, 2):
        ctl._load_adapt_log.record(NOW_H - timedelta(hours=b), 400.0)
    ctl._update_load_adapt(NOW, 20.0, _actuals({1: 600.0, 2: 600.0}))
    # Second tick same hour: logged value must still be the BASE 400, not 400×ratio.
    ctl._update_load_adapt(NOW + timedelta(minutes=1), 20.0, _actuals({1: 600.0, 2: 600.0}))
    assert ctl._load_adapt_log.get(NOW_H) == 400.0


def test_base_predict_exception_degrades_gracefully():
    class _Boom:
        def predict(self, *a, **k):
            raise RuntimeError("boom")

    ctl = _make_ctl()
    ctl.predictor = _Boom()
    out = ctl._update_load_adapt(NOW, 20.0, {})
    assert out is ctl.predictor  # never raises, returns base


def _away_ctl(persons_now, hourly_max):
    ctl = _make_ctl()
    ctl._persons_home_now = persons_now
    ctl._occ_recent_max = hourly_max
    for b in range(1, 25):
        ctl._load_adapt_log.record(NOW_H - timedelta(hours=b), 400.0)
    return ctl, _actuals({b: 132.0 for b in range(1, 25)})


def test_away_mode_scales_far_horizon_by_full_ratio():
    ctl, act = _away_ctl(0, [0.0] * 24)
    out = ctl._update_load_adapt(NOW, 20.0, act)
    assert ctl._load_adapt_away is True
    assert ctl._load_adapt_ratio == pytest.approx(0.33)
    assert out.predict(NOW + timedelta(hours=20), 20.0, 350.0) == pytest.approx(132.0)


def test_person_home_ends_away_mode():
    ctl, act = _away_ctl(1, [0.0] * 24)
    out = ctl._update_load_adapt(NOW, 20.0, act)
    assert ctl._load_adapt_away is False
    assert ctl._load_adapt_ratio == pytest.approx(0.7)
    assert out.predict(NOW + timedelta(hours=20), 20.0, 350.0) == pytest.approx(400.0)  # faded out
