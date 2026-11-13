from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.anker_x1_smartgrid.margin import battery_margin_by_day
from custom_components.anker_x1_smartgrid.usecost import (
    Inventory,
    cheapest_window,
    hourly_series,
    live_state,
    merge_days,
    replay,
    roll_plan,
)

AMS = ZoneInfo("Europe/Amsterdam")
CAP = 20.0
T0 = datetime(2026, 11, 10, 8, 0, tzinfo=UTC)
DAY = date(2026, 11, 10)


def _phase(start, minutes, soc0, soc1, batt, price, *, pv=0.0, load=0.0, p1=None, export=None, state=None):
    """One row per minute; SoC moves linearly. ``p1`` defaults to the lossless balance."""
    return [
        {
            "ts": (start + timedelta(minutes=k)).isoformat(),
            "soc": soc0 + (soc1 - soc0) * k / minutes,
            "batt_w": batt,
            "pv_w": pv,
            "load_w": load,
            "p1_w": load - pv - batt if p1 is None else p1,
            "import_price": price,
            "export_price": export,
            "state": state,
        }
        for k in range(minutes)
    ]


def _end(at, soc):
    return [
        {"ts": at.isoformat(), "soc": soc, "batt_w": 0.0, "pv_w": 0.0, "load_w": 0.0, "p1_w": 0.0, "import_price": 0.5}
    ]


H1, H2 = T0 + timedelta(hours=1), T0 + timedelta(hours=2)


