"""Tests for Anker X1 SmartGrid config flow."""
from homeassistant import config_entries
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import EntitySelector
from pytest_homeassistant_custom_component.common import MockConfigEntry
from custom_components.anker_x1_smartgrid import const
from custom_components.anker_x1_smartgrid.const import DOMAIN
from custom_components.anker_x1_smartgrid.models import Config
from tests.test_anker_resolver import _register_anker_device
from tests.conftest import ANKER_TEST_ENTITIES
from homeassistant.data_entry_flow import section as _section


def _flat_schema_items(schema_obj):
    """{conf_key: (marker, validator)} across sections and top-level keys."""
    items = {}
    for marker, validator in schema_obj.schema.items():
        if isinstance(validator, _section):
            for m2, v2 in validator.schema.schema.items():
                items[m2.schema] = (m2, v2)
        else:
            items[marker.schema] = (marker, validator)
    return items


def _flat_keys(schema_obj):
    return set(_flat_schema_items(schema_obj))


def _flat_markers(schema_obj):
    return {key: mv[0] for key, mv in _flat_schema_items(schema_obj).items()}


def _nest(flat):
    """Group a flat options dict into the section-nested submit shape."""
    from custom_components.anker_x1_smartgrid.config_flow import OPTIONS_SECTIONS
    key_to_section = {
        key: name for name, keys in OPTIONS_SECTIONS.items() for key in keys
    }
    nested = {name: {} for name in OPTIONS_SECTIONS}
    for key, value in flat.items():
        sec = key_to_section.get(key)
        if sec is None:
            nested[key] = value
        else:
            nested[sec][key] = value
    return nested


def _validate_flat(schema_obj, flat):
    """Validate flat input against the sectioned schema; return a flat result."""
    from custom_components.anker_x1_smartgrid.config_flow import _flatten_sections
    return _flatten_sections(schema_obj(_nest(flat)))


def test_sections_cover_all_option_fields():
    from custom_components.anker_x1_smartgrid import config_flow
    fields = config_flow._options_fields({})
    field_keys = {marker.schema for marker in fields}
    section_keys = {k for keys in config_flow.OPTIONS_SECTIONS.values() for k in keys}
    assert field_keys == section_keys
    assert len(section_keys) == 53


def test_options_schema_is_sectioned_devices_expanded():
    from custom_components.anker_x1_smartgrid.config_flow import (
        OPTIONS_SECTIONS, SECTION_DEVICES, _options_schema,
    )
    schema_obj = _options_schema({})
    top = {k.schema: v for k, v in schema_obj.schema.items()}
    assert set(top) == set(OPTIONS_SECTIONS)
    for name, sec in top.items():
        assert isinstance(sec, _section)
        assert sec.options["collapsed"] is (name != SECTION_DEVICES)


def test_options_schema_excludes_device_derived_limits():
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema, _schema
    assert const.CONF_MAX_CHARGE_W not in _flat_keys(_options_schema({}))
    assert const.CONF_MAX_EXPORT_W not in _flat_keys(_options_schema({}))
    install_keys = {k.schema for k in _schema({}).schema}
    assert const.CONF_MAX_CHARGE_W not in install_keys
    assert const.CONF_MAX_EXPORT_W not in install_keys


def test_flatten_sections_roundtrip():
    from custom_components.anker_x1_smartgrid.config_flow import _flatten_sections
    flat = {"soc_floor": 10.0, "soc_target": 90.0, "addon_timeout": 7}
    assert _flatten_sections(_nest(flat)) == flat


async def test_options_flow_saves_flat_options(hass):
    from custom_components.anker_x1_smartgrid.config_flow import OPTIONS_SECTIONS
    entry = await _create_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result2 = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input=_nest({const.CONF_SOC_TARGET: 90.0})
    )
    assert result2["type"] == "create_entry"
    assert entry.options[const.CONF_SOC_TARGET] == 90.0
    assert all(key not in OPTIONS_SECTIONS for key in entry.options)
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_user_flow_creates_entry(hass):
    device_id, _ = _register_anker_device(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == "form"
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={const.CONF_ANKER_DEVICE: device_id}
    )
    assert result2["type"] == "create_entry"
    assert result2["title"] == "Anker X1 SmartGrid"
    assert result2["data"]["soc_target"] == 97.0



async def _create_entry(hass):
    """Helper: run the user flow with a resolved Anker device, return the ConfigEntry."""
    device_id, _ = _register_anker_device(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={const.CONF_ANKER_DEVICE: device_id}
    )
    return result2["result"]


# ---------------------------------------------------------------------------
# P1-T3 — CONF_ENT_WEATHER_FORECAST + CONF_RETENTION_HOURLY_DAYS
# ---------------------------------------------------------------------------

def test_weather_forecast_config_default():
    cfg = Config()
    assert cfg.ent_weather_forecast == "weather.forecast_home"


def test_weather_forecast_config_override():
    cfg = Config.from_dict({"ent_weather_forecast": "weather.other_station"})
    assert cfg.ent_weather_forecast == "weather.other_station"


def test_retention_hourly_days_config_default():
    cfg = Config()
    assert cfg.retention_hourly_days == 730


def test_retention_hourly_days_config_override():
    cfg = Config.from_dict({"retention_hourly_days": 365})
    assert cfg.retention_hourly_days == 365


def test_from_dict_both_new_keys_honored():
    cfg = Config.from_dict({
        "ent_weather_forecast": "weather.custom",
        "retention_hourly_days": 180,
    })
    assert cfg.ent_weather_forecast == "weather.custom"
    assert cfg.retention_hourly_days == 180


def test_schema_includes_weather_forecast():
    from custom_components.anker_x1_smartgrid.config_flow import _schema
    schema_obj = _schema({})
    keys = _flat_keys(schema_obj)
    assert const.CONF_ENT_WEATHER_FORECAST in keys


# ---------------------------------------------------------------------------
# P3-T1 — CONF_ADDON_ENABLED / CONF_ADDON_URL / CONF_ADDON_TIMEOUT
# ---------------------------------------------------------------------------

