"""Pure hourly-rollup aggregation for the ML load-forecast pipeline.

No I/O, no DB access — the only impure shell is DataRecorder.rollup_hours.
All aggregation math lives here for unit-testability.

Stable column order for samples_hourly
(Phase 2 featureset reads columns by name; order is documented here so any
positional access stays consistent across the schema lifetime):

  Position 0  : hour_ts (PK)
  Positions 1-5  : house_load_mean/max/min/std/count
  Positions 6-10 : pv_w_mean/max/min/std/count
  Positions 11-15: soc_mean/max/min/std/count
  Positions 16-20: irradiance_mean/max/min/std/count
  Positions 21-25: temp_mean/max/min/std/count
  Positions 26-30: temp_forecast_mean/max/min/std/count
  Positions 31-35: cloud_cover_mean/max/min/std/count
  Positions 36-40: humidity_mean/max/min/std/count
  Positions 41-45: wind_speed_mean/max/min/std/count
  Positions 46-50: persons_home_mean/max/min/std/count
  Positions 51-56: grid_import_kwh_sum, grid_export_kwh_sum,
                    house_load_kwh_sum, pv_kwh_sum, batt_charge_kwh_sum,
                    batt_discharge_kwh_sum (v10)

Design decisions (recorded here to avoid future guessing):
  - std when count < 2 : 0.0 (not None).  Avoids NaN leaking into the ML
    feature matrix; clearly non-missing rather than "unknown".
  - std formula       : sample std (ddof=1, N-1 denominator) — conventional
    for feature stores; population std would differ by √(N/(N-1)).
  - hour_ts format    : UTC ISO string truncated to the clock-hour, preserving
    the timezone offset, e.g. "2026-06-20T14:00:00+00:00".
  - All-NULL field    : mean/max/min/std = None, count = 0.
  - house_load        : load_w column (sensor.power_usage, v6+) takes priority;
    falls back to derive_house_load_w (p1+batt+pv, pv NULL→0) for pre-v6 rows.
    None when both load_w and p1_w are NULL → excluded from stats.
  - transition window : during the ~14-day post-v6 window most raw rows are
    pre-v6 (load_w=NULL) so hourly aggregates blend derived (old) + recorded
    (new) values for house_load.  The mix automatically shifts toward the
    ground-truth sensor readings as stale rows age out of the retention window.
  - kWh sums (v10)    : each *_kwh_sum column is a two-tier aggregate of the
    per-tick *_kwh energy-delta columns (v9). Tier 1 (accurate): if ANY row
    in the hour has a non-NULL *_kwh value, the result is the sum of the
    non-NULL values (honest rectangle-rule integration with gap clamping).
    Tier 2 (approximate, pre-v9 data): if ALL rows are NULL for that column,
    fall back to mean_watts × 1h / 1000 using the already-computed stats
    (house_load_mean, pv_w_mean) or an inline mean of p1_w/batt_w (NOT stored
    as hourly columns — see _kwh_sum_pass). Sign conventions match
    dataquality.py: p1_w import-positive, batt_w discharge-positive.
"""
from __future__ import annotations

import math
from datetime import datetime

from .dataquality import house_load_w as _house_load_w

# Ordered tuple of features to roll up.  ORDER IS STABLE — do not reorder.
# "house_load" is a derived feature (not a raw column name); all others are
# raw column names in the samples table.
_ROLLUP_FEATURES: tuple[str, ...] = (
    "house_load",    # Derived: p1_w + batt_w + pv_w (pv NULL→0); ML target
    "pv_w",          # PV AC output (NULL at night → excluded from stats)
    "soc",           # Battery state of charge
    "irradiance",    # Live irradiance sensor (not forecast)
    "temp",          # Live ambient temperature
    "temp_forecast", # Forecast temperature aligned to the hour
    "cloud_cover",   # Forecast cloud cover
    "humidity",      # Forecast humidity
    "wind_speed",    # Forecast wind speed
    "persons_home",  # Count of person.* entities in state 'home' (v8)
)

# v10: per-tick kWh energy-delta columns (samples table, v9) summed per hour
# into samples_hourly's *_kwh_sum columns. ORDER IS STABLE — mirrors
# recorder.py::_HOURLY_COLUMNS tail and _SCHEMA_SAMPLES_HOURLY.
_ENERGY_KWH_COLUMNS: tuple[str, ...] = (
    "grid_import_kwh",
    "grid_export_kwh",
    "house_load_kwh",
    "pv_kwh",
    "batt_charge_kwh",
    "batt_discharge_kwh",
)


