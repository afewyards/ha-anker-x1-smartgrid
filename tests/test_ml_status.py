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
    # 26 straddles the boundary from below (20 lag-complete dates, both False);
    # 27 straddles it from above (21 lag-complete dates, both True) — without a
    # below-threshold case this test only proves agreement above the boundary.
    for n_days in (26, 27, 28, 29, 35):
        rows = _rows(datetime(2026, 6, 1, 0, 0, tzinfo=UTC), n_days * 24)
        counter_ready = ml_status.count_lag_complete_days(rows) >= ml_status.COVERAGE_REQUIRED_DAYS
        assert counter_ready == model.is_ready(rows), f"diverged at {n_days} days"


NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
HEALTH_DORMANT = {"ready": False, "promoted": False, "n_rows": 622,
                  "last_trained": "2026-07-21T01:00:00+00:00"}
HEALTH_READY = {**HEALTH_DORMANT, "ready": True}
HEALTH_PROMOTED = {**HEALTH_DORMANT, "ready": True, "promoted": True}


def _attrs(**overrides):
    kw = dict(addon_enabled=True, addon_url="http://x:8099", health=HEALTH_DORMANT,
              health_ts=NOW, coverage_days=20, active_model="bucketed")
    kw.update(overrides)
    return ml_status.build_ml_status_attrs(**kw)


def test_status_addon_off():
    assert _attrs(addon_enabled=False)["ml_status"] == "add-on off"
    assert _attrs(addon_url="")["ml_status"] == "add-on off"
    assert _attrs(addon_enabled=False)["addon_configured"] is False


def test_status_addon_off_clears_stale_health_fields():
    """Not-configured must not leak a previous (enabled) session's health
    reading — health-derived fields all report None, not stale values."""
    a = _attrs(addon_enabled=False, health=HEALTH_PROMOTED, health_ts=NOW)
    assert a["ml_status"] == "add-on off"
    assert a["addon_reachable"] is None
    assert a["addon_ready"] is None
    assert a["addon_promoted"] is None
    assert a["addon_n_rows"] is None
    assert a["addon_last_trained"] is None
    assert a["last_health_check"] is None


def test_status_unreachable():
    a = _attrs(health=None)
    assert a["ml_status"] == "⚠ unreachable"
    assert a["addon_reachable"] is False
    assert a["addon_n_rows"] is None and a["addon_last_trained"] is None


def test_status_eta_countdown():
    a = _attrs()
    assert a["ml_status"] == "ML in ~1d"
    assert a["eta_days"] == 1
    assert a["coverage_days"] == 20 and a["coverage_required"] == 21
    assert a["addon_reachable"] is True and a["addon_ready"] is False


def test_status_eta_clamps_at_zero():
    assert _attrs(coverage_days=25)["eta_days"] == 0


def test_status_backtest_gate():
    assert _attrs(health=HEALTH_READY)["ml_status"] == "backtest gate"


def test_status_ml_active():
    a = _attrs(health=HEALTH_PROMOTED, active_model="remote")
    assert a["ml_status"] == "ML active"
    assert a["addon_promoted"] is True


def test_status_promoted_not_consumed():
    assert _attrs(health=HEALTH_PROMOTED)["ml_status"] == "⚠ promoted, not consumed"


def test_status_no_health_check_yet_falls_back_to_eta():
    # enabled but first tick hasn't polled yet: NOT flagged unreachable
    a = _attrs(health=None, health_ts=None)
    assert a["ml_status"] == "ML in ~1d"
    assert a["addon_reachable"] is None
    assert a["last_health_check"] is None


def test_status_collecting_data_when_no_coverage():
    assert _attrs(coverage_days=None)["ml_status"] == "collecting data"


def test_last_health_check_iso():
    assert _attrs()["last_health_check"] == NOW.isoformat()


# ---------------------------------------------------------------------------
# addon_n_rows / addon_last_trained bounding — defends the recorder's 16 KiB
# attribute cap against arbitrary add-on JSON passed straight through.
# ---------------------------------------------------------------------------


def test_addon_n_rows_int_passes_through():
    assert _attrs()["addon_n_rows"] == 622  # HEALTH_DORMANT's n_rows, unchanged


def test_addon_n_rows_numeric_string_coerced_to_int():
    a = _attrs(health={**HEALTH_DORMANT, "n_rows": "622"})
    assert a["addon_n_rows"] == 622


def test_addon_n_rows_non_numeric_becomes_none():
    a = _attrs(health={**HEALTH_DORMANT, "n_rows": {"garbage": True}})
    assert a["addon_n_rows"] is None


def test_addon_n_rows_bool_is_not_numeric():
    a = _attrs(health={**HEALTH_DORMANT, "n_rows": True})
    assert a["addon_n_rows"] is None


def test_addon_n_rows_nan_becomes_none():
    a = _attrs(health={**HEALTH_DORMANT, "n_rows": float("nan")})
    assert a["addon_n_rows"] is None


def test_addon_last_trained_oversized_string_truncated_to_64_chars():
    huge = "x" * 5000
    a = _attrs(health={**HEALTH_DORMANT, "last_trained": huge})
    assert a["addon_last_trained"] == "x" * 64


def test_addon_last_trained_non_string_coerced_and_bounded():
    a = _attrs(health={**HEALTH_DORMANT, "last_trained": 20260721})
    assert a["addon_last_trained"] == "20260721"


def test_addon_last_trained_none_stays_none():
    a = _attrs(health={**HEALTH_DORMANT, "last_trained": None})
    assert a["addon_last_trained"] is None
