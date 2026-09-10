"""Pure calibration cost-model tests. No HA, no I/O, no clock."""

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.anker_x1_smartgrid import calibration, daily_stats
from custom_components.anker_x1_smartgrid.models import Config

CFG = Config(
    capacity_kwh=20.0, max_charge_w=12000.0, eta_charge=0.92, calibration_top_soc=100.0, calibration_dwell_h=0.5
)
BASE = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


def _slot(t0, minutes=60, **kw):
    fields = dict(
        import_price=0.20,
        export_price=0.20,
        pv_kwh=0.0,
        load_kwh=0.0,
        soc_start=100.0,
        soc_end=100.0,
        plan_import_kwh=0.0,
        plan_export_kwh=0.0,
    )
    fields.update(kw)
    return calibration.CalibSlot(t0=t0, t1=t0 + timedelta(minutes=minutes), **fields)


def peak_export_slot(t0, minutes=60):
    """A slot the DP plans to export from at a high price -- holding through
    it (rather than exporting) is the expensive case."""
    return _slot(
        t0,
        minutes,
        import_price=0.40,
        export_price=0.40,
        load_kwh=0.5,
        soc_start=100.0,
        soc_end=67.5,
        plan_export_kwh=6.0,
    )


def midday_spill_slot(t0, minutes=60):
    """A slot with PV surplus and no export planned -- calibrating here
    spills the same surplus the plan already spills."""
    return _slot(
        t0,
        minutes,
        import_price=0.10,
        export_price=0.10,
        pv_kwh=3.5,
        load_kwh=0.5,
        soc_start=100.0,
        soc_end=100.0,
        plan_export_kwh=3.0,
    )


def test_window_cost_pre_peak_hold_is_expensive():
    slot = peak_export_slot(BASE)
    cost = calibration.window_cost([slot], BASE, BASE + timedelta(minutes=30), cfg=CFG, water_value=0.21)
    assert cost == pytest.approx(0.6175)


def test_window_cost_midday_spill_is_free():
    slot = midday_spill_slot(BASE)
    cost = calibration.window_cost([slot], BASE, BASE + timedelta(minutes=60), cfg=CFG, water_value=0.21)
    assert cost == pytest.approx(0.0)


def test_window_cost_topup_credit_with_water_value():
    slot = _slot(BASE, soc_start=95.0, soc_end=95.0)
    end = BASE + timedelta(hours=1.0 / (12.0 * CFG.eta_charge) + CFG.calibration_dwell_h)
    cost = calibration.window_cost([slot], BASE, end, cfg=CFG, water_value=0.21)
    assert cost == pytest.approx(0.20 / CFG.eta_charge - 0.21)


def test_window_cost_topup_credit_without_water_value():
    slot = _slot(BASE, soc_start=95.0, soc_end=95.0)
    end = BASE + timedelta(hours=1.0 / (12.0 * CFG.eta_charge) + CFG.calibration_dwell_h)
    cost = calibration.window_cost([slot], BASE, end, cfg=CFG, water_value=None)
    assert cost == pytest.approx(0.20 / CFG.eta_charge)


def test_window_cost_two_short_slots():
    slot0 = _slot(BASE, minutes=15, import_price=0.10, export_price=0.10, load_kwh=0.25, soc_start=100.0, soc_end=98.75)
    slot1 = _slot(
        BASE + timedelta(minutes=15),
        minutes=15,
        import_price=0.30,
        export_price=0.30,
        load_kwh=0.25,
        soc_start=98.75,
        soc_end=97.5,
    )
    cost = calibration.window_cost([slot0, slot1], BASE, BASE + timedelta(minutes=30), cfg=CFG, water_value=0.21)
    assert cost == pytest.approx(-0.005)


def test_window_cost_none_export_price_zeroes_revenue_leg():
    slot = dataclasses.replace(peak_export_slot(BASE), export_price=None)
    cost = calibration.window_cost([slot], BASE, BASE + timedelta(minutes=30), cfg=CFG, water_value=None)
    assert cost == pytest.approx(0.10)


