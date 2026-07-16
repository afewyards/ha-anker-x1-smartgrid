"""Layer A residual corrector: prediction log, ratio, adaptive wrapper."""

from datetime import datetime, timedelta, timezone, UTC

import pytest

from custom_components.anker_x1_smartgrid import load_adapt
from custom_components.anker_x1_smartgrid.load_adapt import (
    AdaptivePredictor,
    PredictionLog,
    compute_ratio,
)

H = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)  # now_h


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
    assert log.get(H) is None  # oldest trimmed
    assert log.get(H + timedelta(hours=5)) == 5.0


# ── compute_ratio ──────────────────────────────────────────────────────
def test_ratio_basic():
    log = _log_with({1: 400.0, 2: 400.0, 3: 400.0})
    actuals = _actuals({1: 520.0, 2: 520.0, 3: 520.0})
    ratio, matched, raw = compute_ratio(log, actuals, H, window_h=3)
    assert ratio == pytest.approx(1.3)
    assert matched == 3
    assert raw == pytest.approx(1.3)  # inside the clamp band: raw == clamped


def test_ratio_clamped_up_and_down():
    log = _log_with({1: 400.0, 2: 400.0, 3: 400.0})
    hi, _, hi_raw = compute_ratio(log, _actuals({1: 4000.0, 2: 4000.0, 3: 4000.0}), H, 3)
    lo, _, lo_raw = compute_ratio(log, _actuals({1: 40.0, 2: 40.0, 3: 40.0}), H, 3)
    assert hi == load_adapt.RATIO_MAX
    assert lo == load_adapt.RATIO_MIN
    # raw passes through unclamped in both directions (4000/400=10.0, 40/400=0.1)
    assert hi_raw == pytest.approx(10.0)
    assert lo_raw == pytest.approx(0.1)


def test_ratio_requires_min_matched_hours():
    log = _log_with({1: 400.0})  # only 1 matched
    ratio, matched, raw = compute_ratio(log, _actuals({1: 500.0}), H, 3)
    assert ratio is None
    assert matched == 1
    assert raw is None  # bail case: raw mirrors the same neutral value


def test_ratio_skips_hours_missing_either_side():
    log = _log_with({1: 400.0, 3: 400.0, 4: 400.0})  # hour-2 not logged
    actuals = _actuals({1: 440.0, 2: 999.0, 3: 440.0, 4: 440.0})  # hour-2 unmatched
    ratio, matched, _ = compute_ratio(log, actuals, H, 4)
    assert matched == 3
    assert ratio == pytest.approx(1.1)


def test_ratio_skips_nonpositive_predictions_and_negative_actuals():
    log = _log_with({1: 0.0, 2: 400.0, 3: 400.0})
    actuals = _actuals({1: 500.0, 2: -5.0, 3: 440.0})
    ratio, matched, raw = compute_ratio(log, actuals, H, 3)
    assert ratio is None  # only hour-3 matches
    assert matched == 1
    assert raw is None


def test_ratio_empty_inputs():
    assert compute_ratio(PredictionLog(), {}, H, 3) == (None, 0, None)


def test_ratio_actuals_entry_without_load_w_key():
    log = _log_with({1: 400.0, 2: 400.0})
    actuals = {H - timedelta(hours=1): {"soc": 50.0}, H - timedelta(hours=2): {"load_w": 440.0}}
    ratio, matched, _ = compute_ratio(log, actuals, H, 3)
    assert ratio is None
    assert matched == 1


def test_ratio_prefers_load_kwh_over_load_w():
    """load_kwh (true energy integral) wins even when load_w disagrees."""
    log = _log_with({1: 500.0, 2: 500.0, 3: 500.0})
    actuals = {H - timedelta(hours=b): {"load_kwh": 0.6, "load_w": 999.0} for b in (1, 2, 3)}
    ratio, matched, _ = compute_ratio(log, actuals, H, window_h=3)
    assert matched == 3
    assert ratio == pytest.approx(1.2)  # (0.6*1000)/500, NOT 999/500


def test_ratio_falls_back_to_load_w_when_load_kwh_none():
    log = _log_with({1: 400.0, 2: 400.0, 3: 400.0})
    actuals = {H - timedelta(hours=b): {"load_kwh": None, "load_w": 520.0} for b in (1, 2, 3)}
    ratio, matched, _ = compute_ratio(log, actuals, H, window_h=3)
    assert matched == 3
    assert ratio == pytest.approx(1.3)


def test_ratio_never_matches_current_hour_even_if_present_in_actuals():
    """Regression for the partial-hour trap: a still-in-progress current hour
    (back=0) must never be matched, because its load_kwh only integrates the
    elapsed minutes and would under-read (deflate) the true average power.
    The loop only visits now_h - back for back >= 1, so an entry keyed
    exactly at now_h — even present in both the log and past_actuals, with
    an outlier value that would wildly deflate the ratio if matched — is
    structurally never picked up.
    """
    log = _log_with({0: 999999.0, 1: 400.0, 2: 400.0, 3: 400.0})
    actuals = _actuals({1: 520.0, 2: 520.0, 3: 520.0})
    actuals[H] = {"load_kwh": 0.001, "load_w": 1.0}  # partial in-progress hour
    ratio, matched, _ = compute_ratio(log, actuals, H, window_h=4)
    assert matched == 3  # current hour (back=0) never counted
    assert ratio == pytest.approx(1.3)


