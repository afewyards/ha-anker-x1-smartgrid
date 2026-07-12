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

from . import const, forecast_sources, tariff
from .anker_resolver import resolve_anker_config

_LOGGER = logging.getLogger(__name__)


class _ClearableEntitySelector(EntitySelector):
    """EntitySelector that also accepts "" (an optional picker with a
    legitimate blank state).

    A bare EntitySelector raises "Entity is neither a valid entity ID nor
    a valid UUID" on "" — but "" is a real, expected value here: the
    const.py DEFAULT_ENT_EXPORT_PRICE sentinel ("no dedicated sensor,
    mirror import"), and what the frontend resubmits when a user clears an
    already-set picker. Every fresh install stores ent_export_price="", so
    without this the options form could never be saved at all.

    Deliberately a Selector subclass, NOT a vol.Any("", EntitySelector(...))
    wrapper: HA's frontend renders the options form by running the schema
    through voluptuous_serialize.convert(), which only special-cases
    vol.Any in the vol.Maybe shape (`vol.Any(None, X)`) and otherwise
    can't serialize it — a vol.Any("", ...) validator crashes that
    conversion (TypeError), breaking the form entirely rather than just
    its submit. Subclassing keeps `isinstance(_, selector.Selector)` true,
    so serialization (and the rendered widget) stay identical to a plain
    EntitySelector; only validation of "" changes.
    """

    def __call__(self, data):
        if data == "":
            return data
        return super().__call__(data)


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
                description={"suggested_value": defaults.get(const.CONF_ENT_PRICE)},
            ): EntitySelector(EntitySelectorConfig(domain="sensor")),
            vol.Optional(
                const.CONF_ENT_WEATHER_FORECAST,
                default=defaults.get(const.CONF_ENT_WEATHER_FORECAST, const.DEFAULT_ENT_WEATHER_FORECAST),
            ): EntitySelector(EntitySelectorConfig(domain="weather")),
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
        const.CONF_ENT_WEATHER_FORECAST,
        const.CONF_ENT_PV_POWER,
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
        const.CONF_IDLE_DRAIN_W,
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
        const.CONF_PRICE_MODE,
        const.CONF_STATIC_PRICE_IMPORT,
        const.CONF_STATIC_PRICE_OFFPEAK,
        const.CONF_STATIC_OFFPEAK_HOURS,
        const.CONF_STATIC_PRICE_EXPORT,
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


# --- data-driven field tables ----------------------------------------------
# Each table backs a mechanical loop in _options_fields() below. A field
# lives in exactly one table, EXCEPT a handful with bespoke suggested_value
# logic (forecast service, Anker device, live PV-power sensors) which stay
# hand-written in _options_fields itself.
#
# capacity_kwh is intentionally absent from every table: it is ALWAYS-DERIVED
# from the Anker nominal-capacity sensor (see _schema above +
# anker_resolver.resolve_anker_config), so a manually-set value here would be
# silently overwritten on the next resolution — an inert field (review
# finding 4.1).

