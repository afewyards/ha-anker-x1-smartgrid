"""P50 export ride-out reserve: intervals_reserve uses P50 load, not P80.

When export is gated against the ride-out reserve, the reserve should be sized
to the EXPECTED (P50) overnight load, not the worst-case (P80) load.
The separate P80 grid-charge deficit series was removed in Task 2.

RED on old code (intervals_reserve built from P80 quantile → 900 W).
GREEN after the fix (intervals_reserve built from quantile=0.5 → 300 W).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, UTC

import pytest

from custom_components.anker_x1_smartgrid import controller, energy, parsers
from custom_components.anker_x1_smartgrid.forecast import LoadPredictor, build_intervals
from custom_components.anker_x1_smartgrid.models import (
    Config,
    PlanState,
    PlantInputs,
    PriceSlot,
)

# Summer evening: 20:00 UTC, just before tonight's sunset.
NOW = datetime(2026, 6, 28, 20, 0, tzinfo=UTC)
SUNSET = datetime(2026, 6, 28, 21, 0, tzinfo=UTC)
SUNRISE = datetime(2026, 6, 29, 5, 0, tzinfo=UTC)
SUNSET2 = datetime(2026, 6, 29, 21, 0, tzinfo=UTC)

SUN_TIMES = (SUNSET, SUNRISE, SUNSET2)
TODAY_ARRAYS = [(1.0, None)]
TOMORROW_ARRAYS = [(8.0, None)]


class _SpreadModel:
    """Quantile-aware load model: P80=900 W, P50=300 W.

    The 300 W / 900 W split is chosen to produce clearly distinct reserves,
    so the test can distinguish which quantile was passed to build_intervals.
    """

    def predict_load_w(self, when, temp, fallback_w, quantile=0.5):
        return 900.0 if quantile > 0.5 else 300.0


def _cfg(**kw) -> Config:
    d = dict(
        capacity_kwh=10.0,
        soc_floor=5.0,
        soc_target=97.0,
        eta_charge=1.0,
        round_trip_eff=1.0,
    )
    d.update(kw)
    return Config(**d)


def _call_compute_decision(predictor):
    """Call compute_decision via the primary reserve path (sun_times + arrays)."""
    cfg = _cfg()
    plan = PlanState.initial(NOW - timedelta(hours=1))
    inputs = PlantInputs(soc=80.0, meter_w=0.0, now=NOW)
    slots = [PriceSlot(NOW + timedelta(hours=i), 0.40) for i in range(24)]
    return controller.compute_decision(
        plan,
        inputs,
        slots,
        pv_remaining=0.0,
        sunset=SUNSET,
        predictor=predictor,
        cur_temp=None,
        cfg=cfg,
        sun_times=SUN_TIMES,
        today_arrays=TODAY_ARRAYS,
        tomorrow_arrays=TOMORROW_ARRAYS,
    )


def test_intervals_reserve_uses_p50_load_for_overnight_hours():
    """intervals_reserve overnight hours carry P50 load (300 W), not P80 (900 W).

    RED on old code (P80 quantile → 900 W).
    GREEN after fix (P50 quantile → 300 W).
    """
    predictor = LoadPredictor.from_model(_SpreadModel())
    *_, intervals_reserve = _call_compute_decision(predictor)

    overnight = [iv for iv in intervals_reserve if iv.pv_w == 0.0]
    assert overnight, "Expected overnight (pv_w=0) intervals in intervals_reserve"

    for iv in overnight:
        assert iv.load_w == pytest.approx(300.0), (
            f"Expected P50 load_w=300.0 at {iv.start}, got {iv.load_w} (P80 code returns 900.0 — fix not applied)"
        )


def test_p50_reserve_kwh_strictly_lower_than_p80():
    """P50-sized ride-out reserve is strictly lower than P80-sized.

    Documents the behavioral intent: P50 reserve frees ~2 kWh for peak export.
    This test is quantile-agnostic to the controller change and stays GREEN on
    both old and new code — it validates the energy layer logic.
    """
    cfg = _cfg()
    curve = parsers.build_two_day_pv_curve(TODAY_ARRAYS, TOMORROW_ARRAYS, NOW, *SUN_TIMES)
    assert curve, "Expected non-empty PV curve for primary reserve path"

    predictor = LoadPredictor.from_model(_SpreadModel())
    ivs_p50 = build_intervals(curve, predictor, 400.0, cfg, quantile=0.5)
    ivs_p80 = build_intervals(curve, predictor, 400.0, cfg, quantile=0.8)

    rsv_p50 = energy.ride_out_reserve_kwh(NOW, ivs_p50, cfg)
    rsv_p80 = energy.ride_out_reserve_kwh(NOW, ivs_p80, cfg)

    assert rsv_p50 < rsv_p80, (
        f"P50 reserve ({rsv_p50:.3f} kWh) must be strictly lower than P80 reserve ({rsv_p80:.3f} kWh)"
    )


def test_dp_optimizes_on_p50_load(monkeypatch):
    """The co-optimizing DP must run its trajectory on P50 (expected) load, not P80.

    The DP optimizes charge+export against `window_load`. Built from P80 it makes
    the DP hold the expected surplus (over-valuing keeping energy for worst-case
    overnight load) so it never exports down to the (P50) reserve. Building it from
    P50 frees the surplus for the evening peak. Survival is unaffected: the firmware
    5% floor, the P50 export reserve + live executor clamp, and the P80 heuristic
    fallback all guard it independently of `window_load`.

    Spies on optimize_grid's load arg. P50 -> ~0.3 kWh/h overnight; P80 -> ~0.9.
    RED on old code (0.9 P80), GREEN after the repoint to window_load_reserve.
    """
    captured: dict = {}
    real = controller.optimize_mod.optimize_grid

    def spy(window_pv, window_load, window_price, **kw):
        captured["window_load"] = list(window_load)
        return real(window_pv, window_load, window_price, **kw)

    monkeypatch.setattr(controller.optimize_mod, "optimize_grid", spy)

    predictor = LoadPredictor.from_model(_SpreadModel())
    _call_compute_decision(predictor)

    wl = captured.get("window_load")
    assert wl, "optimize_grid was not called by compute_decision"
    # _SpreadModel returns 300 W (P50) / 900 W (P80) for every hour -> bucketed
    # window_load is ~0.3 kWh/h at P50, ~0.9 at P80. No entry may be P80-sized.
    assert max(wl) < 0.5, (
        f"DP window_load must be P50 (~0.3 kWh/h), got max={max(wl):.2f} "
        f"(P80=0.9 -> repoint to window_load_reserve not applied): {wl}"
    )


def test_reserve_is_p50_and_display_is_p50_only(monkeypatch):
    """Task 2 (P80-survival-removal): both the display and reserve intervals are now P50.

    The separate P80 deficit build (build_display_intervals at P80 quantile)
    was deleted in Task 2.  Only the P50 display build remains.  The reserve builder
    (controller.build_intervals) stays P50 as before.

    Spies on both builders to verify quantiles:
    - build_intervals (reserve): all 0.5
    - build_display_intervals (display): only 0.5 (P80 build deleted)
    """
    reserve_q: list = []  # quantiles passed to the reserve builder
    display_q: list = []  # quantiles passed to build_display_intervals

    real_build = controller.build_intervals
    real_display = controller.plan_mod.build_display_intervals

    def spy_build(*a, **kw):
        reserve_q.append(kw.get("quantile"))
        return real_build(*a, **kw)

    def spy_display(*a, **kw):
        display_q.append(kw.get("quantile"))
        return real_display(*a, **kw)

    # f9c68a3 moved _build_reserve_by_hour (and compute_decision) into decision.py;
    # its call to build_intervals now resolves against decision.py's own globals, so
    # patching the controller re-export was a silent no-op (same pattern as C2's
    # comment in tests/test_controller_dp.py).
    import custom_components.anker_x1_smartgrid.decision as decision_mod

    monkeypatch.setattr(decision_mod, "build_intervals", spy_build)
    monkeypatch.setattr(controller.plan_mod, "build_display_intervals", spy_display)

    predictor = LoadPredictor.from_model(_SpreadModel())
    _call_compute_decision(predictor)

    # Reserve built P50.
    assert reserve_q and all(q == 0.5 for q in reserve_q), f"reserve builder quantiles={reserve_q}, expected all 0.5"
    # P80 deficit build deleted (Task 2): quantile 0.8 must NOT appear in display calls.
    # (build_display_horizon also calls build_display_intervals internally with no kwarg
    #  → captured as None; that is an expected internal call, not a deficit build.)
    assert 0.8 not in display_q, (
        f"P80 deficit build must be removed; found 0.8 in build_display_intervals quantiles={display_q}"
    )
