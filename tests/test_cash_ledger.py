"""TDD tests for the battery cash ledger.

Spec: docs/superpowers/specs/2026-07-10-battery-cash-ledger-design.md

Covers:
- cash_flows_eur: attribution math (solar-first min() rule), leg skipping
- price_at: current-slot import price lookup (static-mode-safe path)
- controller: per-tick accumulation, rollover, persistence (Tasks 2-3)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.anker_x1_smartgrid.models import PriceSlot
from custom_components.anker_x1_smartgrid.optimize import cash_flows_eur
from custom_components.anker_x1_smartgrid.resolution import price_at

BASE = datetime(2026, 6, 25, 14, 0, tzinfo=timezone.utc)

TICK_H = 60.0 / 3600.0  # one 60s tick in hours


class TestCashFlowsEur:
    def test_grid_charge_costs_import_price(self):
        # Importing 1500 W while battery charges 2000 W → grid-attributed
        # charge = min(1500, 2000) = 1500 W → 0.025 kWh × 0.30 = 0.0075 €.
        cost, credit = cash_flows_eur(1500.0, -2000.0, 0.30, 0.25, TICK_H)
        assert cost == pytest.approx(0.0075)
        assert credit == 0.0

    def test_solar_first_house_covering_limits_charge_attribution(self):
        # Battery charges 2000 W but only 500 W is imported (PV covers the
        # rest): grid-attributed charge = min(500, 2000) = 500 W.
        cost, credit = cash_flows_eur(500.0, -2000.0, 0.30, None, TICK_H)
        assert cost == pytest.approx(500.0 / 1000.0 * TICK_H * 0.30)
        assert credit == 0.0

    def test_battery_export_earns_effective_price(self):
        # Exporting 1500 W while battery discharges 1800 W → battery-sourced
        # export = min(1500, 1800) = 1500 W → 0.025 kWh × 0.25 = 0.00625 €.
        cost, credit = cash_flows_eur(-1500.0, 1800.0, 0.30, 0.25, TICK_H)
        assert cost == 0.0
        assert credit == pytest.approx(0.00625)

    def test_pv_spill_export_not_credited(self):
        # Exporting 2000 W but battery only discharges 300 W: PV spill is
        # out of scope — credit only min(2000, 300) = 300 W.
        cost, credit = cash_flows_eur(-2000.0, 300.0, 0.30, 0.25, TICK_H)
        assert credit == pytest.approx(300.0 / 1000.0 * TICK_H * 0.25)

    def test_none_import_price_skips_cost_leg_only(self):
        cost, credit = cash_flows_eur(1500.0, -2000.0, None, 0.25, TICK_H)
        assert cost == 0.0

    def test_none_export_price_skips_credit_leg_only(self):
        cost, credit = cash_flows_eur(-1500.0, 1800.0, 0.30, None, TICK_H)
        assert credit == 0.0

    def test_negative_import_price_yields_negative_cost(self):
        # Negative day-ahead hour: charging is a cash GAIN — unclamped.
        cost, _ = cash_flows_eur(1500.0, -2000.0, -0.05, None, TICK_H)
        assert cost == pytest.approx(-0.00125)

    def test_idle_battery_produces_no_flows(self):
        cost, credit = cash_flows_eur(400.0, 0.0, 0.30, 0.25, TICK_H)
        assert cost == 0.0
        assert credit == 0.0


class TestPriceAt:
    def test_finds_containing_slot_at_60min(self):
        slots = [
            PriceSlot(start=BASE, price=0.30),
            PriceSlot(start=BASE + timedelta(hours=1), price=0.10),
        ]
        assert price_at(slots, BASE + timedelta(minutes=30), 60) == 0.30
        assert price_at(slots, BASE + timedelta(minutes=61), 60) == 0.10

    def test_respects_explicit_duration_min_at_15min(self):
        slots = [
            PriceSlot(start=BASE, price=0.30, duration_min=15.0),
            PriceSlot(start=BASE + timedelta(minutes=15), price=0.10, duration_min=15.0),
        ]
        assert price_at(slots, BASE + timedelta(minutes=16), 15) == 0.10

    def test_no_matching_slot_returns_none(self):
        slots = [PriceSlot(start=BASE, price=0.30)]
        assert price_at(slots, BASE + timedelta(hours=3), 60) is None
        assert price_at(slots, BASE - timedelta(minutes=1), 60) is None

    def test_empty_slots_returns_none(self):
        assert price_at([], BASE, 60) is None
