"""Tests for remote_forecast.py — every fallback branch must be covered.

All HTTP interaction is faked via lightweight stub objects; no real network or
aiohttp dependency required.

Session/response stubs mirror the aiohttp async-context-manager protocol:
  ``session.post(url, json=...)`` returns an object usable as ``async with``.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone, UTC
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.anker_x1_smartgrid.remote_forecast import (
    RemoteForecastPredictor,
    build_hours_payload,
    fetch_forecast,
    persons_home_hour_of_week_means,
    project_persons_home,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BASE_URL = "http://localhost:8765"
_TIMEOUT = 5

# Two representative forecast hours.
_HOUR_A = datetime(2026, 6, 22, 10, 0, tzinfo=UTC)
_HOUR_B = datetime(2026, 6, 22, 11, 0, tzinfo=UTC)

_PAYLOAD = [
    {
        "ts": "2026-06-22T10:00:00+00:00",
        "temp_forecast": 18.5,
        "cloud_cover": 20.0,
        "humidity": 55.0,
        "wind_speed": 3.2,
        "irradiance": None,
    },
    {
        "ts": "2026-06-22T11:00:00+00:00",
        "temp_forecast": 19.0,
        "cloud_cover": 15.0,
        "humidity": 50.0,
        "wind_speed": 2.8,
        "irradiance": None,
    },
]

_PREDICTIONS = [
    {"ts": "2026-06-22T10:00:00Z", "p50_w": 400.0, "p80_w": 550.0},
    {"ts": "2026-06-22T11:00:00Z", "p50_w": 380.0, "p80_w": 520.0},
]

# ---------------------------------------------------------------------------
# Stub helpers
# ---------------------------------------------------------------------------


def _make_response(status: int, json_data: dict):
    """Return a fake aiohttp-style response (async context manager)."""
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data)
    return resp


def _make_session(resp):
    """Session whose post() works as ``async with session.post(...) as r:``."""
    session = MagicMock()

    @asynccontextmanager
    async def _post(*args, **kwargs):
        yield resp

    session.post = _post
    return session


def _make_session_raising(exc_factory):
    """Session whose post() raises *exc_factory()* when entered."""
    session = MagicMock()

    @asynccontextmanager
    async def _post(*args, **kwargs):
        raise exc_factory()
        yield  # pragma: no cover — unreachable, satisfies generator protocol

    session.post = _post
    return session


# ---------------------------------------------------------------------------
# (a) Happy path: 200 + ready && promoted → populated map + correct p50/p80
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_forecast_happy_path_returns_map():
    resp = _make_response(
        200,
        {
            "ready": True,
            "promoted": True,
            "predictions": _PREDICTIONS,
        },
    )
    session = _make_session(resp)
    result = await fetch_forecast(session, _BASE_URL, _TIMEOUT, _PAYLOAD)

    assert result is not None
    assert len(result) == 2
    assert result[_HOUR_A] == (400.0, 550.0)
    assert result[_HOUR_B] == (380.0, 520.0)


@pytest.mark.asyncio
async def test_remote_predictor_returns_p80_for_high_quantile():
    forecast_map = {_HOUR_A: (400.0, 550.0), _HOUR_B: (380.0, 520.0)}
    predictor = RemoteForecastPredictor(forecast_map)

    assert predictor.predict(_HOUR_A, temp=18.5, fallback_w=300.0, quantile=0.8) == 550.0
    assert predictor.predict(_HOUR_B, temp=19.0, fallback_w=300.0, quantile=0.8) == 520.0


@pytest.mark.asyncio
async def test_remote_predictor_returns_p50_for_median_quantile():
    forecast_map = {_HOUR_A: (400.0, 550.0), _HOUR_B: (380.0, 520.0)}
    predictor = RemoteForecastPredictor(forecast_map)

    assert predictor.predict(_HOUR_A, temp=18.5, fallback_w=300.0, quantile=0.5) == 400.0
    assert predictor.predict(_HOUR_B, temp=19.0, fallback_w=300.0, quantile=0.5) == 380.0


@pytest.mark.asyncio
async def test_remote_predictor_returns_p50_for_low_quantile():
    forecast_map = {_HOUR_A: (400.0, 550.0)}
    predictor = RemoteForecastPredictor(forecast_map)

    result = predictor.predict(_HOUR_A, temp=None, fallback_w=300.0, quantile=0.2)
    assert result == 400.0


# ---------------------------------------------------------------------------
# (b) Timeout → fetch returns None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_forecast_timeout_returns_none():
    """asyncio.TimeoutError from wait_for must be swallowed → None."""
    session = _make_session_raising(asyncio.TimeoutError)
    result = await fetch_forecast(session, _BASE_URL, _TIMEOUT, _PAYLOAD)
    assert result is None


# ---------------------------------------------------------------------------
# (c) Non-200 (e.g. 503) → None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_forecast_non_200_returns_none():
    resp = _make_response(503, {})
    session = _make_session(resp)
    result = await fetch_forecast(session, _BASE_URL, _TIMEOUT, _PAYLOAD)
    assert result is None


# ---------------------------------------------------------------------------
# (d) ready:false → None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_forecast_not_ready_returns_none():
    resp = _make_response(
        200,
        {
            "ready": False,
            "promoted": True,
            "predictions": _PREDICTIONS,
        },
    )
    session = _make_session(resp)
    result = await fetch_forecast(session, _BASE_URL, _TIMEOUT, _PAYLOAD)
    assert result is None


# ---------------------------------------------------------------------------
# (e) promoted:false → None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_forecast_not_promoted_returns_none():
    resp = _make_response(
        200,
        {
            "ready": True,
            "promoted": False,
            "predictions": _PREDICTIONS,
        },
    )
    session = _make_session(resp)
    result = await fetch_forecast(session, _BASE_URL, _TIMEOUT, _PAYLOAD)
    assert result is None


# ---------------------------------------------------------------------------
# (f) Arbitrary exception from session → None, no propagation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_forecast_connection_error_returns_none():
    session = _make_session_raising(lambda: ConnectionError("add-on not running"))
    result = await fetch_forecast(session, _BASE_URL, _TIMEOUT, _PAYLOAD)
    assert result is None


@pytest.mark.asyncio
async def test_fetch_forecast_generic_exception_returns_none():
    session = _make_session_raising(lambda: RuntimeError("unexpected"))
    result = await fetch_forecast(session, _BASE_URL, _TIMEOUT, _PAYLOAD)
    assert result is None


# ---------------------------------------------------------------------------
# (g) RemoteForecastPredictor map-miss → fallback_w
# ---------------------------------------------------------------------------


def test_remote_predictor_map_miss_returns_fallback():
    forecast_map = {_HOUR_A: (400.0, 550.0)}
    predictor = RemoteForecastPredictor(forecast_map)

    missing_hour = datetime(2026, 6, 22, 14, 0, tzinfo=UTC)
    result = predictor.predict(missing_hour, temp=20.0, fallback_w=350.0, quantile=0.8)
    assert result == 350.0


def test_remote_predictor_empty_map_always_returns_fallback():
    predictor = RemoteForecastPredictor({})
    result = predictor.predict(_HOUR_A, temp=None, fallback_w=500.0, quantile=0.8)
    assert result == 500.0


# ---------------------------------------------------------------------------
# Hour-rounding correctness
# ---------------------------------------------------------------------------


def test_remote_predictor_rounds_when_to_hour():
    """predict() must match a map key even when 'when' has non-zero minutes/seconds."""
    forecast_map = {_HOUR_A: (400.0, 550.0)}
    predictor = RemoteForecastPredictor(forecast_map)

    when_with_minutes = datetime(2026, 6, 22, 10, 37, 15, tzinfo=UTC)
    result = predictor.predict(when_with_minutes, temp=None, fallback_w=300.0, quantile=0.8)
    assert result == 550.0


@pytest.mark.asyncio
async def test_fetch_forecast_ts_with_offset_rounds_to_utc_hour():
    """Timestamps with non-UTC offsets must be normalised to UTC before key lookup."""
    # 2026-06-22T12:00:00+02:00  ==  2026-06-22T10:00:00Z  →  _HOUR_A
    prediction_with_offset = [{"ts": "2026-06-22T12:00:00+02:00", "p50_w": 400.0, "p80_w": 550.0}]
    resp = _make_response(
        200,
        {
            "ready": True,
            "promoted": True,
            "predictions": prediction_with_offset,
        },
    )
    session = _make_session(resp)
    result = await fetch_forecast(session, _BASE_URL, _TIMEOUT, _PAYLOAD)

    assert result is not None
    assert _HOUR_A in result
    assert result[_HOUR_A] == (400.0, 550.0)


# ---------------------------------------------------------------------------
# Null / missing p50 or p80 entries are skipped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_forecast_skips_entries_with_null_p50():
    resp = _make_response(
        200,
        {
            "ready": True,
            "promoted": True,
            "predictions": [
                {"ts": "2026-06-22T10:00:00Z", "p50_w": None, "p80_w": 550.0},
                {"ts": "2026-06-22T11:00:00Z", "p50_w": 380.0, "p80_w": 520.0},
            ],
        },
    )
    session = _make_session(resp)
    result = await fetch_forecast(session, _BASE_URL, _TIMEOUT, _PAYLOAD)

    assert result is not None
    assert _HOUR_A not in result  # skipped due to null p50
    assert _HOUR_B in result


@pytest.mark.asyncio
async def test_fetch_forecast_skips_entries_with_missing_p80():
    resp = _make_response(
        200,
        {
            "ready": True,
            "promoted": True,
            "predictions": [
                {"ts": "2026-06-22T10:00:00Z", "p50_w": 400.0},  # p80_w absent
            ],
        },
    )
    session = _make_session(resp)
    result = await fetch_forecast(session, _BASE_URL, _TIMEOUT, _PAYLOAD)

    assert result is not None
    assert _HOUR_A not in result


# ---------------------------------------------------------------------------
# URL trailing-slash normalisation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_forecast_strips_trailing_slash_from_url():
    """URL with trailing slash must still reach /predict, not //predict."""
    captured_urls: list[str] = []
    resp = _make_response(200, {"ready": True, "promoted": True, "predictions": []})

    session = MagicMock()

    @asynccontextmanager
    async def _post(url: str, **kwargs):
        captured_urls.append(url)
        yield resp

    session.post = _post
    await fetch_forecast(session, "http://localhost:8765/", _TIMEOUT, [])

    assert captured_urls == ["http://localhost:8765/predict"]