# "Plain" tunables: vol.Optional(key, default=defaults.get(key, DEFAULT)) with
# a single validator. (conf_key, default_const, validator).
_TUNABLES: list[tuple[str, object, object]] = [
    (const.CONF_SOC_FLOOR, const.DEFAULT_SOC_FLOOR, vol.All(vol.Coerce(float), vol.Range(min=const.FIRMWARE_SOC_FLOOR, max=50.0))),
    (const.CONF_ADDON_ENABLED, const.DEFAULT_ADDON_ENABLED, cv.boolean),
    (const.CONF_ADDON_URL, const.DEFAULT_ADDON_URL, cv.string),
    (const.CONF_ADDON_TIMEOUT, const.DEFAULT_ADDON_TIMEOUT, vol.All(vol.Coerce(int), vol.Range(min=1, max=60))),
    (const.CONF_SOC_TARGET, const.DEFAULT_SOC_TARGET, vol.All(vol.Coerce(float), vol.Range(min=10.0, max=100.0))),
    (const.CONF_ETA_CHARGE, const.DEFAULT_ETA_CHARGE, vol.All(vol.Coerce(float), vol.Range(min=0.5, max=1.0))),
    (const.CONF_ROUND_TRIP_EFF, const.DEFAULT_ROUND_TRIP_EFF, vol.All(vol.Coerce(float), vol.Range(min=0.5, max=1.0))),
    (const.CONF_USE_MEASURED_ETA, const.DEFAULT_USE_MEASURED_ETA, cv.boolean),
    (const.CONF_USE_LEARNED_MODEL, const.DEFAULT_USE_LEARNED_MODEL, cv.boolean),
    (const.CONF_MIN_TRAIN_SAMPLES, const.DEFAULT_MIN_TRAIN_SAMPLES, cv.positive_int),
    (const.CONF_RETENTION_DAYS, const.DEFAULT_RETENTION_DAYS, cv.positive_int),
    (const.CONF_RETENTION_HOURLY_DAYS, const.DEFAULT_RETENTION_HOURLY_DAYS, cv.positive_int),
    (const.CONF_ENABLE_EXPORT, const.DEFAULT_ENABLE_EXPORT, cv.boolean),
    (
        const.CONF_GRID_EXPORT_LIMIT_W,
        const.DEFAULT_GRID_EXPORT_LIMIT_W,
        # Sane ceiling added here (review LOW, deliberate): previously
        # min-only. 20000W comfortably covers a 3-phase 32A grid connection,
        # well above the X1's own 6000W device ceiling (SETPOINT_MAX_W).
        vol.All(vol.Coerce(float), vol.Range(min=0, max=20000.0)),
    ),
    (const.CONF_EXPORT_FEE_EUR_PER_KWH, const.DEFAULT_EXPORT_FEE_EUR_PER_KWH, vol.All(vol.Coerce(float), vol.Range(min=0.0, max=0.5))),
    (const.CONF_CYCLE_COST_EUR_PER_KWH, const.DEFAULT_CYCLE_COST_EUR_PER_KWH, cv.positive_float),
    (const.CONF_CHARGE_MARGIN_EUR_PER_KWH, const.DEFAULT_CHARGE_MARGIN_EUR_PER_KWH, cv.positive_float),
    (const.CONF_IDLE_DRAIN_W, const.DEFAULT_IDLE_DRAIN_W, vol.All(vol.Coerce(float), vol.Range(min=0.0, max=500.0))),
    (const.CONF_RESERVE_CHEAP_BAND, const.DEFAULT_RESERVE_CHEAP_BAND, vol.All(vol.Coerce(float), vol.Range(min=0.0, max=1.0))),
    (const.CONF_EXPORT_DWELL_MIN, const.DEFAULT_EXPORT_DWELL_MIN, cv.positive_int),
    (const.CONF_EXPORT_EPS_LO_KWH, const.DEFAULT_EXPORT_EPS_LO_KWH, cv.positive_float),
    (const.CONF_EXPORT_EPS_HI_KWH, const.DEFAULT_EXPORT_EPS_HI_KWH, cv.positive_float),
    (const.CONF_EXPORT_PEAK_BAND_FRAC, const.DEFAULT_EXPORT_PEAK_BAND_FRAC, vol.All(vol.Coerce(float), vol.Range(min=0.0, max=1.0))),
    (const.CONF_EXPORT_PEAK_LOOKBACK_H, const.DEFAULT_EXPORT_PEAK_LOOKBACK_H, vol.All(vol.Coerce(int), vol.Range(min=0, max=12))),
    (const.CONF_EXPORT_MIN_BLOCK_KWH, const.DEFAULT_EXPORT_MIN_BLOCK_KWH, vol.All(vol.Coerce(float), vol.Range(min=0.0))),
    (const.CONF_EXPORT_LOAD_COMP_FACTOR, const.DEFAULT_EXPORT_LOAD_COMP_FACTOR, vol.All(vol.Coerce(float), vol.Range(min=0.0, max=1.0))),
    (const.CONF_CHARGE_TROUGH_LOOKBACK_H, const.DEFAULT_CHARGE_TROUGH_LOOKBACK_H, vol.All(vol.Coerce(int), vol.Range(min=0, max=12))),
    (const.CONF_STATIC_PRICE_IMPORT, const.DEFAULT_STATIC_PRICE_IMPORT, vol.All(vol.Coerce(float), vol.Range(min=0.0, max=2.0))),
    (const.CONF_STATIC_PRICE_OFFPEAK, const.DEFAULT_STATIC_PRICE_OFFPEAK, vol.All(vol.Coerce(float), vol.Range(min=0.0, max=2.0))),
    (const.CONF_STATIC_OFFPEAK_HOURS, const.DEFAULT_STATIC_OFFPEAK_HOURS, cv.string),
    (const.CONF_STATIC_PRICE_EXPORT, const.DEFAULT_STATIC_PRICE_EXPORT, vol.All(vol.Coerce(float), vol.Range(min=0.0, max=2.0))),
    (const.CONF_PRICE_HISTORY_DAYS, const.DEFAULT_PRICE_HISTORY_DAYS, vol.All(vol.Coerce(int), vol.Range(min=2, max=30))),
    (const.CONF_PRICE_BLEND_WEIGHT_TODAY, const.DEFAULT_PRICE_BLEND_WEIGHT_TODAY, vol.All(vol.Coerce(float), vol.Range(min=0.0, max=1.0))),
    (const.CONF_ANTICIPATION_CONFIDENCE_HAIRCUT, const.DEFAULT_ANTICIPATION_CONFIDENCE_HAIRCUT, vol.All(vol.Coerce(float), vol.Range(min=0.0, max=1.0))),
    (const.CONF_ANTICIPATION_MARGIN_EUR_PER_KWH, const.DEFAULT_ANTICIPATION_MARGIN_EUR_PER_KWH, vol.All(vol.Coerce(float), vol.Range(min=0.0, max=0.5))),
    (const.CONF_SOC_HEDGE_FRACTION, const.DEFAULT_SOC_HEDGE_FRACTION, vol.All(vol.Coerce(float), vol.Range(min=0.0, max=1.0))),
    (const.CONF_SOC_DRIFT_DEADBAND_KWH, const.DEFAULT_SOC_DRIFT_DEADBAND_KWH, vol.All(vol.Coerce(float), vol.Range(min=0.0, max=5.0))),
    (const.CONF_SOC_DRIFT_DECAY_HALFLIFE_H, const.DEFAULT_SOC_DRIFT_DECAY_HALFLIFE_H, vol.All(vol.Coerce(float), vol.Range(min=0.0, max=48.0))),
    (const.CONF_LOAD_ADAPT_FRACTION, const.DEFAULT_LOAD_ADAPT_FRACTION, vol.All(vol.Coerce(float), vol.Range(min=0.0, max=1.0))),
    (const.CONF_LOAD_ADAPT_WINDOW_H, const.DEFAULT_LOAD_ADAPT_WINDOW_H, vol.All(vol.Coerce(int), vol.Range(min=1, max=12))),
    (const.CONF_LOAD_ADAPT_FADE_H, const.DEFAULT_LOAD_ADAPT_FADE_H, vol.All(vol.Coerce(int), vol.Range(min=1, max=24))),
]

