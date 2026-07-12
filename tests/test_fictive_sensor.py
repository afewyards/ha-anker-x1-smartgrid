"""Tests for X1FictivePlanSensor (T0.6b).

Mirrors test_entities_plan.py — verifies the fictive_plan sensor
reads last_status["fictive_plan"] and exposes the DP-proposed plan
so the dashboard card (T0.6c) can render it.
"""

from custom_components.anker_x1_smartgrid.sensor import X1FictivePlanSensor


class _Ctl:
    def __init__(self, status):
        self.last_status = status


def test_fictive_sensor_native_value_is_planned_grid_hours():
    horizon = [{"start": "2026-06-22T10:00:00+00:00", "price": 0.05, "mode": "grid", "soc": 20.0}]
    s = X1FictivePlanSensor(
        _Ctl({"fictive_plan": {"horizon": horizon, "deadline": "2026-06-22T18:00:00+00:00", "planned_grid_hours": 3}}),
        "e1",
    )
    assert s.native_value == 3
    assert s.native_unit_of_measurement == "h"


def test_fictive_sensor_extra_state_attributes():
    horizon = [{"start": "2026-06-22T10:00:00+00:00", "price": 0.05, "mode": "grid", "soc": 20.0}]
    deadline = "2026-06-22T18:00:00+00:00"
    s = X1FictivePlanSensor(
        _Ctl({"fictive_plan": {"horizon": horizon, "deadline": deadline, "planned_grid_hours": 3}}),
        "e1",
    )
    attrs = s.extra_state_attributes
    assert attrs["horizon"] == horizon
    assert attrs["deadline"] == deadline


def test_fictive_sensor_absent_fictive_plan_native_value_is_none():
    s = X1FictivePlanSensor(_Ctl({}), "e1")
    assert s.native_value is None


def test_fictive_sensor_absent_fictive_plan_attrs_is_empty_dict():
    s = X1FictivePlanSensor(_Ctl({}), "e1")
    assert s.extra_state_attributes == {}


def test_fictive_sensor_none_value_does_not_raise():
    # last_status has fictive_plan key but it is None
    s = X1FictivePlanSensor(_Ctl({"fictive_plan": None}), "e1")
    assert s.native_value is None
    assert s.extra_state_attributes == {}


def test_fictive_sensor_unique_id():
    s = X1FictivePlanSensor(_Ctl({}), "e1")
    assert s.unique_id == "anker_x1_smartgrid_fictive_plan"


def test_fictive_sensor_name_yields_expected_entity_id():
    """Name 'SmartGrid fictive plan' → HA slugifies to sensor.smartgrid_fictive_plan."""
    s = X1FictivePlanSensor(_Ctl({}), "e1")
    # HA derives entity_id from name; we pin the name so the slug is predictable.
    assert s.name == "SmartGrid fictive plan"


def test_fictive_sensor_zero_planned_grid_hours_returns_zero_not_none():
    """planned_grid_hours=0 must return 0 (not None) — guard keys off the plan dict, not value."""
    s = X1FictivePlanSensor(
        _Ctl({"fictive_plan": {"horizon": [], "deadline": "2026-06-22T18:00:00+00:00", "planned_grid_hours": 0}}),
        "e1",
    )
    assert s.native_value == 0


def test_fictive_sensor_horizon_carries_export_fields():
    """grid_export_w, reserve_soc, self_discharge_w added by G1 propagate verbatim through fictive sensor."""
    horizon = [
        {
            "start": "2026-06-25T10:00:00+00:00",
            "price": 0.25,
            "mode": "grid",
            "soc": 50.0,
            "grid_export_w": 2000.0,
            "reserve_soc": 10.0,
            "self_discharge_w": 4.0,
        }
    ]
    s = X1FictivePlanSensor(
        _Ctl({"fictive_plan": {"horizon": horizon, "deadline": "2026-06-25T18:00:00+00:00", "planned_grid_hours": 2}}),
        "e1",
    )
    attrs = s.extra_state_attributes
    entry = attrs["horizon"][0]
    assert entry["grid_export_w"] == 2000.0
    assert entry["reserve_soc"] == 10.0
    assert entry["self_discharge_w"] == 4.0