# ---------------------------------------------------------------------------
# Value validation: negative loads → skipped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_forecast_skips_entry_with_negative_p50():
    """An entry with negative p50 must be skipped; that hour falls back to bucketed."""
    resp = _make_response(
        200,
        {
            "ready": True,
            "promoted": True,
            "predictions": [
                {"ts": "2026-06-22T10:00:00Z", "p50_w": -10.0, "p80_w": 550.0},
                {"ts": "2026-06-22T11:00:00Z", "p50_w": 380.0, "p80_w": 520.0},
            ],
        },
    )
    session = _make_session(resp)
    result = await fetch_forecast(session, _BASE_URL, _TIMEOUT, _PAYLOAD)

    assert result is not None
    assert _HOUR_A not in result  # skipped — negative p50
    assert _HOUR_B in result


@pytest.mark.asyncio
async def test_fetch_forecast_skips_entry_with_negative_p80():
    """An entry with negative p80 must be skipped."""
    resp = _make_response(
        200,
        {
            "ready": True,
            "promoted": True,
            "predictions": [
                {"ts": "2026-06-22T10:00:00Z", "p50_w": 400.0, "p80_w": -5.0},
            ],
        },
    )
    session = _make_session(resp)
    result = await fetch_forecast(session, _BASE_URL, _TIMEOUT, _PAYLOAD)

    assert result is not None
    assert _HOUR_A not in result  # skipped — negative p80

    # RemoteForecastPredictor must therefore return fallback_w for that hour.
    predictor = RemoteForecastPredictor(result)
    assert predictor.predict(_HOUR_A, temp=None, fallback_w=300.0, quantile=0.8) == 300.0


