from custom_components.anker_x1_smartgrid.sensor import (
    X1LoadMaeSensor,
    X1HorizonEnergyMae24hSensor,
    X1HorizonEnergyMae12hSensor,
    X1PinballP50Sensor,
    X1PinballP80Sensor,
    X1ActiveModelSensor,
)


class _Ctl:
    def __init__(self):
        self.last_status = {"load_mae": 123.4}


def test_load_mae_sensor():
    s = X1LoadMaeSensor(_Ctl(), "e1")
    assert s.native_value == 123.4
    assert s.native_unit_of_measurement == "W"


# ---------------------------------------------------------------------------
# New ML-metric sensors (P3-T4)
# ---------------------------------------------------------------------------


class _MlCtl:
    """Stub controller exposing all new backtest metric keys."""

    def __init__(self):
        self.last_status = {
            "load_mae": 42.0,
            "horizon_energy_mae_24h": 1.5,
            "horizon_energy_mae_12h": 0.8,
            "pinball_p50": 12.3,
            "pinball_p80": None,
            "active_model": "bucketed",
        }


def test_horizon_energy_mae_24h_sensor():
    s = X1HorizonEnergyMae24hSensor(_MlCtl(), "e1")
    assert s.native_value == 1.5
    assert s.native_unit_of_measurement == "kWh"


def test_horizon_energy_mae_12h_sensor():
    s = X1HorizonEnergyMae12hSensor(_MlCtl(), "e1")
    assert s.native_value == 0.8
    assert s.native_unit_of_measurement == "kWh"


def test_pinball_p50_sensor():
    s = X1PinballP50Sensor(_MlCtl(), "e1")
    assert s.native_value == 12.3
    assert s.native_unit_of_measurement == "W"


def test_pinball_p80_sensor_none():
    """Sensor returns None when pinball_p80 is unavailable (model lacks quantile support)."""
    s = X1PinballP80Sensor(_MlCtl(), "e1")
    assert s.native_value is None
    assert s.native_unit_of_measurement == "W"


def test_active_model_sensor():
    s = X1ActiveModelSensor(_MlCtl(), "e1")
    assert s.native_value == "bucketed"


def test_active_model_sensor_profile():
    """active_model is 'profile' when no learned model has been trained."""

    class _ProfileCtl:
        last_status = {"active_model": "profile"}

    s = X1ActiveModelSensor(_ProfileCtl(), "e1")
    assert s.native_value == "profile"


def test_new_sensors_none_when_no_backtest():
    """All new metric sensors return None when backtest_result is absent."""

    class _EmptyCtl:
        last_status = {
            "load_mae": None,
            "horizon_energy_mae_24h": None,
            "horizon_energy_mae_12h": None,
            "pinball_p50": None,
            "pinball_p80": None,
            "active_model": "profile",
        }

    for cls in (
        X1HorizonEnergyMae24hSensor,
        X1HorizonEnergyMae12hSensor,
        X1PinballP50Sensor,
        X1PinballP80Sensor,
    ):
        assert cls(_EmptyCtl(), "e1").native_value is None
