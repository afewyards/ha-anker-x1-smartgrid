"""Tests for P3-T3: RemoteForecastPredictor wired into Controller.

Covers:
  (a) addon_enabled=True + fetched non-empty map → active_model_name=="remote"
      and predictor is a RemoteForecastPredictor.
  (b) addon_enabled=False → remote tier skipped; falls through to bucketed/profile.
  (c) fetch returns None (add-on down) → falls through; active_model_name != "remote".
  (d) fetch fires at most once per clock-hour (guard).
  (e) C2 alignment: build_hours_payload → map → predict(HH:23:07) resolves to HH:00 entry.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone, timedelta, UTC
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.anker_x1_smartgrid import const
from custom_components.anker_x1_smartgrid.controller import Controller
from custom_components.anker_x1_smartgrid.remote_forecast import (
    RemoteForecastPredictor,
    build_hours_payload,
)
from tests.helpers import StubActuator as _StubActuator
from tests.helpers import StubHass as _StubHass
from tests.helpers import StubStore as _StubStore

# ---------------------------------------------------------------------------
# Base timestamp: hour 11 UTC on a clean day (not multiple of 6 → no purge guard)
# ---------------------------------------------------------------------------
_NOW = datetime(2026, 6, 20, 11, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Minimal stub infrastructure (mirrors test_controller.py / test_controller_phase2.py)
#
# _StubActuator/_StubHass/_StubStore migrated to helpers (aliased to keep call
# sites unchanged). _StubRecorder is kept local: unlike helpers.StubRecorder,
# every read method here unconditionally returns empty/None regardless of
# append() calls, deliberately decoupling these tick()-based fetch-guard tests
# from retrain/model-selection side effects — a genuine behavioural
# difference, not a copy-paste duplicate.
# ---------------------------------------------------------------------------


class _StubRecorder:
    """Minimal recorder stub — returns empty data on all read paths."""

    def append(self, row):
        pass

    def append_decision(self, **kwargs):
        pass

    def purge_older_than(self, ts, days):
        pass

    def purge_decisions_older_than(self, cutoff):
        pass

    def rollup_hours(self, now_iso):
        return 0

    def purge_hourly_older_than(self, cutoff):
        return 0

    def wal_checkpoint(self) -> None:
        pass

    def read_load_samples(self, since_iso=None):
        return []

    def read_persons_home_samples(self, since_iso=None):
        return []

    def read_feature_rows(self, since_iso=None):
        return []

    def read_hourly_rows(self, since_iso=None):
        return []

    def upsert_daily_regret(self, **kwargs):
        pass

    def read_latest_daily_regret(self):
        return None

    def read_daily_regret_range(self, *a, **kw):
        return []

    def read_decisions(self, *a, **kw):
        return []


# ---------------------------------------------------------------------------
# Minimal data dict — same shape used by test_controller.py
# ---------------------------------------------------------------------------

_BASE_DATA = {
    const.CONF_ENT_SOC: "sensor.soc",
    const.CONF_ENT_METER_POWER: "sensor.meter_power",
    const.CONF_ENT_PRICE: "sensor.price",
    const.CONF_ENT_PV_TODAY: [],
    const.CONF_ENT_PV_TOMORROW: [],
    const.CONF_ENT_SUN: "sun.sun",
    const.CONF_ENT_BATTERY_POWER: "sensor.batt",
    const.CONF_ENT_PV_POWER: "sensor.pv",
    const.CONF_ENT_IRRADIANCE: "sensor.irr",
    const.CONF_ENT_SETPOINT: "number.setpoint",
    const.CONF_ENT_ENGAGE: "switch.engage",
    const.CONF_ENT_WORKMODE: "select.workmode",
}


def _make_controller(*, addon_enabled: bool = False) -> Controller:
    """Build a Controller with addon_enabled flag in cfg."""
    data = dict(_BASE_DATA)
    data["addon_enabled"] = addon_enabled
    data["addon_url"] = "http://test-addon:8099"
    data["addon_timeout"] = 5
    return Controller(
        hass=_StubHass(),
        data=data,
        recorder=_StubRecorder(),
        actuator=_StubActuator(),
        store=_StubStore(),
    )


def _sample_forecast_map(hour: datetime) -> dict:
    """Build a minimal non-empty forecast map with one entry at *hour* (top-of-hour UTC)."""
    hour_key = hour.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    return {hour_key: (300.0, 450.0)}


# ---------------------------------------------------------------------------
# (a) addon_enabled=True + non-empty map → Tier-0 selected
# ---------------------------------------------------------------------------


def test_retrain_sync_tier0_selects_remote_predictor():
    """When addon_enabled and a non-empty map is cached, _retrain_sync picks Tier-0."""
    ctrl = _make_controller(addon_enabled=True)
    ctrl._remote_forecast_map = _sample_forecast_map(_NOW)

    # _retrain_sync is synchronous; call directly.
    ctrl._retrain_sync(since_iso=(_NOW - timedelta(days=30)).isoformat())

    assert ctrl.active_model_name == "remote", f"Expected 'remote', got '{ctrl.active_model_name}'"
    assert isinstance(ctrl.predictor, RemoteForecastPredictor), (
        f"Expected RemoteForecastPredictor, got {type(ctrl.predictor)}"
    )


# ---------------------------------------------------------------------------
# (b) addon_enabled=False → Tier-0 skipped; falls through to bucketed/profile
# ---------------------------------------------------------------------------


def test_retrain_sync_skips_remote_when_addon_disabled():
    """When addon_enabled=False, _retrain_sync ignores the cached map entirely."""
    ctrl = _make_controller(addon_enabled=False)
    # Plant a non-empty map — should be ignored
    ctrl._remote_forecast_map = _sample_forecast_map(_NOW)

    ctrl._retrain_sync(since_iso=(_NOW - timedelta(days=30)).isoformat())

    assert ctrl.active_model_name != "remote", "Remote tier must be skipped when addon_enabled=False"
    assert not isinstance(ctrl.predictor, RemoteForecastPredictor)


# ---------------------------------------------------------------------------
# (c) fetch returns None (add-on down) → falls through to existing chain
# ---------------------------------------------------------------------------


def test_retrain_sync_skips_remote_when_map_is_none():
    """When _remote_forecast_map is None (fetch never succeeded), Tier-0 is bypassed."""
    ctrl = _make_controller(addon_enabled=True)
    assert ctrl._remote_forecast_map is None  # default

    ctrl._retrain_sync(since_iso=(_NOW - timedelta(days=30)).isoformat())

    assert ctrl.active_model_name != "remote"
    assert not isinstance(ctrl.predictor, RemoteForecastPredictor)


def test_retrain_sync_skips_remote_when_map_is_empty():
    """An empty dict (all predictions filtered) also falls through — empty is falsy."""
    ctrl = _make_controller(addon_enabled=True)
    ctrl._remote_forecast_map = {}  # empty dict from partial decode

    ctrl._retrain_sync(since_iso=(_NOW - timedelta(days=30)).isoformat())

    assert ctrl.active_model_name != "remote"


# ---------------------------------------------------------------------------
# (d) fetch fires at most once per clock-hour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_fetch_fires_at_most_once_per_clock_hour():
    """Two ticks in the same clock-hour must call fetch_forecast exactly once."""
    ctrl = _make_controller(addon_enabled=True)

    # Pre-seed the _last_remote_forecast_hour so the guard fires on the FIRST tick,
    # then the second tick (same hour) is suppressed.
    # Both ticks use _NOW (hour=11).
    tick_now = _NOW  # hour 11

    dummy_map = _sample_forecast_map(tick_now)

    # Patch away the blocking/network parts so tick() can run in test.
    with (
        patch(
            "custom_components.anker_x1_smartgrid.controller.fetch_forecast",
            new_callable=AsyncMock,
            return_value=dummy_map,
        ) as mock_fetch,
        patch(
            "custom_components.anker_x1_smartgrid.controller.async_get_clientsession",
            return_value=object(),
        ),
        patch(
            "custom_components.anker_x1_smartgrid.coordinator.read_hourly_weather_forecast",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "homeassistant.util.dt.utcnow",
            return_value=tick_now,
        ),
    ):
        await ctrl.tick()
        await ctrl.tick()

    # fetch_forecast must have been called exactly once despite two ticks.
    assert mock_fetch.call_count == 1, f"Expected 1 fetch_forecast call, got {mock_fetch.call_count}"


@pytest.mark.asyncio
async def test_tick_fetch_fires_again_on_new_clock_hour():
    """A tick at a new clock-hour must trigger a second fetch."""
    ctrl = _make_controller(addon_enabled=True)

    hour_a = _NOW  # hour 11
    hour_b = _NOW.replace(hour=12)  # hour 12

    dummy_map = _sample_forecast_map(hour_a)

    call_count = 0

    async def _counting_fetch(session, url, timeout, payload):
        nonlocal call_count
        call_count += 1
        return dummy_map

    with (
        patch(
            "custom_components.anker_x1_smartgrid.controller.fetch_forecast",
            side_effect=_counting_fetch,
        ),
        patch(
            "custom_components.anker_x1_smartgrid.controller.async_get_clientsession",
            return_value=object(),
        ),
        patch(
            "custom_components.anker_x1_smartgrid.coordinator.read_hourly_weather_forecast",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        with patch("homeassistant.util.dt.utcnow", return_value=hour_a):
            await ctrl.tick()

        with patch("homeassistant.util.dt.utcnow", return_value=hour_b):
            await ctrl.tick()

    assert call_count == 2, f"Expected 2 fetch calls (one per clock-hour), got {call_count}"


# ---------------------------------------------------------------------------
# (e) C2 alignment: build_hours_payload + predict with minute/second offset
# ---------------------------------------------------------------------------


def test_build_hours_payload_normalises_to_top_of_hour():
    """build_hours_payload always emits ts at HH:00:00, regardless of input sub-hour."""
    # Provide a forecast entry with a non-zero minute (simulating an off-hour source).
    entry_dt = datetime(2026, 6, 20, 14, 23, 7, tzinfo=UTC)
    weather_forecast = [
        {
            "datetime": entry_dt,
            "temp_forecast": 18.5,
            "cloud_cover": 40.0,
            "humidity": 65.0,
            "wind_speed": 3.2,
        }
    ]
    payload = build_hours_payload(weather_forecast)

    assert len(payload) == 1
    ts = payload[0]["ts"]
    # ts must represent HH:00:00+00:00
    parsed = datetime.fromisoformat(ts)
    assert parsed.minute == 0
    assert parsed.second == 0
    assert parsed.microsecond == 0
    assert parsed.hour == 14


def test_c2_alignment_predict_with_offset_resolves_to_top_of_hour_entry():
    """RemoteForecastPredictor.predict(HH:23:07) resolves to the HH:00 map entry.

    This is the key C2 alignment regression: build_intervals calls predict with
    start = now + i*step carrying live minutes/seconds.  The predictor must round
    down and hit the HH:00 key built by build_hours_payload.
    """
    # Build a weather forecast for 14:00 UTC (top-of-hour already, as HA emits it).
    base_hour = datetime(2026, 6, 20, 14, 0, tzinfo=UTC)
    weather_forecast = [
        {
            "datetime": base_hour,
            "temp_forecast": 20.0,
            "cloud_cover": 30.0,
            "humidity": 60.0,
            "wind_speed": 2.0,
        }
    ]
    payload = build_hours_payload(weather_forecast)
    assert len(payload) == 1, "Payload must have exactly one entry"

    # Simulate what fetch_forecast returns: parse ts back to a datetime key.
    # In real usage, the add-on server receives the payload and sends back predictions
    # with ts at HH:00.  We construct the map as fetch_forecast would decode it.
    key_dt = datetime.fromisoformat(payload[0]["ts"])
    if key_dt.tzinfo is None:
        key_dt = key_dt.replace(tzinfo=UTC)
    forecast_map = {key_dt: (350.0, 500.0)}  # (p50, p80)

    predictor = RemoteForecastPredictor(forecast_map)

    # Call predict with the same hour but with minutes/seconds carried from live clock.
    when_with_offset = datetime(2026, 6, 20, 14, 23, 7, tzinfo=UTC)

    p50 = predictor.predict(when_with_offset, temp=None, fallback_w=999.0, quantile=0.5)
    p80 = predictor.predict(when_with_offset, temp=None, fallback_w=999.0, quantile=0.8)

    assert p50 == 350.0, f"P50 should resolve to map entry 350.0, got {p50} — C2 alignment broken"
    assert p80 == 500.0, f"P80 should resolve to map entry 500.0, got {p80} — C2 alignment broken"

    # Also verify that a different hour does NOT hit the map (fallback returned).
    when_wrong_hour = datetime(2026, 6, 20, 15, 23, 7, tzinfo=UTC)
    result_miss = predictor.predict(when_wrong_hour, temp=None, fallback_w=999.0, quantile=0.5)
    assert result_miss == 999.0, f"Different hour must fall back to 999.0, got {result_miss}"


def test_build_hours_payload_empty_input():
    """Empty weather forecast → empty payload."""
    assert build_hours_payload([]) == []


def test_build_hours_payload_missing_datetime_skipped():
    """Entries without a 'datetime' key are silently skipped."""
    payload = build_hours_payload([{"temp_forecast": 10.0}])
    assert payload == []


def test_build_hours_payload_irradiance_always_none():
    """irradiance is always None in the payload (supplied by the add-on from its own data)."""
    entry_dt = datetime(2026, 6, 20, 10, 0, tzinfo=UTC)
    payload = build_hours_payload(
        [{"datetime": entry_dt, "temp_forecast": 15.0, "cloud_cover": 50.0, "humidity": 70.0, "wind_speed": 4.0}]
    )
    assert payload[0]["irradiance"] is None


def test_build_hours_payload_passes_weather_fields():
    """All four weather fields are passed through from the forecast entry."""
    entry_dt = datetime(2026, 6, 20, 9, 0, tzinfo=UTC)
    payload = build_hours_payload(
        [{"datetime": entry_dt, "temp_forecast": 12.5, "cloud_cover": 25.0, "humidity": 55.0, "wind_speed": 7.5}]
    )
    assert payload[0]["temp_forecast"] == 12.5
    assert payload[0]["cloud_cover"] == 25.0
    assert payload[0]["humidity"] == 55.0
    assert payload[0]["wind_speed"] == 7.5


# ---------------------------------------------------------------------------
# (f) ML-status visibility: hourly /health poll threaded into last_status
# ---------------------------------------------------------------------------


@contextmanager
def _tick_env(health_fn, *, now=_NOW):
    """Patch the network/blocking edges of tick() and install *health_fn*.

    ``fetch_forecast`` is stubbed to return None so the Tier-0 remote predictor
    never activates — these tests are about the health poll, not model selection.
    """
    with (
        patch(
            "custom_components.anker_x1_smartgrid.controller.fetch_health",
            side_effect=health_fn,
        ),
        patch(
            "custom_components.anker_x1_smartgrid.controller.fetch_forecast",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "custom_components.anker_x1_smartgrid.controller.async_get_clientsession",
            return_value=object(),
        ),
        patch(
            "custom_components.anker_x1_smartgrid.coordinator.read_hourly_weather_forecast",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch("homeassistant.util.dt.utcnow", return_value=now),
    ):
        yield


@pytest.mark.asyncio
async def test_health_polled_and_status_attrs_present():
    """After a tick with addon_enabled, last_status carries ml-status attrs."""
    ctrl = _make_controller(addon_enabled=True)
    health = {
        "ready": False,
        "promoted": False,
        "n_rows": 622,
        "last_trained": "2026-07-21T01:00:00+00:00",
    }

    async def fake_health(session, url, timeout):
        return health

    with _tick_env(fake_health):
        await ctrl.tick()

    status = ctrl.last_status
    assert status["addon_reachable"] is True
    assert status["addon_n_rows"] == 622
    assert status["ml_status"].startswith(("ML in", "collecting"))
    assert status["coverage_required"] == 21


@pytest.mark.asyncio
async def test_health_unreachable_flagged():
    """fetch_health returning None (add-on down) surfaces as unreachable."""
    ctrl = _make_controller(addon_enabled=True)

    async def fake_health(session, url, timeout):
        return None

    with _tick_env(fake_health):
        await ctrl.tick()

    status = ctrl.last_status
    assert status["addon_reachable"] is False
    assert status["ml_status"] == "⚠ unreachable"


@pytest.mark.asyncio
async def test_health_fetch_failure_never_breaks_tick():
    """A raising fetch_health must be swallowed; the tick still produces a plan."""
    ctrl = _make_controller(addon_enabled=True)

    async def exploding_health(session, url, timeout):
        raise RuntimeError("must be swallowed by the tick backstop")

    with _tick_env(exploding_health):
        await ctrl.tick()  # must not raise

    assert "setpoint_w" in ctrl.last_status


@pytest.mark.asyncio
async def test_health_polled_before_forecast_predict_path():
    """A blowing-up forecast path must not skip the health poll.

    Locks ordering requirement (1): the health poll is the FIRST statement in
    the hourly add-on block's ``try:``, so reachability keeps updating even
    when the predict path fails.
    """
    ctrl = _make_controller(addon_enabled=True)
    health = {"ready": True, "promoted": False, "n_rows": 900, "last_trained": None}

    async def fake_health(session, url, timeout):
        return health

    async def exploding_forecast(session, url, timeout, payload):
        raise RuntimeError("predict path down")

    with (
        patch(
            "custom_components.anker_x1_smartgrid.controller.fetch_health",
            side_effect=fake_health,
        ),
        patch(
            "custom_components.anker_x1_smartgrid.controller.fetch_forecast",
            side_effect=exploding_forecast,
        ),
        patch(
            "custom_components.anker_x1_smartgrid.controller.async_get_clientsession",
            return_value=object(),
        ),
        patch(
            "custom_components.anker_x1_smartgrid.coordinator.read_hourly_weather_forecast",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch("homeassistant.util.dt.utcnow", return_value=_NOW),
    ):
        await ctrl.tick()

    assert ctrl.last_status["addon_reachable"] is True
    assert ctrl.last_status["addon_n_rows"] == 900


@pytest.mark.asyncio
async def test_health_poll_failure_reports_unreachable_not_stale():
    """A health poll that raises AFTER a prior successful poll must report
    unreachable with a FRESH timestamp — not silently keep the prior
    successful reading (the stale-health regression).
    """
    ctrl = _make_controller(addon_enabled=True)
    good_health = {"ready": False, "promoted": False, "n_rows": 10, "last_trained": "t0"}

    hour_a = _NOW  # hour 11, poll succeeds
    hour_b = _NOW.replace(hour=12)  # next clock-hour, poll raises

    call_count = 0

    async def flaky_health(session, url, timeout):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return good_health
        raise RuntimeError("add-on died mid-session")

    with (
        patch(
            "custom_components.anker_x1_smartgrid.controller.fetch_health",
            side_effect=flaky_health,
        ),
        patch(
            "custom_components.anker_x1_smartgrid.controller.fetch_forecast",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "custom_components.anker_x1_smartgrid.controller.async_get_clientsession",
            return_value=object(),
        ),
        patch(
            "custom_components.anker_x1_smartgrid.coordinator.read_hourly_weather_forecast",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        with patch("homeassistant.util.dt.utcnow", return_value=hour_a):
            await ctrl.tick()
        assert ctrl.last_status["addon_reachable"] is True  # sanity: first poll succeeded

        with patch("homeassistant.util.dt.utcnow", return_value=hour_b):
            await ctrl.tick()  # must not raise despite the exploding health poll

    status = ctrl.last_status
    assert status["addon_reachable"] is False, "must not silently keep the prior successful reading"
    assert status["ml_status"] == "⚠ unreachable"
    assert ctrl._addon_health_ts == hour_b, "timestamp must reflect THIS failed attempt, not the stale success"


@pytest.mark.asyncio
async def test_coverage_counted_even_when_remote_tier_active():
    """Coverage keeps counting after Tier-0 activates.

    Locks ordering requirement (2): the coverage count sits ABOVE the Tier-0
    early return in ``_retrain_sync``.  With a cached remote map the method
    returns immediately, yet coverage_days must still be refreshed.
    """
    ctrl = _make_controller(addon_enabled=True)
    ctrl._remote_forecast_map = _sample_forecast_map(_NOW)

    # Two lag-complete rows 168 h apart on two distinct Amsterdam dates.
    base = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)
    rows = [
        {"hour_ts": base.isoformat()},
        {"hour_ts": (base + timedelta(hours=168)).isoformat()},
        {"hour_ts": (base + timedelta(hours=24)).isoformat()},
        {"hour_ts": (base + timedelta(hours=192)).isoformat()},
    ]
    ctrl._recorder.read_hourly_rows = lambda since_iso=None: rows

    ctrl._retrain_sync(since_iso=(_NOW - timedelta(days=30)).isoformat())

    assert ctrl.active_model_name == "remote", "precondition: Tier-0 must have short-circuited"
    assert ctrl.coverage_lag_complete_days == 2
