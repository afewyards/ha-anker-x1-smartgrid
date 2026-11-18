"""Battery charge/export limits: user caps that only ever lower the device-derived ceilings."""

from __future__ import annotations

import pytest
import voluptuous as vol
from homeassistant import config_entries
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.anker_x1_smartgrid import const, energy
from custom_components.anker_x1_smartgrid.config_flow import _options_fields
from custom_components.anker_x1_smartgrid.models import Config
from tests.test_anker_resolver import _register_anker_device
from tests.test_config_flow import _nest

_DEVICE = {const.CONF_MAX_CHARGE_W: 12000.0, const.CONF_MAX_EXPORT_W: 12000.0}


def _markers(defaults):
    return {m.schema: m for m in _options_fields(defaults)}


def test_unset_limits_leave_the_device_values():
    cfg = Config.from_dict({**_DEVICE, const.CONF_BATTERY_CHARGE_LIMIT_W: None})
    assert (cfg.max_charge_w, cfg.grid_charge_max_w, cfg.max_export_w) == (12000.0, 12000.0, 12000.0)


def test_a_lower_limit_wins_and_the_charge_limit_bounds_grid_charging_only():
    cfg = Config.from_dict(
        {**_DEVICE, const.CONF_BATTERY_CHARGE_LIMIT_W: 5000.0, const.CONF_BATTERY_EXPORT_LIMIT_W: 6000.0}
    )
    # Solar keeps the inverter's full rate; only the grid portion is held to the limit.
    assert (cfg.max_charge_w, cfg.grid_charge_max_w, cfg.max_export_w) == (12000.0, 5000.0, 6000.0)
    assert energy.grid_charge_request_w(cfg, 0.0) == 5000.0
    assert energy.export_net_target_w(100.0, cfg) == 6000.0


def test_a_higher_limit_does_not_raise_the_device_value():
    cfg = Config.from_dict(
        {**_DEVICE, const.CONF_BATTERY_CHARGE_LIMIT_W: 15000.0, const.CONF_BATTERY_EXPORT_LIMIT_W: 15000.0}
    )
    assert (cfg.max_charge_w, cfg.grid_charge_max_w, cfg.max_export_w) == (12000.0, 12000.0, 12000.0)


@pytest.mark.parametrize("key", [const.CONF_BATTERY_CHARGE_LIMIT_W, const.CONF_BATTERY_EXPORT_LIMIT_W])
def test_both_limits_reject_zero_and_negative(key):
    validator = next(v for m, v in _options_fields({}).items() if m.schema == key)
    for bad in (-100.0, 0.0, 99.0):
        with pytest.raises(vol.Invalid):
            validator(bad)
    assert validator(100.0) == 100.0


def test_options_form_offers_both_limits_and_leaves_them_clearable():
    markers = _markers({const.CONF_BATTERY_CHARGE_LIMIT_W: 5000.0})
    assert markers[const.CONF_BATTERY_CHARGE_LIMIT_W].description == {"suggested_value": 5000.0}
    assert markers[const.CONF_BATTERY_EXPORT_LIMIT_W].description == {"suggested_value": None}
    # No default: a cleared field is omitted on save, i.e. back to the device value.
    assert markers[const.CONF_BATTERY_CHARGE_LIMIT_W].default is vol.UNDEFINED
    assert markers[const.CONF_BATTERY_EXPORT_LIMIT_W].default is vol.UNDEFINED


async def test_old_grid_export_limit_seeds_the_battery_export_limit_and_survives_a_save(hass):
    """France's stored options (import 11000 / export 6000) keep a 6000 W export cap through upgrade and a UI save."""
    device_id, _ = _register_anker_device(hass, setpoint_state="0", setpoint_attrs={"min": -12000, "max": 12000})
    entry = MockConfigEntry(
        domain=const.DOMAIN,
        data={**const.DEFAULT_ENTITIES, const.CONF_ANKER_DEVICE: device_id},
        options={const.CONF_GRID_IMPORT_LIMIT_W: 11000.0, "grid_export_limit_w": 6000.0},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.options[const.CONF_BATTERY_EXPORT_LIMIT_W] == 6000.0
    assert const.CONF_BATTERY_CHARGE_LIMIT_W not in entry.options
    cfg = hass.data[const.DOMAIN][entry.entry_id]["controller"].cfg
    assert (cfg.max_charge_w, cfg.max_export_w, cfg.grid_import_limit_w) == (12000.0, 6000.0, 11000.0)
    # The form pre-fills the seeded value, so an untouched save submits it.
    stored = _markers({**entry.data, **entry.options})[const.CONF_BATTERY_EXPORT_LIMIT_W]
    assert stored.description == {"suggested_value": 6000.0}

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result2 = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input=_nest({const.CONF_BATTERY_EXPORT_LIMIT_W: 6000.0})
    )
    assert result2["type"] == "create_entry"
    await hass.async_block_till_done()

    assert "grid_export_limit_w" not in entry.options
    assert entry.options[const.CONF_BATTERY_EXPORT_LIMIT_W] == 6000.0
    assert hass.data[const.DOMAIN][entry.entry_id]["controller"].cfg.max_export_w == 6000.0

    # Cleared in the form: the device value applies, and a reload does not bring the limit back.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(result["flow_id"], user_input=_nest({}))
    await hass.async_block_till_done()
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert const.CONF_BATTERY_EXPORT_LIMIT_W not in entry.options
    assert hass.data[const.DOMAIN][entry.entry_id]["controller"].cfg.max_export_w == 12000.0
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def _setup(hass, entry):
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    cfg = hass.data[const.DOMAIN][entry.entry_id]["controller"].cfg
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    return cfg


def _device_entry(hass, **kw):
    device_id, _ = _register_anker_device(hass, setpoint_state="0", setpoint_attrs={"min": -12000, "max": 12000})
    return MockConfigEntry(
        domain=const.DOMAIN, data={**const.DEFAULT_ENTITIES, const.CONF_ANKER_DEVICE: device_id}, **kw
    )


async def test_an_entry_that_never_saved_the_old_export_limit_keeps_its_6000_w_default(hass):
    entry = _device_entry(hass, options={const.CONF_GRID_IMPORT_LIMIT_W: 11000.0})
    cfg = await _setup(hass, entry)
    assert entry.options[const.CONF_BATTERY_EXPORT_LIMIT_W] == 6000.0
    assert entry.minor_version == 2
    assert cfg.max_export_w == 6000.0


async def test_an_already_migrated_entry_is_not_migrated_again(hass):
    entry = _device_entry(hass, minor_version=2, options={"grid_export_limit_w": 6000.0})
    cfg = await _setup(hass, entry)
    assert const.CONF_BATTERY_EXPORT_LIMIT_W not in entry.options
    assert cfg.max_export_w == 12000.0


async def test_a_fresh_install_has_no_battery_export_limit(hass):
    device_id, _ = _register_anker_device(hass, setpoint_state="0", setpoint_attrs={"min": -12000, "max": 12000})
    result = await hass.config_entries.flow.async_init(const.DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={const.CONF_ANKER_DEVICE: device_id}
    )
    await hass.async_block_till_done()
    entry = result["result"]
    assert const.CONF_BATTERY_EXPORT_LIMIT_W not in entry.options
    assert hass.data[const.DOMAIN][entry.entry_id]["controller"].cfg.max_export_w == 12000.0
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
