"""Meter fast path: cut at the connection limit at once, trim toward the last tick's aim in between."""

from __future__ import annotations

import asyncio
import copy
import logging
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.anker_x1_smartgrid import const, executor
from tests import helpers
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
    await executor.trim_to_meter(ctrl)
    assert act.calls == [("engage_and_charge", -3500.0)]
    assert ctrl.last_status["setpoint_w"] == -3500.0


@pytest.mark.asyncio
async def test_second_event_inside_the_gap_does_not_write():
    ctrl, act, hass = _ctrl(-7500.0, "15000.0")
    await executor.trim_to_meter(ctrl)
    hass.set_state("sensor.meter_power", "13000.0")
    await executor.trim_to_meter(ctrl)
    assert act.calls == [("engage_and_charge", -3500.0)]


@pytest.mark.asyncio
@pytest.mark.parametrize("meter", ["11000.0", "11050.0", "9000.0", "-15000.0"])
async def test_charge_at_or_under_the_limit_is_left_alone(meter):
    ctrl, act, _ = _ctrl(-7500.0, meter)
    await executor.trim_to_meter(ctrl)
    assert act.calls == []


@pytest.mark.asyncio
async def test_excess_beyond_the_charge_stops_at_zero_without_flipping_sign():
    ctrl, act, _ = _ctrl(-2000.0, "15000.0")
    await executor.trim_to_meter(ctrl)
    assert act.calls == [("engage_hold",)]
    assert act.engaged and act.last_setpoint_w == 0.0
    assert ctrl.last_status["setpoint_w"] == 0.0


@pytest.mark.asyncio
async def test_export_is_cut_by_the_excess():
    ctrl, act, _ = _ctrl(7500.0, "-15000.0")
    await executor.trim_to_meter(ctrl)
    assert act.calls == [("engage_export", 3500.0)]
    assert ctrl.last_status["export_setpoint_w"] == 3500.0


@pytest.mark.asyncio
async def test_export_cut_to_zero_holds_instead_of_releasing():
    ctrl, act, _ = _ctrl(2000.0, "-15000.0")
    await executor.trim_to_meter(ctrl)
    assert act.calls == [("engage_hold",)]
    assert act.engaged


@pytest.mark.asyncio
@pytest.mark.parametrize("meter", ["15000.0", "-11000.0"])
async def test_export_against_import_or_at_the_limit_is_left_alone(meter):
    ctrl, act, _ = _ctrl(7500.0, meter)
    await executor.trim_to_meter(ctrl)
    assert act.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("engaged,setpoint", [(False, -7500.0), (True, 0.0)])
async def test_nothing_happens_unless_we_command_power(engaged, setpoint):
    ctrl, act, _ = _ctrl(setpoint, "15000.0")
    act.engaged = engaged
    await executor.trim_to_meter(ctrl)
    assert act.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("meter", ["unavailable", "unknown", "nan", "inf", "garbage"])
async def test_unreadable_meter_is_ignored(meter):
    ctrl, act, _ = _ctrl(-7500.0, meter)
    await executor.trim_to_meter(ctrl)
    assert act.calls == []


@pytest.mark.asyncio
async def test_skipped_while_a_tick_is_running():
    ctrl, act, _ = _ctrl(-7500.0, "15000.0")
    async with ctrl._tick_lock:
        await executor.trim_to_meter(ctrl)
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
    await executor.trim_to_meter(ctrl)
    await other
    assert act.calls == calls


@pytest.mark.asyncio
async def test_failed_write_is_logged_and_left_for_the_tick(caplog):
    ctrl, act, _ = _ctrl(-7500.0, "15000.0", fail_on={"engage_and_charge"})
    with caplog.at_level(logging.ERROR):
        await executor.trim_to_meter(ctrl)
    assert act.calls == [("engage_and_charge", -3500.0)]
    assert act.engaged and act.last_setpoint_w == -7500.0
    assert "fast path" in caplog.text


