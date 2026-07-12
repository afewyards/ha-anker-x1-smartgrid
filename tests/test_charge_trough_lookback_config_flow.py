"""TDD: charge_trough_lookback_h is exposed in the options schema."""

from __future__ import annotations

from custom_components.anker_x1_smartgrid import const
from custom_components.anker_x1_smartgrid.config_flow import _options_schema


def test_options_schema_exposes_charge_trough_lookback():
    schema = _options_schema({})
    keys = {str(m.schema) for sec in schema.schema.values() for m in sec.schema.schema}
    assert const.CONF_CHARGE_TROUGH_LOOKBACK_H in keys
