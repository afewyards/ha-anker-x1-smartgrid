from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from homeassistant.util import dt as dt_util

from custom_components.anker_x1_smartgrid import const, optimize
from custom_components.anker_x1_smartgrid.recorder import DataRecorder
from tests.helpers import card_inputs, make_controller
from tests.test_carddebug import _horizon
from tests.test_power_card_sensor import NOW, _charge_then_export, _rebuild, _samples


async def _ctrl(tmp_path, rows=None, **kw):
    ctrl, _act = make_controller(**kw)
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
    assert {"export_price", "plan_eur", "real_eur"} <= set(out["slots"][0])
    assert out["hour_costs"] and set(out["hour_costs"]) <= {s["start"] for s in out["slots"]}
    assert out["internals"]["battery_sample_age_s"] == 60 and "pv_forecast_age_s" not in out["internals"]
    for hour in ctrl.power_card["hours"]:
        mine = [s for s in out["slots"] if s["start"][:13] == hour["start"][:13]]
        for key in ("use_kwh", "solar_kwh", "batt_house_kwh"):
            assert sum(s[key] for s in mine) == pytest.approx(hour[key], abs=0.02)


async def test_debug_payload_empty_before_first_build():
    ctrl, _act = make_controller()
    assert await ctrl.power_card_debug() == {
        "slots": [],
        "hour_costs": {},
        "reference": [],
        "internals": {},
        "reasons": {},
    }


async def test_card_payload_unchanged(tmp_path):
    ctrl = await _ctrl(tmp_path)
    await _tick(ctrl, NOW)
    card = ctrl.power_card
    assert set(card) == {
        "built_at",
        "battery",
        "live",
        "days",
        "hours",
        "stored_cost_eur_per_kwh",
        "break_even_eur_per_kwh",
    }
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
    assert [snap[0] for snap in ctrl._recorder.read_plan_snapshots("2000-01-01")] == days[2:]


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
    assert internals["eta_charge"] == optimize.eta_charge_at(cfg.max_charge_w, cfg, curve)
    assert internals["eta_discharge"] == optimize.eta_discharge_at(cfg.max_export_w, cfg, curve)
    keys = list(internals)
    assert keys.index("eta_charge") + 2 == keys.index("eta_discharge") + 1 == keys.index("cycle_cost_eur_per_kwh")


async def test_debug_internals_without_efficiencies_when_they_fail(tmp_path, monkeypatch):
    ctrl = await _ctrl(tmp_path)
    await _tick(ctrl, NOW)

    def _boom(*_a):
        raise ValueError("no curve")

    monkeypatch.setattr(optimize, "eta_charge_at", _boom)
    out = await ctrl.power_card_debug()
    assert out["slots"] and out["internals"]["built_at"] == NOW.isoformat()
    assert not {"eta_charge", "eta_discharge"} & set(out["internals"])


async def _reason_tick(ctrl, now, terminal=0.30):
    ctrl.last_status["terminal_v_hi"] = terminal
    ctrl._battery_margin_at = None
    await ctrl._refresh_battery_margin(now)
    await ctrl._publish_power_card(now, card_inputs(48.0), "hold", False, _charge_then_export())


def _stored(ctrl):
    return {day: json.loads(rj) for day, _ts, _rows, rj in ctrl._recorder.read_plan_snapshots("2000-01-01") if rj}


async def test_each_rebuild_stores_the_reasons_of_todays_hours_not_yet_over(tmp_path):
    ctrl = await _ctrl(tmp_path)
    later = NOW + timedelta(minutes=70)
    await _reason_tick(ctrl, NOW)
    hour_starts = {h["start"] for h in ctrl.power_card["hours"]}
    await _reason_tick(ctrl, later)
    day = dt_util.as_local(NOW).date().isoformat()
    first, held = (dt_util.as_local(NOW + timedelta(hours=h)).isoformat() for h in (0, 2))
    stored = _stored(ctrl)[day]
    assert stored[first]["at"] == NOW.isoformat() and stored[first]["beats"]
    assert stored[held]["at"] == later.isoformat() and stored[held]["beats"]
    assert all(start[:10] == day for start in stored)
    out = await ctrl.power_card_debug()
    assert out["reasons"][first] == stored[first]
    assert out["reasons"][held] == stored[held]
    assert any(start[:10] > day for start in out["reasons"])
    assert set(out["reasons"]) <= hour_starts


