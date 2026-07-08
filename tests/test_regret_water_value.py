import pytest

from custom_components.anker_x1_smartgrid.models import Config
from custom_components.anker_x1_smartgrid.optimize import optimize_grid
from custom_components.anker_x1_smartgrid.regret import DayData, hindsight_optimal_grid


def test_hindsight_accepts_non_24_length():
    cfg = Config(capacity_kwh=10.0, soc_floor=10.0, soc_target=90.0,
                 eta_charge=1.0, max_charge_w=6000.0)
    day = DayData(pv_kwh=(0.0,) * 6, load_kwh=(0.0,) * 6,
                  price=(0.1,) * 6, soc_start=10.0)
    res = hindsight_optimal_grid(day, cfg)  # reserve mode, length 6
    assert sum(res["schedule"]) == pytest.approx(8.0, abs=0.1)


def test_water_value_terminal_parity_with_optimize_grid():
    # The oracle (hindsight) and the online DP must agree under the water-value
    # terminal on the same realized window (re-baselined parity invariant).
    cfg = Config(capacity_kwh=10.0, soc_floor=10.0, soc_target=90.0,
                 eta_charge=0.92, max_charge_w=6000.0)
    pv = [0.0, 0.0, 2.0, 0.0, 0.0, 0.0]
    load = [0.5, 0.5, 0.5, 0.8, 0.8, 0.8]
    price = [0.10, 0.12, 0.30, 0.25, 0.20, 0.08]
    v = 0.08 / 0.92
    day = DayData(pv_kwh=tuple(pv), load_kwh=tuple(load),
                  price=tuple(price), soc_start=50.0)
    oracle = hindsight_optimal_grid(day, cfg, terminal_mode="water_value", water_value=v)
    online = optimize_grid(pv, load, price, soc_start=50.0, cfg=cfg,
                           window_start_h=0, window_len=6,
                           terminal_mode="water_value", water_value=v)
    assert oracle["schedule"] == pytest.approx(online["schedule"], abs=1e-9)
    assert oracle["eur"] == pytest.approx(online["eur"], abs=1e-9)
