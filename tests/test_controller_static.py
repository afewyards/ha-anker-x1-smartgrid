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
