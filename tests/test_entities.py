from custom_components.anker_x1_smartgrid.switch import X1EnableSwitch
from custom_components.anker_x1_smartgrid.sensor import (
    X1SolarChargeSensor,
    X1RegretEurSensor,
    X1OverBuyKwhSensor,
    X1UnderBuyKwhSensor,
)


class _Ctl:
    def __init__(self):
        self.enabled = True
        self.saved_enabled = None
        self.last_status = {
            "state": "forcing",
            "setpoint_w": -3000.0,
            "solar_charge_kwh": 1.9,
        }

    async def set_enabled(self, value):
        self.enabled = value
        self.saved_enabled = value  # stands in for persistence


async def test_enable_switch_toggles_controller():
    ctl = _Ctl()
    sw = X1EnableSwitch(ctl, "e1")
    assert sw.is_on is True
    await sw.async_turn_off()
    assert ctl.enabled is False
    assert ctl.saved_enabled is False   # persisted on toggle
    await sw.async_turn_on()
    assert ctl.enabled is True
    assert ctl.saved_enabled is True


def test_solar_charge_sensor_reads_status():
    ctl = _Ctl()
    s = X1SolarChargeSensor(ctl, "e1")
    assert s.native_value == 1.9


class _RegretCtl:
    """Minimal controller stub with regret keys in last_status."""
    last_status = {
        "regret_eur": 0.42,
        "over_buy_kwh": 1.1,
        "under_buy_kwh": 0.3,
    }


def test_regret_eur_sensor_reads_status():
    ctl = _RegretCtl()
    s = X1RegretEurSensor(ctl, "e1")
    assert s.native_value == 0.42
    assert s._attr_native_unit_of_measurement == "EUR"


def test_over_buy_kwh_sensor_reads_status():
    ctl = _RegretCtl()
    s = X1OverBuyKwhSensor(ctl, "e1")
    assert s.native_value == 1.1
    assert s._attr_native_unit_of_measurement == "kWh"


def test_under_buy_kwh_sensor_reads_status():
    ctl = _RegretCtl()
    s = X1UnderBuyKwhSensor(ctl, "e1")
    assert s.native_value == 0.3
    assert s._attr_native_unit_of_measurement == "kWh"


def test_regret_sensors_return_none_when_no_regret_data():
    """Sensors must return None gracefully when last_regret hasn't been computed yet."""
    class _NoRegretCtl:
        last_status = {"regret_eur": None, "over_buy_kwh": None, "under_buy_kwh": None}

    ctl = _NoRegretCtl()
    assert X1RegretEurSensor(ctl, "e1").native_value is None
    assert X1OverBuyKwhSensor(ctl, "e1").native_value is None
    assert X1UnderBuyKwhSensor(ctl, "e1").native_value is None
