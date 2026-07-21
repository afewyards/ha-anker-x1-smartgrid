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
