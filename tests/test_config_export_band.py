"""C2a: export_peak_band_frac config knob."""

from custom_components.anker_x1_smartgrid import const
from custom_components.anker_x1_smartgrid.config_flow import _options_schema
from custom_components.anker_x1_smartgrid.models import Config


def test_default_value_is_0_12():
    assert Config().export_peak_band_frac == 0.12


def test_const_default():
    assert const.DEFAULT_EXPORT_PEAK_BAND_FRAC == 0.12
    assert const.CONF_EXPORT_PEAK_BAND_FRAC == "export_peak_band_frac"


def test_from_dict_threads_override():
    assert Config.from_dict({"export_peak_band_frac": 0.25}).export_peak_band_frac == 0.25


def test_options_schema_exposes_knob():
    schema = _options_schema({})
    keys = {str(m.schema) for sec in schema.schema.values() for m in sec.schema.schema}
    assert const.CONF_EXPORT_PEAK_BAND_FRAC in keys


def test_export_peak_lookback_default_is_4():
    from custom_components.anker_x1_smartgrid import const
    from custom_components.anker_x1_smartgrid.models import Config

    assert const.DEFAULT_EXPORT_PEAK_LOOKBACK_H == 4
    # Unset key falls back to the dataclass default (live-entry deploy path).
    cfg = Config.from_dict({"capacity_kwh": 10.0})
    assert cfg.export_peak_lookback_h == 4


def test_export_peak_lookback_round_trips_from_dict():
    from custom_components.anker_x1_smartgrid.models import Config

    cfg = Config.from_dict({"capacity_kwh": 10.0, "export_peak_lookback_h": 5})
    assert cfg.export_peak_lookback_h == 5
