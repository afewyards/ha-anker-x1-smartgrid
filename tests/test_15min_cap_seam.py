"""dt_h=1.0 must be byte-identical to the default (no-op seam, T1)."""

from custom_components.anker_x1_smartgrid.models import Config
from custom_components.anker_x1_smartgrid.optimize import optimize_grid
from custom_components.anker_x1_smartgrid.regret import DayData, hindsight_optimal_grid


def _cfg():
    return Config(
        capacity_kwh=10.0,
        soc_floor=20.0,
        soc_target=80.0,
        max_charge_w=3000.0,
        eta_charge=1.0,
        max_export_w=3000.0,
        grid_export_limit_w=6000.0,
        enable_export=True,
    )


def _inputs():
    pv = [0.0] * 9 + [0.5] * 4 + [0.0] * 11
    load = [1.0] * 24
    price = [
        0.30,
        0.28,
        0.25,
        0.22,
        0.20,
        0.18,
        0.22,
        0.28,
        0.32,
        0.35,
        0.33,
        0.31,
        0.29,
        0.27,
        0.25,
        0.28,
        0.32,
        0.35,
        0.38,
        0.40,
        0.36,
        0.32,
        0.28,
        0.25,
    ]
    return pv, load, price


def test_optimize_grid_explicit_dt_h_one_matches_default():
    pv, load, price = _inputs()
    cfg = _cfg()
    base = optimize_grid(pv, load, price, soc_start=60.0, cfg=cfg, window_start_h=0, window_len=24, export_price=price)
    seam = optimize_grid(
        pv, load, price, soc_start=60.0, cfg=cfg, window_start_h=0, window_len=24, export_price=price, dt_h=1.0
    )
    assert seam == base


def test_hindsight_explicit_dt_h_one_matches_default():
    pv, load, price = _inputs()
    cfg = _cfg()
    day = DayData(pv_kwh=tuple(pv), load_kwh=tuple(load), price=tuple(price), soc_start=60.0)
    base = hindsight_optimal_grid(day, cfg, export_price=tuple(price))
    seam = hindsight_optimal_grid(day, cfg, export_price=tuple(price), dt_h=1.0)
    assert seam == base
