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

import sqlite3
from datetime import datetime, timedelta, UTC

import trainer
from forecast_core.backtest import MIN_HORIZON_ORIGINS_24H
from forecast_core.const import DEFAULT_BACKTEST_TEST_DAYS, DEFAULT_TRAIN_DAYS
from forecast_core.hgbr import HGBRQuantileModel
from tests_addon._synthetic import make_samples_hourly_db
from trainer import TrainState, train_once

# make_samples_hourly_db defaults to this start when no `start=` kwarg is given.
_SYNTHETIC_START = datetime(2024, 1, 1, tzinfo=UTC)


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


# ---------------------------------------------------------------------------
# since_iso training-data floor (train_once)
# ---------------------------------------------------------------------------


def test_train_since_floor_filters_rows(tmp_path):
    """since_iso floors load_rows to the last 28 of 36 synthetic days — n_rows
    reflects only the floored rows, and 28 days still clears the is_ready
    coverage gate."""
    db = str(tmp_path / "floor.db")
    make_samples_hourly_db(db, 36)
    since_iso = (_SYNTHETIC_START + timedelta(days=8)).isoformat()

    st = train_once(db, since_iso=since_iso)

    assert st.n_rows == 28 * 24
    assert st.n_rows != 36 * 24
    assert st.ready is True


def test_train_since_floor_below_min_rows_stays_dormant(tmp_path):
    """since_iso within 12h of the newest row leaves fewer than
    trainer._MIN_TRAIN_ROWS rows post-floor — train_once must fall back to the
    not-ready dormant state, never raise."""
    db = str(tmp_path / "thin.db")
    make_samples_hourly_db(db, 28)
    newest = _SYNTHETIC_START + timedelta(hours=28 * 24 - 1)
    since_iso = (newest - timedelta(hours=12)).isoformat()

    st = train_once(db, since_iso=since_iso)

    assert st.ready is False
    assert st.promoted is False
    assert st.model is None


def test_train_once_logs_effective_floor_and_row_count(tmp_path, caplog):
    """M3/L1: when since_iso is set, train_once must log the effective floor
    and the post-floor row count — otherwise 'why did ML go dormant/thin' is
    unanswerable from ha core logs."""
    import logging

    db = str(tmp_path / "floor_logged.db")
    make_samples_hourly_db(db, 36)
    since_iso = (_SYNTHETIC_START + timedelta(days=8)).isoformat()

    with caplog.at_level(logging.INFO, logger="trainer"):
        st = train_once(db, since_iso=since_iso)

    assert st.n_rows == 28 * 24
    assert since_iso in caplog.text
    assert str(28 * 24) in caplog.text


def test_train_once_warns_when_floored_rows_below_minimum(tmp_path, caplog):
    """M3/L1: the sub-minimum floored path must log a warning so 'why is ML
    off' is answerable, instead of silently returning a dormant state."""
    import logging

    db = str(tmp_path / "thin_logged.db")
    make_samples_hourly_db(db, 28)
    newest = _SYNTHETIC_START + timedelta(hours=28 * 24 - 1)
    since_iso = (newest - timedelta(hours=12)).isoformat()

    with caplog.at_level(logging.WARNING, logger="trainer"):
        st = train_once(db, since_iso=since_iso)

    assert st.ready is False
    assert "train_since" in caplog.text or since_iso in caplog.text


# ---------------------------------------------------------------------------
# Rolling backtest window (_backtest_window, train_once)
# ---------------------------------------------------------------------------


def test_backtest_max_origins_sized_off_promotion_gate():
    """_BACKTEST_MAX_ORIGINS must give headroom above the promotion gate
    (MIN_HORIZON_ORIGINS_24H) so a multi-day recorder outage inside the
    window cannot drop the origin count below it — sized off the gate
    constant, not a bare literal, so the two cannot silently drift apart."""
    assert trainer._BACKTEST_MAX_ORIGINS == 2 * MIN_HORIZON_ORIGINS_24H


def test_backtest_window_keeps_recent_rows_drops_old():
    """_backtest_window keeps only rows within train_days + _BACKTEST_MAX_ORIGINS
    * test_days of the newest hour_ts, preserving row order and dropping
    everything older."""
    window_days = 14 + trainer._BACKTEST_MAX_ORIGINS * 3
    total_days = window_days + 16  # comfortably past the window edge, so some rows are dropped
    rows = [{"hour_ts": (_SYNTHETIC_START + timedelta(days=d)).isoformat()} for d in range(total_days)]

    windowed = trainer._backtest_window(rows, train_days=14, test_days=3)

    cutoff = _SYNTHETIC_START + timedelta(days=total_days - 1) - timedelta(days=window_days)
    assert windowed
    assert len(windowed) < len(rows)
    assert all(datetime.fromisoformat(r["hour_ts"]) >= cutoff for r in windowed)
    # order preserved relative to the input
    assert windowed == [r for r in rows if datetime.fromisoformat(r["hour_ts"]) >= cutoff]


def test_backtest_window_empty_input_returned_as_is():
    """L4: empty input is returned as-is (no crash, no allocation surprise)."""
    rows = []
    assert trainer._backtest_window(rows, train_days=14, test_days=3) is rows


