"""Load and clean recorded samples into feature rows for the load model."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from . import featureset

_LOAD_MAX_W = 25000.0


@dataclass(frozen=True)
class FeatureRow:
    ts: datetime
    hour: int
    is_weekend: bool
    load_w: float
    temp: float | None


def _is_export_row(row: dict) -> bool:
    """Return True when the row represents a net grid-export state (p1_w < 0)."""
    p1 = row.get("p1_w")
    return p1 is not None and float(p1) < 0.0


def derive_house_load_w(row: dict) -> float | None:
    """Reconstruct house load from the AC energy balance behind the P1 meter.

    Sources in = sinks out at the AC bus:
        grid_import + batt_discharge + pv = house_load + batt_charge + grid_export
    With signed conventions ``p1_w`` (import +) and ``batt_w`` (discharge +) and
    ``pv_w`` >= 0 (GoodWe AC output), this rearranges to ``p1 + batt + pv``.
    Battery charging from PV surplus falls out automatically.

    Export-safe note
    ----------------
    The AC energy balance holds during grid export: a negative ``p1_w`` contributes
    correctly and the result is still true house load.  However, if ``pv_w`` is
    absent (key missing, not just zero) **and** the row is an export row, we cannot
    distinguish "no PV" from "sensor unavailable" — silently defaulting to 0 would
    under-count load and poison the ML model.  In that case this function returns
    ``None`` so the caller (``house_load_w`` / ``clean_rows``) can discard the row.
    Non-export rows may default missing ``pv_w`` to 0 safely (no PV generation is
    the common case for those rows).
    """
    p1 = row.get("p1_w")
    if p1 is None:
        return None
    batt = row.get("batt_w") or 0.0
    if "pv_w" not in row and _is_export_row(row):
        # pv_w is absent during an export row — cannot safely default to 0.
        return None
    pv = row.get("pv_w") or 0.0
    return float(p1) + float(batt) + float(pv)


def house_load_w(row: dict) -> float | None:
    """Return the best available house-load value for ``row``.

    Priority
    --------
    1. ``load_w`` column (recorded since v6) — **Always preferred**, but note this is
       a COMPUTED value written at sample time (``pv + p1_w(meter) + batt_w −
       inverter_loss``, see recorder.py), not an independent sensor reading. It is
       still preferred over row 2 because it's computed once, consistently, at
       record time rather than re-derived per consumer, but it is derived from the
       same p1/pv/batt terms as the fallback below, not a ground-truth measurement.
    2. ``derive_house_load_w(row)`` — fallback for pre-v6 rows where ``load_w`` is
       NULL; uses the AC energy balance ``p1 + batt + pv``.

    Export-safety contract
    ----------------------
    The derive fallback is only reached when ``load_w`` is NULL.  During export rows
    (``p1_w < 0``) the formula is mathematically correct provided all sensor inputs
    are present; if ``pv_w`` is absent the fallback returns ``None`` so the row is
    dropped rather than silently polluting the load model.  See
    ``derive_house_load_w`` for details.

    Use this function everywhere the ML pipeline needs a per-row house-load value so
    that historical (derived) rows age out naturally as newer recorded rows accumulate.
    """
    recorded = row.get("load_w")
    if recorded is not None:
        return float(recorded)
    return derive_house_load_w(row)


def clean_rows(rows: list[dict]) -> list[FeatureRow]:
    """Parse, validate, outlier-clamp recorded rows into FeatureRows."""
    out: list[FeatureRow] = []
    for row in rows:
        ts_raw = row.get("ts")
        if not ts_raw:
            continue
        load = house_load_w(row)
        if load is None:
            continue
        try:
            ts = datetime.fromisoformat(str(ts_raw))
        except (ValueError, TypeError):
            continue
        load = min(max(0.0, load), _LOAD_MAX_W)
        temp = row.get("temp")
        out.append(
            FeatureRow(
                ts=ts,
                hour=ts.hour,
                is_weekend=ts.weekday() >= 5,
                load_w=load,
                temp=float(temp) if temp is not None else None,
            )
        )
    return out


def clean_hourly_rows(rows: list[dict]) -> list[FeatureRow]:
    """Parse ``samples_hourly`` rollup rows into FeatureRows for the bucketed tier.

    Preference order for load: ``house_load_kwh_sum`` (energy-derived hourly-average
    W, ``× 1000``, rescaled by ``house_load_count`` coverage — see
    ``featureset.hourly_load_w``) then ``house_load_mean`` (fallback, e.g.
    pre-rollup rows or a partial hour). Temp comes from ``temp_mean``. Rows
    missing ``hour_ts`` or both load fields are dropped; outlier-clamped (after
    rescaling) the same way as ``clean_rows``.
    """
    out: list[FeatureRow] = []
    for row in rows:
        ts_raw = row.get("hour_ts")
        if not ts_raw:
            continue
        load = featureset.hourly_load_w(row)
        if load is None:
            continue
        try:
            ts = datetime.fromisoformat(str(ts_raw))
        except (ValueError, TypeError):
            continue
        load = min(max(0.0, float(load)), _LOAD_MAX_W)
        temp = row.get("temp_mean")
        out.append(
            FeatureRow(
                ts=ts,
                hour=ts.hour,
                is_weekend=ts.weekday() >= 5,
                load_w=load,
                temp=float(temp) if temp is not None else None,
            )
        )
    return out