async def test_setup_subscribes_the_meter_and_unload_unsubscribes(hass, monkeypatch):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.anker_x1_smartgrid import const

    seen = []

    async def _cut(controller):
        seen.append(controller)

    monkeypatch.setattr(executor, "trim_to_meter", _cut)
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
@pytest.mark.parametrize("write_lands", [True, False], ids=["waits", "gives-up"])
async def test_tick_waits_briefly_for_a_fast_write_holding_the_lock(caplog, monkeypatch, write_lands):
    monkeypatch.setattr(const, "TICK_LOCK_WAIT_S", 0.05)
    ctrl, act, _ = _ctrl(-7500.0, "15000.0")
    gate = asyncio.Event()

    async def _slow_charge(setpoint_w):
        act.calls.append(("engage_and_charge", setpoint_w))
        await gate.wait()

    async def _tick_impl():
        assert cut.done()
        return {"state": "ticked"}

    act.engage_and_charge = _slow_charge
    ctrl._tick_impl = _tick_impl
    cut = asyncio.create_task(executor.trim_to_meter(ctrl))
    await asyncio.sleep(0)
    assert ctrl._tick_lock.locked()
    tick = asyncio.create_task(ctrl.tick())
    await asyncio.sleep(0)
    if write_lands:
        gate.set()
    with caplog.at_level(logging.WARNING):
        status = await tick
    gate.set()
    await cut

    assert (status == {"state": "ticked"}) is write_lands
    assert ("tick overlap" in caplog.text) is not write_lands
    assert act.calls == [("engage_and_charge", -3500.0)]
    assert not ctrl._tick_lock.locked()


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


# ---------------------------------------------------------------------------
# Between ticks: raise a cut charge back, hold an export at the tick's aim
# ---------------------------------------------------------------------------

BASE = datetime(2026, 11, 18, 18, 10, tzinfo=UTC)
SETTLE = const.METER_SETTLE_S


@pytest.fixture
def clock(monkeypatch):
    c = SimpleNamespace(mono=1000.0, utc=BASE)
    fake = SimpleNamespace(monotonic=lambda: c.mono)
    monkeypatch.setattr(executor, "time", fake)
    monkeypatch.setattr(helpers, "time", fake)
    monkeypatch.setattr(executor.dt_util, "utcnow", lambda: c.utc)
    return c


async def _read(ctrl, hass, clock, *meter_w, wait_s=SETTLE):
    """Meter updates 0.5 s apart, the first ``wait_s`` + 0.5 s from now."""
    clock.mono += wait_s
    for w in meter_w:
        clock.mono += 0.5
        hass.set_state("sensor.meter_power", str(w))
        await executor.trim_to_meter(ctrl)


async def _tick_wrote(ctrl, act, hass, clock, setpoint_w, cap_w, meter_w, *, settled, aim_w=0.0, pv_w=0.0):
    """Stand in for a tick that wrote ``setpoint_w`` and recorded its aim; ``settled``: the meter then shows it."""
    if setpoint_w < 0.0:
        await act.engage_and_charge(setpoint_w)
    else:
        await act.engage_export(setpoint_w)
    executor.record_meter_trim(ctrl, clock.utc, 60, cap_w, aim_w, pv_w)
    # Meter only: a stale or missing battery reading must not matter.
    hass.set_state("sensor.battery_power", "unavailable")
    if settled:
        await _read(ctrl, hass, clock, meter_w, meter_w, meter_w)


async def _charging(clock, *, setpoint_w=-4000.0, meter_w=5000.0, settled=True):
    # 5 kW connection: the tick charges what the house leaves, and 5 kW (its cap) with no house draw.
    ctrl, act, hass = _ctrl(0.0, str(meter_w), limit_w=5000.0)
    await _tick_wrote(ctrl, act, hass, clock, setpoint_w, -5000.0, meter_w, settled=settled)
    return ctrl, act, hass


async def _exporting(
    clock, *, setpoint_w=3000.0, aim_w=2000.0, house_w=1000.0, pv_w=0.0, cap_w=const.SETPOINT_MAX_W, limit_w=5000.0
):
    # By default an evening export: 2 kW net plus the 1 kW house is 3 kW gross, and the meter shows 2 kW out.
    meter_w = house_w - setpoint_w - pv_w
    ctrl, act, hass = _ctrl(0.0, str(meter_w), limit_w=limit_w)
    hass.set_state("sensor.pv_power", str(pv_w))
    await _tick_wrote(ctrl, act, hass, clock, setpoint_w, cap_w, meter_w, settled=True, aim_w=aim_w, pv_w=pv_w)
    return ctrl, act, hass


async def _cut_by_kettle(ctrl, hass, clock):
    """+2 kW on the default 1 kW house: one reading over the 5 kW limit cuts 4 kW of charge to 2 kW."""
    await _read(ctrl, hass, clock, 7000.0)
    assert ctrl._actuator.last_setpoint_w == -2000.0


