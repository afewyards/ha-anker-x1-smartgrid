"""Hard-floor safety rescue: SoC below const.FIRMWARE_SOC_FLOOR always grid-charges.

The planner is economic-only — it models the firmware's 5% discharge cutoff as
physics and never force-charges to hold it. This rescue is the one exception: a
pack that has sagged BELOW that cutoff (standby drain, BMS drift) is protected,
not optimized. It overrides the DP, the price gate, and the master switch, and
releases the moment SoC reads back at the floor.
"""

import dataclasses
import json
import logging
from datetime import timedelta

import pytest

from custom_components.anker_x1_smartgrid import const, controller, guard, scheduler
from custom_components.anker_x1_smartgrid.models import ControllerState, PlanState
from tests.helpers import BASE, CapturingStore, StubHass, make_controller, seed_valid_inputs


def _expected_setpoint(cfg) -> float:
    """The setpoint the rescue must command: same clamp as the FORCING path."""
    return guard.command_setpoint(min(cfg.max_charge_w, cfg.grid_import_limit_w), 0.0, cfg)


def _engages(act) -> list[float]:
    return [c[1] for c in act.calls if c[0] == "engage_and_charge"]


@pytest.mark.asyncio
async def test_subfloor_soc_forces_charge(monkeypatch):
    """SoC under the firmware floor charges even though the DP wants PASSIVE.

    decide_state is faked to bail to PASSIVE unconditionally, so a FORCING
    outcome can only come from the rescue override.
    """
    hass = StubHass()
    ctrl, act = make_controller(hass)
    seed_valid_inputs(hass, soc="4.0")

    monkeypatch.setattr(
        scheduler, "decide_state", lambda plan, *, now, **kw: PlanState(ControllerState.PASSIVE, now, ())
    )

    result = await ctrl.tick()

    assert result["state"] == "forcing"
    assert _engages(act) == [_expected_setpoint(ctrl.cfg)]


@pytest.mark.asyncio
async def test_soc_at_floor_does_not_rescue(monkeypatch):
    """Exactly at the floor is the normal resting state — no rescue."""
    hass = StubHass()
    ctrl, act = make_controller(hass)
    seed_valid_inputs(hass, soc=str(const.FIRMWARE_SOC_FLOOR))

    monkeypatch.setattr(
        scheduler, "decide_state", lambda plan, *, now, **kw: PlanState(ControllerState.PASSIVE, now, ())
    )

    result = await ctrl.tick()

    assert result["state"] == "passive"
    assert _engages(act) == []


@pytest.mark.asyncio
async def test_rescue_stops_at_the_floor(monkeypatch):
    """Rescue releases as soon as SoC reads back at 5.0% — it does not overshoot.

    Second tick keeps decide_state bailing to PASSIVE and the DP not wanting the
    hour, so nothing but the rescue could hold FORCING.
    """
    hass = StubHass()
    ctrl, act = make_controller(hass)
    seed_valid_inputs(hass, soc="4.0")
    monkeypatch.setattr(
        scheduler, "decide_state", lambda plan, *, now, **kw: PlanState(ControllerState.PASSIVE, now, ())
    )

    tick_time = BASE
    monkeypatch.setattr(controller.dt_util, "utcnow", lambda: tick_time)

    assert (await ctrl.tick())["state"] == "forcing"

    # Recovered to the floor, still inside min_dwell_min: the rescue must not
    # coast on the dwell timer the way an economic FORCING would.
    hass.set_state("sensor.soc", str(const.FIRMWARE_SOC_FLOOR))
    tick_time = BASE + timedelta(minutes=1)
    result = await ctrl.tick()

    assert result["state"] == "passive"
    assert any(c[0] == "release_to_self" for c in act.calls)


@pytest.mark.asyncio
async def test_rescue_overrides_master_switch_off():
    """Master switch OFF still rescues: the pack is protected unconditionally."""
    hass = StubHass()
    ctrl, act = make_controller(hass)
    ctrl.enabled = False
    seed_valid_inputs(hass, soc="4.0")

    result = await ctrl.tick()

    assert _engages(act) == [_expected_setpoint(ctrl.cfg)]
    assert result["setpoint_w"] == _expected_setpoint(ctrl.cfg)
    assert not any(c[0] == "release_to_self" for c in act.calls), "must not release while rescuing"


@pytest.mark.asyncio
async def test_master_switch_off_above_floor_stays_hands_off():
    """Above the floor a disabled controller behaves exactly as before."""
    hass = StubHass()
    ctrl, act = make_controller(hass)
    ctrl.enabled = False
    seed_valid_inputs(hass, soc="50.0")

    await ctrl.tick()

    assert _engages(act) == []


