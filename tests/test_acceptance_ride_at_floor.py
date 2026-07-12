"""Acceptance test §6: real DP idles at the firmware floor under all-expensive prices.

§6: a real (un-mocked) optimize_grid at soc_start=floor=5% with an all-above-ceiling
    price window returns a zero schedule — proving the economic-only invariant holds via
    the DP itself, not via the now-removed survival shield.

This is a characterisation / acceptance test: the production code (Tasks 1–5) already
implements the behaviour.  The test locks the invariant so a regression cannot silently
re-introduce forced charging.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, UTC

from custom_components.anker_x1_smartgrid.controller import compute_decision
from custom_components.anker_x1_smartgrid.forecast import LoadPredictor
from custom_components.anker_x1_smartgrid.models import (
    Config,
    ControllerState,
    PlantInputs,
    PlanState,
    PriceSlot,
)
from custom_components.anker_x1_smartgrid.optimize import optimize_grid

BASE = datetime(2026, 6, 25, 7, 0, tzinfo=UTC)
_ALL_EXPENSIVE_PRICE = 0.90  # €/kWh — far above any sane gate ceiling
_PREDICTOR = LoadPredictor.from_profile({})


def _cfg(**overrides) -> Config:
    base = dict(
        capacity_kwh=10.0,
        soc_floor=5.0,
        soc_target=97.0,
        max_charge_w=6000.0,
        eta_charge=0.92,
        round_trip_eff=0.85,
    )
    base.update(overrides)
    return Config(**base)


def _slots(prices: list[float]) -> list[PriceSlot]:
    return [PriceSlot(BASE + timedelta(hours=i), p) for i, p in enumerate(prices)]


def _plan(state: ControllerState = ControllerState.PASSIVE) -> PlanState:
    return PlanState(state, BASE - timedelta(hours=2), ())


def test_real_dp_rides_at_floor_returns_zero_schedule():
    """§6: soc_start=5%, soc_floor=5%, all-above-ceiling window → zero schedule.

    ``chargeable=[False]*N`` is the price-gate mask the controller passes when no
    hour is worthy (every hour above the gate ceiling).  The DP's per-hour mask
    zeroes max_grid_dc, so every charging transition out of the floor state is
    pruned and the optimal plan is to idle — sum(schedule) == 0.
    """
    n = 8
    pv = [0.0] * n
    load = [300.0] * n  # light drain; firmware holds at 5%
    price = [0.90] * n  # all far above any sane gate ceiling
    chargeable = [False] * n  # price-gate mask: nothing worthy
    result = optimize_grid(
        pv,
        load,
        price,
        soc_start=5.0,
        cfg=_cfg(),
        window_start_h=0,
        window_len=n,
        chargeable=chargeable,
        feed_in=None,
        terminal_mode="reserve",
        water_value=None,
    )
    assert sum(result["schedule"]) == 0.0, f"Expected zero schedule (idle at floor), got {result['schedule']}"


def test_compute_decision_passive_at_floor_all_expensive():
    """§6 controller-layer: soc=5% (floor) + all-expensive window → PASSIVE, setpoint=0.

    Verifies the invariant holds at the compute_decision level (not just optimize_grid).
    Mirrors test_acceptance_subfloor_infeasible.py §7 pattern.
    """
    cfg = _cfg(soc_floor=5.0)
    slots = _slots([_ALL_EXPENSIVE_PRICE] * 9)
    sunset = BASE + timedelta(hours=8)
    inputs = PlantInputs(soc=5.0, meter_w=0.0, now=BASE)
    _out: dict = {}

    new_plan, setpoint, *_ = compute_decision(
        _plan(),
        inputs,
        slots,
        0.0,
        sunset,
        _PREDICTOR,
        None,
        cfg,
        _out=_out,
    )

    assert new_plan.state is ControllerState.PASSIVE, (
        f"Expected PASSIVE at floor with all-expensive prices, got {new_plan.state}"
    )
    assert setpoint == 0.0, f"Expected setpoint=0 (no force-charge) at floor, got {setpoint}"
