"""TDD (C1): heuristic DP-fallback also honours the look-back trough."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from custom_components.anker_x1_smartgrid.controller import compute_decision
from custom_components.anker_x1_smartgrid.forecast import LoadPredictor
from custom_components.anker_x1_smartgrid.models import (
    Config, ControllerState, PlantInputs, PlanState, PriceSlot,
)


def _cfg(**ov):
    return Config.from_dict({
        "capacity_kwh": 10.0, "soc_target": 97.0, "eta_charge": 0.92,
        "eps_hi_kwh": 0.4, "eps_lo_kwh": 0.2, "min_dwell_min": 0,
        "max_charge_w": 500.0, "round_trip_eff": 0.85,
        **ov,
    })


def _run(cfg, lookback):
    base = datetime(2026, 6, 22, 0, 0, tzinfo=timezone.utc)
    # cheap noon trough 0.13 (past), expensive evening 'now' 0.25, peak 0.42 later.
    prices = {9: 0.20, 10: 0.15, 11: 0.13, 12: 0.13, 13: 0.15, 14: 0.19,
              15: 0.25, 16: 0.28, 17: 0.34, 18: 0.42}
    slots = [PriceSlot(base.replace(hour=h), p) for h, p in prices.items()]
    now = base.replace(hour=15)
    inputs = PlantInputs(soc=20.0, phase_import_w=(0.0, 0.0, 0.0), now=now)
    sunset = now + timedelta(hours=4)
    plan = PlanState.initial(now - timedelta(hours=2))
    predictor = LoadPredictor.from_profile({})
    with patch(
        "custom_components.anker_x1_smartgrid.optimize.optimize_grid",
        side_effect=RuntimeError("forced DP fallback for test"),
    ):
        new_plan, *_ = compute_decision(plan, inputs, slots, 0.0, sunset, predictor, None, cfg)
    return new_plan


def test_lookback_blocks_evening_topup_on_fallback():
    # With look-back, the 0.25 evening current hour is judged vs the past 0.13 trough → PASSIVE.
    new_plan = _run(_cfg(charge_trough_lookback_h=8), 8)
    assert new_plan.state is ControllerState.PASSIVE


def test_lookback_zero_fallback_is_passive():
    # Task 2 (P80-survival-removal): heuristic charge-slot selection deleted.
    # On DP exception, selected=[] → PASSIVE regardless of charge_trough_lookback_h.
    # (Previously: look-back off → forward-window trough 0.25 itself → FORCING. Bug now moot.)
    new_plan = _run(_cfg(charge_trough_lookback_h=0), 0)
    assert new_plan.state is ControllerState.PASSIVE