# ── compute_ratio: partial-hour extension ───────────────────────────────
def test_ratio_partial_hour_weighted_in():
    # 3 completed hours predicted 1000 W, actual 1000 W each → ratio 1.0
    log = _log_with({1: 1000.0, 2: 1000.0, 3: 1000.0})
    log.record(H, 1000.0)  # current-hour prediction also logged
    actuals = _actuals({1: 1000.0, 2: 1000.0, 3: 1000.0})
    base, _, _ = load_adapt.compute_ratio(log, actuals, H, 5)
    assert base == 1.0
    # running hour at 2000 W for half the hour → weighted ratio > 1
    ratio, matched, _ = load_adapt.compute_ratio(
        log,
        actuals,
        H,
        5,
        partial=(2000.0, 0.5),
    )
    assert matched == 4
    # (3×1000 + 2000×0.5) / (3×1000 + 1000×0.5) = 4000/3500
    assert abs(ratio - 4000.0 / 3500.0) < 1e-9


def test_ratio_partial_below_min_frac_ignored():
    log = _log_with({1: 1000.0, 2: 1000.0, 3: 1000.0})
    log.record(H, 1000.0)
    actuals = _actuals({1: 1000.0, 2: 1000.0, 3: 1000.0})
    r_with, m, _ = load_adapt.compute_ratio(log, actuals, H, 5, partial=(2000.0, 0.2))
    r_without, _, _ = load_adapt.compute_ratio(log, actuals, H, 5)
    assert m == 3 and r_with == r_without


def test_ratio_partial_without_logged_prediction_ignored():
    log = _log_with({1: 1000.0, 2: 1000.0, 3: 1000.0})
    actuals = _actuals({1: 1000.0, 2: 1000.0, 3: 1000.0})
    _, m, _ = load_adapt.compute_ratio(log, actuals, H, 5, partial=(2000.0, 0.5))
    assert m == 3  # no log entry for H → partial contributes nothing


# ── pin timer (RATIO_MAX saturation diagnostic) ─────────────────────────
def test_pinned_since_starts_on_first_pin():
    pinned = load_adapt.update_pinned_since(None, load_adapt.RATIO_MAX, H)
    assert pinned == H


def test_pinned_since_holds_while_still_pinned():
    later = H + timedelta(hours=2)
    pinned = load_adapt.update_pinned_since(H, load_adapt.RATIO_MAX, later)
    assert pinned == H  # start time unchanged, not reset to `later`


def test_pinned_since_resets_below_ratio_max():
    pinned = load_adapt.update_pinned_since(H, load_adapt.RATIO_MIN, H + timedelta(hours=1))
    assert pinned is None


def test_pinned_since_resets_when_ratio_none():
    pinned = load_adapt.update_pinned_since(H, None, H + timedelta(hours=1))
    assert pinned is None


def test_pinned_hours_zero_when_unpinned():
    assert load_adapt.pinned_hours(None, H) == 0.0


def test_pinned_hours_elapsed_rounded_to_one_decimal():
    later = H + timedelta(hours=3, minutes=25)  # 3.4167h → 3.4
    assert load_adapt.pinned_hours(H, later) == pytest.approx(3.4)


def test_pin_timer_grows_across_ticks_and_resets_to_zero_on_unpin():
    """Simulates successive controller updates: pinned_h grows tick over tick
    while the ratio stays saturated, then drops to 0.0 the instant it unpins.
    """
    pinned = load_adapt.update_pinned_since(None, load_adapt.RATIO_MAX, H)
    assert load_adapt.pinned_hours(pinned, H) == 0.0

    t1 = H + timedelta(hours=2)
    pinned = load_adapt.update_pinned_since(pinned, load_adapt.RATIO_MAX, t1)
    assert load_adapt.pinned_hours(pinned, t1) == pytest.approx(2.0)

    t2 = t1 + timedelta(hours=1, minutes=30)
    pinned = load_adapt.update_pinned_since(pinned, load_adapt.RATIO_MAX, t2)
    assert load_adapt.pinned_hours(pinned, t2) == pytest.approx(3.5)

    t3 = t2 + timedelta(hours=1)
    pinned = load_adapt.update_pinned_since(pinned, 1.2, t3)  # unpin
    assert pinned is None
    assert load_adapt.pinned_hours(pinned, t3) == 0.0


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
    assert cfg.load_adapt_fraction == 0.7
    assert cfg.load_adapt_window_h == 5
    assert cfg.load_adapt_fade_h == 8

    cfg2 = Config.from_dict(
        {
            const.CONF_LOAD_ADAPT_FRACTION: 0.0,
            const.CONF_LOAD_ADAPT_WINDOW_H: 4,
            const.CONF_LOAD_ADAPT_FADE_H: 6,
        }
    )
    assert cfg2.load_adapt_fraction == 0.0
    assert cfg2.load_adapt_window_h == 4
    assert cfg2.load_adapt_fade_h == 6


def test_load_adapt_retuned_defaults():
    from custom_components.anker_x1_smartgrid import const

    assert const.DEFAULT_LOAD_ADAPT_WINDOW_H == 5
    assert load_adapt.MIN_MATCHED_HOURS == 3
    assert const.DEFAULT_LOAD_ADAPT_FRACTION == 0.7


def test_min_matched_hours_gate_needs_three():
    """2 matched hours no longer produce a ratio (None); 3 do."""
    now_h = datetime(2026, 7, 8, 12, tzinfo=UTC)
    log = PredictionLog()
    pa = {}
    for back in (1, 2):
        h = now_h - timedelta(hours=back)
        log.record(h, 100.0)
        pa[h] = {"load_w": 120.0}
    assert compute_ratio(log, pa, now_h, window_h=5) == (None, 2, None)
    h3 = now_h - timedelta(hours=3)
    log.record(h3, 100.0)
    pa[h3] = {"load_w": 120.0}
    ratio, matched, _ = compute_ratio(log, pa, now_h, window_h=5)
    assert matched == 3 and ratio is not None
