"""Plan B — persistence price prior.

A rolling store of realized hourly import prices (B1), a blended next-day estimate
(B2), a SEPARATE estimated-slot list bounded to the pre-solar morning window (B3),
and an upside-only reserve-raise input (B4).  Estimated prices NEVER enter the real
`slots` list or the DP price arrays — the only effect is a non-negative bump to the
per-hour ride-out reserve, applied by controller._apply_price_prior.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import optimize
from .models import Config, PriceSlot
from .regret import windowed_peak_prices


# ── B1: rolling realized-price store ──────────────────────────────────────────

def prune_history(
    history: dict[str, dict[str, float]], max_days: int
) -> dict[str, dict[str, float]]:
    """Ring buffer: keep only the `max_days` most-recent date-keyed entries."""
    if max_days <= 0 or len(history) <= max_days:
        return dict(history)
    keep = sorted(history)[-max_days:]
    return {d: history[d] for d in keep}


def extract_realized_day(slots: list[PriceSlot], target_date: date) -> dict[str, float]:
    """Bucket one local date's realized import prices by local hour.

    `slots` may carry elapsed hours (the Zonneplan `forecast` attribute spans ~24 h
    back and `parse_price_curve` has no now-filter), so the just-finished day is
    present at the local-day rollover.  Returns {str(local_hour): price_eur_per_kwh};
    may hold < 24 keys when hours are missing (completeness judged by the consumer).
    """
    out: dict[str, float] = {}
    for s in slots:
        local = dt_util.as_local(s.start)
        if local.date() == target_date:
            out[str(local.hour)] = s.price
    return out


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
        self._history = prune_history(
            {**self._history, date_iso: hourly}, self._max_days
        )
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
    """estimated_tomorrow[h] = w·today[h] + (1−w)·same_weekday_last_week[h].

    `target_date` is tomorrow (the day being estimated).  Fallbacks: today incomplete
    → most-recent full day as the `today` term; same-weekday missing → today-only
    (w=1).  Alignment is by LOCAL hour (DST 23/25-h days degrade via the 24-key
    completeness check).  Returns a 24-length local-hour-indexed list, or None when no
    usable full base day exists.
    """
    today = history.get((target_date - timedelta(days=1)).isoformat())
    if today is None or len(today) < 24:
        # Before local-day rollover, today's entry doesn't exist yet — falls back to
        # the most-recent full day in the ring (yesterday) as the "today" term.
        today = _most_recent_full_day(history)
    if today is None or len(today) < 24:
        return None
    same_wd = history.get((target_date - timedelta(days=7)).isoformat())
    if same_wd is not None and len(same_wd) >= 24:
        return [
            weight_today * today[str(h)] + (1.0 - weight_today) * same_wd[str(h)]
            for h in range(24)
        ]
    return [today[str(h)] for h in range(24)]


# ── B3: estimated slots — SEPARATE list, bounded to the pre-solar window ──────

def build_estimated_slots(
    estimated_tomorrow: list[float] | None,
    real_horizon_end: datetime,
    tomorrow_solar_pickup: datetime | None,
) -> list[PriceSlot]:
    """Estimated PriceSlots covering ONLY [real_horizon_end, tomorrow_solar_pickup).

    Empty (the deferral case) when there is no estimate, no pickup, or real prices
    already extend past tonight (real_horizon_end >= pickup).  These slots are passed
    ONLY to compute_anticipation_held_extra — NEVER merged into `slots`.
    """
    if (
        estimated_tomorrow is None
        or tomorrow_solar_pickup is None
        or real_horizon_end >= tomorrow_solar_pickup
    ):
        return []
    out: list[PriceSlot] = []
    h = real_horizon_end
    while h < tomorrow_solar_pickup:
        out.append(PriceSlot(h, estimated_tomorrow[dt_util.as_local(h).hour]))
        h += timedelta(hours=1)
    return out


# ── B4: upside-only held-extra (withhold tonight, hold for tomorrow morning) ──

def compute_anticipation_held_extra(
    *,
    estimated_slots: list[PriceSlot],
    real_slots: list[PriceSlot],
    now_h: datetime,
    real_horizon_end: datetime,
    tomorrow_solar_pickup: datetime,
    base_reserve_by_hour: dict[datetime, float],
    cfg: Config,
    slot_minutes: int = 60,
    eta_curve=None,
) -> float:
    """DC kWh (>= 0) to withhold from tonight's cheapest export hours and hold for
    tomorrow morning's anticipated peak.

    est_morning = (1 − haircut) · max effective EXPORT price over the estimated
    pre-solar window.  Walk tonight's export-eligible hours (clear the same peak band
    the DP uses) cheapest-first; while est_morning ≥ effective_export_price(hour) +
    margin, redirect that hour's exportable energy (the AC export cap, DC-converted)
    into `held`.  Cap by tomorrow-morning sellable capacity (export cap × estimated
    pre-solar hours) and by pack headroom above the survival reserve — so the result
    can only RAISE the reserve, never force an export or breach survival.

    `slot_minutes` resolves the REAL `real_slots` side to a per-slot dt_h (dual-
    resolution split — see below); `estimated_slots` is always the hourly day-ring
    estimate regardless of `slot_minutes`.

    `eta_curve`, when given, supplies a power-dependent discharge efficiency at
    the AC export-cap bin instead of the static `optimize.eta_discharge(cfg)`
    scalar. `eta_curve=None` (default) reproduces today's behaviour byte-for-byte.
    """
    if not estimated_slots:
        return 0.0
    dt_h_real = slot_minutes / 60.0
    haircut = cfg.anticipation_confidence_haircut
    margin = cfg.anticipation_margin_eur_per_kwh
    est_morning = (1.0 - haircut) * max(
        optimize.effective_export_price(s.price, cfg) for s in estimated_slots
    )
    # Real export-effective prices over the horizon → peak band (mirrors optimize.py).
    horizon = [s for s in real_slots if now_h <= s.start < real_horizon_end]
    if not horizon:
        return 0.0
    # Per-hour windowed peak (mirrors optimize.py's export gate) so the
    # tonight export-eligible set matches what the DP will actually export.
    horizon = sorted(horizon, key=lambda s: s.start)
    eprices = [optimize.effective_export_price(s.price, cfg) for s in horizon]
    _base_day = horizon[0].start.date()
    peak_ref = windowed_peak_prices(
        eprices,
        round(cfg.export_peak_lookback_h / dt_h_real),
        day_index=[(s.start.date() - _base_day).days for s in horizon],
    )
    band = cfg.export_peak_band_frac
    tonight = sorted(
        (
            s for i, s in enumerate(horizon)
            if s.start < tomorrow_solar_pickup
            and eprices[i] >= peak_ref[i] * (1.0 - band) - 1e-9
        ),
        key=lambda s: s.price,   # cheapest export hour first
    )
    ac_cap_w = min(cfg.max_export_w, cfg.grid_export_limit_w)
    eta_d = (
        optimize.eta_discharge(cfg) if eta_curve is None
        else eta_curve.eta_discharge(ac_cap_w)
    )
    ac_cap_kwh = ac_cap_w / 1000.0
    per_hour_dc = ac_cap_kwh / eta_d if eta_d > 1e-9 else 0.0
    per_slot_dc_real = per_hour_dc * dt_h_real
    held = 0.0
    for s in tonight:
        if est_morning < optimize.effective_export_price(s.price, cfg) + margin:
            break
        held += per_slot_dc_real
    if held <= 0.0:
        return 0.0
    # Cap 1 — tomorrow-morning sellable capacity (export cap × pre-solar hours).
    # estimated side stays hourly (24-key day-ring); only the REAL tonight side scales per-slot
    morning_sellable_dc = per_hour_dc * len(estimated_slots)
    # Cap 2 — pack headroom above the survival reserve at the held (pre-pickup) hours.
    affected = [r for h, r in base_reserve_by_hour.items() if h < tomorrow_solar_pickup]
    soc_target_kwh = cfg.capacity_kwh * cfg.soc_target / 100.0
    pack_headroom = soc_target_kwh - (max(affected) if affected else 0.0)
    return max(0.0, min(held, morning_sellable_dc, pack_headroom))
