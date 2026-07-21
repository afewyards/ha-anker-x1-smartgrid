"""Tests for ml_status — coverage counter + (Task 2) status-string builder."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.anker_x1_smartgrid import ml_status


def _rows(start: datetime, n_hours: int) -> list[dict]:
    return [
        {"hour_ts": (start + timedelta(hours=i)).isoformat()}
        for i in range(n_hours)
    ]


def test_count_empty():
    assert ml_status.count_lag_complete_days([]) == 0


def test_count_continuous_30_days():
    # 30 full days: lag-complete rows start at +168h → local dates 06-08..07-01
    # (30*24=720 UTC rows; the trailing 22:00Z/23:00Z rows spill into the next
    # Amsterdam calendar date under CEST, adding one extra date) = 24 dates
    rows = _rows(datetime(2026, 6, 1, 0, 0, tzinfo=UTC), 30 * 24)
    assert ml_status.count_lag_complete_days(rows) == 24


def test_count_below_seed_week_is_zero():
    rows = _rows(datetime(2026, 6, 1, 0, 0, tzinfo=UTC), 6 * 24)
    assert ml_status.count_lag_complete_days(rows) == 0


def test_gap_breaks_lag_completeness():
    # Remove the entire first day: rows at +168h..+191h lose their lag partner
    rows = _rows(datetime(2026, 6, 1, 0, 0, tzinfo=UTC), 30 * 24)
    rows = rows[24:]
    # Same tail-spillover date as above still counts, minus the deleted day's
    # lag-complete date = 23
    assert ml_status.count_lag_complete_days(rows) == 23


def test_malformed_ts_skipped():
    rows = _rows(datetime(2026, 6, 1, 0, 0, tzinfo=UTC), 8 * 24)
    rows.append({"hour_ts": "not-a-date"})
    rows.append({"hour_ts": None})
    # Must not raise; 8 days → lag-complete day 7 plus the CEST tail-spillover
    # date from the last two UTC hours = 2
    assert ml_status.count_lag_complete_days(rows) == 2


def test_amsterdam_date_counting():
    # 2026-06-08T22:00Z = 2026-06-09 00:00 Amsterdam (CEST) — the row's date
    # must be counted in LOCAL time, so a UTC-evening row lands on the next day.
    base = datetime(2026, 6, 1, 22, 0, tzinfo=UTC)
    rows = [{"hour_ts": base.isoformat()},
            {"hour_ts": (base + timedelta(hours=168)).isoformat()}]
    assert ml_status.count_lag_complete_days(rows) == 1  # the +168h row, dated 06-09 local


def test_parity_with_hgbr_is_ready():
    sklearn = pytest.importorskip("sklearn")  # dev venv only; import proves availability
    from custom_components.anker_x1_smartgrid.hgbr import HGBRQuantileModel

    model = HGBRQuantileModel()
    for n_days in (27, 28, 29, 35):
        rows = _rows(datetime(2026, 6, 1, 0, 0, tzinfo=UTC), n_days * 24)
        counter_ready = ml_status.count_lag_complete_days(rows) >= ml_status.COVERAGE_REQUIRED_DAYS
        assert counter_ready == model.is_ready(rows), f"diverged at {n_days} days"
