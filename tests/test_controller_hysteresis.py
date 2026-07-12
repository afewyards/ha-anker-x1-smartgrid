"""Tests for edge hysteresis on the committed current-hour charge (C3b)."""

from datetime import datetime, timedelta, timezone, UTC
from unittest.mock import patch

import pytest

from custom_components.anker_x1_smartgrid import controller as ctrl
from custom_components.anker_x1_smartgrid.models import (
    Config,
    ControllerState,
    PlanState,
    PlantInputs,
    PriceSlot,
)


class _FlatPredictor:
    def predict(self, start, temp, fallback, quantile=0.5):
        return 300.0


def _slots(now, prices):
    base = now.replace(minute=0, second=0, microsecond=0)
    return [PriceSlot(base + timedelta(hours=i), p) for i, p in enumerate(prices)]


def _dp_zero_charge_committed_export(*args, **kwargs):
    """Mock optimize_grid: 0 charge everywhere, current slot committed to export."""
    wl = kwargs.get("window_len", len(args[0]) if args else 1)
    return {
        "schedule": [0.0] * wl,
        "export_schedule": [1.0] + [0.0] * (wl - 1),
        "kwh": 0.0,
        "eur": 0.0,
    }


def _dp_zero_charge(*args, **kwargs):
    """Mock optimize_grid: 0 charge, 0 export everywhere (price mask zeroed all slots)."""
    wl = kwargs.get("window_len", len(args[0]) if args else 1)
    return {
        "schedule": [0.0] * wl,
        "export_schedule": [0.0] * wl,
        "kwh": 0.0,
        "eur": 0.0,
    }


def test_current_hour_decision_held_within_deadband():
    now = datetime(2026, 6, 23, 18, 0, tzinfo=UTC)
    prices = [0.30] * 8 + [0.08] + [0.30] * 6
    slots = _slots(now, prices)
    plan = PlanState(ControllerState.PASSIVE, now, (), committed_charge_kwh=0.0)
    # Fresh DP wants a small nonzero current-hour charge INSIDE the deadband; the
    # hold must keep the prior commit (0.0), not adopt the fresh 0.15 delta.
    with patch(
        "custom_components.anker_x1_smartgrid.optimize.optimize_grid",
        return_value={"schedule": [0.15] + [0.0] * 14, "kwh": 0.15, "eur": 0.0, "export_schedule": [0.0] * 15},
    ):
        new_plan, *_ = ctrl.compute_decision(
            plan=plan,
            inputs=PlantInputs(soc=88.0, meter_w=0.0, now=now),
            slots=slots,
            pv_remaining=0.0,
            sunset=now + timedelta(hours=2),
            predictor=_FlatPredictor(),
            cur_temp=10.0,
            cfg=Config(end_soc_deadband=0.25, min_dwell_min=0),
            _out={},
        )
    assert new_plan.committed_charge_kwh == pytest.approx(0.0)  # held prior, not 0.15


def test_committed_export_hour_not_force_charged():
    """Edge-hysteresis re-injection must not override a genuine committed export.

    Reproduces the live "W"-shaped SoC oscillation: at an evening price peak the
    fresh DP charges 0 kWh for the current hour (the price mask already blocks
    it) and commits a real export for that same hour, but a small stale
    ``prev_cur_kwh`` (within ``end_soc_deadband`` of the fresh 0) makes the
    edge-hysteresis block re-inject the current hour back into ``selected`` —
    which, without the anti-fight guard, forces ``decide_state`` into FORCING
    and grid-charges straight through the committed export hour.

    Review 1.3: a commit now only seeds the deadband compare when it belongs to
    the CURRENT slot, so this test binds ``committed_charge_slot`` to ``now``
    (intra-slot) — otherwise the re-injection this test exists to guard against
    would never fire and the anti-fight guard below would go untested.
    """
    now = datetime(2026, 6, 23, 18, 0, tzinfo=UTC)
    prices = [0.30] * 8 + [0.08] + [0.30] * 6
    slots = _slots(now, prices)
    # Stale previous-tick commitment: small, but within end_soc_deadband (0.25)
    # of the fresh DP's 0 kWh charge — triggers the re-injection branch alone.
    # committed_charge_slot == now (== cur_h, on-the-hour) keeps this intra-slot.
    plan = PlanState(
        ControllerState.PASSIVE,
        now - timedelta(hours=2),
        (),
        committed_charge_kwh=0.1,
        committed_charge_slot=now,
    )

    with patch(
        "custom_components.anker_x1_smartgrid.optimize.optimize_grid",
        side_effect=_dp_zero_charge_committed_export,
    ):
        new_plan, setpoint, *_ = ctrl.compute_decision(
            plan=plan,
            inputs=PlantInputs(soc=30.0, meter_w=0.0, now=now),
            slots=slots,
            pv_remaining=0.0,
            sunset=now + timedelta(hours=2),
            predictor=_FlatPredictor(),
            cur_temp=10.0,
            cfg=Config(end_soc_deadband=0.25, min_dwell_min=0),
        )

    # The committed export must win: no force-charge through the export hour.
    assert new_plan.state is not ControllerState.FORCING
    assert setpoint == 0.0
    assert new_plan.committed_charge_kwh == 0.0


