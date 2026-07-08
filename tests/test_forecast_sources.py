"""Tests for forecast_sources.py."""
from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.anker_x1_smartgrid import forecast_sources


def _register(hass, entry, translation_key, object_id):
    er.async_get(hass).async_get_or_create(
        "sensor",
        entry.domain,
        f"uid_{translation_key}",
        config_entry=entry,
        translation_key=translation_key,
        suggested_object_id=object_id,
    )


async def test_list_forecast_services_filters_supported_enabled(hass):
    e1 = MockConfigEntry(domain="open_meteo_solar_forecast", title="Home")
    e1.add_to_hass(hass)
    e2 = MockConfigEntry(domain="zonneplan", title="Zonneplan")
    e2.add_to_hass(hass)
    services = forecast_sources.list_forecast_services(hass)
    ids = {entry_id for entry_id, _title, _domain in services}
    assert e1.entry_id in ids
    assert e2.entry_id not in ids


async def test_derive_pv_entities_matches_by_translation_key(hass):
    entry = MockConfigEntry(domain="open_meteo_solar_forecast", title="Home")
    entry.add_to_hass(hass)
    _register(hass, entry, "energy_production_today_remaining", "home_energy_production_today_remaining")
    _register(hass, entry, "energy_production_tomorrow", "home_energy_production_tomorrow")
    _register(hass, entry, "power_highest_peak_time_today", "home_power_highest_peak_time_today")
    _register(hass, entry, "power_highest_peak_time_tomorrow", "home_power_highest_peak_time_tomorrow")
    result = forecast_sources.derive_pv_entities(hass, entry.entry_id)
    assert result["today"] == "sensor.home_energy_production_today_remaining"
    assert result["tomorrow"] == "sensor.home_energy_production_tomorrow"
    assert result["peak_today"] == "sensor.home_power_highest_peak_time_today"
    assert result["peak_tomorrow"] == "sensor.home_power_highest_peak_time_tomorrow"


async def test_derive_pv_entities_unresolved_role_is_none(hass):
    entry = MockConfigEntry(domain="open_meteo_solar_forecast", title="Home")
    entry.add_to_hass(hass)
    _register(hass, entry, "energy_production_today_remaining", "home_energy_production_today_remaining")
    result = forecast_sources.derive_pv_entities(hass, entry.entry_id)
    assert result["today"] == "sensor.home_energy_production_today_remaining"
    assert result["tomorrow"] is None
    assert result["peak_today"] is None
    assert result["peak_tomorrow"] is None


async def test_derive_pv_entities_unknown_domain_returns_empty(hass):
    entry = MockConfigEntry(domain="zonneplan", title="Z")
    entry.add_to_hass(hass)
    assert forecast_sources.derive_pv_entities(hass, entry.entry_id) == {}


async def test_derive_pv_entities_entity_id_suffix_fallback(hass):
    """Entities with translation_key=None are matched by entity_id suffix."""
    entry = MockConfigEntry(domain="open_meteo_solar_forecast", title="Home")
    entry.add_to_hass(hass)
    er.async_get(hass).async_get_or_create(
        "sensor",
        entry.domain,
        "uid_suffix_test",
        config_entry=entry,
        translation_key=None,
        suggested_object_id="foo_energy_production_today_remaining",
    )
    result = forecast_sources.derive_pv_entities(hass, entry.entry_id)
    assert result["today"] == "sensor.foo_energy_production_today_remaining"
    assert result["tomorrow"] is None


async def test_list_forecast_services_excludes_disabled(hass):
    """Disabled entries must NOT appear in list_forecast_services."""
    entry = MockConfigEntry(
        domain="open_meteo_solar_forecast",
        title="Disabled",
        disabled_by=ConfigEntryDisabler.USER,
    )
    entry.add_to_hass(hass)
    services = forecast_sources.list_forecast_services(hass)
    ids = {entry_id for entry_id, _title, _domain in services}
    assert entry.entry_id not in ids


# ---------------------------------------------------------------------------
# Task 2A — FORECAST_SOURCE_MAP new shape + Solcast
# ---------------------------------------------------------------------------

def test_forecast_source_map_has_name():
    """Every domain in FORECAST_SOURCE_MAP must have a 'name' key."""
    for domain, info in forecast_sources.FORECAST_SOURCE_MAP.items():
        assert "name" in info, f"{domain} missing 'name'"
        assert "keys" in info, f"{domain} missing 'keys'"


async def test_solcast_derive_pv_entities(hass):
    """Solcast uses its own translation_keys, distinct from shared keys."""
    entry = MockConfigEntry(domain="solcast_solar", title="Rooftop")
    entry.add_to_hass(hass)
    _register(hass, entry, "forecast_remaining_today", "rooftop_forecast_remaining_today")
    _register(hass, entry, "total_kwh_forecast_tomorrow", "rooftop_total_kwh_forecast_tomorrow")
    _register(hass, entry, "peak_w_time_today", "rooftop_peak_w_time_today")
    _register(hass, entry, "peak_w_time_tomorrow", "rooftop_peak_w_time_tomorrow")
    result = forecast_sources.derive_pv_entities(hass, entry.entry_id)
    assert result["today"] == "sensor.rooftop_forecast_remaining_today"
    assert result["tomorrow"] == "sensor.rooftop_total_kwh_forecast_tomorrow"
    assert result["peak_today"] == "sensor.rooftop_peak_w_time_today"
    assert result["peak_tomorrow"] == "sensor.rooftop_peak_w_time_tomorrow"


async def test_list_forecast_services_includes_solcast(hass):
    entry = MockConfigEntry(domain="solcast_solar", title="Rooftop")
    entry.add_to_hass(hass)
    services = forecast_sources.list_forecast_services(hass)
    ids = {entry_id for entry_id, _title, _domain in services}
    assert entry.entry_id in ids


# ---------------------------------------------------------------------------
# ha_solcast_fusion (blended Solcast + Open-Meteo) — uses the shared keys
# ---------------------------------------------------------------------------

async def test_ha_solcast_fusion_derive_pv_entities(hass):
    """ha_solcast_fusion reuses the shared forecast_solar/open-meteo translation_keys."""
    entry = MockConfigEntry(domain="ha_solcast_fusion", title="Roof East")
    entry.add_to_hass(hass)
    _register(hass, entry, "energy_production_today_remaining", "roof_east_energy_production_today_remaining")
    _register(hass, entry, "energy_production_tomorrow", "roof_east_energy_production_tomorrow")
    _register(hass, entry, "power_highest_peak_time_today", "roof_east_power_highest_peak_time_today")
    _register(hass, entry, "power_highest_peak_time_tomorrow", "roof_east_power_highest_peak_time_tomorrow")
    result = forecast_sources.derive_pv_entities(hass, entry.entry_id)
    assert result["today"] == "sensor.roof_east_energy_production_today_remaining"
    assert result["tomorrow"] == "sensor.roof_east_energy_production_tomorrow"
    assert result["peak_today"] == "sensor.roof_east_power_highest_peak_time_today"
    assert result["peak_tomorrow"] == "sensor.roof_east_power_highest_peak_time_tomorrow"


async def test_list_forecast_services_includes_ha_solcast_fusion(hass):
    entry = MockConfigEntry(domain="ha_solcast_fusion", title="Roof East")
    entry.add_to_hass(hass)
    services = forecast_sources.list_forecast_services(hass)
    ids = {entry_id for entry_id, _title, _domain in services}
    assert entry.entry_id in ids
