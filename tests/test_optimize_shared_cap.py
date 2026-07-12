"""Shared rate-cap: grid charge is bounded by the rate left after solar."""

from custom_components.anker_x1_smartgrid.models import Config
from custom_components.anker_x1_smartgrid import optimize as opt
from custom_components.anker_x1_smartgrid.regret import _max_grid_dc


def _cfg(**kw):
    base = dict(capacity_kwh=10.0, soc_floor=10.0, soc_target=100.0, max_charge_w=6000.0, eta_charge=1.0)
    base.update(kw)
    return Config(**base)


def test_helper_full_rate_when_no_solar():
    # soc_after == soc (no solar absorbed) -> grid ceiling is full rate (eta=1).
    cfg = _cfg()
    # no solar, ample headroom (8.0) > rate (6.0) -> rate binds -> 6.0
    assert _max_grid_dc(2.0, 2.0, cfg) == 6.0
    # large headroom: ceiling is the rate
    assert _max_grid_dc(0.0, 0.0, cfg) == 6.0


def test_helper_subtracts_solar_rate():
    # solar absorbed 2 kWh DC this hour (eta=1 -> 2 kWh AC of the 6 kWh budget).
    # remaining AC = 4 -> DC ceiling 4.0; headroom 100% large -> 4.0
    cfg = _cfg()
    assert _max_grid_dc(0.0, 2.0, cfg) == 4.0


def test_helper_solar_saturates_rate():
    # solar absorbed the whole 6 kWh rate -> remaining 0 -> grid ceiling 0.
    cfg = _cfg()
    assert _max_grid_dc(0.0, 6.0, cfg) == 0.0


def test_optimize_grid_caps_solar_plus_grid_at_rate():
    # One hour, strong solar surplus + cheapest price: total charge must not
    # exceed max_charge_w AC. With eta=1, solar surplus 4 kWh and a 6 kWh rate,
    # grid may add at most 2 kWh -> SoC rises by at most 6 kWh.
    cfg = _cfg(soc_target=100.0)
    pv = [4.0, 0.0]
    load = [0.0, 0.0]
    price = [0.10, 0.50]
    out = opt.optimize_grid(pv, load, price, soc_start=0.0, cfg=cfg, window_start_h=0, window_len=2)
    # grid in hour 0 (AC) <= rate - solar_ac = 6 - 4 = 2
    assert out["schedule"][0] <= 2.0 + 1e-6
