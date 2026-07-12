"""DB read layer and training pipeline for the HGBR trainer.

Opens the recorder DB read-only (the add-on mounts /config as read-only via
``map: [config:ro]`` in config.yaml).  DataRecorder.__init__ is NOT used here
because _migrate() unconditionally issues DDL writes (CREATE TABLE IF NOT
EXISTS + CREATE INDEX IF NOT EXISTS + PRAGMA user_version) even when the
schema is already at the current version — those writes would raise
"attempt to write a readonly database" on a RO-mounted file.

Instead we open the connection ourselves with ``mode=ro`` URI and run the
same SELECT that DataRecorder.read_hourly_rows() uses, so the dict shape
(column names, ordering) is identical.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, UTC
from pathlib import Path
from collections.abc import Sequence

from forecast_core.backtest import should_promote, walk_forward_hgbr
from forecast_core.const import (
    DEFAULT_BACKTEST_TEST_DAYS,
    DEFAULT_FALLBACK_LOAD_W,
    DEFAULT_TRAIN_DAYS,
)
from forecast_core.hgbr import HGBRQuantileModel

_log = logging.getLogger(__name__)

# Minimum number of samples_hourly rows required to attempt training.
_MIN_TRAIN_ROWS = 24

# Minimum lag-complete days to activate the ML path (was const.DEFAULT_HGBR_MIN_DAYS
# until that constant was removed from const.py in the config-keys cleanup).
_DEFAULT_HGBR_MIN_DAYS = 21


@dataclass
class TrainState:
    """Snapshot of the training pipeline result."""

    ready: bool
    promoted: bool
    last_trained: datetime | None
    n_rows: int
    metrics: dict | None
    model: object | None


def train_once(
    db_path: str,
    *,
    train_days: int = DEFAULT_TRAIN_DAYS,
    test_days: int = DEFAULT_BACKTEST_TEST_DAYS,
    fallback_w: float = DEFAULT_FALLBACK_LOAD_W,
    quantiles: Sequence[float] = (0.5, 0.8),
    min_days: int = _DEFAULT_HGBR_MIN_DAYS,
) -> TrainState:
    """Load rows, check coverage, fit, backtest, and return a TrainState.

    Never raises — any failure is logged and returned as a not-ready state.

    Parameters
    ----------
    db_path:
        Path to the SQLite recorder DB (opened read-only by load_rows).
    train_days:
        Rolling-origin training window in days (passed to walk_forward_hgbr).
    test_days:
        Rolling-origin test window in days (passed to walk_forward_hgbr).
    fallback_w:
        Watt value to use when the model has no prediction (passed through).
    quantiles:
        Quantile levels to train and evaluate (e.g. (0.5, 0.8)).
    min_days:
        Minimum lag-complete local-calendar days required to activate the ML
        path (passed to HGBRQuantileModel.is_ready).

    Returns
    -------
    TrainState — always; never raises.
    """
    rows = load_rows(db_path)
    if rows is None:
        return TrainState(
            ready=False,
            promoted=False,
            last_trained=None,
            n_rows=0,
            metrics=None,
            model=None,
        )

    now = datetime.now(UTC)
    model = HGBRQuantileModel()

    if not model.is_ready(rows, min_days=min_days):
        return TrainState(
            ready=False,
            promoted=False,
            last_trained=now,
            n_rows=len(rows),
            metrics=None,
            model=None,
        )

    try:
        model.fit(rows, quantiles=tuple(quantiles))
        metrics = walk_forward_hgbr(
            rows,
            train_days=train_days,
            test_days=test_days,
            fallback_w=fallback_w,
            quantiles=tuple(quantiles),
        )
        promoted = should_promote(metrics)
        return TrainState(
            ready=True,
            promoted=promoted,
            last_trained=now,
            n_rows=len(rows),
            metrics=metrics,
            model=model,
        )
    except Exception:
        _log.exception("train_once: fit/backtest failed for db_path=%r", db_path)
        return TrainState(
            ready=False,
            promoted=False,
            last_trained=now,
            n_rows=len(rows),
            metrics=None,
            model=None,
        )


def load_rows(db_path: str, *, since_iso: str | None = None) -> list[dict] | None:
    """Load samples_hourly rows from db_path, read-only.

    Mirrors DataRecorder.read_hourly_rows() — same SELECT, same dict shape,
    same column order — but opens the connection with ``mode=ro`` URI so no
    write is ever attempted on the DB file.

    Args:
        db_path: Path to the sqlite3 database file.
        since_iso: Optional ISO timestamp; if provided, only rows with
            hour_ts >= since_iso are returned (for serve-time refresh, not
            full training).

    Returns:
        A list of row dicts (one per hourly bucket, ordered by hour_ts ASC)
        when the table contains >= _MIN_TRAIN_ROWS rows (or any rows if
        since_iso is provided — serve-time refresh doesn't require a minimum).

        None when:
        - db_path does not exist
        - samples_hourly is empty (0 rows)
        - fewer than _MIN_TRAIN_ROWS rows are present (training mode only)
        - any sqlite3.Error occurs
    """
    if not Path(db_path).exists():
        return None

    conn: sqlite3.Connection | None = None
    try:
        # mode=ro&immutable=1: raises OperationalError if the file doesn't exist
        # or is not a valid SQLite DB — never creates or writes to the file.
        # immutable=1 reads a consistent snapshot as of the last integration
        # wal_checkpoint(TRUNCATE) (accepts checkpoint lag; no -shm write on the
        # config:ro mount).
        conn = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            if since_iso is not None:
                cur = conn.execute(
                    "SELECT * FROM samples_hourly WHERE hour_ts >= ? ORDER BY hour_ts ASC",
                    (since_iso,),
                )
            else:
                cur = conn.execute("SELECT * FROM samples_hourly ORDER BY hour_ts ASC")
            rows = [dict(r) for r in cur.fetchall()]
        finally:
            conn.row_factory = None

        # Minimum row check only applies to full training loads, not serve-time refresh.
        if since_iso is None and len(rows) < _MIN_TRAIN_ROWS:
            return None

        return rows if rows else None

    except sqlite3.Error:
        return None

    finally:
        if conn is not None:
            conn.close()


# Days of history to read for serve-time lag refresh (covers lag_168h + buffer).
_REFRESH_LOOKBACK_DAYS = 9


def refresh_model_lookups(model, db_path: str) -> bool:
    """Refresh a model's lag lookups from the RO recorder DB (serve-time).

    Re-reads the last ~9 days of samples_hourly so load_lag_1h /
    rolling_mean_24h reflect the live day instead of the 03:00 retrain anchor.
    Never raises — any failure keeps the fit-time lookups and the model
    serves as before.
    """
    try:
        refresh = getattr(model, "refresh_lookups", None)
        if refresh is None:
            return False
        since_iso = (datetime.now(UTC) - timedelta(days=_REFRESH_LOOKBACK_DAYS)).isoformat()
        rows = load_rows(db_path, since_iso=since_iso)
        if not rows:
            return False
        return bool(refresh(rows))
    except Exception:
        return False
