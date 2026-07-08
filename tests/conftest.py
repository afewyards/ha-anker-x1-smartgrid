"""Shared test fixtures."""
import pytest

from custom_components.anker_x1_smartgrid import const

pytest_plugins = ["pytest_homeassistant_custom_component"]


# Canonical Anker-role entity ids for tests that build a full config dict.
# These used to live in const.DEFAULT_ENTITIES but are now resolved from the
# picked Anker device at config time; tests stub them here.
ANKER_TEST_ENTITIES = {
    const.CONF_ENT_SETPOINT: "number.anker_x1_battery_setpoint_charge_discharge",
    const.CONF_ENT_ENGAGE: "switch.anker_x1_modbus_control_hand_battery_to_ha_vpp",
    const.CONF_ENT_WORKMODE: "select.anker_x1_work_mode",
    const.CONF_ENT_SOC: "sensor.anker_x1_battery_soc",
    const.CONF_ENT_BATTERY_POWER: "sensor.anker_x1_battery_power",
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading custom integrations in all tests."""
    yield
