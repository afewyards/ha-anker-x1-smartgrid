"""The DP plans no grid charge past the SoC where the executor stops charging."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.anker_x1_smartgrid import executor
from custom_components.anker_x1_smartgrid.decision import compute_decision
from custom_components.anker_x1_smartgrid.efficiency import EfficiencyCurve
from custom_components.anker_x1_smartgrid.models import (
    Config,
    ControllerState,
    ExportState,
    PlantInputs,
    PlanState,
    PriceSlot,
)
from tests.helpers import StubHass, make_controller

NOW = datetime(2026, 11, 16, 2, 0, tzinfo=UTC)
CFG = Config.from_dict(
    {
        "capacity_kwh": 20.0,
        "soc_target": 100.0,
        "max_charge_w": 12000.0,
        "idle_drain_w": 100.0,
        "cycle_cost_eur_per_kwh": 0.03,
        "min_dwell_min": 0,
    }
)


class _Heavy:
    def predict(self, start, temp, fallback, quantile=0.5):
        return 1500.0


# A cheap night before a dear day and a dear estimated night: the whole pack gets used,
# so topping it up tonight pays.
SLOTS = [PriceSlot(NOW + timedelta(minutes=15 * i), p) for i, p in enumerate([0.20] * 16 + [0.45] * 32)]


def _run(soc: float) -> dict:
    out: dict = {}
    compute_decision(
        PlanState(ControllerState.PASSIVE, NOW - timedelta(hours=1), ()),
        PlantInputs(soc=soc, meter_w=1500.0, now=NOW),
        SLOTS,
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


@pytest.mark.parametrize("soc", [99.0, 99.5, 100.0])
def test_at_or_above_the_charge_stop_the_plan_holds_instead_of_topping_up(soc):
    out = _run(soc)
    assert out["grid_request"] == {}
    assert NOW in out["held_slots"]


def test_below_the_charge_stop_the_plan_fills_only_to_it():
    bought_dc = sum(_run(98.0)["grid_request"].values()) * 0.25 / 1000.0 * CFG.eta_charge
    assert bought_dc == pytest.approx(CFG.pct_to_kwh(1.0), abs=0.05)


def test_the_hold_keeps_the_solar_ceiling_above_the_charge_stop():
    assert _run(98.0)["ceiling_by_hour"][NOW] == pytest.approx(CFG.soc_target)


@pytest.mark.parametrize("soc", [99.5, 100.0])
async def test_above_the_charge_stop_a_top_up_slot_holds_instead_of_draining(soc):
    out = _run(soc)
    # A top-up planned a tick earlier, which the charge stop does not force.
    out["grid_request"] = {NOW: 900.0}
    out["held_slots"] = out["held_slots"] - {NOW}
    ctrl, act = make_controller(StubHass())
    ctrl.cfg = CFG
    ctrl._house_load_fresh = True
    ctrl._hold_deficit_w.append((NOW, 1500.0))
    ctrl.hold_state = ctrl.export_state = ExportState(engaged=False, state_since=NOW - timedelta(hours=1))
    await executor.run_forcing_and_export(
        ctrl,
        NOW,
        PlanState(ControllerState.PASSIVE, NOW, ()),
        PlantInputs(soc=soc, meter_w=1500.0, now=NOW),
        SLOTS,
        out,
        [],
        15,
        None,
        1500.0,
    )
    assert act.calls == [("engage_hold",)]