@pytest.mark.asyncio
async def test_a_charge_cut_by_a_load_step_is_raised_back_when_the_load_goes(clock):
    ctrl, act, hass = await _charging(clock)
    await _cut_by_kettle(ctrl, hass, clock)
    # Kettle off: 2 kW of room, raised straight back.
    await _read(ctrl, hass, clock, 3000.0, 3000.0, 3000.0)
    assert act.last_setpoint_w == -4000.0
    await _read(ctrl, hass, clock, 5000.0, 5000.0, 5000.0)  # the meter is back at the 5 kW limit
    # House off and 3 kW of PV: room for 4 kW more, but never past the tick's 5 kW cap.
    await _read(ctrl, hass, clock, 1000.0, 1000.0, 1000.0, wait_s=0.0)
    await _read(ctrl, hass, clock, 2000.0, 2000.0, 2000.0)

    assert act.calls == [
        ("engage_and_charge", -4000.0),
        ("engage_and_charge", -2000.0),
        ("engage_and_charge", -4000.0),
        ("engage_and_charge", -5000.0),
    ]


@pytest.mark.asyncio
async def test_a_single_low_reading_does_not_raise(clock):
    ctrl, act, hass = await _charging(clock)
    await _read(ctrl, hass, clock, 5000.0, 2000.0, 5000.0, wait_s=0.0)
    assert act.calls == [("engage_and_charge", -4000.0)]


@pytest.mark.asyncio
async def test_a_load_step_in_the_newest_reading_shrinks_the_raise(clock):
    ctrl, act, hass = await _charging(clock)
    await _cut_by_kettle(ctrl, hass, clock)
    # Kettle off for two readings, then a 1.5 kW load lands: only the 500 W left is taken.
    await _read(ctrl, hass, clock, 3000.0, 3000.0, 4500.0)
    assert act.calls[-1] == ("engage_and_charge", -2500.0)


@pytest.mark.asyncio
async def test_no_raise_until_three_readings_land_after_the_settle(clock):
    ctrl, act, hass = await _charging(clock)
    await _cut_by_kettle(ctrl, hass, clock)
    cut_at = clock.mono
    # Kettle off at once: readings inside the settle time don't count.
    await _read(ctrl, hass, clock, 3000.0, 3000.0, 3000.0, wait_s=0.0)
    clock.mono = cut_at + SETTLE - 0.5
    await _read(ctrl, hass, clock, 3000.0, 3000.0, wait_s=0.0)
    assert act.calls[-1] == ("engage_and_charge", -2000.0)
    await _read(ctrl, hass, clock, 3000.0, wait_s=0.0)
    assert act.calls[-1] == ("engage_and_charge", -4000.0)
    assert clock.mono - cut_at == 3.0


@pytest.mark.asyncio
@pytest.mark.parametrize("meter_w,calls", [(4750.0, []), (4700.0, [("engage_and_charge", -4300.0)])])
async def test_a_raise_waits_for_the_deadband(clock, meter_w, calls):
    ctrl, act, hass = await _charging(clock)
    await _read(ctrl, hass, clock, meter_w, meter_w, meter_w, wait_s=0.0)
    assert act.calls[1:] == calls


@pytest.mark.asyncio
async def test_a_pack_that_does_not_take_the_tick_charge_is_not_wound_up_to_the_cap(clock):
    # Near full the pack tapers to 2 kW of the 4 kW asked: the meter settles 2 kW short of the limit.
    ctrl, act, hass = await _charging(clock, meter_w=3000.0, settled=False)
    await _read(ctrl, hass, clock, 3000.0, 3000.0, 3000.0, 3000.0)
    # The house then drops 500 W: still no raise past what the tick asked, as the tick won't either.
    await _read(ctrl, hass, clock, 2500.0, 2500.0, 2500.0, wait_s=0.0)
    assert act.calls == [("engage_and_charge", -4000.0)]


@pytest.mark.asyncio
async def test_a_fast_raise_the_pack_does_not_take_is_not_repeated(clock):
    ctrl, act, hass = await _charging(clock)
    await _cut_by_kettle(ctrl, hass, clock)
    await _read(ctrl, hass, clock, 3000.0, 3000.0, 3000.0)
    # Raised to 4 kW, but the pack stops at 3 kW: 1 kW of room stays.
    await _read(ctrl, hass, clock, 4000.0, 4000.0, 4000.0, 4000.0)
    assert act.calls[1:] == [("engage_and_charge", -2000.0), ("engage_and_charge", -4000.0)]