class TestReplay:
    def test_battery_use_costs_the_released_share_of_what_charging_cost(self):
        # 10 kWh AC in at 0.20 stores 9.5 kWh; 9.4 kWh DC out delivers 8.0 kWh AC to the house.
        rows = (
            _phase(T0, 60, 0, 47.5, -10_000, 0.20)
            + _phase(H1, 60, 47.5, 0.5, 9_400, 0.40, load=8_000, p1=0.0)
            + _end(H2, 0.5)
        )
        rec = replay(rows, CAP, AMS)[0][DAY]
        assert rec["use_cost_eur"] == pytest.approx(9.4 / 9.5 * 2.00)

    def test_a_fall_with_no_output_is_house_cost_in_full(self):
        # Standby: the pack powers the inverter, nothing reaches the AC bus.
        rows = _phase(T0, 60, 50, 49, 100, 0.30, load=0.0, p1=0.0) + _end(H1, 49)
        rec = replay(rows, CAP, AMS)[0][DAY]
        assert rec["use_cost_eur"] == pytest.approx(10 * 0.30 * 0.2 / 10)

    def test_solar_charge_is_free_for_use_cost_and_costs_the_export_price_for_saved(self):
        rows = _phase(T0, 60, 50, 70, -4_000, 0.30, pv=5_000, load=1_000, export=0.10) + _end(H1, 70)
        _days, inv = replay(rows, CAP, AMS, export_fee=0.02)
        assert inv.e_kwh == pytest.approx(14.0)
        assert inv.c_free == pytest.approx(10 * 0.30)
        assert inv.c_exp == pytest.approx(10 * 0.30 + 4 * 0.08)
        assert (inv.grid_kwh, inv.sun_kwh) == pytest.approx((10.0, 4.0))

    def test_only_the_house_share_of_a_discharge_is_use_cost(self):
        # 4 kWh out, 1 kWh to the house, 3 kWh exported.
        rows = _phase(T0, 60, 50, 30, 4_000, 0.30, load=1_000, p1=-3_000) + _end(H1, 30)
        rec = replay(rows, CAP, AMS)[0][DAY]
        assert rec["use_cost_eur"] == pytest.approx(3.0 * 4 / 10 * 0.25)

    def test_grid_direct_is_the_deficit_the_battery_did_not_cover(self):
        # deficit 2.5 kWh, battery covers 1.0 AC, grid 1.5 at 0.40; battery part releases 4.0 x 1/10.
        rows = _phase(T0, 60, 50, 45, 1_000, 0.40, pv=500, load=3_000, p1=1_500) + _end(H1, 45)
        rec = replay(rows, CAP, AMS)[0][DAY]
        assert rec["use_cost_eur"] == pytest.approx(1.5 * 0.40 + 0.4)
        assert rec["use_kwh"] == pytest.approx(3.0)
        assert rec["solar_kwh"] == pytest.approx(0.5)

    def test_gap_over_five_minutes_books_nothing_and_rescales_both_costs(self):
        rows = [
            {"ts": T0.isoformat(), "soc": 50, "batt_w": 10_000, "load_w": 10_000, "p1_w": 0, "import_price": 0.5},
            {"ts": (T0 + timedelta(minutes=10)).isoformat(), "soc": 40, "batt_w": 0, "load_w": 0, "import_price": 0.5},
        ]
        days, inv = replay(rows, CAP, AMS)
        assert days[DAY]["use_kwh"] == 0.0 and days[DAY]["use_cost_eur"] == 0.0 and days[DAY]["saved_eur"] == 0.0
        assert (inv.e_kwh, inv.c_free, inv.c_exp, inv.grid_kwh) == pytest.approx((8.0, 4.0, 4.0, 8.0))

    def test_saved_and_solar_gain_are_the_battery_margins_two_parts(self):
        # Across local midnight (23:00Z) and a gap, mixed grid and solar charge.
        t = datetime(2026, 11, 10, 20, 0, tzinfo=UTC)
        rows = (
            _phase(T0, 60, 10, 50, -10_000, 0.30, pv=3_000, load=500, export=0.12)
            + _phase(H1, 60, 50, 20, 6_000, 0.45, pv=200, load=2_500, export=0.12)
            + _phase(H2, 60, 20, 18, 100, 0.25, load=300)
            + _phase(t, 60, 40, 70, -6_000, 0.20)
            + _phase(t + timedelta(hours=1, minutes=20), 120, 70, 30, 4_000, 0.40, load=3_000, export=0.05)
            + _end(t + timedelta(hours=3, minutes=20), 30)
        )
        days, _inv = replay(rows, CAP, AMS, export_fee=0.02)
        margin = battery_margin_by_day(rows, CAP, AMS, 0.02)
        assert set(days) == set(margin) == {DAY, date(2026, 11, 11)}
        for day, m in margin.items():
            assert days[day]["saved_eur"] + days[day]["solar_gain_eur"] == pytest.approx(m["value_eur"] - m["cost_eur"])
            assert days[day]["saved_eur"] == pytest.approx(m["margin_eur"])
            assert days[day]["solar_gain_eur"] == pytest.approx(m["solar_gain_eur"])
        assert margin[DAY]["solar_gain_eur"] != pytest.approx(0.0)

    def test_a_gap_keeps_the_grid_and_solar_parts_of_the_stock(self):
        # 10 kWh grid at 0.30 and 5 kWh solar at 0.20, then 10 minutes unrecorded: the level reads
        # 12 kWh, so both parts keep 80%. All 12 kWh then go to the house at 0.50, a third of it solar.
        t1, t2 = H1 + timedelta(hours=1), H1 + timedelta(hours=1, minutes=10)
        rows = (
            _phase(T0, 60, 0, 50, -10_000, 0.30, export=0.20)
            + _phase(H1, 60, 50, 75, -5_000, 0.30, pv=5_000, export=0.20)
            + _end(t1, 75)
            + _phase(t2, 60, 60, 0, 12_000, 0.50, load=12_000, export=0.20)
            + _end(t2 + timedelta(hours=1), 0)
        )
        rec = replay(rows, CAP, AMS)[0][DAY]
        assert rec["solar_gain_eur"] == pytest.approx(12 * 0.50 / 3 - 5 * 0.20 * 0.8)
        assert rec["saved_eur"] == pytest.approx(12 * 0.50 * 2 / 3 - 10 * 0.30 * 0.8)
        assert battery_margin_by_day(rows, CAP, AMS)[DAY]["solar_gain_eur"] == pytest.approx(rec["solar_gain_eur"])

    def test_a_25_hour_local_day_sums_into_one_date(self):
        # 2026-10-25 is the end of CEST: 00:00 local is 22:00Z the day before, the day lasts 25 h.
        start = datetime(2026, 10, 24, 22, 0, tzinfo=UTC)
        rows = []
        for h in range(25):
            rows += _phase(start + timedelta(hours=h), 60, 50, 50, 0.0, 0.20 if h < 12 else 0.30, load=1_000)
        rows += _end(start + timedelta(hours=25), 50)
        days = replay(rows, CAP, AMS)[0]
        rec = days[date(2026, 10, 25)]
        assert rec["use_kwh"] == pytest.approx(25.0)
        assert rec["avg_price"] == pytest.approx((12 * 0.20 + 13 * 0.30) / 25)
        assert days[date(2026, 10, 26)]["use_kwh"] == 0.0

    def test_empty_input(self):
        days, inv = replay([], CAP, AMS)
        assert days == {} and inv.e_kwh == 0.0


def _slot(start, soc, price, *, load=0.0, pv=0.0, grid=0.0, solar=0.0, house=0.0, export=0.0, mode="idle", est=False):
    return {
        "start": start.isoformat(),
        "price": price,
        "soc": soc,
        "load_kwh": load,
        "pv_kwh": pv,
        "grid_charge_kwh": grid,
        "solar_charge_kwh": solar,
        "self_discharge_kwh": house,
        "grid_export_kwh": export,
        "mode": "estimated" if est else mode,
        "estimated": est,
    }


def _exp_at(_start, _price):
    return 0.05


NOW = datetime(2026, 11, 10, 21, 0, tzinfo=UTC)  # 22:00 local


