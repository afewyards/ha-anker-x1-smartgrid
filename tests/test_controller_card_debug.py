from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from homeassistant.util import dt as dt_util

from custom_components.anker_x1_smartgrid import optimize
from custom_components.anker_x1_smartgrid.recorder import DataRecorder
from tests.helpers import card_inputs, make_controller
from tests.test_carddebug import _horizon
from tests.test_power_card_sensor import NOW, _rebuild, _samples


async def _ctrl(tmp_path, rows=None):
    ctrl, _act = make_controller()
    ctrl._recorder = DataRecorder(str(tmp_path / "t.db"))
    for row in _samples() if rows is None else rows:
        ctrl._recorder.append(row)
    return ctrl


async def _tick(ctrl, now):
    ctrl._battery_margin_at = None
    await ctrl._refresh_battery_margin(now)
    await ctrl._publish_power_card(now, card_inputs(48.0), "hold", False, _rebuild())


async def test_snapshot_written_once_per_day(tmp_path):
    ctrl = await _ctrl(tmp_path)
    await _tick(ctrl, NOW)
    await _tick(ctrl, NOW + timedelta(minutes=10))
    snaps = ctrl._recorder.read_plan_snapshots("2000-01-01")
    assert len(snaps) == 1
    first_ts = snaps[0][1]
    ctrl._snapshot_day = None
    await _tick(ctrl, NOW + timedelta(minutes=20))
    snaps = ctrl._recorder.read_plan_snapshots("2000-01-01")
    assert len(snaps) == 1 and snaps[0][1] == first_ts


async def test_debug_payload_hour_sums_match_card(tmp_path):
    ctrl = await _ctrl(tmp_path)
    await _tick(ctrl, NOW)
    out = await ctrl.power_card_debug()
    assert out["slots"] and out["reference"] and out["internals"]["built_at"] == NOW.isoformat()
    for hour in ctrl.power_card["hours"]:
        mine = [s for s in out["slots"] if s["start"][:13] == hour["start"][:13]]
        for key in ("use_kwh", "solar_kwh", "batt_house_kwh"):
            assert sum(s[key] for s in mine) == pytest.approx(hour[key], abs=0.02)


async def test_debug_payload_empty_before_first_build():
    ctrl, _act = make_controller()
    assert await ctrl.power_card_debug() == {"slots": [], "reference": [], "internals": {}}


async def test_card_payload_unchanged(tmp_path):
    ctrl = await _ctrl(tmp_path)
    await _tick(ctrl, NOW)
    card = ctrl.power_card
    assert set(card) == {"built_at", "battery", "days", "hours", "stored_cost_eur_per_kwh"}
    assert not {"setpoint_w", "batt_w"} & set(card["hours"][0])


async def test_failing_snapshot_write_keeps_the_card(tmp_path):
    ctrl = await _ctrl(tmp_path)

    def _boom(*_a):
        raise OSError("disk full")

    ctrl._recorder.write_plan_snapshot = _boom
    await _tick(ctrl, NOW)
    assert ctrl.power_card is not None and ctrl._snapshot_day is None


async def test_failing_snapshot_purge_does_not_raise(tmp_path):
    ctrl = await _ctrl(tmp_path)

    def _boom(*_a):
        raise OSError("locked")

    ctrl._recorder.purge_plan_snapshots_older_than = _boom
    await ctrl._purge_plan_snapshots(NOW)


async def test_purge_keeps_today_and_the_two_days_before(tmp_path):
    ctrl = await _ctrl(tmp_path, [])
    today = dt_util.as_local(NOW).date()
    days = [(today - timedelta(days=n)).isoformat() for n in (4, 3, 2, 1, 0)]
    for day in days:
        ctrl._recorder.write_plan_snapshot(day, NOW.isoformat(), "[]")
    await ctrl._purge_plan_snapshots(NOW)
    assert [day for day, _ts, _rows in ctrl._recorder.read_plan_snapshots("2000-01-01")] == days[2:]


async def test_debug_reads_from_yesterday_local_midnight_after_fall_back(tmp_path, hass):
    await hass.config.async_set_time_zone("Europe/Amsterdam")
    ams = ZoneInfo("Europe/Amsterdam")
    now = datetime(2026, 10, 25, 23, 30, tzinfo=ams).astimezone(UTC)
    midnight = datetime(2026, 10, 24, tzinfo=ams)
    t0 = midnight.astimezone(UTC)
    rows = [s | {"ts": (t0 + timedelta(minutes=k)).isoformat()} for k, s in enumerate(_samples(49)[:2910])]
    ctrl = await _ctrl(tmp_path, rows)
    await ctrl._refresh_battery_margin(now)
    await ctrl._publish_power_card(now, card_inputs(48.0), "hold", False, _rebuild(_horizon(start=now)))
    out = await ctrl.power_card_debug()
    hour = next(h for h in ctrl.power_card["hours"] if h["start"] == midnight.isoformat())
    mine = [s for s in out["slots"] if s["start"][:13] == "2026-10-24T00"]
    assert len(mine) == 4 and hour["use_kwh"] == pytest.approx(0.4, abs=0.01)
    for key in ("use_kwh", "solar_kwh", "batt_house_kwh"):
        assert sum(s[key] for s in mine) == pytest.approx(hour[key], abs=0.02)


async def test_debug_leaves_out_samples_newer_than_the_card(tmp_path):
    ctrl = await _ctrl(tmp_path)
    await _tick(ctrl, NOW)
    before = await ctrl.power_card_debug()
    for ts in (NOW - timedelta(seconds=30), NOW):
        ctrl._recorder.append(_samples()[0] | {"ts": ts.isoformat(), "soc": 99.0, "load_w": 5000.0})
    assert (await ctrl.power_card_debug())["slots"] == before["slots"]


async def test_debug_internals_carry_the_planner_efficiencies(tmp_path):
    ctrl = await _ctrl(tmp_path)
    await _tick(ctrl, NOW)
    internals = (await ctrl.power_card_debug())["internals"]
    cfg, curve = ctrl.cfg, ctrl._planner_curve()
    assert internals["eta_charge"] == optimize._eta_charge_at(cfg.max_charge_w, cfg, curve)
    assert internals["eta_discharge"] == optimize._eta_discharge_at(cfg.max_export_w, cfg, curve)
    keys = list(internals)
    assert keys.index("eta_charge") + 2 == keys.index("eta_discharge") + 1 == keys.index("cycle_cost_eur_per_kwh")


async def test_debug_internals_without_efficiencies_when_they_fail(tmp_path, monkeypatch):
    ctrl = await _ctrl(tmp_path)
    await _tick(ctrl, NOW)

    def _boom(*_a):
        raise ValueError("no curve")

    monkeypatch.setattr(optimize, "_eta_charge_at", _boom)
    out = await ctrl.power_card_debug()
    assert out["slots"] and out["internals"]["built_at"] == NOW.isoformat()
    assert not {"eta_charge", "eta_discharge"} & set(out["internals"])
