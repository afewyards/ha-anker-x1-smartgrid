"""TDD tests for per-day grid/€ statistics.

Spec: docs/superpowers/specs/2026-08-01-card-crop-daily-stats-design.md
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone, UTC

import pytest

from custom_components.anker_x1_smartgrid import daily_stats

# UTC+2 — the lab/France local zone in August. Chosen so that a 22:00Z tick
# lands on the NEXT local day, which is what makes the tz parameter matter.
CEST = timezone(timedelta(hours=2))


def _row(ts: datetime, **cols) -> dict:
    base = {
        "ts": ts.isoformat(),
        "grid_import_kwh": 0.0,
        "grid_export_kwh": 0.0,
        "batt_charge_kwh": 0.0,
        "batt_discharge_kwh": 0.0,
        "import_price": 0.30,
        "export_price": 0.25,
    }
    base.update(cols)
    return base


class TestAggregateActualDays:
    def test_grid_charge_is_the_min_of_import_and_battery_charge(self):
        # Imported 0.05 kWh while the battery took 0.02 kWh: only 0.02 is
        # grid-attributed (the rest fed the house).
        rows = [_row(datetime(2026, 7, 20, 10, 0, tzinfo=UTC), grid_import_kwh=0.05, batt_charge_kwh=0.02)]
        out = daily_stats.aggregate_actual_days(rows, 0.0, CEST)
        day = out[date(2026, 7, 20)]
        assert day["grid_charge_kwh"] == pytest.approx(0.02)
        assert day["cost_eur"] == pytest.approx(0.02 * 0.30)
        assert day["grid_export_kwh"] == 0.0
        assert day["revenue_eur"] == 0.0

    def test_export_is_battery_sourced_only_and_fee_is_subtracted(self):
        # Exported 0.10 kWh but the battery only discharged 0.04: the rest is
        # PV spill. Fee 0.02 → credited at 0.25 - 0.02.
        rows = [_row(datetime(2026, 7, 20, 12, 0, tzinfo=UTC), grid_export_kwh=0.10, batt_discharge_kwh=0.04)]
        out = daily_stats.aggregate_actual_days(rows, 0.02, CEST)
        day = out[date(2026, 7, 20)]
        assert day["grid_export_kwh"] == pytest.approx(0.04)
        assert day["revenue_eur"] == pytest.approx(0.04 * 0.23)

    def test_ticks_accumulate_within_a_day(self):
        rows = [
            _row(datetime(2026, 7, 20, 1, 0, tzinfo=UTC), grid_import_kwh=0.03, batt_charge_kwh=0.03),
            _row(datetime(2026, 7, 20, 2, 0, tzinfo=UTC), grid_import_kwh=0.03, batt_charge_kwh=0.03),
        ]
        out = daily_stats.aggregate_actual_days(rows, 0.0, CEST)
        assert out[date(2026, 7, 20)]["grid_charge_kwh"] == pytest.approx(0.06)
        assert out[date(2026, 7, 20)]["coverage_ticks"] == 2

    def test_buckets_on_the_local_day_not_utc(self):
        # 22:30Z is 00:30 the NEXT day in CEST.
        rows = [_row(datetime(2026, 7, 20, 22, 30, tzinfo=UTC), grid_import_kwh=0.01, batt_charge_kwh=0.01)]
        out = daily_stats.aggregate_actual_days(rows, 0.0, CEST)
        assert set(out) == {date(2026, 7, 21)}

    def test_null_price_zeroes_only_its_own_leg(self):
        # France: export_price entity unconfigured → NULL. Charge still costs.
        rows = [
            _row(
                datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
                grid_import_kwh=0.02,
                batt_charge_kwh=0.02,
                grid_export_kwh=0.05,
                batt_discharge_kwh=0.05,
                export_price=None,
            )
        ]
        out = daily_stats.aggregate_actual_days(rows, 0.0, CEST)
        day = out[date(2026, 7, 20)]
        assert day["cost_eur"] == pytest.approx(0.02 * 0.30)
        assert day["revenue_eur"] == 0.0
        # kWh is still measured — only the € leg drops out.
        assert day["grid_export_kwh"] == pytest.approx(0.05)

    def test_null_delta_row_counts_as_null_tick_and_contributes_nothing(self):
        rows = [
            _row(datetime(2026, 7, 20, 3, 0, tzinfo=UTC), grid_import_kwh=0.02, batt_charge_kwh=0.02),
            _row(datetime(2026, 7, 20, 4, 0, tzinfo=UTC), batt_charge_kwh=None),
        ]
        out = daily_stats.aggregate_actual_days(rows, 0.0, CEST)
        day = out[date(2026, 7, 20)]
        assert day["grid_charge_kwh"] == pytest.approx(0.02)
        assert day["coverage_ticks"] == 1
        assert day["null_ticks"] == 1

    def test_unparseable_and_missing_ts_rows_are_skipped(self):
        rows = [
            {"ts": None},
            {"ts": "not-a-timestamp"},
            _row(datetime(2026, 7, 20, 5, 0, tzinfo=UTC), grid_import_kwh=0.01, batt_charge_kwh=0.01),
        ]
        out = daily_stats.aggregate_actual_days(rows, 0.0, CEST)
        assert set(out) == {date(2026, 7, 20)}

    def test_empty_input(self):
        assert daily_stats.aggregate_actual_days([], 0.0, CEST) == {}


class TestLedgerParity:
    def test_recorded_replay_equals_live_ledger_euros(self):
        """A synthetic tick stream priced both ways must agree to 1e-9.

        Live path:     optimize.cash_flows_eur(meter_w, batt_w, ...) per tick.
        Recorded path: daily_stats.aggregate_actual_days over the SAME ticks
                       expressed as v9 kWh deltas.

        If these ever diverge, one of the two attribution sites has grown its
        own copy of the min() rule.
        """
        from custom_components.anker_x1_smartgrid.optimize import cash_flows_eur

        tick_h = 60.0 / 3600.0
        fee = 0.015
        # (meter_w, batt_w) pairs: grid charge, PV-covered charge, battery
        # export (cap ties with discharge), PV-spill export (discharge
        # binds), idle, a partially-covered charge, and export-cap-binds
        # (discharge exceeds what actually leaves via the meter).
        ticks = [
            (1500.0, -2000.0),
            (200.0, -2000.0),
            (-2500.0, 2500.0),
            (-3000.0, 1000.0),
            (0.0, 0.0),
            (900.0, -400.0),
            # Export cap binds: battery discharges 1500 W but only 500 W
            # leaves via the meter — the rest served house load. Without
            # the grid_export_kwh cap this tick's credit would be
            # over-attributed to the battery leg.
            (-500.0, 1500.0),
        ]
        import_price, raw_export_price = 0.31, 0.24

        live_cost = live_credit = 0.0
        rows = []
        base = datetime(2026, 7, 20, 6, 0, tzinfo=UTC)
        for i, (meter_w, batt_w) in enumerate(ticks):
            cost, credit = cash_flows_eur(meter_w, batt_w, import_price, raw_export_price - fee, tick_h)
            live_cost += cost
            live_credit += credit
            rows.append(
                {
                    "ts": (base + timedelta(minutes=i)).isoformat(),
                    # Recorder columns are the UNattributed per-leg deltas;
                    # the min() happens inside aggregate_actual_days.
                    "grid_import_kwh": max(0.0, meter_w) / 1000.0 * tick_h,
                    "grid_export_kwh": max(0.0, -meter_w) / 1000.0 * tick_h,
                    "batt_charge_kwh": max(0.0, -batt_w) / 1000.0 * tick_h,
                    "batt_discharge_kwh": max(0.0, batt_w) / 1000.0 * tick_h,
                    "import_price": import_price,
                    "export_price": raw_export_price,
                }
            )

        out = daily_stats.aggregate_actual_days(rows, fee, CEST)
        day = out[date(2026, 7, 20)]
        assert day["cost_eur"] == pytest.approx(live_cost, abs=1e-9)
        assert day["revenue_eur"] == pytest.approx(live_credit, abs=1e-9)


def _plan_row(start: datetime, **cols) -> dict:
    base = {
        "start": start.isoformat(),
        "price": 0.30,
        "grid_charge_kwh": 0.0,
        "grid_export_kwh": 0.0,
        "estimated": False,
        "mode": "idle",
    }
    base.update(cols)
    return base


def _flat_export(_start):
    return 0.20


class TestAggregatePlannedDays:
    def test_sums_charge_cost_and_export_revenue(self):
        horizon = [
            _plan_row(datetime(2026, 8, 2, 2, 0, tzinfo=UTC), grid_charge_kwh=2.0, mode="grid"),
            _plan_row(datetime(2026, 8, 2, 17, 0, tzinfo=UTC), grid_export_kwh=1.5, mode="export"),
        ]
        out = daily_stats.aggregate_planned_days(horizon, _flat_export, CEST)
        day = out[date(2026, 8, 2)]
        assert day["grid_charge_kwh"] == pytest.approx(2.0)
        assert day["grid_export_kwh"] == pytest.approx(1.5)
        assert day["cost_eur"] == pytest.approx(2.0 * 0.30)
        assert day["revenue_eur"] == pytest.approx(1.5 * 0.20)

    def test_estimated_rows_are_excluded(self):
        horizon = [
            _plan_row(datetime(2026, 8, 3, 2, 0, tzinfo=UTC), grid_charge_kwh=5.0, estimated=True, mode="estimated"),
        ]
        assert daily_stats.aggregate_planned_days(horizon, _flat_export, CEST) == {}

    def test_actual_mode_rows_are_excluded(self):
        # Past plan rows are back-filled measurements; counting them here
        # would double-count against the ledger's today figures.
        horizon = [
            _plan_row(datetime(2026, 8, 1, 8, 0, tzinfo=UTC), grid_charge_kwh=3.0, mode="actual"),
        ]
        assert daily_stats.aggregate_planned_days(horizon, _flat_export, CEST) == {}

    def test_export_price_none_zeroes_only_the_revenue_leg(self):
        horizon = [
            _plan_row(datetime(2026, 8, 2, 17, 0, tzinfo=UTC), grid_export_kwh=1.5, grid_charge_kwh=1.0, mode="export"),
        ]
        out = daily_stats.aggregate_planned_days(horizon, lambda _s: None, CEST)
        day = out[date(2026, 8, 2)]
        assert day["revenue_eur"] == 0.0
        assert day["grid_export_kwh"] == pytest.approx(1.5)
        assert day["cost_eur"] == pytest.approx(1.0 * 0.30)

    def test_per_slot_export_curve_is_honoured(self):
        peak = datetime(2026, 8, 2, 17, 0, tzinfo=UTC)
        off = datetime(2026, 8, 2, 11, 0, tzinfo=UTC)
        curve = {peak: 0.40, off: 0.05}
        horizon = [
            _plan_row(peak, grid_export_kwh=1.0, mode="export"),
            _plan_row(off, grid_export_kwh=1.0, mode="export"),
        ]
        out = daily_stats.aggregate_planned_days(horizon, lambda s: curve.get(s), CEST)
        assert out[date(2026, 8, 2)]["revenue_eur"] == pytest.approx(0.45)

    def test_buckets_on_the_local_day(self):
        # 22:30Z is 00:30 the next day in CEST.
        horizon = [_plan_row(datetime(2026, 8, 2, 22, 30, tzinfo=UTC), grid_charge_kwh=1.0, mode="grid")]
        out = daily_stats.aggregate_planned_days(horizon, _flat_export, CEST)
        assert set(out) == {date(2026, 8, 3)}

    def test_empty_and_none_horizon(self):
        assert daily_stats.aggregate_planned_days([], _flat_export, CEST) == {}
        assert daily_stats.aggregate_planned_days(None, _flat_export, CEST) == {}


class TestAggregatePlannedDaysInProgressSlot:
    """The in-progress clock-hour seam (review finding I1).

    ``past_actuals`` stops strictly BEFORE the current clock-hour, so no row of
    that hour is ``mode == "actual"`` — yet the live ledger has already booked
    every kWh that flowed in it.  Two independent leaks therefore have to be
    closed, and at 15-min slots (live on both deployments) they compound:

    1. Elapsed slots of the current hour still carry their modelled energy.
    2. ``plan.build_horizon`` adds the hour's ALREADY-DELIVERED kWh to EVERY
       row of that hour (its ``delivered_by_hour`` lookup is clock-hour keyed,
       not slot keyed), so all four quarters each claim the full hour's charge.
    """

    # 10:37Z, mid-way through the 10:00 clock-hour, at 15-min slot resolution.
    NOW = datetime(2026, 8, 2, 10, 37, tzinfo=UTC)
    HOUR = datetime(2026, 8, 2, 10, 0, tzinfo=UTC)
    DELIVERED = 2.0  # kWh already delivered this clock-hour, per the recorder

    def _quarters(self, modelled: float) -> list[dict]:
        # What build_horizon really emits for the in-progress hour: the
        # modelled remainder for that slot PLUS the whole hour's delivered kWh.
        return [
            _plan_row(self.HOUR + timedelta(minutes=15 * i), grid_charge_kwh=modelled + self.DELIVERED, mode="grid")
            for i in range(4)
        ]

    def _delivered_at(self, start):
        return self.DELIVERED if start.replace(minute=0) == self.HOUR else 0.0

    def test_already_started_slots_are_skipped(self):
        # 10:00 / 10:15 / 10:30 have elapsed; only 10:45 is still ahead.
        out = daily_stats.aggregate_planned_days(self._quarters(0.5), _flat_export, CEST, self.NOW)
        assert out[date(2026, 8, 2)]["grid_charge_kwh"] == pytest.approx(0.5 + self.DELIVERED)

    def test_delivered_add_back_is_subtracted_from_the_remaining_slots(self):
        out = daily_stats.aggregate_planned_days(
            self._quarters(0.5), _flat_export, CEST, self.NOW, self._delivered_at
        )
        day = out[date(2026, 8, 2)]
        # Only 10:45 survives, and its 2.5 is the 0.5 modelled remainder plus
        # the 2.0 the ledger already holds. Without BOTH guards this reads
        # 4 x 2.5 = 10.0 kWh -- a 20x overstatement of the real remainder.
        assert day["grid_charge_kwh"] == pytest.approx(0.5)
        assert day["cost_eur"] == pytest.approx(0.5 * 0.30)

    def test_future_hours_are_untouched(self):
        # The delivered dict only ever holds the current clock-hour, so a
        # later slot must keep its full modelled energy.
        later = _plan_row(datetime(2026, 8, 2, 17, 0, tzinfo=UTC), grid_charge_kwh=3.0, mode="grid")
        out = daily_stats.aggregate_planned_days(
            [later], _flat_export, CEST, self.NOW, self._delivered_at
        )
        assert out[date(2026, 8, 2)]["grid_charge_kwh"] == pytest.approx(3.0)

    def test_subtraction_clamps_at_zero(self):
        # An idle in-progress hour: the row is pure add-back, nothing modelled.
        rows = [_plan_row(self.HOUR + timedelta(minutes=45), grid_charge_kwh=self.DELIVERED, mode="grid")]
        out = daily_stats.aggregate_planned_days(rows, _flat_export, CEST, self.NOW, self._delivered_at)
        assert out[date(2026, 8, 2)]["grid_charge_kwh"] == 0.0
        assert out[date(2026, 8, 2)]["cost_eur"] == 0.0

    def test_defaults_stay_byte_identical_when_neither_is_supplied(self):
        # Both parameters are optional; omitting them must reproduce the
        # pre-fix behaviour exactly (no accidental filtering).
        rows = self._quarters(0.5)
        assert daily_stats.aggregate_planned_days(rows, _flat_export, CEST) == daily_stats.aggregate_planned_days(
            rows, _flat_export, CEST, None, None
        )


def _totals(charge=0.0, export=0.0, cost=0.0, revenue=0.0) -> dict:
    out = daily_stats.new_day_totals()
    out.update(
        {"grid_charge_kwh": charge, "grid_export_kwh": export, "cost_eur": cost, "revenue_eur": revenue}
    )
    return out


TODAY = date(2026, 8, 1)


class TestMergeDays:
    def test_past_day_is_actual_only(self):
        actual = {date(2026, 7, 31): _totals(charge=4.0, export=2.0, cost=1.20, revenue=0.60)}
        rows = daily_stats.merge_days(actual, {}, _totals(), TODAY)
        row = next(r for r in rows if r["date"] == "2026-07-31")
        assert row["source"] == "actual"
        assert row["net_eur"] == pytest.approx(-0.60)
        assert row["actual_net_eur"] == pytest.approx(-0.60)
        assert row["planned_net_eur"] is None

    def test_future_day_is_plan_only(self):
        planned = {date(2026, 8, 2): _totals(charge=6.0, export=3.0, cost=1.50, revenue=1.10)}
        rows = daily_stats.merge_days({}, planned, _totals(), TODAY)
        row = next(r for r in rows if r["date"] == "2026-08-02")
        assert row["source"] == "plan"
        assert row["net_eur"] == pytest.approx(-0.40)
        assert row["actual_net_eur"] is None
        assert row["planned_net_eur"] == pytest.approx(-0.40)

    def test_today_sums_actual_so_far_and_planned_remainder(self):
        today_totals = _totals(charge=2.0, export=1.0, cost=0.50, revenue=0.30)
        planned = {TODAY: _totals(charge=1.0, export=4.0, cost=0.25, revenue=1.40)}
        rows = daily_stats.merge_days({}, planned, today_totals, TODAY)
        row = next(r for r in rows if r["date"] == "2026-08-01")
        assert row["source"] == "mixed"
        assert row["grid_charge_kwh"] == pytest.approx(3.0)
        assert row["grid_export_kwh"] == pytest.approx(5.0)
        assert row["actual_net_eur"] == pytest.approx(-0.20)
        assert row["planned_net_eur"] == pytest.approx(1.15)
        assert row["net_eur"] == pytest.approx(0.95)

    def test_live_ledger_wins_over_a_samples_entry_for_today(self):
        # The samples pass also covers today; the ledger is authoritative
        # because the card subtitle already publishes it.
        actual = {TODAY: _totals(charge=99.0, cost=99.0)}
        rows = daily_stats.merge_days(actual, {}, _totals(charge=2.0, cost=0.50), TODAY)
        row = next(r for r in rows if r["date"] == "2026-08-01")
        assert row["grid_charge_kwh"] == pytest.approx(2.0)
        assert row["cost_eur"] == pytest.approx(0.50)

    def test_today_always_present_even_with_no_data(self):
        rows = daily_stats.merge_days({}, {}, _totals(), TODAY)
        assert [r["date"] for r in rows] == ["2026-08-01"]

    def test_rows_are_ordered_oldest_first(self):
        actual = {date(2026, 7, 30): _totals(), date(2026, 7, 31): _totals()}
        planned = {date(2026, 8, 2): _totals()}
        rows = daily_stats.merge_days(actual, planned, _totals(), TODAY)
        assert [r["date"] for r in rows] == ["2026-07-30", "2026-07-31", "2026-08-01", "2026-08-02"]

    def test_days_older_than_the_window_are_dropped(self):
        actual = {
            date(2026, 7, 18): _totals(charge=1.0),  # exactly 14 days back — kept
            date(2026, 7, 17): _totals(charge=1.0),  # 15 days back — dropped
        }
        rows = daily_stats.merge_days(actual, {}, _totals(), TODAY)
        dates = [r["date"] for r in rows]
        assert "2026-07-18" in dates
        assert "2026-07-17" not in dates

    def test_window_days_is_configurable(self):
        actual = {date(2026, 7, 30): _totals(charge=1.0), date(2026, 7, 29): _totals(charge=1.0)}
        rows = daily_stats.merge_days(actual, {}, _totals(), TODAY, window_days=2)
        assert [r["date"] for r in rows] == ["2026-07-30", "2026-08-01"]

    def test_date_is_an_iso_string_not_a_date_object(self):
        rows = daily_stats.merge_days({}, {}, _totals(), TODAY)
        assert isinstance(rows[0]["date"], str)
