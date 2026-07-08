"""Tests for the v10 samples_hourly kWh sum columns and aggregate_hour()'s
tier-1/tier-2 kWh SUM pass.

See docs/superpowers/plans/2026-07-06-rollup-kwh-sums.md for the design.
"""
import sqlite3

import pytest

from custom_components.anker_x1_smartgrid.recorder import DataRecorder, _VERSION_TARGET
from custom_components.anker_x1_smartgrid.rollup import aggregate_hour

KWH_SUM_COLS = {
    "grid_import_kwh_sum", "grid_export_kwh_sum", "house_load_kwh_sum",
    "pv_kwh_sum", "batt_charge_kwh_sum", "batt_discharge_kwh_sum",
}


def _hourly_columns(db_path):
    conn = sqlite3.connect(db_path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(samples_hourly)")}
    conn.close()
    return cols


# ---------------------------------------------------------------------------
# v10 migration
# ---------------------------------------------------------------------------

def test_v10_fresh_db_has_kwh_sum_columns_and_version(tmp_path):
    db = str(tmp_path / "t.db")
    rec = DataRecorder(db)
    rec.close()
    assert _VERSION_TARGET == 10
    assert KWH_SUM_COLS <= _hourly_columns(db)
    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 10
    conn.close()


def test_v10_crash_idempotent_rerun(tmp_path):
    """Crash between ALTERs and version bump: columns exist but user_version
    is still 9 — re-running the ladder must be a safe no-op that lands on 10."""
    db = str(tmp_path / "t.db")
    DataRecorder(db).close()
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA user_version = 9")
    conn.commit()
    conn.close()
    rec = DataRecorder(db)  # must not raise "duplicate column name"
    rec.close()
    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 10
    conn.close()


def test_v10_migration_backfills_house_load_and_pv_from_mean(tmp_path):
    """Existing samples_hourly rows (pre-v10) get house_load/pv kWh sums
    backfilled via tier-2 (mean/1000); grid/batt stay NULL (no sign-split
    data available in the hourly table)."""
    db = str(tmp_path / "t.db")
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA user_version = 9")
    conn.execute(
        "CREATE TABLE samples_hourly (hour_ts TEXT PRIMARY KEY, "
        "house_load_mean REAL, pv_w_mean REAL)"
    )
    conn.execute(
        "INSERT INTO samples_hourly (hour_ts, house_load_mean, pv_w_mean) "
        "VALUES ('2026-07-01T10:00:00+00:00', 1500.0, 300.0)"
    )
    conn.commit()
    conn.close()

    rec = DataRecorder(db)
    rec.close()

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = dict(
        conn.execute(
            "SELECT * FROM samples_hourly WHERE hour_ts = '2026-07-01T10:00:00+00:00'"
        ).fetchone()
    )
    conn.close()
    assert row["house_load_kwh_sum"] == pytest.approx(1.5)
    assert row["pv_kwh_sum"] == pytest.approx(0.3)
    assert row["grid_import_kwh_sum"] is None
    assert row["grid_export_kwh_sum"] is None
    assert row["batt_charge_kwh_sum"] is None
    assert row["batt_discharge_kwh_sum"] is None


# ---------------------------------------------------------------------------
# aggregate_hour() kWh SUM pass
# ---------------------------------------------------------------------------

def test_aggregate_hour_kwh_sums():
    """Tier 1: sum of per-tick *_kwh values when all ticks are non-NULL."""
    rows = [
        {
            "ts": "2026-07-06T10:00:00+00:00",
            "grid_import_kwh": 0.02, "grid_export_kwh": 0.0,
            "house_load_kwh": 0.01, "pv_kwh": 0.03,
            "batt_charge_kwh": 0.0, "batt_discharge_kwh": 0.005,
        },
        {
            "ts": "2026-07-06T10:01:00+00:00",
            "grid_import_kwh": 0.015, "grid_export_kwh": 0.0,
            "house_load_kwh": 0.012, "pv_kwh": 0.028,
            "batt_charge_kwh": 0.0, "batt_discharge_kwh": 0.004,
        },
    ]
    result = aggregate_hour(rows)
    assert result["grid_import_kwh_sum"] == pytest.approx(0.035)
    assert result["grid_export_kwh_sum"] == pytest.approx(0.0)
    assert result["house_load_kwh_sum"] == pytest.approx(0.022)
    assert result["pv_kwh_sum"] == pytest.approx(0.058)
    assert result["batt_charge_kwh_sum"] == pytest.approx(0.0)
    assert result["batt_discharge_kwh_sum"] == pytest.approx(0.009)


def test_aggregate_hour_kwh_sums_all_null_falls_back_to_mean():
    """Tier 2: every tick NULL for a *_kwh column (pre-v9 data) → mean_watts
    × 1h / 1000 approximation, reusing the already-computed stats."""
    rows = [
        {"ts": "2026-07-06T10:00:00+00:00", "p1_w": 1000.0, "batt_w": 0.0,
         "pv_w": 500.0, "load_w": 1500.0},
        {"ts": "2026-07-06T10:30:00+00:00", "p1_w": 2000.0, "batt_w": 0.0,
         "pv_w": 300.0, "load_w": 2000.0},
    ]
    # No *_kwh columns present at all → tier 2 for every column.
    result = aggregate_hour(rows)
    assert result["house_load_mean"] == pytest.approx(1750.0)
    assert result["house_load_kwh_sum"] == pytest.approx(1750.0 / 1000.0)
    assert result["pv_w_mean"] == pytest.approx(400.0)
    assert result["pv_kwh_sum"] == pytest.approx(400.0 / 1000.0)


def test_aggregate_hour_kwh_sums_partial_null_scales_by_coverage():
    """NULL ticks within recorded rows are GAPS, not zero energy. 2 non-NULL
    (0.02+0.03) of 3 rows → 0.05 * 3/2 = 0.075."""
    rows = [
        {"ts": "2026-07-06T10:00:00+00:00", "pv_kwh": 0.02},
        {"ts": "2026-07-06T10:01:00+00:00", "pv_kwh": None},
        {"ts": "2026-07-06T10:02:00+00:00", "pv_kwh": 0.03},
    ]
    result = aggregate_hour(rows)
    assert result["pv_kwh_sum"] == pytest.approx(0.075)


def test_aggregate_hour_kwh_sums_sparse_coverage_no_undercount():
    """6 non-NULL ticks of 60 rows → scaled ×10, not ~90% undercount."""
    rows = [{"ts": f"2026-07-06T10:{m:02d}:00+00:00",
             "pv_kwh": (0.01 if m < 6 else None)} for m in range(60)]
    result = aggregate_hour(rows)
    assert result["pv_kwh_sum"] == pytest.approx(0.06 * 60 / 6)   # 0.6


def test_aggregate_hour_kwh_sums_grid_batt_sign_split_fallback():
    """Tier 2 for grid/batt: sign-split mean of p1_w/batt_w computed inline
    (import=+p1, export=-p1; discharge=+batt, charge=-batt)."""
    rows = [
        {"ts": "2026-07-06T10:00:00+00:00", "p1_w": -600.0, "batt_w": -400.0},
        {"ts": "2026-07-06T10:30:00+00:00", "p1_w": -400.0, "batt_w": -800.0},
    ]
    # p1_mean = -500 (net export); batt_mean = -600 (net charge).
    result = aggregate_hour(rows)
    assert result["grid_import_kwh_sum"] == pytest.approx(0.0)
    assert result["grid_export_kwh_sum"] == pytest.approx(500.0 / 1000.0)
    assert result["batt_charge_kwh_sum"] == pytest.approx(600.0 / 1000.0)
    assert result["batt_discharge_kwh_sum"] == pytest.approx(0.0)


def test_aggregate_hour_kwh_sums_all_none_when_no_data():
    """No *_kwh ticks and no fallback power readings → sums stay None."""
    rows = [{"ts": "2026-07-06T10:00:00+00:00"}]
    result = aggregate_hour(rows)
    for col in KWH_SUM_COLS:
        assert result[col] is None
