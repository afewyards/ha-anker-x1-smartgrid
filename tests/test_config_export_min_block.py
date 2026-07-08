"""T1: Config plumbing for export_min_block_kwh."""
from custom_components.anker_x1_smartgrid import const
from custom_components.anker_x1_smartgrid.config_flow import _options_schema
from custom_components.anker_x1_smartgrid.models import Config


def test_default_value_is_0_5():
    assert Config().export_min_block_kwh == 0.5


def test_const_default():
    assert const.DEFAULT_EXPORT_MIN_BLOCK_KWH == 0.5
    assert const.CONF_EXPORT_MIN_BLOCK_KWH == "export_min_block_kwh"


def test_from_dict_round_trips_zero():
    cfg = Config.from_dict({"export_min_block_kwh": 0.0})
    assert cfg.export_min_block_kwh == 0.0


def test_from_dict_round_trips_custom_value():
    cfg = Config.from_dict({"capacity_kwh": 10.0, "export_min_block_kwh": 1.0})
    assert cfg.export_min_block_kwh == 1.0


def test_options_schema_exposes_knob():
    schema = _options_schema({})
    keys = {str(m.schema) for sec in schema.schema.values() for m in sec.schema.schema}
    assert const.CONF_EXPORT_MIN_BLOCK_KWH in keys
