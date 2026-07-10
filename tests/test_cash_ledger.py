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


from custom_components.anker_x1_smartgrid.models import PlantInputs
from tests.test_export_pnl_ledger import (  # noqa: E402
    _StubHass,
    _make_controller,
)


def _ledger_ctrl():
    hass = _StubHass()
    # export_fee_eur_per_kwh: _make_export_cfg's defaults dict does not
    # override this field, so Config's own default (0.02, see
    # const.DEFAULT_EXPORT_FEE_EUR_PER_KWH) would otherwise leak through and
    # break the "eff = raw" pass-through assumed by the cash-ledger tests
    # below. Zero it explicitly so effective_export_price(raw, cfg) == raw.
    ctrl, _act, _store, _rec = _make_controller(
        hass, cfg_overrides={"export_fee_eur_per_kwh": 0.0}
    )
    return ctrl, hass


class TestCashLedgerAccumulate:
    def test_grid_charge_tick_accumulates_cost(self):
        ctrl, hass = _ledger_ctrl()
        hass.set_state("sensor.battery_power", "-2000.0")  # charging
        inputs = PlantInputs(soc=50.0, meter_w=1500.0, now=BASE)  # importing
        slots = [PriceSlot(start=BASE, price=0.30)]
        ctrl._accumulate_cash_ledger(BASE, inputs, slots, 60, None)
        assert ctrl.today_charge_cost_eur == pytest.approx(0.0075)
        assert ctrl.today_export_revenue_eur == 0.0
        assert ctrl.total_net_eur == pytest.approx(-0.0075)

    def test_export_tick_accumulates_credit_at_effective_price(self):
        # export_fee_eur_per_kwh defaults to 0 in _make_export_cfg → eff = raw.
        ctrl, hass = _ledger_ctrl()
        hass.set_state("sensor.battery_power", "1800.0")  # discharging
        inputs = PlantInputs(soc=50.0, meter_w=-1500.0, now=BASE)  # exporting
        ctrl._accumulate_cash_ledger(BASE, inputs, [], 60, 0.25)
        assert ctrl.today_export_revenue_eur == pytest.approx(0.00625)
        assert ctrl.today_charge_cost_eur == 0.0
        assert ctrl.total_net_eur == pytest.approx(0.00625)

    def test_missing_battery_reading_skips_both_legs(self):
        ctrl, hass = _ledger_ctrl()  # battery_power never set → read None
        inputs = PlantInputs(soc=50.0, meter_w=1500.0, now=BASE)
        ctrl._accumulate_cash_ledger(
            BASE, inputs, [PriceSlot(start=BASE, price=0.30)], 60, 0.25
        )
        assert ctrl.today_charge_cost_eur == 0.0
        assert ctrl.today_export_revenue_eur == 0.0
        assert ctrl.total_net_eur == 0.0

    def test_no_matching_slot_skips_cost_leg_not_credit_leg(self):
        # Static-mode regression guard: cost leg must key off the slots list.
        ctrl, hass = _ledger_ctrl()
        hass.set_state("sensor.battery_power", "1800.0")
        inputs = PlantInputs(soc=50.0, meter_w=-1500.0, now=BASE)
        stale = [PriceSlot(start=BASE - timedelta(hours=6), price=0.30)]
        ctrl._accumulate_cash_ledger(BASE, inputs, stale, 60, 0.25)
        assert ctrl.today_charge_cost_eur == 0.0
        assert ctrl.today_export_revenue_eur == pytest.approx(0.00625)

    def test_accumulation_compounds_across_ticks(self):
        ctrl, hass = _ledger_ctrl()
        hass.set_state("sensor.battery_power", "-2000.0")
        inputs = PlantInputs(soc=50.0, meter_w=1500.0, now=BASE)
        slots = [PriceSlot(start=BASE, price=0.30)]
        ctrl._accumulate_cash_ledger(BASE, inputs, slots, 60, None)
        ctrl._accumulate_cash_ledger(BASE, inputs, slots, 60, None)
        assert ctrl.today_charge_cost_eur == pytest.approx(0.015)
        assert ctrl.total_net_eur == pytest.approx(-0.015)


class TestCashLedgerRollover:
    def test_rollover_resets_daily_fields_not_total(self):
        ctrl, _hass = _ledger_ctrl()
        ctrl.today_export_pnl_eur = 0.5
        ctrl.today_charge_cost_eur = 1.0
        ctrl.today_export_revenue_eur = 2.0
        ctrl.total_net_eur = 1.0
        ctrl._export_pnl_day = "2026-06-24"  # yesterday relative to BASE
        ctrl._rollover_daily_ledgers(BASE)
        assert ctrl.today_export_pnl_eur == 0.0
        assert ctrl.today_charge_cost_eur == 0.0
        assert ctrl.today_export_revenue_eur == 0.0
        assert ctrl.total_net_eur == 1.0  # lifetime survives
        assert ctrl._export_pnl_day is not None

    def test_same_day_rollover_is_noop(self):
        ctrl, _hass = _ledger_ctrl()
        ctrl._rollover_daily_ledgers(BASE)  # sets the key
        ctrl.today_charge_cost_eur = 1.0
        ctrl._rollover_daily_ledgers(BASE + timedelta(minutes=5))
        assert ctrl.today_charge_cost_eur == 1.0


class TestCashLedgerStatus:
    def test_status_exposes_cash_keys(self):
        ctrl, _hass = _ledger_ctrl()
        ctrl.today_charge_cost_eur = 0.75
        ctrl.today_export_revenue_eur = 2.0
        ctrl.total_net_eur = 12.345
        status = ctrl._status(BASE, 0.0, None, "test")
        assert status["today_charge_cost_eur"] == pytest.approx(0.75)
        assert status["today_export_revenue_eur"] == pytest.approx(2.0)
        assert status["battery_net_today_eur"] == pytest.approx(1.25)
        assert status["battery_net_total_eur"] == pytest.approx(12.345)
