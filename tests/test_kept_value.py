"""Kept value: a stored kWh is worth its refill before the next need, plus the charge margin.

Using it today must clear that refill plus wear; a tie must not buy it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

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


def _run(cfg: Config, prices: list[float], *, soc: float = 60.0, est: float | None = 0.32, export: bool = False):
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
        eta_curve=EfficiencyCurve.static(cfg),
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