def test_stale_commit_not_reinjected_across_slot_boundary():
    """Review 1.3 (the fix): a commit belongs to ONE slot. A stale commit
    carried over from the PREVIOUS slot must not seed the deadband compare —
    it must not re-inject the current hour into ``selected`` and force
    ``decide_state`` into FORCING, bypassing the DP's fresh price mask.
    """
    now = datetime(2026, 6, 23, 18, 0, tzinfo=UTC)
    prices = [0.30] * 8 + [0.08] + [0.30] * 6
    slots = _slots(now, prices)
    cur_h = now
    prev_slot = cur_h - timedelta(hours=1)
    # Stale commit: belongs to the PREVIOUS slot, not the current one.
    plan = PlanState(
        ControllerState.PASSIVE,
        now,
        (),
        committed_charge_kwh=0.2,
        committed_charge_slot=prev_slot,
    )

    with patch(
        "custom_components.anker_x1_smartgrid.optimize.optimize_grid",
        side_effect=_dp_zero_charge,
    ):
        new_plan, setpoint, *_ = ctrl.compute_decision(
            plan=plan,
            inputs=PlantInputs(soc=50.0, meter_w=0.0, now=now),
            slots=slots,
            pv_remaining=0.0,
            sunset=now + timedelta(hours=2),
            predictor=_FlatPredictor(),
            cur_temp=10.0,
            cfg=Config(end_soc_deadband=0.25, min_dwell_min=0),
        )

    # No re-injection: the stale cross-slot commit is dropped, decide_state
    # never sees cur_h as selected, and stays PASSIVE (not FORCING).
    assert new_plan.state is ControllerState.PASSIVE
    assert setpoint == 0.0
    assert new_plan.committed_charge_kwh == 0.0
    # The commit is now (re-)bound to the current slot for the next tick.
    assert new_plan.committed_charge_slot == cur_h


def test_stale_commit_reinjected_within_same_slot():
    """Positive control for the above: identical setup, but
    ``committed_charge_slot == cur_h`` (intra-slot) — the deadband-hold still
    re-injects the current hour, exactly as before this fix (unchanged
    behaviour within a single slot).
    """
    now = datetime(2026, 6, 23, 18, 0, tzinfo=UTC)
    prices = [0.30] * 8 + [0.08] + [0.30] * 6
    slots = _slots(now, prices)
    cur_h = now
    # Same-slot commit: belongs to the CURRENT slot.
    plan = PlanState(
        ControllerState.PASSIVE,
        now,
        (),
        committed_charge_kwh=0.2,
        committed_charge_slot=cur_h,
    )

    with patch(
        "custom_components.anker_x1_smartgrid.optimize.optimize_grid",
        side_effect=_dp_zero_charge,
    ):
        new_plan, setpoint, *_ = ctrl.compute_decision(
            plan=plan,
            inputs=PlantInputs(soc=50.0, meter_w=0.0, now=now),
            slots=slots,
            pv_remaining=0.0,
            sunset=now + timedelta(hours=2),
            predictor=_FlatPredictor(),
            cur_temp=10.0,
            cfg=Config(end_soc_deadband=0.25, min_dwell_min=0),
        )

    # Re-injection still happens intra-slot: decide_state enters FORCING.
    assert new_plan.state is ControllerState.FORCING
    assert setpoint != 0.0
    assert new_plan.committed_charge_kwh == 0.2
    assert new_plan.committed_charge_slot == cur_h
