from custom_components.anker_x1_smartgrid import const


def test_domain():
    assert const.DOMAIN == "anker_x1_smartgrid"


def test_defaults_present():
    assert const.DEFAULT_SOC_TARGET == 97.0
    assert const.PRICE_SCALE == 1e7


def test_pv_power_entity_default():
    assert const.DEFAULT_ENTITIES[const.CONF_ENT_PV_POWER] == "sensor.anker_x1_usable_pv_power"


def test_meter_power_and_inverter_loss_entities():
    """Single-scalar Anker X1 meter power replaces the 3-phase P1 inputs."""
    assert const.CONF_ENT_METER_POWER == "ent_meter_power"
    assert const.CONF_ENT_INVERTER_LOSS == "ent_inverter_loss"
    assert (
        const.DEFAULT_ENTITIES[const.CONF_ENT_METER_POWER]
        == "sensor.anker_x1_meter_total_power"
    )
    assert (
        const.DEFAULT_ENTITIES[const.CONF_ENT_INVERTER_LOSS]
        == "sensor.anker_x1_inverter_loss"
    )


def test_phase_and_house_load_constants_removed():
    """P1 phase inputs + house-load input sensor are gone (house load is computed)."""
    assert not hasattr(const, "CONF_ENT_PHASE")
    assert not hasattr(const, "CONF_ENT_HOUSE_LOAD")
    assert not hasattr(const, "DEFAULT_ENT_HOUSE_LOAD")


def test_weather_forecast_const():
    assert const.CONF_ENT_WEATHER_FORECAST == "ent_weather_forecast"
    assert const.DEFAULT_ENT_WEATHER_FORECAST == "weather.forecast_home"
    assert const.DEFAULT_ENTITIES[const.CONF_ENT_WEATHER_FORECAST] == "weather.forecast_home"
    assert const.DEFAULT_ENTITIES[const.CONF_ENT_TEMP] == "weather.forecast_home"


def test_default_entities_drops_nl_third_party_ids():
    """NL-install third-party defaults are removed; only Anker-derived + sun kept."""
    for key in (
        const.CONF_ENT_PRICE,
        const.CONF_ENT_IRRADIANCE,
        const.CONF_ENT_PV_TODAY,
        const.CONF_ENT_PV_TOMORROW,
        const.CONF_ENT_PV_PEAK_TODAY,
        const.CONF_ENT_PV_PEAK_TOMORROW,
    ):
        assert key not in const.DEFAULT_ENTITIES
    # Kept:
    assert const.DEFAULT_ENTITIES[const.CONF_ENT_SUN] == "sun.sun"
    assert const.CONF_ENT_EXPORT_PRICE in const.DEFAULT_ENTITIES
    assert const.CONF_ENT_METER_POWER in const.DEFAULT_ENTITIES
    assert const.CONF_ENT_PV_POWER in const.DEFAULT_ENTITIES


def test_retention_hourly_days_const():
    assert const.CONF_RETENTION_HOURLY_DAYS == "retention_hourly_days"
    assert const.DEFAULT_RETENTION_HOURLY_DAYS == 730


def test_default_soc_floor_is_firmware_floor():
    """soc_floor default aligns with the Anker X1 firmware 5% hard floor."""
    from custom_components.anker_x1_smartgrid import const
    assert const.DEFAULT_SOC_FLOOR == 5.0


# ── A2: export hardware constants & config keys ──────────────────────────────

def test_setpoint_max_w():
    """SETPOINT_MAX_W mirrors SETPOINT_MIN_W (full ~6000W ceiling per A1)."""
    assert const.SETPOINT_MAX_W == 6000.0


def test_setpoint_max_is_mirror_of_min():
    assert const.SETPOINT_MAX_W == -const.SETPOINT_MIN_W


def test_export_dwell_and_eps_band_defaults():
    """Standalone hysteresis-band constants used by decide_export_state (C2)."""
    assert const.DEFAULT_EXPORT_DWELL_MIN == 15
    assert const.DEFAULT_EXPORT_EPS_LO_KWH == 0.2
    assert const.DEFAULT_EXPORT_EPS_HI_KWH == 0.4


def test_export_conf_key_strings():
    assert const.CONF_ENABLE_EXPORT == "enable_export"
    assert const.CONF_MAX_EXPORT_W == "max_export_w"
    assert const.CONF_GRID_EXPORT_LIMIT_W == "grid_export_limit_w"
    assert const.CONF_CYCLE_COST_EUR_PER_KWH == "cycle_cost_eur_per_kwh"
    assert const.CONF_EXPORT_EPS_LO_KWH == "export_eps_lo_kwh"
    assert const.CONF_EXPORT_EPS_HI_KWH == "export_eps_hi_kwh"
    assert const.CONF_EXPORT_DWELL_MIN == "export_dwell_min"


def test_export_default_values():
    assert const.DEFAULT_ENABLE_EXPORT is True
    assert const.DEFAULT_MAX_EXPORT_W == 6000.0
    assert const.DEFAULT_GRID_EXPORT_LIMIT_W == 6000.0
    assert const.DEFAULT_CYCLE_COST_EUR_PER_KWH == 0.04
    # eps/dwell shared with standalone constants above
    assert const.DEFAULT_EXPORT_EPS_LO_KWH == 0.2
    assert const.DEFAULT_EXPORT_EPS_HI_KWH == 0.4
    assert const.DEFAULT_EXPORT_DWELL_MIN == 15


def test_default_export_fee_is_two_cents():
    from custom_components.anker_x1_smartgrid import const
    assert const.CONF_EXPORT_FEE_EUR_PER_KWH == "export_fee_eur_per_kwh"
    assert const.DEFAULT_EXPORT_FEE_EUR_PER_KWH == 0.02


def test_dead_keys_removed():
    """Confirm dead config keys are no longer exported."""
    assert not hasattr(const, "CONF_GUARD_W_PER_PHASE")
    assert not hasattr(const, "CONF_MIN_EXPORT_CHUNK_KWH")
    assert not hasattr(const, "CONF_HGBR_MIN_DAYS")
    assert not hasattr(const, "CONF_ENT_P1_TOTAL")


def test_default_entities_excludes_anker_roles():
    """Anker-role entity ids are resolved from the picked device, not hardcoded."""
    for key in (
        const.CONF_ENT_SOC,
        const.CONF_ENT_BATTERY_POWER,
        const.CONF_ENT_SETPOINT,
        const.CONF_ENT_WORKMODE,
        const.CONF_ENT_ENGAGE,
    ):
        assert key not in const.DEFAULT_ENTITIES


def test_export_drain_window_default():
    from custom_components.anker_x1_smartgrid import const
    from custom_components.anker_x1_smartgrid.models import Config
    assert const.DEFAULT_EXPORT_DRAIN_WINDOW_H == 0.0
    assert Config().export_drain_window_h == 0.0