def test_addon_enabled_config_default():
    cfg = Config()
    assert cfg.addon_enabled is False


def test_addon_enabled_config_override():
    cfg = Config.from_dict({"addon_enabled": True})
    assert cfg.addon_enabled is True


def test_addon_url_config_default():
    cfg = Config()
    assert cfg.addon_url == "http://local-anker_x1_forecast:8099"


def test_addon_url_config_override():
    cfg = Config.from_dict({"addon_url": "http://192.168.1.100:8099"})
    assert cfg.addon_url == "http://192.168.1.100:8099"


def test_addon_timeout_config_default():
    cfg = Config()
    assert cfg.addon_timeout == 5


def test_addon_timeout_config_override():
    cfg = Config.from_dict({"addon_timeout": 30})
    assert cfg.addon_timeout == 30


def test_addon_config_all_defaults_when_absent():
    """from_dict with unrelated keys leaves all addon fields at defaults."""
    cfg = Config.from_dict({"soc_target": 80.0})
    assert cfg.addon_enabled is False
    assert cfg.addon_url == "http://local-anker_x1_forecast:8099"
    assert cfg.addon_timeout == 5


def test_addon_config_all_overrides_honoured():
    cfg = Config.from_dict({
        "addon_enabled": True,
        "addon_url": "http://custom:9000",
        "addon_timeout": 10,
    })
    assert cfg.addon_enabled is True
    assert cfg.addon_url == "http://custom:9000"
    assert cfg.addon_timeout == 10


def test_options_schema_includes_addon_enabled():
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    schema_obj = _options_schema({})
    keys = _flat_keys(schema_obj)
    assert const.CONF_ADDON_ENABLED in keys


def test_options_schema_includes_addon_url():
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    schema_obj = _options_schema({})
    keys = _flat_keys(schema_obj)
    assert const.CONF_ADDON_URL in keys


def test_options_schema_includes_addon_timeout():
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    schema_obj = _options_schema({})
    keys = _flat_keys(schema_obj)
    assert const.CONF_ADDON_TIMEOUT in keys


def test_options_schema_addon_enabled_default():
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    schema_obj = _options_schema({})
    schema_keys = _flat_markers(schema_obj)
    assert schema_keys[const.CONF_ADDON_ENABLED].default() is False


def test_options_schema_addon_url_default():
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    schema_obj = _options_schema({})
    schema_keys = _flat_markers(schema_obj)
    assert schema_keys[const.CONF_ADDON_URL].default() == "http://local-anker_x1_forecast:8099"


def test_options_schema_addon_timeout_default():
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    schema_obj = _options_schema({})
    schema_keys = _flat_markers(schema_obj)
    assert schema_keys[const.CONF_ADDON_TIMEOUT].default() == 5


def test_options_schema_addon_timeout_rejects_below_range():
    """Schema must reject addon_timeout < 1."""
    import pytest
    import voluptuous as vol
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    schema_obj = _options_schema({})
    with pytest.raises(vol.Invalid):
        _validate_flat(schema_obj, {
            const.CONF_ADDON_ENABLED: False,
            const.CONF_ADDON_URL: "http://local-anker_x1_forecast:8099",
            const.CONF_ADDON_TIMEOUT: 0,
        })


def test_options_schema_addon_timeout_rejects_above_range():
    """Schema must reject addon_timeout > 60."""
    import pytest
    import voluptuous as vol
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    schema_obj = _options_schema({})
    with pytest.raises(vol.Invalid):
        _validate_flat(schema_obj, {
            const.CONF_ADDON_ENABLED: False,
            const.CONF_ADDON_URL: "http://local-anker_x1_forecast:8099",
            const.CONF_ADDON_TIMEOUT: 61,
        })


def test_options_schema_addon_roundtrip():
    """All three addon fields must round-trip through the options schema."""
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    schema_obj = _options_schema({})
    result = _validate_flat(schema_obj, {
        const.CONF_ADDON_ENABLED: True,
        const.CONF_ADDON_URL: "http://192.168.1.50:8099",
        const.CONF_ADDON_TIMEOUT: 15,
    })
    assert result[const.CONF_ADDON_ENABLED] is True
    assert result[const.CONF_ADDON_URL] == "http://192.168.1.50:8099"
    assert result[const.CONF_ADDON_TIMEOUT] == 15


# ---------------------------------------------------------------------------
# T0.3 — ent_export_price entity config
# ---------------------------------------------------------------------------

def test_ent_export_price_config_default():
    cfg = Config()
    assert cfg.ent_export_price == const.DEFAULT_ENT_EXPORT_PRICE


def test_ent_export_price_config_override():
    cfg = Config.from_dict({"ent_export_price": "sensor.feed_in"})
    assert cfg.ent_export_price == "sensor.feed_in"


def test_ent_export_price_config_absent_uses_default():
    """from_dict with unrelated keys leaves ent_export_price at default."""
    cfg = Config.from_dict({"soc_target": 80.0})
    assert cfg.ent_export_price == const.DEFAULT_ENT_EXPORT_PRICE


def test_options_schema_includes_ent_export_price():
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    schema_obj = _options_schema({})
    keys = _flat_keys(schema_obj)
    assert const.CONF_ENT_EXPORT_PRICE in keys


def test_options_schema_ent_export_price_suggested_value():
    """Export price uses suggested_value (clearable), not hard default."""
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    schema_obj = _options_schema({"ent_export_price": "sensor.feed_in"})
    schema_keys = _flat_markers(schema_obj)
    key = schema_keys[const.CONF_ENT_EXPORT_PRICE]
    assert key.description["suggested_value"] == "sensor.feed_in"


def test_options_schema_ent_export_price_roundtrip():
    """ent_export_price round-trips through _options_schema validation."""
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    schema_obj = _options_schema({})
    result = _validate_flat(schema_obj, {const.CONF_ENT_EXPORT_PRICE: "sensor.feed_in"})
    assert result[const.CONF_ENT_EXPORT_PRICE] == "sensor.feed_in"