class TestRollPlan:
    def test_planned_charge_is_booked_on_the_day_the_house_uses_it(self):
        inv = Inventory(2.0, 0.6, 0.6, 2.0, 0.0)
        horizon = [
            _slot(NOW + timedelta(hours=1), 57.5, 0.20, grid=10.0, mode="grid"),  # 23:00 local
            _slot(NOW + timedelta(hours=2), 35.0, 0.40, load=4.0, house=4.0),  # 00:00 local, next day
        ]
        planned = roll_plan(horizon, inv, CAP, AMS, NOW, _exp_at)
        released = (0.6 + 2.0) * 4.5 / 11.5
        assert planned[DAY]["use_cost_eur"] == pytest.approx(0.0)
        assert planned[date(2026, 11, 11)]["use_cost_eur"] == pytest.approx(released)
        assert planned[date(2026, 11, 11)]["saved_eur"] == pytest.approx(4.0 * 0.40 - released)
        assert planned[date(2026, 11, 11)]["solar_gain_eur"] == 0.0
        assert (inv.e_kwh, inv.c_free, inv.grid_kwh, inv.sun_kwh) == (2.0, 0.6, 2.0, 0.0), (
            "the caller's inventory is not touched"
        )

    def test_solar_charge_is_free_for_use_and_export_valued_for_solar_gain(self):
        inv = Inventory(10.0, 3.0, 3.0, 10.0, 0.0)
        horizon = [
            _slot(NOW, 60.0, 0.30, pv=3.0, load=1.0, solar=2.0, mode="solar"),
            _slot(NOW + timedelta(hours=1), 50.0, 0.30, load=2.0, house=2.0),
        ]
        rec = roll_plan(horizon, inv, CAP, AMS, NOW, _exp_at, export_fee=0.02)[DAY]
        assert rec["use_cost_eur"] == pytest.approx(3.0 * 2 / 12)
        # The stock is 10 kWh grid and 2 kWh solar; the 2 kWh out draw on both in proportion.
        assert rec["saved_eur"] == pytest.approx(2.0 * 0.30 * 10 / 12 - 3.0 * 2 / 12)
        assert rec["solar_gain_eur"] == pytest.approx(2.0 * 0.30 * 2 / 12 - 2.0 * 0.05 * 2 / 12)
        assert rec["saved_eur"] + rec["solar_gain_eur"] == pytest.approx(2.0 * 0.30 - (3.0 + 2.0 * 0.05) * 2 / 12)

    def test_output_is_valued_by_the_stock_before_the_slots_own_charge(self):
        # All-grid stock; one slot charges 2 kWh solar and exports 4 kWh, so the level falls 10 -> 8.
        horizon = [_slot(NOW, 40.0, 0.30, solar=2.0, export=4.0, mode="export")]
        rec = roll_plan(horizon, Inventory(10.0, 3.0, 3.0, 10.0, 0.0), CAP, AMS, NOW, _exp_at)[DAY]
        assert rec["saved_eur"] == pytest.approx(4.0 * 0.05 - 3.0 * 0.2)
        # The export earns nothing as sun; the net fall still releases its share of the fresh solar's cost.
        assert rec["solar_gain_eur"] == pytest.approx(-2.0 * 0.05 * 0.2)
        assert rec["saved_eur"] + rec["solar_gain_eur"] == pytest.approx(4.0 * 0.05 - (3.0 + 2.0 * 0.05) * 0.2)

    def test_a_solar_charged_stock_earns_solar_gain_only(self):
        horizon = [
            _slot(NOW, 50.0, 0.30, pv=10.0, solar=10.0, mode="solar"),
            _slot(NOW + timedelta(hours=1), 0.0, 0.40, load=10.0, house=10.0),
        ]
        rec = roll_plan(horizon, Inventory(0.0, 0.0, 0.0, 0.0, 0.0), CAP, AMS, NOW, _exp_at)[DAY]
        assert rec["saved_eur"] == pytest.approx(0.0)
        assert rec["solar_gain_eur"] == pytest.approx(10.0 * 0.40 - 10.0 * 0.05)

    def test_grid_direct_and_export_share(self):
        inv = Inventory(10.0, 3.0, 3.0, 10.0, 0.0)
        # 1 kWh of house deficit from the battery, 1 from the grid, 2 kWh exported: share 1/3.
        horizon = [_slot(NOW, 35.0, 0.40, load=2.5, pv=0.5, house=1.0, export=2.0, mode="export")]
        rec = roll_plan(horizon, inv, CAP, AMS, NOW, _exp_at)[DAY]
        assert rec["use_cost_eur"] == pytest.approx(1.0 * 0.40 + 3.0 * 3 / 10 / 3)

    def test_finished_and_actual_rows_do_not_count(self):
        inv = Inventory(10.0, 3.0, 3.0, 10.0, 0.0)
        horizon = [
            _slot(NOW - timedelta(minutes=15), 50.0, 0.30, load=9.0),  # ends at NOW
            _slot(NOW + timedelta(hours=1), 50.0, 0.30, load=9.0, mode="actual"),
            _slot(NOW, 50.0, 0.30, load=1.0),
        ]
        assert roll_plan(horizon, inv, CAP, AMS, NOW, _exp_at)[DAY]["use_kwh"] == pytest.approx(1.0)

    def test_the_slot_in_progress_counts_what_is_left_of_it(self):
        # 21:00-21:30Z at 21:10Z: two thirds left. Load is whole-slot; the grid column
        # already covers only the rest, plus the 1 kWh delivered so far.
        now = NOW + timedelta(minutes=10)
        horizon = [
            _slot(NOW, 60.0, 0.20, load=0.6, grid=3.0, mode="grid"),
            _slot(NOW + timedelta(minutes=30), 50.0, 0.40, load=2.0, house=2.0),
        ]
        rec = roll_plan(
            horizon,
            Inventory(10.0, 3.0, 3.0, 10.0, 0.0),
            CAP,
            AMS,
            now,
            _exp_at,
            delivered_at=lambda s: 1.0 * (s == NOW),
        )[DAY]
        assert rec["use_kwh"] == pytest.approx(0.4 + 2.0)
        assert rec["use_cost_eur"] == pytest.approx(0.4 * 0.20 + (3.0 + 2.0 * 0.20) * 2 / 12)
        assert rec["avg_price"] == pytest.approx((0.20 / 3 + 0.40 / 2) / (1 / 3 + 1 / 2))

    def test_rows_at_the_soc_floor_put_nothing_out_of_the_battery(self):
        # plan.py keeps planning house use from the battery once the level sits clamped at the floor.
        horizon = [_slot(NOW + timedelta(minutes=15 * k), 5.0, 0.25, load=0.4, house=0.4) for k in range(4)]
        rec = roll_plan(horizon, Inventory(1.0, 0.25, 0.25, 1.0, 0.0), CAP, AMS, NOW, _exp_at)[DAY]
        assert rec["use_cost_eur"] == pytest.approx(4 * 0.4 * 0.25)
        assert rec["saved_eur"] == pytest.approx(0.0)

    def test_the_drift_hedge_debit_is_not_house_use(self):
        # The hedge takes 1 kWh off the plan's level for display; the cost per stored kWh stays 0.30.
        horizon = [
            _slot(NOW, 45.0, 0.30) | {"hedge_kwh": 1.0},
            _slot(NOW + timedelta(hours=1), 35.0, 0.30, load=2.0, house=2.0),
        ]
        rec = roll_plan(horizon, Inventory(10.0, 3.0, 3.0, 10.0, 0.0), CAP, AMS, NOW, _exp_at)[DAY]
        assert rec["use_cost_eur"] == pytest.approx(2.0 * 0.30)
        assert rec["saved_eur"] == pytest.approx(0.0)

    def test_missing_horizon_plans_nothing(self):
        assert roll_plan(None, Inventory(10.0, 3.0, 3.0, 10.0, 0.0), CAP, AMS, NOW, _exp_at) == {}


