"""Intraday residual corrector for the load forecast (Layer A).

Compares recent actual house load (recorder past-actuals) against what the
active predictor tier forecast for those same hours, and scales the
remainder-of-day P50 by a bounded, lead-time-faded ratio.  Tier-agnostic:
wraps whatever tier ``_retrain_sync`` selected; applied ONLY to the live
``compute_decision`` call.  ``load_adapt_fraction=0.0`` disables entirely
(the wrapper is never constructed — byte-identical planning).
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timedelta

RATIO_MIN = 0.7
RATIO_MAX = 1.5
MIN_MATCHED_HOURS = 2
LOG_MAX_ENTRIES = 48


class PredictionLog:
    """Ring buffer of base-tier P50 predictions keyed by UTC hour.

    Needed because the remote tier's forecast map only holds future hours —
    past hours cannot be re-queried after the fact.  In-memory only: after a
    restart the corrector is silently inactive until the log refills.
    """

    def __init__(self) -> None:
        self._entries: OrderedDict[datetime, float] = OrderedDict()

    def record(self, hour_dt: datetime, p50_w: float) -> None:
        if hour_dt in self._entries:
            self._entries.pop(hour_dt)
        self._entries[hour_dt] = float(p50_w)
        while len(self._entries) > LOG_MAX_ENTRIES:
            self._entries.popitem(last=False)

    def get(self, hour_dt: datetime) -> float | None:
        return self._entries.get(hour_dt)

    def __len__(self) -> int:
        return len(self._entries)


def compute_ratio(
    log: PredictionLog,
    past_actuals: dict,
    now_h: datetime,
    window_h: int,
) -> tuple[float | None, int]:
    """Bounded actual/predicted energy ratio over the last ``window_h`` hours.

    Only hours present in BOTH the log and past_actuals count; requires
    ``MIN_MATCHED_HOURS`` matches else ``(None, matched)``.
    """
    pred_sum = 0.0
    act_sum = 0.0
    matched = 0
    for back in range(1, max(1, int(window_h)) + 1):
        h = now_h - timedelta(hours=back)
        pred = log.get(h)
        actual = (past_actuals.get(h) or {}).get("load_w")
        if pred is None or actual is None or pred <= 0.0 or actual < 0.0:
            continue
        pred_sum += pred
        act_sum += float(actual)
        matched += 1
    if matched < MIN_MATCHED_HOURS or pred_sum <= 0.0:
        return None, matched
    return min(RATIO_MAX, max(RATIO_MIN, act_sum / pred_sum)), matched


class AdaptivePredictor:
    """Duck-typed predictor wrapper: base P50 × lead-time-faded ratio.

    factor(lead) = 1 + fraction·(ratio−1)·max(0, 1 − lead_h/fade_h)
    """

    def __init__(
        self, base, ratio: float, now: datetime, fade_h: int, fraction: float,
    ) -> None:
        self._base = base
        self._ratio = float(ratio)
        self._now = now
        self._fade_h = float(fade_h)
        self._fraction = float(fraction)

    def predict(
        self,
        when: datetime,
        temp: float | None,
        fallback_w: float,
        *,
        quantile: float = 0.5,
    ) -> float:
        base_w = self._base.predict(when, temp, fallback_w, quantile=quantile)
        if self._fade_h <= 0.0:
            return base_w
        lead_h = max(0.0, (when - self._now).total_seconds() / 3600.0)
        fade = max(0.0, 1.0 - lead_h / self._fade_h)
        factor = 1.0 + self._fraction * (self._ratio - 1.0) * fade
        return base_w * factor
