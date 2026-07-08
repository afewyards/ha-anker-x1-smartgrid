from custom_components.anker_x1_smartgrid import const
from custom_components.anker_x1_smartgrid.models import Config


def test_default_factor_is_one():
    assert const.DEFAULT_EXPORT_LOAD_COMP_FACTOR == 1.0
    assert Config().export_load_comp_factor == 1.0


def test_from_dict_overrides_factor():
    cfg = Config.from_dict({"export_load_comp_factor": 0.0})
    assert cfg.export_load_comp_factor == 0.0


def test_conf_key_constant():
    assert const.CONF_EXPORT_LOAD_COMP_FACTOR == "export_load_comp_factor"
