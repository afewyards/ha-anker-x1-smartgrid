"""Remote ML-forecast predictor tier (P3-T2).

Fetches per-hour load predictions from the x1-ml-forecast add-on and exposes a
duck-typed ``predict`` interface that ``build_intervals`` / ``plan.py`` can use
as a drop-in for ``LoadPredictor``.

Safety contract
---------------
*This module MUST NEVER raise or block battery control.*  Every network call is
wrapped in ``asyncio.wait_for`` and a broad ``except Exception`` so that any
failure (timeout, non-200, bad JSON, missing add-on) silently returns ``None``
and the caller falls back to the bucketed/profile predictor.
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timedelta, timezone, UTC

from . import const
from .resolution import hour_floor

_LOGGER = logging.getLogger(__name__)

# Look-back window for the persons-home hour-of-week climatology (P8).
PERSONS_HOW_LOOKBACK_DAYS = 30


def build_hours_payload(
    weather_forecast: list[dict],
    persons_by_ts: dict[str, float | None] | None = None,
) -> list[dict]:
    """Convert ``read_hourly_weather_forecast`` output to a /predict hours payload.

    Each entry in *weather_forecast* has the shape produced by
    ``coordinator.read_hourly_weather_forecast``:

        {"datetime": <UTC-aware datetime>, "temp_forecast": float|None,
         "cloud_cover": float|None, "humidity": float|None, "wind_speed": float|None}

    Returns a list of dicts with ``ts`` set to the **top-of-hour** ISO-8601 UTC
    string (HH:00:00+00:00).

    Alignment note (C2)
    -------------------
    ``RemoteForecastPredictor.predict`` rounds its *when* argument **down** to the
    whole hour before looking up the map.  ``build_intervals`` calls ``predict``
    with ``start = now + i * step_h``, which carries live minutes and seconds.
    The map keys built here are therefore HH:00:00 UTC so that the rounded lookup
    always hits.  If an entry's ``datetime`` already lands on HH:00:00 it is used
    as-is; any sub-hour component is stripped regardless.
    """
    payload: list[dict] = []
    for entry in weather_forecast:
        dt: datetime | None = entry.get("datetime")
        if dt is None:
            continue
        # Normalise to UTC top-of-hour (C2 alignment).
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        else:
            dt = dt.astimezone(UTC)
        top_of_hour = hour_floor(dt)
        top_iso = top_of_hour.isoformat()
        payload.append(
            {
                "ts": top_iso,
                "temp_forecast": entry.get("temp_forecast"),
                "cloud_cover": entry.get("cloud_cover"),
                "humidity": entry.get("humidity"),
                "wind_speed": entry.get("wind_speed"),
                "irradiance": None,  # vestigial; dropped from the featureset in Task 2
                "persons_home": (persons_by_ts or {}).get(top_iso),
            }
        )
    return payload


def persons_home_hour_of_week_means(
    samples: list[tuple[str, float]],
) -> dict[int, float]:
    """Mean persons-home count per UTC hour-of-week (0..167) over the samples.

    ``samples`` is (ts_iso, persons_home) from read_persons_home_samples. ts is
    parsed as UTC-aware (recorder writes dt_util.utcnow().isoformat()); the
    bucket key is weekday()*24 + hour so it aligns with the UTC clock-hour the
    addon receives at serve time.
    """
    acc: dict[int, list[float]] = {}
    for ts_iso, val in samples:
        if val is None:
            continue
        try:
            ts = datetime.fromisoformat(ts_iso)
            v = float(val)
        except (ValueError, TypeError):
            continue
        if math.isnan(v):
            continue
        acc.setdefault(ts.weekday() * 24 + ts.hour, []).append(v)
    return {k: sum(v) / len(v) for k, v in acc.items() if v}


def project_persons_home(
    now: datetime,
    current_count: float | None,
    how_means: dict[int, float],
    hour_starts: list[datetime],
    persistence_hours: int = const.PERSONS_PERSISTENCE_H,
) -> dict[str, float | None]:
    """Per-hour persons-home projection keyed by UTC top-of-hour ISO string.

    Within persistence_hours of now the current count persists; beyond it, the
    UTC hour-of-week climatology mean is used (None if that bucket has no
    history). current_count None (feature unconfigured) → every value None.
    """
    cutoff = now + timedelta(hours=persistence_hours)
    out: dict[str, float | None] = {}
    for hs in hour_starts:
        top = hour_floor(hs.replace(tzinfo=UTC) if hs.tzinfo is None else hs.astimezone(UTC))
        key_iso = top.isoformat()
        if current_count is None:
            out[key_iso] = None
        elif top < cutoff:
            out[key_iso] = float(current_count)
        else:
            out[key_iso] = how_means.get(top.weekday() * 24 + top.hour)
    return out


def _parse_ts(ts_str: str) -> datetime | None:
    """Parse an ISO-8601 UTC timestamp and round down to the whole hour.

    Returns a tz-aware UTC datetime or None on failure.
    """
    try:
        dt = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        else:
            dt = dt.astimezone(UTC)
        return hour_floor(dt)
    except (ValueError, TypeError):
        return None


async def fetch_forecast(
    session,
    url: str,
    timeout: int,
    hours_payload: list[dict],
) -> dict | None:
    """POST to the /predict endpoint and return a ``{hour_dt -> (p50_w, p80_w)}`` map.

    Parameters
    ----------
    session:
        An ``aiohttp.ClientSession`` (or compatible object with a ``.post`` context manager).
    url:
        Base URL of the ML add-on (e.g. ``http://localhost:8765``).  A ``/predict`` path
        is appended automatically; any trailing slash on *url* is stripped.
    timeout:
        Seconds to wait for the full HTTP round-trip before giving up.
    hours_payload:
        List of per-hour dicts to POST as ``{"hours": hours_payload}``.

    Returns
    -------
    dict | None
        Mapping from tz-aware UTC ``datetime`` (truncated to the hour) to
        ``(p50_w, p80_w)`` float tuples, or ``None`` when:

        * the add-on is unreachable / returns non-200,
        * the response indicates ``ready:false`` or ``promoted:false``,
        * a timeout or any other exception occurs.

        Never raises.
    """
    endpoint = url.rstrip("/") + "/predict"

    async def _do_request() -> dict | None:
        """Inner coroutine so wait_for can cancel the entire HTTP exchange."""
        async with session.post(endpoint, json={"hours": hours_payload}) as resp:
            if resp.status != 200:
                _LOGGER.debug(
                    "remote_forecast: /predict returned HTTP %s — falling back",
                    resp.status,
                )
                return None
            try:
                return await resp.json(content_type=None)
            except Exception as exc:
                _LOGGER.warning("remote_forecast: JSON parse error: %s", exc)
                return None

    try:
        data = await asyncio.wait_for(_do_request(), timeout=timeout)
    except TimeoutError:
        _LOGGER.debug("remote_forecast: request timed out after %ss — falling back", timeout)
        return None
    except Exception as exc:
        _LOGGER.debug("remote_forecast: connection error: %s — falling back", exc)
        return None

    if data is None:
        return None

    if not (data.get("ready") and data.get("promoted")):
        _LOGGER.debug(
            "remote_forecast: model not ready/promoted (ready=%s promoted=%s) — falling back",
            data.get("ready"),
            data.get("promoted"),
        )
        return None

    # Decode loop is wrapped so ANY malformed predictions shape (dict, string,
    # entries that aren't dicts, etc.) returns None rather than raising.
    try:
        predictions = data.get("predictions") or []
        forecast_map: dict[datetime, tuple[float, float]] = {}
        for entry in predictions:
            ts_str = entry.get("ts")
            if ts_str is None:
                continue
            hour_dt = _parse_ts(ts_str)
            if hour_dt is None:
                continue
            p50_raw = entry.get("p50_w")
            p80_raw = entry.get("p80_w")
            if p50_raw is None or p80_raw is None:
                continue
            try:
                p50 = float(p50_raw)
                p80 = float(p80_raw)
            except (ValueError, TypeError):
                continue
            # Skip non-finite values (NaN / ±Inf) — they would corrupt the deficit calc.
            if not math.isfinite(p50) or not math.isfinite(p80):
                _LOGGER.debug(
                    "remote_forecast: skipping entry %s — non-finite p50=%s p80=%s",
                    ts_str,
                    p50_raw,
                    p80_raw,
                )
                continue
            # Skip negative loads — nonsensical; better to fall back to bucketed.
            if p50 < 0 or p80 < 0:
                _LOGGER.debug(
                    "remote_forecast: skipping entry %s — negative load p50=%s p80=%s",
                    ts_str,
                    p50,
                    p80,
                )
                continue
            # Clamp p80 >= p50: the P50/P80 models are trained independently and
            # can cross; an inverted cushion would under-size the charge deficit.
            forecast_map[hour_dt] = (p50, max(p80, p50))
    except Exception as exc:
        _LOGGER.warning("remote_forecast: error decoding predictions body: %s — falling back", exc)
        return None

    _LOGGER.debug("remote_forecast: loaded %d hour entries from add-on", len(forecast_map))
    return forecast_map


class RemoteForecastPredictor:
    """Drop-in replacement for ``LoadPredictor`` backed by the ML add-on forecast map.

    Duck-types ``LoadPredictor.predict`` so it can be passed directly to
    ``build_intervals`` and ``plan.py`` without any changes to those callers.
    On a map-miss (hour not in the forecast) it transparently returns *fallback_w*,
    preserving the same safety contract as the profile/bucketed tiers.
    """

    def __init__(self, forecast_map: dict[datetime, tuple[float, float]]) -> None:
        self._map = forecast_map

    def predict(
        self,
        when: datetime,
        temp: float | None,
        fallback_w: float,
        *,
        quantile: float = 0.5,
    ) -> float:
        """Return the ML-forecast load for *when* at the requested *quantile*.

        Parameters
        ----------
        when:
            The forecast hour (any minute/second values are ignored; lookup is
            by hour-truncated UTC datetime, matching how ``build_intervals``
            iterates the PV curve).
        temp:
            Ambient temperature — accepted for interface compatibility but
            unused (the add-on has already baked temperature into its
            predictions).
        fallback_w:
            Returned as-is when *when* is not present in the forecast map.
        quantile:
            ``> 0.5`` → return ``p80_w`` (upper-bound quantile);
            ``<= 0.5`` → return ``p50_w`` (central forecast for display).
        """
        # Round down to the hour in UTC — matches how _parse_ts built the map keys.
        if when.tzinfo is None:
            hour_key = hour_floor(when.replace(tzinfo=UTC))
        else:
            hour_key = hour_floor(when.astimezone(UTC))

        entry = self._map.get(hour_key)
        if entry is None:
            return fallback_w

        p50_w, p80_w = entry
        return p80_w if quantile > 0.5 else p50_w