async def test_debug_shows_a_stored_reason_only_once_its_hour_is_over(tmp_path):
    ctrl = await _ctrl(tmp_path)
    await _reason_tick(ctrl, NOW)
    await _reason_tick(ctrl, NOW + timedelta(minutes=70), terminal=None)
    over, planned = (dt_util.as_local(NOW + timedelta(hours=h)).isoformat() for h in (0, 2))
    assert planned in _stored(ctrl)[dt_util.as_local(NOW).date().isoformat()]
    reasons = (await ctrl.power_card_debug())["reasons"]
    assert planned not in reasons
    assert reasons[over]["at"] == NOW.isoformat()


async def test_a_reason_stored_before_its_hour_starts_is_kept_through_the_hour(tmp_path):
    ctrl = await _ctrl(tmp_path)
    await _reason_tick(ctrl, NOW)
    for mins in (70, 100):
        await _reason_tick(ctrl, NOW + timedelta(minutes=mins))
    stored = _stored(ctrl)[dt_util.as_local(NOW).date().isoformat()]
    assert stored[dt_util.as_local(NOW + timedelta(hours=1)).isoformat()]["at"] == NOW.isoformat()


async def test_the_running_hours_first_reason_is_stored_once_and_kept(tmp_path):
    ctrl = await _ctrl(tmp_path)
    first = NOW + timedelta(minutes=10)
    for at in (first, NOW + timedelta(minutes=40)):
        await _reason_tick(ctrl, at)
    stored = _stored(ctrl)[dt_util.as_local(NOW).date().isoformat()]
    assert stored[dt_util.as_local(NOW).isoformat()]["at"] == first.isoformat()


async def test_stored_reasons_survive_a_restart(tmp_path):
    ctrl = await _ctrl(tmp_path)
    await _reason_tick(ctrl, NOW)
    ctrl._recorder.close()
    again = await _ctrl(tmp_path, [])
    later = NOW + timedelta(minutes=70)
    await _reason_tick(again, later)
    reasons = (await again.power_card_debug())["reasons"]
    assert reasons[dt_util.as_local(NOW).isoformat()]["at"] == NOW.isoformat()
    assert reasons[dt_util.as_local(NOW + timedelta(hours=2)).isoformat()]["at"] == later.isoformat()


async def test_failing_reasons_write_warns_once_and_keeps_the_card(tmp_path, caplog):
    ctrl = await _ctrl(tmp_path)

    def _boom(*_a):
        raise OSError("disk full")

    ctrl._recorder.write_plan_reasons = _boom
    with caplog.at_level(logging.WARNING):
        await _reason_tick(ctrl, NOW)
        await _reason_tick(ctrl, NOW + timedelta(minutes=10))
    assert ctrl.power_card is not None
    assert caplog.text.count("plan snapshot failed") == 1


async def test_debug_internals_age_the_oldest_pv_forecast_entity(tmp_path):
    ents = {const.CONF_ENT_PV_TODAY: ["sensor.pv_a", "sensor.pv_c"], const.CONF_ENT_PV_TOMORROW: ["sensor.pv_b"]}
    ctrl = await _ctrl(tmp_path, data_overrides=ents)
    for ent, mins in (("sensor.pv_a", 20), ("sensor.pv_b", 95)):
        ctrl._hass._states[ent] = SimpleNamespace(state="1", attributes={}, last_updated=NOW - timedelta(minutes=mins))
    await _tick(ctrl, NOW)
    assert (await ctrl.power_card_debug())["internals"]["pv_forecast_age_s"] == 95 * 60


async def test_stored_reasons_keep_both_hours_of_a_fall_back_night(tmp_path, hass):
    await hass.config.async_set_time_zone("Europe/Amsterdam")
    now = datetime(2026, 10, 24, 23, 30, tzinfo=UTC)
    t0 = now - timedelta(hours=30)
    rows = [s | {"ts": (t0 + timedelta(minutes=k)).isoformat()} for k, s in enumerate(_samples())]
    ctrl = await _ctrl(tmp_path, rows)
    ctrl.last_status["terminal_v_hi"] = 0.30
    await ctrl._refresh_battery_margin(now)
    await ctrl._publish_power_card(now, card_inputs(48.0), "hold", False, _rebuild(_horizon(start=now)))
    (stored,) = _stored(ctrl).values()
    assert {"2026-10-25T02:00:00+02:00", "2026-10-25T02:00:00+01:00"} <= set(stored)
