import sqlite3
import pytest
from datetime import datetime, timezone
from custom_components.anker_x1_smartgrid.recorder import DataRecorder, _VERSION_TARGET


# ---------------------------------------------------------------------------
# R1 — read_load_samples: load_w-first with derive fallback
# ---------------------------------------------------------------------------

def test_read_load_samples_prefers_load_w(tmp_path):
    """Row with load_w=150, p1_w=99 → returns (ts, 150.0) — load_w wins."""
    rec = DataRecorder(str(tmp_path / "t.db"))
    rec.append({"ts": "2026-06-24T10:00:00+00:00", "p1_w": 99.0, "batt_w": 0.0, "pv_w": 0.0, "load_w": 150.0})
    rows = rec.read_load_samples()
    assert len(rows) == 1
    assert rows[0] == ("2026-06-24T10:00:00+00:00", 150.0)
    rec.close()


def test_read_load_samples_falls_back_to_derive_when_load_w_null(tmp_path):
    """Row with load_w=NULL, p1_w=200, batt_w=50, pv_w=0 → returns (ts, 250.0) via derive."""
    rec = DataRecorder(str(tmp_path / "t.db"))
    rec.append({"ts": "2026-06-24T10:00:00+00:00", "p1_w": 200.0, "batt_w": 50.0, "pv_w": 0.0, "load_w": None})
    rows = rec.read_load_samples()
    assert len(rows) == 1
    assert rows[0] == ("2026-06-24T10:00:00+00:00", 250.0)
    rec.close()


def test_read_load_samples_excludes_when_both_load_w_and_p1_null(tmp_path):
    """Row with load_w=NULL and p1_w=NULL → excluded (no valid load value)."""
    rec = DataRecorder(str(tmp_path / "t.db"))
    rec.append({"ts": "2026-06-24T10:00:00+00:00", "p1_w": None, "batt_w": 50.0, "pv_w": 100.0, "load_w": None})
    rows = rec.read_load_samples()
    assert rows == []
    rec.close()


def test_read_load_samples_since_filter(tmp_path):
    """since_iso filter excludes rows before the cutoff (works for both load_w and derive paths)."""
    rec = DataRecorder(str(tmp_path / "t.db"))
    rec.append({"ts": "2026-06-10T00:00:00+00:00", "load_w": 100.0})
    rec.append({"ts": "2026-06-18T00:00:00+00:00", "p1_w": 200.0, "batt_w": 0.0, "pv_w": 0.0})  # derive path
    rec.append({"ts": "2026-06-20T00:00:00+00:00", "load_w": 300.0})
    rows = rec.read_load_samples(since_iso="2026-06-15T00:00:00+00:00")
    assert len(rows) == 2
    assert rows[0][1] == 200.0  # derived from p1_w
    assert rows[1][1] == 300.0
    rec.close()


def test_read_load_samples_empty_when_no_data(tmp_path):
    rec = DataRecorder(str(tmp_path / "t.db"))
    assert rec.read_load_samples() == []
    rec.close()


# ---------------------------------------------------------------------------
# R8 — read_persons_home_samples: mirrors read_load_samples (P8)
# ---------------------------------------------------------------------------

def test_read_persons_home_samples_returns_non_null_rows(tmp_path):
    rec = DataRecorder(str(tmp_path / "t.db"))
    rec.append({"ts": "2026-06-24T10:00:00+00:00", "persons_home": 2.0})
    rec.append({"ts": "2026-06-24T11:00:00+00:00", "persons_home": None})
    rows = rec.read_persons_home_samples()
    assert rows == [("2026-06-24T10:00:00+00:00", 2.0)]
    rec.close()


def test_read_persons_home_samples_since_filter(tmp_path):
    rec = DataRecorder(str(tmp_path / "t.db"))
    rec.append({"ts": "2026-06-10T00:00:00+00:00", "persons_home": 1.0})
    rec.append({"ts": "2026-06-20T00:00:00+00:00", "persons_home": 3.0})
    rows = rec.read_persons_home_samples(since_iso="2026-06-15T00:00:00+00:00")
    assert rows == [("2026-06-20T00:00:00+00:00", 3.0)]
    rec.close()


def test_read_persons_home_samples_empty_when_no_data(tmp_path):
    rec = DataRecorder(str(tmp_path / "t.db"))
    assert rec.read_persons_home_samples() == []
    rec.close()


def test_append_and_count(tmp_path):
    rec = DataRecorder(str(tmp_path / "t.db"))
    rec.append({"ts": "2026-06-20T12:00:00+00:00", "soc": 50.0, "state": "passive"})
    rec.append({"ts": "2026-06-20T12:01:00+00:00", "soc": 51.0, "state": "forcing"})
    assert rec.count() == 2
    rec.close()


def test_append_missing_keys_ok(tmp_path):
    rec = DataRecorder(str(tmp_path / "t.db"))
    rec.append({"ts": "2026-06-20T12:00:00+00:00"})
    assert rec.count() == 1
    rec.close()


def test_purge_old_rows(tmp_path):
    rec = DataRecorder(str(tmp_path / "t.db"))
    old = (datetime(2026, 1, 1, tzinfo=timezone.utc)).isoformat()
    new = (datetime(2026, 6, 20, tzinfo=timezone.utc)).isoformat()
    rec.append({"ts": old})
    rec.append({"ts": new})
    now = datetime(2026, 6, 21, tzinfo=timezone.utc).isoformat()
    deleted = rec.purge_older_than(now, retention_days=90)
    assert deleted == 1
    assert rec.count() == 1
    rec.close()


# ---------------------------------------------------------------------------
# decisions table — migration
# ---------------------------------------------------------------------------

def _make_old_db(path: str) -> None:
    """Create a DB that looks like pre-v1: only the samples table, user_version=0."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS samples ("
        "ts TEXT, hour INTEGER, weekday INTEGER, soc REAL, pv_w REAL, "
        "batt_w REAL, p1_w REAL, p1_l1 REAL, p1_l2 REAL, p1_l3 REAL, "
        "import_price REAL, export_price REAL, temp REAL, irradiance REAL, "
        "state TEXT, setpoint_w REAL, deficit_kwh REAL)"
    )
    conn.execute(
        "INSERT INTO samples (ts, soc, state) VALUES (?, ?, ?)",
        ("2026-01-01T00:00:00+00:00", 42.0, "passive"),
    )
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    conn.close()


def test_migration_creates_decisions_table(tmp_path):
    """Opening an old DB (samples-only, user_version=0) must add decisions table."""
    db_path = str(tmp_path / "old.db")
    _make_old_db(db_path)

    rec = DataRecorder(db_path)
    rec.close()

    # Re-open with plain sqlite3 to inspect schema.
    conn = sqlite3.connect(db_path)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    indexes = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    # Original sample row must be untouched.
    sample_count = conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
    conn.close()

    assert "decisions" in tables, "decisions table must exist after migration"
    assert "idx_decisions_ts" in indexes, "decisions ts index must exist"
    assert version == _VERSION_TARGET
    assert sample_count == 1, "existing samples rows must survive migration"


def test_migration_is_idempotent(tmp_path):
    """Running migration twice (open DB twice) must not error or change version."""
    db_path = str(tmp_path / "t.db")
    DataRecorder(db_path).close()
    DataRecorder(db_path).close()

    conn = sqlite3.connect(db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()

    assert version == _VERSION_TARGET


# ---------------------------------------------------------------------------
# decisions table — append + read round-trip
# ---------------------------------------------------------------------------

_SAMPLE_DECISION = dict(
    ts="2026-06-21T10:00:00+00:00",
    active=True,
    start_soc=55.5,
    deadline="2026-06-21T18:00:00+00:00",
    committed_hours=["2026-06-21T11:00:00+00:00", "2026-06-21T12:00:00+00:00"],
    horizon_mode="today",
    pv_today_forecast_kwh=8.1,
    pv_tomorrow_forecast_kwh=6.4,
    predicted_load_json='[1.5, 1.6, 1.4]',
    price_window_json='{"start": "2026-06-21T10:00:00+00:00"}',
    setpoint_w=3600.0,
    state="forcing",
)


def _decisions_rows(db_path: str) -> list[sqlite3.Row]:
    """Read all decisions rows directly via SQL, ordered by ts.

    Bypasses DataRecorder — ``read_decisions`` was removed as dead production
    code in Task 13 (zero live callers). These tests verify the write path
    (append_decision / purge_decisions_older_than) persists correctly by
    querying the table directly instead.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM decisions ORDER BY ts ASC").fetchall()
    conn.close()
    return rows


