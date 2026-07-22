"""Pure helpers for ML predictor status visibility.

Observability only — nothing here touches planning or actuation.

``count_lag_complete_days`` mirrors ``HGBRQuantileModel.is_ready``'s coverage
rule WITHOUT the sklearn import guard (sklearn cannot install on the on-box
py3.14/musl HA core, so ``is_ready`` always returns False there).  Kept
standalone rather than factored into ``hgbr.py`` to avoid add-on vendoring
lockstep; ``test_parity_with_hgbr_is_ready`` locks the two implementations
together in the dev venv.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

from .backtest import MIN_HORIZON_ORIGINS_24H
from .featureset import _TZ_AMS

COVERAGE_REQUIRED_DAYS: int = 21
_LAG_7D = timedelta(hours=168)
_LAST_TRAINED_MAX_LEN = 64


def count_lag_complete_days(hourly_rows: list[dict]) -> int:
    """Count distinct Europe/Amsterdam dates carrying lag-complete rows.

    A row at UTC time *t* is lag-complete when a row at *t − 168 h* is also
    present.  Never raises; malformed timestamps are skipped.
    """
    ts_set: set[datetime] = set()
    for row in hourly_rows:
        ts_str = row.get("hour_ts")
        if not ts_str:
            continue
        try:
            ts_set.add(datetime.fromisoformat(str(ts_str)))
        except (ValueError, TypeError):
            continue

    lag_complete_dates = set()
    for ts in ts_set:
        if (ts - _LAG_7D) in ts_set:
            lag_complete_dates.add(ts.astimezone(_TZ_AMS).date())
    return len(lag_complete_dates)


def _coerce_n_rows(value: object) -> int | None:
    """Coerce a pass-through add-on health value to a bounded int.

    Guards the HA recorder's 16 KiB attribute cap against an oversized or
    malformed add-on payload. ``None`` when *value* is not numeric-looking
    (bools are deliberately excluded — they are not "numeric-looking" even
    though ``bool`` is an ``int`` subclass). Never raises.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            return int(value.strip())
        except (ValueError, TypeError):
            return None
    return None


def _coerce_last_trained(value: object) -> str | None:
    """Coerce a pass-through add-on health value to a string, truncated to 64 chars.

    Guards the HA recorder's 16 KiB attribute cap against an oversized
    add-on payload. Never raises.
    """
    if value is None:
        return None
    try:
        return str(value)[:_LAST_TRAINED_MAX_LEN]
    except Exception:
        return None


def _coerce_metric_float(value: object, ndigits: int) -> float | None:
    """Coerce an add-on backtest metric to a bounded rounded float.

    Stricter than ``_coerce_n_rows``: strings are rejected (metrics are
    machine-emitted floats; a string means a malformed payload). Never raises.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        f = float(value)
        return round(f, ndigits) if math.isfinite(f) else None
    return None


def _coerce_metric_int(value: object) -> int | None:
    """Coerce an add-on backtest count to an int; strings rejected. Never raises."""
    f = _coerce_metric_float(value, 0)
    return int(f) if f is not None else None


def build_ml_status_attrs(
    *,
    addon_enabled: bool,
    addon_url: str | None,
    health: dict | None,
    health_ts: datetime | None,
    coverage_days: int | None,
    active_model: str,
) -> dict:
    """Build the diagnostic attribute dict for the active-load-model sensor.

    Priority order (spec §4): off → unreachable → active/promoted →
    backtest gate → coverage ETA → collecting data.  Never raises.
    """
    configured = bool(addon_enabled) and bool(addon_url)
    # When the add-on is not configured, health-derived fields must not leak a
    # stale reading from a previous (enabled) session — force them all to
    # None rather than trusting whatever the caller happened to pass in.
    if not configured:
        health = None
        health_ts = None

    checked = health_ts is not None
    reachable: bool | None = (health is not None) if checked else None
    ready: bool | None = bool(health.get("ready")) if health else None
    promoted: bool | None = bool(health.get("promoted")) if health else None
    eta_days = (
        max(0, COVERAGE_REQUIRED_DAYS - coverage_days)
        if coverage_days is not None
        else None
    )

    raw_metrics = health.get("metrics") if health else None
    m = raw_metrics if isinstance(raw_metrics, dict) else {}
    improvement = _coerce_metric_float(m.get("improvement_pct"), 1)
    origins = _coerce_metric_int(m.get("n_horizon_origins_24h"))
    model_mae = _coerce_metric_float(m.get("model_mae"), 1)
    baseline_mae = _coerce_metric_float(m.get("baseline_mae"), 1)
    h24_mae = _coerce_metric_float(m.get("horizon_energy_mae_24h"), 2)
    baseline_h24_mae = _coerce_metric_float(m.get("baseline_horizon_energy_mae_24h"), 2)

    if not configured:
        status = "add-on off"
    elif checked and health is None:
        status = "⚠ unreachable"
    elif promoted and active_model == "remote":
        status = "ML active"
    elif promoted:
        status = "⚠ promoted, not consumed"
    elif ready:
        parts = ["backtest gate"]
        if origins is not None:
            parts.append(f"{origins}/{MIN_HORIZON_ORIGINS_24H}")
        if improvement is not None:
            parts.append(f"{improvement:+.0f}%")
        status = " · ".join(parts)
    elif eta_days is not None:
        status = f"ML in ~{eta_days}d"
    else:
        status = "collecting data"

    return {
        "ml_status": status,
        "addon_configured": configured,
        "addon_reachable": reachable,
        "addon_ready": ready,
        "addon_promoted": promoted,
        "addon_n_rows": _coerce_n_rows(health.get("n_rows")) if health else None,
        "addon_last_trained": _coerce_last_trained(health.get("last_trained")) if health else None,
        "addon_improvement_pct": improvement,
        "addon_origins_24h": origins,
        "addon_origins_required": MIN_HORIZON_ORIGINS_24H,
        "addon_model_mae": model_mae,
        "addon_baseline_mae": baseline_mae,
        "addon_h24_mae": h24_mae,
        "addon_baseline_h24_mae": baseline_h24_mae,
        "coverage_days": coverage_days,
        "coverage_required": COVERAGE_REQUIRED_DAYS,
        "eta_days": eta_days,
        "last_health_check": health_ts.isoformat() if health_ts else None,
    }