@pytest.mark.asyncio
async def test_a_raise_stays_inside_the_live_inverter_limit(clock, monkeypatch):
    ctrl, act, hass = await _charging(clock)
    await _cut_by_kettle(ctrl, hass, clock)
    monkeypatch.setattr(act, "_clamp_to_live_limits", lambda w, quiet=False: max(w, -3500.0))
    await _read(ctrl, hass, clock, 3000.0, 3000.0, 3000.0)
    await _read(ctrl, hass, clock, 4500.0, 4500.0, 4500.0, 4500.0)
    assert act.calls[1:] == [("engage_and_charge", -2000.0), ("engage_and_charge", -3500.0)]


@pytest.mark.asyncio
async def test_a_charge_the_fast_path_cut_to_zero_is_raised_again(clock):
    # 3 kW house: the tick charges 2 kW.
    ctrl, act, hass = await _charging(clock, setpoint_w=-2000.0)
    # +2 kW load: 2 kW over the limit cuts the charge to 0 W, held rather than released.
    await _read(ctrl, hass, clock, 7000.0)
    assert act.calls[-1] == ("engage_hold",) and act.engaged
    await _read(ctrl, hass, clock, 3000.0, 3000.0, 3000.0)
    assert act.calls == [("engage_and_charge", -2000.0), ("engage_hold",), ("engage_and_charge", -2000.0)]


@pytest.mark.asyncio
async def test_export_is_trimmed_up_and_down_to_hold_the_meter_at_the_tick_aim(clock):
    ctrl, act, hass = await _exporting(clock)
    # Oven on (+1.5 kW): only 0.5 kW still leaves at the meter, so the pack covers the oven.
    await _read(ctrl, hass, clock, -500.0, -500.0, -500.0, wait_s=0.0)
    assert act.last_setpoint_w == 4500.0
    await _read(ctrl, hass, clock, -2000.0, -2000.0, -2000.0)
    # Oven off: 3.5 kW leaves, 1.5 kW over the aim.
    await _read(ctrl, hass, clock, -3500.0, -3500.0, -3500.0, wait_s=0.0)
    assert act.calls == [("engage_export", 3000.0), ("engage_export", 4500.0), ("engage_export", 3000.0)]


@pytest.mark.asyncio
async def test_an_export_raise_stops_at_the_max_gross(clock):
    ctrl, act, hass = await _exporting(clock, cap_w=4000.0)
    # +3 kW in the house: 3 kW short of the aim, but the tick allows 4 kW gross at most.
    await _read(ctrl, hass, clock, 1000.0, 1000.0, 1000.0, wait_s=0.0)
    assert act.calls == [("engage_export", 3000.0), ("engage_export", 4000.0)]


@pytest.mark.asyncio
async def test_an_export_trimmed_past_zero_holds_at_zero_and_comes_back(clock):
    # 200 W house: 800 W net is 1 kW gross.
    ctrl, act, hass = await _exporting(clock, setpoint_w=1000.0, aim_w=800.0, house_w=200.0, limit_w=11000.0)
    # The house goes off and 1.3 kW of PV appears: 1.5 kW over the aim, more than the whole export.
    hass.set_state("sensor.pv_power", "1300.0")
    await _read(ctrl, hass, clock, -2300.0, -2300.0, -2300.0, wait_s=0.0)
    assert act.calls[-1] == ("engage_hold",) and act.engaged
    # The PV goes again: nothing leaves, so the export comes back from 0 W.
    hass.set_state("sensor.pv_power", "0.0")
    await _read(ctrl, hass, clock, 0.0, 0.0, 0.0)
    assert act.calls == [("engage_export", 1000.0), ("engage_hold",), ("engage_export", 800.0)]


@pytest.mark.asyncio
async def test_export_neither_covers_lost_pv_spill_nor_chases_extra_pv(clock):
    # 2 kW of PV spills on top of the 2 kW net: the tick's gross leaves 4 kW at the meter.
    ctrl, act, hass = await _exporting(clock, pv_w=2000.0, limit_w=11000.0)
    # A cloud takes the PV: the meter falls back to the pack's own 2 kW and the pack stays put.
    hass.set_state("sensor.pv_power", "0.0")
    await _read(ctrl, hass, clock, -2000.0, -2000.0, -2000.0, wait_s=0.0)
    assert act.calls == [("engage_export", 3000.0)]
    # Sun back stronger than the tick saw: the extra 2 kW of spill is not added to the aim.
    hass.set_state("sensor.pv_power", "4000.0")
    await _read(ctrl, hass, clock, -6000.0, -6000.0, -6000.0, wait_s=0.0)
    assert act.calls == [("engage_export", 3000.0), ("engage_export", 1000.0)]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["charge", "export"])
