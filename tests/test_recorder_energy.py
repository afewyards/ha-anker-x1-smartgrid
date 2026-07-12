import sqlite3
import pytest
from custom_components.anker_x1_smartgrid.recorder import DataRecorder, _VERSION_TARGET

ENERGY_COLS = {
    "grid_import_kwh",
    "grid_export_kwh",
    "house_load_kwh",
    "pv_kwh",
    "batt_charge_kwh",
    "batt_discharge_kwh",
}


def _samples_columns(db_path):
    conn = sqlite3.connect(db_path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(samples)")}
    conn.close()
    return cols


# ---------------------------------------------------------------------------
# E1 — v9 migration
# ---------------------------------------------------------------------------


def test_v9_fresh_db_has_energy_columns_and_version(tmp_path):
    db = str(tmp_path / "t.db")
    rec = DataRecorder(db)
    rec.close()
    assert _samples_columns(db) >= ENERGY_COLS
    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == _VERSION_TARGET
    conn.close()


def test_v9_crash_idempotent_rerun(tmp_path):
    """Crash between ALTERs and version bump: columns exist but user_version
    is still 8 — re-running the ladder must be a safe no-op that lands on
    _VERSION_TARGET (later migration steps, e.g. v10, still apply on top)."""
    db = str(tmp_path / "t.db")
    DataRecorder(db).close()
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA user_version = 8")
    conn.commit()
    conn.close()
    rec = DataRecorder(db)  # must not raise "duplicate column name"
    rec.close()
    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == _VERSION_TARGET
    conn.close()


# ---------------------------------------------------------------------------
# E2 — append() energy deltas
# ---------------------------------------------------------------------------


def _last_row(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    r = conn.execute("SELECT * FROM samples ORDER BY rowid DESC LIMIT 1").fetchone()
    conn.close()
    return dict(r)


def _row_count(db_path):
    conn = sqlite3.connect(db_path)
    n = conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
    conn.close()
    return n


def test_first_append_energy_all_null(tmp_path):
    db = str(tmp_path / "t.db")
    rec = DataRecorder(db)
    rec.append({"ts": "2026-07-06T10:00:00+00:00", "pv_w": 1000.0, "p1_w": 200.0})
    rec.close()
    row = _last_row(db)
    for col in ENERGY_COLS:
        assert row[col] is None


def test_sign_splits_and_rectangle_rule(tmp_path):
    """60 s tick: pv 1200 W → 0.02 kWh; p1 −500 W → export 0.008333/import 0.0;
    batt +800 W (discharge +) → discharge 0.013333/charge 0.0; load 300 W → 0.005."""
    db = str(tmp_path / "t.db")
    rec = DataRecorder(db)
    rec.append({"ts": "2026-07-06T10:00:00+00:00", "pv_w": 0.0})
    rec.append({"ts": "2026-07-06T10:01:00+00:00", "pv_w": 1200.0, "p1_w": -500.0, "batt_w": 800.0, "load_w": 300.0})
    rec.close()
    row = _last_row(db)
    dt_h = 60.0 / 3600.0
    assert row["pv_kwh"] == pytest.approx(1.2 * dt_h)
    assert row["grid_export_kwh"] == pytest.approx(0.5 * dt_h)
    assert row["grid_import_kwh"] == 0.0
    assert row["batt_discharge_kwh"] == pytest.approx(0.8 * dt_h)
    assert row["batt_charge_kwh"] == 0.0
    assert row["house_load_kwh"] == pytest.approx(0.3 * dt_h)


def test_import_side_split(tmp_path):
    """p1 +2000 W (import), batt −1500 W (charging) → import/charge positive."""
    db = str(tmp_path / "t.db")
    rec = DataRecorder(db)
    rec.append({"ts": "2026-07-06T10:00:00+00:00"})
    rec.append({"ts": "2026-07-06T10:01:00+00:00", "p1_w": 2000.0, "batt_w": -1500.0})
    rec.close()
    row = _last_row(db)
    dt_h = 60.0 / 3600.0
    assert row["grid_import_kwh"] == pytest.approx(2.0 * dt_h)
    assert row["grid_export_kwh"] == 0.0
    assert row["batt_charge_kwh"] == pytest.approx(1.5 * dt_h)
    assert row["batt_discharge_kwh"] == 0.0


def test_none_reading_nulls_only_that_flow(tmp_path):
    db = str(tmp_path / "t.db")
    rec = DataRecorder(db)
    rec.append({"ts": "2026-07-06T10:00:00+00:00"})
    rec.append({"ts": "2026-07-06T10:01:00+00:00", "p1_w": None, "pv_w": 600.0})
    rec.close()
    row = _last_row(db)
    assert row["grid_import_kwh"] is None
    assert row["grid_export_kwh"] is None
    assert row["pv_kwh"] == pytest.approx(0.6 * 60.0 / 3600.0)
    assert row["house_load_kwh"] is None  # load_w absent → None
    assert row["batt_charge_kwh"] is None  # batt_w absent → None


def test_gap_clamps_to_120s(tmp_path):
    """1 h gap (restart/stall) credits at most 120 s of energy — honest undercount."""
    db = str(tmp_path / "t.db")
    rec = DataRecorder(db)
    rec.append({"ts": "2026-07-06T10:00:00+00:00"})
    rec.append({"ts": "2026-07-06T11:00:00+00:00", "pv_w": 1200.0})
    rec.close()
    row = _last_row(db)
    assert row["pv_kwh"] == pytest.approx(1.2 * 120.0 / 3600.0)


def test_out_of_order_ts_clamps_to_zero(tmp_path):
    db = str(tmp_path / "t.db")
    rec = DataRecorder(db)
    rec.append({"ts": "2026-07-06T10:02:00+00:00"})
    rec.append({"ts": "2026-07-06T10:01:00+00:00", "pv_w": 1200.0})
    rec.close()
    row = _last_row(db)
    assert row["pv_kwh"] == 0.0


def test_out_of_order_ts_does_not_overcount_next_row(tmp_path):
    """Out-of-order row must not rewind _last_sample_ts — the follow-on row
    should see a normal 60 s dt, not an inflated one."""
    db = str(tmp_path / "t.db")
    rec = DataRecorder(db)
    rec.append({"ts": "2026-07-06T10:00:00+00:00"})  # seed
    rec.append({"ts": "2026-07-06T10:02:00+00:00", "pv_w": 1000.0})  # normal
    rec.append({"ts": "2026-07-06T10:01:00+00:00", "pv_w": 1000.0})  # out-of-order
    rec.append({"ts": "2026-07-06T10:03:00+00:00", "pv_w": 1000.0})  # follow-on
    rec.close()
    row = _last_row(db)
    dt_h = 60.0 / 3600.0  # 10:03 − 10:02 = 60 s, not 120 s
    assert row["pv_kwh"] == pytest.approx(1.0 * dt_h)


def test_malformed_ts_row_still_inserts_with_null_energy(tmp_path):
    """Failure isolation: energy compute must never abort the telemetry INSERT."""
    db = str(tmp_path / "t.db")
    rec = DataRecorder(db)
    rec.append({"ts": "2026-07-06T10:00:00+00:00", "pv_w": 500.0})
    rec.append({"ts": "not-a-date", "pv_w": 500.0})  # must not raise
    rec.close()
    assert _row_count(db) == 2
    row = _last_row(db)
    for col in ENERGY_COLS:
        assert row[col] is None
