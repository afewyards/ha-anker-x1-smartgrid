"""TDD tests for the recorder row shape after the meter-power migration.

The Anker X1 meter reports one signed net-grid scalar instead of derived
per-phase import telemetry, so the recorder row's legacy p1_l1/p1_l2/p1_l3
columns are retired (kept as NULL for schema compatibility) and p1_w now
mirrors PlantInputs.meter_w directly. load_w keeps recording the (now
computed, not sensor-read) house load — either threaded in from the live
tick or, on the disabled path where house_load_w isn't passed, computed by
_record_sample itself via the same _compute_house_load_w formula.
"""

from __future__ import annotations

from datetime import datetime, timezone, UTC

from custom_components.anker_x1_smartgrid import const
from custom_components.anker_x1_smartgrid.controller import Controller
from custom_components.anker_x1_smartgrid.models import PlantInputs
from tests.helpers import StubRecorder as _Recorder

NOW = datetime(2026, 7, 9, 12, 30, tzinfo=UTC)


# _StateObj/_States/_Hass kept local (not migrated to helpers.StubHass): tests
# below drive states via ``hass.states.set(entity_id, state)`` (no attributes
# arg), whereas helpers.StubHass exposes ``hass.set_state(entity_id, state,
# attributes=None)`` at the hass level instead — a different call convention,
# not an equivalent stub.
class _StateObj:
    def __init__(self, state):
        self.state = state


class _States:
    def __init__(self):
        self._states: dict[str, _StateObj] = {}

    def get(self, entity_id):
        return self._states.get(entity_id)

    def set(self, entity_id, state):
        self._states[entity_id] = _StateObj(state)


class _Hass:
    def __init__(self):
        self.states = _States()

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


def _make_ctrl(hass, *, last_house_load_w: float = 0.0) -> Controller:
    """Minimal Controller carrying only what _record_sample touches."""
    ctrl = Controller.__new__(Controller)
    ctrl._hass = hass
    ctrl._data = {
        const.CONF_ENT_PV_POWER: "sensor.pv",
        const.CONF_ENT_BATTERY_POWER: "sensor.batt",
        const.CONF_ENT_INVERTER_LOSS: "sensor.loss",
        const.CONF_ENT_PRICE: "sensor.price",
        const.CONF_ENT_IRRADIANCE: "sensor.irr",
    }
    ctrl._recorder = _Recorder()
    ctrl._last_house_load_w = last_house_load_w
    return ctrl


async def test_recorder_row_uses_meter_w_and_nulls_legacy_phase_columns():
    """Value threaded in from the tick (enabled path) lands verbatim in load_w."""
    hass = _Hass()
    ctrl = _make_ctrl(hass)
    inputs = PlantInputs(soc=55.0, meter_w=321.5, now=NOW)

    await ctrl._record_sample(
        NOW,
        inputs,
        setpoint=0.0,
        state="passive",
        house_load_w=777.0,
    )

    row = ctrl._recorder.rows[0]
    assert row["p1_w"] == 321.5
    assert row["p1_l1"] is None
    assert row["p1_l2"] is None
    assert row["p1_l3"] is None
    assert row["load_w"] == 777.0


async def test_recorder_row_computes_house_load_when_not_threaded_in():
    """Disabled-tick path (house_load_w omitted) computes it via the same formula."""
    hass = _Hass()
    hass.states.set("sensor.pv", "500.0")
    hass.states.set("sensor.batt", "-100.0")
    hass.states.set("sensor.loss", "20.0")
    ctrl = _make_ctrl(hass)
    inputs = PlantInputs(soc=55.0, meter_w=50.0, now=NOW)

    await ctrl._record_sample(NOW, inputs, setpoint=0.0, state="disabled")

    row = ctrl._recorder.rows[0]
    assert row["p1_w"] == 50.0
    assert row["p1_l1"] is None
    assert row["p1_l2"] is None
    assert row["p1_l3"] is None
    assert row["load_w"] == 500.0 + 50.0 + (-100.0) - 20.0


# ---------------------------------------------------------------------------
# Multi-sensor PV list: recorded row's pv_w sums CONF_ENT_PV_POWER when it's
# a list of entity ids (B — controller consumes the normalized list).
# ---------------------------------------------------------------------------


def _make_ctrl_multi_pv(hass, *, last_house_load_w: float = 0.0) -> Controller:
    ctrl = Controller.__new__(Controller)
    ctrl._hass = hass
    ctrl._data = {
        const.CONF_ENT_PV_POWER: ["sensor.pv_1", "sensor.pv_2"],
        const.CONF_ENT_BATTERY_POWER: "sensor.batt",
        const.CONF_ENT_INVERTER_LOSS: "sensor.loss",
        const.CONF_ENT_PRICE: "sensor.price",
        const.CONF_ENT_IRRADIANCE: "sensor.irr",
    }
    ctrl._recorder = _Recorder()
    ctrl._last_house_load_w = last_house_load_w
    return ctrl


async def test_recorder_row_pv_w_sums_multi_sensor_list():
    hass = _Hass()
    hass.states.set("sensor.pv_1", "300.0")
    hass.states.set("sensor.pv_2", "150.0")
    hass.states.set("sensor.batt", "-50.0")
    hass.states.set("sensor.loss", "10.0")
    ctrl = _make_ctrl_multi_pv(hass)
    inputs = PlantInputs(soc=55.0, meter_w=20.0, now=NOW)

    await ctrl._record_sample(NOW, inputs, setpoint=0.0, state="disabled")

    row = ctrl._recorder.rows[0]
    assert row["pv_w"] == 450.0


async def test_recorder_row_pv_w_one_sensor_unavailable_uses_the_other():
    hass = _Hass()
    hass.states.set("sensor.pv_1", "unavailable")
    hass.states.set("sensor.pv_2", "150.0")
    hass.states.set("sensor.batt", "-50.0")
    hass.states.set("sensor.loss", "10.0")
    ctrl = _make_ctrl_multi_pv(hass)
    inputs = PlantInputs(soc=55.0, meter_w=20.0, now=NOW)

    await ctrl._record_sample(NOW, inputs, setpoint=0.0, state="disabled")

    row = ctrl._recorder.rows[0]
    assert row["pv_w"] == 150.0


async def test_recorder_row_pv_w_all_unavailable_is_none():
    hass = _Hass()
    hass.states.set("sensor.pv_1", "unavailable")
    hass.states.set("sensor.pv_2", "unknown")
    hass.states.set("sensor.batt", "0.0")
    hass.states.set("sensor.loss", "0.0")
    ctrl = _make_ctrl_multi_pv(hass)
    inputs = PlantInputs(soc=55.0, meter_w=20.0, now=NOW)

    await ctrl._record_sample(NOW, inputs, setpoint=0.0, state="disabled")

    row = ctrl._recorder.rows[0]
    assert row["pv_w"] is None
