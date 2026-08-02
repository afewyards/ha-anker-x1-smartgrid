"""Current-hour partial-actual blend for the load forecast.

Rectangle-rule integration of per-tick house load into a per-hour kWh
accumulator (mirrors recorder's per-tick ``_kwh`` derivation), plus a
predictor wrapper that replaces the CURRENT hour's model value with
``observed kWh so far + model × remaining fraction``.  Applied as the
OUTERMOST wrapper so the load-adapt log never records a blended value.
Blend keys on an exact ``when == now_h`` (hour-floored) match.  Since
``build_display_intervals`` (plan.py) predicts once per HOUR rather than once
per slot, that match is on the hour itself and the blend engages at ANY slot
width, not only when ``slot_minutes == 60``.  Caveat: at sub-hour slot widths
the emitted rows for the current hour cover only the remaining part of that
hour, while the blend's value is a whole-hour mean that already includes
observed (elapsed) energy — so enabling ``current_hour_blend`` at sub-hour
resolution feeds elapsed energy into forward slots.  Pure module: no HA
imports, no I/O.
"""

from __future__ import annotations

from datetime import datetime

from .resolution import hour_floor

MIN_COVERAGE_S = 600.0  # ≥10 observed minutes before the blend engages
MAX_STEP_S = 300.0  # ticks further apart than this break integration


class HourAccumulator:
    """In-memory ∫P dt over the current clock hour (UTC, hour-floored key)."""

    def __init__(self) -> None:
        self.hour: datetime | None = None
        self.kwh: float = 0.0
        self.covered_s: float = 0.0
        self._last_ts: datetime | None = None

    def add(self, ts: datetime, load_w: float | None) -> None:
        hour = hour_floor(ts)
        if self.hour != hour:
            self.hour = hour
            self.kwh = 0.0
            self.covered_s = 0.0
            self._last_ts = None
        if load_w is None:
            self._last_ts = None  # unknown tick breaks the integration chain
            return
        if self._last_ts is not None:
            dt_s = (ts - self._last_ts).total_seconds()
            if 0.0 < dt_s <= MAX_STEP_S:
                self.kwh += float(load_w) * dt_s / 3_600_000.0
                self.covered_s += dt_s
        self._last_ts = ts


class CurrentHourBlendPredictor:
    """Duck-typed predictor wrapper: current slot = observed + model remainder."""

    def __init__(self, base, acc: HourAccumulator, now_h: datetime) -> None:
        self._base = base
        self._acc = acc
        self._now_h = now_h

    def predict(
        self,
        when: datetime,
        temp: float | None,
        fallback_w: float,
        *,
        quantile: float = 0.5,
    ) -> float:
        base_w = self._base.predict(when, temp, fallback_w, quantile=quantile)
        if when != self._now_h or self._acc.hour != self._now_h or self._acc.covered_s < MIN_COVERAGE_S:
            return base_w
        frac_rem = max(0.0, 1.0 - self._acc.covered_s / 3600.0)
        return self._acc.kwh * 1000.0 + base_w * frac_rem