def test_default_entities_includes_ent_export_price():
    """DEFAULT_ENTITIES must carry the ent_export_price key so it lands in entry.data."""
    assert const.CONF_ENT_EXPORT_PRICE in const.DEFAULT_ENTITIES
    assert const.DEFAULT_ENTITIES[const.CONF_ENT_EXPORT_PRICE] == const.DEFAULT_ENT_EXPORT_PRICE


# ---------------------------------------------------------------------------
# House-load field removed — house load is now computed (resolver-managed
# meter/inverter-loss roles), not a user-configurable entity picker.
# ---------------------------------------------------------------------------

def test_house_load_not_in_initial_schema():
    from custom_components.anker_x1_smartgrid.config_flow import _schema
    install_keys = {k.schema for k in _schema({}).schema}
    assert "ent_house_load" not in install_keys


def test_house_load_not_in_options_schema():
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    assert "ent_house_load" not in _flat_keys(_options_schema({}))


def test_meter_power_and_inverter_loss_not_in_any_flow_schema():
    """Resolver-managed Anker roles never appear as pickable flow fields."""
    from custom_components.anker_x1_smartgrid.config_flow import _schema, _options_schema
    install_keys = {k.schema for k in _schema({}).schema}
    options_keys = _flat_keys(_options_schema({}))
    for key in ("ent_meter_power", "ent_inverter_loss"):
        assert key not in install_keys
        assert key not in options_keys


# ---------------------------------------------------------------------------
# Task 5 — CONF_SOC_FLOOR exposed in options flow
# ---------------------------------------------------------------------------

def test_options_schema_exposes_soc_floor():
    """CONF_SOC_FLOOR is settable via the options flow so the live entry can move to 5%."""
    from custom_components.anker_x1_smartgrid import const
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    schema = _options_schema({})
    keys = _flat_keys(schema)
    assert const.CONF_SOC_FLOOR in keys


# ---------------------------------------------------------------------------
# H1 — Export options in options flow
# ---------------------------------------------------------------------------

def test_options_schema_includes_enable_export():
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    schema_obj = _options_schema({})
    keys = _flat_keys(schema_obj)
    assert const.CONF_ENABLE_EXPORT in keys


def test_options_schema_enable_export_default():
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    schema_obj = _options_schema({})
    schema_keys = _flat_markers(schema_obj)
    assert schema_keys[const.CONF_ENABLE_EXPORT].default() is True


def test_options_schema_includes_grid_export_limit_w():
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    schema_obj = _options_schema({})
    keys = _flat_keys(schema_obj)
    assert const.CONF_GRID_EXPORT_LIMIT_W in keys


def test_options_schema_grid_export_limit_w_default():
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    schema_obj = _options_schema({})
    schema_keys = _flat_markers(schema_obj)
    assert schema_keys[const.CONF_GRID_EXPORT_LIMIT_W].default() == const.DEFAULT_GRID_EXPORT_LIMIT_W


def test_options_schema_grid_export_limit_w_roundtrip():
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    schema_obj = _options_schema({})
    result = _validate_flat(schema_obj, {const.CONF_GRID_EXPORT_LIMIT_W: 4500.0})
    assert result[const.CONF_GRID_EXPORT_LIMIT_W] == 4500.0


def test_options_schema_grid_export_limit_w_rejects_negative():
    import pytest
    import voluptuous as vol
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    schema_obj = _options_schema({})
    with pytest.raises(vol.Invalid):
        _validate_flat(schema_obj, {const.CONF_GRID_EXPORT_LIMIT_W: -100.0})


def test_options_schema_includes_cycle_cost_eur_per_kwh():
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    schema_obj = _options_schema({})
    keys = _flat_keys(schema_obj)
    assert const.CONF_CYCLE_COST_EUR_PER_KWH in keys


def test_options_schema_cycle_cost_eur_per_kwh_default():
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    schema_obj = _options_schema({})
    schema_keys = _flat_markers(schema_obj)
    assert schema_keys[const.CONF_CYCLE_COST_EUR_PER_KWH].default() == const.DEFAULT_CYCLE_COST_EUR_PER_KWH


def test_options_schema_cycle_cost_eur_per_kwh_roundtrip():
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    schema_obj = _options_schema({})
    result = _validate_flat(schema_obj, {const.CONF_CYCLE_COST_EUR_PER_KWH: 0.06})
    assert result[const.CONF_CYCLE_COST_EUR_PER_KWH] == 0.06


def test_options_schema_includes_export_dwell_min():
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    schema_obj = _options_schema({})
    keys = _flat_keys(schema_obj)
    assert const.CONF_EXPORT_DWELL_MIN in keys


def test_options_schema_export_dwell_min_default():
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    schema_obj = _options_schema({})
    schema_keys = _flat_markers(schema_obj)
    assert schema_keys[const.CONF_EXPORT_DWELL_MIN].default() == const.DEFAULT_EXPORT_DWELL_MIN


def test_options_schema_export_dwell_min_roundtrip():
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    schema_obj = _options_schema({})
    result = _validate_flat(schema_obj, {const.CONF_EXPORT_DWELL_MIN: 10})
    assert result[const.CONF_EXPORT_DWELL_MIN] == 10


def test_options_schema_includes_export_eps_lo_kwh():
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    schema_obj = _options_schema({})
    keys = _flat_keys(schema_obj)
    assert const.CONF_EXPORT_EPS_LO_KWH in keys


def test_options_schema_export_eps_lo_kwh_default():
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    schema_obj = _options_schema({})
    schema_keys = _flat_markers(schema_obj)
    assert schema_keys[const.CONF_EXPORT_EPS_LO_KWH].default() == const.DEFAULT_EXPORT_EPS_LO_KWH


def test_options_schema_export_eps_lo_kwh_roundtrip():
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    schema_obj = _options_schema({})
    result = _validate_flat(schema_obj, {const.CONF_EXPORT_EPS_LO_KWH: 0.3})
    assert result[const.CONF_EXPORT_EPS_LO_KWH] == 0.3


