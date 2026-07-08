"""Config flow for Anker X1 SmartGrid."""
from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import section
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.selector import (
    DeviceSelector,
    DeviceSelectorConfig,
    EntitySelector,
    EntitySelectorConfig,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from . import const, forecast_sources
from .anker_resolver import resolve_anker_config

_LOGGER = logging.getLogger(__name__)


def _schema(defaults: dict, services=None) -> vol.Schema:
    """Initial-setup schema: the Anker device + core sensors only.

    Everything else defaults at install and is tunable later in the options
    flow.  capacity_kwh has no field: it is ALWAYS re-derived from the Anker
    nominal-capacity sensor (anker_resolver.resolve_anker_config) at install
    and on every reload.
    """
    return vol.Schema(
        {
            vol.Required(
                const.CONF_ANKER_DEVICE,
                description={"suggested_value": defaults.get(const.CONF_ANKER_DEVICE)},
            ): DeviceSelector(DeviceSelectorConfig(integration=const.ANKER_X1_DOMAIN)),
            vol.Optional(
                const.CONF_ENT_PRICE,
                default=defaults.get(const.CONF_ENT_PRICE, const.DEFAULT_ENTITIES[const.CONF_ENT_PRICE]),
            ): EntitySelector(EntitySelectorConfig(domain="sensor")),
            vol.Optional(
                const.CONF_ENT_WEATHER_FORECAST,
                default=defaults.get(const.CONF_ENT_WEATHER_FORECAST, const.DEFAULT_ENT_WEATHER_FORECAST),
            ): EntitySelector(EntitySelectorConfig(domain="weather")),
            vol.Optional(
                const.CONF_ENT_HOUSE_LOAD,
                default=defaults.get(const.CONF_ENT_HOUSE_LOAD, const.DEFAULT_ENT_HOUSE_LOAD),
            ): EntitySelector(EntitySelectorConfig(domain="sensor")),
            vol.Optional(
                const.CONF_FORECAST_SERVICE,
                description={"suggested_value": _stored_forecast_services(defaults, services)},
            ): SelectSelector(
                SelectSelectorConfig(
                    options=_forecast_service_options(services),
                    mode=SelectSelectorMode.DROPDOWN,
                    multiple=True,
                )
            ),
        }
    )


def _forecast_service_options(services) -> list[SelectOptionDict]:
    """Selector options for the forecast-service multi-select."""
    options = []
    for entry_id, title, domain in (services or []):
        integration_name = forecast_sources.FORECAST_SOURCE_MAP.get(domain, {}).get("name", domain)
        options.append(SelectOptionDict(value=entry_id, label=f"{title} — {integration_name}"))
    return options


def _stored_forecast_services(defaults: dict, services) -> list[str]:
    """Prior selection, minus ids whose integration is gone (a default not
    present in the selector's options raises at submit)."""
    available = {entry_id for entry_id, _title, _domain in (services or [])}
    return [s for s in defaults.get(const.CONF_FORECAST_SERVICE, []) if s in available]


def _apply_forecast_pv_derivation(hass, user_input: dict, chosen: list[str], stored: list[str]) -> None:
    """Rebuild the PV energy/peak entity lists from selected forecast services.

    Mutates ``user_input`` in place.  No-op when nothing was chosen or the
    selection is unchanged (keeps manually-tuned entity lists intact).
    """
    if not chosen or chosen == stored:
        return
    resolved = {
        eid: forecast_sources.derive_pv_entities(hass, eid)
        for eid in chosen
    }
    day_roles = [
        ("today", "peak_today",
         const.CONF_ENT_PV_TODAY, const.CONF_ENT_PV_PEAK_TODAY),
        ("tomorrow", "peak_tomorrow",
         const.CONF_ENT_PV_TOMORROW, const.CONF_ENT_PV_PEAK_TOMORROW),
    ]
    for e_role, p_role, e_key, p_key in day_roles:
        pairs: list[tuple[str, str | None]] = []
        for eid in chosen:            # preserve selection order
            energy = resolved[eid].get(e_role)
            if energy and energy not in [p[0] for p in pairs]:
                pairs.append((energy, resolved[eid].get(p_role)))
        if pairs:
            user_input[e_key] = [energy for energy, _ in pairs]
            peaks = [peak for _, peak in pairs]
            # All-or-nothing: a partial peak list would misalign
            # _read_pv_arrays (positional energy[i]↔peak[i]).
            user_input[p_key] = peaks if all(peaks) else []


SECTION_DEVICES = "devices"
SECTION_SOLAR = "solar_forecast"
SECTION_BATTERY = "battery"
SECTION_EXPORT = "export"
SECTION_PRICE = "price_anticipation"
SECTION_LOAD_ML = "load_ml"
SECTION_SYSTEM = "system"

# Section -> option keys.  Single source of truth for grouping: the sectioned
# options schema, the submit-time flatten and the strings.json coverage test
# all read this mapping.
OPTIONS_SECTIONS: dict[str, tuple[str, ...]] = {
    SECTION_DEVICES: (
        const.CONF_ANKER_DEVICE,
        const.CONF_ENT_PRICE,
        const.CONF_ENT_EXPORT_PRICE,
        const.CONF_ENT_HOUSE_LOAD,
        const.CONF_ENT_WEATHER_FORECAST,
        const.CONF_PERSON_ENTITIES,
    ),
    SECTION_SOLAR: (
        const.CONF_FORECAST_SERVICE,
        const.CONF_ENT_PV_TODAY,
        const.CONF_ENT_PV_TOMORROW,
        const.CONF_ENT_PV_PEAK_TODAY,
        const.CONF_ENT_PV_PEAK_TOMORROW,
    ),
    SECTION_BATTERY: (
        const.CONF_SOC_FLOOR,
        const.CONF_SOC_TARGET,
        const.CONF_ETA_CHARGE,
        const.CONF_ROUND_TRIP_EFF,
        const.CONF_USE_MEASURED_ETA,
        const.CONF_CHARGE_MARGIN_EUR_PER_KWH,
        const.CONF_CHARGE_TROUGH_LOOKBACK_H,
    ),
    SECTION_EXPORT: (
        const.CONF_ENABLE_EXPORT,
        const.CONF_GRID_EXPORT_LIMIT_W,
        const.CONF_EXPORT_FEE_EUR_PER_KWH,
        const.CONF_CYCLE_COST_EUR_PER_KWH,
        const.CONF_RESERVE_ANCHOR,
        const.CONF_RESERVE_CHEAP_BAND,
        const.CONF_EXPORT_DWELL_MIN,
        const.CONF_EXPORT_EPS_LO_KWH,
        const.CONF_EXPORT_EPS_HI_KWH,
        const.CONF_EXPORT_PEAK_BAND_FRAC,
        const.CONF_EXPORT_PEAK_LOOKBACK_H,
        const.CONF_EXPORT_MIN_BLOCK_KWH,
        const.CONF_EXPORT_LOAD_COMP_FACTOR,
    ),
    SECTION_PRICE: (
        const.CONF_PRICE_HISTORY_DAYS,
        const.CONF_PRICE_BLEND_WEIGHT_TODAY,
        const.CONF_ANTICIPATION_CONFIDENCE_HAIRCUT,
        const.CONF_ANTICIPATION_MARGIN_EUR_PER_KWH,
    ),
    SECTION_LOAD_ML: (
        const.CONF_USE_LEARNED_MODEL,
        const.CONF_MIN_TRAIN_SAMPLES,
        const.CONF_LOAD_ADAPT_FRACTION,
        const.CONF_LOAD_ADAPT_WINDOW_H,
        const.CONF_LOAD_ADAPT_FADE_H,
        const.CONF_ADDON_ENABLED,
        const.CONF_ADDON_URL,
        const.CONF_ADDON_TIMEOUT,
    ),
    SECTION_SYSTEM: (
        const.CONF_SLOT_RESOLUTION,
        const.CONF_RETENTION_DAYS,
        const.CONF_RETENTION_HOURLY_DAYS,
        const.CONF_SOC_HEDGE_FRACTION,
        const.CONF_SOC_DRIFT_DEADBAND_KWH,
        const.CONF_SOC_DRIFT_DECAY_HALFLIFE_H,
    ),
}


def _options_fields(defaults: dict, services=None) -> dict:
    """Flat {vol marker: validator} map of every options-flow field.

    Extensible: add new optional keys here AND to OPTIONS_SECTIONS.
    `services` lists (entry_id, title, domain) tuples from forecast_sources.
    """
    service_options = _forecast_service_options(services)
    stored_services = _stored_forecast_services(defaults, services)
    return {
            vol.Optional(
                const.CONF_FORECAST_SERVICE,
                description={"suggested_value": stored_services},
            ): SelectSelector(
                SelectSelectorConfig(
                    options=service_options,
                    mode=SelectSelectorMode.DROPDOWN,
                    multiple=True,
                )
            ),
            vol.Optional(
                const.CONF_ANKER_DEVICE,
                description={"suggested_value": defaults.get(const.CONF_ANKER_DEVICE)},
            ): DeviceSelector(DeviceSelectorConfig(integration=const.ANKER_X1_DOMAIN)),
            vol.Optional(
                const.CONF_SOC_FLOOR,
                default=defaults.get(const.CONF_SOC_FLOOR, const.DEFAULT_SOC_FLOOR),
            ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=50.0)),
            vol.Optional(
                const.CONF_ADDON_ENABLED,
                default=defaults.get(const.CONF_ADDON_ENABLED, const.DEFAULT_ADDON_ENABLED),
            ): cv.boolean,
            vol.Optional(
                const.CONF_ADDON_URL,
                default=defaults.get(const.CONF_ADDON_URL, const.DEFAULT_ADDON_URL),
            ): cv.string,
            vol.Optional(
                const.CONF_ADDON_TIMEOUT,
                default=defaults.get(const.CONF_ADDON_TIMEOUT, const.DEFAULT_ADDON_TIMEOUT),
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=60)),
            vol.Optional(
                const.CONF_SLOT_RESOLUTION,
                default=defaults.get(const.CONF_SLOT_RESOLUTION, const.DEFAULT_SLOT_RESOLUTION),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(value="auto", label="Auto-detect"),
                        SelectOptionDict(value="15", label="15 minutes"),
                        SelectOptionDict(value="30", label="30 minutes"),
                        SelectOptionDict(value="60", label="60 minutes"),
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            # --- entity pickers ---
            vol.Optional(
                const.CONF_ENT_PV_TODAY,
                description={"suggested_value": defaults.get(const.CONF_ENT_PV_TODAY, [])},
            ): EntitySelector(EntitySelectorConfig(domain="sensor", multiple=True)),
            vol.Optional(
                const.CONF_ENT_PV_TOMORROW,
                description={"suggested_value": defaults.get(const.CONF_ENT_PV_TOMORROW, [])},
            ): EntitySelector(EntitySelectorConfig(domain="sensor", multiple=True)),
            vol.Optional(
                const.CONF_ENT_PV_PEAK_TODAY,
                description={"suggested_value": defaults.get(const.CONF_ENT_PV_PEAK_TODAY, [])},
            ): EntitySelector(EntitySelectorConfig(domain="sensor", multiple=True)),
            vol.Optional(
                const.CONF_ENT_PV_PEAK_TOMORROW,
                description={"suggested_value": defaults.get(const.CONF_ENT_PV_PEAK_TOMORROW, [])},
            ): EntitySelector(EntitySelectorConfig(domain="sensor", multiple=True)),
            vol.Optional(
                const.CONF_ENT_WEATHER_FORECAST,
                description={"suggested_value": defaults.get(const.CONF_ENT_WEATHER_FORECAST)},
            ): EntitySelector(EntitySelectorConfig(domain="weather")),
            # NOTE: no device_class filter — €/kWh tariff sensors (e.g. the
            # Zonneplan default) typically carry no device_class, so a
            # "monetary" filter here hides them from the picker (Task 16).
            vol.Optional(
                const.CONF_ENT_PRICE,
                description={"suggested_value": defaults.get(const.CONF_ENT_PRICE)},
            ): EntitySelector(EntitySelectorConfig(domain="sensor")),
            vol.Optional(
                const.CONF_ENT_EXPORT_PRICE,
                description={"suggested_value": defaults.get(const.CONF_ENT_EXPORT_PRICE)},
            ): EntitySelector(EntitySelectorConfig(domain="sensor")),
            vol.Optional(
                const.CONF_ENT_HOUSE_LOAD,
                description={"suggested_value": defaults.get(const.CONF_ENT_HOUSE_LOAD)},
            ): EntitySelector(EntitySelectorConfig(domain="sensor")),
            # --- tunables promoted from install-only to editable ---
            # capacity_kwh intentionally omitted: it is ALWAYS-DERIVED from the Anker
            # nominal-capacity sensor (see _schema above + anker_resolver.resolve_anker_config),
            # so a manually-set value here would be silently overwritten on the next
            # resolution — an inert field (review finding 4.1).
            vol.Optional(const.CONF_SOC_TARGET, default=defaults.get(const.CONF_SOC_TARGET, const.DEFAULT_SOC_TARGET)): vol.All(vol.Coerce(float), vol.Range(min=10.0, max=100.0)),
            vol.Optional(const.CONF_ETA_CHARGE, default=defaults.get(const.CONF_ETA_CHARGE, const.DEFAULT_ETA_CHARGE)): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=1.0)),
            vol.Optional(const.CONF_ROUND_TRIP_EFF, default=defaults.get(const.CONF_ROUND_TRIP_EFF, const.DEFAULT_ROUND_TRIP_EFF)): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=1.0)),
            vol.Optional(const.CONF_USE_MEASURED_ETA, default=defaults.get(const.CONF_USE_MEASURED_ETA, const.DEFAULT_USE_MEASURED_ETA)): cv.boolean,
            vol.Optional(const.CONF_USE_LEARNED_MODEL, default=defaults.get(const.CONF_USE_LEARNED_MODEL, const.DEFAULT_USE_LEARNED_MODEL)): cv.boolean,
            vol.Optional(const.CONF_MIN_TRAIN_SAMPLES, default=defaults.get(const.CONF_MIN_TRAIN_SAMPLES, const.DEFAULT_MIN_TRAIN_SAMPLES)): cv.positive_int,
            vol.Optional(const.CONF_RETENTION_DAYS, default=defaults.get(const.CONF_RETENTION_DAYS, const.DEFAULT_RETENTION_DAYS)): cv.positive_int,
            vol.Optional(const.CONF_RETENTION_HOURLY_DAYS, default=defaults.get(const.CONF_RETENTION_HOURLY_DAYS, const.DEFAULT_RETENTION_HOURLY_DAYS)): cv.positive_int,
            vol.Optional(
                const.CONF_ENABLE_EXPORT,
                default=defaults.get(const.CONF_ENABLE_EXPORT, const.DEFAULT_ENABLE_EXPORT),
            ): cv.boolean,
            vol.Optional(
                const.CONF_GRID_EXPORT_LIMIT_W,
                default=defaults.get(const.CONF_GRID_EXPORT_LIMIT_W, const.DEFAULT_GRID_EXPORT_LIMIT_W),
            ): vol.All(vol.Coerce(float), vol.Range(min=0)),
            vol.Optional(
                const.CONF_EXPORT_FEE_EUR_PER_KWH,
                default=defaults.get(const.CONF_EXPORT_FEE_EUR_PER_KWH, const.DEFAULT_EXPORT_FEE_EUR_PER_KWH),
            ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=0.5)),
            vol.Optional(
                const.CONF_CYCLE_COST_EUR_PER_KWH,
                default=defaults.get(const.CONF_CYCLE_COST_EUR_PER_KWH, const.DEFAULT_CYCLE_COST_EUR_PER_KWH),
            ): cv.positive_float,
            vol.Optional(
                const.CONF_CHARGE_MARGIN_EUR_PER_KWH,
                default=defaults.get(const.CONF_CHARGE_MARGIN_EUR_PER_KWH, const.DEFAULT_CHARGE_MARGIN_EUR_PER_KWH),
            ): cv.positive_float,
            vol.Optional(
                const.CONF_RESERVE_ANCHOR,
                default=defaults.get(const.CONF_RESERVE_ANCHOR, const.DEFAULT_RESERVE_ANCHOR),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(value=const.RESERVE_ANCHOR_TROUGH, label="ride-to-trough (self-scaling)"),
                        SelectOptionDict(value=const.RESERVE_ANCHOR_LEGACY, label="legacy (debit-to-trough + price-prior)"),
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional(
                const.CONF_RESERVE_CHEAP_BAND,
                default=defaults.get(const.CONF_RESERVE_CHEAP_BAND, const.DEFAULT_RESERVE_CHEAP_BAND),
            ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=1.0)),
            vol.Optional(
                const.CONF_EXPORT_DWELL_MIN,
                default=defaults.get(const.CONF_EXPORT_DWELL_MIN, const.DEFAULT_EXPORT_DWELL_MIN),
            ): cv.positive_int,
            vol.Optional(
                const.CONF_EXPORT_EPS_LO_KWH,
                default=defaults.get(const.CONF_EXPORT_EPS_LO_KWH, const.DEFAULT_EXPORT_EPS_LO_KWH),
            ): cv.positive_float,
            vol.Optional(
                const.CONF_EXPORT_EPS_HI_KWH,
                default=defaults.get(const.CONF_EXPORT_EPS_HI_KWH, const.DEFAULT_EXPORT_EPS_HI_KWH),
            ): cv.positive_float,
            vol.Optional(
                const.CONF_EXPORT_PEAK_BAND_FRAC,
                default=defaults.get(const.CONF_EXPORT_PEAK_BAND_FRAC, const.DEFAULT_EXPORT_PEAK_BAND_FRAC),
            ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=1.0)),
            vol.Optional(
                const.CONF_EXPORT_PEAK_LOOKBACK_H,
                default=defaults.get(const.CONF_EXPORT_PEAK_LOOKBACK_H, const.DEFAULT_EXPORT_PEAK_LOOKBACK_H),
            ): vol.All(vol.Coerce(int), vol.Range(min=0, max=12)),
            vol.Optional(
                const.CONF_EXPORT_MIN_BLOCK_KWH,
                default=defaults.get(const.CONF_EXPORT_MIN_BLOCK_KWH, const.DEFAULT_EXPORT_MIN_BLOCK_KWH),
            ): vol.All(vol.Coerce(float), vol.Range(min=0.0)),
            vol.Optional(
                const.CONF_EXPORT_LOAD_COMP_FACTOR,
                default=defaults.get(const.CONF_EXPORT_LOAD_COMP_FACTOR, const.DEFAULT_EXPORT_LOAD_COMP_FACTOR),
            ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=1.0)),
            vol.Optional(
                const.CONF_CHARGE_TROUGH_LOOKBACK_H,
                default=defaults.get(const.CONF_CHARGE_TROUGH_LOOKBACK_H, const.DEFAULT_CHARGE_TROUGH_LOOKBACK_H),
            ): vol.All(vol.Coerce(int), vol.Range(min=0, max=12)),
            vol.Optional(
                const.CONF_PRICE_HISTORY_DAYS,
                default=defaults.get(const.CONF_PRICE_HISTORY_DAYS, const.DEFAULT_PRICE_HISTORY_DAYS),
            ): vol.All(vol.Coerce(int), vol.Range(min=2, max=30)),
            vol.Optional(
                const.CONF_PRICE_BLEND_WEIGHT_TODAY,
                default=defaults.get(const.CONF_PRICE_BLEND_WEIGHT_TODAY, const.DEFAULT_PRICE_BLEND_WEIGHT_TODAY),
            ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=1.0)),
            vol.Optional(
                const.CONF_ANTICIPATION_CONFIDENCE_HAIRCUT,
                default=defaults.get(const.CONF_ANTICIPATION_CONFIDENCE_HAIRCUT, const.DEFAULT_ANTICIPATION_CONFIDENCE_HAIRCUT),
            ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=1.0)),
            vol.Optional(
                const.CONF_ANTICIPATION_MARGIN_EUR_PER_KWH,
                default=defaults.get(const.CONF_ANTICIPATION_MARGIN_EUR_PER_KWH, const.DEFAULT_ANTICIPATION_MARGIN_EUR_PER_KWH),
            ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=0.5)),
            vol.Optional(
                const.CONF_SOC_HEDGE_FRACTION,
                default=defaults.get(const.CONF_SOC_HEDGE_FRACTION, const.DEFAULT_SOC_HEDGE_FRACTION),
            ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=1.0)),
            vol.Optional(
                const.CONF_SOC_DRIFT_DEADBAND_KWH,
                default=defaults.get(const.CONF_SOC_DRIFT_DEADBAND_KWH, const.DEFAULT_SOC_DRIFT_DEADBAND_KWH),
            ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=5.0)),
            vol.Optional(
                const.CONF_SOC_DRIFT_DECAY_HALFLIFE_H,
                default=defaults.get(const.CONF_SOC_DRIFT_DECAY_HALFLIFE_H, const.DEFAULT_SOC_DRIFT_DECAY_HALFLIFE_H),
            ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=48.0)),
            vol.Optional(
                const.CONF_LOAD_ADAPT_FRACTION,
                default=defaults.get(const.CONF_LOAD_ADAPT_FRACTION, const.DEFAULT_LOAD_ADAPT_FRACTION),
            ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=1.0)),
            vol.Optional(
                const.CONF_LOAD_ADAPT_WINDOW_H,
                default=defaults.get(const.CONF_LOAD_ADAPT_WINDOW_H, const.DEFAULT_LOAD_ADAPT_WINDOW_H),
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=12)),
            vol.Optional(
                const.CONF_LOAD_ADAPT_FADE_H,
                default=defaults.get(const.CONF_LOAD_ADAPT_FADE_H, const.DEFAULT_LOAD_ADAPT_FADE_H),
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=24)),
            vol.Optional(
                const.CONF_PERSON_ENTITIES,
                description={"suggested_value": defaults.get(const.CONF_PERSON_ENTITIES, [])},
            ): EntitySelector(EntitySelectorConfig(domain="person", multiple=True)),
    }