def test_append_decision_active_false_stores_zero(tmp_path):
    """active=False must be stored as 0."""
    db_path = str(tmp_path / "t.db")
    rec = DataRecorder(db_path)
    d = dict(_SAMPLE_DECISION, active=False, ts="2026-06-21T10:00:00+00:00")
    rec.append_decision(**d)  # type: ignore[arg-type]
    rec.close()

    rows = _decisions_rows(db_path)
    assert rows[0]["active"] == 0


# ---------------------------------------------------------------------------
# decisions table — purge
# ---------------------------------------------------------------------------

def test_purge_decisions_older_than(tmp_path):
    """purge_decisions_older_than removes rows with ts < cutoff, keeps newer."""
    db_path = str(tmp_path / "t.db")
    rec = DataRecorder(db_path)
    for hour in [8, 9, 10, 11]:
        rec.append_decision(**dict(  # type: ignore[arg-type]
            _SAMPLE_DECISION,
            ts=f"2026-06-21T{hour:02d}:00:00+00:00",
        ))

    deleted = rec.purge_decisions_older_than("2026-06-21T10:00:00+00:00")
    rec.close()

    remaining = _decisions_rows(db_path)
    assert deleted == 2, "hours 08 and 09 must be removed"
    assert len(remaining) == 2
    assert remaining[0]["ts"] == "2026-06-21T10:00:00+00:00"
    assert remaining[1]["ts"] == "2026-06-21T11:00:00+00:00"


def test_purge_decisions_returns_zero_when_nothing_to_delete(tmp_path):
    """Purge with a cutoff before all rows must return 0."""
    rec = DataRecorder(str(tmp_path / "t.db"))
    rec.append_decision(**dict(  # type: ignore[arg-type]
        _SAMPLE_DECISION, ts="2026-06-21T12:00:00+00:00"
    ))
    deleted = rec.purge_decisions_older_than("2026-06-01T00:00:00+00:00")
    rec.close()

    assert deleted == 0


