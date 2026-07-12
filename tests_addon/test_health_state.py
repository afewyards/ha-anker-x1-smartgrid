"""Tests for addon/anker_x1_forecast/health.py.

Deliberately imports ONLY health (and trainer for TrainState) — never server.py,
never fastapi/uvicorn — so these tests run cleanly in the repo .venv.
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone, UTC
from pathlib import Path
from zoneinfo import ZoneInfo

# conftest.py inserts addon/anker_x1_forecast onto sys.path
from health import build_health_payload, read_options, seconds_until_next_run
from trainer import TrainState

_TZ_AMS = ZoneInfo("Europe/Amsterdam")

# ---------------------------------------------------------------------------
# build_health_payload — READY state
# ---------------------------------------------------------------------------


def _ready_state() -> TrainState:
    return TrainState(
        ready=True,
        promoted=True,
        last_trained=datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC),
        n_rows=500,
        metrics={"mae_p50": 42.1, "mae_p80": 55.3},
        model=object(),
    )


def _dormant_state() -> TrainState:
    return TrainState(
        ready=False,
        promoted=False,
        last_trained=None,
        n_rows=0,
        metrics=None,
        model=None,
    )


def test_build_health_payload_ready_all_keys_present():
    state = _ready_state()
    payload = build_health_payload(state, "1.4.0", "3.11.0 (default)")
    expected_keys = {
        "ready",
        "promoted",
        "last_trained",
        "n_rows",
        "metrics",
        "sklearn_version",
        "python_version",
        "db_readable",
    }
    assert set(payload.keys()) == expected_keys


def test_build_health_payload_ready_values():
    state = _ready_state()
    payload = build_health_payload(state, "1.4.0", "3.11.0")
    assert payload["ready"] is True
    assert payload["promoted"] is True
    assert payload["last_trained"] == "2024-06-01T12:00:00+00:00"
    assert payload["n_rows"] == 500
    assert payload["metrics"] == {"mae_p50": 42.1, "mae_p80": 55.3}
    assert payload["sklearn_version"] == "1.4.0"
    assert payload["python_version"] == "3.11.0"


def test_build_health_payload_last_trained_is_isoformat_string():
    state = _ready_state()
    payload = build_health_payload(state, "1.4.0", "3.11.0")
    # Must be a string, not a datetime
    assert isinstance(payload["last_trained"], str)
    # Must round-trip via fromisoformat
    dt = datetime.fromisoformat(payload["last_trained"])
    assert dt == state.last_trained


# ---------------------------------------------------------------------------
# build_health_payload — DORMANT state
# ---------------------------------------------------------------------------


def test_build_health_payload_dormant_last_trained_is_none():
    state = _dormant_state()
    payload = build_health_payload(state, "1.4.0", "3.11.0")
    assert payload["last_trained"] is None


def test_build_health_payload_dormant_metrics_is_empty_dict():
    state = _dormant_state()
    payload = build_health_payload(state, "1.4.0", "3.11.0")
    assert payload["metrics"] == {}


def test_build_health_payload_dormant_ready_and_promoted_false():
    state = _dormant_state()
    payload = build_health_payload(state, "1.4.0", "3.11.0")
    assert payload["ready"] is False
    assert payload["promoted"] is False


# ---------------------------------------------------------------------------
# build_health_payload — db_readable (H3b)
# ---------------------------------------------------------------------------


def test_health_payload_includes_db_readable():
    from types import SimpleNamespace

    state = SimpleNamespace(ready=False, promoted=False, last_trained=None, n_rows=0, metrics=None)
    payload = build_health_payload(state, "1.5.2", "3.12", db_readable=False)
    assert payload["db_readable"] is False


# ---------------------------------------------------------------------------
# read_options
# ---------------------------------------------------------------------------


def test_read_options_returns_defaults_when_file_missing():
    opts = read_options(path="/nonexistent/path/options.json")
    assert opts["db_path"] == "/config/anker_x1_smartgrid.db"
    assert opts["retrain_hour"] == 3


def test_read_options_merges_written_file_over_defaults():
    custom = {"db_path": "/data/custom.db"}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(custom, f)
        tmp_path = f.name

    opts = read_options(path=tmp_path)
    assert opts["db_path"] == "/data/custom.db"
    # Default for keys not in the file is preserved
    assert opts["retrain_hour"] == 3

    Path(tmp_path).unlink(missing_ok=True)


def test_read_options_custom_retrain_hour():
    custom = {"retrain_hour": 5}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(custom, f)
        tmp_path = f.name

    opts = read_options(path=tmp_path)
    assert opts["retrain_hour"] == 5
    assert opts["db_path"] == "/config/anker_x1_smartgrid.db"  # default preserved

    Path(tmp_path).unlink(missing_ok=True)


def test_read_options_returns_defaults_on_malformed_json():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        f.write("{not valid")
        tmp_path = f.name

    opts = read_options(path=tmp_path)
    assert opts["db_path"] == "/config/anker_x1_smartgrid.db"
    assert opts["retrain_hour"] == 3

    Path(tmp_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# seconds_until_next_run
# ---------------------------------------------------------------------------


def _ams(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """Construct a tz-aware datetime in Europe/Amsterdam."""
    return datetime(year, month, day, hour, minute, tzinfo=_TZ_AMS)


def test_seconds_until_next_run_ahead_today():
    # 01:00 Amsterdam, retrain_hour=3 → next run is today at 03:00 → ~2h
    now = _ams(2024, 1, 1, 1, 0)
    secs = seconds_until_next_run(now, retrain_hour=3)
    expected = 2 * 3600  # exactly 2 hours
    assert abs(secs - expected) < 5, f"Expected ~{expected}s, got {secs}s"


def test_seconds_until_next_run_past_today():
    # 04:00 Amsterdam, retrain_hour=3 → next run is TOMORROW at 03:00 → ~23h
    now = _ams(2024, 1, 1, 4, 0)
    secs = seconds_until_next_run(now, retrain_hour=3)
    expected = 23 * 3600  # exactly 23 hours
    assert abs(secs - expected) < 5, f"Expected ~{expected}s, got {secs}s"


def test_seconds_until_next_run_exactly_at_hour():
    # 03:00:00 Amsterdam exactly → candidate == now → rolls to tomorrow → 24h
    now = _ams(2024, 1, 1, 3, 0)
    secs = seconds_until_next_run(now, retrain_hour=3)
    expected = 24 * 3600
    assert abs(secs - expected) < 5, f"Expected ~{expected}s, got {secs}s"


def test_seconds_until_next_run_utc_input():
    # Supply a UTC datetime and confirm it works correctly.
    # 00:00 UTC on Jan 1 2024 = 01:00 Amsterdam (CET, UTC+1)
    # retrain_hour=3 → today at 03:00 AMS → 2h from 01:00 AMS
    now_utc = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
    secs = seconds_until_next_run(now_utc, retrain_hour=3)
    expected = 2 * 3600
    assert abs(secs - expected) < 5, f"Expected ~{expected}s, got {secs}s"


def test_seconds_until_next_run_always_positive():
    """Result must always be > 0."""
    for hour_now in range(24):
        now = _ams(2024, 6, 15, hour_now, 30)
        secs = seconds_until_next_run(now, retrain_hour=3)
        assert secs > 0, f"Got non-positive {secs}s for hour_now={hour_now}"


# ---------------------------------------------------------------------------
# read_options — retrain_hour clamp/validate
# ---------------------------------------------------------------------------


def test_read_options_clamps_out_of_range_retrain_hour():
    import json
    import tempfile
    from pathlib import Path

    for bad, _ in [(27, 23), (-5, 0)]:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"retrain_hour": bad}, f)
            tmp = f.name
        opts = read_options(path=tmp)
        assert 0 <= opts["retrain_hour"] <= 23
        Path(tmp).unlink(missing_ok=True)


def test_read_options_defaults_retrain_hour_on_nonint():
    import json
    import tempfile
    from pathlib import Path

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({"retrain_hour": "3am"}, f)
        tmp = f.name
    opts = read_options(path=tmp)
    assert opts["retrain_hour"] == 3  # falls back to default, never raises
    Path(tmp).unlink(missing_ok=True)
