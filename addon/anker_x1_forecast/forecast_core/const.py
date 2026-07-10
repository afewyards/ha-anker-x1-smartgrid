"""Constants for Anker X1 SmartGrid."""
from __future__ import annotations

DOMAIN = "anker_x1_smartgrid"
PLATFORMS = ["sensor", "switch"]

# Tick
TICK_SECONDS = 60

# Config keys
CONF_CAPACITY_KWH = "capacity_kwh"
CONF_SOC_FLOOR = "soc_floor"
CONF_SOC_TARGET = "soc_target"
CONF_MAX_CHARGE_W = "max_charge_w"
CONF_ETA_CHARGE = "eta_charge"
CONF_RETENTION_DAYS = "retention_days"
CONF_USE_LEARNED_MODEL = "use_learned_model"
CONF_RETRAIN_HOURS = "retrain_hours"
CONF_MIN_TRAIN_SAMPLES = "min_train_samples"
CONF_TRAIN_DAYS = "train_days"
CONF_BACKTEST_TEST_DAYS = "backtest_test_days"
CONF_ROUND_TRIP_EFF = "round_trip_eff"

CONF_CHARGE_MARGIN_EUR_PER_KWH = "charge_margin_eur_per_kwh"
CONF_RESERVE_ANCHOR = "reserve_anchor"
CONF_RESERVE_CHEAP_BAND = "reserve_cheap_band"
CONF_RETENTION_HOURLY_DAYS = "retention_hourly_days"
CONF_ADDON_ENABLED = "addon_enabled"
CONF_ADDON_URL = "addon_url"
CONF_ADDON_TIMEOUT = "addon_timeout"
CONF_FORECAST_SERVICE = "forecast_service"   # persisted list of forecast config-entry ids

# Entity-id config keys
CONF_ENT_SETPOINT = "ent_setpoint"
CONF_ENT_ENGAGE = "ent_engage"
CONF_ENT_WORKMODE = "ent_workmode"
CONF_ENT_SOC = "ent_soc"
CONF_ENT_BATTERY_POWER = "ent_battery_power"
CONF_ENT_PV_POWER = "ent_pv_power"
CONF_ENT_METER_POWER = "ent_meter_power"  # single scalar, + = grid import (Anker X1 meter)
CONF_ENT_INVERTER_LOSS = "ent_inverter_loss"
CONF_ENT_PRICE = "ent_price"
CONF_ENT_PV_TODAY = "ent_pv_today"  # list
CONF_ENT_PV_TOMORROW = "ent_pv_tomorrow"  # list
CONF_ENT_PV_PEAK_TODAY = "ent_pv_peak_today"        # list, index-aligned with CONF_ENT_PV_TODAY
CONF_ENT_PV_PEAK_TOMORROW = "ent_pv_peak_tomorrow"  # list, index-aligned with CONF_ENT_PV_TOMORROW
CONF_ENT_IRRADIANCE = "ent_irradiance"
CONF_ENT_SUN = "ent_sun"
CONF_ENT_TEMP = "ent_temp"
CONF_ENT_WEATHER_FORECAST = "ent_weather_forecast"
CONF_ENT_EXPORT_PRICE = "ent_export_price"
CONF_PERSON_ENTITIES = "person_entities"  # list of person.* entity ids (options-only)

# Charge trough look-back config key
CONF_CHARGE_TROUGH_LOOKBACK_H = "charge_trough_lookback_h"

# Price-slot resolution: "auto" (detect from datetime spacing) | "15" | "30" | "60"
CONF_SLOT_RESOLUTION = "slot_resolution"

# Static tariff mode (price source selection) — France/EDF has no dynamic price
# integration.  price_mode="static" synthesizes slots from these flat/HP-HC values.
CONF_PRICE_MODE = "price_mode"
CONF_STATIC_PRICE_IMPORT = "static_price_import"
CONF_STATIC_PRICE_OFFPEAK = "static_price_offpeak"
CONF_STATIC_OFFPEAK_HOURS = "static_offpeak_hours"
CONF_STATIC_PRICE_EXPORT = "static_price_export"
PRICE_MODE_SENSOR = "sensor"
PRICE_MODE_STATIC = "static"

