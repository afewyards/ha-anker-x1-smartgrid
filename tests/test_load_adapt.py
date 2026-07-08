"""Layer A residual corrector: prediction log, ratio, adaptive wrapper."""
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.anker_x1_smartgrid import load_adapt
from custom_components.anker_x1_smartgrid.load_adapt import (
    AdaptivePredictor, PredictionLog, compute_ratio,
)

H = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)  # now_h


def _log_with(hours_back_to_w: dict[int, float]) -> PredictionLog:
    log = PredictionLog()
    for back, w in hours_back_to_w.items():
        log.record(H - timedelta(hours=back), w)
    return log


def _actuals(hours_back_to_w: dict[int, float]) -> dict:
    return {H - timedelta(hours=b): {"load_w": w} for b, w in hours_back_to_w.items()}


class _StubBase:
    """Records calls; returns fixed base watts."""
    def __init__(self, base_w=400.0):
        self.base_w = base_w
        self.calls = []

    def predict(self, when, temp, fallback_w, *, quantile=0.5):
        self.calls.append((when, temp, fallback_w, quantile))
        return self.base_w


# ── PredictionLog ──────────────────────────────────────────────────────
def test_log_record_get_roundtrip():
    log = PredictionLog()
    log.record(H, 500.0)
    assert log.get(H) == 500.0
    assert log.get(H - timedelta(hours=1)) is None


def test_log_overwrites_same_hour():
    log = PredictionLog()
    log.record(H, 500.0)
    log.record(H, 620.0)
    assert log.get(H) == 620.0
    assert len(log) == 1


def test_log_trims_oldest_past_max():
    log = PredictionLog()
    for i in range(load_adapt.LOG_MAX_ENTRIES + 5):
        log.record(H + timedelta(hours=i), float(i))
    assert len(log) == load_adapt.LOG_MAX_ENTRIES
    assert log.get(H) is None            # oldest trimmed
    assert log.get(H + timedelta(hours=5)) == 5.0


# ── compute_ratio ──────────────────────────────────────────────────────
def test_ratio_basic():
    log = _log_with({1: 400.0, 2: 400.0, 3: 400.0})
    actuals = _actuals({1: 520.0, 2: 520.0, 3: 520.0})
    ratio, matched = compute_ratio(log, actuals, H, window_h=3)
    assert ratio == pytest.approx(1.3)
    assert matched == 3


def test_ratio_clamped_up_and_down():
    log = _log_with({1: 400.0, 2: 400.0})
    hi, _ = compute_ratio(log, _actuals({1: 4000.0, 2: 4000.0}), H, 3)
    lo, _ = compute_ratio(log, _actuals({1: 40.0, 2: 40.0}), H, 3)
    assert hi == load_adapt.RATIO_MAX
    assert lo == load_adapt.RATIO_MIN


def test_ratio_requires_min_matched_hours():
    log = _log_with({1: 400.0})                       # only 1 matched
    ratio, matched = compute_ratio(log, _actuals({1: 500.0}), H, 3)
    assert ratio is None
    assert matched == 1


def test_ratio_skips_hours_missing_either_side():
    log = _log_with({1: 400.0, 3: 400.0})             # hour-2 not logged
    actuals = _actuals({1: 440.0, 2: 999.0, 3: 440.0})  # hour-2 unmatched
    ratio, matched = compute_ratio(log, actuals, H, 3)
    assert matched == 2
    assert ratio == pytest.approx(1.1)


def test_ratio_skips_nonpositive_predictions_and_negative_actuals():
    log = _log_with({1: 0.0, 2: 400.0, 3: 400.0})
    actuals = _actuals({1: 500.0, 2: -5.0, 3: 440.0})
    ratio, matched = compute_ratio(log, actuals, H, 3)
    assert ratio is None                              # only hour-3 matches
    assert matched == 1


def test_ratio_empty_inputs():
    assert compute_ratio(PredictionLog(), {}, H, 3) == (None, 0)


def test_ratio_actuals_entry_without_load_w_key():
    log = _log_with({1: 400.0, 2: 400.0})
    actuals = {H - timedelta(hours=1): {"soc": 50.0},
               H - timedelta(hours=2): {"load_w": 440.0}}
    ratio, matched = compute_ratio(log, actuals, H, 3)
    assert ratio is None
    assert matched == 1


# ── AdaptivePredictor ──────────────────────────────────────────────────
def test_wrapper_full_correction_at_lead_zero():
    base = _StubBase(400.0)
    p = AdaptivePredictor(base, ratio=1.3, now=H, fade_h=8, fraction=1.0)
    assert p.predict(H, 20.0, 350.0) == pytest.approx(400.0 * 1.3)


def test_wrapper_fades_linearly_and_dies_at_fade_h():
    base = _StubBase(400.0)
    p = AdaptivePredictor(base, ratio=1.3, now=H, fade_h=8, fraction=1.0)
    half = p.predict(H + timedelta(hours=4), None, 350.0)
    assert half == pytest.approx(400.0 * (1 + 0.3 * 0.5))
    assert p.predict(H + timedelta(hours=8), None, 350.0) == pytest.approx(400.0)
    assert p.predict(H + timedelta(hours=12), None, 350.0) == pytest.approx(400.0)


def test_wrapper_negative_lead_clamps_to_full_correction():
    p = AdaptivePredictor(_StubBase(400.0), ratio=1.2, now=H, fade_h=8, fraction=1.0)
    assert p.predict(H - timedelta(hours=2), None, 350.0) == pytest.approx(480.0)


def test_wrapper_fraction_scales_correction():
    p = AdaptivePredictor(_StubBase(400.0), ratio=1.4, now=H, fade_h=8, fraction=0.5)
    assert p.predict(H, None, 350.0) == pytest.approx(400.0 * 1.2)


def test_wrapper_downward_ratio():
    p = AdaptivePredictor(_StubBase(400.0), ratio=0.8, now=H, fade_h=8, fraction=1.0)
    assert p.predict(H, None, 350.0) == pytest.approx(320.0)


def test_wrapper_passes_args_through_to_base():
    base = _StubBase(400.0)
    p = AdaptivePredictor(base, ratio=1.1, now=H, fade_h=8, fraction=1.0)
    p.predict(H, 21.5, 333.0, quantile=0.8)
    assert base.calls == [(H, 21.5, 333.0, 0.8)]


def test_wrapper_fade_h_nonpositive_returns_base():
    p = AdaptivePredictor(_StubBase(400.0), ratio=1.5, now=H, fade_h=0, fraction=1.0)
    assert p.predict(H, None, 350.0) == pytest.approx(400.0)


# ── Config plumbing ────────────────────────────────────────────────────
def test_config_defaults_and_from_dict():
    from custom_components.anker_x1_smartgrid import const
    from custom_components.anker_x1_smartgrid.models import Config

    cfg = Config.from_dict({})
    assert cfg.load_adapt_fraction == 1.0
    assert cfg.load_adapt_window_h == 3
    assert cfg.load_adapt_fade_h == 8

    cfg2 = Config.from_dict({
        const.CONF_LOAD_ADAPT_FRACTION: 0.0,
        const.CONF_LOAD_ADAPT_WINDOW_H: 4,
        const.CONF_LOAD_ADAPT_FADE_H: 6,
    })
    assert cfg2.load_adapt_fraction == 0.0
    assert cfg2.load_adapt_window_h == 4
    assert cfg2.load_adapt_fade_h == 6