def test_options_schema_includes_export_eps_hi_kwh():
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    schema_obj = _options_schema({})
    keys = _flat_keys(schema_obj)
    assert const.CONF_EXPORT_EPS_HI_KWH in keys


def test_options_schema_export_eps_hi_kwh_default():
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    schema_obj = _options_schema({})
    schema_keys = _flat_markers(schema_obj)
    assert schema_keys[const.CONF_EXPORT_EPS_HI_KWH].default() == const.DEFAULT_EXPORT_EPS_HI_KWH


def test_options_schema_export_eps_hi_kwh_roundtrip():
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    schema_obj = _options_schema({})
    result = _validate_flat(schema_obj, {const.CONF_EXPORT_EPS_HI_KWH: 0.6})
    assert result[const.CONF_EXPORT_EPS_HI_KWH] == 0.6


def test_options_schema_export_all_roundtrip():
    """All export fields round-trip through _options_schema validation."""
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    schema_obj = _options_schema({})
    result = _validate_flat(schema_obj, {
        const.CONF_ENABLE_EXPORT: False,
        const.CONF_GRID_EXPORT_LIMIT_W: 3500.0,
        const.CONF_CYCLE_COST_EUR_PER_KWH: 0.05,
        const.CONF_EXPORT_DWELL_MIN: 20,
        const.CONF_EXPORT_EPS_LO_KWH: 0.25,
        const.CONF_EXPORT_EPS_HI_KWH: 0.5,
    })
    assert result[const.CONF_ENABLE_EXPORT] is False
    assert result[const.CONF_GRID_EXPORT_LIMIT_W] == 3500.0
    assert result[const.CONF_CYCLE_COST_EUR_PER_KWH] == 0.05
    assert result[const.CONF_EXPORT_DWELL_MIN] == 20
    assert result[const.CONF_EXPORT_EPS_LO_KWH] == 0.25
    assert result[const.CONF_EXPORT_EPS_HI_KWH] == 0.5


def test_config_reflects_export_defaults():
    """Config model exposes export defaults correctly."""
    cfg = Config()
    assert cfg.enable_export is True
    assert cfg.max_export_w == const.DEFAULT_MAX_EXPORT_W
    assert cfg.grid_export_limit_w == const.DEFAULT_GRID_EXPORT_LIMIT_W
    assert cfg.cycle_cost_eur_per_kwh == const.DEFAULT_CYCLE_COST_EUR_PER_KWH
    assert cfg.export_dwell_min == const.DEFAULT_EXPORT_DWELL_MIN
    assert cfg.export_eps_lo_kwh == const.DEFAULT_EXPORT_EPS_LO_KWH
    assert cfg.export_eps_hi_kwh == const.DEFAULT_EXPORT_EPS_HI_KWH


def test_config_reflects_submitted_export_values():
    """Config.from_dict correctly applies submitted export values."""
    cfg = Config.from_dict({
        "enable_export": False,
        "max_export_w": 3000.0,
        "grid_export_limit_w": 2500.0,
        "cycle_cost_eur_per_kwh": 0.06,
        "export_dwell_min": 10,
        "export_eps_lo_kwh": 0.3,
        "export_eps_hi_kwh": 0.6,
    })
    assert cfg.enable_export is False
    assert cfg.max_export_w == 3000.0
    assert cfg.grid_export_limit_w == 2500.0
    assert cfg.cycle_cost_eur_per_kwh == 0.06
    assert cfg.export_dwell_min == 10
    assert cfg.export_eps_lo_kwh == 0.3
    assert cfg.export_eps_hi_kwh == 0.6


# ---------------------------------------------------------------------------
# Task 5 (co-opt) — CONF_EXPORT_FEE_EUR_PER_KWH exposed in options flow
# ---------------------------------------------------------------------------

def test_options_schema_exposes_export_fee():
    from custom_components.anker_x1_smartgrid import const
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    schema = _options_schema({})
    keys = _flat_keys(schema)
    assert const.CONF_EXPORT_FEE_EUR_PER_KWH in keys


# ---------------------------------------------------------------------------
# Task 2 — editable tunables + entity dropdowns in the options flow
# ---------------------------------------------------------------------------

def test_options_schema_includes_editable_tunables():
    """capacity_kwh excluded: it is always-derived (Task 8, finding 4.1) — see
    test_options_schema_excludes_capacity_kwh below."""
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    keys = _flat_keys(_options_schema({}))
    for key in (
        const.CONF_SOC_TARGET,
        const.CONF_ETA_CHARGE,
        const.CONF_USE_LEARNED_MODEL,
        const.CONF_MIN_TRAIN_SAMPLES,
        const.CONF_RETENTION_DAYS,
        const.CONF_RETENTION_HOURLY_DAYS,
    ):
        assert key in keys


def test_options_schema_includes_entity_dropdowns():
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    keys = _flat_keys(_options_schema({}))
    for key in (
        const.CONF_ENT_PV_TODAY,
        const.CONF_ENT_PV_TOMORROW,
        const.CONF_ENT_PV_PEAK_TODAY,
        const.CONF_ENT_PV_PEAK_TOMORROW,
        const.CONF_ENT_WEATHER_FORECAST,
        const.CONF_ENT_PRICE,
        const.CONF_ENT_EXPORT_PRICE,
    ):
        assert key in keys


def test_options_schema_pv_today_roundtrips_a_list():
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    schema_obj = _options_schema({})
    result = _validate_flat(schema_obj, {const.CONF_ENT_PV_TODAY: ["sensor.a", "sensor.b"]})
    assert result[const.CONF_ENT_PV_TODAY] == ["sensor.a", "sensor.b"]


def test_options_schema_soc_target_roundtrips():
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    schema_obj = _options_schema({})
    result = _validate_flat(schema_obj, {const.CONF_SOC_TARGET: 95.0})
    assert result[const.CONF_SOC_TARGET] == 95.0


# ---------------------------------------------------------------------------
# Task 3 — forecast-service picker + derive-on-save
# ---------------------------------------------------------------------------

