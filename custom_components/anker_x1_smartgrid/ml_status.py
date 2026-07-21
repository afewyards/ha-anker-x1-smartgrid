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

from datetime import datetime, timedelta

from .featureset import _TZ_AMS

COVERAGE_REQUIRED_DAYS: int = 21
_LAG_7D = timedelta(hours=168)


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
    checked = health_ts is not None
    reachable: bool | None = (health is not None) if checked else None
    ready: bool | None = bool(health.get("ready")) if health else None
    promoted: bool | None = bool(health.get("promoted")) if health else None
    eta_days = (
        max(0, COVERAGE_REQUIRED_DAYS - coverage_days)
        if coverage_days is not None
        else None
    )

    if not configured:
        status = "add-on off"
    elif checked and health is None:
        status = "⚠ unreachable"
    elif promoted and active_model == "remote":
        status = "ML active"
    elif promoted:
        status = "⚠ promoted, not consumed"
    elif ready:
        status = "backtest gate"
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
        "addon_n_rows": health.get("n_rows") if health else None,
        "addon_last_trained": health.get("last_trained") if health else None,
        "coverage_days": coverage_days,
        "coverage_required": COVERAGE_REQUIRED_DAYS,
        "eta_days": eta_days,
        "last_health_check": health_ts.isoformat() if health_ts else None,
    }
