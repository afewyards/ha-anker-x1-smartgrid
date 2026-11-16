"""Kept value: a stored kWh is worth its refill before the next need, plus the charge margin.

Using it today must clear that refill plus wear; a tie must not buy it.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.util import dt as dt_util

from custom_components.anker_x1_smartgrid import optimize
from custom_components.anker_x1_smartgrid.decision import compute_decision
from custom_components.anker_x1_smartgrid.efficiency import EfficiencyCurve
from custom_components.anker_x1_smartgrid.models import (
    Config,
    ControllerState,
    PlantInputs,
    PlanState,
    PriceSlot,
)

NOW = datetime(2026, 11, 16, 14, 0, tzinfo=UTC)
# 14..22 UTC: shoulders, a 0.46 peak 16-19, the real minimum 0.355 in the last slot.
TODAY = [0.40, 0.40, 0.46, 0.46, 0.46, 0.46, 0.40, 0.38, 0.355]
PEAK = [NOW + timedelta(hours=h) for h in (2, 3, 4, 5)]
LAST = NOW + timedelta(hours=len(TODAY) - 1)


class _Flat:
    def predict(self, start, temp, fallback, quantile=0.5):
        return 1000.0


def _cfg(**over) -> Config:
    return Config.from_dict(
        {
            "capacity_kwh": 10.0,
            "eta_charge": 0.8878,
            "round_trip_eff": 0.8878 * 0.9256,
            "max_charge_w": 6000.0,
            "cycle_cost_eur_per_kwh": 0.03,
            "idle_drain_w": 100.0,
            "low_power_hold": True,
            "soc_floor": 5.0,
            "min_dwell_min": 0,
            "export_min_block_kwh": 0.0,
            "export_fee_eur_per_kwh": 0.0,
            **over,
        }
    )


def _run(
    cfg: Config,
    prices: list[float],
    *,
    soc: float = 60.0,
    est: float | None = 0.32,
    export: bool = False,
    curve: EfficiencyCurve | None = None,
):
    out: dict = {}
    compute_decision(
        PlanState(ControllerState.PASSIVE, NOW - timedelta(hours=2), ()),
        PlantInputs(soc=soc, meter_w=0.0, now=NOW),
        [PriceSlot(NOW + timedelta(hours=i), p) for i, p in enumerate(prices)],
        0.0,
        NOW + timedelta(hours=len(prices)),
        _Flat(),
        None,
        cfg,
        export_price=0.0 if export else None,
        export_price_matches_import=export,
        estimated_tomorrow=None if est is None else [est] * 24,
        _out=out,
        eta_curve=curve or EfficiencyCurve.static(cfg),
    )
    return out


def _held(out: dict) -> list[datetime]:
    return [t for t in PEAK if t in (out.get("held_slots") or ())]


def test_peak_feeds_the_house_when_the_estimated_night_refills_cheaper():
    out = _run(_cfg(charge_margin_eur_per_kwh=0.0), TODAY)
    assert out["water_value"] == pytest.approx(0.32 / 0.8878, abs=1e-5)
    assert _held(out) == []


def test_peak_is_held_when_the_gain_does_not_clear_wear_and_margin():
    out = _run(_cfg(charge_margin_eur_per_kwh=0.05), TODAY)
    assert out["water_value"] == pytest.approx(0.32 / 0.8878 + 0.05, abs=1e-5)
    assert _held(out) == PEAK


@pytest.mark.parametrize("margin", [0.0, 0.03])
def test_no_charge_at_the_horizon_min_slot_on_a_tie(margin):
    out = _run(_cfg(charge_margin_eur_per_kwh=margin), TODAY, est=None)
    assert out["water_value"] == pytest.approx(0.355 / 0.8878 + margin, abs=1e-5)
    assert LAST not in (out.get("grid_request") or {})
    assert out["dp_selected"] == []


@pytest.mark.parametrize("margin", [0.0, 0.03])
def test_no_charge_on_a_tie_when_the_curve_charges_best_below_full_rate(margin):
    # Measured: 0.89 below the top bin, 0.8878 at full rate. Refilling at the full-rate eta would leave a
    # paced charge at 0.89 just under the tie, and the DP bought it at the last real slot.
    cfg = _cfg(charge_margin_eur_per_kwh=margin)
    base = EfficiencyCurve.static(cfg)
    charge = [replace(b, eta=0.89 if b.hi_w < float("inf") else 0.8878) for b in base._charge]
    out = _run(cfg, TODAY, est=None, curve=EfficiencyCurve(charge, list(base._discharge), base._fc, base._fd))
    assert LAST not in (out.get("grid_request") or {})
    assert out["dp_selected"] == []


def test_energy_is_kept_for_a_published_tomorrow_peak():
    # Tonight never drops below 0.42, so a kWh used at 0.46 today cannot be bought back for 0.80.
    tomorrow = [0.42] * 10 + [0.80] * 3 + [0.42] * 2
    # 40 %: less than tomorrow's peak alone would use, and an estimated night far below every real price.
    out = _run(_cfg(charge_margin_eur_per_kwh=0.0), TODAY[:6] + tomorrow, soc=40.0, est=0.20)
    assert _held(out) == PEAK


@pytest.mark.parametrize(("margin", "exports"), [(0.0, True), (0.06, False)])
def test_sell_today_and_refill_tonight_only_when_it_clears_wear_and_margin(margin, exports):
    # 25 kWh: enough left after the house's day to have something to sell.
    cfg = _cfg(charge_margin_eur_per_kwh=margin, enable_export=True, low_power_hold=False, capacity_kwh=25.0)
    out = _run(cfg, TODAY, soc=90.0, export=True)
    assert any(t in (out.get("export_request") or {}) for t in PEAK) is exports


def test_a_slot_keeps_for_the_richest_later_use_unless_bought_back_cheaper_first():
    cfg = Config(eta_charge=1.0, round_trip_eff=1.0, cycle_cost_eur_per_kwh=0.0)
    kept = optimize.kept_values([0.40, 0.80, 0.20, 0.80], [500.0] * 4, 0.10, cfg)
    assert kept == pytest.approx([0.80, 0.20, 0.80, 0.10], abs=1e-5)


def test_without_an_estimated_gap_the_real_minimum_anchors_the_kept_value():
    out = _run(_cfg(charge_margin_eur_per_kwh=0.02), TODAY, est=None)
    assert out["water_value"] == pytest.approx(0.355 / 0.8878 + 0.02, abs=1e-5)


def test_a_refill_before_tomorrows_peak_cheaper_than_using_now_does_not_hold_today():
    tomorrow = [0.42] * 4 + [0.20] * 6 + [0.80] * 3 + [0.42] * 2
    out = _run(_cfg(charge_margin_eur_per_kwh=0.0), TODAY[:6] + tomorrow, soc=40.0, est=0.20)
    assert _held(out) == []


def test_a_later_sale_counts_as_a_use_worth_keeping_for():
    cfg = Config(eta_charge=1.0, round_trip_eff=1.0, cycle_cost_eur_per_kwh=0.0)
    price, deficit = [0.40, 0.90, 0.20], [500.0, 0.0, 500.0]
    assert optimize.kept_values(price, deficit, 0.10, cfg)[0] == pytest.approx(0.20, abs=1e-5)
    assert optimize.kept_values(price, deficit, 0.10, cfg, sell=[0.0, 0.70, 0.0])[0] == pytest.approx(0.70)


def test_kept_values_follow_single_quarters():
    q = timedelta(minutes=15)
    # 0.50 quarters, one cheap 0.10 quarter at 15:45, one rich 0.90 quarter at 17:45, a 0.05 last quarter.
    prices = [0.50] * 7 + [0.10] + [0.50] * 7 + [0.90] + [0.50] * 7 + [0.05]
    # A wide cheap-charge band, so the DP may charge the 0.10 quarter ahead of the 0.05 one.
    cfg = _cfg(charge_margin_eur_per_kwh=0.0, charge_window_price_band=1.0)
    curve = EfficiencyCurve.static(cfg)
    out: dict = {}
    compute_decision(
        PlanState(ControllerState.PASSIVE, NOW - timedelta(hours=2), ()),
        PlantInputs(soc=60.0, meter_w=0.0, now=NOW),
        [PriceSlot(NOW + i * q, p) for i, p in enumerate(prices)],
        0.0,
        NOW + len(prices) * q,
        _Flat(),
        None,
        cfg,
        _out=out,
        eta_curve=curve,
        slot_minutes=15,
    )
    kept = out["kept_by_slot"]
    # Kept through the quarter before the cheap one: worth only buying it back there.
    assert kept[NOW + 6 * q] == pytest.approx(optimize.compute_water_value(0.10, cfg, curve))
    # Kept through the quarter before the rich one: worth using it there.
    assert kept[NOW + 14 * q] == pytest.approx(optimize.discharge_value(0.90, 1000.0, cfg, curve))


@pytest.mark.parametrize("night", [0.355, 0.3549])
def test_an_estimate_that_copies_the_last_real_price_buys_nothing(night):
    # The estimated 23:00 equals the real 22:00 price, the rest of the night is dear: refilling at
    # 22:00 to carry into the night gains nothing over refilling at 23:00.
    est = [0.60] * 24
    est[dt_util.as_local(LAST + timedelta(hours=1)).hour] = night
    out: dict = {}
    compute_decision(
        PlanState(ControllerState.PASSIVE, NOW - timedelta(hours=2), ()),
        PlantInputs(soc=20.0, meter_w=0.0, now=NOW),
        [PriceSlot(NOW + timedelta(hours=i), p) for i, p in enumerate(TODAY)],
        0.0,
        NOW + timedelta(hours=len(TODAY)),
        _Flat(),
        None,
        _cfg(charge_margin_eur_per_kwh=0.0),
        estimated_tomorrow=est,
        _out=out,
        eta_curve=EfficiencyCurve.static(_cfg()),
    )
    assert LAST not in out["grid_request"]


@pytest.mark.parametrize("est", [0.20, 0.80])
def test_estimated_prices_never_raise_a_kept_value_above_the_real_one(est):
    # A real 0.20 refill tonight; the estimated night either side of it.
    prices = [*TODAY[:-1], 0.20]
    cfg = _cfg(charge_margin_eur_per_kwh=0.0)
    real = _run(cfg, prices, est=None)["kept_by_slot"]
    kept = _run(cfg, prices, est=est)["kept_by_slot"]
    assert all(kept[t] <= real[t] + 1e-9 for t in real)


def test_a_slot_without_a_price_carries_the_kept_value_through():
    cfg = Config(eta_charge=1.0, round_trip_eff=1.0, cycle_cost_eur_per_kwh=0.0)
    # The 0.0 is a pad for a missing price, not a free refill.
    kept = optimize.kept_values([0.40, 0.0, 0.80], [500.0] * 3, 0.10, cfg, valid=[True, False, True])
    assert kept == pytest.approx([0.80, 0.80, 0.10], abs=1e-5)


def test_only_a_slot_the_dp_may_charge_in_caps_the_kept_value():
    cfg = Config(eta_charge=1.0, round_trip_eff=1.0, cycle_cost_eur_per_kwh=0.0)
    kept = optimize.kept_values([0.40, 0.20, 0.80], [500.0] * 3, 0.10, cfg, chargeable=[True, False, True])
    assert kept[0] == pytest.approx(0.80, abs=1e-5)


def test_only_a_later_slot_the_dp_may_charge_in_caps_the_value_kept_past_the_horizon():
    # A wide band, so the 0.355 last slot stays chargeable behind the cheap current one.
    cfg = _cfg(charge_margin_eur_per_kwh=0.0, charge_window_price_band=1.0)
    curve = EfficiencyCurve.static(cfg)
    # The cheap current slot refills nothing kept through it; the 0.355 last slot is the refill.
    out = _run(cfg, [0.10, *TODAY[1:]], est=0.80)
    assert out["kept_by_slot"][LAST] == pytest.approx(optimize.compute_water_value(0.355, cfg, curve))
    # Flat prices above the charge ceiling: no refill at all, so the DP's own terminal value stands.
    flat = _run(_cfg(charge_margin_eur_per_kwh=0.0), [0.40] * len(TODAY), est=0.80)
    stored = optimize.hold_stored_value(flat["water_value"], flat["terminal_segments"])
    assert flat["kept_by_slot"][LAST] == pytest.approx(stored)