def test_options_schema_includes_forecast_service_picker():
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    keys = _flat_keys(_options_schema({}))
    assert const.CONF_FORECAST_SERVICE in keys


async def test_options_service_multiselect_derives_and_persists(hass):
    src = MockConfigEntry(domain="open_meteo_solar_forecast", title="Home")
    src.add_to_hass(hass)
    reg = er.async_get(hass)
    for tkey, oid in (
        ("energy_production_today_remaining", "home_energy_production_today_remaining"),
        ("energy_production_tomorrow", "home_energy_production_tomorrow"),
        ("power_highest_peak_time_today", "home_power_highest_peak_time_today"),
        ("power_highest_peak_time_tomorrow", "home_power_highest_peak_time_tomorrow"),
    ):
        reg.async_get_or_create("sensor", src.domain, f"uid_{tkey}", config_entry=src,
                                translation_key=tkey, suggested_object_id=oid)
    entry = await _create_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"], user_input=_nest({const.CONF_FORECAST_SERVICE: [src.entry_id]})
    )
    assert entry.options[const.CONF_ENT_PV_TODAY] == ["sensor.home_energy_production_today_remaining"]
    assert entry.options[const.CONF_ENT_PV_TOMORROW] == ["sensor.home_energy_production_tomorrow"]
    assert entry.options[const.CONF_ENT_PV_PEAK_TODAY] == ["sensor.home_power_highest_peak_time_today"]
    # selection is now PERSISTED (was transient)
    assert entry.options[const.CONF_FORECAST_SERVICE] == [src.entry_id]
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_options_empty_selection_keeps_current_pv(hass):
    entry = await _create_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input=_nest({
            const.CONF_FORECAST_SERVICE: [],
            const.CONF_ENT_PV_TODAY: ["sensor.manual_pv"],
        }),
    )
    assert entry.options[const.CONF_ENT_PV_TODAY] == ["sensor.manual_pv"]
    assert entry.options[const.CONF_FORECAST_SERVICE] == []  # persisted, empty = forget
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_options_missing_peak_drops_peak_list_for_alignment(hass):
    """A source without peak sensors → peak list [] (all-or-nothing), never misaligned."""
    src = MockConfigEntry(domain="open_meteo_solar_forecast", title="Home")
    src.add_to_hass(hass)
    reg = er.async_get(hass)
    for tkey, oid in (
        ("energy_production_today_remaining", "home_energy_production_today_remaining"),
        ("energy_production_tomorrow", "home_energy_production_tomorrow"),
    ):
        reg.async_get_or_create("sensor", src.domain, f"uid_{tkey}", config_entry=src,
                                translation_key=tkey, suggested_object_id=oid)
    entry = await _create_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input=_nest({
            const.CONF_FORECAST_SERVICE: [src.entry_id],
            const.CONF_ENT_PV_PEAK_TODAY: ["sensor.manual_peak"],
        }),
    )
    assert entry.options[const.CONF_ENT_PV_TODAY] == ["sensor.home_energy_production_today_remaining"]
    # peak unresolved → dropped to [] to keep index alignment (replaces submitted manual peak)
    assert entry.options[const.CONF_ENT_PV_PEAK_TODAY] == []
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_options_two_sources_union_energy_and_align_peaks(hass):
    reg = er.async_get(hass)
    srcs = []
    for i in (1, 2):
        s = MockConfigEntry(domain="open_meteo_solar_forecast", title=f"Roof{i}")
        s.add_to_hass(hass)
        for tkey in ("energy_production_today_remaining", "power_highest_peak_time_today"):
            reg.async_get_or_create("sensor", s.domain, f"uid{i}_{tkey}", config_entry=s,
                                    translation_key=tkey, suggested_object_id=f"roof{i}_{tkey}")
        srcs.append(s)
    entry = await _create_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input=_nest({const.CONF_FORECAST_SERVICE: [srcs[0].entry_id, srcs[1].entry_id]}),
    )
    assert entry.options[const.CONF_ENT_PV_TODAY] == [
        "sensor.roof1_energy_production_today_remaining",
        "sensor.roof2_energy_production_today_remaining",
    ]
    assert entry.options[const.CONF_ENT_PV_PEAK_TODAY] == [
        "sensor.roof1_power_highest_peak_time_today",
        "sensor.roof2_power_highest_peak_time_today",
    ]
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_options_unchanged_selection_does_not_rebuild(hass):
    src = MockConfigEntry(domain="open_meteo_solar_forecast", title="Home")
    src.add_to_hass(hass)
    reg = er.async_get(hass)
    reg.async_get_or_create("sensor", src.domain, "uid_today", config_entry=src,
                            translation_key="energy_production_today_remaining",
                            suggested_object_id="home_energy_production_today_remaining")
    entry = await _create_entry(hass)
    r1 = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        r1["flow_id"], user_input=_nest({const.CONF_FORECAST_SERVICE: [src.entry_id]})
    )
    # Re-open, same selection but a hand-edited PV list → NOT rebuilt/clobbered.
    r2 = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        r2["flow_id"],
        user_input=_nest({
            const.CONF_FORECAST_SERVICE: [src.entry_id],
            const.CONF_ENT_PV_TODAY: ["sensor.hand_added"],
        }),
    )
    assert entry.options[const.CONF_ENT_PV_TODAY] == ["sensor.hand_added"]
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def test_options_schema_forecast_service_is_multiselect_no_sentinel():
    from homeassistant.helpers.selector import SelectSelector
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    schema = _options_schema({})
    _marker, selector = _flat_schema_items(schema)[const.CONF_FORECAST_SERVICE]
    assert isinstance(selector, SelectSelector)
    assert selector.config["multiple"] is True
    assert all(o["value"] != "__keep__" for o in selector.config["options"])


def test_options_schema_stored_selection_filtered_to_available():
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    # stored has a gone id + a live one; only the live one survives as suggested_value
    services = [("live-id", "Roof", "open_meteo_solar_forecast")]
    schema = _options_schema({const.CONF_FORECAST_SERVICE: ["gone-id", "live-id"]}, services=services)
    marker, _selector = _flat_schema_items(schema)[const.CONF_FORECAST_SERVICE]
    assert marker.description["suggested_value"] == ["live-id"]


