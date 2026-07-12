"""Tests: price-gate config defaults and override."""

from custom_components.anker_x1_smartgrid.models import Config


def test_pricegate_config_defaults():
    cfg = Config()
    assert cfg.round_trip_eff == 0.85


def test_pricegate_config_override():
    cfg = Config.from_dict({"round_trip_eff": 0.9})
    assert cfg.round_trip_eff == 0.9
