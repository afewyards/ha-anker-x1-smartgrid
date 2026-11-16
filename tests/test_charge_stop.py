"""The DP plans no grid charge past the SoC where the executor stops charging."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.anker_x1_smartgrid.decision import compute_decision
from custom_components.anker_x1_smartgrid.efficiency import EfficiencyCurve
from custom_components.anker_x1_smartgrid.models import (
    Config,
    ControllerState,
    PlantInputs,
    PlanState,
    PriceSlot,
)

NOW = datetime(2026, 11, 16, 2, 0, tzinfo=UTC)
CFG = Config.from_dict(
    {
        "capacity_kwh": 20.0,
        "soc_target": 100.0,
        "max_charge_w": 12000.0,
        "low_power_hold": True,
        "idle_drain_w": 100.0,
        "cycle_cost_eur_per_kwh": 0.03,
        "min_dwell_min": 0,
    }
)


class _Heavy:
    def predict(self, start, temp, fallback, quantile=0.5):
        return 1500.0


def _run(soc: float) -> dict:
    # A cheap night before a dear day and a dear estimated night: the whole pack gets used,
    # so topping it up tonight pays.
    prices = [0.20] * 16 + [0.45] * 32
    out: dict = {}
    compute_decision(
        PlanState(ControllerState.PASSIVE, NOW - timedelta(hours=1), ()),
        PlantInputs(soc=soc, meter_w=1500.0, now=NOW),
        [PriceSlot(NOW + timedelta(minutes=15 * i), p) for i, p in enumerate(prices)],
        0.0,
        NOW + timedelta(hours=12),
        _Heavy(),
        None,
        CFG,
        _out=out,
        eta_curve=EfficiencyCurve.static(CFG),
        slot_minutes=15,
        estimated_tomorrow=[0.45] * 24,
    )
    return out


def test_at_the_charge_stop_the_plan_holds_instead_of_topping_up():
    out = _run(99.0)
    assert out["grid_request"] == {}
    assert NOW in out["held_slots"]


def test_below_the_charge_stop_the_plan_fills_only_to_it():
    bought_dc = sum(_run(98.0)["grid_request"].values()) * 0.25 / 1000.0 * CFG.eta_charge
    assert bought_dc == pytest.approx(CFG.pct_to_kwh(1.0), abs=0.05)


def test_grid_charging_stops_where_the_executor_does():
    assert max(_run(98.0)["ceiling_by_hour"].values()) == pytest.approx(CFG.soc_target - 1.0)