def test_window_cost_none_when_window_extends_past_last_slot():
    slot = _slot(BASE, minutes=60)
    cost = calibration.window_cost(
        [slot], BASE + timedelta(hours=2), BASE + timedelta(hours=2, minutes=30), cfg=CFG, water_value=0.21
    )
    assert cost is None


def test_window_cost_none_across_a_gap_between_slots():
    slot0 = _slot(BASE, minutes=15)
    slot1 = _slot(BASE + timedelta(minutes=30), minutes=15)  # 15-min gap after slot0's t1
    cost = calibration.window_cost([slot0, slot1], BASE, BASE + timedelta(minutes=45), cfg=CFG, water_value=0.21)
    assert cost is None


def _plan_row(start, **kw):
    row = dict(
        start=start.isoformat(),
        price=0.20,
        soc=100.0,
        mode=None,
        estimated=False,
        pv_kwh=0.0,
        load_kwh=0.0,
        solar_charge_kwh=0.0,
        grid_charge_kwh=0.0,
        self_discharge_kwh=0.0,
        grid_export_kwh=0.0,
    )
    row.update(kw)
    return row


def test_build_calib_slots():
    now = BASE + timedelta(minutes=15)
    horizon = [
        _plan_row(BASE - timedelta(hours=1), price=0.05, soc=85.0, mode="actual"),
        _plan_row(BASE, price=0.20, soc=80.0, pv_kwh=2.0, load_kwh=1.0, grid_charge_kwh=0.9),
        _plan_row(BASE + timedelta(hours=1), price=0.25, soc=90.0, pv_kwh=1.0, load_kwh=0.5, grid_charge_kwh=0.5),
        _plan_row(BASE + timedelta(hours=2), price=0.15, soc=95.0, estimated=True),
    ]

    def delivered_at(start):
        return 0.3 if start == BASE else 0.0

    def export_price_at(start, price):
        return price - 0.01

    slots = calibration.build_calib_slots(
        horizon, now, live_soc=75.0, slot_minutes=60, export_price_at=export_price_at, delivered_at=delivered_at
    )

    assert len(slots) == 2
    slot0, slot1 = slots

    assert slot0.t0 == now
    assert slot0.t1 == BASE + timedelta(hours=1)
    assert slot0.pv_kwh == pytest.approx(1.5)
    assert slot0.load_kwh == pytest.approx(0.75)
    assert slot0.soc_start == pytest.approx(75.0)
    assert slot0.soc_end == pytest.approx(80.0)
    assert slot0.export_price == pytest.approx(export_price_at(BASE, 0.20))
    expected_imp, expected_exp = daily_stats.planned_house_flows({**horizon[1], "pv_kwh": 1.5, "load_kwh": 0.75}, 0.6)
    assert slot0.plan_import_kwh == pytest.approx(expected_imp)
    assert slot0.plan_export_kwh == pytest.approx(expected_exp)

    assert slot1.soc_start == pytest.approx(80.0)
    assert slot1.soc_end == pytest.approx(90.0)
    assert slot1.export_price == pytest.approx(export_price_at(BASE + timedelta(hours=1), 0.25))


def test_plan_soc_at_interpolates_and_none_outside():
    slot0 = _slot(BASE, minutes=60, soc_start=80.0, soc_end=90.0)
    slot1 = _slot(BASE + timedelta(minutes=60), minutes=60, soc_start=90.0, soc_end=95.0)
    slots = [slot0, slot1]

    assert calibration._plan_soc_at(slots, BASE + timedelta(minutes=30)) == pytest.approx(85.0)
    assert calibration._plan_soc_at(slots, BASE - timedelta(minutes=1)) is None
    assert calibration._plan_soc_at(slots, BASE + timedelta(minutes=150)) is None