def test_existing_row_survives_reopen(tmp_path):
    """Re-opening (re-running migration) must not destroy existing decision rows."""
    db_path = str(tmp_path / "t.db")

    rec = DataRecorder(db_path)
    rec.append_decision(**_SAMPLE_DECISION)  # type: ignore[arg-type]
    rec.close()

    # Reopen — migration runs again (CREATE TABLE IF NOT EXISTS path).
    rec2 = DataRecorder(db_path)
    rec2.close()

    rows = _decisions_rows(db_path)
    assert len(rows) == 1, "decision row must survive reopen"
    assert rows[0]["ts"] == _SAMPLE_DECISION["ts"]

    # Confirm user_version is still at _VERSION_TARGET (not re-bumped or zeroed).
    conn = sqlite3.connect(db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    assert version == _VERSION_TARGET


# ---------------------------------------------------------------------------
# daily_regret table — migration v1 → v2
# ---------------------------------------------------------------------------

def _make_v1_db(path: str) -> None:
    """Create a DB at user_version=1 (samples + decisions, no daily_regret)."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS samples ("
        "ts TEXT, hour INTEGER, weekday INTEGER, soc REAL, pv_w REAL, "
        "batt_w REAL, p1_w REAL, p1_l1 REAL, p1_l2 REAL, p1_l3 REAL, "
        "import_price REAL, export_price REAL, temp REAL, irradiance REAL, "
        "state TEXT, setpoint_w REAL, deficit_kwh REAL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS decisions ("
        "ts TEXT, active INTEGER, start_soc REAL, deficit_kwh REAL, "
        "deadline TEXT, committed_hours TEXT, horizon_mode TEXT, "
        "pv_today_forecast_kwh REAL, pv_tomorrow_forecast_kwh REAL, "
        "predicted_load_json TEXT, price_window_json TEXT, "
        "setpoint_w REAL, state TEXT)"
    )
    conn.execute(
        "INSERT INTO decisions (ts, active, start_soc, deficit_kwh, state, committed_hours) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("2026-01-01T00:00:00+00:00", 1, 50.0, 1.0, "forcing", "[]"),
    )
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()


def test_migration_v1_to_v2_creates_daily_regret_table(tmp_path):
    """Opening a v1 DB must add the daily_regret table and bump user_version to 2."""
    db_path = str(tmp_path / "v1.db")
    _make_v1_db(db_path)

    rec = DataRecorder(db_path)
    rec.close()

    conn = sqlite3.connect(db_path)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    # Existing decision row must survive.
    decision_count = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    conn.close()

    assert "daily_regret" in tables, "daily_regret table must exist after v1→v2 migration"
    assert version == _VERSION_TARGET  # must equal 3 after migration
    assert decision_count == 1, "existing decision rows must survive migration"


# ---------------------------------------------------------------------------
# daily_regret table — upsert + read round-trips
# ---------------------------------------------------------------------------

_SAMPLE_REGRET = dict(
    day="2026-06-20",
    regret_eur=0.42,
    over_buy_kwh=1.5,
    over_buy_eur=0.15,
    under_buy_kwh=0.0,
    cost_regret_eur=0.27,
    optimal_kwh=3.0,
    optimal_eur=1.00,
    realized_kwh=4.5,
    realized_eur=1.42,
    infeasible=0,
    computed_ts="2026-06-21T00:01:00+00:00",
)


def test_upsert_and_read_daily_regret_round_trip(tmp_path):
    """upsert_daily_regret + read_latest + read_range all round-trip correctly."""
    rec = DataRecorder(str(tmp_path / "t.db"))
    rec.upsert_daily_regret(**_SAMPLE_REGRET)  # type: ignore[arg-type]
    rec.close()

    rec2 = DataRecorder(str(tmp_path / "t.db"))
    row = rec2.read_latest_daily_regret()
    assert row is not None
    assert row["day"] == "2026-06-20"
    assert row["regret_eur"] == pytest.approx(0.42)
    assert row["over_buy_kwh"] == pytest.approx(1.5)
    assert row["under_buy_kwh"] == pytest.approx(0.0)
    assert row["cost_regret_eur"] == pytest.approx(0.27)
    assert row["optimal_kwh"] == pytest.approx(3.0)
    assert row["optimal_eur"] == pytest.approx(1.00)
    assert row["realized_kwh"] == pytest.approx(4.5)
    assert row["realized_eur"] == pytest.approx(1.42)
    assert row["infeasible"] == 0
    assert row["computed_ts"] == "2026-06-21T00:01:00+00:00"

    rows = rec2.read_daily_regret_range("2026-06-20", "2026-06-21")
    assert len(rows) == 1
    assert rows[0]["day"] == "2026-06-20"
    rec2.close()


def test_upsert_daily_regret_replaces_existing(tmp_path):
    """Upserting twice for the same day keeps only the latest values."""
    rec = DataRecorder(str(tmp_path / "t.db"))
    rec.upsert_daily_regret(**_SAMPLE_REGRET)  # type: ignore[arg-type]
    rec.upsert_daily_regret(
        day="2026-06-20",
        regret_eur=0.10, over_buy_kwh=0.1, over_buy_eur=0.01,
        under_buy_kwh=0.0, cost_regret_eur=0.09,
        optimal_kwh=3.0, optimal_eur=2.00,
        realized_kwh=3.1, realized_eur=2.10,
        infeasible=0,
        computed_ts="2026-06-21T00:05:00+00:00",
    )
    row = rec.read_latest_daily_regret()
    rec.close()

    assert row is not None
    assert row["regret_eur"] == pytest.approx(0.10), "second upsert must overwrite first"
    assert row["realized_eur"] == pytest.approx(2.10)


def test_upsert_daily_regret_infeasible_stores_null_metrics(tmp_path):
    """infeasible=1 row stores NULL for all metric columns."""
    rec = DataRecorder(str(tmp_path / "t.db"))
    rec.upsert_daily_regret(
        day="2026-06-20",
        regret_eur=None, over_buy_kwh=None, over_buy_eur=None,
        under_buy_kwh=None, cost_regret_eur=None,
        optimal_kwh=None, optimal_eur=None,
        realized_kwh=None, realized_eur=None,
        infeasible=1,
        computed_ts="2026-06-21T00:01:00+00:00",
    )
    row = rec.read_latest_daily_regret()
    rec.close()

    assert row is not None
    assert row["infeasible"] == 1
    assert row["regret_eur"] is None
    assert row["over_buy_kwh"] is None
    assert row["under_buy_kwh"] is None


def test_read_latest_daily_regret_returns_none_when_empty(tmp_path):
    rec = DataRecorder(str(tmp_path / "t.db"))
    row = rec.read_latest_daily_regret()
    rec.close()
    assert row is None


def test_read_daily_regret_range_respects_window(tmp_path):
    """read_daily_regret_range returns only rows within [since_day, until_day)."""
    rec = DataRecorder(str(tmp_path / "t.db"))
    for day in ["2026-06-18", "2026-06-19", "2026-06-20", "2026-06-21"]:
        rec.upsert_daily_regret(
            day=day, regret_eur=0.1, over_buy_kwh=0.0, over_buy_eur=0.0,
            under_buy_kwh=0.0, cost_regret_eur=0.1,
            optimal_kwh=1.0, optimal_eur=1.0,
            realized_kwh=1.1, realized_eur=1.1,
            infeasible=0,
            computed_ts="2026-06-22T00:00:00+00:00",
        )
    rows = rec.read_daily_regret_range("2026-06-19", "2026-06-21")
    rec.close()

    assert len(rows) == 2
    assert rows[0]["day"] == "2026-06-19"
    assert rows[1]["day"] == "2026-06-20"


# ---------------------------------------------------------------------------
# weather-forecast columns — migration v2 → v3
# ---------------------------------------------------------------------------

_WEATHER_COLS = ("temp_forecast", "cloud_cover", "humidity", "wind_speed")


def _make_v2_db(path: str) -> None:
    """Create a DB at user_version=2 (samples + decisions + daily_regret, no weather cols)."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS samples ("
        "ts TEXT, hour INTEGER, weekday INTEGER, soc REAL, pv_w REAL, "
        "batt_w REAL, p1_w REAL, p1_l1 REAL, p1_l2 REAL, p1_l3 REAL, "
        "import_price REAL, export_price REAL, temp REAL, irradiance REAL, "
        "state TEXT, setpoint_w REAL, deficit_kwh REAL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS decisions ("
        "ts TEXT, active INTEGER, start_soc REAL, deficit_kwh REAL, "
        "deadline TEXT, committed_hours TEXT, horizon_mode TEXT, "
        "pv_today_forecast_kwh REAL, pv_tomorrow_forecast_kwh REAL, "
        "predicted_load_json TEXT, price_window_json TEXT, "
        "setpoint_w REAL, state TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS daily_regret ("
        "day TEXT PRIMARY KEY, regret_eur REAL, over_buy_kwh REAL, over_buy_eur REAL, "
        "under_buy_kwh REAL, cost_regret_eur REAL, optimal_kwh REAL, optimal_eur REAL, "
        "realized_kwh REAL, realized_eur REAL, infeasible INTEGER, computed_ts TEXT)"
    )
    # Insert a sample row (no weather cols) to verify survival after migration.
    conn.execute(
        "INSERT INTO samples (ts, soc, temp, state) VALUES (?, ?, ?, ?)",
        ("2026-05-01T12:00:00+00:00", 60.0, 18.5, "passive"),
    )
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()


def test_fresh_db_is_at_version_target_with_weather_columns(tmp_path):
    """A freshly-initialised DB ends at _VERSION_TARGET and has all 4 weather cols."""
    rec = DataRecorder(str(tmp_path / "fresh.db"))
    rec.close()

    conn = sqlite3.connect(str(tmp_path / "fresh.db"))
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    col_names = {row[1] for row in conn.execute("PRAGMA table_info(samples)").fetchall()}
    conn.close()

    assert version == _VERSION_TARGET
    for col in _WEATHER_COLS:
        assert col in col_names, f"expected column '{col}' in samples"


