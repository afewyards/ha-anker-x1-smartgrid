"""Tests: charge_price_ceiling() in scheduler."""

from custom_components.anker_x1_smartgrid.models import Config
from custom_components.anker_x1_smartgrid import scheduler


def test_charge_ceiling_blocks_peakish_price():
    cfg = Config(round_trip_eff=0.85)
    ceiling = scheduler.charge_price_ceiling(0.356, cfg)
    assert abs(ceiling - 0.3026) < 1e-6
    # tonight's 0.343 is above the ceiling -> not worth charging
    assert ceiling < 0.343


def test_charge_ceiling_allows_cheap_night():
    cfg = Config(round_trip_eff=0.85)
    ceiling = scheduler.charge_price_ceiling(0.40, cfg)  # 0.34
    assert ceiling >= 0.28  # a 0.28 night slot still passes


def test_charge_ceiling_none_when_no_peak():
    assert scheduler.charge_price_ceiling(None, Config()) is None


def test_charge_price_ceiling_none_curve_is_identical():
    """eta_curve=None (default) must be byte-identical to the pre-curve path."""
    cfg = Config(round_trip_eff=0.85)
    assert scheduler.charge_price_ceiling(0.40, cfg) == scheduler.charge_price_ceiling(0.40, cfg, eta_curve=None)
    assert scheduler.charge_price_ceiling(None, cfg, eta_curve=None) is None


def test_charge_price_ceiling_curve_uses_effective_round_trip():
    """With a curve supplied, the round-trip is looked up (charge @ max_charge_w,
    discharge @ the fallback load power) instead of the static config scalar."""
    from custom_components.anker_x1_smartgrid.efficiency import EfficiencyCurve

    cfg = Config(round_trip_eff=0.85, eta_charge=0.92)
    curve = EfficiencyCurve.static(cfg)
    ceiling = scheduler.charge_price_ceiling(0.40, cfg, eta_curve=curve)
    assert abs(ceiling - 0.40 * 0.85) < 1e-9
