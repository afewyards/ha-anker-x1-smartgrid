"""T2 — Config + const fields: SoC drift-hedge tunables.

Tests CONF_ keys, DEFAULT_ values, Config dataclass fields, and from_dict roundtrip.
Hysteresis release band is DERIVED (0.5 × deadband) — not a config field.
"""

from custom_components.anker_x1_smartgrid import const
from custom_components.anker_x1_smartgrid.models import Config


def test_defaults_off():
    c = Config()
    assert c.soc_hedge_fraction == 0.0
    assert c.soc_drift_deadband_kwh == 0.3
    assert c.soc_drift_decay_halflife_h == 0.0


def test_const_defaults():
    assert const.DEFAULT_SOC_HEDGE_FRACTION == 0.0
    assert const.DEFAULT_SOC_DRIFT_DEADBAND_KWH == 0.3
    assert const.DEFAULT_SOC_DRIFT_DECAY_HALFLIFE_H == 0.0
    assert const.CONF_SOC_HEDGE_FRACTION == "soc_hedge_fraction"
    assert const.CONF_SOC_DRIFT_DEADBAND_KWH == "soc_drift_deadband_kwh"
    assert const.CONF_SOC_DRIFT_DECAY_HALFLIFE_H == "soc_drift_decay_halflife_h"


def test_from_dict_roundtrip():
    c = Config.from_dict({"soc_hedge_fraction": 0.5, "soc_drift_deadband_kwh": 0.4, "soc_drift_decay_halflife_h": 6.0})
    assert (c.soc_hedge_fraction, c.soc_drift_deadband_kwh, c.soc_drift_decay_halflife_h) == (0.5, 0.4, 6.0)