# Multi-select entity pickers whose suggested_value is simply
# defaults.get(key, []). (conf_key, domain).
_ENTITY_LIST_PICKERS: list[tuple[str, str]] = [
    (const.CONF_ENT_PV_TODAY, "sensor"),
    (const.CONF_ENT_PV_TOMORROW, "sensor"),
    (const.CONF_ENT_PV_PEAK_TODAY, "sensor"),
    (const.CONF_ENT_PV_PEAK_TOMORROW, "sensor"),
    (const.CONF_PERSON_ENTITIES, "person"),
]

# Single, clearable entity pickers: suggested_value is defaults.get(key) or
# None, and "" must still validate (see _ClearableEntitySelector).
# (conf_key, domain).
# NOTE: no device_class filter on ent_price — €/kWh tariff sensors (e.g. the
# Zonneplan default) typically carry no device_class, so a "monetary" filter
# here would hide them from the picker (Task 16).
_CLEARABLE_ENTITY_PICKERS: list[tuple[str, str]] = [
    (const.CONF_ENT_WEATHER_FORECAST, "weather"),
    (const.CONF_ENT_PRICE, "sensor"),
    (const.CONF_ENT_EXPORT_PRICE, "sensor"),
]

# Select-dropdown groups: (conf_key, default_const, options). Same
# default-value pattern as _TUNABLES, but wired to a fixed option list
# instead of a plain validator. Keyed by name for readability at the
# call site; iteration uses .values() only.
_SELECT_GROUPS: dict[str, tuple[str, object, list[SelectOptionDict]]] = {
    "slot_resolution": (
        const.CONF_SLOT_RESOLUTION,
        const.DEFAULT_SLOT_RESOLUTION,
        [
            SelectOptionDict(value="auto", label="Auto-detect"),
            SelectOptionDict(value="15", label="15 minutes"),
            SelectOptionDict(value="30", label="30 minutes"),
            SelectOptionDict(value="60", label="60 minutes"),
        ],
    ),
    "reserve_anchor": (
        const.CONF_RESERVE_ANCHOR,
        const.DEFAULT_RESERVE_ANCHOR,
        [
            SelectOptionDict(value=const.RESERVE_ANCHOR_TROUGH, label="ride-to-trough (self-scaling)"),
            SelectOptionDict(value=const.RESERVE_ANCHOR_LEGACY, label="legacy (debit-to-trough + price-prior)"),
        ],
    ),
    "price_mode": (
        const.CONF_PRICE_MODE,
        const.DEFAULT_PRICE_MODE,
        [
            SelectOptionDict(value=const.PRICE_MODE_SENSOR, label="Dynamic price sensor"),
            SelectOptionDict(value=const.PRICE_MODE_STATIC, label="Static tariff (flat / HP-HC)"),
        ],
    ),
}


