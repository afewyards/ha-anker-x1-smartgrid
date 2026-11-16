from types import SimpleNamespace

import pytest

from custom_components.anker_x1_smartgrid import optimize
from custom_components.anker_x1_smartgrid.dp_common import KEEP_TIE_EUR_PER_KWH
from custom_components.anker_x1_smartgrid.models import Config
from custom_components.anker_x1_smartgrid.optimize import compute_water_value, eta_discharge, optimize_grid


def test_compute_water_value_divides_by_eta_and_scales():
    cfg = Config(eta_charge=0.92, water_value_factor=1.0, clamp_water_value_nonneg=True)
    assert compute_water_value(0.10, cfg) == pytest.approx(0.10 / 0.92 - KEEP_TIE_EUR_PER_KWH)


def test_compute_water_value_uses_the_best_measured_charge_eta_up_to_full_rate():
    cfg = Config(eta_charge=0.95, max_charge_w=6000.0, water_value_factor=1.0)
    # 0.85 in the 2.5-4 kW bin beats 0.80 at full rate; above full rate the DP never charges.
    curve = SimpleNamespace(eta_charge=lambda w: 0.99 if w > 6000.0 else 0.85 if 2500.0 <= w < 4000.0 else 0.80)
    assert compute_water_value(0.10, cfg, curve) == pytest.approx(0.10 / 0.85 - KEEP_TIE_EUR_PER_KWH)


def test_compute_water_value_clamps_negative_to_zero():
    cfg = Config(clamp_water_value_nonneg=True)
    assert compute_water_value(-0.05, cfg) == 0.0


def test_compute_water_value_factor_scales_down():
    cfg = Config(eta_charge=1.0, water_value_factor=0.5, clamp_water_value_nonneg=True)
    assert compute_water_value(0.20, cfg) == pytest.approx(0.10 - KEEP_TIE_EUR_PER_KWH)


def test_compute_water_value_adds_the_charge_margin():
    cfg = Config(eta_charge=0.92, charge_margin_eur_per_kwh=0.03)
    assert compute_water_value(0.10, cfg) == pytest.approx(0.10 / 0.92 + 0.03 - KEEP_TIE_EUR_PER_KWH)


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


def test_export_slot_feeding_the_house_pays_wear_once_per_dc_kwh(monkeypatch):
    """The house leg and the export leg each pay wear on their own DC kWh, never on each other's."""
    best = {}
    real = optimize.select_end_state

    def _spy(*a, **k):
        out = real(*a, **k)
        best["cost"] = out[1]
        return out

    monkeypatch.setattr(optimize, "select_end_state", _spy)

    def run(cc):
        cfg = Config(capacity_kwh=10.0, soc_floor=10.0, soc_target=90.0, cycle_cost_eur_per_kwh=cc)
        res = optimize_grid(
            [0.0],
            [0.5],
            [0.30],
            soc_start=80.0,
            cfg=cfg,
            window_start_h=0,
            window_len=1,
            chargeable=[False],
            export_price=[2.0],
            terminal_mode="water_value",
            water_value=0.0,
        )
        return res, best["cost"], eta_discharge(cfg)

    free, cost_free, eta_d = run(0.0)
    worn, cost_worn, _ = run(0.05)
    assert worn["export_kwh"] > 0.0
    assert worn["export_kwh"] == pytest.approx(free["export_kwh"])
    export_dc = worn["export_kwh"] / eta_d
    assert cost_worn - cost_free == pytest.approx(0.05 * (0.5 + export_dc))  # house DC is 1:1 without a curve
