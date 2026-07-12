"""Unit tests for controller status sensors."""
from __future__ import annotations

from datetime import timedelta

import pytest

from homeassistant.components.sensor import SensorStateClass

from custom_components.anker_x1_smartgrid import const
from custom_components.anker_x1_smartgrid import controller as ctrl_mod
from tests.helpers import (
    BASE,
    StubHass as _StubHass,
    make_controller as _make_controller,
)


def test_export_setpoint_sensor_reads_key():
    from custom_components.anker_x1_smartgrid.sensor import X1ExportSetpointSensor

    class _C:
        last_status = {"export_setpoint_w": 1500.0}

    s = X1ExportSetpointSensor(_C(), "e")
    assert s.native_value == 1500.0


# ---------------------------------------------------------------------------
# T16/T22: house load sensor — event-driven, reads live HA state directly
# (not last_status / the 60s controller tick).
# ---------------------------------------------------------------------------

_PV_ENT = "sensor.pv_power"
_METER_ENT = "sensor.meter_power"
_BATT_ENT = "sensor.battery_power"
_LOSS_ENT = "sensor.inverter_loss"


class _StubController:
    last_status: dict = {}


def _seed_house_load_states(hass, *, pv="1200.0", meter="100.0", batt="-500.0", loss="30.0"):
    hass.set_state(_PV_ENT, pv)
    hass.set_state(_METER_ENT, meter)
    hass.set_state(_BATT_ENT, batt)
    hass.set_state(_LOSS_ENT, loss)


def _make_house_load_sensor(hass):
    from custom_components.anker_x1_smartgrid.sensor import X1HouseLoadSensor

    s = X1HouseLoadSensor(_StubController(), "e", _PV_ENT, _METER_ENT, _BATT_ENT, _LOSS_ENT)
    s.hass = hass
    return s


def test_house_load_sensor_unique_id_and_attrs():
    s = _make_house_load_sensor(_StubHass())
    assert s._attr_unique_id == "anker_x1_smartgrid_house_load_w"
    assert s._attr_native_unit_of_measurement == "W"
    assert s._attr_state_class == SensorStateClass.MEASUREMENT
    assert s._attr_should_poll is False


def test_house_load_sensor_recomputes_without_controller_tick():
    """Value tracks live source-entity state directly on _recompute() calls
    -- no controller tick / last_status involved at all."""
    hass = _StubHass()
    s = _make_house_load_sensor(hass)
    _seed_house_load_states(hass)

    s._recompute()
    assert s.native_value == 1200.0 + 100.0 + (-500.0) - 30.0

    # A source entity changes -- mirrors what the state-change event handler
    # observes -- and the recomputed value reflects it immediately.
    hass.set_state(_PV_ENT, "1500.0")
    s._recompute()
    assert s.native_value == 1500.0 + 100.0 + (-500.0) - 30.0


@pytest.mark.parametrize("entity", [_PV_ENT, _METER_ENT, _BATT_ENT])
def test_house_load_sensor_any_of_pv_meter_batt_unavailable_is_none(entity):
    hass = _StubHass()
    s = _make_house_load_sensor(hass)
    _seed_house_load_states(hass)
    hass.set_state(entity, "unavailable")

    s._recompute()

    assert s.native_value is None


def test_house_load_sensor_loss_unavailable_treated_as_zero():
    hass = _StubHass()
    s = _make_house_load_sensor(hass)
    _seed_house_load_states(hass)
    hass.set_state(_LOSS_ENT, "unavailable")

    s._recompute()

    assert s.native_value == 1200.0 + 100.0 + (-500.0) - 0.0


def test_house_load_sensor_clamped_at_zero():
    hass = _StubHass()
    s = _make_house_load_sensor(hass)
    _seed_house_load_states(hass, pv="0.0", meter="0.0", batt="-2000.0", loss="0.0")

    s._recompute()

    assert s.native_value == 0.0


def test_house_load_sensor_none_before_any_recompute():
    """Mirrors sibling last_status keys pre-tick: None until first computed."""
    s = _make_house_load_sensor(_StubHass())
    assert s.native_value is None