async def test_the_connection_cut_is_immediate_whatever_the_median_says(clock, mode):
    if mode == "charge":
        ctrl, act, hass = await _charging(clock)
        steady, spike, cut = 5000.0, 6500.0, ("engage_and_charge", -2500.0)
    else:
        ctrl, act, hass = await _exporting(clock)
        steady, spike, cut = -2000.0, -6500.0, ("engage_export", 1500.0)
    await _read(ctrl, hass, clock, steady, steady, spike, wait_s=0.0)
    assert act.calls[1:] == [cut]


_STAND_DOWN = {
    "no-target": lambda ctrl, act, clock: setattr(ctrl, "_meter_trim", None),
    "new-slot": lambda ctrl, act, clock: setattr(clock, "utc", BASE + timedelta(hours=1)),
    "disabled": lambda ctrl, act, clock: setattr(ctrl, "enabled", False),
    "not-engaged": lambda ctrl, act, clock: setattr(act, "engaged", False),
    "mode-mismatch": lambda ctrl, act, clock: setattr(
        ctrl, "_meter_trim", ctrl._meter_trim._replace(cap_w=const.SETPOINT_MAX_W)
    ),
    "tick-chose-0-W": lambda ctrl, act, clock: act.engage_hold(),
    "unrecorded-write": lambda ctrl, act, clock: act.engage_and_charge(-4000.0),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("stand_down", list(_STAND_DOWN.values()), ids=list(_STAND_DOWN))
async def test_no_raise_while_standing_down(clock, stand_down):
    ctrl, act, hass = await _charging(clock)
    if asyncio.iscoroutine(result := stand_down(ctrl, act, clock)):
        await result
    calls = list(act.calls)
    await _read(ctrl, hass, clock, 3000.0, 3000.0, 3000.0, 3000.0)
    assert act.calls == calls
    assert not ctrl._meter_errors


@pytest.mark.asyncio
async def test_no_raise_while_a_tick_holds_the_lock(clock):
    ctrl, act, hass = await _charging(clock)
    async with ctrl._tick_lock:
        await _read(ctrl, hass, clock, 3000.0, 3000.0, 3000.0, wait_s=0.0)
    assert act.calls == [("engage_and_charge", -4000.0)]


def _books(ctrl):
    return copy.deepcopy(
        (
            asdict(ctrl._ledger),
            ctrl._soc_drift_kwh,
            ctrl._soc_drift_last_export_kwh_dc,
            ctrl.export_state,
            ctrl.hold_state,
            ctrl.plan,
            vars(ctrl._hour_acc),
            list(ctrl._hold_deficit_w),
            ctrl._recorder.rows,
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["charge", "export"])
async def test_fast_path_moves_leave_the_books_alone(clock, mode):
    if mode == "charge":
        ctrl, act, hass = await _charging(clock)
        meters = (7000.0, 3000.0, 3000.0, 3000.0)
    else:
        ctrl, act, hass = await _exporting(clock)
        meters = (-6000.0, -500.0, -500.0, -500.0)
    ctrl._store = store = helpers.CapturingStore()
    before = _books(ctrl)
    await _read(ctrl, hass, clock, meters[0])
    await _read(ctrl, hass, clock, *meters[1:])
    assert len(act.calls) == 3
    assert _books(ctrl) == before
    assert store.saved is None


def _forcing_controller(clock, charge_limit_w=None):
    """A real-DP FORCING tick: 12 kW connection, 11 kW inverter, 1.3 kW house."""
    from custom_components.anker_x1_smartgrid.models import Config, ExportState
    from tests.test_dp_to_executor_e2e import (
        TestCheapHourGridCharge,
        _make_controller,
        _seed_common,
        _seed_price_forecast,
    )

    clock.utc = base = TestCheapHourGridCharge.BASE
    hass = StubHass()
    ctrl, act = _make_controller(
        hass, cfg_overrides={"capacity_kwh": 40.0, "max_charge_w": 11000.0, "grid_import_limit_w": 12000.0}
    )
    if charge_limit_w is not None:
        ctrl.cfg = Config.from_dict({**asdict(ctrl.cfg), const.CONF_BATTERY_CHARGE_LIMIT_W: charge_limit_w})
    _seed_common(hass, base, soc="15.0", export_price="0.30")
    _seed_price_forecast(hass, base, [0.05] + [0.35] * 11)
    hass.set_state("sensor.meter_power", "1300.0")
    ctrl.export_state = ExportState(engaged=False, state_since=base - timedelta(hours=1))
    return ctrl, act, hass


@pytest.mark.asyncio
@pytest.mark.parametrize("charge_limit_w,cap_w", [(None, -11000.0), (4000.0, -4000.0)])
async def test_a_forcing_tick_records_its_zero_draw_charge_as_the_cap(clock, charge_limit_w, cap_w):
    ctrl, act, _ = _forcing_controller(clock, charge_limit_w)
    await ctrl.tick()
    assert (ctrl._meter_trim.cap_w, ctrl._meter_trim.written_at) == (cap_w, act.last_write_at)


@pytest.mark.asyncio
async def test_the_tick_after_a_fast_charge_raise_keeps_it(clock):
    ctrl, act, hass = _forcing_controller(clock)
    await ctrl.tick()
    # +4 kW in the house: 16 kW at the meter is cut to 6.7 kW of charge.
    await _read(ctrl, hass, clock, 16000.0)
    # Load gone: raised back to the limit between ticks.
    await _read(ctrl, hass, clock, 8000.0, 8000.0, 8000.0)
    # Settled: the battery register has caught up by the next tick.
    hass.set_state("sensor.battery_power", "-10700.0")
    hass.set_state("sensor.meter_power", "12000.0")
    clock.mono += 10.0
    await ctrl.tick()
    assert [c[1] for c in act.calls if c[0] == "engage_and_charge"] == [-10700.0, -6700.0, -10700.0, -10700.0]


async def _fast_raise_then_tick(ctrl, act, hass, clock, wait_s, tweak):
    await ctrl.tick()
    # +4 kW in the house cuts the charge to 6.7 kW; the load goes and the fast path raises it back.
    await _read(ctrl, hass, clock, 16000.0)
    await _read(ctrl, hass, clock, 8000.0, 8000.0, 8000.0)
    assert act.last_setpoint_w == -10700.0
    if tweak == "write-failed":
        act.fail_on = {"engage_and_charge"}
        with pytest.raises(RuntimeError):
            await act.engage_and_charge(-9000.0)
        act.fail_on = set()
    elif tweak == "not-engaged":
        act.engaged = False
    # The meter shows the raise; battery_power still shows the cut.
    hass.set_state("sensor.battery_power", "-6700.0")
    hass.set_state("sensor.meter_power", "12000.0")
    clock.mono += wait_s
    await ctrl.tick()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wait_s,tweak,written_w",
    [
        (2.0, None, -10700.0),
        (const.BATTERY_POWER_LAG_S, None, -6700.0),
        (2.0, "not-engaged", -6700.0),
        (2.0, "write-failed", -6700.0),
    ],
    ids=["lagging", "settled", "not-engaged", "write-failed"],
)
async def test_a_tick_right_after_a_fast_raise_uses_the_setpoint_for_the_battery(clock, wait_s, tweak, written_w):
    ctrl, act, hass = _forcing_controller(clock)
    await _fast_raise_then_tick(ctrl, act, hass, clock, wait_s, tweak)
    assert act.calls[-1] == ("engage_and_charge", written_w)


def _export_tick_controller(clock, monkeypatch, **cfg_overrides):
    """3 kW net committed into a 300 W house with no PV: 3.3 kW gross, 3 kW out at the meter."""
    from custom_components.anker_x1_smartgrid import controller as ctrl_mod
    from custom_components.anker_x1_smartgrid.models import ExportState
    from tests.test_controller_export_executor import (
        BASE as EXPORT_BASE,
        _make_controller,
        _patched_compute_decision,
        _seed_passive_inputs,
    )

    clock.utc = EXPORT_BASE
    hass = StubHass()
    ctrl, act, _ = _make_controller(hass, cfg_overrides={"grid_import_limit_w": 4000.0, **cfg_overrides})
    _seed_passive_inputs(hass, soc="90.0", export_price="0.40")
    cur_h = EXPORT_BASE.replace(minute=0, second=0, microsecond=0)
    monkeypatch.setattr(ctrl_mod, "compute_decision", _patched_compute_decision(export_request={cur_h: 3000.0}))
    ctrl.export_state = ExportState(engaged=True, state_since=EXPORT_BASE - timedelta(hours=1))
    act.engaged, act.last_setpoint_w = True, 3300.0
    hass.set_state("sensor.battery_power", "3300.0")
    hass.set_state("sensor.pv_power", "0.0")
    hass.set_state("sensor.meter_power", "-3000.0")
    return ctrl, act, hass


@pytest.mark.asyncio
async def test_the_tick_after_a_fast_export_trim_keeps_it(clock, monkeypatch):
    ctrl, act, hass = _export_tick_controller(clock, monkeypatch)
    await ctrl.tick()
    assert ctrl._meter_trim.aim_w == 3000.0
    await _read(ctrl, hass, clock, -3000.0, -3000.0, -3000.0)
    # +1 kW in the house: the pack covers it between ticks.
    await _read(ctrl, hass, clock, -2000.0, -2000.0, -2000.0, wait_s=0.0)
    hass.set_state("sensor.battery_power", "4300.0")
    hass.set_state("sensor.meter_power", "-3000.0")
    clock.mono += 10.0
    await ctrl.tick()
    assert [c[1] for c in act.calls if c[0] == "engage_export"] == [3300.0, 4300.0, 4300.0]


@pytest.mark.asyncio
async def test_a_tick_right_after_a_fast_export_raise_keeps_it_and_records_the_true_house_load(clock, monkeypatch):
    ctrl, act, hass = _export_tick_controller(clock, monkeypatch)
    await ctrl.tick()
    await _read(ctrl, hass, clock, -3000.0, -3000.0, -3000.0)
    await _read(ctrl, hass, clock, -2000.0, -2000.0, -2000.0, wait_s=0.0)
    assert act.last_setpoint_w == 4300.0
    # The meter shows the raise; battery_power still shows the 3.3 kW before it.
    hass.set_state("sensor.meter_power", "-3000.0")
    clock.mono += 2.0
    await ctrl.tick()
    assert [c[1] for c in act.calls if c[0] == "engage_export"] == [3300.0, 4300.0, 4300.0]
    assert ctrl._recorder.rows[-1]["load_w"] == ctrl._hold_deficit_w[-1][1] == 1300.0


@pytest.mark.asyncio
async def test_partial_load_compensation_leaves_the_export_to_the_tick(clock, monkeypatch):
    ctrl, act, hass = _export_tick_controller(clock, monkeypatch)
    await ctrl.tick()
    assert ctrl._meter_trim is not None
    ctrl.cfg = replace(ctrl.cfg, export_load_comp_factor=0.5)
    clock.mono += 10.0
    await ctrl.tick()
    calls = list(act.calls)
    assert calls[-1][0] == "engage_export"
    meter_w = 300.0 - calls[-1][1]
    # Settled at the tick's gross, then +1 kW in the house: no aim, old or new, steers this export.
    await _read(ctrl, hass, clock, meter_w, meter_w, meter_w)
    await _read(ctrl, hass, clock, meter_w + 1000.0, meter_w + 1000.0, meter_w + 1000.0, wait_s=0.0)
    assert act.calls == calls


async def _toggling_load(ctrl, act, hass, clock, *, seconds=120.0):
    """1 kW house plus a 2 kW load 3 s on, 3 s off; the pack follows a write 1 s later. Returns (writes, s over)."""
    start, written = clock.mono, len(act.calls)
    pack = [(float("-inf"), act.last_setpoint_w)]
    over_s = 0.0
    for i in range(1, int(seconds * 2) + 1):
        clock.mono = start + i * 0.5
        charge_w = next(w for at, w in reversed(pack) if at <= clock.mono - 1.0)
        meter_w = 1000.0 + (2000.0 if (i * 0.5) % 6.0 < 3.0 else 0.0) - charge_w
        over_s += 0.5 if meter_w > ctrl.cfg.grid_import_limit_w else 0.0
        hass.set_state("sensor.meter_power", str(meter_w))
        n = len(act.calls)
        await executor.trim_to_meter(ctrl)
        if len(act.calls) > n:
            pack.append((clock.mono, act.last_setpoint_w))
    return len(act.calls) - written, over_s


@pytest.mark.asyncio
async def test_a_toggling_load_does_not_set_off_a_cut_raise_cycle(clock):
    ctrl, act, hass = await _charging(clock)
    writes, over_s = await _toggling_load(ctrl, act, hass, clock)
    # Cut, raised back once, cut again and held: each cut overshoots only while the pack follows it.
    assert (writes, over_s) == (3, 2.0)
    assert act.calls[1:] == [
        ("engage_and_charge", -2000.0),
        ("engage_and_charge", -4000.0),
        ("engage_and_charge", -2000.0),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("after,cut", [("fast-raise", True), ("tick-write", False)])
async def test_an_over_limit_reading_right_after_a_fast_raise_cuts_at_once(clock, after, cut):
    ctrl, act, hass = await _charging(clock)
    if after == "fast-raise":
        await _cut_by_kettle(ctrl, hass, clock)
        await _read(ctrl, hass, clock, 3000.0, 3000.0, 3000.0)
        assert act.last_setpoint_w == -4000.0
    else:
        await _tick_wrote(ctrl, act, hass, clock, -4000.0, -5000.0, 5000.0, settled=False)
    calls = len(act.calls)
    # The kettle lands half a second after the write.
    await _read(ctrl, hass, clock, 7000.0, wait_s=0.0)
    assert act.calls[calls:] == ([("engage_and_charge", -2000.0)] if cut else [])


@pytest.mark.asyncio
async def test_a_fast_raise_cut_again_holds_until_the_next_tick(clock):
    ctrl, act, hass = await _charging(clock)
    await _cut_by_kettle(ctrl, hass, clock)
    await _read(ctrl, hass, clock, 3000.0, 3000.0, 3000.0)
    await _read(ctrl, hass, clock, 7000.0, wait_s=0.0)
    await _read(ctrl, hass, clock, 3000.0, 3000.0, 3000.0)
    assert act.calls[1:] == [
        ("engage_and_charge", -2000.0),
        ("engage_and_charge", -4000.0),
        ("engage_and_charge", -2000.0),
    ]
    # The next tick's aim may raise again.
    await _tick_wrote(ctrl, act, hass, clock, -2000.0, -5000.0, 5000.0, settled=True)
    await _read(ctrl, hass, clock, 3000.0, 3000.0, 3000.0, wait_s=0.0)
    assert act.calls[-1] == ("engage_and_charge", -4000.0)


@pytest.mark.asyncio
async def test_an_export_raise_cut_at_the_connection_limit_holds_until_the_next_tick(clock):
    ctrl, act, hass = await _exporting(clock)
    # Oven on: raised to 4.5 kW. Oven off and 2 kW of PV spill: 5.5 kW out cuts it to 4 kW.
    await _read(ctrl, hass, clock, -500.0, -500.0, -500.0, wait_s=0.0)
    hass.set_state("sensor.pv_power", "2000.0")
    await _read(ctrl, hass, clock, -5500.0)
    # PV gone, oven on: 500 W short of the aim, but no raise past the cut.
    hass.set_state("sensor.pv_power", "0.0")
    await _read(ctrl, hass, clock, -1500.0, -1500.0, -1500.0)
    assert act.calls == [("engage_export", 3000.0), ("engage_export", 4500.0), ("engage_export", 4000.0)]


@pytest.mark.asyncio
async def test_readings_older_than_the_window_do_not_count(clock):
    ctrl, act, hass = await _charging(clock)
    await _cut_by_kettle(ctrl, hass, clock)
    await _read(ctrl, hass, clock, 3000.0, 3000.0)
    # The meter holds still (no events) past the window: the two readings above drop out.
    await _read(ctrl, hass, clock, 3000.0, wait_s=const.METER_WINDOW_S)
    assert act.calls[-1] == ("engage_and_charge", -2000.0)
    await _read(ctrl, hass, clock, 3000.0, 3000.0, wait_s=0.0)
    assert act.calls[-1] == ("engage_and_charge", -4000.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["unavailable", "nan"])
async def test_an_unreadable_meter_mid_window_clears_it(clock, bad):
    ctrl, act, hass = await _charging(clock)
    await _cut_by_kettle(ctrl, hass, clock)
    await _read(ctrl, hass, clock, 3000.0, 3000.0, bad, 3000.0, 3000.0)
    assert act.calls[-1] == ("engage_and_charge", -2000.0)
    assert len(ctrl._meter_errors) == 2


@pytest.mark.asyncio
async def test_a_failed_raise_stands_down_until_the_next_tick(clock, caplog):
    ctrl, act, hass = await _charging(clock)
    await _cut_by_kettle(ctrl, hass, clock)
    act.fail_on = {"engage_and_charge"}
    with caplog.at_level(logging.ERROR):
        await _read(ctrl, hass, clock, 3000.0, 3000.0, 3000.0)
    assert act.calls[-1] == ("engage_and_charge", -4000.0)
    assert act.last_setpoint_w == ctrl.last_status["setpoint_w"] == -2000.0
    assert "fast path" in caplog.text
    act.fail_on = set()
    await _read(ctrl, hass, clock, 3000.0, 3000.0, 3000.0)
    assert len(act.calls) == 3 and not ctrl._meter_errors
    # The connection cut still works without an aim.
    await _read(ctrl, hass, clock, 6000.0)
    assert act.calls[-1] == ("engage_and_charge", -1000.0)