@pytest.mark.asyncio
async def test_master_switch_off_rescue_releases_on_recovery():
    """Disabled path hands control back once SoC reaches the floor."""
    hass = StubHass()
    ctrl, act = make_controller(hass)
    ctrl.enabled = False
    seed_valid_inputs(hass, soc="4.0")
    await ctrl.tick()
    assert ctrl.plan.state is ControllerState.FORCING

    hass.set_state("sensor.soc", str(const.FIRMWARE_SOC_FLOOR))
    await ctrl.tick()

    assert any(c[0] == "release_to_self" for c in act.calls)
    assert ctrl.plan.state is ControllerState.PASSIVE


@pytest.mark.asyncio
async def test_rescue_suppresses_export(monkeypatch):
    """Export can never fire while the pack is below the floor."""
    hass = StubHass()
    ctrl, act = make_controller(hass)
    ctrl.cfg = dataclasses.replace(ctrl.cfg, enable_export=True)
    seed_valid_inputs(hass, soc="4.0")

    await ctrl.tick()

    assert not any(c[0].startswith("engage_export") for c in act.calls)


@pytest.mark.asyncio
async def test_rescue_warns_once_per_episode(monkeypatch, caplog):
    """Edge-triggered WARNING: once on entry, re-armed after recovery."""
    hass = StubHass()
    ctrl, _act = make_controller(hass)
    seed_valid_inputs(hass, soc="4.0")

    def _count() -> int:
        return sum(1 for r in caplog.records if "below the firmware floor" in r.message)

    with caplog.at_level(logging.WARNING):
        await ctrl.tick()
        assert _count() == 1
        await ctrl.tick()
        assert _count() == 1, "must not re-warn every tick"

        hass.set_state("sensor.soc", "50.0")
        await ctrl.tick()
        hass.set_state("sensor.soc", "4.0")
        await ctrl.tick()
        assert _count() == 2, "recovery must re-arm the warning"


@pytest.mark.asyncio
async def test_rescue_survives_engage_failure(monkeypatch):
    """A failed engage must not raise out of the tick (actuation is best-effort)."""
    from tests.helpers import StubActuator

    hass = StubHass()
    act = StubActuator(fail_on={"engage_and_charge"})
    ctrl, _ = make_controller(hass, actuator=act)
    ctrl.enabled = False
    seed_valid_inputs(hass, soc="4.0")

    await ctrl.tick()

    assert any(c[0] == "engage_and_charge" for c in act.calls)


@pytest.mark.asyncio
async def test_failsafe_tick_still_rescues_subfloor():
    """Missing price slots (failsafe path) must not disable the rescue."""
    hass = StubHass()
    ctrl, act = make_controller(hass)
    seed_valid_inputs(hass, soc="1.0")
    hass.set_state("sensor.price", "unavailable")

    await ctrl.tick()

    assert _engages(act) == [_expected_setpoint(ctrl.cfg)]
    assert ctrl.plan.state is ControllerState.FORCING
    assert not any(c[0] == "release_to_self" for c in act.calls)


@pytest.mark.asyncio
async def test_failsafe_tick_above_floor_releases():
    """Failsafe above the floor is unchanged: release and PASSIVE."""
    hass = StubHass()
    ctrl, act = make_controller(hass)
    seed_valid_inputs(hass, soc="6.0")
    hass.set_state("sensor.price", "unavailable")

    await ctrl.tick()

    assert _engages(act) == []
    assert ctrl.plan.state is ControllerState.PASSIVE


def _glitch(hass, batt="0.0"):
    hass.set_state("sensor.soc", "0.0")
    hass.set_state("sensor.battery_power", batt)


def _clock(monkeypatch):
    """Tick time under test control: ``at(minutes)`` moves it to BASE + minutes."""
    now = [BASE]
    monkeypatch.setattr(controller.dt_util, "utcnow", lambda: now[0])

    def at(minutes: float) -> None:
        now[0] = BASE + timedelta(minutes=minutes)

    return at


@pytest.mark.asyncio
async def test_zero_soc_glitch_holds_the_pack_and_keeps_recording(caplog):
    """The Anker entities sometimes read SoC 0 / 0 W at a real 96 %: hold at 0 W, keep the meter side."""
    hass = StubHass()
    ctrl, act = make_controller(hass)
    seed_valid_inputs(hass, soc="96.0")
    await ctrl.tick()
    act.calls.clear()
    imported = ctrl.today_house_import_kwh

    _glitch(hass)
    hass.set_state("sensor.meter_power", "11000.0")
    with caplog.at_level(logging.WARNING):
        first = await ctrl.tick()
        second = await ctrl.tick()

    assert _engages(act) == []
    assert not any(c[0].startswith("engage_export") or c[0] == "release_to_self" for c in act.calls)
    assert ("engage_hold",) in act.calls
    assert first["reason"] == second["reason"] == "soc glitch"
    assert first["setpoint_w"] == 0.0
    assert ctrl.plan.state is ControllerState.PASSIVE
    rows = ctrl._recorder.rows[-2:]
    assert [r["soc"] for r in rows] == [None, None]
    assert [r["p1_w"] for r in rows] == [11000.0, 11000.0]
    assert ctrl.today_house_import_kwh - imported == pytest.approx(2 * 11.0 * const.TICK_SECONDS / 3600)
    assert len([r for r in caplog.records if "SoC glitch" in r.getMessage()]) == 1


