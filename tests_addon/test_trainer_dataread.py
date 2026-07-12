"""Tests for trainer.load_rows() — DB read layer (T3).

All cases verify the never-crash / read-only contract.
"""

from __future__ import annotations

import os
import sqlite3


from tests_addon._synthetic import make_hourly_rows, make_samples_hourly_db
from trainer import _MIN_TRAIN_ROWS, load_rows


# ---------------------------------------------------------------------------
# (a) Missing path → None, no raise
# ---------------------------------------------------------------------------


def test_load_rows_missing_file_returns_none(tmp_path):
    result = load_rows(str(tmp_path / "nonexistent.db"))
    assert result is None


# ---------------------------------------------------------------------------
# (b) DB present but samples_hourly is empty (0 rows) → None
# ---------------------------------------------------------------------------


def test_load_rows_empty_table_returns_none(tmp_path):
    db_path = str(tmp_path / "empty.db")
    make_samples_hourly_db(db_path, days=0)  # schema created, 0 rows inserted
    result = load_rows(db_path)
    assert result is None


# ---------------------------------------------------------------------------
# (c) Fewer than _MIN_TRAIN_ROWS rows → None
# ---------------------------------------------------------------------------


def test_load_rows_too_few_rows_returns_none(tmp_path):
    db_path = str(tmp_path / "sparse.db")
    # Build a 2-day (48-row) DB, then delete down to _MIN_TRAIN_ROWS - 1 = 23 rows.
    make_samples_hourly_db(db_path, days=2)
    conn = sqlite3.connect(db_path)
    try:
        total = conn.execute("SELECT COUNT(*) FROM samples_hourly").fetchone()[0]
        # Delete enough rows so that (total - deleted) == _MIN_TRAIN_ROWS - 1
        to_delete = total - (_MIN_TRAIN_ROWS - 1)
        assert to_delete > 0, "pre-condition: need more rows than target"
        conn.execute(
            f"DELETE FROM samples_hourly WHERE hour_ts IN "
            f"(SELECT hour_ts FROM samples_hourly ORDER BY hour_ts DESC LIMIT {to_delete})"
        )
        conn.commit()
        remaining = conn.execute("SELECT COUNT(*) FROM samples_hourly").fetchone()[0]
    finally:
        conn.close()

    assert remaining == _MIN_TRAIN_ROWS - 1
    result = load_rows(db_path)
    assert result is None


# ---------------------------------------------------------------------------
# (d) Enough rows → returns list with correct length and key set
# ---------------------------------------------------------------------------


def test_load_rows_sufficient_rows_returns_list(tmp_path):
    days = 28
    db_path = str(tmp_path / "full.db")
    make_samples_hourly_db(db_path, days=days)

    result = load_rows(db_path)

    expected_count = days * 24
    assert result is not None
    assert len(result) == expected_count

    # Key set must exactly match what make_hourly_rows produces.
    expected_keys = set(make_hourly_rows(1)[0].keys())
    actual_keys = set(result[0].keys())
    assert actual_keys == expected_keys


# ---------------------------------------------------------------------------
# (e) Read-only enforcement: chmod 0o444 → load_rows must still succeed
# ---------------------------------------------------------------------------


def test_load_rows_readonly_file_succeeds(tmp_path):
    db_path = str(tmp_path / "ro.db")
    make_samples_hourly_db(db_path, days=28)

    # Make file read-only — any write attempt raises "attempt to write a
    # readonly database"; this proves no migration/write path is triggered.
    os.chmod(db_path, 0o444)
    try:
        result = load_rows(db_path)
        assert result is not None
        assert len(result) == 28 * 24
    finally:
        # Restore so tmp_path cleanup can delete the file.
        os.chmod(db_path, 0o644)


# ---------------------------------------------------------------------------
# (f) Drift guard: load_rows() must equal DataRecorder.read_hourly_rows() on the
#     same (writable) DB. load_rows mirrors that SELECT against a read-only conn
#     (DataRecorder._migrate() would write, so it can't run on the RO mount);
#     this locks the mirror to the canonical reader so a future query/schema
#     change in recorder.py can't silently diverge.
# ---------------------------------------------------------------------------


def test_load_rows_matches_recorder_read_hourly_rows(tmp_path):
    from forecast_core.recorder import DataRecorder

    db_path = str(tmp_path / "parity.db")
    make_samples_hourly_db(db_path, days=28)

    rec = DataRecorder(db_path)  # writable copy — _migrate() DDL is fine here
    try:
        expected = rec.read_hourly_rows()
    finally:
        rec.close()

    got = load_rows(db_path)
    assert got is not None
    # Same rows, same order, same keys, same values as the canonical reader.
    assert got == expected


# ---------------------------------------------------------------------------
# (g) H3b: load_rows must open the DB immutable so it sees rows checkpointed
#     out of the WAL by the integration's periodic wal_checkpoint(TRUNCATE)
#     (Task 9) — the read-only-mount contract for the addon.
# ---------------------------------------------------------------------------


def test_load_rows_reads_wal_db_via_immutable(tmp_path):
    from forecast_core.recorder import DataRecorder

    path = str(tmp_path / "t.db")
    rec = DataRecorder(path)
    for h in range(30):
        rec.append(
            {
                "ts": f"2026-06-{h % 28 + 1:02d}T10:00:00+00:00",
                "p1_w": 100.0,
                "batt_w": 0.0,
                "pv_w": 0.0,
                "load_w": 100.0,
            }
        )
    rec.rollup_hours("2026-07-08T00:00:00+00:00")
    rec.wal_checkpoint()
    rec.close()
    rows = load_rows(path)
    assert rows is not None and len(rows) >= 24