# Defaults (spec §8)
DEFAULT_CAPACITY_KWH = 10.0
DEFAULT_SOC_FLOOR = 5.0
DEFAULT_SOC_TARGET = 97.0
FIRMWARE_SOC_FLOOR = 5.0  # X1 hardware floor (%): discharge stops here regardless of cfg.soc_floor
# Device-derived (X1 nominal charge ceiling): forced at setup in __init__.py,
# never a UI option.
DEFAULT_MAX_CHARGE_W = 6000.0
DEFAULT_ETA_CHARGE = 0.92
# Heuristic-scheduler / guard knobs (deadline_buffer_min … lookback_days):
# const-only by design — read via Config field defaults, not exposed in any flow.
DEFAULT_DEADLINE_BUFFER_MIN = 60
DEFAULT_PEAK_K = 1.3
DEFAULT_PEAK_AFTER_HOUR = 15
DEFAULT_MIN_DWELL_MIN = 15
DEFAULT_DEADBAND_W = 300.0
DEFAULT_LOOKBACK_DAYS = 14
DEFAULT_RETENTION_DAYS = 90
DEFAULT_USE_LEARNED_MODEL = True
DEFAULT_RETRAIN_HOURS = 24
DEFAULT_MIN_TRAIN_SAMPLES = 2000
# Bucketed tier now trains on samples_hourly energy rollups (one FeatureRow per
# hour) rather than per-tick W samples; 48h = 2 days of hourly rollups. Replaces
# the 2000-tick min_train_samples gate for this tier. min_train_samples / its
# config option are kept-dead (still read elsewhere) by decision.
DEFAULT_MIN_TRAIN_HOURS = 48
DEFAULT_TRAIN_DAYS = 14
DEFAULT_BACKTEST_TEST_DAYS = 3
DEFAULT_ROUND_TRIP_EFF = 0.85       # battery charge+discharge round-trip

DEFAULT_CHARGE_MARGIN_EUR_PER_KWH = 0.0
DEFAULT_ENT_WEATHER_FORECAST = "weather.forecast_home"
DEFAULT_ENT_EXPORT_PRICE = ""  # empty = no dedicated sensor; controller mirrors import price
DEFAULT_RETENTION_HOURLY_DAYS = 730
DEFAULT_ADDON_ENABLED = False
DEFAULT_ADDON_URL = "http://local-anker_x1_forecast:8099"
DEFAULT_ADDON_TIMEOUT = 5

# Water-value planner defaults.  Deliberately const-only tunables: there is no
# options-schema field for these (Config reads the constant directly, by design —
# not exposed for end-user tuning via the UI).
DEFAULT_TROUGH_PERCENTILE = 30.0       # percentile of lookahead prices a trough must beat
DEFAULT_TROUGH_LOOKAHEAD_H = 48        # hours of forward prices scanned for the trough
DEFAULT_MIN_HORIZON_H = 6              # trough must be at least this many hours out
DEFAULT_WATER_VALUE_FACTOR = 1.0       # scales the terminal water value v
DEFAULT_CLAMP_WATER_VALUE_NONNEG = True
DEFAULT_END_SOC_DEADBAND = 0.25        # kWh deadband on the current-hour committed grid charge
DEFAULT_CHARGE_WINDOW_PRICE_BAND = 0.005  # €/kWh: max spread above trough price to allow charging
# Hours of real-price look-back for the cheap-charge band trough.  trough[h] is the
# min effective price over [h - lookback, horizon_edge) so an UP-SLOPE hour after the
# day's trough is judged against that trough and blocked by the band (no expensive
# post-trough top-ups).  0 = look-back off (forward-only per-hour).  Mirror of
# DEFAULT_EXPORT_PEAK_LOOKBACK_H.
DEFAULT_CHARGE_TROUGH_LOOKBACK_H = 8