class TestMergeDays:
    def test_today_is_actual_so_far_plus_the_planned_rest(self):
        # Yesterday and today 00:00-22:00 local measured at 1 kW / 0.30; 22:00-24:00 planned at 2 kWh/h / 0.40.
        start = datetime(2026, 11, 8, 23, 0, tzinfo=UTC)
        rows = _phase(start, 46 * 60, 50, 50, 0.0, 0.30, load=1_000) + _end(NOW, 50)
        actual, inv = replay(rows, CAP, AMS)
        horizon = [_slot(NOW + timedelta(hours=h), 50.0, 0.40, load=2.0, pv=0.5) for h in range(2)]
        planned = roll_plan(horizon, inv, CAP, AMS, NOW, _exp_at)
        y, t, tm = merge_days(actual, planned, DAY)
        assert (y["date"], y["planned"], y["use_kwh"]) == ("2026-11-09", "none", 24.0)
        assert (t["date"], t["planned"]) == ("2026-11-10", "partial")
        assert t["use_kwh"] == pytest.approx(22 + 4)
        assert t["solar_kwh"] == pytest.approx(1.0)
        assert t["use_cost_eur"] == pytest.approx(22 * 0.30 + 3 * 0.40)
        assert t["avg_price"] == pytest.approx((22 * 0.30 + 2 * 0.40) / 24, abs=1e-4)
        assert tm == {
            "date": "2026-11-11",
            "use_kwh": None,
            "use_cost_eur": None,
            "saved_eur": None,
            "solar_gain_eur": None,
            "avg_price": None,
            "solar_kwh": None,
            "planned": "none",
            "grid_in_kwh": None,
            "grid_out_kwh": None,
            "cheap": None,
        }

    def test_tomorrow_before_prices_shows_use_and_solar_only(self):
        inv = Inventory(10.0, 3.0, 3.0, 10.0, 0.0)
        tomorrow = datetime(2026, 11, 10, 23, 0, tzinfo=UTC)
        horizon = [_slot(NOW, 50.0, 0.30, load=1.0)] + [
            _slot(tomorrow + timedelta(hours=h), 45.0, 0.30, load=1.0, pv=0.25, est=True) for h in range(24)
        ]
        _y, _t, tm = merge_days({}, roll_plan(horizon, inv, CAP, AMS, NOW, _exp_at), DAY)
        assert tm["planned"] == "full"
        assert tm["use_kwh"] == pytest.approx(24.0) and tm["solar_kwh"] == pytest.approx(6.0)
        assert tm["use_cost_eur"] is None and tm["saved_eur"] is None and tm["avg_price"] is None
        assert tm["solar_gain_eur"] is None

    def test_grid_day_totals_are_the_sum_of_that_days_hours(self):
        # Measured from yesterday 00:00 local to NOW at 1 kW from the grid, but today two hours
        # also take a 2 kW grid charge and one hour sells 1.5 kW.
        start = datetime(2026, 11, 8, 23, 0, tzinfo=UTC)
        rows = _phase(start, 27 * 60, 50, 50, 0.0, 0.30, load=1_000)
        rows += _phase(start + timedelta(hours=27), 120, 50, 70, -2_000, 0.20, load=1_000)
        rows += _phase(start + timedelta(hours=29), 4 * 60, 70, 70, 0.0, 0.30, load=1_000)
        rows += _phase(start + timedelta(hours=33), 60, 70, 60, 2_500, 0.40, load=1_000)
        rows += _phase(start + timedelta(hours=34), 12 * 60, 60, 60, 0.0, 0.30, load=1_000)
        rows += _end(NOW, 60)
        actual, inv = replay(rows, CAP, AMS)
        horizon = [
            _slot(NOW, 70.0, 0.20, load=0.5, grid=2.0, mode="grid"),
            _slot(NOW + timedelta(hours=1), 65.0, 0.40, load=1.0, house=0.6, export=0.4, mode="export"),
            _slot(NOW + timedelta(hours=2), 60.0, 0.30, load=1.0, house=1.0),
        ]
        planned = roll_plan(horizon, inv, CAP, AMS, NOW, _exp_at)
        hours = hourly_series(rows, horizon, AMS, NOW)
        y, t, tm = merge_days(actual, planned, DAY, hours)

        def total(day, *keys):
            return sum(h[k] for h in hours if h["start"].startswith(day) for k in keys)

        assert y["grid_in_kwh"] == pytest.approx(24.0)
        assert y["grid_out_kwh"] == pytest.approx(0.0)
        assert t["grid_in_kwh"] == pytest.approx(total("2026-11-10", "grid_in_house_kwh", "grid_in_charge_kwh"))
        assert t["grid_out_kwh"] == pytest.approx(total("2026-11-10", "grid_out_kwh"))
        # 22 measured hours at 1 kWh less the sold one plus 4 charged; then the planned 2.5 and 0.4.
        assert t["grid_in_kwh"] == pytest.approx(21 + 4 + 2.5 + 0.4)
        assert t["grid_out_kwh"] == pytest.approx(1.5 + 0.4)
        assert tm["date"] == "2026-11-11"
        assert tm["grid_in_kwh"] == pytest.approx(total("2026-11-11", "grid_in_house_kwh", "grid_in_charge_kwh"))
        assert tm["grid_in_kwh"] == pytest.approx(0.0)

    def test_grid_totals_are_unknown_when_the_days_figures_are(self):
        inv = Inventory(10.0, 3.0, 3.0, 10.0, 0.0)
        tomorrow = datetime(2026, 11, 10, 23, 0, tzinfo=UTC)
        horizon = [_slot(NOW, 50.0, 0.30, load=1.0)] + [
            _slot(tomorrow + timedelta(hours=h), 45.0, 0.30, load=1.0, est=True) for h in range(24)
        ]
        hours = hourly_series([], horizon, AMS, NOW)
        y, t, tm = merge_days({}, roll_plan(horizon, inv, CAP, AMS, NOW, _exp_at), DAY, hours)
        assert y["grid_in_kwh"] is None and y["grid_out_kwh"] is None
        assert t["grid_in_kwh"] == pytest.approx(1.0)
        assert tm["use_kwh"] == pytest.approx(24.0)
        assert tm["grid_in_kwh"] is None and tm["grid_out_kwh"] is None

    def test_a_25_hour_day_sums_all_its_hours(self):
        start = datetime(2026, 10, 24, 22, 0, tzinfo=UTC)
        now = start + timedelta(hours=26)
        rows = _phase(start - timedelta(hours=24), 50 * 60, 50, 50, 0.0, 0.30, load=1_000) + _end(now, 50)
        actual, _inv = replay(rows, CAP, AMS)
        hours = hourly_series(rows, [], AMS, now)
        y, _t, _tm = merge_days(actual, {}, date(2026, 10, 26), hours)
        assert y["date"] == "2026-10-25"
        assert y["grid_in_kwh"] == pytest.approx(25.0)


