from custom_components.anker_x1_smartgrid.models import Config
from custom_components.anker_x1_smartgrid import const, config_flow


def test_defaults_trough_and_band():
    c = Config()
    assert c.reserve_anchor == "trough"
    assert c.reserve_cheap_band == 0.20
    assert const.RESERVE_ANCHOR_TROUGH == "trough"
    assert const.RESERVE_ANCHOR_LEGACY == "legacy"
    assert const.RESERVE_CHEAP_BAND_EPS == 0.02


def test_from_dict_roundtrips_new_fields():
    c = Config.from_dict({"reserve_anchor": "legacy", "reserve_cheap_band": 0.05})
    assert c.reserve_anchor == "legacy"
    assert c.reserve_cheap_band == 0.05


def test_options_schema_exposes_new_keys():
    schema = config_flow._options_schema({})
    keys = {str(m.schema) for sec in schema.schema.values() for m in sec.schema.schema}
    assert const.CONF_RESERVE_ANCHOR in keys
    assert const.CONF_RESERVE_CHEAP_BAND in keys