# Export / arbitrage config keys (A2)
CONF_ENABLE_EXPORT = "enable_export"
CONF_MAX_EXPORT_W = "max_export_w"
CONF_GRID_EXPORT_LIMIT_W = "grid_export_limit_w"
CONF_CYCLE_COST_EUR_PER_KWH = "cycle_cost_eur_per_kwh"
CONF_EXPORT_EPS_LO_KWH = "export_eps_lo_kwh"
CONF_EXPORT_EPS_HI_KWH = "export_eps_hi_kwh"
CONF_EXPORT_DWELL_MIN = "export_dwell_min"
CONF_EXPORT_FEE_EUR_PER_KWH = "export_fee_eur_per_kwh"
CONF_EXPORT_PEAK_BAND_FRAC = "export_peak_band_frac"
CONF_EXPORT_PEAK_LOOKBACK_H = "export_peak_lookback_h"
CONF_EXPORT_MIN_BLOCK_KWH = "export_min_block_kwh"
CONF_EXPORT_LOAD_COMP_FACTOR = "export_load_comp_factor"
# Persistence price prior config keys (Plan B)
CONF_PRICE_HISTORY_DAYS = "price_history_days"
CONF_PRICE_BLEND_WEIGHT_TODAY = "price_blend_weight_today"
CONF_ANTICIPATION_CONFIDENCE_HAIRCUT = "anticipation_confidence_haircut"
CONF_ANTICIPATION_MARGIN_EUR_PER_KWH = "anticipation_margin_eur_per_kwh"

# Export / arbitrage defaults (A2; per Global Constraints)
DEFAULT_ENABLE_EXPORT = True
# Device-derived (X1 nominal discharge ceiling): forced at setup in
# __init__.py, never a UI option.  Nominal discharge is 6600 W but the
# net-export setpoint ceiling is ~6000 W — kept at 6000 for parity.
DEFAULT_MAX_EXPORT_W = 6000.0
DEFAULT_GRID_EXPORT_LIMIT_W = 6000.0    # configurable grid-connection cap
DEFAULT_CYCLE_COST_EUR_PER_KWH = 0.04  # battery cycle degradation cost (€/kWh stored)
# Two-sided surplus hysteresis band (mirrors decide_state's eps_lo/eps_hi)
DEFAULT_EXPORT_EPS_LO_KWH = 0.2        # disengage below this surplus
DEFAULT_EXPORT_EPS_HI_KWH = 0.4        # engage above this surplus
DEFAULT_EXPORT_DWELL_MIN = 15          # dwell before engage/disengage transition (minutes)
DEFAULT_EXPORT_FEE_EUR_PER_KWH = 0.02  # €/kWh feed-in fee subtracted from export price
# Export admitted only within this fraction below the horizon peak export price.
# 0.12 = export when effective price >= peak * (1 - 0.12). Tune from first live day.
DEFAULT_EXPORT_PEAK_BAND_FRAC = 0.12
# Hours of recent-past peak the export band remembers.  peak_from[h] is the max
# effective export price over [h - lookback, end] so a post-peak DOWN-SLOPE hour
# is judged against the recent peak and blocked by the band (no reduced-price
# dribbles).  0 = legacy forward-only suffix-max.  Tune from first live day.
DEFAULT_EXPORT_PEAK_LOOKBACK_H = 4
# Minimum total AC kWh a contiguous battery-export run must reach to survive the
# post-DP filter.  0.0 = no-op (restores exact current behaviour).  Default 0.5 kWh
# ≈ €0.05–0.15 of export revenue — enough to justify inverter actuation.
DEFAULT_EXPORT_MIN_BLOCK_KWH = 0.5
# Fraction of live house load added back to the export setpoint so the gross
# battery discharge delivers the DP's planned NET grid export (firmware serves
# house first, exports remainder).  1.0 = full compensation; 0.0 = legacy
# (net-as-gross, under-exports by house load).
DEFAULT_EXPORT_LOAD_COMP_FACTOR = 1.0
# Hours over which the live executor drains surplus-above-reserve when a financial
# export is planned. 0.0 = drain over one controller tick → decisive dump at the
# export cap, stopping at the live reserve. 1.0 ≈ legacy ~1-hour exponential
# (rollback). Const-only; not in the options flow.
DEFAULT_EXPORT_DRAIN_WINDOW_H = 0.0

# Persistence price prior defaults (Plan B)
DEFAULT_PRICE_HISTORY_DAYS = 8          # rolling realized-price store depth (days)
DEFAULT_PRICE_BLEND_WEIGHT_TODAY = 0.5  # today vs same-weekday-last-week blend weight
DEFAULT_ANTICIPATION_CONFIDENCE_HAIRCUT = 0.15  # discount on the estimated morning price
DEFAULT_ANTICIPATION_MARGIN_EUR_PER_KWH = 0.02  # estimate must beat tonight by this (€/kWh)