# ---------------------------------------------------------------------------
# Task 1: Part B — Entity selector alignment
# ---------------------------------------------------------------------------

def test_export_price_uses_entity_selector():
    """CONF_ENT_EXPORT_PRICE should render as an EntitySelector, not cv.string."""
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    schema = _options_schema({})
    _marker, validator = _flat_schema_items(schema)["ent_export_price"]
    assert isinstance(validator, EntitySelector)


# ---------------------------------------------------------------------------
# Task 2A — Dropdown label format + sentinel rename
# ---------------------------------------------------------------------------

def test_options_schema_forecast_service_label_includes_integration_name():
    """Service option labels must be '{title} — {IntegrationName}'."""
    from homeassistant.helpers.selector import SelectSelector
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    services = [("entry-123", "My Roof", "open_meteo_solar_forecast")]
    schema = _options_schema({}, services=services)
    _marker, selector = _flat_schema_items(schema)[const.CONF_FORECAST_SERVICE]
    assert isinstance(selector, SelectSelector)
    opt = next(o for o in selector.config["options"] if o["value"] == "entry-123")
    assert opt["label"] == "My Roof — Open-Meteo"


# ---------------------------------------------------------------------------
# Anker device picker — schema presence
# ---------------------------------------------------------------------------

def test_user_schema_includes_anker_device():
    from homeassistant.helpers.selector import DeviceSelector
    from custom_components.anker_x1_smartgrid.config_flow import _schema
    schema = _schema({})
    key = next(k for k in schema.schema if k.schema == const.CONF_ANKER_DEVICE)
    assert isinstance(schema.schema[key], DeviceSelector)


def test_options_schema_includes_anker_device():
    from homeassistant.helpers.selector import DeviceSelector
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    schema = _options_schema({})
    _marker, selector = _flat_schema_items(schema)[const.CONF_ANKER_DEVICE]
    assert isinstance(selector, DeviceSelector)


def test_anker_device_selector_filters_integration():
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    schema = _options_schema({})
    _marker, selector = _flat_schema_items(schema)[const.CONF_ANKER_DEVICE]
    assert selector.config["integration"] == const.ANKER_X1_DOMAIN


# ---------------------------------------------------------------------------
# Anker device picker — flow wiring (Task 3)
# ---------------------------------------------------------------------------

async def test_user_flow_with_device_resolves_into_data(hass):
    device_id, _ = _register_anker_device(hass, capacity_state="15.0")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={const.CONF_ANKER_DEVICE: device_id}
    )
    assert result2["type"] == "create_entry"
    data = result2["data"]
    assert data[const.CONF_ENT_SOC] == "sensor.anker_x1_battery_soc"
    assert data[const.CONF_ENT_SETPOINT] == "number.anker_x1_battery_setpoint_charge_discharge"
    assert data[const.CONF_ENT_ENGAGE] == "switch.anker_x1_modbus_control_hand_battery_to_ha_vpp"
    assert data[const.CONF_CAPACITY_KWH] == 15.0
    assert data[const.CONF_ANKER_DEVICE] == device_id


async def test_user_flow_missing_role_shows_error(hass):
    device_id, _ = _register_anker_device(hass, drop=("soc",))
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={const.CONF_ANKER_DEVICE: device_id}
    )
    assert result2["type"] == "form"
    assert result2["errors"] == {"base": "anker_roles_missing"}


def test_user_schema_marks_anker_device_required():
    """Setup requires an Anker device so the 5 roles always resolve."""
    import voluptuous as vol
    from custom_components.anker_x1_smartgrid.config_flow import _schema
    schema = _schema({})
    key = next(k for k in schema.schema if k.schema == const.CONF_ANKER_DEVICE)
    assert isinstance(key, vol.Required)


async def test_user_flow_no_device_blocks_creation(hass):
    """Submitting setup without a device must not create an entry missing the 5
    Anker roles. The Required field is enforced by HA on every flow-manager
    submit (UI + REST/WS), surfacing as InvalidData rather than an entry."""
    import pytest
    from homeassistant.data_entry_flow import InvalidData

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={}
        )


async def test_options_flow_device_resolves_into_options(hass):
    device_id, _ = _register_anker_device(hass)
    # Entry starts WITHOUT an anker device, so picking one in options is a real
    # change that triggers re-resolution into entry.options. (Setup now requires
    # a device, so this device-less starting state is built directly.)
    entry = MockConfigEntry(
        domain=DOMAIN, data={**const.DEFAULT_ENTITIES, **ANKER_TEST_ENTITIES}
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"], user_input=_nest({const.CONF_ANKER_DEVICE: device_id})
    )
    assert entry.options[const.CONF_ENT_SOC] == "sensor.anker_x1_battery_soc"
    assert entry.options[const.CONF_ANKER_DEVICE] == device_id
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_options_flow_no_device_no_error_no_resolution(hass):
    entry = await _create_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    res2 = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input=_nest({const.CONF_SOC_FLOOR: 8.0})
    )
    assert res2["type"] == "create_entry"
    assert const.CONF_ENT_SOC not in entry.options  # not a schema field; not resolved
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# Task 8 — drop inert capacity option (review finding 4.1)
#
# Capacity is ALWAYS-DERIVED from the Anker nominal-capacity sensor via
# anker_resolver.resolve_anker_config (install-time, and re-resolved
# in-memory on every reload). The options-flow field was inert: voluptuous
# re-fills it from the current merged value on every save, so a "user
# override" was indistinguishable from the resolver-derived default.
# ---------------------------------------------------------------------------

def test_options_schema_excludes_capacity_kwh():
    """The options flow no longer exposes capacity_kwh — it was inert."""
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    keys = _flat_keys(_options_schema({}))
    assert const.CONF_CAPACITY_KWH not in keys


# ---------------------------------------------------------------------------
# Task 9 — schema range bounds + cross-field soc_floor<soc_target check
# (review finding 4.2)
# ---------------------------------------------------------------------------

