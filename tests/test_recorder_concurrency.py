import sqlite3
import threading

from custom_components.anker_x1_smartgrid.recorder import DataRecorder


def test_file_db_uses_wal_journal_mode(tmp_path):
    """WAL on a file-backed DB; never asserted on :memory: (no-op there)."""
    rec = DataRecorder(str(tmp_path / "wal.db"))
    mode = rec._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    rec.close()


def test_read_does_not_mutate_connection_row_factory(tmp_path):
    """A read must use a per-call cursor and leave self._conn.row_factory alone."""
    rec = DataRecorder(str(tmp_path / "rf.db"))
    rec.append({"ts": "2026-06-20T12:00:00+00:00", "soc": 50.0, "state": "passive"})
    sentinel = sqlite3.Row
    rec._conn.row_factory = sentinel  # caller-owned global factory
    rows = rec.read_feature_rows()
    assert rows and rows[0]["soc"] == 50.0  # still returns dict rows
    # Current code resets row_factory to None in its finally → clobbers sentinel (RED).
    # Per-cursor code never touches self._conn.row_factory → sentinel preserved (GREEN).
    assert rec._conn.row_factory is sentinel
    rec.close()


def test_concurrent_append_and_read_is_thread_safe(tmp_path):
    """8 threads × 50 iters of append()+read under the lock: no exception, exact count."""
    rec = DataRecorder(str(tmp_path / "conc.db"))
    errors: list[Exception] = []
    n_threads, n_iters = 8, 50

    def worker(tid: int) -> None:
        try:
            for i in range(n_iters):
                rec.append({
                    "ts": f"2026-06-20T12:{tid:02d}:{i:02d}+00:00",
                    "soc": float(i), "state": "passive",
                })
                rec.read_feature_rows()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert rec.count() == n_threads * n_iters
    rec.close()