def aggregate_hour(rows: list[dict]) -> dict:
    """Aggregate raw sample rows for ONE clock-hour into stat columns.

    Args:
        rows: Non-empty list of raw sample dicts that all belong to the same
              UTC clock-hour (caller's responsibility to pre-group them).

    Returns:
        Dict suitable for INSERT into ``samples_hourly``, with keys::

            hour_ts                         — UTC-truncated-to-hour ISO string
            <feature>_mean/max/min/std      — REAL (None when all values NULL)
            <feature>_count                 — INTEGER (0 when all values NULL)

        Features follow the stable order defined by :data:`_ROLLUP_FEATURES`.

    Aggregation rules:
        - Stats are computed over **non-NULL** raw values for each feature.
        - All-NULL field → mean/max/min/std = None, count = 0.
        - count < 2    → std = 0.0 (not None; avoids NaN in feature matrix).
        - std uses sample variance: ``sqrt(Σ(xᵢ − x̄)² / (N-1))``.
        - ``house_load`` is derived via :func:`~dataquality.derive_house_load_w`
          per row (pv NULL → 0).  When p1_w is NULL the row contributes no
          valid house_load value and is excluded from that feature's stats.

    Raises:
        ValueError: if ``rows`` is empty or the first row has no ``ts`` field.
    """
    if not rows:
        raise ValueError("aggregate_hour requires at least one row")

    # Derive hour_ts from the first row's ts, truncated to the clock-hour.
    ts_raw = rows[0].get("ts")
    if not ts_raw:
        raise ValueError("aggregate_hour: first row has no 'ts' field")
    ts = datetime.fromisoformat(str(ts_raw))
    hour_ts = ts.replace(minute=0, second=0, microsecond=0).isoformat()

    result: dict = {"hour_ts": hour_ts}

    for feature in _ROLLUP_FEATURES:
        values: list[float] = []
        for row in rows:
            if feature == "house_load":
                v = _house_load_w(row)
            else:
                v = row.get(feature)
            if v is not None:
                values.append(float(v))

        count = len(values)
        result[f"{feature}_count"] = count

        if count == 0:
            result[f"{feature}_mean"] = None
            result[f"{feature}_max"] = None
            result[f"{feature}_min"] = None
            result[f"{feature}_std"] = None
        else:
            mean = sum(values) / count
            result[f"{feature}_mean"] = mean
            result[f"{feature}_max"] = max(values)
            result[f"{feature}_min"] = min(values)
            if count < 2:
                # Single-sample hour: std is undefined; 0.0 is a safe sentinel
                # that avoids NaN in the downstream feature matrix.
                result[f"{feature}_std"] = 0.0
            else:
                variance = sum((v - mean) ** 2 for v in values) / (count - 1)
                result[f"{feature}_std"] = math.sqrt(variance)

    _kwh_sum_pass(rows, result)

    return result


def _kwh_sum_pass(rows: list[dict], result: dict) -> None:
    """Populate the 6 ``*_kwh_sum`` keys in ``result`` (mutated in place).

    Tier 1 (accurate): if any row has a non-NULL ``*_kwh`` value, the column
    is the sum of the non-NULL values scaled by ``len(rows) / len(non_null)``
    — NULL ticks WITHIN recorded rows are gaps (filled by the observed
    per-tick average), not zero energy. Full coverage scales by ×1 (byte-
    identical). Genuine downtime (fewer recorded rows) is NOT inflated —
    only within-row NULL gaps are filled. Honest per-tick rectangle-rule
    integration with gap clamping (see recorder.py::_energy_deltas).

    Tier 2 (approximate, for pre-v9 rows where every tick is NULL): fall back
    to ``mean_watts × 1h / 1000``. house_load/pv reuse the stats already
    computed in ``result``. Grid import/export and battery charge/discharge
    have NO tier-2 fallback — the sign-split mean of ``p1_w``/``batt_w`` was
    removed because these four columns are unconsumed in production, and
    averaging before sign-splitting zeroed oscillating import/export hours.
    They stay ``None`` when tier-1 yields no data.
    """
    n_rows = len(rows)
    for col in _ENERGY_KWH_COLUMNS:
        values = [row[col] for row in rows if row.get(col) is not None]
        if not values:
            result[f"{col}_sum"] = None
        else:
            # Scale by coverage: NULL ticks among recorded rows are gaps, not 0.
            # Full coverage → ×1 (byte-identical). Genuine downtime (fewer rows) is
            # not inflated — only within-row NULL gaps are filled.
            result[f"{col}_sum"] = sum(values) * n_rows / len(values)

    if result["house_load_kwh_sum"] is None:
        mean = result.get("house_load_mean")
        result["house_load_kwh_sum"] = None if mean is None else mean / 1000.0

    if result["pv_kwh_sum"] is None:
        mean = result.get("pv_w_mean")
        result["pv_kwh_sum"] = None if mean is None else mean / 1000.0

    # Tier-2 sign-split DROPPED for grid import/export and battery charge/discharge:
    # these four *_kwh_sum columns are unconsumed in production, and averaging
    # p1_w/batt_w before the sign-split zeroed oscillating import/export hours.
    # Leave them None (already set by the tier-1 loop above when all ticks NULL).