async def test_options_flow_rejects_soc_floor_above_target(hass):
    """Same cross-field guard applies to the options flow."""
    entry = await _create_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result2 = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input=_nest({
            const.CONF_SOC_FLOOR: 30.0,
            const.CONF_SOC_TARGET: 25.0,
        }),
    )
    assert result2["type"] == "form"
    assert result2["errors"] == {"base": "soc_floor_above_target"}
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def test_options_schema_range_bounds_on_soc_floor_target_eta():
    """Range guards live in the options schema now (install form lost the fields)."""
    import pytest
    import voluptuous as vol
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    schema_obj = _options_schema({})
    with pytest.raises(vol.Invalid):
        _validate_flat(schema_obj, {const.CONF_SOC_FLOOR: 51.0})
    with pytest.raises(vol.Invalid):
        _validate_flat(schema_obj, {const.CONF_SOC_TARGET: 101.0})
    with pytest.raises(vol.Invalid):
        _validate_flat(schema_obj, {const.CONF_ETA_CHARGE: 1.5})
    ok = _validate_flat(schema_obj, {const.CONF_SOC_FLOOR: 10.0, const.CONF_SOC_TARGET: 90.0})
    assert ok[const.CONF_SOC_FLOOR] == 10.0


async def test_controller_warns_when_soc_floor_above_firmware_floor(hass, caplog):
    """A soc_floor above the firmware 5% floor means the DP prices the
    [firmware, soc_floor] band as phantom grid imports — Controller should
    warn about that on construction (review 4.2)."""
    import logging
    from unittest.mock import MagicMock
    from custom_components.anker_x1_smartgrid.controller import Controller

    data = dict(const.DEFAULT_ENTITIES)
    data[const.CONF_SOC_FLOOR] = 15.0
    with caplog.at_level(logging.WARNING):
        Controller(
            hass=hass,
            data=data,
            recorder=MagicMock(),
            actuator=MagicMock(),
            store=MagicMock(),
        )
    assert any(
        "firmware" in record.message.lower() and "5" in record.message
        for record in caplog.records
    )


# ---------------------------------------------------------------------------
# Task 16 — un-filter price pickers + expose round_trip_eff in options
# ---------------------------------------------------------------------------

def test_price_selectors_do_not_filter_by_monetary_device_class():
    """€/kWh tariff sensors (e.g. Zonneplan) carry no device_class, so the
    'monetary' filter used to hide them from the picker. Both price
    selectors must render as plain sensor-domain EntitySelectors now."""
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    schema = _options_schema({})
    for conf_key in (const.CONF_ENT_PRICE, const.CONF_ENT_EXPORT_PRICE):
        _marker, selector = _flat_schema_items(schema)[conf_key]
        assert isinstance(selector, EntitySelector)
        assert selector.config.get("device_class") is None
        assert selector.config.get("domain") == ["sensor"]


def test_options_schema_includes_round_trip_eff():
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    schema_obj = _options_schema({})
    keys = _flat_keys(schema_obj)
    assert const.CONF_ROUND_TRIP_EFF in keys


def test_options_schema_round_trip_eff_default():
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    schema_obj = _options_schema({})
    schema_keys = _flat_markers(schema_obj)
    assert schema_keys[const.CONF_ROUND_TRIP_EFF].default() == const.DEFAULT_ROUND_TRIP_EFF


def test_options_schema_round_trip_eff_rejects_above_range():
    import pytest
    import voluptuous as vol
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    schema_obj = _options_schema({})
    with pytest.raises(vol.Invalid):
        _validate_flat(schema_obj, {const.CONF_ROUND_TRIP_EFF: 1.5})


def test_options_schema_round_trip_eff_accepts_in_range():
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    schema_obj = _options_schema({})
    result = _validate_flat(schema_obj, {const.CONF_ROUND_TRIP_EFF: 0.85})
    assert result[const.CONF_ROUND_TRIP_EFF] == 0.85


async def test_controller_no_warning_at_default_soc_floor(hass, caplog):
    """No warning when soc_floor stays at (or below) the firmware floor."""
    import logging
    from unittest.mock import MagicMock
    from custom_components.anker_x1_smartgrid.controller import Controller

    data = dict(const.DEFAULT_ENTITIES)
    with caplog.at_level(logging.WARNING):
        Controller(
            hass=hass,
            data=data,
            recorder=MagicMock(),
            actuator=MagicMock(),
            store=MagicMock(),
        )
    assert not any("firmware" in record.message.lower() for record in caplog.records)


def test_forecast_service_helper_text_present():
    """Verify helper text for forecast_service exists in BOTH the config step and the options section."""
    import json
    import pathlib

    base = pathlib.Path("custom_components/anker_x1_smartgrid")
    for rel in ("strings.json", "translations/en.json"):
        data = json.loads((base / rel).read_text())
        opt_desc = data["options"]["step"]["init"]["sections"]["solar_forecast"][
            "data_description"]["forecast_service"]
        cfg_desc = data["config"]["step"]["user"]["data_description"]["forecast_service"]
        for desc in (opt_desc, cfg_desc):
            assert "different arrays" in desc and "summed" in desc


def test_strings_cover_all_option_sections_and_fields():
    import json
    import pathlib
    from custom_components.anker_x1_smartgrid.config_flow import OPTIONS_SECTIONS

    strings = json.loads(pathlib.Path(
        "custom_components/anker_x1_smartgrid/strings.json").read_text())
    sections = strings["options"]["step"]["init"]["sections"]
    assert set(sections) == set(OPTIONS_SECTIONS)
    for name, keys in OPTIONS_SECTIONS.items():
        assert sections[name]["name"], name
        assert sections[name]["description"], name
        assert set(sections[name]["data"]) == set(keys), name
        assert set(sections[name]["data_description"]) == set(keys), name


