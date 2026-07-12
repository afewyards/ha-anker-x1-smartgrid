"""TDD: charge_trough_lookback_h config key + Config field."""

from __future__ import annotations

from custom_components.anker_x1_smartgrid import const
from custom_components.anker_x1_smartgrid.models import Config


def test_default_is_8():
    assert const.DEFAULT_CHARGE_TROUGH_LOOKBACK_H == 8
    assert const.CONF_CHARGE_TROUGH_LOOKBACK_H == "charge_trough_lookback_h"


def test_config_default_field():
    cfg = Config.from_dict({"capacity_kwh": 10.0})
    assert cfg.charge_trough_lookback_h == 8


def test_config_override_maps():
    cfg = Config.from_dict({"capacity_kwh": 10.0, "charge_trough_lookback_h": 0})
    assert cfg.charge_trough_lookback_h == 0