@pytest.mark.asyncio
async def test_glitch_end_resumes_normal_operation(caplog):
    hass = StubHass()
    ctrl, act = make_controller(hass)
    seed_valid_inputs(hass, soc="96.0")
    await ctrl.tick()
    _glitch(hass)
    await ctrl.tick()

    hass.set_state("sensor.soc", "96.0")
    hass.set_state("sensor.battery_power", "-500.0")
    with caplog.at_level(logging.WARNING):
        result = await ctrl.tick()

    assert result["reason"] != "soc glitch"
    assert ctrl._recorder.rows[-1]["soc"] == 96.0
    assert len([r for r in caplog.records if "SoC glitch over" in r.getMessage()]) == 1
    # Out of the glitch, a 0 with the pack still moving is real again.
    _glitch(hass, batt="-500.0")
    await ctrl.tick()
    assert _engages(act) == [_expected_setpoint(ctrl.cfg)]


@pytest.mark.asyncio
async def test_glitch_holds_through_an_odd_battery_power_tick():
    hass = StubHass()
    ctrl, act = make_controller(hass)
    seed_valid_inputs(hass, soc="96.0")
    await ctrl.tick()

    reasons = []
    for batt in ("0.0", "0.0", "-3.0", "0.0", "0.0"):
        _glitch(hass, batt=batt)
        reasons.append((await ctrl.tick())["reason"])

    assert _engages(act) == []
    assert reasons == ["soc glitch"] * 5


@pytest.mark.asyncio
async def test_glitch_ends_after_four_hours_and_rescues(monkeypatch):
    at = _clock(monkeypatch)
    hass = StubHass()
    ctrl, act = make_controller(hass)
    seed_valid_inputs(hass, soc="96.0")
    await ctrl.tick()

    _glitch(hass)
    at(1)
    await ctrl.tick()
    at(239)
    assert (await ctrl.tick())["reason"] == "soc glitch"
    assert _engages(act) == []

    at(240)
    await ctrl.tick()
    assert _engages(act) == [_expected_setpoint(ctrl.cfg)]


@pytest.mark.asyncio
async def test_zero_after_a_stale_reading_is_real_and_rescues(monkeypatch):
    """After a sensor outage the pack may really have emptied: the 0 is accepted."""
    at = _clock(monkeypatch)
    hass = StubHass()
    ctrl, act = make_controller(hass)
    seed_valid_inputs(hass, soc="96.0")
    await ctrl.tick()

    hass.set_state("sensor.soc", "unavailable")
    for minute in range(1, 10):
        at(minute)
        assert (await ctrl.tick())["reason"] == "failsafe"
    _glitch(hass)
    at(10)
    await ctrl.tick()

    assert _engages(act) == [_expected_setpoint(ctrl.cfg)]


@pytest.mark.asyncio
async def test_restart_mid_glitch_keeps_holding(monkeypatch):
    at = _clock(monkeypatch)
    hass = StubHass()
    ctrl, _act = make_controller(hass)
    ctrl._store = CapturingStore()
    seed_valid_inputs(hass, soc="96.0")
    await ctrl.tick()
    _glitch(hass)
    at(1)
    await ctrl.tick()
    at(30)
    await ctrl.tick()

    fresh, act = make_controller(hass)
    fresh.restore(json.loads(json.dumps(ctrl._store.saved)))
    at(31)
    result = await fresh.tick()

    assert _engages(act) == []
    assert result["reason"] == "soc glitch"


@pytest.mark.asyncio
async def test_glitch_with_master_switch_off_does_not_rescue():
    hass = StubHass()
    ctrl, act = make_controller(hass)
    ctrl.enabled = False
    seed_valid_inputs(hass, soc="96.0")
    await ctrl.tick()

    _glitch(hass)
    await ctrl.tick()

    assert _engages(act) == []
    assert ctrl._recorder.rows[-1]["soc"] is None


@pytest.mark.asyncio
async def test_real_descent_to_zero_still_rescues():
    """From a real low SoC the 0 reading is accepted and the rescue charges."""
    hass = StubHass()
    ctrl, act = make_controller(hass)
    seed_valid_inputs(hass, soc="2.9")
    await ctrl.tick()
    act.calls.clear()

    _glitch(hass)
    await ctrl.tick()

    assert _engages(act) == [_expected_setpoint(ctrl.cfg)]
    assert ctrl.plan.state is ControllerState.FORCING
