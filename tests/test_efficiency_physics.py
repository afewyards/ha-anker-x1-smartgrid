"""Tests for the shared eta helpers and optional eta_curve param on the shared
battery physics functions in regret.py (_apply_solar_load, _max_grid_dc).

eta_curve=None (default) MUST be byte-identical to today's behaviour — this is
the parity invariant. When a curve is supplied, load-driven discharge bears
eta_d (the headline physics change).
"""
from custom_components.anker_x1_smartgrid.models import Config
from custom_components.anker_x1_smartgrid.efficiency import EfficiencyCurve
from custom_components.anker_x1_smartgrid import regret


def test_apply_solar_load_none_is_byte_identical_discharge():
    cfg = Config(eta_charge=0.92, round_trip_eff=0.85, capacity_kwh=10.0, soc_target=97.0)
    assert regret._apply_solar_load(5.0, -1.0, cfg) == 5.0 - 1.0


def test_apply_solar_load_curve_makes_discharge_bear_eta_d():
    cfg = Config(eta_charge=0.92, round_trip_eff=0.85, capacity_kwh=10.0, soc_target=97.0)
    curve = EfficiencyCurve.static(cfg)
    out = regret._apply_solar_load(5.0, -1.0, cfg, eta_curve=curve)
    assert out < 5.0 - 1.0
    assert abs(out - (5.0 - 1.0 / (0.85 / 0.92))) < 1e-9


def test_helpers_none_equal_scalar():
    cfg = Config(eta_charge=0.92, round_trip_eff=0.85)
    assert regret._eta_charge_at(6000.0, cfg, None) == 0.92
    assert abs(regret._eta_discharge_at(300.0, cfg, None) - min(0.85 / 0.92, 1.0)) < 1e-12
