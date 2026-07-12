"""Plan B: the four anticipation tunables are wired the same way as
export_peak_band_frac (const default → Config field → from_dict override → options schema)."""

from custom_components.anker_x1_smartgrid import config_flow, const
from custom_components.anker_x1_smartgrid.models import Config


def test_anticipation_tunable_defaults():
    cfg = Config()
    assert cfg.price_history_days == 8
    assert cfg.price_blend_weight_today == 0.5
    assert cfg.anticipation_confidence_haircut == 0.15
    assert cfg.anticipation_margin_eur_per_kwh == 0.02


def test_anticipation_tunables_round_trip_through_from_dict():
    cfg = Config.from_dict(
        {
            const.CONF_PRICE_HISTORY_DAYS: 14,
            const.CONF_PRICE_BLEND_WEIGHT_TODAY: 0.7,
            const.CONF_ANTICIPATION_CONFIDENCE_HAIRCUT: 0.25,
            const.CONF_ANTICIPATION_MARGIN_EUR_PER_KWH: 0.05,
        }
    )
    assert cfg.price_history_days == 14
    assert cfg.price_blend_weight_today == 0.7
    assert cfg.anticipation_confidence_haircut == 0.25
    assert cfg.anticipation_margin_eur_per_kwh == 0.05
    # CONF string identity == field name (the from_dict binding contract).
    assert const.CONF_PRICE_HISTORY_DAYS == "price_history_days"
    assert const.CONF_PRICE_BLEND_WEIGHT_TODAY == "price_blend_weight_today"
    assert const.CONF_ANTICIPATION_CONFIDENCE_HAIRCUT == "anticipation_confidence_haircut"
    assert const.CONF_ANTICIPATION_MARGIN_EUR_PER_KWH == "anticipation_margin_eur_per_kwh"


def test_anticipation_tunables_in_options_schema():
    schema = config_flow._options_schema({}, services={})
    keys = {str(m.schema) for sec in schema.schema.values() for m in sec.schema.schema}
    assert const.CONF_PRICE_HISTORY_DAYS in keys
    assert const.CONF_PRICE_BLEND_WEIGHT_TODAY in keys
    assert const.CONF_ANTICIPATION_CONFIDENCE_HAIRCUT in keys
    assert const.CONF_ANTICIPATION_MARGIN_EUR_PER_KWH in keys
