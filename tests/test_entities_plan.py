from custom_components.anker_x1_smartgrid.sensor import X1PlanSensor


class _Ctl:
    def __init__(self, status):
        self.last_status = status


def test_plan_sensor_value_and_attrs():
    horizon = [{"start": "2026-06-20T11:00:00+00:00", "price": 0.3, "mode": "grid", "soc": 55.0}]
    s = X1PlanSensor(
        _Ctl({"plan": {"horizon": horizon, "deadline": "2026-06-20T18:00:00+00:00", "planned_grid_hours": 1}}), "e1"
    )
    assert s.native_value == 1
    assert s.native_unit_of_measurement == "h"
    attrs = s.extra_state_attributes
    assert attrs["horizon"] == horizon
    assert attrs["deadline"] == "2026-06-20T18:00:00+00:00"


def test_plan_sensor_missing_plan_is_safe():
    s = X1PlanSensor(_Ctl({}), "e1")
    assert s.native_value is None
    assert s.extra_state_attributes == {}


def test_plan_sensor_horizon_carries_export_fields():
    """grid_export_w and reserve_soc added by G1 propagate verbatim through the sensor."""
    horizon = [
        {
            "start": "2026-06-25T10:00:00+00:00",
            "price": 0.28,
            "mode": "grid",
            "soc": 60.0,
            "grid_export_w": 1500.0,
            "reserve_soc": 10.0,
            "self_discharge_w": 5.0,
        }
    ]
    s = X1PlanSensor(
        _Ctl({"plan": {"horizon": horizon, "deadline": "2026-06-25T18:00:00+00:00", "planned_grid_hours": 1}}),
        "e1",
    )
    attrs = s.extra_state_attributes
    entry = attrs["horizon"][0]
    assert entry["grid_export_w"] == 1500.0
    assert entry["reserve_soc"] == 10.0
    assert entry["self_discharge_w"] == 5.0


def test_plan_sensor_arbitrage_pnl_present():
    """arbitrage_pnl reads planned_export_revenue_eur (C4) from the top-level last_status dict."""
    horizon = [{"start": "2026-06-25T10:00:00+00:00", "price": 0.28, "mode": "grid", "soc": 60.0}]
    status = {
        "plan": {"horizon": horizon, "deadline": "2026-06-25T18:00:00+00:00", "planned_grid_hours": 1},
        "planned_export_revenue_eur": 0.123456,
        "today_export_pnl_eur": 0.0,
    }
    s = X1PlanSensor(_Ctl(status), "e1")
    assert s.extra_state_attributes["arbitrage_pnl"] == 0.123456


def test_plan_sensor_arbitrage_pnl_missing_is_none():
    """arbitrage_pnl is None when planned_export_revenue_eur is absent from last_status."""
    horizon = [{"start": "2026-06-25T10:00:00+00:00", "price": 0.28, "mode": "grid", "soc": 60.0}]
    status = {
        "plan": {"horizon": horizon, "deadline": "2026-06-25T18:00:00+00:00", "planned_grid_hours": 1},
    }
    s = X1PlanSensor(_Ctl(status), "e1")
    assert s.extra_state_attributes["arbitrage_pnl"] is None
