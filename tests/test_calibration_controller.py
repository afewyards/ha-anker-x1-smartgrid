"""Calibration override at the controller boundary: isolation + active path."""

import dataclasses
from datetime import timedelta

import pytest

from custom_components.anker_x1_smartgrid import calibration, controller, scheduler
from custom_components.anker_x1_smartgrid.models import ControllerState, PlanState
from tests.helpers import BASE, StubHass, make_controller, seed_valid_inputs


def _wrap_compute_decision(monkeypatch, now_selected_holder):
    """Patch controller.compute_decision to force _out["now_selected"] to a
    test-controlled value, otherwise delegating to the REAL function.

    now_selected is computed deep inside decision.compute_decision from the
    real DP/heuristic `selected` list -- monkeypatching scheduler.decide_state
    (as most tests in this file do) does NOT touch it, since that computation
    is independent of decide_state's own return value. Wrapping (not
    replacing) compute_decision keeps deadline/horizon/intervals_reserve and
    every other _out key fully real and valid; only now_selected is
    overridden, deterministically, after the real call already ran.
    """
    real_compute_decision = controller.compute_decision

    def _fake(*args, **kwargs):
        result = real_compute_decision(*args, **kwargs)
        _out = kwargs.get("_out")
        if _out is not None:
            _out["now_selected"] = now_selected_holder["value"]
        return result

    monkeypatch.setattr(controller, "compute_decision", _fake)


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
    stale mandate once calibration said stop. now_selected is forced False
    explicitly (via _wrap_compute_decision) rather than left to whatever the
    real DP happens to decide for this fixture, so the cancel firing is not
    hostage to incidental DP behaviour.
    """
    hass = StubHass()
    ctrl, act = make_controller(hass)
    seed_valid_inputs(hass, soc="50.0")  # below the high-SoC guard (soc_target-1 = 96)
    ctrl.cfg = dataclasses.replace(ctrl.cfg, calibration_enabled=True)

    calibration_active = True
    now_selected_holder = {"value": False}
    action = calibration.CalibAction(
        phase="charging",
        window_start=BASE,
        window_end=BASE + timedelta(hours=3),
    )

    def _fake_action(*a, **k):
        return action if calibration_active else None

    monkeypatch.setattr(calibration, "calibration_action", _fake_action)
    monkeypatch.setattr(scheduler, "decide_state", lambda plan, **kwargs: plan)  # always coast
    _wrap_compute_decision(monkeypatch, now_selected_holder)

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


@pytest.mark.asyncio
async def test_calibration_cancel_does_not_dwell_lock_economic_reentry(monkeypatch):
    """N1: the (B) cancel must not restart the PASSIVE dwell timer.

    Regression for the bug where cancelling a coasting calibration FORCING
    stamped state_since=now on the resulting PASSIVE plan. scheduler.decide_state's
    own PASSIVE->FORCING re-entry is itself dwell-gated
    (`if not dwell_elapsed: return plan`, scheduler.py:246-247), so a fresh
    `now` there would dwell-LOCK a legitimate, DP-wanted charge for up to
    min_dwell_min (~15 min) instead of freeing it after the one cancel tick.

    `now_selected_holder` is the ONE "does the DP want this hour" signal,
    fed to both the fake `decide_state` (which honours `plan.state_since` for
    the dwell check exactly as the real scheduler does -- a simplified
    stand-in, not a full reimplementation) and, via `_wrap_compute_decision`,
    the REAL `_dp_out["now_selected"]` the controller's override gate reads.
    One holder, not two independent toggles, so the two can't drift apart.
    """
    hass = StubHass()
    ctrl, _act = make_controller(hass)
    seed_valid_inputs(hass, soc="50.0")
    ctrl.cfg = dataclasses.replace(ctrl.cfg, calibration_enabled=True)

    calibration_active = True
    now_selected_holder = {"value": False}
    action = calibration.CalibAction(
        phase="charging",
        window_start=BASE,
        window_end=BASE + timedelta(hours=3),
    )

    def _fake_action(*a, **k):
        return action if calibration_active else None

    def _fake_decide_state(plan, *, now, cfg, **kwargs):
        # Simplified stand-in for scheduler.decide_state -- real enough about
        # the ONE mechanic this test cares about (dwell gated on
        # plan.state_since), not a full reimplementation.
        if plan.state is ControllerState.FORCING:
            return plan  # always coast while FORCING, as in the round-1 test
        dwell_elapsed = (now - plan.state_since) >= timedelta(minutes=cfg.min_dwell_min)
        if not dwell_elapsed:
            return plan
        if now_selected_holder["value"]:
            return PlanState(ControllerState.FORCING, now, ())
        return plan

    monkeypatch.setattr(calibration, "calibration_action", _fake_action)
    monkeypatch.setattr(scheduler, "decide_state", _fake_decide_state)
    _wrap_compute_decision(monkeypatch, now_selected_holder)

    tick_time = BASE
    monkeypatch.setattr(controller.dt_util, "utcnow", lambda: tick_time)

    # Tick 1: calibration engages.
    result1 = await ctrl.tick()
    assert result1["state"] == "forcing"

    # Tick 2, well past min_dwell_min later: calibration still active, still coasting.
    tick_time = BASE + timedelta(minutes=30)
    result2 = await ctrl.tick()
    assert result2["state"] == "forcing"

    # Tick 3: calibration stops. The DP does NOT want this hour, so the (B)
    # cancel fires -- a prerequisite for tick 4's dwell-lock check below.
    calibration_active = False
    tick_time = BASE + timedelta(minutes=31)
    result3 = await ctrl.tick()
    assert result3["state"] == "passive"  # cancelled, per C1(B)

    # Tick 4, only 30 SECONDS later: the DP NOW wants this hour. Must be free
    # to re-enter FORCING -- not dwell-blocked for another ~15 minutes.
    now_selected_holder["value"] = True
    tick_time = BASE + timedelta(minutes=31, seconds=30)
    result4 = await ctrl.tick()
    assert result4["state"] == "forcing", (
        "a calibration cancel must not dwell-lock the next tick's economic FORCING re-entry"
    )


@pytest.mark.asyncio
async def test_calibration_disengage_does_not_cancel_dp_wanted_forcing(monkeypatch):
    """N3: an economic FORCING that calibration merely coincided with must
    survive calibration disengaging, when the DP still wants this hour.

    The round-1/2 premise that only ENTRY (scheduler.py:249) constructs a
    fresh PlanState was wrong: every SUBSEQUENT tick of a still-running
    economic FORCING ALSO coasts through the identical `return plan`
    short-circuits (scheduler.py:233-234/241-242) that calibration's own
    coasting goes through -- the identity check alone cannot tell them apart.
    now_selected (threaded out of decision.compute_decision via `_out`, from
    the SAME `selected_slots` passed to decide_state this tick) is the real
    signal that closes the gap.
    """
    hass = StubHass()
    ctrl, _act = make_controller(hass)
    seed_valid_inputs(hass, soc="50.0")
    ctrl.cfg = dataclasses.replace(ctrl.cfg, calibration_enabled=True)

    calibration_active = True
    action = calibration.CalibAction(
        phase="charging",
        window_start=BASE,
        window_end=BASE + timedelta(hours=3),
    )

    def _fake_action(*a, **k):
        return action if calibration_active else None

    def _fake_decide_state(plan, *, now, **kwargs):
        # Tick 1: genuine PASSIVE->FORCING entry (mirrors scheduler.py:249),
        # a FRESH PlanState -- exactly like a real DP decision. Every later
        # tick: pure coasting (mirrors scheduler.py:233-234/241-242's
        # `return plan`) -- the ambiguous-identity case this fix closes.
        if plan.state is ControllerState.FORCING:
            return plan
        return PlanState(ControllerState.FORCING, now, ())

    now_selected_holder = {"value": True}
    monkeypatch.setattr(calibration, "calibration_action", _fake_action)
    monkeypatch.setattr(scheduler, "decide_state", _fake_decide_state)
    _wrap_compute_decision(monkeypatch, now_selected_holder)

    tick_time = BASE
    monkeypatch.setattr(controller.dt_util, "utcnow", lambda: tick_time)

    # Tick 1: the DP enters FORCING economically (fresh PlanState). Calibration
    # is ALSO active this tick, sees new_plan already FORCING, leaves it alone.
    result1 = await ctrl.tick()
    assert result1["state"] == "forcing"

    # Tick 2: calibration disengages. The scheduler only coasts (same
    # object) -- but the DP still wants this hour (now_selected=True), so the
    # cancel must NOT fire.
    calibration_active = False
    tick_time = BASE + timedelta(minutes=1)
    result2 = await ctrl.tick()

    assert result2["state"] == "forcing", "a DP-wanted FORCING must survive calibration disengaging"


@pytest.mark.asyncio
async def test_calibration_stop_still_cancels_when_dp_does_not_want_the_hour(monkeypatch):
    """N3 complement: with now_selected explicitly False, the (B) cancel must
    still fire -- the new now_selected gate must not neuter C1(B) for the
    exact case it was built for.

    test_calibration_stop_cancels_coasting_forcing already exercises this via
    the REAL DP's incidental now_selected=False in that scenario (soc=50,
    flat cheap prices, confirmed unchanged by this fix). This test pins the
    same requirement explicitly and deterministically, independent of
    whatever the real DP happens to decide.
    """
    hass = StubHass()
    ctrl, _act = make_controller(hass)
    seed_valid_inputs(hass, soc="50.0")
    ctrl.cfg = dataclasses.replace(ctrl.cfg, calibration_enabled=True)

    calibration_active = True
    action = calibration.CalibAction(
        phase="charging",
        window_start=BASE,
        window_end=BASE + timedelta(hours=3),
    )

    def _fake_action(*a, **k):
        return action if calibration_active else None

    now_selected_holder = {"value": False}
    monkeypatch.setattr(calibration, "calibration_action", _fake_action)
    monkeypatch.setattr(scheduler, "decide_state", lambda plan, **kwargs: plan)  # always coast
    _wrap_compute_decision(monkeypatch, now_selected_holder)

    tick_time = BASE
    monkeypatch.setattr(controller.dt_util, "utcnow", lambda: tick_time)

    result1 = await ctrl.tick()
    assert result1["state"] == "forcing"

    calibration_active = False
    tick_time = BASE + timedelta(minutes=1)
    result2 = await ctrl.tick()

    assert result2["state"] == "passive", "the cancel must still fire when the DP does not want this hour"


@pytest.mark.asyncio
async def test_calibration_engaging_mid_economic_forcing_preserves_state_since(monkeypatch):
    """N2 regression: state_since must not re-stamp when calibration engages
    for the FIRST time on a tick where self.plan is ALREADY FORCING for a
    genuine economic reason (calibration was never engaged before this tick).

    Reverting N2 alone (restoring `and _calibration_was_engaged` to the (A)
    branch's condition) makes this fail: `_calibration_was_engaged` is False
    on calibration's first tick regardless of self.plan's own state, so the
    pre-N2 code re-stamped state_since=now even though the device never left
    FORCING.
    """
    hass = StubHass()
    ctrl, _act = make_controller(hass)
    seed_valid_inputs(hass, soc="50.0")
    ctrl.cfg = dataclasses.replace(ctrl.cfg, calibration_enabled=True)

    # Seed an ALREADY-FORCING plan with nothing to do with calibration --
    # self._calibration_engaged stays False (its __init__ default) since
    # calibration has never run a tick yet.
    economic_since = BASE - timedelta(hours=1)
    ctrl.plan = PlanState(ControllerState.FORCING, economic_since, ())

    action = calibration.CalibAction(
        phase="charging",
        window_start=BASE,
        window_end=BASE + timedelta(hours=3),
    )
    monkeypatch.setattr(calibration, "calibration_action", lambda *a, **k: action)

    def _always_bail(plan, *, now, **kwargs):
        # Mirrors scheduler.py's dwell-elapsed-but-not-selected bail -- the
        # SAME fake as the C1(A) test, so the (A) replace branch actually
        # fires and the two conditions (pre-/post-N2) can diverge.
        if plan.state is ControllerState.FORCING:
            return PlanState(ControllerState.PASSIVE, now, ())
        return plan

    monkeypatch.setattr(scheduler, "decide_state", _always_bail)

    tick_time = BASE
    monkeypatch.setattr(controller.dt_util, "utcnow", lambda: tick_time)

    result = await ctrl.tick()

    assert result["state"] == "forcing"
    assert ctrl.plan.state_since == economic_since, (
        "state_since must not re-stamp when calibration engages mid-economic-FORCING"
    )


@pytest.mark.asyncio
async def test_calibration_days_since_falls_back_to_history_span_when_never_calibrated(monkeypatch):
    """Minor 6 (Task 6 review): calibration.calibration_action itself falls
    back to history_span_days for days_since when last_success_end returns
    None (no qualifying dwell yet) -- exactly the "overdue, about to force a
    charge" case. The controller's published calibration_days_since must
    mirror that fallback rather than reporting None right when the number
    is most useful.
    """
    hass = StubHass()
    ctrl, _act = make_controller(hass)
    seed_valid_inputs(hass, soc="50.0")
    ctrl.cfg = dataclasses.replace(ctrl.cfg, calibration_enabled=True, calibration_interval_days=90)
    monkeypatch.setattr(calibration, "calibration_action", lambda *a, **k: None)

    start = BASE - timedelta(days=120)
    ctrl._recorder._soc_samples = [(start.isoformat(), 50.0), (BASE.isoformat(), 50.0)]
    monkeypatch.setattr(controller.dt_util, "utcnow", lambda: BASE)

    await ctrl.tick()

    expected_span = (BASE - start).total_seconds() / 86400.0
    assert ctrl.last_status["calibration_days_since"] == pytest.approx(expected_span)