def test_strings_cover_install_fields():
    import json
    import pathlib
    from custom_components.anker_x1_smartgrid.config_flow import _schema

    strings = json.loads(pathlib.Path(
        "custom_components/anker_x1_smartgrid/strings.json").read_text())
    user = strings["config"]["step"]["user"]
    form_keys = {k.schema for k in _schema({}).schema}
    assert set(user["data"]) == form_keys
    assert set(user["data_description"]) == form_keys
    assert "anker_roles_missing" in strings["config"]["error"]
    assert "soc_floor_above_target" in strings["options"]["error"]


def test_en_json_matches_strings_json():
    import json
    import pathlib

    base = pathlib.Path("custom_components/anker_x1_smartgrid")
    strings = json.loads((base / "strings.json").read_text())
    en = json.loads((base / "translations/en.json").read_text())
    assert en == strings


def test_options_schema_exposes_person_entities():
    from custom_components.anker_x1_smartgrid import const
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    schema = _options_schema({})
    keys = _flat_keys(schema)
    assert const.CONF_PERSON_ENTITIES in keys


def test_options_schema_person_entities_round_trips_selection():
    from custom_components.anker_x1_smartgrid import const
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    chosen = ["person.alice", "person.bob"]
    schema = _options_schema({const.CONF_PERSON_ENTITIES: chosen})
    # suggested_value is carried in the marker description for the person key
    marker, _selector = _flat_schema_items(schema)[const.CONF_PERSON_ENTITIES]
    assert marker.description == {"suggested_value": chosen}


# ---------------------------------------------------------------------------
# Task 3 — shrink install flow to device + core entities
# ---------------------------------------------------------------------------

def test_install_schema_is_minimal_core_fields():
    from custom_components.anker_x1_smartgrid.config_flow import _schema
    keys = {k.schema for k in _schema({}).schema}
    assert keys == {
        const.CONF_ANKER_DEVICE,
        const.CONF_ENT_PRICE,
        const.CONF_ENT_WEATHER_FORECAST,
        const.CONF_FORECAST_SERVICE,
    }


async def test_user_flow_derives_pv_from_forecast_service(hass):
    src = MockConfigEntry(domain="open_meteo_solar_forecast", title="Home")
    src.add_to_hass(hass)
    reg = er.async_get(hass)
    for tkey, oid in (
        ("energy_production_today_remaining", "home_energy_production_today_remaining"),
        ("energy_production_tomorrow", "home_energy_production_tomorrow"),
        ("power_highest_peak_time_today", "home_power_highest_peak_time_today"),
        ("power_highest_peak_time_tomorrow", "home_power_highest_peak_time_tomorrow"),
    ):
        reg.async_get_or_create("sensor", src.domain, f"uid_{tkey}", config_entry=src,
                                translation_key=tkey, suggested_object_id=oid)
    device_id, _ = _register_anker_device(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            const.CONF_ANKER_DEVICE: device_id,
            const.CONF_FORECAST_SERVICE: [src.entry_id],
        },
    )
    assert result2["type"] == "create_entry"
    assert result2["data"][const.CONF_ENT_PV_TODAY] == [
        "sensor.home_energy_production_today_remaining"
    ]
    assert result2["data"][const.CONF_FORECAST_SERVICE] == [src.entry_id]


def test_options_schema_includes_static_price_fields():
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    keys = _flat_keys(_options_schema({}))
    for k in (
        const.CONF_PRICE_MODE,
        const.CONF_STATIC_PRICE_IMPORT,
        const.CONF_STATIC_PRICE_OFFPEAK,
        const.CONF_STATIC_OFFPEAK_HOURS,
        const.CONF_STATIC_PRICE_EXPORT,
    ):
        assert k in keys


async def test_options_static_mode_requires_positive_import(hass):
    entry = await _create_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result2 = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input=_nest({
            const.CONF_PRICE_MODE: const.PRICE_MODE_STATIC,
            const.CONF_STATIC_PRICE_IMPORT: 0.0,
        }),
    )
    assert result2["type"] == "form"
    assert result2["errors"]["base"] == "static_import_price_required"


async def test_options_static_offpeak_hours_must_parse(hass):
    entry = await _create_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result2 = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input=_nest({
            const.CONF_PRICE_MODE: const.PRICE_MODE_STATIC,
            const.CONF_STATIC_PRICE_IMPORT: 0.30,
            const.CONF_STATIC_PRICE_OFFPEAK: 0.10,
            const.CONF_STATIC_OFFPEAK_HOURS: "25:00-07:00",
        }),
    )
    assert result2["type"] == "form"
    assert result2["errors"]["base"] == "static_offpeak_hours_invalid"


async def test_options_static_offpeak_requires_positive_price(hass):
    entry = await _create_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result2 = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input=_nest({
            const.CONF_PRICE_MODE: const.PRICE_MODE_STATIC,
            const.CONF_STATIC_PRICE_IMPORT: 0.30,
            const.CONF_STATIC_PRICE_OFFPEAK: 0.0,
            const.CONF_STATIC_OFFPEAK_HOURS: "01:00-06:00",
        }),
    )
    assert result2["type"] == "form"
    assert result2["errors"]["base"] == "static_offpeak_price_required"


async def test_options_static_mode_valid_saves(hass):
    entry = await _create_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result2 = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input=_nest({
            const.CONF_PRICE_MODE: const.PRICE_MODE_STATIC,
            const.CONF_STATIC_PRICE_IMPORT: 0.30,
            const.CONF_STATIC_PRICE_OFFPEAK: 0.10,
            const.CONF_STATIC_OFFPEAK_HOURS: "01:00-06:00",
        }),
    )
    assert result2["type"] == "create_entry"
    assert entry.options[const.CONF_PRICE_MODE] == const.PRICE_MODE_STATIC
    assert entry.options[const.CONF_STATIC_PRICE_IMPORT] == 0.30
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_options_sensor_mode_ignores_static_fields(hass):
    """Default price_mode=sensor: a zero static_price_import does NOT error."""
    entry = await _create_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result2 = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input=_nest({
            const.CONF_PRICE_MODE: const.PRICE_MODE_SENSOR,
            const.CONF_STATIC_PRICE_IMPORT: 0.0,
        }),
    )
    assert result2["type"] == "create_entry"
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
