"""Hard-floor safety rescue: SoC below const.FIRMWARE_SOC_FLOOR always grid-charges.

The planner is economic-only — it models the firmware's 5% discharge cutoff as
physics and never force-charges to hold it. This rescue is the one exception: a
pack that has sagged BELOW that cutoff (standby drain, BMS drift) is protected,
not optimized. It overrides the DP, the price gate, and the master switch, and
releases the moment SoC reads back at the floor.
"""

import dataclasses
import logging
from datetime import timedelta

import pytest

from custom_components.anker_x1_smartgrid import const, controller, guard, scheduler
from custom_components.anker_x1_smartgrid.models import ControllerState, PlanState
from tests.helpers import BASE, StubHass, make_controller, seed_valid_inputs


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
