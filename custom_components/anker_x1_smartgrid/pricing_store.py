"""Plan B — persistence price prior.

A rolling store of realized hourly import prices (B1), a blended next-day estimate
(B2), and a SEPARATE estimated-slot list bounded to the pre-solar morning window (B3).
Estimated prices NEVER enter the real `slots` list or the DP price arrays.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from statistics import median

from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .models import PriceSlot


# ── B1: rolling realized-price store ──────────────────────────────────────────


def prune_history(history: dict[str, dict[str, float]], max_days: int) -> dict[str, dict[str, float]]:
    """Ring buffer: keep only the `max_days` most-recent date-keyed entries."""
    if max_days <= 0 or len(history) <= max_days:
        return dict(history)
    keep = sorted(history)[-max_days:]
    return {d: history[d] for d in keep}


def extract_realized_day(slots: list[PriceSlot], target_date: date) -> dict[str, float]:
    """Bucket one local date's realized import prices by local hour (mean of the hour's slots).

    `slots` may carry elapsed hours (the Zonneplan `forecast` attribute spans ~24 h
    back and `parse_price_curve` has no now-filter), so the just-finished day is
    present at the local-day rollover.  Returns {str(local_hour): price_eur_per_kwh};
    may hold < 24 keys when hours are missing (completeness judged by the consumer).
    """
    by_hour: dict[str, list[float]] = {}
    for s in slots:
        local = dt_util.as_local(s.start)
        if local.date() == target_date:
            by_hour.setdefault(str(local.hour), []).append(s.price)
    return {h: sum(p) / len(p) for h, p in by_hour.items()}


class PriceHistoryStore:
    """Thin async wrapper over HA `Store` holding the date-keyed price ring."""

    def __init__(self, store: Store, *, max_days: int) -> None:
        self._store = store
        self._max_days = max_days
        self._history: dict[str, dict[str, float]] = {}

    async def async_load(self) -> None:
        saved = await self._store.async_load()
        if saved and isinstance(saved.get("history"), dict):
            self._history = saved["history"]

    @property
    def history(self) -> dict[str, dict[str, float]]:
        return self._history

    async def async_snapshot(self, date_iso: str, hourly: dict[str, float]) -> None:
        """Date-keyed write of one realized day; ring-prune + persist.

        Completeness guard: a stored day is never overwritten by a LESS-complete
        snapshot (e.g. after a mid-day restart the Zonneplan back-horizon may no
        longer cover all 24 of yesterday's hours).  We keep the entry with the
        most keys; on a tie the existing entry wins.
        """
        if not hourly:
            return
        existing = self._history.get(date_iso)
        if existing is not None and len(existing) >= len(hourly):
            return  # existing is at least as complete — skip overwrite
        self._history = prune_history({**self._history, date_iso: hourly}, self._max_days)
        await self._store.async_save({"history": self._history})


# ── B2: blended tomorrow estimate ─────────────────────────────────────────────


def _most_recent_full_day(history: dict[str, dict[str, float]]) -> dict[str, float] | None:
    for d in sorted(history, reverse=True):
        if len(history[d]) >= 24:
            return history[d]
    return None


def blend_price_prior(
    history: dict[str, dict[str, float]],
    target_date: date,
    *,
    weight_today: float,
) -> list[float] | None:
    """estimated_tomorrow[h] = w·today[h] + (1−w)·typical[h].

    `target_date` is tomorrow (the day being estimated).  Fallbacks: today incomplete
    → most-recent full day as the `today` term.  `typical[h]` is the per-hour median over
    all full days in the history when there are at least 3 (one spike day cannot skew it);
    with fewer, the same weekday last week if present, else today-only (w=1).
    Alignment is by LOCAL hour (DST 23/25-h days degrade via the 24-key completeness check).
    Returns a 24-length local-hour-indexed list, or None when no usable full base day exists.
    """
    today = history.get((target_date - timedelta(days=1)).isoformat())
    if today is None or len(today) < 24:
        # Before local-day rollover, today's entry doesn't exist yet — falls back to
        # the most-recent full day in the ring (yesterday) as the "today" term.
        today = _most_recent_full_day(history)
    if today is None or len(today) < 24:
        return None
    full = [d for d in history.values() if len(d) >= 24]
    if len(full) >= 3:
        return [
            weight_today * today[str(h)] + (1.0 - weight_today) * median(d[str(h)] for d in full) for h in range(24)
        ]
    same_wd = history.get((target_date - timedelta(days=7)).isoformat())
    if same_wd is not None and len(same_wd) >= 24:
        return [weight_today * today[str(h)] + (1.0 - weight_today) * same_wd[str(h)] for h in range(24)]
    return [today[str(h)] for h in range(24)]


def estimate_day_after_horizon(
    history: dict[str, dict[str, float]],
    slots: list[PriceSlot],
    tomorrow: date,
    *,
    weight_today: float,
) -> list[float] | None:
    # Published tomorrow joins the (unpersisted) history as the freshest base day.
    published = extract_realized_day(slots, tomorrow)
    if len(published) >= 24:
        return blend_price_prior(
            {**history, tomorrow.isoformat(): published}, tomorrow + timedelta(days=1), weight_today=weight_today
        )
    # The history's today entry only lands at the day rollover; the feed already holds today.
    today = tomorrow - timedelta(days=1)
    realized = extract_realized_day(slots, today)
    if len(realized) >= 24:
        history = {**history, today.isoformat(): realized}
    return blend_price_prior(history, tomorrow, weight_today=weight_today)


# ── B3: estimated slots — SEPARATE list, bounded to the pre-solar window ──────


def build_estimated_slots(
    estimated_tomorrow: list[float] | None,
    real_horizon_end: datetime,
    tomorrow_solar_pickup: datetime | None,
) -> list[PriceSlot]:
    """Estimated PriceSlots covering ONLY [real_horizon_end, tomorrow_solar_pickup).

    Never more than 24 h past ``real_horizon_end``: the estimate is a single day,
    and repeating it across a multi-day sunless gap would invent prices.
    Empty (the deferral case) when there is no estimate, no pickup, or real prices
    already extend past tonight (real_horizon_end >= pickup).  These slots are NEVER
    merged into the DP's `slots` and never reach `_dp_select_slots`; the display
    layer appends them to its own copy of the slot list via
    `build_display_horizon(est_slots=...)` to render the estimated tail.
    """
    if estimated_tomorrow is None or tomorrow_solar_pickup is None or real_horizon_end >= tomorrow_solar_pickup:
        return []
    out: list[PriceSlot] = []
    h = real_horizon_end
    while h < min(tomorrow_solar_pickup, real_horizon_end + timedelta(hours=24)):
        out.append(PriceSlot(h, estimated_tomorrow[dt_util.as_local(h).hour]))
        h += timedelta(hours=1)
    return out
