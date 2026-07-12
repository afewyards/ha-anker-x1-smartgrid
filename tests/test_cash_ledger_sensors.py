"""Tests for the battery cash-ledger sensors.

Spec: docs/superpowers/specs/2026-07-10-battery-cash-ledger-design.md §3.
State-class split is deliberate: today = MEASUREMENT (a daily-resetting value
under TOTAL without last_reset corrupts long-term statistics at midnight);
total = TOTAL (never resets, may decrease; LTS sum deltas give per-day charts).
"""

from homeassistant.components.sensor import SensorStateClass

from custom_components.anker_x1_smartgrid.sensor import (
    X1BatteryNetTodaySensor,
    X1BatteryNetTotalSensor,
)


class _Ctl:
    def __init__(self, status):
        self.last_status = status


_STATUS = {
    "battery_net_today_eur": 1.25,
    "battery_net_total_eur": 86.4,
    "today_charge_cost_eur": 0.75,
    "today_export_revenue_eur": 2.0,
}


def test_net_today_state_and_attributes():
    s = X1BatteryNetTodaySensor(_Ctl(_STATUS), "e1")
    assert s.native_value == 1.25
    assert s.extra_state_attributes == {
        "charge_cost_today": 0.75,
        "export_revenue_today": 2.0,
    }


def test_net_today_classes_and_identity():
    s = X1BatteryNetTodaySensor(_Ctl({}), "e1")
    assert s.native_unit_of_measurement == "EUR"
    assert s.state_class == SensorStateClass.MEASUREMENT
    assert s.unique_id == "anker_x1_smartgrid_battery_net_today_eur"
    # Name pins the slug → entity id sensor.smartgrid_battery_net_today,
    # which the Lovelace card binds verbatim.
    assert s.name == "SmartGrid battery net today"


def test_net_total_state_and_identity():
    s = X1BatteryNetTotalSensor(_Ctl(_STATUS), "e1")
    assert s.native_value == 86.4
    assert s.native_unit_of_measurement == "EUR"
    assert s.state_class == SensorStateClass.TOTAL
    assert s.last_reset is None  # never resets; TOTAL tracks signed deltas
    assert s.unique_id == "anker_x1_smartgrid_battery_net_total_eur"
    assert s.name == "SmartGrid battery net total"


def test_sensors_return_none_before_first_tick():
    today = X1BatteryNetTodaySensor(_Ctl({}), "e1")
    total = X1BatteryNetTotalSensor(_Ctl({}), "e1")
    assert today.native_value is None
    assert total.native_value is None
    assert today.extra_state_attributes == {
        "charge_cost_today": None,
        "export_revenue_today": None,
    }


def test_negative_net_is_passed_through():
    s = X1BatteryNetTodaySensor(_Ctl({"battery_net_today_eur": -0.42}), "e1")
    assert s.native_value == -0.42