class TestHourlySeries:
    # NOW is 22:00 local; yesterday 00:00 local is 2026-11-08T23:00Z.
    def _past(self, *phases):
        rows = [r for ph in phases for r in ph]
        return hourly_series(rows + _end(NOW, rows[-1]["soc"]), [], AMS, NOW)

    def _state(self, *phases):
        return self._past(*phases)[0]["state"]

    def test_recorded_hold_marks_the_hour(self):
        assert self._state(_phase(T0, 60, 50, 50, 0.0, 0.3, load=400, state="hold")) == "hold"

    def test_hold_needs_more_than_half_the_hour_and_nothing_moving(self):
        held_short = _phase(T0, 20, 50, 50, 0.0, 0.3, state="hold") + _phase(
            T0 + timedelta(minutes=20), 40, 50, 50, 0.0, 0.3
        )
        assert self._state(held_short) == "idle"
        held_then_used = _phase(T0, 40, 50, 50, 0.0, 0.3, state="hold") + _phase(
            T0 + timedelta(minutes=40), 20, 50, 48, 1_000, 0.3, load=1_000
        )
        assert self._state(held_then_used) == "house"

    def test_50_w_dead_band(self):
        assert self._state(_phase(T0, 60, 50, 50, 40.0, 0.3, load=40)) == "idle"
        assert self._state(_phase(T0, 60, 50, 50, -40.0, 0.3)) == "idle"

    def test_discharge_while_the_meter_exports_is_export(self):
        assert self._state(_phase(T0, 60, 50, 40, 3_000, 0.3, load=500)) == "export"
        assert self._state(_phase(T0, 60, 50, 45, 500, 0.3, load=800)) == "house"

    def test_charge_is_split_into_grid_and_solar_by_the_pv_surplus(self):
        assert self._state(_phase(T0, 60, 50, 60, -2_000, 0.3, pv=2_500, load=500)) == "charge_solar"
        assert self._state(_phase(T0, 60, 50, 60, -2_000, 0.3, pv=1_300, load=500)) == "charge_grid"

    def test_most_energy_wins(self):
        hour = _phase(T0, 30, 50, 52, -1_000, 0.3, pv=1_500, load=500) + _phase(
            T0 + timedelta(minutes=30), 30, 52, 51, 600, 0.3, load=600
        )
        assert self._state(hour) == "charge_solar"

    def test_past_hour_soc_is_the_end_of_the_hour_and_price_is_the_mean(self):
        hour = _phase(T0, 30, 50, 55, 0.0, 0.2) + _phase(T0 + timedelta(minutes=30), 30, 55, 60, 0.0, 0.4)
        row = self._past(hour, _end(H1, 60))[0]
        assert row["soc"] == pytest.approx(59.8, abs=0.1)
        assert row["price"] == pytest.approx(0.3)
        assert row["plan"] is False
        assert row["start"] == "2026-11-10T09:00:00+01:00"

    def test_plan_hour_averages_the_quarters_and_maps_the_mode(self):
        q = [_slot(NOW + timedelta(minutes=15 * k), 50 - k, 0.1 * (k + 1), load=0.25, house=0.25) for k in range(4)]
        (row,) = hourly_series([], q, AMS, NOW)
        assert row["price"] == pytest.approx(0.25)
        assert row["soc"] == 47
        assert row["state"] == "house"
        assert row["use_kwh"] == pytest.approx(1.0)
        assert row["plan"] is True

    @pytest.mark.parametrize(
        ("slot", "state"),
        [
            ({"mode": "hold"}, "hold"),
            ({"mode": "grid", "grid": 2.0}, "charge_grid"),
            ({"mode": "export", "export": 1.5, "house": 0.2}, "export"),
            ({"mode": "solar", "solar": 0.8, "pv": 1.5}, "charge_solar"),
            ({"mode": "solar", "pv": 1.5}, "idle"),
            ({"mode": "idle"}, "idle"),
        ],
    )
    def test_plan_modes(self, slot, state):
        (row,) = hourly_series([], [_slot(NOW, 50, 0.3, **slot)], AMS, NOW)
        assert row["state"] == state

    def test_the_seam_at_now_keeps_one_row_for_the_running_hour(self):
        now = datetime(2026, 11, 10, 9, 30, tzinfo=UTC)
        rows = _phase(T0, 90, 50, 50, 0.0, 0.3, load=1_000) + _end(now, 50)
        horizon = [
            _slot(T0 + timedelta(hours=1), 50, 0.3, mode="actual", load=0.25),
            _slot(T0 + timedelta(hours=1, minutes=15), 49, 0.3, load=0.25),  # in progress
            _slot(now, 48, 0.3, load=0.25),
            _slot(now + timedelta(minutes=15), 47, 0.3, load=0.25),
        ]
        series = hourly_series(rows, horizon, AMS, now)
        assert [r["start"] for r in series] == ["2026-11-10T09:00:00+01:00", "2026-11-10T10:00:00+01:00"]
        assert series[1]["use_kwh"] == pytest.approx(0.5 + 0.5)
        assert series[1]["soc"] == 47
        assert (series[0]["plan"], series[1]["plan"]) == (False, True)

    def test_the_slot_in_progress_adds_what_is_left_of_it(self):
        now = datetime(2026, 11, 10, 9, 40, tzinfo=UTC)
        rows = _phase(H1, 40, 50, 50, 0.0, 0.3, load=1_200) + _end(now, 50)
        horizon = [
            _slot(H1 + timedelta(minutes=30), 49, 0.6, load=0.3),  # in progress, a third left
            _slot(H1 + timedelta(minutes=45), 48, 0.6, load=0.3),
        ]
        (row,) = hourly_series(rows, horizon, AMS, now)
        assert row["use_kwh"] == pytest.approx(0.8 + 0.1 + 0.3)
        assert row["price"] == pytest.approx((0.3 * 40 + 0.6 * 20) / 60)

    def test_plan_rows_at_the_soc_floor_are_not_house(self):
        rows = _phase(NOW - timedelta(minutes=10), 10, 5, 5, 0.0, 0.3)
        horizon = [_slot(NOW + timedelta(minutes=15 * k), 5.0, 0.25, load=0.4, house=0.4) for k in range(4)]
        assert hourly_series(rows, horizon, AMS, NOW)[-1]["state"] == "idle"

    def test_hours_follow_the_local_clock_in_half_hour_zones(self):
        tz = ZoneInfo("Asia/Kolkata")
        now = datetime(2026, 11, 10, 3, 0, tzinfo=UTC)  # 08:30 local
        rows = _phase(now - timedelta(hours=1), 60, 50, 50, 0.0, 0.3, load=1_000) + _end(now, 50)
        series = hourly_series(rows, [], tz, now)
        assert [r["start"] for r in series] == ["2026-11-10T07:00:00+05:30", "2026-11-10T08:00:00+05:30"]
        assert [r["use_kwh"] for r in series] == pytest.approx([0.5, 0.5])

    def test_series_starts_yesterday_midnight_local(self):
        rows = _phase(datetime(2026, 11, 8, 22, 0, tzinfo=UTC), 120, 50, 50, 0.0, 0.3)
        starts = [r["start"] for r in self._past(rows)]
        assert starts[0] == "2026-11-09T00:00:00+01:00"

    def test_estimated_rows_carry_use_and_solar_only(self):
        (row,) = hourly_series([], [_slot(NOW, 40, 0.3, load=1.0, pv=0.5, house=1.0, est=True)], AMS, NOW)
        assert (row["price"], row["soc"], row["state"]) == (None, None, "idle")
        assert (row["use_kwh"], row["solar_kwh"]) == (1.0, 0.5)
        assert (row["grid_in_house_kwh"], row["grid_in_charge_kwh"], row["grid_out_kwh"]) == (None, None, None)

    def _grid(self, *phases):
        row = self._past(*phases, _end(H1, 50))[0]
        return row["grid_in_house_kwh"], row["grid_in_charge_kwh"], row["grid_out_kwh"]

    def test_past_import_splits_into_house_and_grid_charge(self):
        assert self._grid(_phase(T0, 60, 50, 60, -2_000, 0.3, load=500)) == pytest.approx((0.5, 2.0, 0.0))

    def test_grid_charge_cannot_exceed_the_import(self):
        # The meter shows less than the battery took: the solar surplus is not grid charge either.
        assert self._grid(_phase(T0, 60, 50, 60, -2_000, 0.3, load=500, p1=1_500)) == pytest.approx((0.0, 1.5, 0.0))
        assert self._grid(_phase(T0, 60, 50, 60, -2_000, 0.3, pv=1_300, load=500)) == pytest.approx((0.0, 1.2, 0.0))

    def test_past_export_is_the_meter_export(self):
        assert self._grid(_phase(T0, 60, 50, 40, 3_000, 0.3, load=500)) == pytest.approx((0.0, 0.0, 2.5))

    def test_past_hours_without_a_meter_reading_are_unknown(self):
        rows = [r | {"p1_w": None} for r in _phase(T0, 60, 50, 50, 0.0, 0.3, load=500)]
        assert self._grid(rows) == (None, None, None)

    def test_plan_grid_is_charge_plus_the_deficit_the_battery_does_not_cover(self):
        horizon = [
            _slot(NOW, 60, 0.2, load=0.3, grid=2.0, mode="grid"),
            _slot(NOW + timedelta(hours=1), 50, 0.4, load=1.0, pv=0.2, house=0.5, export=1.2, mode="export"),
            _slot(NOW + timedelta(hours=2), 50, 0.3, load=0.2, pv=1.0, solar=0.5, mode="solar"),
        ]
        rows = hourly_series([], horizon, AMS, NOW)
        grid = [(r["grid_in_house_kwh"], r["grid_in_charge_kwh"], r["grid_out_kwh"]) for r in rows]
        assert grid == pytest.approx([(0.3, 2.0, 0.0), (0.3, 0.0, 1.2), (0.0, 0.0, 0.3)])

    def test_plan_rows_at_the_soc_floor_draw_the_house_from_the_grid(self):
        rows = _phase(NOW - timedelta(minutes=10), 10, 5, 5, 0.0, 0.3)
        horizon = [_slot(NOW + timedelta(minutes=15 * k), 5.0, 0.25, load=0.4, house=0.4) for k in range(4)]
        assert hourly_series(rows, horizon, AMS, NOW)[-1]["grid_in_house_kwh"] == pytest.approx(1.6)

    def test_the_running_hour_adds_measured_grid_and_the_planned_rest(self):
        # 09:00-09:40Z measured at 1.2 kW grid charge; the slot in progress has a third left and
        # 1 kWh of its 2 kWh grid column was delivered already.
        now = datetime(2026, 11, 10, 9, 40, tzinfo=UTC)
        rows = _phase(H1, 40, 50, 55, -1_200, 0.3) + _end(now, 55)
        horizon = [
            _slot(H1 + timedelta(minutes=30), 56, 0.3, load=0.3, grid=2.0, mode="grid"),
            _slot(H1 + timedelta(minutes=45), 58, 0.3, load=0.3, grid=0.5, mode="grid"),
        ]
        (row,) = hourly_series(rows, horizon, AMS, now, delivered_at=lambda s: 1.0 * (s == H1 + timedelta(minutes=30)))
        assert row["grid_in_charge_kwh"] == pytest.approx(0.8 + 1.0 + 0.5)
        assert row["grid_in_house_kwh"] == pytest.approx(0.1 + 0.3)


