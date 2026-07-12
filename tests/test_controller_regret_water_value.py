from custom_components.anker_x1_smartgrid.models import Config
from custom_components.anker_x1_smartgrid.optimize import compute_water_value, optimize_grid
from custom_components.anker_x1_smartgrid.regret import (
    DayData,
    hindsight_optimal_grid,
    realized_grid_cost,
    score_regret,
)


def test_dp_regret_near_zero_under_water_value_terminal():
    # With perfect foresight, the DP schedule scored against the water-value
    # oracle on the same realized day must have ~zero regret (parity sanity).
    cfg = Config(capacity_kwh=10.0, soc_floor=10.0, soc_target=90.0, eta_charge=0.92, max_charge_w=6000.0)
    pv = [0.0] * 24
    load = [0.4] * 24
    price = [0.20] * 12 + [0.08] + [0.20] * 11  # trough at hour 12
    v = compute_water_value(min(price), cfg)
    day = DayData(pv_kwh=tuple(pv), load_kwh=tuple(load), price=tuple(price), soc_start=50.0)
    opt = hindsight_optimal_grid(day, cfg, terminal_mode="water_value", water_value=v)
    dp = optimize_grid(
        pv,
        load,
        price,
        soc_start=50.0,
        cfg=cfg,
        window_start_h=0,
        window_len=24,
        terminal_mode="water_value",
        water_value=v,
    )
    realized = realized_grid_cost(day, dp["schedule"], cfg)
    score = score_regret(realized, opt)
    assert abs(score["regret_eur"]) < 1e-6
