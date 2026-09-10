"""Calibration options: defaults, Config wiring, _TUNABLES membership."""

from custom_components.anker_x1_smartgrid import const
from custom_components.anker_x1_smartgrid.config_flow import _TUNABLES
from custom_components.anker_x1_smartgrid.models import Config


def test_defaults_ship_on():
    cfg = Config()
    assert cfg.calibration_enabled is True
    assert cfg.calibration_interval_days == 5
    assert cfg.calibration_top_soc == 100.0
    assert cfg.calibration_dwell_h == 0.5


def test_tuning_consts():
    assert const.CALIBRATION_PRICE_PERCENTILE == 30.0
    assert const.CALIBRATION_GRACE_DAYS == 7
    assert const.CALIBRATION_HOLD_TOLERANCE == 1.0


def test_new_cost_placement_consts():
    assert const.CALIBRATION_MIN_START_SOC == 95.0
    assert const.CALIBRATION_COST_ALLOWANCE_EUR == 0.50
    assert const.CALIBRATION_OVERDUE_COST_CAP_EUR == 1.00
    assert const.CALIBRATION_MAX_DWELL_H == 0.5


def test_from_dict_clamps_a_stored_dwell_above_the_max():
    """Stored options are not re-validated: a dwell above
    CALIBRATION_MAX_DWELL_H is clamped to it, one at or below passes through."""
    assert Config.from_dict({"calibration_dwell_h": 2.0}).calibration_dwell_h == 0.5
    assert Config.from_dict({"calibration_dwell_h": 0.5}).calibration_dwell_h == 0.5
    assert Config.from_dict({"calibration_dwell_h": 0.25}).calibration_dwell_h == 0.25


def test_top_soc_schema_admits_the_firmware_cap():
    """The default is the 100% cap, so a validator that stopped short of it
    would reject the shipped value on the first options save."""
    validator = next(v for name, _d, v in _TUNABLES if name == const.CONF_CALIBRATION_TOP_SOC)
    assert validator(100.0) == 100.0


def test_all_four_options_are_tunable():
    """Outside _TUNABLES an option is wiped by the next UI options save."""
    keys = {name for name, _default, _validator in _TUNABLES}
    assert const.CONF_CALIBRATION_ENABLED in keys
    assert const.CONF_CALIBRATION_INTERVAL_DAYS in keys
    assert const.CONF_CALIBRATION_TOP_SOC in keys
    assert const.CONF_CALIBRATION_DWELL_H in keys