@pytest.mark.parametrize(
    ("recorded", "exporting", "batt", "p1", "state"),
    [
        ("forcing", False, -9_000, 9_500, "charge_grid"),
        ("hold", False, 10, 300, "hold"),
        ("passive", True, 20, 0, "export"),
        ("passive", False, -1_200, -100, "charge_solar"),
        ("passive", False, 800, 50, "house"),
        ("passive", False, 800, -300, "export"),
        ("passive", False, 45, 300, "idle"),
        ("passive", False, None, 300, "idle"),
        ("disabled", False, 800, 50, "disabled"),
    ],
)
def test_live_state(recorded, exporting, batt, p1, state):
    assert live_state(recorded, exporting, batt, p1) == state


def _day_rows(day, prices, first_plan=0):
    t0 = datetime.combine(day, datetime.min.time(), tzinfo=AMS).astimezone(UTC)
    out = []
    for i, p in enumerate(prices):
        t = (t0 + timedelta(hours=i)).astimezone(AMS)
        out.append(
            {
                "start": t.isoformat(),
                "price": p,
                "plan": i >= first_plan,
                "grid_out_kwh": 0.0,
                "grid_in_house_kwh": 0.0,
                "grid_in_charge_kwh": 0.0,
            }
        )
    return out


class TestCheapestWindow:
    def test_cheapest_three_hours_ahead_only(self):
        prices = [0.1] * 3 + [0.5] * 9 + [0.4, 0.3, 0.3, 0.3] + [0.5] * 8
        w = cheapest_window(_day_rows(DAY, prices, first_plan=10))
        assert w["start"] == "2026-11-10T13:00:00+01:00"
        assert w["end"] == "2026-11-10T16:00:00+01:00"
        assert w["price"] == 0.3

    def test_end_of_day_window_ends_at_midnight(self):
        w = cheapest_window(_day_rows(DAY, [0.5] * 21 + [0.1] * 3))
        assert w["end"] == "2026-11-11T00:00:00+01:00"

    def test_less_than_three_hours_left(self):
        assert cheapest_window(_day_rows(DAY, [0.2] * 24, first_plan=22)) is None

    def test_unpriced_hours_break_a_window_and_unpublished_day_is_none(self):
        assert cheapest_window(_day_rows(DAY, [None] * 24)) is None
        assert cheapest_window(_day_rows(DAY, [0.1, 0.1, None, 0.1, 0.1])) is None

    def test_tie_takes_the_earliest(self):
        w = cheapest_window(_day_rows(DAY, [0.2] * 24))
        assert w["start"].startswith("2026-11-10T00:00")

    def test_dst_days(self):
        spring = cheapest_window(_day_rows(date(2026, 3, 29), [0.5] * 1 + [0.1] * 3 + [0.5] * 19))
        assert spring["start"] == "2026-03-29T01:00:00+01:00" and spring["end"] == "2026-03-29T05:00:00+02:00"
        long = cheapest_window(_day_rows(date(2026, 10, 25), [0.5] * 2 + [0.1] * 3 + [0.5] * 20))
        assert long["start"] == "2026-10-25T02:00:00+02:00" and long["end"] == "2026-10-25T04:00:00+01:00"
        fall = cheapest_window(_day_rows(date(2026, 10, 25), [0.5] * 2 + [0.1] * 3 + [0.5] * 20))
        assert fall["start"] == "2026-10-25T02:00:00+02:00" and fall["price"] == 0.1


