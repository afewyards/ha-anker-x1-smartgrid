import pytest

from custom_components.anker_x1_smartgrid.models import Config
from custom_components.anker_x1_smartgrid.optimize import compute_water_value, optimize_grid


def test_compute_water_value_divides_by_eta_and_scales():
    cfg = Config(eta_charge=0.92, water_value_factor=1.0, clamp_water_value_nonneg=True)
    assert compute_water_value(0.10, cfg) == pytest.approx(0.10 / 0.92)


def test_compute_water_value_clamps_negative_to_zero():
    cfg = Config(clamp_water_value_nonneg=True)
    assert compute_water_value(-0.05, cfg) == 0.0


def test_compute_water_value_factor_scales_down():
    cfg = Config(eta_charge=1.0, water_value_factor=0.5, clamp_water_value_nonneg=True)
    assert compute_water_value(0.20, cfg) == pytest.approx(0.10)


def test_water_value_zero_ends_at_floor():
    # No load/PV, soc at floor: zero water value -> no reason to charge above floor.
    cfg = Config(capacity_kwh=10.0, soc_floor=10.0, soc_target=90.0, eta_charge=1.0, max_charge_w=6000.0)
    res = optimize_grid(
        [0.0, 0.0, 0.0],
        [0.0, 0.0, 0.0],
        [0.10, 0.10, 0.10],
        soc_start=10.0,
        cfg=cfg,
        window_start_h=0,
        window_len=3,
        terminal_mode="water_value",
        water_value=0.0,
    )
    assert sum(res["schedule"]) < 1e-6
    assert not res.get("infeasible", False)


def test_water_value_high_fills_to_target():
    cfg = Config(capacity_kwh=10.0, soc_floor=10.0, soc_target=90.0, eta_charge=1.0, max_charge_w=6000.0)
    res = optimize_grid(
        [0.0, 0.0, 0.0],
        [0.0, 0.0, 0.0],
        [0.10, 0.10, 0.10],
        soc_start=10.0,
        cfg=cfg,
        window_start_h=0,
        window_len=3,
        terminal_mode="water_value",
        water_value=1.0,  # credit >> price -> fill
    )
    # 10% -> 90% of a 10 kWh battery = 8.0 kWh stored; eta=1 so 8.0 AC kWh.
    assert sum(res["schedule"]) == pytest.approx(8.0, abs=0.1)


def test_water_value_soc_above_target_not_infeasible():
    # soc_start above target, no load -> no charging needed; must NOT flag infeasible.
    cfg = Config(capacity_kwh=10.0, soc_floor=10.0, soc_target=90.0, eta_charge=1.0, max_charge_w=6000.0)
    res = optimize_grid(
        [0.0, 0.0, 0.0],
        [0.0, 0.0, 0.0],
        [0.10, 0.10, 0.10],
        soc_start=95.0,
        cfg=cfg,
        window_start_h=0,
        window_len=3,
        terminal_mode="water_value",
        water_value=0.5,
    )
    assert not res.get("infeasible", False)
    assert sum(res["schedule"]) < 1e-6


def test_reserve_mode_is_default_and_unchanged():
    cfg = Config(capacity_kwh=10.0, soc_floor=10.0, soc_target=90.0, eta_charge=1.0, max_charge_w=6000.0)
    res = optimize_grid(
        [0.0, 0.0, 0.0],
        [0.0, 0.0, 0.0],
        [0.10, 0.10, 0.10],
        soc_start=10.0,
        cfg=cfg,
        window_start_h=0,
        window_len=3,
    )
    # Reserve mode forces SoC >= target: must fill the full 8.0 kWh.
    assert sum(res["schedule"]) == pytest.approx(8.0, abs=0.1)
