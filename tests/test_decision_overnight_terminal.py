"""Task 5 — overnight terminal credit live wiring in ``compute_decision``.

The DP horizon always ends at ``horizon_edge`` (last priced slot + 1h), so an
unpriced post-horizon night exists on essentially every plan.  When
``cfg.terminal_overnight_credit`` is ON, ``compute_decision`` builds
``(water_value_hi, overnight_need_kwh)`` from the persistence price estimate for
the gap ``[horizon_edge, next solar pickup)`` and threads them through
``_dp_select_slots -> optimize_grid -> select_end_state`` so end-of-horizon
energy that serves the overnight load earns the richer overnight credit.

Scenario design
---------------
``now = 14:00`` gives a price horizon that crosses midnight (ends ~03:00).  The
degraded-data synthetic reserve extension is then suppressed (the real horizon
already runs past midnight), so the ride-out reserve stays low and does NOT
mask the soft terminal credit — the credit becomes the binding lever, exactly
the pre-publication regime the feature targets.  Behavioural scenarios read the
DP's chosen terminal SoC directly via a ``select_end_state`` spy, which bypasses
display-SoC clamping and grid-charge tie-break noise.

Scenarios (spec "Call sites" #1 + "Testing"):
  * threading pin — the built params reach the builder, ``optimize_grid`` + ``_out``
  * expensive overnight estimate → DP holds for the night (OFF over-liquidates)
  * cheap overnight estimate      → v_hi collapses to v_lo → liquidates like OFF
  * tall evening peak over a cheap-ish est → burst still fires (F1 over-hold band)
  * flag explicitly False         → water_value_hi=None → byte-identical legacy
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.util import dt as dt_util

from custom_components.anker_x1_smartgrid import optimize as optimize_mod
from custom_components.anker_x1_smartgrid import pricing_store
from custom_components.anker_x1_smartgrid.decision import _next_synthetic_pickup, compute_decision
from custom_components.anker_x1_smartgrid.forecast import LoadPredictor
from custom_components.anker_x1_smartgrid.models import (
    Config,
    ControllerState,
    PlantInputs,
    PlanState,
    PriceSlot,
)

BASE = datetime(2026, 7, 18, 14, 0, tzinfo=UTC)  # 14:00 UTC → horizon crosses midnight
_PREDICTOR = LoadPredictor.from_profile({})

# 13 hourly slots (14:00..02:00 next day): shoulders, an evening export staircase
# with a tall 22:00 peak (the best in-window export = v_hi clamp), then a cheap
# after-midnight tail (sets a low v_lo).
#   idx  0    1    2    3   | 4    5    6    7    8   | 9    10   11   12
#   hr   14   15   16   17  | 18   19   20   21   22  | 23   00   01   02
_PRICES = [0.20, 0.20, 0.20, 0.20, 0.28, 0.30, 0.32, 0.40, 0.45, 0.20, 0.18, 0.15, 0.13]
_PEAK_HOUR = BASE + timedelta(hours=8)  # 22:00, the 0.45 slot
_HORIZON_EDGE = BASE + timedelta(hours=13)  # 03:00 next day (last slot 02:00 + 1h)

# Gap UTC hours = [03:00, 08:00) (horizon_edge → synthetic pickup at 08:00 UTC).
_GAP_UTC_HOURS = (3, 4, 5, 6, 7)


def _expensive_estimate() -> list[float]:
    """24-entry hour-of-LOCAL-day estimate: expensive + gently sloped over the gap
    hours, cheap elsewhere.

    ``build_estimated_slots`` indexes the estimate by ``as_local(h).hour``, so the
    ramp is keyed to the LOCAL hour each gap UTC hour maps to under the harness
    default timezone (built at call time, not import time).  The downward slope
    keeps the ride-out walk from an early is_cheap break so ``need`` spans the
    night (a flat estimate looks "cheap within its own band" → breaks at h+1).
    """
    est = [0.10] * 24
    ramp = [0.40, 0.40, 0.40, 0.40, 0.30]  # last gap hour is the cheap break
    for utc_h, price in zip(_GAP_UTC_HOURS, ramp):
        local_h = dt_util.as_local(datetime(2026, 7, 19, utc_h, 0, tzinfo=UTC)).hour
        est[local_h] = price
    return est


def _cfg(**overrides) -> Config:
    return Config.from_dict(
        {
            "capacity_kwh": 10.0,
            "soc_target": 97.0,
            "soc_floor": 5.0,  # floor_kwh == firmware_floor_kwh (0.5) → cheap-est parity
            "eta_charge": 0.92,
            "round_trip_eff": 0.85,
            "min_dwell_min": 0,
            "max_charge_w": 6000.0,
            "enable_export": True,
            "export_fee_eur_per_kwh": 0.02,
            "export_min_block_kwh": 0.0,  # disable the sub-block filter (deterministic)
            "cycle_cost_eur_per_kwh": 0.10,
            **overrides,
        }
    )


def _slots(prices: list[float]) -> list[PriceSlot]:
    return [PriceSlot(BASE + timedelta(hours=i), p) for i, p in enumerate(prices)]


def _plan() -> PlanState:
    return PlanState(ControllerState.PASSIVE, BASE - timedelta(hours=2), ())


def _call(cfg: Config, *, soc: float, prices: list[float], estimated_tomorrow=None, out=None, export_price=0.30):
    inputs = PlantInputs(soc=soc, meter_w=0.0, now=BASE)
    sunset = BASE + timedelta(hours=len(prices))
    return compute_decision(
        _plan(),
        inputs,
        _slots(prices),
        0.0,  # pv_remaining → no solar pickup → synthetic overnight gap
        sunset,
        _PREDICTOR,
        None,
        cfg,
        export_price=export_price,
        export_price_matches_import=True,
        estimated_tomorrow=estimated_tomorrow,
        _out=out,
    )


def _end_state_kwh(cfg: Config, *, soc: float, prices: list[float], estimated_tomorrow=None) -> tuple[float, dict]:
    """Run compute_decision and return the DP's chosen terminal SoC (DC kWh).

    Spies on ``optimize.select_end_state`` — invoked once by the live DP — to read
    ``from_bin(best_end_b)`` directly, bypassing the reconstructed/clamped display
    horizon.  Returns ``(end_kwh, _out)``.
    """
    real = optimize_mod.select_end_state
    captured: list[float] = []

    def _spy(*args, **kwargs):
        result = real(*args, **kwargs)
        captured.append(kwargs["from_bin"](result[0]))
        return result

    optimize_mod.select_end_state = _spy
    try:
        out: dict = {}
        _call(cfg, soc=soc, prices=prices, estimated_tomorrow=estimated_tomorrow, out=out)
    finally:
        optimize_mod.select_end_state = real
    assert captured, "the live DP must call select_end_state exactly once"
    return captured[-1], out


# ---------------------------------------------------------------------------
# 1. Threading pins: built params reach the builder, optimize_grid + _out
# ---------------------------------------------------------------------------


def test_wiring_threads_params_to_optimize_grid_and_out(monkeypatch):
    """The (v_hi, need) built in the wv block flow to optimize_grid and _out.

    Spies replace the builder (returns a sentinel) and ``optimize_grid`` (records
    the pass-through kwargs), so the test pins the wiring — arguments handed to the
    builder, and the sentinel forwarded downstream — without depending on the DP's
    internal end-state arithmetic (covered by the Task 3 helper tests).
    """
    captured_builder: dict = {}
    captured_dp: dict = {}

    def _spy_builder(*, gap_start, pickup, est_price_by_hour, load_w_by_hod, v_lo, max_export_dc_value, cfg, eta_curve):
        captured_builder.update(
            gap_start=gap_start,
            pickup=pickup,
            est_price_by_hour=est_price_by_hour,
            load_w_by_hod=load_w_by_hod,
            v_lo=v_lo,
            max_export_dc_value=max_export_dc_value,
            eta_curve=eta_curve,
        )
        return (0.99, 3.33)

    def _spy_dp(*args, **kwargs):
        captured_dp.update(
            water_value_hi=kwargs.get("water_value_hi"),
            overnight_need_kwh=kwargs.get("overnight_need_kwh"),
        )
        wl = kwargs["window_len"]
        return {
            "schedule": [0.0] * wl,
            "kwh": 0.0,
            "eur": 0.0,
            "export_schedule": [0.0] * wl,
            "export_kwh": 0.0,
            "export_revenue_eur": 0.0,
            "infeasible": False,
        }

    monkeypatch.setattr(optimize_mod, "overnight_terminal_params", _spy_builder)
    monkeypatch.setattr(optimize_mod, "optimize_grid", _spy_dp)

    cfg = _cfg()
    est = _expensive_estimate()
    out: dict = {}
    _call(cfg, soc=80.0, prices=_PRICES, estimated_tomorrow=est, out=out)

    # gap = [horizon_edge, synthetic pickup)
    assert captured_builder["gap_start"] == _HORIZON_EDGE
    assert captured_builder["pickup"] == _next_synthetic_pickup(_HORIZON_EDGE)
    # v_lo is the horizon-min water value.
    assert captured_builder["v_lo"] == pytest.approx(optimize_mod.compute_water_value(min(_PRICES), cfg))
    # est_price_by_hour == build_estimated_slots over the gap, keyed by start.
    expected_est = {
        s.start: s.price
        for s in pricing_store.build_estimated_slots(est, _HORIZON_EDGE, captured_builder["pickup"])
    }
    assert captured_builder["est_price_by_hour"] == expected_est
    assert expected_est, "gap must be priced by the estimate (non-empty)"
    assert captured_builder["eta_curve"] is None

    # sentinel forwarded downstream + stashed.
    assert captured_dp["water_value_hi"] == 0.99
    assert captured_dp["overnight_need_kwh"] == 3.33
    assert out["terminal_v_hi"] == 0.99
    assert out["terminal_need_kwh"] == 3.33


def test_max_export_dc_value_is_best_in_window_export(monkeypatch):
    """The upper clamp handed to the builder = max_h(eff_export·η_d) − cycle_cost."""
    captured: dict = {}

    def _spy_builder(*, max_export_dc_value, v_lo, **_):
        captured["max_export_dc_value"] = max_export_dc_value
        captured["v_lo"] = v_lo
        return (v_lo, 0.0)

    monkeypatch.setattr(optimize_mod, "overnight_terminal_params", _spy_builder)
    cfg = _cfg()
    _call(cfg, soc=80.0, prices=_PRICES, estimated_tomorrow=_expensive_estimate(), out={})

    eta_d = cfg.eta_discharge_static()
    best_eff = max(optimize_mod.effective_export_price(p, cfg) for p in _PRICES)  # export == import
    assert captured["max_export_dc_value"] == pytest.approx(best_eff * eta_d - cfg.cycle_cost_eur_per_kwh)


def test_export_off_clamps_max_export_at_v_lo(monkeypatch):
    """Export disabled → the upper clamp degrades to v_lo (the refill anchor)."""
    captured: dict = {}

    def _spy_builder(*, max_export_dc_value, v_lo, **_):
        captured["max_export_dc_value"] = max_export_dc_value
        captured["v_lo"] = v_lo
        return (v_lo, 0.0)

    monkeypatch.setattr(optimize_mod, "overnight_terminal_params", _spy_builder)
    cfg = _cfg()
    _call(cfg, soc=80.0, prices=_PRICES, estimated_tomorrow=_expensive_estimate(), out={}, export_price=None)
    assert captured["max_export_dc_value"] == pytest.approx(captured["v_lo"])


# ---------------------------------------------------------------------------
# 2. Expensive overnight estimate → DP holds for the night
# ---------------------------------------------------------------------------


def test_truncated_morning_holds_overnight_need():
    """An expensive overnight estimate makes the DP hold energy it would else burst.

    Flag OFF over-liquidates to the firmware floor (the pre-publication pathology);
    flag ON, valuing the held energy at the overnight v_hi, ends materially higher.
    """
    cfg_on = _cfg()  # terminal_overnight_credit defaults ON
    cfg_off = _cfg(terminal_overnight_credit=False)

    end_on, out_on = _end_state_kwh(cfg_on, soc=90.0, prices=_PRICES, estimated_tomorrow=_expensive_estimate())
    end_off, _ = _end_state_kwh(cfg_off, soc=90.0, prices=_PRICES, estimated_tomorrow=_expensive_estimate())

    v_lo = optimize_mod.compute_water_value(min(_PRICES), cfg_on)
    assert out_on["terminal_v_hi"] is not None
    assert out_on["terminal_v_hi"] > v_lo  # credit is active (richer than the refill anchor)
    assert out_on["terminal_need_kwh"] > 0.0
    # ON retains energy for the night; OFF liquidates to (near) the firmware floor.
    assert end_on > end_off
    assert end_on >= cfg_on.firmware_floor_kwh + out_on["terminal_need_kwh"] - 1e-6


# ---------------------------------------------------------------------------
# 3. Cheap overnight estimate → v_hi collapses to v_lo → liquidates like OFF
# ---------------------------------------------------------------------------


def test_cheap_overnight_estimate_keeps_burst():
    """A cheap overnight estimate → v_hi == v_lo → same liquidation as flag-off."""
    cfg_on = _cfg()
    cfg_off = _cfg(terminal_overnight_credit=False)
    est_cheap = [0.10] * 24  # below v_lo after wear → v_hi floors at v_lo

    end_on, out_on = _end_state_kwh(cfg_on, soc=90.0, prices=_PRICES, estimated_tomorrow=est_cheap)
    end_off, out_off = _end_state_kwh(cfg_off, soc=90.0, prices=_PRICES, estimated_tomorrow=est_cheap)

    v_lo = optimize_mod.compute_water_value(min(_PRICES), cfg_on)
    assert out_on["terminal_v_hi"] == pytest.approx(v_lo)
    # v_hi == v_lo and floor_kwh == firmware_floor_kwh → the two-segment credit
    # collapses to the legacy single-rate formula → identical terminal + schedule.
    assert end_on == pytest.approx(end_off)
    assert out_on["export_request"] == out_off["export_request"]


# ---------------------------------------------------------------------------
# 4. Tall evening peak over a cheap-ish overnight estimate → burst still fires
# ---------------------------------------------------------------------------


def test_tall_peak_over_cheap_overnight_still_bursts():
    """Guards the F1 over-hold band: ep_eff > gap ⇒ export wins despite the credit.

    Overnight estimate 0.28 is decent, but the 22:00 peak (0.45) clears it after
    wear (``ep_eff·η_d − cc > v_hi``); the wear-symmetric v_hi must not suppress
    that genuinely-profitable burst.
    """
    cfg = _cfg()
    out: dict = {}
    _call(cfg, soc=90.0, prices=_PRICES, estimated_tomorrow=[0.28] * 24, out=out)

    assert _PEAK_HOUR in out.get("export_request", {}), (
        f"tall 0.45 peak must still export; got {sorted(out.get('export_request', {}))}"
    )


# ---------------------------------------------------------------------------
# 5. Flag explicitly False → byte-identical legacy terminal
# ---------------------------------------------------------------------------


def test_flag_off_byte_identical(monkeypatch):
    """Flag False → water_value_hi=None, builder never called, legacy schedule."""
    calls: list = []
    real_builder = optimize_mod.overnight_terminal_params

    def _tracking_builder(*a, **k):
        calls.append(1)
        return real_builder(*a, **k)

    monkeypatch.setattr(optimize_mod, "overnight_terminal_params", _tracking_builder)

    cfg = _cfg(terminal_overnight_credit=False)
    out_est: dict = {}
    out_none: dict = {}
    _call(cfg, soc=90.0, prices=_PRICES, estimated_tomorrow=_expensive_estimate(), out=out_est)
    _call(cfg, soc=90.0, prices=_PRICES, estimated_tomorrow=None, out=out_none)

    assert calls == [], "builder must not run when the flag is OFF"
    assert out_est["terminal_v_hi"] is None
    assert out_est["terminal_need_kwh"] == 0.0
    # The estimate must not leak into the DP result when the flag is OFF.
    assert out_est["export_request"] == out_none["export_request"]
    assert out_est["grid_request"] == out_none["grid_request"]
    assert out_est["dp_selected"] == out_none["dp_selected"]
