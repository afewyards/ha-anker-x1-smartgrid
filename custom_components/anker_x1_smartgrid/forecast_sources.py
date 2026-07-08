"""Discover PV forecast services and derive their forecast entities."""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

# Roles consumed by the PV forecast readers (coordinator.read_pv_*).
PV_ROLES: tuple[str, ...] = ("today", "tomorrow", "peak_today", "peak_tomorrow")

# forecast_solar, open_meteo_solar_forecast and ha_solcast_fusion expose identical entity keys.
_SHARED_FORECAST_KEYS: dict[str, str] = {
    "today": "energy_production_today_remaining",
    "tomorrow": "energy_production_tomorrow",
    "peak_today": "power_highest_peak_time_today",
    "peak_tomorrow": "power_highest_peak_time_tomorrow",
}

# Solcast uses its own translation_keys (verified against BJReplay/ha-solcast-solar).
_SOLCAST_FORECAST_KEYS: dict[str, str] = {
    "today": "forecast_remaining_today",
    "tomorrow": "total_kwh_forecast_tomorrow",
    "peak_today": "peak_w_time_today",
    "peak_tomorrow": "peak_w_time_tomorrow",
}

# domain -> {name, keys} — accepted PV-energy-production forecast integrations.
FORECAST_SOURCE_MAP: dict[str, dict] = {
    "forecast_solar":            {"name": "Forecast.Solar", "keys": _SHARED_FORECAST_KEYS},
    "open_meteo_solar_forecast": {"name": "Open-Meteo",     "keys": _SHARED_FORECAST_KEYS},
    "solcast_solar":             {"name": "Solcast",        "keys": _SOLCAST_FORECAST_KEYS},
    "ha_solcast_fusion":         {"name": "SolcastFusion",  "keys": _SHARED_FORECAST_KEYS},
}


def list_forecast_services(hass: HomeAssistant) -> list[tuple[str, str, str]]:
    """Return (entry_id, title, domain) for every supported, enabled forecast entry."""
    services: list[tuple[str, str, str]] = []
    for entry in hass.config_entries.async_entries():
        if entry.domain in FORECAST_SOURCE_MAP and entry.disabled_by is None:
            services.append((entry.entry_id, entry.title or entry.domain, entry.domain))
    return services


def derive_pv_entities(hass: HomeAssistant, entry_id: str) -> dict[str, str | None]:
    """Resolve the PV roles to entity_ids for a forecast config entry.

    Match each role by entity ``translation_key`` (fallback: ``entity_id`` suffix).
    Unresolved roles map to ``None``.  Returns ``{}`` for an unknown/unmapped entry.
    """
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain not in FORECAST_SOURCE_MAP:
        return {}
    role_keys = FORECAST_SOURCE_MAP[entry.domain]["keys"]
    entities = er.async_entries_for_config_entry(er.async_get(hass), entry_id)
    result: dict[str, str | None] = {}
    for role in PV_ROLES:
        key = role_keys[role]
        result[role] = next(
            (
                ent.entity_id
                for ent in entities
                if ent.translation_key == key or ent.entity_id.endswith(key)
            ),
            None,
        )
    return result
