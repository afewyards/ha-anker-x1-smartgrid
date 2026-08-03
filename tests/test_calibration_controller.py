"""Calibration override at the controller boundary: isolation + active path."""

import dataclasses
from datetime import timedelta

import pytest

from custom_components.anker_x1_smartgrid import calibration, controller, scheduler
from custom_components.anker_x1_smartgrid.models import ControllerState, PlanState
from tests.helpers import BASE, StubHass, make_controller, seed_valid_inputs


@pytest.mark.asyncio
async def test_disabled_never_consults_the_policy(monkeypatch):
    """calibration_enabled=False => behaviour identical to today."""
    hass = StubHass()
    ctrl, _act = make_controller(hass)
    seed_valid_inputs(hass, soc="50.0")
    called = False

    def _spy(*a, **k):
        nonlocal called
        called = True
        return None

    monkeypatch.setattr(calibration, "calibration_action", _spy)
    result = await ctrl.tick()
    assert called is False, "policy must not even be consulted when disabled"
    assert result["state"] == "passive"


@pytest.mark.asyncio
async def test_active_calibration_forces_charge(monkeypatch):
    """Active calibration flips the plan to FORCING regardless of the DP.

    soc=98 would otherwise decide PASSIVE (see
    test_controller.py::test_tick_forcing_to_passive_calls_release), so a
    FORCING outcome here can only come from the override.
    """
    hass = StubHass()
    ctrl, act = make_controller(hass)
    seed_valid_inputs(hass, soc="98.0")
    ctrl.cfg = dataclasses.replace(ctrl.cfg, calibration_enabled=True)
    action = calibration.CalibAction(
        phase="holding",
        window_start=BASE - timedelta(hours=1),
        window_end=BASE + timedelta(hours=2),
    )
    monkeypatch.setattr(calibration, "calibration_action", lambda *a, **k: action)

    result = await ctrl.tick()

    assert result["state"] == "forcing"
    assert ctrl.last_status["calibration_state"] == "holding"
    assert any(c[0] == "engage_and_charge" for c in act.calls)


@pytest.mark.asyncio
async def test_calibration_state_since_stable_across_continuing_ticks(monkeypatch):
    """C1(A): state_since must not be re-stamped while calibration continues.

    Regression for the bug where the override replaced state_since=now on
    every tick the scheduler would otherwise have bailed to PASSIVE (e.g.
    below the high-SoC guard, once min_dwell_min elapses and the hour isn't
    economically selected -- scheduler.py's `now_selected` branch). That
    branch always constructs a FRESH PlanState, so faking it unconditionally
    reproduces the worst case: every tick tries to bail, and only the
    override's own state_since handling can keep it stable.
    """
    hass = StubHass()
    ctrl, _act = make_controller(hass)
    seed_valid_inputs(hass, soc="50.0")
    ctrl.cfg = dataclasses.replace(ctrl.cfg, calibration_enabled=True)

    action = calibration.CalibAction(
        phase="charging",
        window_start=BASE,
        window_end=BASE + timedelta(hours=3),
    )
    monkeypatch.setattr(calibration, "calibration_action", lambda *a, **k: action)

    def _always_bail(plan, *, now, **kwargs):
        # Mirrors scheduler.decide_state's `... return PlanState(PASSIVE, now,
        # ())` bail (scheduler.py's dwell-elapsed-but-not-selected branch) --
        # unconditional, so it fires on every tick regardless of real timing.
        if plan.state is ControllerState.FORCING:
            return PlanState(ControllerState.PASSIVE, now, ())
        return plan

    monkeypatch.setattr(scheduler, "decide_state", _always_bail)

    tick_time = BASE
    monkeypatch.setattr(controller.dt_util, "utcnow", lambda: tick_time)

    result1 = await ctrl.tick()
    assert result1["state"] == "forcing"
    first_since = ctrl.plan.state_since
    assert first_since == BASE

    tick_time = BASE + timedelta(minutes=20)  # past default min_dwell_min=15
    result2 = await ctrl.tick()
    assert result2["state"] == "forcing"
    assert ctrl.plan.state_since == first_since, "state_since must not move while calibration continues"

    tick_time = BASE + timedelta(minutes=40)
    result3 = await ctrl.tick()
    assert result3["state"] == "forcing"
    assert ctrl.plan.state_since == first_since


@pytest.mark.asyncio
async def test_calibration_stop_cancels_coasting_forcing(monkeypatch):
    """C1(B): when calibration stops, a calibration-induced FORCING must not
    ride the scheduler's dwell short-circuit into an unauthorised charge.

    scheduler.decide_state's `if not dwell_elapsed: return plan` (and its
    `if now_selected: return plan` sibling) return the SAME PlanState object
    unchanged -- faking that unconditionally means the scheduler never
    independently re-approves FORCING, so a FORCING outcome on the third tick
    here can only be because the override failed to cancel calibration's own
    stale mandate once calibration said stop.
    """
    hass = StubHass()
    ctrl, act = make_controller(hass)
    seed_valid_inputs(hass, soc="50.0")  # below the high-SoC guard (soc_target-1 = 96)
    ctrl.cfg = dataclasses.replace(ctrl.cfg, calibration_enabled=True)

    calibration_active = True
    action = calibration.CalibAction(
        phase="charging",
        window_start=BASE,
        window_end=BASE + timedelta(hours=3),
    )

    def _fake_action(*a, **k):
        return action if calibration_active else None

    monkeypatch.setattr(calibration, "calibration_action", _fake_action)
    monkeypatch.setattr(scheduler, "decide_state", lambda plan, **kwargs: plan)  # always coast

    tick_time = BASE
    monkeypatch.setattr(controller.dt_util, "utcnow", lambda: tick_time)

    result1 = await ctrl.tick()
    assert result1["state"] == "forcing"
    since1 = ctrl.plan.state_since

    tick_time = BASE + timedelta(minutes=5)
    result2 = await ctrl.tick()
    assert result2["state"] == "forcing"
    assert ctrl.plan.state_since == since1  # still coasting, stable (bonus (A) coverage)

    calibration_active = False
    tick_time = BASE + timedelta(minutes=6)
    result3 = await ctrl.tick()

    assert result3["state"] == "passive", (
        "a calibration-induced FORCING must not survive on the scheduler's own "
        "dwell short-circuit once calibration itself has stopped"
    )
    assert ctrl.plan.state is ControllerState.PASSIVE
    assert any(c[0] == "release_to_self" for c in act.calls), (
        "actuator must be released when the calibration-induced FORCING ends"
    )