class TestMergeDaysCheap:
    @staticmethod
    def _rec(est=False):
        return {
            "use_kwh": 1.0, "use_cost_eur": 1.0, "saved_eur": 0.0, "solar_gain_eur": 0.0, "solar_kwh": 0.0,
            "price_eur_h": 1.0, "price_h": 1.0, "estimated": est,
        }  # fmt: skip

    def _hours(self):
        prices = [0.5] * 21 + [0.1] * 3
        return [r for k in (-1, 0, 1) for r in _day_rows(DAY + timedelta(days=k), prices)]

    def test_priced_today_and_tomorrow_have_a_window_yesterday_none(self):
        actual = {DAY - timedelta(days=1): self._rec(), DAY: self._rec()}
        planned = {DAY: self._rec(), DAY + timedelta(days=1): self._rec()}
        y, t, tm = merge_days(actual, planned, DAY, self._hours())
        assert y["cheap"] is None
        assert t["cheap"]["price"] == 0.1 and tm["cheap"]["price"] == 0.1

    def test_estimated_day_has_none(self):
        planned = {DAY: self._rec(), DAY + timedelta(days=1): self._rec(est=True)}
        _y, t, tm = merge_days({DAY: self._rec()}, planned, DAY, self._hours())
        assert t["cheap"] is not None and tm["cheap"] is None
