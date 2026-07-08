"""TDD tests for H2: recorder writes and purges must run off the HA event loop.

Task 2 of Batch-1 safety fixes.
"""
from datetime import datetime, timedelta, timezone

from custom_components.anker_x1_smartgrid import const
from custom_components.anker_x1_smartgrid import controller as ctrl_mod
from custom_components.anker_x1_smartgrid.models import PlantInputs
from tests.test_controller_export_executor import (
    _StubHass, _make_controller, _patched_compute_decision,
)

PURGE_BASE = datetime(2026, 6, 25, 18, 0, tzinfo=timezone.utc)  # 18 % 6 == 0 → purge fires


async def test_record_sample_is_async_and_appends_via_executor():
    """_record_sample is a coroutine and the blocking append runs off-loop."""
    executor_fns: list[str] = []
    rows: list[dict] = []

    class _States:
        def get(self, entity_id):
            return None

    class _Hass:
        states = _States()

        async def async_add_executor_job(self, fn, *args):
            executor_fns.append(getattr(fn, "__name__", repr(fn)))
            return fn(*args)

    class _Rec:
        def append(self, row):
            rows.append(row)

    from custom_components.anker_x1_smartgrid.controller import Controller
    ctl = Controller.__new__(Controller)
    ctl._hass = _Hass()
    ctl._data = {
        const.CONF_ENT_PV_POWER: "sensor.pv",
        const.CONF_ENT_BATTERY_POWER: "sensor.batt",
        const.CONF_ENT_PRICE: "sensor.price",
        const.CONF_ENT_IRRADIANCE: "sensor.irr",
    }
    ctl._recorder = _Rec()
    now = datetime(2026, 6, 22, 10, 30, tzinfo=timezone.utc)
    inputs = PlantInputs(soc=50.0, phase_import_w=(0.0, 0.0, 0.0), now=now)

    await ctl._record_sample(now, inputs, setpoint=0.0, state="passive", weather_entry=None)

    assert rows and rows[0]["state"] == "passive"   # the write landed
    assert "append" in executor_fns                 # routed off the event loop


class _ExecutorSpyHass(_StubHass):
    def __init__(self):
        super().__init__()
        self.executor_fns: list[str] = []

    async def async_add_executor_job(self, fn, *args):
        self.executor_fns.append(getattr(fn, "__name__", repr(fn)))
        return fn(*args)


def _seed_purge_scenario(hass):
    hass.set_state("sensor.soc", "80.0")
    hass.set_state("sensor.phase_l1", "100.0")
    hass.set_state("sensor.phase_l2", "100.0")
    hass.set_state("sensor.phase_l3", "100.0")
    hass.set_state("sensor.pv_power", "0.0")
    hass.set_state("sensor.battery_power", "0.0")
    hass.set_state("sensor.irradiance", "0.0")
    hass.set_state("weather.home", "clear", {"temperature": 18.0})
    hass.set_state("sensor.export_price", "0.10")
    hass.set_state("sun.sun", "above_horizon",
                   {"next_setting": (PURGE_BASE + timedelta(hours=4)).isoformat()})
    hour0 = PURGE_BASE.replace(minute=0, second=0, microsecond=0)
    hass.set_state("sensor.price", "0.30", {
        "forecast": [
            {"datetime": (hour0 + timedelta(hours=i)).isoformat(),
             "electricity_price": int(0.30 * const.PRICE_SCALE)}
            for i in range(12)
        ]
    })


async def test_purges_run_via_executor(monkeypatch):
    """The 6-hourly purge_older_than + purge_decisions_older_than run off-loop."""
    monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: PURGE_BASE)
    monkeypatch.setattr(
        ctrl_mod, "compute_decision", _patched_compute_decision(export_request={})
    )
    hass = _ExecutorSpyHass()
    ctrl, _act, _store = _make_controller(hass)
    ctrl.enabled = True
    ctrl._last_purge_hour = -1
    _seed_purge_scenario(hass)

    await ctrl.tick()

    assert "append" in hass.executor_fns
    assert "purge_older_than" in hass.executor_fns
    assert "purge_decisions_older_than" in hass.executor_fns
