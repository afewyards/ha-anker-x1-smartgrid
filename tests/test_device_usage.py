import sqlite3

import pytest

from custom_components.anker_x1_smartgrid import const, coordinator
from custom_components.anker_x1_smartgrid.recorder import DataRecorder, _VERSION_TARGET
from custom_components.anker_x1_smartgrid.sensor import X1TrackedDevicesSensor
from tests.helpers import StubHass
from tests.test_controller_phase2 import _RollupRecorder, _T6_NOW, _make_t6_controller

from custom_components.anker_x1_smartgrid import controller


def _rec(tmp_path):
    return DataRecorder(str(tmp_path / "t.db"))


def _rows(rec, sql):
    return rec._conn.execute(sql).fetchall()


def test_version_target_and_tables(tmp_path):
    assert _VERSION_TARGET == 13
    rec = _rec(tmp_path)
    assert rec._conn.execute("PRAGMA user_version").fetchone()[0] == 13
    names = {r[0] for r in _rows(rec, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"device_samples", "device_hourly"} <= names
    rec.close()


def test_migrates_v12_db_idempotently(tmp_path):
    path = str(tmp_path / "old.db")
    rec = DataRecorder(path)
    rec._conn.execute("DROP TABLE device_samples")
    rec._conn.execute("DROP TABLE device_hourly")
    rec._conn.execute("PRAGMA user_version = 12")
    rec._conn.commit()
    rec.close()
    for _ in range(2):
        rec = DataRecorder(path)
        assert rec._conn.execute("PRAGMA user_version").fetchone()[0] == 13
        assert _rows(rec, "SELECT count(*) FROM device_samples") == [(0,)]
        rec.close()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 12")
    conn.commit()
    conn.close()
    DataRecorder(path).close()


def test_append_keeps_null_and_ignores_duplicate(tmp_path):
    rec = _rec(tmp_path)
    ts = "2026-06-20T10:00:00+00:00"
    rec.append_device_samples(ts, {"sensor.a": (None, None), "switch.b": (700.0, "on")})
    rec.append_device_samples(ts, {"sensor.a": (5.0, None)})
    assert _rows(rec, "SELECT entity_id, watts, state FROM device_samples ORDER BY entity_id") == [
        ("sensor.a", None, None),
        ("switch.b", 700.0, "on"),
    ]
    rec.close()


def _fill(rec, hour, watts, ent="sensor.a"):
    for i, w in enumerate(watts):
        rec.append_device_samples(f"2026-06-20T{hour:02d}:{i:02d}:00+00:00", {ent: (w, None)})


def test_rollup_mean_of_non_null_and_count(tmp_path):
    rec = _rec(tmp_path)
    _fill(rec, 8, [100.0, None, 300.0])
    assert rec.rollup_device_hours("2026-06-20T09:30:00+00:00") == 1
    assert _rows(rec, "SELECT hour_ts, entity_id, w_mean, n FROM device_hourly") == [
        ("2026-06-20T08:00:00+00:00", "sensor.a", 200.0, 2)
    ]
    rec.close()


def test_rollup_all_null_hour_has_no_row_and_current_hour_not_rolled(tmp_path):
    rec = _rec(tmp_path)
    _fill(rec, 8, [None, None])
    _fill(rec, 9, [50.0])
    assert rec.rollup_device_hours("2026-06-20T09:30:00+00:00") == 0
    assert _rows(rec, "SELECT * FROM device_hourly") == []
    rec.close()


def test_rollup_second_run_adds_nothing_and_missed_hour_heals(tmp_path):
    rec = _rec(tmp_path)
    _fill(rec, 8, [100.0])
    _fill(rec, 10, [200.0])
    assert rec.rollup_device_hours("2026-06-20T09:30:00+00:00") == 1
    assert rec.rollup_device_hours("2026-06-20T09:45:00+00:00") == 0
    assert rec.rollup_device_hours("2026-06-20T11:00:00+00:00") == 1
    assert [r[0] for r in _rows(rec, "SELECT hour_ts FROM device_hourly ORDER BY hour_ts")] == [
        "2026-06-20T08:00:00+00:00",
        "2026-06-20T10:00:00+00:00",
    ]
    rec.close()


def test_rollup_deletes_future_hourly_rows(tmp_path):
    rec = _rec(tmp_path)
    _fill(rec, 8, [None])
    rec._conn.execute("INSERT INTO device_hourly VALUES ('2026-06-20T12:00:00+00:00', 'sensor.a', 1.0, 1)")
    rec._conn.execute("INSERT INTO device_hourly VALUES ('2026-06-20T08:00:00+00:00', 'sensor.a', 1.0, 1)")
    rec.rollup_device_hours("2026-06-20T10:30:00+00:00")
    assert [r[0] for r in _rows(rec, "SELECT hour_ts FROM device_hourly")] == ["2026-06-20T08:00:00+00:00"]
    rec.close()


def test_rollup_heals_several_pending_hours_with_microseconds(tmp_path):
    rec = _rec(tmp_path)
    for h, w in ((6, 100.0), (7, 200.0), (8, 300.0)):
        rec.append_device_samples(f"2026-06-20T{h:02d}:15:00.123456+00:00", {"sensor.a": (w, None)})
    assert rec.rollup_device_hours("2026-06-20T09:10:00.654321+00:00") == 3
    assert _rows(rec, "SELECT hour_ts, w_mean FROM device_hourly ORDER BY hour_ts") == [
        ("2026-06-20T06:00:00+00:00", 100.0),
        ("2026-06-20T07:00:00+00:00", 200.0),
        ("2026-06-20T08:00:00+00:00", 300.0),
    ]
    rec.close()


def test_rollup_keeps_rows_the_raw_table_cannot_rebuild_when_clock_runs_back(tmp_path):
    rec = _rec(tmp_path)
    rec._conn.execute("INSERT INTO device_hourly VALUES ('2026-01-01T00:00:00+00:00', 'sensor.a', 1.0, 1)")
    rec._conn.commit()
    rec.rollup_device_hours("2025-12-01T10:30:00+00:00")
    rec.rollup_device_hours("2026-06-20T10:30:00+00:00")
    assert _rows(rec, "SELECT hour_ts FROM device_hourly") == [("2026-01-01T00:00:00+00:00",)]
    rec.close()


def test_purges_keep_cutoff_and_newer(tmp_path):
    rec = _rec(tmp_path)
    _fill(rec, 8, [1.0])
    _fill(rec, 9, [1.0])
    rec._conn.execute("INSERT INTO device_hourly VALUES ('2026-06-20T08:00:00+00:00', 'sensor.a', 1.0, 1)")
    rec._conn.execute("INSERT INTO device_hourly VALUES ('2026-06-20T09:00:00+00:00', 'sensor.a', 1.0, 1)")
    assert rec.purge_device_samples("2026-06-20T09:00:00+00:00") == 1
    assert rec.purge_device_hourly("2026-06-20T09:00:00+00:00") == 1
    assert _rows(rec, "SELECT count(*) FROM device_samples") == [(1,)]
    assert _rows(rec, "SELECT hour_ts FROM device_hourly") == [("2026-06-20T09:00:00+00:00",)]
    rec.close()


def _read(states, **opts):
    h = StubHass()
    for ent, (st, attrs) in states.items():
        h.set_state(ent, st, attrs)
    return coordinator.read_device_watts(h, opts)


def test_power_sensor_w_kw_and_unavailable():
    out = _read(
        {
            "sensor.w": ("250", {"unit_of_measurement": "W"}),
            "sensor.kw": ("1.5", {"unit_of_measurement": "kW"}),
            "sensor.off": ("unavailable", {}),
        },
        device_power_entities=["sensor.w", "sensor.kw", "sensor.off", "sensor.gone"],
    )
    assert out == {
        "sensor.w": (250.0, None),
        "sensor.kw": (1500.0, None),
        "sensor.off": (None, None),
        "sensor.gone": (None, None),
    }


def test_state_devices():
    out = _read(
        {
            "sensor.oven": ("running", {}),
            "sensor.paused": ("paused", {}),
            "sensor.dead": ("unavailable", {}),
            "sensor.num": ("42", {}),
            "climate.hp": ("heat", {"hvac_action": "heating"}),
            "climate.idle": ("heat", {"hvac_action": "idle"}),
            "climate.bare": ("heat", {}),
        },
        device_state_entities=[
            "sensor.oven",
            "sensor.paused",
            "sensor.dead",
            "sensor.num",
            "climate.hp",
            "climate.idle",
            "climate.bare",
            "sensor.gone",
            "sensor.nowatts",
        ],
        device_state_watts={
            "sensor.oven": 2000,
            "sensor.paused": 2000,
            "sensor.dead": 2000,
            "sensor.num": 2000,
            "climate.hp": 900,
            "climate.idle": 900,
            "climate.bare": 900,
        },
    )
    assert out["sensor.oven"] == (2000.0, "running")
    assert out["sensor.paused"] == (0.0, "paused")
    assert out["sensor.dead"][0] is None
    assert out["sensor.num"][0] is None
    assert out["climate.hp"] == (900.0, "heating")
    assert out["climate.idle"] == (0.0, "idle")
    assert out["climate.bare"][0] is None
    assert out["sensor.gone"] == (None, None)
    assert out["sensor.nowatts"] == (None, None)


def test_state_device_without_wattage_and_power_wins():
    out = _read(
        {"switch.a": ("on", {}), "sensor.p": ("on", {"unit_of_measurement": "W"})},
        device_state_entities=["switch.a", "sensor.p"],
        device_power_entities=["sensor.p"],
        device_state_watts={"sensor.p": 500},
    )
    assert out["switch.a"] == (None, "on")
    assert out["sensor.p"] == (None, None)


def test_state_device_without_hvac_action_warns_once(caplog):
    h = StubHass()
    h.set_state("water_heater.boiler", "eco", {})
    opts = {"device_state_entities": ["water_heater.boiler"], "device_state_watts": {"water_heater.boiler": 2000}}
    with caplog.at_level("WARNING"):
        for _ in range(3):
            assert coordinator.read_device_watts(h, opts)["water_heater.boiler"] == (None, "eco")
    assert len([r for r in caplog.records if "water_heater.boiler" in r.getMessage()]) == 1


@pytest.mark.parametrize("action", ["preheating", "defrosting", "drying"])
def test_more_on_states(action):
    h = StubHass()
    h.set_state("climate.hp", "heat", {"hvac_action": action})
    out = coordinator.read_device_watts(
        h, {"device_state_entities": ["climate.hp"], "device_state_watts": {"climate.hp": 900}}
    )
    assert out["climate.hp"] == (900.0, action)


def test_no_options_means_no_devices():
    assert _read({}) == {}


class _DeviceRecorder(_RollupRecorder):
    def __init__(self, fail=False):
        super().__init__()
        self.device_rows = []
        self.fail = fail

    def wal_checkpoint(self):
        pass

    def append_device_samples(self, ts, rows):
        if self.fail:
            raise RuntimeError("boom")
        self.device_rows.append((ts, rows))

    def rollup_device_hours(self, now_iso):
        if self.fail:
            raise RuntimeError("boom")
        return 0


@pytest.mark.asyncio
async def test_disabled_tick_records_devices_and_exposes_them(monkeypatch):
    rec = _DeviceRecorder()
    hass = StubHass()
    hass.set_state("sensor.pc", "700", {"unit_of_measurement": "W"})
    hass.set_state("sensor.dead", "unavailable")
    ctrl = _make_t6_controller(hass, rec)
    ctrl._data[const.CONF_DEVICE_POWER_ENTITIES] = ["sensor.pc", "sensor.dead"]
    monkeypatch.setattr(controller.dt_util, "utcnow", lambda: _T6_NOW)
    await ctrl.tick()
    assert rec.device_rows[0][1] == {"sensor.pc": (700.0, None), "sensor.dead": (None, None)}
    assert ctrl.last_status["tracked_devices_w"] == 700.0
    s = X1TrackedDevicesSensor(ctrl, "e1")
    assert s.native_value == 700.0
    assert s.extra_state_attributes == {"devices": {"sensor.pc": 700.0, "sensor.dead": None}}
    assert "devices" in s._unrecorded_attributes


@pytest.mark.asyncio
async def test_no_devices_no_write_and_sensor_none(monkeypatch):
    rec = _DeviceRecorder()
    ctrl = _make_t6_controller(StubHass(), rec)
    monkeypatch.setattr(controller.dt_util, "utcnow", lambda: _T6_NOW)
    await ctrl.tick()
    assert rec.device_rows == []
    assert ctrl.last_status["tracked_devices_w"] is None


@pytest.mark.asyncio
async def test_device_recorder_failures_never_fail_the_tick(monkeypatch):
    rec = _DeviceRecorder(fail=True)
    hass = StubHass()
    hass.set_state("sensor.pc", "700")
    ctrl = _make_t6_controller(hass, rec)
    ctrl._data[const.CONF_DEVICE_POWER_ENTITIES] = ["sensor.pc"]
    monkeypatch.setattr(controller.dt_util, "utcnow", lambda: _T6_NOW)
    result = await ctrl.tick()
    assert result["state"] != "failsafe"
    assert len(rec.rollup_calls) == 1


@pytest.mark.asyncio
async def test_read_device_watts_raising_never_fails_the_tick(monkeypatch):
    rec = _DeviceRecorder()
    ctrl = _make_t6_controller(StubHass(), rec)
    monkeypatch.setattr(controller.dt_util, "utcnow", lambda: _T6_NOW)

    def boom(*a):
        raise RuntimeError("boom")

    monkeypatch.setattr(controller.coordinator, "read_device_watts", boom)
    assert (await ctrl.tick())["state"] != "failsafe"


def test_rollup_failure_still_runs_both_purges():
    ctrl = _make_t6_controller(StubHass(), _DeviceRecorder(fail=True))
    calls = []
    ctrl._recorder.purge_device_samples = lambda c: calls.append("raw")
    ctrl._recorder.purge_device_hourly = lambda c: calls.append("hourly")
    ctrl._rollup_hourly_sync("2026-06-20T10:00:00+00:00", "a", "b")
    assert calls == ["raw", "hourly"]


@pytest.mark.asyncio
async def test_enabled_tick_exposes_tracked_devices_sensor():
    from tests.helpers import make_controller

    hass = StubHass()
    hass.set_state("sensor.soc", "50.0")
    hass.set_state("sensor.meter_power", "0.0")
    hass.set_state("sensor.pv_power", "0.0")
    hass.set_state("sensor.battery_power", "0.0")
    hass.set_state("sensor.pc", "700", {"unit_of_measurement": "W"})
    ctrl, _ = make_controller(hass, data_overrides={const.CONF_DEVICE_POWER_ENTITIES: ["sensor.pc"]})
    ctrl.enabled = True
    await ctrl.tick()
    s = X1TrackedDevicesSensor(ctrl, "e1")
    assert s.native_value == 700.0
    assert s.extra_state_attributes == {"devices": {"sensor.pc": 700.0}}
