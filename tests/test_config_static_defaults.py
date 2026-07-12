"""Static tariff config keys + Config field defaults."""

from custom_components.anker_x1_smartgrid import const
from custom_components.anker_x1_smartgrid.models import Config


def test_price_mode_constants():
    assert const.CONF_PRICE_MODE == "price_mode"
    assert const.PRICE_MODE_SENSOR == "sensor"
    assert const.PRICE_MODE_STATIC == "static"
    assert const.DEFAULT_PRICE_MODE == const.PRICE_MODE_SENSOR


def test_static_config_key_strings():
    assert const.CONF_STATIC_PRICE_IMPORT == "static_price_import"
    assert const.CONF_STATIC_PRICE_OFFPEAK == "static_price_offpeak"
    assert const.CONF_STATIC_OFFPEAK_HOURS == "static_offpeak_hours"
    assert const.CONF_STATIC_PRICE_EXPORT == "static_price_export"


def test_static_config_defaults():
    cfg = Config()
    assert cfg.price_mode == "sensor"
    assert cfg.static_price_import == 0.25
    assert cfg.static_price_offpeak == 0.0
    assert cfg.static_offpeak_hours == ""
    assert cfg.static_price_export == 0.0


def test_static_config_from_dict_override():
    cfg = Config.from_dict(
        {
            "price_mode": "static",
            "static_price_import": 0.30,
            "static_price_offpeak": 0.12,
            "static_offpeak_hours": "01:00-06:00",
            "static_price_export": 0.10,
        }
    )
    assert cfg.price_mode == "static"
    assert cfg.static_price_import == 0.30
    assert cfg.static_price_offpeak == 0.12
    assert cfg.static_offpeak_hours == "01:00-06:00"
    assert cfg.static_price_export == 0.10