def _options_fields(defaults: dict, services=None) -> dict:
    """Flat {vol marker: validator} map of every options-flow field.

    Extensible: add new optional keys here AND to OPTIONS_SECTIONS. Most
    fields are table-driven (_TUNABLES / _ENTITY_LIST_PICKERS /
    _CLEARABLE_ENTITY_PICKERS / _SELECT_GROUPS above); a few with bespoke
    suggested_value logic (forecast service, Anker device, live PV-power
    sensors) stay hand-written here.
    `services` lists (entry_id, title, domain) tuples from forecast_sources.
    """
    service_options = _forecast_service_options(services)
    stored_services = _stored_forecast_services(defaults, services)
    fields: dict = {
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
        # Live PV-power sensors (W), summed at read time — distinct from the
        # ent_pv_today/tomorrow *forecast* (kWh) lists above. Supports the
        # legacy single entity-id string (normalized to a one-element list
        # for display) alongside the new multi-sensor list (99a7b53).
        # suggested_value is None (not []) when unconfigured so the
        # DEFAULT_ENTITIES soft-role fallback (resolve_pv_power_entities)
        # stays in effect on save-through — an explicit [] would persist as
        # "configured to nothing" rather than "unconfigured", though both
        # currently resolve identically at runtime.
        vol.Optional(
            const.CONF_ENT_PV_POWER,
            description={
                "suggested_value": const.normalize_pv_power_entities(
                    defaults.get(const.CONF_ENT_PV_POWER)
                ) or None
            },
        ): EntitySelector(EntitySelectorConfig(domain="sensor", multiple=True)),
    }
    for key, default, validator in _TUNABLES:
        fields[vol.Optional(key, default=defaults.get(key, default))] = validator
    for key, domain in _ENTITY_LIST_PICKERS:
        fields[
            vol.Optional(key, description={"suggested_value": defaults.get(key, [])})
        ] = EntitySelector(EntitySelectorConfig(domain=domain, multiple=True))
    for key, domain in _CLEARABLE_ENTITY_PICKERS:
        fields[
            vol.Optional(key, description={"suggested_value": defaults.get(key) or None})
        ] = _ClearableEntitySelector(EntitySelectorConfig(domain=domain))
    for key, default, options in _SELECT_GROUPS.values():
        fields[vol.Optional(key, default=defaults.get(key, default))] = SelectSelector(
            SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
        )
    return fields


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
            if not errors and user_input.get(
                const.CONF_PRICE_MODE, merged.get(const.CONF_PRICE_MODE, const.DEFAULT_PRICE_MODE)
            ) == const.PRICE_MODE_STATIC:
                _imp = user_input.get(
                    const.CONF_STATIC_PRICE_IMPORT,
                    merged.get(const.CONF_STATIC_PRICE_IMPORT, const.DEFAULT_STATIC_PRICE_IMPORT),
                )
                _op_hours = (user_input.get(
                    const.CONF_STATIC_OFFPEAK_HOURS,
                    merged.get(const.CONF_STATIC_OFFPEAK_HOURS, const.DEFAULT_STATIC_OFFPEAK_HOURS),
                ) or "").strip()
                _op_price = user_input.get(
                    const.CONF_STATIC_PRICE_OFFPEAK,
                    merged.get(const.CONF_STATIC_PRICE_OFFPEAK, const.DEFAULT_STATIC_PRICE_OFFPEAK),
                )
                if _imp <= 0:
                    errors["base"] = "static_import_price_required"
                elif _op_hours:
                    if _op_price <= 0:
                        errors["base"] = "static_offpeak_price_required"
                    else:
                        try:
                            tariff.parse_offpeak_ranges(_op_hours)
                        except ValueError:
                            errors["base"] = "static_offpeak_hours_invalid"
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
