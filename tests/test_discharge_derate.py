"""Discharging the 5-10% band delivers only const.DISCHARGE_DERATE_5_10 of the gauge drop.

SoC transitions stay linear; both DPs, the oracle backtrack and the realized sim price
the shortfall (import where it served the house, lost revenue where it was exported).
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.anker_x1_smartgrid import const, plan
from custom_components.anker_x1_smartgrid.dp_common import select_end_state, soc_bins
from custom_components.anker_x1_smartgrid.efficiency import EfficiencyCurve
from custom_components.anker_x1_smartgrid.models import Config, ForecastInterval, PriceSlot
from custom_components.anker_x1_smartgrid.optimize import optimize_grid
from custom_components.anker_x1_smartgrid.regret import (
    DayData,
    band_shortfall_dc,
    band_top_kwh,
    grid_floor_kwh,
    hindsight_optimal_grid,
    realized_grid_cost,
)

LOSS = 1.0 - const.DISCHARGE_DERATE_5_10


def _cfg(**kw) -> Config:
    d = dict(
        capacity_kwh=10.0, soc_floor=5.0, soc_target=100.0, max_charge_w=6000.0, eta_charge=1.0, round_trip_eff=1.0
    )
    d.update(kw)
    return Config(**d)


@pytest.mark.parametrize(
    ("before", "after", "band_kwh"),
    [
        (5.0, 4.0, 0.0),  # above the band
        (1.2, 0.8, 0.2),  # crosses the band top
        (0.9, 0.6, 0.3),  # inside
        (0.7, 0.3, 0.2),  # crosses the floor: below it is the floor import's
        (0.5, 0.3, 0.0),  # at the floor
        (0.6, 0.9, 0.0),  # charging
    ],
)
def test_shortfall_per_band_portion(before, after, band_kwh):
    cfg = _cfg()  # band 0.5-1.0 kWh
    got = band_shortfall_dc(before, after, cfg.firmware_floor_kwh, band_top_kwh(cfg))
    assert got == pytest.approx(LOSS * band_kwh, abs=1e-12)


def test_band_edges_snap_onto_the_grid():
    cfg = _cfg(capacity_kwh=20.48)  # 5% = 1.024, 10% = 2.048
    assert grid_floor_kwh(cfg.firmware_floor_kwh) == pytest.approx(1.05)
    assert band_top_kwh(cfg) == pytest.approx(2.05)


def _both(cfg, pv, load, price, soc_start, dt_h, *, eta_curve=None, export_price=None, water_value=0.20):
    n = len(load)
    kwargs = dict(
        terminal_mode="water_value", water_value=water_value, dt_h=dt_h, eta_curve=eta_curve, export_price=export_price
    )
    hind = hindsight_optimal_grid(DayData(tuple(pv), tuple(load), tuple(price), soc_start), cfg, **kwargs)
    opt = optimize_grid(
        list(pv),
        list(load),
        list(price),
        soc_start=soc_start,
        cfg=cfg,
        window_start_h=0,
        window_len=n,
        slots_per_day=round(24 / dt_h),
        **kwargs,
    )
    return opt, hind


@pytest.mark.parametrize("dt_h", [0.25, 1.0])
@pytest.mark.parametrize("curve", [False, True])
def test_dp_oracle_parity_through_the_band(dt_h, curve, monkeypatch):
    cfg = _cfg(
        capacity_kwh=20.48,
        soc_target=90.0,
        eta_charge=0.92,
        round_trip_eff=0.85,
        enable_export=True,
        max_export_w=6000.0,
        grid_export_limit_w=6000.0,
        export_peak_band_frac=0.5,
        cycle_cost_eur_per_kwh=0.03,
    )
    eta_curve = EfficiencyCurve.static(cfg) if curve else None
    n = round(12 / dt_h)
    load = [0.6 * dt_h] * n
    price = [0.30] * (n // 2) + [0.10] * (n // 4) + [0.35] * (n - n // 2 - n // 4)
    args = (cfg, [0.0] * n, load, price, 18.0, dt_h)
    kw = dict(eta_curve=eta_curve, export_price=[p - 0.02 for p in price])

    opt, hind = _both(*args, **kw)
    for key in ("kwh", "eur", "export_revenue_eur"):
        assert opt[key] == pytest.approx(hind[key], abs=1e-9), key
    assert opt["schedule"] == pytest.approx(hind["schedule"], abs=1e-9)
    assert opt["export_schedule"] == pytest.approx(hind["export_schedule"], abs=1e-9)

    monkeypatch.setattr(const, "DISCHARGE_DERATE_5_10", 1.0)
    uniform, _ = _both(*args, **kw)
    assert uniform["eur"] < opt["eur"] - 0.01  # the trajectory really lives in the band


def test_realized_replay_of_the_oracle_books_its_cost():
    """A band export and a band drain, replayed through realized_grid_cost: regret 0."""
    cfg = _cfg(enable_export=True, max_export_w=6000.0, grid_export_limit_w=6000.0, cycle_cost_eur_per_kwh=0.0)
    price = (0.50, 0.05, 0.05, 0.05, 0.05)
    export = [0.50, 0.0, 0.0, 0.0, 0.0]
    day = DayData((0.0,) * 5, (0.0, 0.1, 0.1, 0.1, 0.1), price, 10.0)
    hind = hindsight_optimal_grid(day, cfg, terminal_mode="water_value", water_value=0.0, export_price=export)
    assert hind["export_schedule"][0] == pytest.approx(0.5)  # the whole band, at f x 0.50
    real = realized_grid_cost(
        day, hind["schedule"], cfg, realized_export_by_hour=hind["export_schedule"], export_price=export
    )
    assert real["eur"] == pytest.approx(hind["eur"], abs=1e-9)
    assert real["kwh"] == pytest.approx(hind["kwh"], abs=1e-9)
    assert hind["eur"] == pytest.approx(0.4 * 0.05 - 0.5 * (1.0 - LOSS) * 0.50, abs=1e-9)


def test_band_is_not_sold_to_buy_the_load_back():
    """Selling the band at 0.25 and rebuying the load at 0.30 never pays, derate or not."""
    cfg = _cfg(
        soc_target=95.0,
        eta_charge=0.95,
        round_trip_eff=0.90,
        max_export_w=6000.0,
        grid_export_limit_w=6000.0,
        enable_export=True,
        export_peak_band_frac=0.9,
        cycle_cost_eur_per_kwh=0.03,
    )
    load = [0.0] + [0.1] * 5
    opt, hind = _both(cfg, [0.0] * 6, load, [0.30] * 6, 10.0, 1.0, export_price=[0.25] + [0.05] * 5, water_value=0.0)
    assert sum(opt["export_schedule"]) == pytest.approx(0.0, abs=1e-9)
    assert sum(hind["export_schedule"]) == pytest.approx(0.0, abs=1e-9)
    assert opt["eur"] == pytest.approx(LOSS * 0.5 * 0.30, abs=1e-9)  # the band served the load at f


@pytest.mark.parametrize("segments", [[(0.5, 0.40)], None])
def test_terminal_credit_values_the_band_at_the_derate(segments, monkeypatch):
    """Filling the band costs 0.30/kWh: worth it at a 0.40 credit, not at 0.61 x 0.40."""
    n_states, to_bin, from_bin = soc_bins(10.0)
    dp = [0.3 * max(0.0, from_bin(b) - 0.5) + 1.0 * max(0.0, from_bin(b) - 1.0) for b in range(n_states)]
    kw = dict(
        terminal_mode="water_value",
        water_value=0.40,
        firmware_floor_kwh=0.5,
        floor_kwh=0.5,
        target_kwh=9.0,
        to_bin=to_bin,
        from_bin=from_bin,
        n_states=n_states,
        terminal_segments=segments,
        band_top_kwh=1.0,
    )
    assert from_bin(select_end_state(dp, **kw)[0]) == pytest.approx(0.5)
    monkeypatch.setattr(const, "DISCHARGE_DERATE_5_10", 1.0)
    assert from_bin(select_end_state(dp, **kw)[0]) == pytest.approx(1.0)


def test_terminal_segments_band_loss_shrinks_delivered_energy():
    """The band leaves the pack last: its loss shortens the walk, it does not discount the richest segment."""
    n_states, to_bin, from_bin = soc_bins(10.0)
    segs = [(0.2, 0.11), (0.3, 0.35)]  # (kWh, EUR/kWh), unsorted on purpose: richest first is 0.3 kWh at 0.35
    # End at the band top (1.0): 0.5 kWh above the floor, all of it inside the band.
    # Old: 0.61 x walk(0.5) = 0.61 x (0.3 x 0.35 + 0.2 x 0.11) = 0.07747
    # New: walk(0.5 - 0.39 x 0.5 = 0.305) = 0.3 x 0.35 + 0.005 x 0.11 = 0.10605
    dp = [float("inf")] * n_states
    dp[to_bin(0.5)] = 0.0
    dp[to_bin(1.0)] = 0.09
    kw = dict(
        terminal_mode="water_value",
        water_value=0.0,
        firmware_floor_kwh=0.5,
        floor_kwh=0.5,
        target_kwh=9.0,
        to_bin=to_bin,
        from_bin=from_bin,
        n_states=n_states,
        terminal_segments=segs,
        band_top_kwh=1.0,
    )
    assert pytest.approx(0.39) == LOSS
    best_b, _, _ = select_end_state(dp, **kw)
    assert from_bin(best_b) == pytest.approx(1.0)  # 0.09 < 0.10605 (the old formula's 0.0775 would skip it)
    dp[to_bin(1.0)] = 0.11
    assert from_bin(select_end_state(dp, **kw)[0]) == pytest.approx(0.5)  # 0.11 > 0.10605


@pytest.mark.parametrize("order", [1, -1])
def test_equal_score_end_states_resolve_to_the_highest(order):
    n_states, to_bin, from_bin = soc_bins(10.0)
    lo, mid, hi = (to_bin(x) for x in (2.0, 3.0, 4.0))
    dp = [float("inf")] * n_states
    kw = dict(
        terminal_mode="water_value",
        water_value=0.20,
        firmware_floor_kwh=0.5,
        floor_kwh=0.5,
        target_kwh=9.0,
        to_bin=to_bin,
        from_bin=from_bin,
        n_states=n_states,
    )
    # Each kWh costs exactly its 0.20 credit: all three are tied up to rounding noise.
    for b, noise in zip((lo, mid, hi)[::order], (0.0, 3e-13, -2e-13)):
        dp[b] = 0.20 * (from_bin(b) - 0.5) + noise
    assert select_end_state(dp, **kw)[0] == hi
    dp[lo] -= 0.01  # strictly better wins
    assert select_end_state(dp, **kw)[0] == lo


def test_loss_adds_no_material_current_slot_buy():
    """The reviewer's H1 probe: an evening drain into the band before a cheap trough.
    A live-slot buy (decision.py acts on > 0.01 kWh) that only the loss brings must be
    bin noise, not money the loss conjures out of rounding."""
    cfg = _cfg(
        capacity_kwh=20.48,
        soc_target=97.0,
        eta_charge=0.92,
        round_trip_eff=0.85,
        idle_drain_w=100.0,
        charge_margin_eur_per_kwh=0.0,
        cycle_cost_eur_per_kwh=0.03,
    )
    curve = EfficiencyCurve.static(cfg)
    rng = random.Random(5)
    n = 64

    def solve(load, price, soc0, f, chargeable=None):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(const, "DISCHARGE_DERATE_5_10", f)
            return optimize_grid(
                [0.0] * n,
                load,
                price,
                soc_start=soc0,
                cfg=cfg,
                window_start_h=0,
                window_len=n,
                slots_per_day=96,
                dt_h=0.25,
                terminal_mode="water_value",
                water_value=0.10,
                eta_curve=curve,
                chargeable=chargeable,
            )

    gains = []
    for _ in range(12):
        price = [0.30 + rng.uniform(-0.03, 0.03)] * 16 + [0.25] * 16 + [0.12] * 16 + [0.22] * 16
        load = [rng.uniform(0.06, 0.15) for _ in range(n)]
        soc0 = rng.uniform(9.0, 16.0)
        on = solve(load, price, soc0, const.DISCHARGE_DERATE_5_10)
        if on["schedule"][0] > 0.01 >= solve(load, price, soc0, 1.0)["schedule"][0]:
            blocked = solve(load, price, soc0, const.DISCHARGE_DERATE_5_10, [i > 0 for i in range(n)])
            gains.append(blocked["eur"] - on["eur"])
    assert len(gains) <= 1
    assert all(g < 0.01 for g in gains), gains


def test_plan_display_drains_faster_in_the_band():
    base = datetime(2026, 11, 14, 20, 0, tzinfo=UTC)
    cfg = _cfg()
    slots = [PriceSlot(base + timedelta(hours=i), 0.30) for i in range(2)]
    ivs = [ForecastInterval(base + timedelta(hours=i), pv_w=0.0, load_w=100.0, dt_h=1.0) for i in range(2)]
    out = plan.build_plan_horizon(slots, ivs, [], 20.0, base + timedelta(hours=2), cfg)
    assert out[0]["soc"] == pytest.approx(19.0, abs=0.01)  # 1% of gauge per hour above the band
    out = plan.build_plan_horizon(slots, ivs, [], 9.0, base + timedelta(hours=2), cfg)
    assert out[0]["soc"] == pytest.approx(9.0 - 1.0 / const.DISCHARGE_DERATE_5_10, abs=0.05)
