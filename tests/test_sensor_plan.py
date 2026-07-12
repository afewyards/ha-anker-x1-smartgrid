from homeassistant.components.sensor import SensorStateClass

from custom_components.anker_x1_smartgrid.sensor import (
    X1ActiveModelSensor,
    X1DpRegretSensor,
    X1FictivePlanSensor,
    X1HorizonEnergyMae12hSensor,
    X1HorizonEnergyMae24hSensor,
    X1LoadMaeSensor,
    X1OverBuyKwhSensor,
    X1PinballP50Sensor,
    X1PinballP80Sensor,
    X1PlanSensor,
    X1RegretEurSensor,
    X1SetpointSensor,
    X1SolarChargeSensor,
    X1StateSensor,
    X1UnderBuyKwhSensor,
)


def test_plan_horizon_attrs_not_recorded():
    assert "horizon" in X1PlanSensor._unrecorded_attributes
    assert "horizon" in X1FictivePlanSensor._unrecorded_attributes


# ---------------------------------------------------------------------------
# Task 16 — state_class on numeric sensors
#
# HA's CachedProperties metaclass rewrites a class-level `_attr_state_class =
# ...` assignment into a data-descriptor `property` on the class (backing a
# private `__attr_state_class`). Accessing that property via the class
# object itself (`Cls._attr_state_class`) returns the property object, not
# the value — so these assertions instantiate each sensor and read the
# public `state_class` property instead.
# ---------------------------------------------------------------------------


class _Ctl:
    last_status = {}


def test_numeric_sensors_expose_measurement_state_class():
    """Numeric sensors get MEASUREMENT state_class so HA plots long-term stats."""
    numeric_sensors = (
        X1SolarChargeSensor,
        X1SetpointSensor,
        X1LoadMaeSensor,
        X1HorizonEnergyMae24hSensor,
        X1HorizonEnergyMae12hSensor,
        X1PinballP50Sensor,
        X1PinballP80Sensor,
        X1RegretEurSensor,
        X1DpRegretSensor,
        X1OverBuyKwhSensor,
        X1UnderBuyKwhSensor,
    )
    for cls in numeric_sensors:
        instance = cls(_Ctl(), "e1")
        assert instance.state_class == SensorStateClass.MEASUREMENT, cls


def test_non_numeric_sensors_do_not_expose_state_class():
    """Enum/blob sensors must NOT set state_class — it's meaningless for them."""
    non_numeric_sensors = (
        X1StateSensor,
        X1ActiveModelSensor,
        X1PlanSensor,
        X1FictivePlanSensor,
    )
    for cls in non_numeric_sensors:
        instance = cls(_Ctl(), "e1")
        assert instance.state_class is None, cls