@pytest.mark.asyncio
async def test_house_load_sensor_subscribes_on_added_to_hass(monkeypatch):
    """async_added_to_hass wires a state-change subscription on all 4 source
    entities and primes the value at add time -- so the sensor is live from
    startup without waiting for the first controller tick."""
    from custom_components.anker_x1_smartgrid import sensor as sensor_mod

    hass = _StubHass()
    s = _make_house_load_sensor(hass)
    _seed_house_load_states(hass)

    captured = {}

    def _fake_track(hass_arg, entity_ids, action):
        captured["hass"] = hass_arg
        captured["entity_ids"] = list(entity_ids)
        captured["action"] = action
        return lambda: None  # unsub

    monkeypatch.setattr(sensor_mod, "async_track_state_change_event", _fake_track)

    await s.async_added_to_hass()

    assert captured["hass"] is hass
    assert captured["entity_ids"] == [_PV_ENT, _METER_ENT, _BATT_ENT, _LOSS_ENT]
    # Primed at add time -- no controller tick required.
    assert s.native_value == 1200.0 + 100.0 + (-500.0) - 30.0

    # Firing the captured handler (the state-change callback) recomputes and
    # writes state, mirroring a live HA state-changed event.
    written = []
    monkeypatch.setattr(s, "async_write_ha_state", lambda: written.append(True))
    hass.set_state(_PV_ENT, "1800.0")
    captured["action"](None)

    assert s.native_value == 1800.0 + 100.0 + (-500.0) - 30.0
    assert written == [True]


# ---------------------------------------------------------------------------
# PV-multi B: house load sensor consumes a normalized list of PV entities
# (const.resolve_pv_power_entities), summed together. Legacy single-string
# construction (above) stays byte-identical.
# ---------------------------------------------------------------------------

_PV_ENT_2 = "sensor.pv_power_2"


def _make_house_load_sensor_multi(hass, pv_entities):
    from custom_components.anker_x1_smartgrid.sensor import X1HouseLoadSensor

    s = X1HouseLoadSensor(_StubController(), "e", pv_entities, _METER_ENT, _BATT_ENT, _LOSS_ENT)
    s.hass = hass
    return s


def test_house_load_sensor_sums_multiple_pv_entities():
    hass = _StubHass()
    s = _make_house_load_sensor_multi(hass, [_PV_ENT, _PV_ENT_2])
    hass.set_state(_PV_ENT, "800.0")
    hass.set_state(_PV_ENT_2, "400.0")
    hass.set_state(_METER_ENT, "100.0")
    hass.set_state(_BATT_ENT, "-500.0")
    hass.set_state(_LOSS_ENT, "30.0")

    s._recompute()

    assert s.native_value == 800.0 + 400.0 + 100.0 + (-500.0) - 30.0


def test_house_load_sensor_multi_pv_one_unavailable_uses_the_other():
    hass = _StubHass()
    s = _make_house_load_sensor_multi(hass, [_PV_ENT, _PV_ENT_2])
    hass.set_state(_PV_ENT, "unavailable")
    hass.set_state(_PV_ENT_2, "400.0")
    hass.set_state(_METER_ENT, "100.0")
    hass.set_state(_BATT_ENT, "-50.0")
    hass.set_state(_LOSS_ENT, "30.0")

    s._recompute()

    assert s.native_value == 400.0 + 100.0 + (-50.0) - 30.0


def test_house_load_sensor_pv_entity_in_kw_converted_before_summing():
    """A PV entity reporting kW (unit_of_measurement="kW") is converted to W
    via coordinator.read_pv_power_w -> read_power_w before summing into the
    house-load computation."""
    hass = _StubHass()
    s = _make_house_load_sensor_multi(hass, [_PV_ENT, _PV_ENT_2])
    hass.set_state(_PV_ENT, "0.8", {"unit_of_measurement": "kW"})
    hass.set_state(_PV_ENT_2, "400.0")
    hass.set_state(_METER_ENT, "100.0")
    hass.set_state(_BATT_ENT, "-500.0")
    hass.set_state(_LOSS_ENT, "30.0")

    s._recompute()

    assert s.native_value == 800.0 + 400.0 + 100.0 + (-500.0) - 30.0


def test_house_load_sensor_multi_pv_all_unavailable_is_none():
    hass = _StubHass()
    s = _make_house_load_sensor_multi(hass, [_PV_ENT, _PV_ENT_2])
    hass.set_state(_PV_ENT, "unavailable")
    hass.set_state(_PV_ENT_2, "unknown")
    hass.set_state(_METER_ENT, "100.0")
    hass.set_state(_BATT_ENT, "-500.0")
    hass.set_state(_LOSS_ENT, "30.0")

    s._recompute()

    assert s.native_value is None


