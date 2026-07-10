"""Feature engineering for the ML load-forecast model (Phase 2).

All functions are PURE — no DB access, no I/O, no clock reads.
The HistGradientBoostingRegressor (Phase 3) tolerates NaN, so missing
values are represented as ``float("nan")`` rather than raising.

Calendar features are computed in **Europe/Amsterdam** local time (CET = UTC+1
in winter, CEST = UTC+2 in summer).  All timestamps entering this module must
be UTC-aware datetimes or ISO-8601 strings with a UTC offset.

Public API (P2-T1)
------------------
encode_calendar_features(ts) -> dict
    Encodes calendar/time features for a single timestamp.

    Returns a dict with these STABLE keys (P2-T4 builds the feature matrix in
    a fixed column order — do not rename or reorder keys here):

        hour_sin    float  sin(2π · hour_local / 24)
        hour_cos    float  cos(2π · hour_local / 24)
        doy_sin     float  sin(2π · doy_local  / 365.25)
        doy_cos     float  cos(2π · doy_local  / 365.25)
        day_of_week int    0 = Monday … 6 = Sunday (local), matching datetime.weekday()
        is_holiday  int    1 if NL public holiday, else 0

Public API (P2-T2)
------------------
encode_lag_features_from_lookups(utc_lookup, local_date_kwh, t_utc) -> dict
    **Canonical lag implementation** — single source of truth shared by
    both build_feature_matrix (training path) and
    HGBRQuantileModel._assemble_feature_vector (serving path).  Any change
    to lag offsets / window bounds / prev-day semantics must be made HERE.
    Takes pre-built lookup dicts for O(1) per-row access.

Public API (P2-T3)
------------------
encode_weather_features(row) -> dict
    Extract weather features from a target hourly-rollup row dict.
    Keys: temp_forecast, hdd, cdd, cloud_cover, humidity, wind_speed.
    None/missing → float("nan").

Public API (P2-T4)
------------------
feature_names() -> list[str]
    Return the STABLE, ORDERED list of 18 feature column names.
    WARNING: HistGBR is positional — never reorder silently.

build_feature_matrix(hourly_rows) -> (X, y, index)
    Assemble the full feature matrix from hourly rollup rows.
    Excludes rows with None/NaN target; keeps NaN-feature rows.
    O(n) — builds lookups once, not per-row.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Union

import holidays as holidays_lib

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TZ_AMS = ZoneInfo("Europe/Amsterdam")

# Per-year cache for the NL holiday set; built lazily on first access.
# The holidays package object supports ``date in obj`` lookups in O(1).
_NL_HOLIDAY_CACHE: dict[int, holidays_lib.HolidayBase] = {}

# Sentinel for missing numeric values (HistGBR tolerates NaN).
_NAN: float = float("nan")

# Degree-day base temperatures (spec §4, Decision #4).
_HDD_BASE: float = 15.5  # °C — heating degree-day base
_CDD_BASE: float = 22.0  # °C — cooling degree-day base

# STABLE feature column order for HistGradientBoostingRegressor.
# WARNING: HistGBR is positional — do NOT silently reorder these names.
# Any change requires retraining ALL stored models.  This constant is the
# single source of truth for the matrix column order; feature_names() wraps it.
# Column count: calendar (6) + lags (5) + weather (6) + presence (1) = 18.
_FEATURE_NAMES: list[str] = [
    # Calendar features (6) — computed in Europe/Amsterdam local time
    "hour_sin",
    "hour_cos",
    "doy_sin",
    "doy_cos",
    "day_of_week",
    "is_holiday",
    # Lag features (5) — UTC-arithmetic from hourly rollup table; NaN if missing
    "load_lag_1h",
    "load_lag_24h",
    "load_lag_168h",
    "rolling_mean_24h",
    "prev_day_total_kwh",
    # Weather features (6) — from target row *_mean columns; NaN if missing.
    # NOTE: live ambient temp (temp_mean) is intentionally excluded — it is
    # not available at forecast time for future hours (train/serve skew) and
    # is redundant with temp_forecast.
    "temp_forecast",
    "hdd",
    "cdd",
    "cloud_cover",
    "humidity",
    "wind_speed",
    # Home-presence (1) — hourly-mean persons-home count (v8). NaN if missing.
    "persons_home",
]


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _parse_ts(ts: Union[datetime, str]) -> datetime:
    """Return a UTC-aware datetime from a datetime or ISO-8601 string.

    Accepted forms:
      - A ``datetime`` object with timezone info (UTC recommended; any offset accepted).
      - An ISO-8601 string such as ``"2026-06-20T14:00:00+00:00"`` or
        ``"2026-06-20T12:00:00Z"`` (Python 3.11+ fromisoformat handles 'Z').

    Raises ``ValueError`` if the input is a naive datetime or an unparseable string.
    """
    if isinstance(ts, str):
        dt = datetime.fromisoformat(ts)
    else:
        dt = ts

    if dt.tzinfo is None:
        raise ValueError(
            f"encode_calendar_features requires a timezone-aware datetime; got naive: {dt!r}"
        )
    return dt


def _to_amsterdam(dt: datetime) -> datetime:
    """Convert any tz-aware datetime to Europe/Amsterdam local time."""
    return dt.astimezone(_TZ_AMS)


def _cyclical_encode(value: float, period: float) -> tuple[float, float]:
    """Return (sin, cos) of ``value`` scaled to ``period``.

    sin(2π · value / period), cos(2π · value / period)
    """
    angle = 2.0 * math.pi * value / period
    return math.sin(angle), math.cos(angle)


def _nl_holidays(year: int) -> holidays_lib.HolidayBase:
    """Return (cached) NL holiday set for ``year``."""
    if year not in _NL_HOLIDAY_CACHE:
        _NL_HOLIDAY_CACHE[year] = holidays_lib.country_holidays("NL", years=year)
    return _NL_HOLIDAY_CACHE[year]


def _coerce_float(v: object) -> float:
    """Coerce ``v`` to float, returning NaN on None, NaN input, or error."""
    if v is None:
        return _NAN
    try:
        f = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return _NAN
    return _NAN if math.isnan(f) else f


# ---------------------------------------------------------------------------
# Public API — P2-T1: Calendar features
# ---------------------------------------------------------------------------


def encode_calendar_features(ts: Union[datetime, str]) -> dict:
    """Encode calendar/time features for a single UTC timestamp.

    Parameters
    ----------
    ts:
        A UTC-aware :class:`datetime` **or** an ISO-8601 string with a UTC
        offset (e.g. the ``hour_ts`` value stored by the rollup table:
        ``"2026-06-20T14:00:00+00:00"``).

    Returns
    -------
    dict with keys (stable; downstream slices depend on these names):

    ========== ====== =====================================================
    Key        Type   Description
    ========== ====== =====================================================
    hour_sin   float  sin(2π · local_hour / 24)
    hour_cos   float  cos(2π · local_hour / 24)
    doy_sin    float  sin(2π · local_doy  / 365.25)
    doy_cos    float  cos(2π · local_doy  / 365.25)
    day_of_week int   0 = Monday … 6 = Sunday (local), weekday() convention
    is_holiday  int   1 if the local date is a NL public holiday, else 0
    ========== ====== =====================================================
    """
    dt_utc = _parse_ts(ts)
    dt_local = _to_amsterdam(dt_utc)

    local_hour: int = dt_local.hour
    local_doy: int = dt_local.timetuple().tm_yday  # 1-based (Jan 1 = 1)
    local_dow: int = dt_local.weekday()             # 0=Monday … 6=Sunday
    local_date = dt_local.date()

    hour_sin, hour_cos = _cyclical_encode(local_hour, 24.0)
    doy_sin, doy_cos = _cyclical_encode(local_doy, 365.25)

    nl_hols = _nl_holidays(local_date.year)
    is_holiday: int = 1 if local_date in nl_hols else 0

    return {
        "hour_sin": hour_sin,
        "hour_cos": hour_cos,
        "doy_sin": doy_sin,
        "doy_cos": doy_cos,
        "day_of_week": local_dow,
        "is_holiday": is_holiday,
    }


# ---------------------------------------------------------------------------
# Public API — P2-T2: Lag features
# ---------------------------------------------------------------------------


_EXPECTED_ROWS_PER_HOUR = 60.0  # recorder ticks every 60 s


def hourly_load_w(row: dict) -> float | None:
    """Energy-derived hourly-average load W: kwh_sum×1000 (true ∫P dt), mean-W fallback.

    Partial-coverage hours (restart/gap): kwh_sum only integrates observed ticks
    but spans a full clock-hour, so it is rescaled by expected/observed rows
    (house_load_count) to the time-weighted average W over the observed window
    instead of diluting the target toward zero.
    """
    kwh = row.get("house_load_kwh_sum")
    if kwh is not None:
        count = row.get("house_load_count")
        if count is not None and 0 < float(count) < _EXPECTED_ROWS_PER_HOUR:
            return float(kwh) * 1000.0 * (_EXPECTED_ROWS_PER_HOUR / float(count))
        return float(kwh) * 1000.0
    mean = row.get("house_load_mean")
    return float(mean) if mean is not None else None


# ---------------------------------------------------------------------------
# Canonical lag helper — single source of truth for train AND predict paths
# ---------------------------------------------------------------------------


def encode_lag_features_from_lookups(
    utc_lookup: dict,
    local_date_kwh: dict,
    t: datetime,
) -> dict:
    """Compute lag/rolling features from pre-built lookup structures.

    This is the **canonical lag implementation**.  It is called by both
    ``build_feature_matrix`` (training path) and
    ``HGBRQuantileModel._assemble_feature_vector`` (serving path), ensuring
    that train and predict feature vectors use identical lag arithmetic.

    Any future change to window bounds, lag offsets, or rolling semantics
    must be made here — never duplicated in the callers.

    Parameters
    ----------
    utc_lookup:
        Mapping of UTC-aware :class:`datetime` → energy-derived hourly load
        (``house_load_kwh_sum``×1000, ``house_load_mean`` fallback) in W, or
        ``None`` (SQLite NULL).  Built once per training batch or stored in
        ``HGBRQuantileModel._utc_lookup`` for reuse at serve time.
    local_date_kwh:
        Mapping of ``datetime.date`` (Europe/Amsterdam) → daily energy total
        in kWh, summed from ``house_load_kwh_sum``.  Used for
        ``prev_day_total_kwh``.
    t:
        UTC-aware :class:`datetime` of the target hour.

    Returns
    -------
    dict with 5 keys: ``load_lag_1h``, ``load_lag_24h``, ``load_lag_168h``,
    ``rolling_mean_24h``, ``prev_day_total_kwh``.  Missing values → NaN.
    """
    def _lkup(delta_h: int) -> float:
        v = utc_lookup.get(t - timedelta(hours=delta_h))
        return float(v) if v is not None else _NAN

    lag_1h = _lkup(1)
    lag_24h = _lkup(24)
    lag_168h = _lkup(168)

    window_vals = [
        float(v)
        for dh in range(1, 25)
        if (v := utc_lookup.get(t - timedelta(hours=dh))) is not None
    ]
    rolling_mean: float = sum(window_vals) / len(window_vals) if window_vals else _NAN

    prev_local_date = t.astimezone(_TZ_AMS).date() - timedelta(days=1)
    prev_day_total_kwh: float = local_date_kwh.get(prev_local_date, _NAN)

    return {
        "load_lag_1h": lag_1h,
        "load_lag_24h": lag_24h,
        "load_lag_168h": lag_168h,
        "rolling_mean_24h": rolling_mean,
        "prev_day_total_kwh": prev_day_total_kwh,
    }


# ---------------------------------------------------------------------------
# Public API — P2-T3: Weather features
# ---------------------------------------------------------------------------


def encode_weather_features(row: dict) -> dict:
    """Extract weather features from a target hourly-rollup row.

    Parameters
    ----------
    row:
        A single hourly-rollup row dict, e.g. as returned by
        ``recorder.read_hourly_rows()``.  All weather columns are optional;
        ``None`` (SQLite NULL) and missing keys → ``float("nan")``.

    Returns
    -------
    dict with 6 keys:

    ============== ====== ===================================================
    Key            Type   Description
    ============== ====== ===================================================
    temp_forecast  float  Forecast temperature for the hour (°C); NaN if N/A
    hdd            float  max(0, 15.5 − temp_forecast); NaN if temp_forecast NaN
    cdd            float  max(0, temp_forecast − 22.0); NaN if temp_forecast NaN
    cloud_cover    float  Forecast cloud cover; NaN if N/A
    humidity       float  Forecast relative humidity; NaN if N/A
    wind_speed     float  Forecast wind speed; NaN if N/A
    ============== ====== ===================================================

    Notes
    -----
    - ``hdd``/``cdd`` are derived from ``temp_forecast`` with base temperatures
      15.5 °C (HDD) and 22.0 °C (CDD) per spec Decision #4.
    - Live ambient ``temp_mean`` is intentionally omitted: it is unavailable
      at predict-time for future hours (train/serve skew) and is redundant
      with ``temp_forecast``.
    - No EV / big-load / HVAC features (spec Decision #0).
    """
    temp_forecast = _coerce_float(row.get("temp_forecast_mean"))

    if not math.isnan(temp_forecast):
        hdd: float = max(0.0, _HDD_BASE - temp_forecast)
        cdd: float = max(0.0, temp_forecast - _CDD_BASE)
    else:
        hdd = _NAN
        cdd = _NAN

    return {
        "temp_forecast": temp_forecast,
        "hdd": hdd,
        "cdd": cdd,
        "cloud_cover": _coerce_float(row.get("cloud_cover_mean")),
        "humidity": _coerce_float(row.get("humidity_mean")),
        "wind_speed": _coerce_float(row.get("wind_speed_mean")),
    }


def encode_persons_feature(row: dict) -> dict:
    """Home-presence feature from the hourly rollup.

    Reads ``persons_home_mean`` (hourly-mean count of configured person
    entities in state 'home'). None/missing → NaN (HGBR-native).
    """
    return {"persons_home": _coerce_float(row.get("persons_home_mean"))}


# ---------------------------------------------------------------------------
# Public API — P2-T4: Feature names + matrix assembly
# ---------------------------------------------------------------------------


def feature_names() -> list[str]:
    """Return the STABLE, ORDERED list of feature column names for HistGBR.

    Column count: calendar (6) + lags (5) + weather (6) + presence (1) = 18 features.

    **WARNING:** ``HistGradientBoostingRegressor`` is positional.  This list
    must never be silently reordered — any change requires retraining all
    stored models.  The authoritative order is defined by ``_FEATURE_NAMES``.

    Returns a fresh copy so callers cannot mutate the module constant.
    """
    return list(_FEATURE_NAMES)


def build_feature_matrix(
    hourly_rows: list[dict],
) -> tuple[list[list[float]], list[float], list[str]]:
    """Build the ML feature matrix, target vector, and index from hourly rows.

    Parameters
    ----------
    hourly_rows:
        List of hourly rollup row dicts, ordered ASC by ``hour_ts``.

    Returns
    -------
    X:     list[list[float]] — one feature vector per kept row (18 values each).
    y:     list[float] — energy-derived hourly load (``house_load_kwh_sum``×1000,
           ``house_load_mean`` fallback) target for each kept row.
    index: list[str] — ``hour_ts`` string for each kept row (same order as X/y).

    Target exclusion contract
    -------------------------
    Rows whose **target** (energy-derived hourly load: ``house_load_kwh_sum``×1000,
    ``house_load_mean`` fallback) is ``None`` or NaN are **excluded** from all
    three return values.  Feature-column NaN values are *kept* — ``HistGBR``
    handles them natively.
    ``HGBRQuantileModel.fit()`` (Phase 3) relies on this contract.

    Complexity
    ----------
    O(n) — the UTC lookup dict and local-date energy totals are built in a
    single pre-pass, not rebuilt per row.
    """
    if not hourly_rows:
        return [], [], []

    # ---- O(n) pre-pass: build lookup structures ----
    utc_lookup: dict[datetime, float | None] = {}
    local_date_kwh: dict = {}  # local_date -> Σ house_load_kwh_sum (kWh)

    for row in hourly_rows:
        ts_str = row.get("hour_ts")
        if not ts_str:
            continue
        ts = datetime.fromisoformat(str(ts_str))
        load = hourly_load_w(row)
        utc_lookup[ts] = load

        kwh = row.get("house_load_kwh_sum")
        if kwh is not None:
            local_d = ts.astimezone(_TZ_AMS).date()
            local_date_kwh.setdefault(local_d, 0.0)
            local_date_kwh[local_d] += float(kwh)

    # ---- Per-row feature assembly ----
    X: list[list[float]] = []
    y: list[float] = []
    index: list[str] = []

    for row in hourly_rows:
        # Exclude rows where the target is missing/NaN — cannot train on them.
        target_val = hourly_load_w(row)
        if target_val is None:
            continue
        try:
            target_f = float(target_val)
        except (TypeError, ValueError):
            continue
        if math.isnan(target_f):
            continue

        hour_ts_str = str(row["hour_ts"])
        t = datetime.fromisoformat(hour_ts_str)

        # Calendar features (P2-T1)
        cal = encode_calendar_features(t)

        # Lag features — shared helper is the single source of truth
        lags = encode_lag_features_from_lookups(utc_lookup, local_date_kwh, t)

        # Weather features (P2-T3)
        weather = encode_weather_features(row)

        # Home-presence feature
        persons = encode_persons_feature(row)

        # Assemble feature dict and project to the stable column order
        feat_dict: dict[str, float] = {
            **cal,
            **lags,
            **weather,
            **persons,
        }
        vec: list[float] = [feat_dict[name] for name in _FEATURE_NAMES]

        X.append(vec)
        y.append(target_f)
        index.append(hour_ts_str)

    return X, y, index
