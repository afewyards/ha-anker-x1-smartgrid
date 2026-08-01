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
