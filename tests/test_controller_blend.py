"""Controller wiring: current-hour blend + partial-hour load-adapt flags."""

from datetime import datetime, timedelta, timezone, UTC

from custom_components.anker_x1_smartgrid.controller import Controller
from custom_components.anker_x1_smartgrid.intra_hour import CurrentHourBlendPredictor, HourAccumulator
from custom_components.anker_x1_smartgrid.load_adapt import PredictionLog
from custom_components.anker_x1_smartgrid.models import Config

NOW = datetime(2026, 6, 3, 12, 30, tzinfo=UTC)
NOW_H = NOW.replace(minute=0)

_BASE_P50_W = 400.0


class _StubPredictor:
    def __init__(self, base_w=_BASE_P50_W):
        self.base_w = base_w

    def predict(self, when, temp, fallback_w, *, quantile=0.5):
        return self.base_w


def _make_ctl(cfg_overrides: dict) -> Controller:
    """Bare controller via __new__ (mimics tests/test_controller_load_adapt.py:_make_ctl)."""
    ctl = Controller.__new__(Controller)
    ctl.cfg = Config.from_dict(cfg_overrides)
    ctl.predictor = _StubPredictor()
    ctl._load_adapt_log = PredictionLog()
    ctl._load_adapt_ratio = None
    ctl._load_adapt_matched = 0
    ctl._occ_table = None
    ctl._persons_home_now = None
    ctl._hour_acc = HourAccumulator()
    return ctl


def _prime_acc(ctl, minutes=30, load_w=2000.0):
    for i in range(minutes + 1):
        ctl._hour_acc.add(NOW_H + timedelta(minutes=i), load_w)


def _seed_matched_hours(ctl, now_h, ratio=1.0, hours=3):
    """Seed ctl._load_adapt_log with ``hours`` completed predictions at
    _BASE_P50_W; remember the seeded hours + ratio so _seeded_actuals can
    replay matching actuals (actual = predicted * ratio)."""
    ctl._seed_hours = [now_h - timedelta(hours=b) for b in range(1, hours + 1)]
    ctl._seed_ratio = ratio
    for h in ctl._seed_hours:
        ctl._load_adapt_log.record(h, _BASE_P50_W)


def _seeded_actuals(ctl) -> dict:
    ratio = getattr(ctl, "_seed_ratio", 1.0)
    return {h: {"load_w": _BASE_P50_W * ratio} for h in ctl._seed_hours}


def test_default_flags_no_blend_wrapper():
    ctl = _make_ctl({})
    _prime_acc(ctl)
    pred = ctl._update_load_adapt(NOW, 20.0, {})
    assert not isinstance(pred, CurrentHourBlendPredictor)


def test_blend_flag_wraps_outermost():
    ctl = _make_ctl({"current_hour_blend": True})
    _prime_acc(ctl)
    pred = ctl._update_load_adapt(NOW, 20.0, {})
    assert isinstance(pred, CurrentHourBlendPredictor)
    # log recorded the UNBLENDED p50
    base_w = ctl.predictor.predict(NOW_H, 20.0, 250.0, quantile=0.5)
    assert ctl._load_adapt_log.get(NOW_H) == base_w


def test_partial_hour_flag_feeds_ratio():
    # 3 completed matched hours at ratio 1.0; running hour hot at 2× → ratio rises
    ctl = _make_ctl({"load_adapt_fraction": 0.7, "load_adapt_partial_hour": True})
    _prime_acc(ctl, minutes=30, load_w=2 * _BASE_P50_W)
    _seed_matched_hours(ctl, NOW_H, ratio=1.0, hours=3)
    ctl._update_load_adapt(NOW, 20.0, _seeded_actuals(ctl))
    assert ctl._load_adapt_ratio is not None and ctl._load_adapt_ratio > 1.0
    assert ctl._load_adapt_matched == 4


def test_partial_hour_flag_off_ignores_running_hour():
    ctl = _make_ctl({"load_adapt_fraction": 0.7})
    _prime_acc(ctl, minutes=30, load_w=2 * _BASE_P50_W)
    _seed_matched_hours(ctl, NOW_H, ratio=1.0, hours=3)
    ctl._update_load_adapt(NOW, 20.0, _seeded_actuals(ctl))
    assert ctl._load_adapt_matched == 3
