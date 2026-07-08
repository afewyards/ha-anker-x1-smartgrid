"""Integration tests for trainer.train_once (T5 — training pipeline).

Uses make_samples_hourly_db from _synthetic to populate real SQLite DBs, then
exercises the full train_once flow:  load_rows → is_ready gate → fit → backtest.

Low-history boundary
--------------------
is_ready requires ≥ 21 lag-complete Europe/Amsterdam calendar dates.  The
lag-complete rule demands that the row at (t − 168 h) is also present, so the
FIRST 7 days of rows can never be lag-complete.  With the synthetic start of
2024-01-01 00:00 UTC (= 01:00 CET), the lag-complete date count grows roughly
as (days − 7) calendar dates.  Testing with 5 days → 0 lag-complete dates →
is_ready always returns False, which is the "not-ready" gate we want to verify.
"""
from __future__ import annotations

import pytest

from tests_addon._synthetic import make_samples_hourly_db
from trainer import TrainState, train_once


# ---------------------------------------------------------------------------
# Sufficient history: 28 days → ≥ 21 lag-complete dates → model should train
# ---------------------------------------------------------------------------


def test_sufficient_history_trains_and_returns_ready(tmp_path):
    """28 days of synthetic data → ready=True, metrics populated, model fitted."""
    db = str(tmp_path / "test.db")
    make_samples_hourly_db(db, 28)

    st = train_once(db)

    assert isinstance(st, TrainState)
    assert st.ready is True
    assert st.model is not None
    assert st.n_rows == 28 * 24
    assert st.last_trained is not None

    # metrics dict must exist and carry the three keys the spec requires
    assert isinstance(st.metrics, dict)
    assert "horizon_energy_mae_24h" in st.metrics
    assert "pinball_p50" in st.metrics
    assert "pinball_p80" in st.metrics

    # promoted is a bool — do NOT assert a specific value; synthetic quality varies
    assert isinstance(st.promoted, bool)


# ---------------------------------------------------------------------------
# Low history: 5 days → 0 lag-complete dates → not-ready gate trips
# ---------------------------------------------------------------------------


def test_low_history_stays_dormant(tmp_path):
    """5 days of synthetic data → not enough lag-complete days → ready=False.

    The point is to exercise the is_ready gate path.  With only 5 days
    the first-7-day lag window means zero rows can be lag-complete.
    """
    db = str(tmp_path / "low.db")
    make_samples_hourly_db(db, 5)

    # Must not raise — container must survive a cold-start / early-deploy state
    st = train_once(db)

    assert st.ready is False
    assert st.promoted is False
    assert st.model is None
    # n_rows may be 0 if load_rows returns None (< _MIN_TRAIN_ROWS),
    # or 5*24 if load_rows returned rows but is_ready tripped.
    # Either way, promoted=False and ready=False are the contract.


# ---------------------------------------------------------------------------
# Missing DB: path does not exist → graceful not-ready, n_rows == 0
# ---------------------------------------------------------------------------


def test_missing_db_returns_not_ready(tmp_path):
    """Non-existent DB path → ready=False, n_rows=0, no exception."""
    st = train_once(str(tmp_path / "nope.db"))

    assert st.ready is False
    assert st.promoted is False
    assert st.n_rows == 0
    assert st.last_trained is None
    assert st.model is None
    assert st.metrics is None
