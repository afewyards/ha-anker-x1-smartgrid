"""Calibration options: defaults, Config wiring, _TUNABLES membership."""

from custom_components.anker_x1_smartgrid import const
from custom_components.anker_x1_smartgrid.config_flow import _TUNABLES
from custom_components.anker_x1_smartgrid.models import Config


def test_defaults_ship_off():
    cfg = Config()
    assert cfg.calibration_enabled is False
    assert cfg.calibration_interval_days == 5
    assert cfg.calibration_top_soc == 97.0
    assert cfg.calibration_dwell_h == 2.0


def test_tuning_consts():
    assert const.CALIBRATION_PRICE_PERCENTILE == 30.0
    assert const.CALIBRATION_GRACE_DAYS == 7


def test_all_four_options_are_tunable():
    """Outside _TUNABLES an option is wiped by the next UI options save."""
    keys = {name for name, _default, _validator in _TUNABLES}
    assert const.CONF_CALIBRATION_ENABLED in keys
    assert const.CONF_CALIBRATION_INTERVAL_DAYS in keys
    assert const.CONF_CALIBRATION_TOP_SOC in keys
    assert const.CONF_CALIBRATION_DWELL_H in keys
