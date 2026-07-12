"""Shared test fixtures."""
import pytest

from custom_components.anker_x1_smartgrid import const
from custom_components.anker_x1_smartgrid.recorder import DataRecorder

pytest_plugins = ["pytest_homeassistant_custom_component"]


# Canonical entity ids for tests that build a full config dict.  The Anker-role
# ids are resolved from the picked device at config time; the price/irradiance/PV
# ids used to live in const.DEFAULT_ENTITIES but were removed as NL-install
# defaults.  Both groups are stubbed here so `{**DEFAULT_ENTITIES,
# **ANKER_TEST_ENTITIES}` fixtures stay byte-identical to pre-removal behaviour.
ANKER_TEST_ENTITIES = {
    const.CONF_ENT_SETPOINT: "number.anker_x1_battery_setpoint_charge_discharge",
    const.CONF_ENT_ENGAGE: "switch.anker_x1_modbus_control_hand_battery_to_ha_vpp",
    const.CONF_ENT_WORKMODE: "select.anker_x1_work_mode",
    const.CONF_ENT_SOC: "sensor.anker_x1_battery_soc",
    const.CONF_ENT_BATTERY_POWER: "sensor.anker_x1_battery_power",
    const.CONF_ENT_PRICE: "sensor.zonneplan_current_electricity_tariff",
    const.CONF_ENT_IRRADIANCE: "sensor.knmi_solar_irradiance",
    const.CONF_ENT_PV_TODAY: ["sensor.home_energy_production_today_remaining"],
    const.CONF_ENT_PV_TOMORROW: ["sensor.home_energy_production_tomorrow"],
    const.CONF_ENT_PV_PEAK_TODAY: ["sensor.home_power_highest_peak_time_today"],
    const.CONF_ENT_PV_PEAK_TOMORROW: ["sensor.home_power_highest_peak_time_tomorrow"],
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading custom integrations in all tests."""
    yield


@pytest.fixture
def recorder_db(tmp_path):
    """Real DataRecorder backed by a throwaway sqlite file under tmp_path."""
    rec = DataRecorder(str(tmp_path / "t.db"))
    yield rec
    rec.close()