# SoC drift-hedge config keys
CONF_SOC_HEDGE_FRACTION = "soc_hedge_fraction"
CONF_SOC_DRIFT_DEADBAND_KWH = "soc_drift_deadband_kwh"
CONF_SOC_DRIFT_DECAY_HALFLIFE_H = "soc_drift_decay_halflife_h"
# Fraction 0.0 = OFF (byte-identical / parity-safe). Deadband ignores sub-0.3 kWh drift (release
# at 0.5× = 0.15 kWh, derived). Decay 0.0 = OFF (a bad morning's deficit is real; only
# grid-charge recovery / over-delivery shrinks it).
DEFAULT_SOC_HEDGE_FRACTION = 0.0
DEFAULT_SOC_DRIFT_DEADBAND_KWH = 0.3
DEFAULT_SOC_DRIFT_DECAY_HALFLIFE_H = 0.0

# Intraday residual corrector (Layer A) config keys
CONF_LOAD_ADAPT_FRACTION = "load_adapt_fraction"
CONF_LOAD_ADAPT_WINDOW_H = "load_adapt_window_h"
CONF_LOAD_ADAPT_FADE_H = "load_adapt_fade_h"
# fraction=0.0 disables (byte-identical planning).
DEFAULT_LOAD_ADAPT_FRACTION = 0.7
DEFAULT_LOAD_ADAPT_WINDOW_H = 5
DEFAULT_LOAD_ADAPT_FADE_H = 8

# Measured efficiency curve (eta as a function of DC power) config key + tunables.
# use_measured_eta=False keeps the static eta_charge/round_trip_eff behavior
# (byte-identical / parity-safe); True switches to the recorder-derived curve.
CONF_USE_MEASURED_ETA = "use_measured_eta"
DEFAULT_USE_MEASURED_ETA = False

# Static tariff mode defaults.  offpeak price 0.0 = flat-only; export 0.0 = no
# export credit and never mirrors import (mirror = NL salderen assumption).
DEFAULT_PRICE_MODE = PRICE_MODE_SENSOR
DEFAULT_STATIC_PRICE_IMPORT = 0.25
DEFAULT_STATIC_PRICE_OFFPEAK = 0.0
DEFAULT_STATIC_OFFPEAK_HOURS = ""
DEFAULT_STATIC_PRICE_EXPORT = 0.0
EFFICIENCY_DC_BIN_EDGES_W = [400.0, 800.0, 1500.0, 2500.0, 4000.0]
EFFICIENCY_MIN_RUNS = 10
EFFICIENCY_MIN_DC_KWH = 2.0
EFFICIENCY_DSOC_GATE_PCT = 3.0
EFFICIENCY_ENVELOPE = (0.50, 1.02)
EFFICIENCY_WINDOW_DAYS = 30
EFFICIENCY_HYSTERESIS_W = 150.0
EFFICIENCY_CACHE_SECONDS = 3600

SETPOINT_STEP_W = 100.0
DEFAULT_FALLBACK_LOAD_W = 400.0
# Conservative synthetic solar pickup hour (UTC) used as a ride-out endpoint when the
# sun entity is unavailable and find_next_solar_pickup returns None.  The reserve is
# sized to cover P50 load until the next occurrence of this hour, so the battery can
# survive the 23:00→morning gap even without a real sunrise forecast.  The firmware
# 5% hard floor backstops this estimate.  At NL latitude (52°N) sunrise in summer is
# ≈05:00 UTC, so 08:00 UTC is a conservative (safe, slightly over-sized) pick.
FALLBACK_SOLAR_PICKUP_HOUR_UTC = 8  # UTC hour
RESERVE_WINDOW_MAX_H = 24  # cap the ride-out walk so a recovery-free multi-cloudy
# stretch cannot bleed the NEXT night's drawdown into today's reserve.

