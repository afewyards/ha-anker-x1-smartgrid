"""Meter fast path: cut a commanded charge/export by the meter's excess over the connection limit."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import timedelta

import pytest

from custom_components.anker_x1_smartgrid import executor
from tests.helpers import StubActuator, StubHass, make_controller


def _ctrl(setpoint_w: float, meter: str, *, limit_w: float = 11000.0, fail_on=None):
    hass = StubHass()
    ctrl, act = make_controller(hass, actuator=StubActuator(fail_on=fail_on))
    ctrl.cfg = replace(ctrl.cfg, grid_import_limit_w=limit_w)
    act.engaged, act.last_setpoint_w = True, setpoint_w
    hass.set_state("sensor.meter_power", meter)
    return ctrl, act, hass


@pytest.mark.asyncio
async def test_charge_is_cut_by_the_excess():
    ctrl, act, _ = _ctrl(-7500.0, "15000.0")
    await executor.cut_to_connection_limit(ctrl)
    assert act.calls == [("engage_and_charge", -3500.0)]
    assert ctrl.last_status["setpoint_w"] == -3500.0


@pytest.mark.asyncio
async def test_second_event_inside_the_gap_does_not_write():
    ctrl, act, hass = _ctrl(-7500.0, "15000.0")
    await executor.cut_to_connection_limit(ctrl)
    hass.set_state("sensor.meter_power", "13000.0")
    await executor.cut_to_connection_limit(ctrl)
    assert act.calls == [("engage_and_charge", -3500.0)]


@pytest.mark.asyncio
@pytest.mark.parametrize("meter", ["11000.0", "11050.0", "9000.0", "-15000.0"])
async def test_charge_at_or_under_the_limit_is_left_alone(meter):
    ctrl, act, _ = _ctrl(-7500.0, meter)
    await executor.cut_to_connection_limit(ctrl)
    assert act.calls == []


@pytest.mark.asyncio
async def test_excess_beyond_the_charge_stops_at_zero_without_flipping_sign():
    ctrl, act, _ = _ctrl(-2000.0, "15000.0")
    await executor.cut_to_connection_limit(ctrl)
    assert act.calls == [("engage_hold",)]
    assert act.engaged and act.last_setpoint_w == 0.0
    assert ctrl.last_status["setpoint_w"] == 0.0


@pytest.mark.asyncio
async def test_export_is_cut_by_the_excess():
    ctrl, act, _ = _ctrl(7500.0, "-15000.0")
    await executor.cut_to_connection_limit(ctrl)
    assert act.calls == [("engage_export", 3500.0)]
    assert ctrl.last_status["export_setpoint_w"] == 3500.0


@pytest.mark.asyncio
async def test_export_cut_to_zero_holds_instead_of_releasing():
    ctrl, act, _ = _ctrl(2000.0, "-15000.0")
    await executor.cut_to_connection_limit(ctrl)
    assert act.calls == [("engage_hold",)]
    assert act.engaged


@pytest.mark.asyncio
@pytest.mark.parametrize("meter", ["15000.0", "-11000.0"])
async def test_export_against_import_or_at_the_limit_is_left_alone(meter):
    ctrl, act, _ = _ctrl(7500.0, meter)
    await executor.cut_to_connection_limit(ctrl)
    assert act.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("engaged,setpoint", [(False, -7500.0), (True, 0.0)])
async def test_nothing_happens_unless_we_command_power(engaged, setpoint):
    ctrl, act, _ = _ctrl(setpoint, "15000.0")
    act.engaged = engaged
    await executor.cut_to_connection_limit(ctrl)
    assert act.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("meter", ["unavailable", "unknown", "nan", "inf", "garbage"])
async def test_unreadable_meter_is_ignored(meter):
    ctrl, act, _ = _ctrl(-7500.0, meter)
    await executor.cut_to_connection_limit(ctrl)
    assert act.calls == []


@pytest.mark.asyncio
async def test_skipped_while_a_tick_is_running():
    ctrl, act, _ = _ctrl(-7500.0, "15000.0")
    async with ctrl._tick_lock:
        await executor.cut_to_connection_limit(ctrl)
    assert act.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first_in_line,calls",
    [
        (lambda ctrl: ctrl.set_enabled(False), [("engage_hold",)]),
        (lambda ctrl: ctrl.release(), [("release_to_self",)]),
    ],
    ids=["disable", "unload"],
)
async def test_a_cut_queued_behind_another_lock_holder_is_dropped(first_in_line, calls):
    ctrl, act, _ = _ctrl(-7500.0, "15000.0")
    await ctrl._tick_lock.acquire()
    other = asyncio.create_task(first_in_line(ctrl))
    while not ctrl._tick_lock._waiters:
        await asyncio.sleep(0)
    # Free again, but the queued task goes first: the cut sees an open lock and queues behind it.
    ctrl._tick_lock.release()
    await executor.cut_to_connection_limit(ctrl)
    await other
    assert act.calls == calls


@pytest.mark.asyncio
async def test_failed_write_is_logged_and_left_for_the_tick(caplog):
    ctrl, act, _ = _ctrl(-7500.0, "15000.0", fail_on={"engage_and_charge"})
    with caplog.at_level(logging.ERROR):
        await executor.cut_to_connection_limit(ctrl)
    assert act.calls == [("engage_and_charge", -3500.0)]
    assert act.engaged and act.last_setpoint_w == -7500.0
    assert "fast path" in caplog.text


async def test_setup_subscribes_the_meter_and_unload_unsubscribes(hass, monkeypatch):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.anker_x1_smartgrid import const

    seen = []

    async def _cut(controller):
        seen.append(controller)

    monkeypatch.setattr(executor, "cut_to_connection_limit", _cut)
    entry = MockConfigEntry(domain=const.DOMAIN, data={**const.DEFAULT_ENTITIES})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    controller = hass.data[const.DOMAIN][entry.entry_id]["controller"]
    meter = controller._data[const.CONF_ENT_METER_POWER]

    hass.states.async_set(meter, "12000")
    await hass.async_block_till_done()
    assert seen == [controller]

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    hass.states.async_set(meter, "13000")
    await hass.async_block_till_done()
    assert seen == [controller]


@pytest.mark.asyncio
async def test_tick_skips_while_a_fast_write_holds_the_lock(caplog):
    ctrl, act, _ = _ctrl(-7500.0, "15000.0")
    gate = asyncio.Event()

    async def _slow_charge(setpoint_w):
        act.calls.append(("engage_and_charge", setpoint_w))
        await gate.wait()

    async def _no_tick():
        raise AssertionError("tick ran while the fast path held the lock")

    act.engage_and_charge = _slow_charge
    ctrl._tick_impl = _no_tick
    cut = asyncio.create_task(executor.cut_to_connection_limit(ctrl))
    await asyncio.sleep(0)
    assert ctrl._tick_lock.locked()
    with caplog.at_level(logging.WARNING):
        status = await ctrl.tick()
    gate.set()
    await cut

    assert status is ctrl.last_status
    assert "tick overlap" in caplog.text
    assert act.calls == [("engage_and_charge", -3500.0)]


@pytest.mark.asyncio
@pytest.mark.parametrize("meter_w,expected", [(-5000.0, 300.0), (-1000.0, 3300.0)], ids=["still-over", "spill-gone"])
async def test_the_tick_after_an_export_cut_stays_inside_the_room_the_meter_shows(monkeypatch, meter_w, expected):
    from custom_components.anker_x1_smartgrid import controller as ctrl_mod
    from custom_components.anker_x1_smartgrid.models import ExportState
    from tests.test_controller_export_executor import (
        BASE,
        _make_controller,
        _patched_compute_decision,
        _seed_passive_inputs,
    )

    monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
    hass = StubHass()
    ctrl, act, _ = _make_controller(hass, cfg_overrides={"grid_import_limit_w": 4000.0})
    _seed_passive_inputs(hass, soc="90.0", export_price="0.40")
    cur_h = BASE.replace(minute=0, second=0, microsecond=0)
    monkeypatch.setattr(ctrl_mod, "compute_decision", _patched_compute_decision(export_request={cur_h: 3000.0}))
    ctrl.export_state = ExportState(engaged=True, state_since=BASE - timedelta(hours=1))
    # The fast path cut a 3300 W export to 1300 W; PV spill keeps the meter at `meter_w`.
    act.engaged, act.last_setpoint_w = True, 1300.0
    house_w = 300.0
    hass.set_state("sensor.battery_power", "1300.0")
    hass.set_state("sensor.meter_power", str(meter_w))
    hass.set_state("sensor.pv_power", str(house_w - meter_w - 1300.0))

    await ctrl.tick()

    assert [c for c in act.calls if c[0] == "engage_export"] == [("engage_export", expected)]