# ---------------------------------------------------------------------------
# Value validation: NaN / Inf → skipped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_forecast_skips_entry_with_nan_p50():
    resp = _make_response(
        200,
        {
            "ready": True,
            "promoted": True,
            "predictions": [
                {"ts": "2026-06-22T10:00:00Z", "p50_w": float("nan"), "p80_w": 550.0},
                {"ts": "2026-06-22T11:00:00Z", "p50_w": 380.0, "p80_w": 520.0},
            ],
        },
    )
    session = _make_session(resp)
    result = await fetch_forecast(session, _BASE_URL, _TIMEOUT, _PAYLOAD)

    assert result is not None
    assert _HOUR_A not in result  # NaN p50 → skipped
    assert _HOUR_B in result


@pytest.mark.asyncio
async def test_fetch_forecast_skips_entry_with_inf_p80():
    resp = _make_response(
        200,
        {
            "ready": True,
            "promoted": True,
            "predictions": [
                {"ts": "2026-06-22T10:00:00Z", "p50_w": 400.0, "p80_w": float("inf")},
            ],
        },
    )
    session = _make_session(resp)
    result = await fetch_forecast(session, _BASE_URL, _TIMEOUT, _PAYLOAD)

    assert result is not None
    assert _HOUR_A not in result  # Inf p80 → skipped