@pytest.mark.asyncio
async def test_house_load_sensor_subscribes_to_all_pv_entities_in_list(monkeypatch):
    """Subscription includes EVERY entity in the normalized PV list, and a
    state change of either one alone triggers a recompute."""
    from custom_components.anker_x1_smartgrid import sensor as sensor_mod

    hass = _StubHass()
    s = _make_house_load_sensor_multi(hass, [_PV_ENT, _PV_ENT_2])
    hass.set_state(_PV_ENT, "800.0")
    hass.set_state(_PV_ENT_2, "400.0")
    hass.set_state(_METER_ENT, "100.0")
    hass.set_state(_BATT_ENT, "-500.0")
    hass.set_state(_LOSS_ENT, "30.0")

    captured = {}

    def _fake_track(hass_arg, entity_ids, action):
        captured["hass"] = hass_arg
        captured["entity_ids"] = list(entity_ids)
        captured["action"] = action
        return lambda: None  # unsub

    monkeypatch.setattr(sensor_mod, "async_track_state_change_event", _fake_track)

    await s.async_added_to_hass()

    assert captured["hass"] is hass
    assert captured["entity_ids"] == [_PV_ENT, _PV_ENT_2, _METER_ENT, _BATT_ENT, _LOSS_ENT]
    assert s.native_value == 800.0 + 400.0 + 100.0 + (-500.0) - 30.0

    written = []
    monkeypatch.setattr(s, "async_write_ha_state", lambda: written.append(True))

    # A state change of only the SECOND pv entity alone triggers a recompute
    # that reflects the new sum.
    hass.set_state(_PV_ENT_2, "600.0")
    captured["action"](None)

    assert s.native_value == 800.0 + 600.0 + 100.0 + (-500.0) - 30.0
    assert written == [True]


# ---------------------------------------------------------------------------
# T16: controller tick() publishes the computed house load into last_status
# ---------------------------------------------------------------------------
# StubActuator/StubStore/StubRecorder/StubHass/make_controller/BASE imported
# from tests.helpers above — this file's local variants were plain/
# byte-identical duplicates. _seed_valid_inputs stays local below: it
# genuinely differs from helpers.seed_valid_inputs (meter_power="100.0" vs
# "0.0", and it seeds sensor.inverter_loss which the shared helper omits) —
# both values are baked into this file's house-load-arithmetic assertions.


def _seed_valid_inputs(hass, *, soc="20.0"):
    """Seed HA states so read_plant_inputs succeeds and tick() reaches "ok"."""
    hass.set_state("sensor.soc", soc)
    hass.set_state("sensor.meter_power", "100.0")
    sunset_iso = (BASE + timedelta(hours=8)).isoformat()
    hass.set_state("sun.sun", "above_horizon", {"next_setting": sunset_iso})
    hass.set_state("sensor.price", "0.05", {
        "forecast": [
            {
                "datetime": (BASE + timedelta(hours=i)).isoformat(),
                "electricity_price": int(0.05 * const.PRICE_SCALE),
            }
            for i in range(9)
        ]
    })
    hass.set_state("sensor.pv_power", "1200.0")
    hass.set_state("sensor.battery_power", "-500.0")
    hass.set_state("sensor.inverter_loss", "30.0")
    hass.set_state("sensor.irradiance", "350.0")
    hass.set_state("weather.home", "cloudy", {"temperature": 18.5})


@pytest.mark.asyncio
async def test_tick_publishes_computed_house_load_into_last_status(monkeypatch):
    """After a successful ("ok") tick, last_status["house_load_w"] equals the
    live per-tick house-load compute: pv + meter_w + batt − inverter_loss."""
    monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
    hass = _StubHass()
    ctrl, _act = _make_controller(hass)
    _seed_valid_inputs(hass, soc="20.0")

    result = await ctrl.tick()

    assert result["reason"] == "ok"
    expected = 1200.0 + 100.0 + (-500.0) - 30.0
    assert result["house_load_w"] == expected
    assert ctrl.last_status.get("house_load_w") == expected


@pytest.mark.asyncio
async def test_tick_house_load_absent_before_any_successful_compute():
    """Mirrors sibling keys (e.g. export_setpoint_w): the failsafe tick path
    never computes house load, so the key is absent from last_status."""
    hass = _StubHass()
    ctrl, _act = _make_controller(hass)
    # sensor.soc left un-seeded → read_plant_inputs returns None → failsafe path.

    result = await ctrl.tick()

    assert result["reason"] == "failsafe"
    assert ctrl.last_status.get("house_load_w") is None