def test_migration_v2_to_v3_adds_weather_columns_and_preserves_rows(tmp_path):
    """Opening a v2 DB must add 4 weather cols, reach _VERSION_TARGET, and leave rows intact."""
    db_path = str(tmp_path / "v2.db")
    _make_v2_db(db_path)

    rec = DataRecorder(db_path)
    rec.close()

    conn = sqlite3.connect(db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    col_names = {row[1] for row in conn.execute("PRAGMA table_info(samples)").fetchall()}
    sample_count = conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
    # Pre-existing row should have NULL for all new weather cols.
    row = conn.execute(
        "SELECT temp_forecast, cloud_cover, humidity, wind_speed FROM samples"
    ).fetchone()
    conn.close()

    assert version == _VERSION_TARGET
    for col in _WEATHER_COLS:
        assert col in col_names, f"expected column '{col}' in samples after v2→v3 migration"
    assert sample_count == 1, "pre-existing sample row must survive migration"
    assert row == (None, None, None, None), "new weather cols must be NULL for old rows"


def test_migration_v2_to_v3_is_idempotent(tmp_path):
    """Running migration twice on a v2 DB must not error and must stay at _VERSION_TARGET."""
    db_path = str(tmp_path / "v2.db")
    _make_v2_db(db_path)

    DataRecorder(db_path).close()   # first open: v2 → v3 → v4
    DataRecorder(db_path).close()   # second open: already at target, no-op

    conn = sqlite3.connect(db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    col_names = {row[1] for row in conn.execute("PRAGMA table_info(samples)").fetchall()}
    conn.close()

    assert version == _VERSION_TARGET
    for col in _WEATHER_COLS:
        assert col in col_names


def test_migration_v2_to_v3_recovers_from_partial_alter(tmp_path):
    """Crash between ALTERs (some cols added, user_version still 2) must recover."""
    db_path = str(tmp_path / "v2_partial.db")
    _make_v2_db(db_path)

    # Simulate a crash mid-step: one column was added but user_version stayed at 2.
    conn = sqlite3.connect(db_path)
    conn.execute("ALTER TABLE samples ADD COLUMN temp_forecast REAL")
    conn.commit()  # user_version intentionally left at 2
    conn.close()

    # Must NOT raise duplicate column name — the guard skips already-present cols.
    DataRecorder(db_path).close()

    conn = sqlite3.connect(db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    col_names = {r[1] for r in conn.execute("PRAGMA table_info(samples)").fetchall()}
    conn.close()

    assert version == _VERSION_TARGET
    for col in _WEATHER_COLS:
        assert col in col_names


# ---------------------------------------------------------------------------
# samples_hourly table — migration v3 → v4
# ---------------------------------------------------------------------------

def _make_v3_db(path: str) -> None:
    """Create a DB at user_version=3 (samples with weather cols, no samples_hourly)."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS samples ("
        "ts TEXT, hour INTEGER, weekday INTEGER, soc REAL, pv_w REAL, "
        "batt_w REAL, p1_w REAL, p1_l1 REAL, p1_l2 REAL, p1_l3 REAL, "
        "import_price REAL, export_price REAL, temp REAL, irradiance REAL, "
        "state TEXT, setpoint_w REAL, deficit_kwh REAL, "
        "temp_forecast REAL, cloud_cover REAL, humidity REAL, wind_speed REAL)"
    )
    conn.execute(
        "INSERT INTO samples (ts, soc, temp, state) VALUES (?, ?, ?, ?)",
        ("2026-05-01T12:00:00+00:00", 60.0, 18.5, "passive"),
    )
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    conn.close()


def test_migration_v3_to_v4_creates_samples_hourly(tmp_path):
    """Opening a v3 DB must create samples_hourly and reach _VERSION_TARGET."""
    db_path = str(tmp_path / "v3.db")
    _make_v3_db(db_path)

    rec = DataRecorder(db_path)
    rec.close()

    conn = sqlite3.connect(db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    sample_count = conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
    conn.close()

    assert version == _VERSION_TARGET
    assert "samples_hourly" in tables, "samples_hourly must exist after v3→v4 migration"
    assert sample_count == 1, "existing sample row must survive v3→v4 migration"


def test_migration_v3_to_v4_is_idempotent(tmp_path):
    """Re-opening a v4 DB must not error and must stay at _VERSION_TARGET."""
    db_path = str(tmp_path / "v3.db")
    _make_v3_db(db_path)

    DataRecorder(db_path).close()   # first open: v3 → v4
    DataRecorder(db_path).close()   # second open: already at target, no-op

    conn = sqlite3.connect(db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    conn.close()

    assert version == _VERSION_TARGET
    assert "samples_hourly" in tables


def test_fresh_db_has_samples_hourly(tmp_path):
    """A freshly-initialised DB has the samples_hourly table."""
    rec = DataRecorder(str(tmp_path / "fresh.db"))
    rec.close()

    conn = sqlite3.connect(str(tmp_path / "fresh.db"))
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    conn.close()

    assert "samples_hourly" in tables


# ---------------------------------------------------------------------------
# rollup_hours — functional tests
# ---------------------------------------------------------------------------

def test_rollup_hours_rolls_up_completed_hour(tmp_path):
    """rollup_hours aggregates a completed clock-hour and returns count=1."""
    rec = DataRecorder(str(tmp_path / "t.db"))
    # Two samples in hour 10 (completed), one in hour 11 (in-progress).
    rec.append({"ts": "2026-06-20T10:10:00+00:00", "p1_w": 1000.0, "batt_w": 0.0, "soc": 50.0})
    rec.append({"ts": "2026-06-20T10:50:00+00:00", "p1_w": 2000.0, "batt_w": 0.0, "soc": 55.0})
    rec.append({"ts": "2026-06-20T11:10:00+00:00", "p1_w": 3000.0, "batt_w": 0.0, "soc": 60.0})

    # now is within hour 11 → only hour 10 is completed
    rolled = rec.rollup_hours("2026-06-20T11:30:00+00:00")
    rec.close()

    assert rolled == 1, "exactly one completed hour must be rolled up"


def test_rollup_hours_skips_in_progress_hour(tmp_path):
    """The in-progress current hour must NOT appear in samples_hourly."""
    rec = DataRecorder(str(tmp_path / "t.db"))
    rec.append({"ts": "2026-06-20T11:10:00+00:00", "p1_w": 3000.0, "batt_w": 0.0})

    rec.rollup_hours("2026-06-20T11:30:00+00:00")
    hourly = rec.read_hourly_rows()
    rec.close()

    assert len(hourly) == 0, "in-progress hour must not be rolled up"


def test_rollup_hours_microsecond_ts_at_boundary_stays_in_progress(tmp_path):
    """Regression: a raw ts with microseconds at the exact hour boundary is in-progress.

    Real recorded ts values carry microseconds (e.g. "...T11:00:00.123456+00:00").
    The WHERE filter ``ts < current_hour_iso`` compares these as ISO strings.
    ASCII '.' (0x2E) > '+' (0x2B), so "T11:00:00.123456+00:00" sorts AFTER the
    microsecond-free current_hour_iso "T11:00:00+00:00" — the row is correctly
    held back as in-progress.  This test locks in that byte-ordering assumption
    so any future format drift is caught immediately.

    Would fail if the filter were changed to ``<=`` or the hour-truncation logic
    somehow matched microsecond-bearing timestamps to the closed hour.
    """
    rec = DataRecorder(str(tmp_path / "t.db"))
    # One row in completed hour 10, one row at the microsecond-bearing instant
    # that begins hour 11 (must NOT be rolled up).
    rec.append({"ts": "2026-06-20T10:45:00+00:00", "p1_w": 1000.0, "batt_w": 0.0})
    rec.append({"ts": "2026-06-20T11:00:00.123456+00:00", "p1_w": 2000.0, "batt_w": 0.0})

    # now is mid-hour-11 → current_hour_iso = "2026-06-20T11:00:00+00:00"
    rolled = rec.rollup_hours("2026-06-20T11:30:00+00:00")
    hourly = rec.read_hourly_rows()
    rec.close()

    assert rolled == 1, "only hour 10 is completed"
    assert len(hourly) == 1, "microsecond-bearing hour-11 row must stay in-progress"
    assert hourly[0]["hour_ts"] == "2026-06-20T10:00:00+00:00", \
        "only the completed hour-10 bucket must appear in samples_hourly"


def test_rollup_hours_is_idempotent(tmp_path):
    """Re-running rollup_hours produces no duplicate rows (UPSERT is stable)."""
    rec = DataRecorder(str(tmp_path / "t.db"))
    rec.append({"ts": "2026-06-20T10:10:00+00:00", "p1_w": 1000.0, "batt_w": 0.0})
    rec.append({"ts": "2026-06-20T10:50:00+00:00", "p1_w": 2000.0, "batt_w": 0.0})

    rec.rollup_hours("2026-06-20T11:00:00+00:00")
    second_rolled = rec.rollup_hours("2026-06-20T11:00:00+00:00")
    hourly = rec.read_hourly_rows()
    rec.close()

    assert len(hourly) == 1, "idempotent: re-run must not duplicate rows"
    assert second_rolled == 0, "second run returns 0 (nothing new to roll up)"


def test_rollup_hours_leaves_raw_rows_untouched(tmp_path):
    """rollup_hours must not delete or modify the raw samples rows."""
    rec = DataRecorder(str(tmp_path / "t.db"))
    rec.append({"ts": "2026-06-20T10:10:00+00:00", "p1_w": 1000.0})
    rec.append({"ts": "2026-06-20T10:50:00+00:00", "p1_w": 2000.0})

    rec.rollup_hours("2026-06-20T11:00:00+00:00")
    assert rec.count() == 2, "raw sample count must be unchanged after rollup"
    rec.close()


def test_rollup_hours_aggregates_stats_correctly(tmp_path):
    """rollup_hours stores correct stats for a two-sample hour."""
    rec = DataRecorder(str(tmp_path / "t.db"))
    # row1: house_load = 1000+200+300=1500; row2: house_load = 2000+0+0=2000 (pv_w NULL→0)
    rec.append({"ts": "2026-06-20T10:00:00+00:00", "p1_w": 1000.0, "batt_w": 200.0, "pv_w": 300.0, "soc": 50.0})
    rec.append({"ts": "2026-06-20T10:30:00+00:00", "p1_w": 2000.0, "batt_w": 0.0, "pv_w": None, "soc": 55.0})

    rec.rollup_hours("2026-06-20T11:00:00+00:00")
    hourly = rec.read_hourly_rows()
    rec.close()

    assert len(hourly) == 1
    row = hourly[0]
    assert row["hour_ts"] == "2026-06-20T10:00:00+00:00"
    # house_load: mean of [1500, 2000] = 1750
    assert row["house_load_mean"] == pytest.approx(1750.0)
    assert row["house_load_count"] == 2
    # pv_w: only row1 has non-NULL pv_w (300); count=1, std=0.0 (count<2 rule)
    assert row["pv_w_mean"] == pytest.approx(300.0)
    assert row["pv_w_count"] == 1
    assert row["pv_w_std"] == pytest.approx(0.0)
    # soc: mean of [50.0, 55.0] = 52.5
    assert row["soc_mean"] == pytest.approx(52.5)
    assert row["soc_count"] == 2


def test_rollup_hours_multiple_completed_hours(tmp_path):
    """rollup_hours rolls up multiple completed hours in a single call."""
    rec = DataRecorder(str(tmp_path / "t.db"))
    for hour in [8, 9, 10]:
        rec.append({
            "ts": f"2026-06-20T{hour:02d}:30:00+00:00",
            "p1_w": float(hour * 100),
            "batt_w": 0.0,
        })

    rolled = rec.rollup_hours("2026-06-20T11:00:00+00:00")
    hourly = rec.read_hourly_rows()
    rec.close()

    assert rolled == 3
    assert len(hourly) == 3
    assert hourly[0]["hour_ts"] == "2026-06-20T08:00:00+00:00"
    assert hourly[1]["hour_ts"] == "2026-06-20T09:00:00+00:00"
    assert hourly[2]["hour_ts"] == "2026-06-20T10:00:00+00:00"


# ---------------------------------------------------------------------------
# rollup_hours — watermark-bounded read
# ---------------------------------------------------------------------------

def test_rollup_hours_bounded_read_uses_watermark(tmp_path):
    rec = DataRecorder(str(tmp_path / "t.db"))
    rec.append({"ts": "2026-06-20T08:30:00+00:00", "p1_w": 100.0, "batt_w": 0.0})
    rec.append({"ts": "2026-06-20T09:30:00+00:00", "p1_w": 200.0, "batt_w": 0.0})
    rec.rollup_hours("2026-06-20T10:00:00+00:00")        # rolls hours 8 & 9

    stmts: list[str] = []
    rec._conn.set_trace_callback(stmts.append)
    rec.rollup_hours("2026-06-20T11:00:00+00:00")        # nothing new; must NOT rescan 8/9
    rec._conn.set_trace_callback(None)

    raw_selects = [
        s for s in stmts
        if "FROM samples" in s and "samples_hourly" not in s and "WHERE" in s
    ]
    assert raw_selects, "expected a bounded raw-sample SELECT"
    assert any("ts >= " in s for s in raw_selects), (
        "bounded rollup must lower-bound the raw read by the samples_hourly watermark"
    )
    rec.close()


def test_rollup_hours_bounded_still_rolls_new_hours(tmp_path):
    rec = DataRecorder(str(tmp_path / "t.db"))
    rec.append({"ts": "2026-06-20T08:30:00+00:00", "p1_w": 100.0, "batt_w": 0.0})
    assert rec.rollup_hours("2026-06-20T09:00:00+00:00") == 1     # hour 8
    rec.append({"ts": "2026-06-20T09:30:00+00:00", "p1_w": 200.0, "batt_w": 0.0})
    assert rec.rollup_hours("2026-06-20T10:00:00+00:00") == 1     # hour 9 (new) only
    assert rec.rollup_hours("2026-06-20T10:00:00+00:00") == 0     # idempotent re-run
    assert {r["hour_ts"] for r in rec.read_hourly_rows()} == {
        "2026-06-20T08:00:00+00:00", "2026-06-20T09:00:00+00:00",
    }
    rec.close()


# ---------------------------------------------------------------------------
# purge_hourly_older_than
# ---------------------------------------------------------------------------

def test_purge_hourly_older_than_deletes_older_rows(tmp_path):
    """purge_hourly_older_than removes rows with hour_ts < cutoff, keeps boundary."""
    rec = DataRecorder(str(tmp_path / "t.db"))
    for hour in [8, 9, 10, 11]:
        rec.append({
            "ts": f"2026-06-20T{hour:02d}:30:00+00:00",
            "p1_w": float(hour * 100),
            "batt_w": 0.0,
        })
    rec.rollup_hours("2026-06-20T12:00:00+00:00")  # rolls up hours 8, 9, 10, 11

    # Delete rows with hour_ts < 10:00 → hours 8 and 9 removed; 10 and 11 kept.
    deleted = rec.purge_hourly_older_than("2026-06-20T10:00:00+00:00")
    hourly = rec.read_hourly_rows()
    rec.close()

    assert deleted == 2
    assert len(hourly) == 2
    assert hourly[0]["hour_ts"] == "2026-06-20T10:00:00+00:00"
    assert hourly[1]["hour_ts"] == "2026-06-20T11:00:00+00:00"


def test_purge_hourly_boundary_row_is_kept(tmp_path):
    """purge_hourly_older_than uses strict < so the cutoff hour_ts itself is kept."""
    rec = DataRecorder(str(tmp_path / "t.db"))
    rec.append({"ts": "2026-06-20T10:30:00+00:00", "p1_w": 1000.0, "batt_w": 0.0})
    rec.rollup_hours("2026-06-20T11:00:00+00:00")

    # Cutoff matches the exact hour_ts of the only row — must NOT delete it.
    deleted = rec.purge_hourly_older_than("2026-06-20T10:00:00+00:00")
    hourly = rec.read_hourly_rows()
    rec.close()

    assert deleted == 0
    assert len(hourly) == 1, "boundary row must be kept (strict < semantics)"


def test_purge_hourly_returns_zero_when_nothing_to_delete(tmp_path):
    """Purge with a cutoff before all rows returns 0."""
    rec = DataRecorder(str(tmp_path / "t.db"))
    rec.append({"ts": "2026-06-20T10:30:00+00:00", "p1_w": 1000.0, "batt_w": 0.0})
    rec.rollup_hours("2026-06-20T11:00:00+00:00")

    deleted = rec.purge_hourly_older_than("2026-06-20T01:00:00+00:00")
    rec.close()

    assert deleted == 0


# ---------------------------------------------------------------------------
# P1-T2: weather-forecast columns — append/read round-trips
# ---------------------------------------------------------------------------

def test_append_with_weather_columns_persists_all_four(tmp_path):
    """append() with all 4 weather keys stores them; read_feature_rows returns exact values."""
    rec = DataRecorder(str(tmp_path / "t.db"))
    rec.append({
        "ts": "2026-06-22T10:00:00+00:00",
        "soc": 50.0,
        "temp_forecast": 18.5,
        "cloud_cover": 30.0,
        "humidity": 65.0,
        "wind_speed": 4.2,
    })
    rows = rec.read_feature_rows()
    rec.close()

    assert len(rows) == 1
    r = rows[0]
    assert r["temp_forecast"] == pytest.approx(18.5)
    assert r["cloud_cover"] == pytest.approx(30.0)
    assert r["humidity"] == pytest.approx(65.0)
    assert r["wind_speed"] == pytest.approx(4.2)


def test_append_without_weather_columns_stores_null(tmp_path):
    """append() with no weather keys stores NULL (None) for all 4 forecast columns."""
    rec = DataRecorder(str(tmp_path / "t.db"))
    rec.append({
        "ts": "2026-06-22T10:00:00+00:00",
        "soc": 50.0,
    })
    rows = rec.read_feature_rows()
    rec.close()

    assert len(rows) == 1
    r = rows[0]
    assert r["temp_forecast"] is None
    assert r["cloud_cover"] is None
    assert r["humidity"] is None
    assert r["wind_speed"] is None


# ---------------------------------------------------------------------------
# load_w column — migration v5 → v6
# ---------------------------------------------------------------------------

def _make_v5_db(path: str) -> None:
    """Create a DB at user_version=5 (all current columns except load_w)."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS samples ("
        "ts TEXT, hour INTEGER, weekday INTEGER, soc REAL, pv_w REAL, "
        "batt_w REAL, p1_w REAL, p1_l1 REAL, p1_l2 REAL, p1_l3 REAL, "
        "import_price REAL, export_price REAL, temp REAL, irradiance REAL, "
        "state TEXT, setpoint_w REAL, deficit_kwh REAL, "
        "temp_forecast REAL, cloud_cover REAL, humidity REAL, wind_speed REAL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS decisions ("
        "ts TEXT, active INTEGER, start_soc REAL, deficit_kwh REAL, "
        "deadline TEXT, committed_hours TEXT, horizon_mode TEXT, "
        "pv_today_forecast_kwh REAL, pv_tomorrow_forecast_kwh REAL, "
        "predicted_load_json TEXT, price_window_json TEXT, "
        "setpoint_w REAL, state TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS daily_regret ("
        "day TEXT PRIMARY KEY, regret_eur REAL, over_buy_kwh REAL, over_buy_eur REAL, "
        "under_buy_kwh REAL, cost_regret_eur REAL, optimal_kwh REAL, optimal_eur REAL, "
        "realized_kwh REAL, realized_eur REAL, infeasible INTEGER, computed_ts TEXT, "
        "dp_regret_eur REAL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS samples_hourly (hour_ts TEXT PRIMARY KEY)"
    )
    # Insert a sample row (no load_w col) to verify survival after migration.
    conn.execute(
        "INSERT INTO samples (ts, soc, p1_w, state) VALUES (?, ?, ?, ?)",
        ("2026-05-01T12:00:00+00:00", 60.0, 500.0, "passive"),
    )
    conn.execute("PRAGMA user_version = 5")
    conn.commit()
    conn.close()


def test_fresh_db_has_load_w_column_at_version_6(tmp_path):
    """A freshly-initialised DB ends at version 6 and has load_w column."""
    rec = DataRecorder(str(tmp_path / "fresh.db"))
    rec.close()

    conn = sqlite3.connect(str(tmp_path / "fresh.db"))
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    col_names = {row[1] for row in conn.execute("PRAGMA table_info(samples)").fetchall()}
    conn.close()

    assert version == _VERSION_TARGET
    assert "load_w" in col_names, "expected column 'load_w' in samples on fresh DB"


def test_migration_v5_to_v6_adds_load_w_and_preserves_rows(tmp_path):
    """Opening a v5 DB must add load_w, reach _VERSION_TARGET, and leave rows intact."""
    db_path = str(tmp_path / "v5.db")
    _make_v5_db(db_path)

    rec = DataRecorder(db_path)
    rec.close()

    conn = sqlite3.connect(db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    col_names = {row[1] for row in conn.execute("PRAGMA table_info(samples)").fetchall()}
    sample_count = conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
    row = conn.execute("SELECT load_w FROM samples").fetchone()
    conn.close()

    assert version == _VERSION_TARGET
    assert "load_w" in col_names, "expected column 'load_w' after v5→v6 migration"
    assert sample_count == 1, "pre-existing sample row must survive migration"
    assert row[0] is None, "load_w must be NULL for pre-existing rows"


def test_migration_v5_to_v6_is_idempotent(tmp_path):
    """Re-opening a migrated v6 DB must not error and must stay at _VERSION_TARGET."""
    db_path = str(tmp_path / "v5.db")
    _make_v5_db(db_path)

    DataRecorder(db_path).close()   # first open: v5 → v6
    DataRecorder(db_path).close()   # second open: already at target, no-op

    conn = sqlite3.connect(db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    col_names = {row[1] for row in conn.execute("PRAGMA table_info(samples)").fetchall()}
    conn.close()

    assert version == _VERSION_TARGET
    assert "load_w" in col_names


def test_migration_v5_to_v6_recovers_from_partial_alter(tmp_path):
    """Crash after ALTER (load_w added) but before user_version=6 must recover cleanly."""
    db_path = str(tmp_path / "v5_partial.db")
    _make_v5_db(db_path)

    # Simulate crash: column added but user_version still 5.
    conn = sqlite3.connect(db_path)
    conn.execute("ALTER TABLE samples ADD COLUMN load_w REAL")
    conn.commit()  # user_version intentionally left at 5
    conn.close()

    # Must NOT raise duplicate column name — the guard skips already-present col.
    DataRecorder(db_path).close()

    conn = sqlite3.connect(db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    col_names = {r[1] for r in conn.execute("PRAGMA table_info(samples)").fetchall()}
    conn.close()

    assert version == _VERSION_TARGET
    assert "load_w" in col_names


def test_append_with_load_w_roundtrips(tmp_path):
    """append() with load_w=150.0 stores it; read_feature_rows returns exact value."""
    rec = DataRecorder(str(tmp_path / "t.db"))
    rec.append({
        "ts": "2026-06-24T10:00:00+00:00",
        "p1_w": 300.0,
        "load_w": 150.0,
    })
    rows = rec.read_feature_rows()
    rec.close()

    assert len(rows) == 1
    assert rows[0]["load_w"] == pytest.approx(150.0)


# ---------------------------------------------------------------------------
# v7 export signal columns — migration v6 → v7
# ---------------------------------------------------------------------------

_V7_EXPORT_COLS = [
    "export_setpoint_w",
    "export_kwh",
    "reserve_kwh",
    "surplus_kwh",
]


def _make_v6_db(path: str) -> None:
    """Create a DB at user_version=6 (all current columns including load_w, no v7 cols)."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS samples ("
        "ts TEXT, hour INTEGER, weekday INTEGER, soc REAL, pv_w REAL, "
        "batt_w REAL, p1_w REAL, p1_l1 REAL, p1_l2 REAL, p1_l3 REAL, "
        "import_price REAL, export_price REAL, temp REAL, irradiance REAL, "
        "state TEXT, setpoint_w REAL, deficit_kwh REAL, "
        "temp_forecast REAL, cloud_cover REAL, humidity REAL, wind_speed REAL, "
        "load_w REAL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS decisions ("
        "ts TEXT, active INTEGER, start_soc REAL, deficit_kwh REAL, "
        "deadline TEXT, committed_hours TEXT, horizon_mode TEXT, "
        "pv_today_forecast_kwh REAL, pv_tomorrow_forecast_kwh REAL, "
        "predicted_load_json TEXT, price_window_json TEXT, "
        "setpoint_w REAL, state TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS daily_regret ("
        "day TEXT PRIMARY KEY, regret_eur REAL, over_buy_kwh REAL, over_buy_eur REAL, "
        "under_buy_kwh REAL, cost_regret_eur REAL, optimal_kwh REAL, optimal_eur REAL, "
        "realized_kwh REAL, realized_eur REAL, infeasible INTEGER, computed_ts TEXT, "
        "dp_regret_eur REAL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS samples_hourly (hour_ts TEXT PRIMARY KEY)"
    )
    # Insert a sample row to verify survival after migration.
    conn.execute(
        "INSERT INTO samples (ts, soc, p1_w, load_w, state) VALUES (?, ?, ?, ?, ?)",
        ("2026-06-01T10:00:00+00:00", 55.0, 400.0, 350.0, "passive"),
    )
    conn.execute("PRAGMA user_version = 6")
    conn.commit()
    conn.close()


def test_fresh_db_has_v7_export_columns_at_version_7(tmp_path):
    """A freshly-initialised DB ends at version 7 and has all 4 v7 export columns."""
    rec = DataRecorder(str(tmp_path / "fresh.db"))
    rec.close()

    conn = sqlite3.connect(str(tmp_path / "fresh.db"))
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    col_names = {row[1] for row in conn.execute("PRAGMA table_info(samples)").fetchall()}
    conn.close()

    assert version == _VERSION_TARGET
    for col in _V7_EXPORT_COLS:
        assert col in col_names, f"expected column '{col}' in fresh DB"


def test_migration_v6_to_v7_adds_export_columns_and_preserves_rows(tmp_path):
    """Opening a v6 DB must add 4 export cols, reach _VERSION_TARGET, and leave rows intact."""
    db_path = str(tmp_path / "v6.db")
    _make_v6_db(db_path)

    rec = DataRecorder(db_path)
    rec.close()

    conn = sqlite3.connect(db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    col_names = {row[1] for row in conn.execute("PRAGMA table_info(samples)").fetchall()}
    sample_count = conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
    # New v7 cols must be NULL for pre-existing rows.
    row = conn.execute(
        "SELECT export_setpoint_w, export_kwh, reserve_kwh, surplus_kwh FROM samples"
    ).fetchone()
    conn.close()

    assert version == _VERSION_TARGET
    for col in _V7_EXPORT_COLS:
        assert col in col_names, f"expected column '{col}' after v6→v7 migration"
    assert sample_count == 1, "pre-existing sample row must survive migration"
    assert all(v is None for v in row), "v7 cols must be NULL for pre-v7 rows"


def test_migration_v6_to_v7_is_idempotent(tmp_path):
    """Re-opening a migrated v7 DB must not error and must stay at _VERSION_TARGET."""
    db_path = str(tmp_path / "v6.db")
    _make_v6_db(db_path)

    DataRecorder(db_path).close()   # first open: v6 → v7
    DataRecorder(db_path).close()   # second open: already at target, no-op

    conn = sqlite3.connect(db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    col_names = {row[1] for row in conn.execute("PRAGMA table_info(samples)").fetchall()}
    conn.close()

    assert version == _VERSION_TARGET
    for col in _V7_EXPORT_COLS:
        assert col in col_names, f"expected column '{col}' after idempotent re-open"


def test_migration_v6_to_v7_recovers_from_partial_alter(tmp_path):
    """Crash after some ALTERs but before user_version=7 must recover cleanly (no duplicate col)."""
    db_path = str(tmp_path / "v6_partial.db")
    _make_v6_db(db_path)

    # Simulate crash: first two cols added, user_version still 6.
    conn = sqlite3.connect(db_path)
    conn.execute("ALTER TABLE samples ADD COLUMN export_setpoint_w REAL")
    conn.execute("ALTER TABLE samples ADD COLUMN export_kwh REAL")
    conn.commit()  # user_version intentionally left at 6
    conn.close()

    # Must NOT raise "duplicate column name".
    DataRecorder(db_path).close()

    conn = sqlite3.connect(db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    col_names = {r[1] for r in conn.execute("PRAGMA table_info(samples)").fetchall()}
    conn.close()

    assert version == _VERSION_TARGET
    for col in _V7_EXPORT_COLS:
        assert col in col_names, f"expected column '{col}' after partial-alter recovery"


def test_append_with_v7_export_columns_roundtrips(tmp_path):
    """append() with v7 export columns stores them; missing cols → NULL."""
    rec = DataRecorder(str(tmp_path / "t7.db"))
    rec.append({
        "ts": "2026-06-25T10:00:00+00:00",
        "p1_w": 300.0,
        "export_setpoint_w": 2500.0,
        "export_kwh": 3.5,
        "reserve_kwh": 5.0,
        "surplus_kwh": 2.1,
    })
    rec.append({
        "ts": "2026-06-25T10:05:00+00:00",
        "p1_w": 300.0,
        # v7 cols absent → must store as NULL
    })
    rec.close()

    conn = sqlite3.connect(str(tmp_path / "t7.db"))
    rows = conn.execute(
        "SELECT export_setpoint_w, export_kwh, reserve_kwh, surplus_kwh FROM samples ORDER BY ts"
    ).fetchall()
    conn.close()

    assert rows[0] == pytest.approx((2500.0, 3.5, 5.0, 2.1))
    assert all(v is None for v in rows[1]), "missing v7 cols must be stored as NULL"


# ---------------------------------------------------------------------------
# v8 persons_home columns — migration v7 → v8
# ---------------------------------------------------------------------------

_V8_COLS = ["persons_home"]
_V8_HOURLY_COLS = [
    "persons_home_mean", "persons_home_max", "persons_home_min",
    "persons_home_std", "persons_home_count",
]


def _make_v7_db(path: str) -> None:
    """Create a DB at user_version=7: full v7 samples + a pre-v8 samples_hourly
    (no persons_home_* columns) so the v8 migration must ALTER both tables."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS samples ("
        "ts TEXT, hour INTEGER, weekday INTEGER, soc REAL, pv_w REAL, "
        "batt_w REAL, p1_w REAL, p1_l1 REAL, p1_l2 REAL, p1_l3 REAL, "
        "import_price REAL, export_price REAL, temp REAL, irradiance REAL, "
        "state TEXT, setpoint_w REAL, deficit_kwh REAL, "
        "temp_forecast REAL, cloud_cover REAL, humidity REAL, wind_speed REAL, "
        "load_w REAL, export_setpoint_w REAL, export_kwh REAL, "
        "reserve_kwh REAL, surplus_kwh REAL)"
    )
    # Minimal pre-v8 samples_hourly (mirrors _make_v6_db): exists so the v8
    # ALTERs target it; lacks the persons_home_* stat columns.
    conn.execute("CREATE TABLE IF NOT EXISTS samples_hourly (hour_ts TEXT PRIMARY KEY)")
    conn.execute(
        "INSERT INTO samples (ts, soc, p1_w, load_w, state) VALUES (?, ?, ?, ?, ?)",
        ("2026-07-01T10:00:00+00:00", 55.0, 400.0, 350.0, "passive"),
    )
    conn.execute("PRAGMA user_version = 7")
    conn.commit()
    conn.close()


def test_fresh_db_has_persons_home_columns_at_version_8(tmp_path):
    rec = DataRecorder(str(tmp_path / "fresh.db"))
    rec.close()
    conn = sqlite3.connect(str(tmp_path / "fresh.db"))
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    s_cols = {row[1] for row in conn.execute("PRAGMA table_info(samples)").fetchall()}
    h_cols = {row[1] for row in conn.execute("PRAGMA table_info(samples_hourly)").fetchall()}
    conn.close()
    assert version == _VERSION_TARGET
    for col in _V8_COLS:
        assert col in s_cols, f"expected samples column '{col}' in fresh DB"
    for col in _V8_HOURLY_COLS:
        assert col in h_cols, f"expected samples_hourly column '{col}' in fresh DB"


def test_migration_v7_to_v8_adds_columns_and_preserves_rows(tmp_path):
    db_path = str(tmp_path / "v7.db")
    _make_v7_db(db_path)
    DataRecorder(db_path).close()
    conn = sqlite3.connect(db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    s_cols = {row[1] for row in conn.execute("PRAGMA table_info(samples)").fetchall()}
    h_cols = {row[1] for row in conn.execute("PRAGMA table_info(samples_hourly)").fetchall()}
    sample_count = conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
    ph = conn.execute("SELECT persons_home FROM samples").fetchone()[0]
    conn.close()
    assert version == _VERSION_TARGET
    for col in _V8_COLS:
        assert col in s_cols
    for col in _V8_HOURLY_COLS:
        assert col in h_cols, f"expected samples_hourly column '{col}' after v7→v8"
    assert sample_count == 1
    assert ph is None, "persons_home must be NULL for pre-v8 rows"


def test_migration_v7_to_v8_is_idempotent(tmp_path):
    db_path = str(tmp_path / "v7.db")
    _make_v7_db(db_path)
    DataRecorder(db_path).close()
    DataRecorder(db_path).close()
    conn = sqlite3.connect(db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    s_cols = {row[1] for row in conn.execute("PRAGMA table_info(samples)").fetchall()}
    h_cols = {row[1] for row in conn.execute("PRAGMA table_info(samples_hourly)").fetchall()}
    conn.close()
    assert version == _VERSION_TARGET
    for col in _V8_COLS:
        assert col in s_cols
    for col in _V8_HOURLY_COLS:
        assert col in h_cols


def test_migration_v7_to_v8_recovers_from_partial_alter(tmp_path):
    db_path = str(tmp_path / "v7_partial.db")
    _make_v7_db(db_path)
    conn = sqlite3.connect(db_path)
    # Simulate crash: samples col + some hourly cols added, user_version still 7.
    conn.execute("ALTER TABLE samples ADD COLUMN persons_home REAL")
    conn.execute("ALTER TABLE samples_hourly ADD COLUMN persons_home_mean REAL")
    conn.execute("ALTER TABLE samples_hourly ADD COLUMN persons_home_max REAL")
    conn.commit()  # user_version intentionally left at 7
    conn.close()
    DataRecorder(db_path).close()  # must NOT raise "duplicate column name"
    conn = sqlite3.connect(db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    s_cols = {r[1] for r in conn.execute("PRAGMA table_info(samples)").fetchall()}
    h_cols = {r[1] for r in conn.execute("PRAGMA table_info(samples_hourly)").fetchall()}
    conn.close()
    assert version == _VERSION_TARGET
    for col in _V8_COLS:
        assert col in s_cols
    for col in _V8_HOURLY_COLS:
        assert col in h_cols


# ---------------------------------------------------------------------------
# D1 — rollup watermark clamp + future hourly-row purge; NULL-ts purge
# ---------------------------------------------------------------------------

def test_rollup_recovers_after_future_dated_watermark(tmp_path):
    """A future-dated sample rolled during a pre-NTP boot must NOT freeze the
    hourly rollup once the clock steps back; the bogus future hourly row is dropped."""
    rec = DataRecorder(str(tmp_path / "t.db"))
    rec.append({"ts": "2027-01-01T10:05:00+00:00", "p1_w": 100.0, "batt_w": 0.0,
                "pv_w": 0.0, "load_w": 100.0})
    rec.rollup_hours("2027-01-01T11:00:00+00:00")           # future watermark written
    rec.append({"ts": "2026-07-08T09:10:00+00:00", "p1_w": 200.0, "batt_w": 0.0,
                "pv_w": 0.0, "load_w": 200.0})
    rec.rollup_hours("2026-07-08T10:00:00+00:00")           # clock corrected
    hours = {r["hour_ts"] for r in rec.read_hourly_rows()}
    assert "2026-07-08T09:00:00+00:00" in hours             # recovered
    assert "2027-01-01T10:00:00+00:00" not in hours         # bogus future row dropped
    rec.close()


def test_purge_removes_null_ts_rows(tmp_path):
    rec = DataRecorder(str(tmp_path / "t.db"))
    rec.append({"ts": None, "p1_w": 1.0, "batt_w": 0.0, "pv_w": 0.0, "load_w": 1.0})
    removed = rec.purge_older_than("2026-07-08T00:00:00+00:00", 30)
    assert removed == 1
    rec.close()


def test_wal_checkpoint_makes_rows_visible_to_immutable_reader(tmp_path):
    path = str(tmp_path / "t.db")
    rec = DataRecorder(path)
    rec.append({"ts": "2026-07-08T09:00:00+00:00", "p1_w": 100.0, "batt_w": 0.0,
                "pv_w": 0.0, "load_w": 100.0})
    rec.wal_checkpoint()
    ro = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    n = ro.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
    ro.close()
    rec.close()
    assert n == 1


def test_append_normalizes_non_utc_ts_to_plus0000(tmp_path):
    """A +02:00 ts is stored as its canonical +00:00 equivalent so lexicographic
    ts compares (rollup watermark / purge cutoff) stay sound."""
    rec = DataRecorder(str(tmp_path / "t.db"))
    rec.append({"ts": "2026-07-08T12:00:00+02:00", "p1_w": 100.0, "batt_w": 0.0,
                "pv_w": 0.0, "load_w": 100.0})
    stored = rec._conn.execute("SELECT ts FROM samples").fetchone()[0]
    assert stored == "2026-07-08T10:00:00+00:00"
    rec.close()


def test_append_utc_ts_is_byte_identical(tmp_path):
    rec = DataRecorder(str(tmp_path / "t.db"))
    rec.append({"ts": "2026-07-08T09:00:00+00:00", "p1_w": 1.0, "batt_w": 0.0,
                "pv_w": 0.0, "load_w": 1.0})
    stored = rec._conn.execute("SELECT ts FROM samples").fetchone()[0]
    assert stored == "2026-07-08T09:00:00+00:00"
    rec.close()


def test_append_decision_normalizes_ts(tmp_path):
    rec = DataRecorder(str(tmp_path / "t.db"))
    rec.append_decision(
        ts="2026-07-08T12:00:00+02:00", active=True, start_soc=50.0, deadline=None,
        committed_hours=[], horizon_mode="single-day",
        pv_today_forecast_kwh=None, pv_tomorrow_forecast_kwh=None,
        predicted_load_json=None, price_window_json=None, setpoint_w=0.0, state="passive")
    stored = rec._conn.execute("SELECT ts FROM decisions").fetchone()[0]
    assert stored == "2026-07-08T10:00:00+00:00"
    rec.close()