# ---------------------------------------------------------------------------
# Value validation: p80 < p50 → clamped so p80 >= p50
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_forecast_clamps_inverted_p80_up_to_p50():
    """When p80 < p50 (models crossed), p80 is clamped to p50 so quantile never inverts."""
    resp = _make_response(
        200,
        {
            "ready": True,
            "promoted": True,
            "predictions": [
                {"ts": "2026-06-22T10:00:00Z", "p50_w": 500.0, "p80_w": 300.0},  # crossed
            ],
        },
    )
    session = _make_session(resp)
    result = await fetch_forecast(session, _BASE_URL, _TIMEOUT, _PAYLOAD)

    assert result is not None
    assert _HOUR_A in result
    stored_p50, stored_p80 = result[_HOUR_A]
    assert stored_p50 == 500.0
    assert stored_p80 == 500.0  # clamped up from 300 → 500

    predictor = RemoteForecastPredictor(result)
    p80_result = predictor.predict(_HOUR_A, temp=None, fallback_w=300.0, quantile=0.8)
    p50_result = predictor.predict(_HOUR_A, temp=None, fallback_w=300.0, quantile=0.5)
    assert p80_result >= p50_result
    assert p80_result == 500.0


# ---------------------------------------------------------------------------
# Never-raise: malformed predictions body → None, no exception
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_forecast_malformed_predictions_dict_returns_none():
    """`predictions` being a dict (not a list) must not raise — return None."""
    resp = _make_response(
        200,
        {
            "ready": True,
            "promoted": True,
            "predictions": {"x": 1},  # wrong shape
        },
    )
    session = _make_session(resp)
    # Must not raise; None or a (possibly empty) map are both acceptable —
    # what matters is that the call returns without propagating an exception.
    result = await fetch_forecast(session, _BASE_URL, _TIMEOUT, _PAYLOAD)
    # A dict iteration would attempt .get("ts") on int keys; implementation
    # must swallow any AttributeError and return None.
    assert result is None


@pytest.mark.asyncio
async def test_fetch_forecast_malformed_predictions_string_returns_none():
    """`predictions` being a string must not raise — return None."""
    resp = _make_response(
        200,
        {
            "ready": True,
            "promoted": True,
            "predictions": "oops",
        },
    )
    session = _make_session(resp)
    result = await fetch_forecast(session, _BASE_URL, _TIMEOUT, _PAYLOAD)
    assert result is None


# ---------------------------------------------------------------------------
# P8 — persons_home serve-value projection
# ---------------------------------------------------------------------------


def test_hour_of_week_means_buckets_utc():
    # Two samples in the same UTC hour-of-week bucket (Wed 2026-07-01 14:00Z, weekday=2)
    samples = [
        ("2026-07-01T14:00:00+00:00", 2.0),
        ("2026-07-01T14:00:30+00:00", 1.0),
        ("2026-07-08T14:10:00+00:00", 3.0),  # same bucket, next week
    ]
    means = persons_home_hour_of_week_means(samples)
    assert means[2 * 24 + 14] == 2.0  # (2+1+3)/3


def test_project_persistence_then_climatology():
    now = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)  # Wed
    how = {2 * 24 + 18: 1.5}  # Wed 18:00Z bucket
    hours = [
        datetime(2026, 7, 1, 13, 0, tzinfo=UTC),  # +1h → persistence
        datetime(2026, 7, 1, 18, 0, tzinfo=UTC),
    ]  # +6h → climatology
    out = project_persons_home(now, current_count=2, how_means=how, hour_starts=hours)
    assert out["2026-07-01T13:00:00+00:00"] == 2.0
    assert out["2026-07-01T18:00:00+00:00"] == 1.5


def test_project_none_when_unconfigured():
    now = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    hours = [datetime(2026, 7, 1, 13, 0, tzinfo=UTC)]
    out = project_persons_home(now, current_count=None, how_means={}, hour_starts=hours)
    assert out["2026-07-01T13:00:00+00:00"] is None


def test_build_hours_payload_emits_persons_home():
    wf = [
        {
            "datetime": datetime(2026, 7, 1, 13, 0, tzinfo=UTC),
            "temp_forecast": 12.0,
            "cloud_cover": 50.0,
            "humidity": 60.0,
            "wind_speed": 3.0,
        }
    ]
    by_ts = {"2026-07-01T13:00:00+00:00": 2.0}
    payload = build_hours_payload(wf, by_ts)
    assert payload[0]["persons_home"] == 2.0


def test_build_hours_payload_persons_home_defaults_none():
    wf = [{"datetime": datetime(2026, 7, 1, 13, 0, tzinfo=UTC)}]
    payload = build_hours_payload(wf)
    assert payload[0]["persons_home"] is None