def _options_schema(defaults: dict, services=None) -> vol.Schema:
    """Sectioned options schema — collapsible groups (HA 2024.6+).

    The stored options/data shape stays FLAT: sections exist only in the form.
    _flatten_sections un-nests the submit data before any validation/persist.
    """
    fields = {
        marker.schema: (marker, validator)
        for marker, validator in _options_fields(defaults, services).items()
    }
    schema: dict = {}
    for name, keys in OPTIONS_SECTIONS.items():
        schema[vol.Required(name)] = section(
            vol.Schema({fields[k][0]: fields[k][1] for k in keys}),
            {"collapsed": name != SECTION_DEVICES},
        )
    return vol.Schema(schema)


def _flatten_sections(user_input: dict) -> dict:
    """Un-nest section-grouped submit data back to the flat storage shape."""
    flat: dict = {}
    for key, value in user_input.items():
        if key in OPTIONS_SECTIONS and isinstance(value, dict):
            flat.update(value)
        else:
            flat[key] = value
    return flat


class X1SmartGridConfigFlow(config_entries.ConfigFlow, domain=const.DOMAIN):
    VERSION = 1

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "X1SmartGridOptionsFlow":
        return X1SmartGridOptionsFlow(config_entry)

    async def async_step_user(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None:
            device_id = user_input.get(const.CONF_ANKER_DEVICE)
            if device_id:
                resolved, missing = resolve_anker_config(self.hass, device_id)
                if missing:
                    _LOGGER.warning("Anker device %s missing roles: %s", device_id, missing)
                    errors["base"] = "anker_roles_missing"
                else:
                    user_input.update(resolved)
            if not errors:
                chosen = user_input.get(const.CONF_FORECAST_SERVICE, []) or []
                _apply_forecast_pv_derivation(self.hass, user_input, chosen, stored=[])
                data = dict(const.DEFAULT_ENTITIES)
                data.update(
                    {
                        const.CONF_CAPACITY_KWH: const.DEFAULT_CAPACITY_KWH,
                        const.CONF_SOC_FLOOR: const.DEFAULT_SOC_FLOOR,
                        const.CONF_SOC_TARGET: const.DEFAULT_SOC_TARGET,
                        const.CONF_ETA_CHARGE: const.DEFAULT_ETA_CHARGE,
                        const.CONF_RETENTION_DAYS: const.DEFAULT_RETENTION_DAYS,
                        const.CONF_USE_LEARNED_MODEL: const.DEFAULT_USE_LEARNED_MODEL,
                        const.CONF_RETRAIN_HOURS: const.DEFAULT_RETRAIN_HOURS,
                        const.CONF_MIN_TRAIN_SAMPLES: const.DEFAULT_MIN_TRAIN_SAMPLES,
                        const.CONF_TRAIN_DAYS: const.DEFAULT_TRAIN_DAYS,
                        const.CONF_BACKTEST_TEST_DAYS: const.DEFAULT_BACKTEST_TEST_DAYS,
                        const.CONF_RETENTION_HOURLY_DAYS: const.DEFAULT_RETENTION_HOURLY_DAYS,
                    }
                )
                data.update(user_input)
                return self.async_create_entry(title="Anker X1 SmartGrid", data=data)
        services = forecast_sources.list_forecast_services(self.hass)
        return self.async_show_form(
            step_id="user", data_schema=_schema(user_input or {}, services), errors=errors
        )


class X1SmartGridOptionsFlow(config_entries.OptionsFlow):
    """Options flow — exposes per-entry toggles without re-running setup.

    To extend: add a key to _options_fields() and OPTIONS_SECTIONS above.
    """

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input = _flatten_sections(user_input)
            merged = {**self._config_entry.data, **self._config_entry.options}
            device_id = user_input.get(const.CONF_ANKER_DEVICE)
            if device_id and device_id != merged.get(const.CONF_ANKER_DEVICE):
                resolved, missing = resolve_anker_config(self.hass, device_id)
                if missing:
                    _LOGGER.warning("Anker device %s missing roles: %s", device_id, missing)
                    errors["base"] = "anker_roles_missing"
                else:
                    user_input.update(resolved)
            if not errors and user_input.get(
                const.CONF_SOC_FLOOR, const.DEFAULT_SOC_FLOOR
            ) >= user_input.get(const.CONF_SOC_TARGET, const.DEFAULT_SOC_TARGET):
                errors["base"] = "soc_floor_above_target"
            if not errors:
                chosen = user_input.get(const.CONF_FORECAST_SERVICE, []) or []
                stored = {**self._config_entry.data, **self._config_entry.options}.get(
                    const.CONF_FORECAST_SERVICE, []) or []
                _apply_forecast_pv_derivation(self.hass, user_input, chosen, stored)
                # CONF_FORECAST_SERVICE stays in user_input → persisted for next open.
                return self.async_create_entry(title="", data=user_input)
        # Merge entry data + existing options so repeated opens show current values.
        # services is only needed here (schema rendering), not on submit.
        services = forecast_sources.list_forecast_services(self.hass)
        defaults = {**self._config_entry.data, **self._config_entry.options}
        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(defaults, services),
            errors=errors,
        )
