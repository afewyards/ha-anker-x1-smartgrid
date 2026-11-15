from __future__ import annotations

from datetime import timedelta

import pytest

from custom_components.anker_x1_smartgrid.recorder import DataRecorder
from tests.helpers import make_controller
from tests.test_power_card_sensor import NOW, _inputs, _rebuild, _samples


async def _ctrl(tmp_path):
    ctrl, _act = make_controller()
    ctrl._recorder = DataRecorder(str(tmp_path / "t.db"))
    ctrl._recorder.read_feature_rows = lambda since: _samples()
    return ctrl


async def _tick(ctrl, now):
    ctrl._battery_margin_at = None
    await ctrl._refresh_battery_margin(now)
    await ctrl._publish_power_card(now, _inputs(48.0), "hold", False, _rebuild())


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
