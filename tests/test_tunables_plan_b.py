"""Plan B: the two price-prior tunables are wired the same way as
export_peak_band_frac (const default → Config field → from_dict override → options schema)."""

from custom_components.anker_x1_smartgrid import config_flow, const
from custom_components.anker_x1_smartgrid.models import Config


def test_price_prior_tunable_defaults():
    cfg = Config()
    assert cfg.price_history_days == 8
    assert cfg.price_blend_weight_today == 0.5


def test_price_prior_tunables_round_trip_through_from_dict():
    cfg = Config.from_dict(
        {
            const.CONF_PRICE_HISTORY_DAYS: 14,
            const.CONF_PRICE_BLEND_WEIGHT_TODAY: 0.7,
        }
    )
    assert cfg.price_history_days == 14
    assert cfg.price_blend_weight_today == 0.7
    # CONF string identity == field name (the from_dict binding contract).
    assert const.CONF_PRICE_HISTORY_DAYS == "price_history_days"
    assert const.CONF_PRICE_BLEND_WEIGHT_TODAY == "price_blend_weight_today"


def test_price_prior_tunables_in_options_schema():
    schema = config_flow._options_schema({}, services={})
    keys = {str(m.schema) for sec in schema.schema.values() for m in sec.schema.schema}
    assert const.CONF_PRICE_HISTORY_DAYS in keys
    assert const.CONF_PRICE_BLEND_WEIGHT_TODAY in keys