def test_backtest_window_all_rows_missing_hour_ts_returns_empty():
    """L4: rows entirely lacking hour_ts parse to nothing → []."""
    rows = [{"house_load_mean": 100.0}, {"house_load_mean": 200.0}]
    assert trainer._backtest_window(rows, train_days=14, test_days=3) == []


def test_backtest_window_drops_unparseable_hour_ts_keeps_parseable():
    """L4: an unparseable hour_ts row is dropped (per-row ValueError catch)
    while a parseable row alongside it is kept."""
    good = {"hour_ts": _SYNTHETIC_START.isoformat()}
    bad = {"hour_ts": "not-a-timestamp"}
    rows = [good, bad]

    windowed = trainer._backtest_window(rows, train_days=14, test_days=3)

    assert windowed == [good]


def test_backtest_window_mixed_naive_aware_hour_ts_does_not_raise():
    """A DB mixing tz-aware and tz-naive hour_ts must not raise TypeError out of
    _backtest_window ('can't compare offset-naive and offset-aware datetimes' at
    the max()/comparison) — degrade to the old full-history behavior instead:
    return rows unchanged."""
    rows = [{"hour_ts": (_SYNTHETIC_START + timedelta(days=d)).isoformat()} for d in range(5)]
    # Strip the tz offset from one row to create a naive/aware mix.
    rows[2]["hour_ts"] = (_SYNTHETIC_START + timedelta(days=2)).replace(tzinfo=None).isoformat()

    windowed = trainer._backtest_window(rows, train_days=14, test_days=3)

    assert windowed == rows


def test_train_once_survives_mixed_tz_hour_ts(tmp_path):
    """H1: a recorder DB whose samples_hourly mixes naive and aware hour_ts must
    not crash train_once's fit/backtest path. Before the fix, _backtest_window's
    TypeError escaped into train_once's outer except Exception and killed the
    served model (ready=False) even though fit had already succeeded.

    The naive row also has its target nulled: build_feature_matrix only calls
    encode_calendar_features (which raises ValueError on a naive timestamp) for
    rows with a real target, so a targetless naive row reaches fit() safely and
    isolates the _backtest_window TypeError as the sole failure mode under test.
    """
    db = str(tmp_path / "mixed_tz.db")
    make_samples_hourly_db(db, 28)

    # Strip the tz offset from the oldest row's hour_ts and null its target to
    # create the mix without tripping build_feature_matrix's own tz check.
    oldest_aware = _SYNTHETIC_START.isoformat()
    oldest_naive = _SYNTHETIC_START.replace(tzinfo=None).isoformat()
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "UPDATE samples_hourly SET hour_ts = ?, house_load_kwh_sum = NULL, house_load_mean = NULL "
            "WHERE hour_ts = ?",
            (oldest_naive, oldest_aware),
        )
        conn.commit()
    finally:
        conn.close()

    st = train_once(db)

    assert st.ready is True
    assert st.model is not None


def test_train_once_backtests_on_window(tmp_path, monkeypatch):
    """train_once must feed walk_forward_hgbr only the windowed rows (the gate
    measures the current model) while model.fit sees the full floored rows
    (the served model keeps all history) — pinned by capturing fit's own rows,
    not just the n_rows bookkeeping field."""
    # Comfortably exceeds DEFAULT_TRAIN_DAYS + _BACKTEST_MAX_ORIGINS * DEFAULT_BACKTEST_TEST_DAYS
    # so the window genuinely drops rows regardless of how the cap is sized.
    total_days = 90
    db = str(tmp_path / "window.db")
    make_samples_hourly_db(db, total_days)

    captured = {}

    def _fake_walk_forward_hgbr(rows, **kwargs):
        captured["backtest_rows"] = rows
        return {
            "model_mae": None,
            "baseline_mae": None,
            "model_rmse": None,
            "baseline_rmse": None,
            "n_test": 0,
            "improvement_pct": 0.0,
            "horizon_energy_mae_24h": None,
            "horizon_energy_mae_12h": None,
            "baseline_horizon_energy_mae_24h": None,
            "pinball_p50": None,
            "pinball_p80": None,
        }

    monkeypatch.setattr(trainer, "walk_forward_hgbr", _fake_walk_forward_hgbr)

    real_fit = HGBRQuantileModel.fit

    def _capturing_fit(self, hourly_rows, quantiles=(0.5, 0.8)):
        captured["fit_rows"] = hourly_rows
        return real_fit(self, hourly_rows, quantiles=quantiles)

    monkeypatch.setattr(HGBRQuantileModel, "fit", _capturing_fit)

    st = train_once(db)

    assert st.n_rows == total_days * 24
    assert len(captured["fit_rows"]) == total_days * 24  # fit genuinely saw everything

    newest = _SYNTHETIC_START + timedelta(hours=total_days * 24 - 1)
    cutoff = newest - timedelta(days=DEFAULT_TRAIN_DAYS + trainer._BACKTEST_MAX_ORIGINS * DEFAULT_BACKTEST_TEST_DAYS)
    earliest_captured = min(datetime.fromisoformat(r["hour_ts"]) for r in captured["backtest_rows"])
    assert earliest_captured >= cutoff
    assert len(captured["backtest_rows"]) < len(captured["fit_rows"])  # genuinely windowed
