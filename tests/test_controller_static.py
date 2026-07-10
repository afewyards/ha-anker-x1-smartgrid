"""Static-tariff wiring in the controller (export price + recorder tolerance)."""
import pytest

from custom_components.anker_x1_smartgrid import controller, const
from tests.test_controller import _StubHass, _make_controller, _seed_valid_inputs, BASE


def test_resolve_export_price_static_constant():
    ctrl, _ = _make_controller(_StubHass(), data_overrides={
        const.CONF_PRICE_MODE: const.PRICE_MODE_STATIC,
        const.CONF_STATIC_PRICE_EXPORT: 0.12,
    })
    assert ctrl._resolve_export_price() == (0.12, False)


def test_resolve_export_price_static_zero_is_none():
    ctrl, _ = _make_controller(_StubHass(), data_overrides={
        const.CONF_PRICE_MODE: const.PRICE_MODE_STATIC,
        const.CONF_STATIC_PRICE_EXPORT: 0.0,
    })
    assert ctrl._resolve_export_price() == (None, False)


def test_resolve_export_price_sensor_mode_unchanged():
    # Default price_mode=sensor, no export entity configured → (None, False).
    ctrl, _ = _make_controller(_StubHass())
    assert ctrl._resolve_export_price() == (None, False)


@pytest.mark.asyncio
async def test_record_sample_tolerates_absent_price_and_irradiance(monkeypatch):
    monkeypatch.setattr(controller.dt_util, "utcnow", lambda: BASE)
    hass = _StubHass()
    ctrl, _ = _make_controller(hass)
    ctrl.enabled = False
    ctrl._data.pop(const.CONF_ENT_PRICE, None)       # new-install shape (post NL removal)
    ctrl._data.pop(const.CONF_ENT_IRRADIANCE, None)
    _seed_valid_inputs(hass, soc="20.0")
    result = await ctrl.tick()
    assert result["reason"] == "disabled"
    assert ctrl._recorder.rows, "a sample row must have been recorded without KeyError"
    row = ctrl._recorder.rows[-1]
    assert row["import_price"] is None
    assert row["irradiance"] is None


from datetime import timedelta


@pytest.mark.asyncio
async def test_tick_static_mode_zero_price_entities_runs_dp(monkeypatch):
    """Static mode with NO price sensor still ticks (reason ok) and populates a plan."""
    monkeypatch.setattr(controller.dt_util, "utcnow", lambda: BASE)
    hass = _StubHass()
    ctrl, act = _make_controller(hass, data_overrides={
        const.CONF_PRICE_MODE: const.PRICE_MODE_STATIC,
        const.CONF_STATIC_PRICE_IMPORT: 0.30,
        const.CONF_STATIC_PRICE_OFFPEAK: 0.10,
        const.CONF_STATIC_OFFPEAK_HOURS: "01:00-06:00",
        const.CONF_ENT_PRICE: "",          # no dynamic price sensor
        const.CONF_ENT_PV_TODAY: [],
        const.CONF_ENT_PV_TOMORROW: [],
    })
    # Seed plant inputs + sun ONLY — no price forecast entity exists.
    hass.set_state("sensor.soc", "20.0")
    hass.set_state("sensor.meter_power", "0.0")
    hass.set_state("sun.sun", "above_horizon",
                   {"next_setting": (BASE + timedelta(hours=8)).isoformat()})
    hass.set_state("sensor.pv_power", "0.0")
    hass.set_state("sensor.battery_power", "0.0")

    result = await ctrl.tick()

    # NOT failsafe → synth produced slots, all inputs present, DP ran.
    assert result["reason"] == "ok"
    assert ctrl.last_decision, "last_decision must be populated in static mode"
    assert isinstance(ctrl.last_decision["committed_hours"], list)
    # The synthesized horizon carried both tariff levels.
    slots = controller.coordinator.read_price_slots(hass, ctrl._data)
    assert {round(s.price, 2) for s in slots} == {0.30, 0.10}