# Ride-to-trough reserve (rev-2) — anchor selector + cheap-relief band
RESERVE_ANCHOR_TROUGH = "trough"    # new default: early-break at first cheap grid hour
RESERVE_ANCHOR_LEGACY = "legacy"    # rollback: old debit-to-signed-trough + price-prior
DEFAULT_RESERVE_ANCHOR = RESERVE_ANCHOR_TROUGH
DEFAULT_RESERVE_CHEAP_BAND = 0.20   # a later hour is "relief" within 20% of its OWN forward trough
RESERVE_CHEAP_BAND_EPS = 0.02       # €/kWh floor on the band denominator (near-zero/neg NL prices)
SLOT_RESOLUTION_AUTO = "auto"
DEFAULT_SLOT_RESOLUTION = SLOT_RESOLUTION_AUTO
SETPOINT_MIN_W = -6000.0
SETPOINT_MAX_W = 6000.0  # NET-EXPORT ceiling per A1 (full ~6000W, no firmware cap)
WORKMODE_SELF = "Self-consumption"
PRICE_SCALE = 1e7  # Zonneplan forecast electricity_price integer scaling

# The 5 hard Anker-role entities (SOC, battery power, setpoint, workmode,
# engage) are resolved at config time from CONF_ANKER_DEVICE via
# anker_resolver.resolve_anker_config, so they are intentionally absent here.
# Meter power / inverter loss are SOFT Anker roles (also resolved via
# resolve_anker_config, but a miss is never fatal): they ship a
# DEFAULT_ENTITIES fallback below that runtime readers apply via
# data.get(CONF_ENT_*, DEFAULT_ENTITIES[CONF_ENT_*]) whenever the resolver
# didn't set them (soft-role miss, or a config predating the resolver).
DEFAULT_ENTITIES = {
    # Anker-device soft-role fallbacks (also resolved per-device by anker_resolver).
    CONF_ENT_PV_POWER: "sensor.anker_x1_usable_pv_power",
    CONF_ENT_METER_POWER: "sensor.anker_x1_meter_total_power",
    CONF_ENT_INVERTER_LOSS: "sensor.anker_x1_inverter_loss",
    # HA-universal defaults (not NL-specific).
    CONF_ENT_SUN: "sun.sun",
    CONF_ENT_TEMP: DEFAULT_ENT_WEATHER_FORECAST,
    CONF_ENT_WEATHER_FORECAST: DEFAULT_ENT_WEATHER_FORECAST,
    CONF_ENT_EXPORT_PRICE: DEFAULT_ENT_EXPORT_PRICE,
    # NL-install third-party defaults removed (ent_price, ent_irradiance, and the
    # ent_pv_today/tomorrow/peak_* lists): every runtime reader tolerates a
    # blank/missing id (None-degrade); see Tasks 4-6.
}

# --- Anker X1 device picker ---
ANKER_X1_DOMAIN = "anker_x1"
CONF_ANKER_DEVICE = "anker_device"
# x1 config-key -> Anker entity unique_id suffix.  Matched EXACTLY against
# f"{anker_entry_id}_{suffix}".  Workmode uses the *select* suffix
# (work_mode_select); _work_mode alone is the enum sensor and must not match.
# HARD roles: a miss is appended to resolve_anker_config's `missing` list and
# blocks config-flow setup/reload (anker_roles_missing).
ANKER_ROLE_SUFFIXES: dict[str, str] = {
    CONF_ENT_SOC: "soc",
    CONF_ENT_BATTERY_POWER: "battery_power",
    CONF_ENT_SETPOINT: "battery_setpoint",
    CONF_ENT_WORKMODE: "work_mode_select",
    CONF_ENT_ENGAGE: "modbus_control",
}
# SOFT roles: resolved opportunistically (like ANKER_CAPACITY_SUFFIX below) —
# a miss is omitted from resolved_values and NEVER appended to `missing`, so it
# never blocks setup/reload.  Older anker_x1 versions may not ship these
# entities yet; runtime falls back to DEFAULT_ENTITIES via .get() everywhere.
ANKER_SOFT_ROLE_SUFFIXES: dict[str, str] = {
    CONF_ENT_METER_POWER: "meter_total_power",
    CONF_ENT_INVERTER_LOSS: "inverter_loss",
    # Anker-native usable PV power replaces the NL-specific GoodWe sensor.solar_power
    # default; resolved per-device, soft (a miss falls back to DEFAULT_ENTITIES).
    CONF_ENT_PV_POWER: "usable_pv_power",
}
ANKER_CAPACITY_SUFFIX = "battery_nominal_capacity"
