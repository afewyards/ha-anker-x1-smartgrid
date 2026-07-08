"""Pure scheduling logic: deadline, peak detection, slot selection, state machine."""
from __future__ import annotations

import math
import statistics
from datetime import datetime, timedelta

from . import const
from .models import Config, ControllerState, ExportState, ForecastInterval, PlanState, PriceSlot
from .regret import _eta_charge_at, _eta_discharge_at


def detect_evening_peak(
    now: datetime, slots: list[PriceSlot], cfg: Config
) -> datetime | None:
    """First rising slot after peak_after_hour whose price >= median * peak_k."""
    future = [s for s in slots if s.start >= now]
    if len(future) < 2:
        return None
    median = statistics.median([s.price for s in future])
    threshold = max(median, 0.0) * cfg.peak_k
    prev_price = None
    for s in future:
        rising = prev_price is None or s.price > prev_price
        if s.start.hour >= cfg.peak_after_hour and s.price >= threshold and rising:
            return s.start
        prev_price = s.price
    return None


def compute_deadline(
    now: datetime, sunset: datetime, slots: list[PriceSlot], cfg: Config
) -> datetime:
    """min(sunset-buffer, peak) clamped to [now+min_dwell, sunset-buffer]."""
    hard = sunset - timedelta(minutes=cfg.deadline_buffer_min)
    peak = detect_evening_peak(now, slots, cfg)
    candidate = min(hard, peak) if peak is not None else hard
    floor = now + timedelta(minutes=cfg.min_dwell_min)
    # never later than hard, never earlier than floor (but floor cannot exceed hard)
    candidate = min(candidate, hard)
    return max(candidate, min(floor, hard))


def charge_price_ceiling(peak, cfg, eta_curve=None):
    """Max price at which grid-charging still saves money vs the peak,
    after round-trip losses. None if peak unknown.

    ``eta_curve`` (optional measured :class:`~efficiency.EfficiencyCurve`):
    when supplied, the round-trip factor is looked up from the curve —
    charge side at the grid-charge rate (``cfg.max_charge_w``, always
    full-rate) and discharge side at the typical fallback load power —
    instead of the static ``cfg.round_trip_eff`` scalar. ``eta_curve=None``
    (the default) preserves today's byte-identical behaviour."""
    if peak is None:
        return None
    eta_c = _eta_charge_at(cfg.max_charge_w, cfg, eta_curve)
    eta_d = _eta_discharge_at(const.DEFAULT_FALLBACK_LOAD_W, cfg, eta_curve)
    return peak * eta_c * eta_d


def _slot_start(dt: datetime, slot_minutes: int = 60) -> datetime:
    """Floor `dt` to the `slot_minutes` grid. At 60 == byte-identical hour floor."""
    minute = (dt.minute // slot_minutes) * slot_minutes
    return dt.replace(minute=minute, second=0, microsecond=0)


def _percentile(values: list[float], pct: float) -> float:
    """Linear-interpolation percentile (numpy-default method). Pure stdlib."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (pct / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return s[int(k)]
    return s[f] + (s[c] - s[f]) * (k - f)


def find_next_trough(
    now: datetime, slots: list[PriceSlot], cfg: Config
) -> tuple[datetime, float]:
    """Next local price minimum that defines the optimization horizon.

    Scans a fixed ``trough_lookahead_h`` window of forward prices, finds the
    earliest local minimum below the ``trough_percentile`` threshold that is at
    least ``min_horizon_h`` hours out, and returns ``(trough_hour_start,
    trough_price)``.  ``trough_hour_start`` is hour-aligned; the window
    ``[now, trough]`` is inclusive of the trough hour.

    Fallback (no qualifying trough, or fewer than two slots): horizon = the last
    known price slot, reference price = the minimum remaining price.
    """
    now_h = _slot_start(now)
    horizon_end = now_h + timedelta(hours=cfg.trough_lookahead_h)
    window = [s for s in slots if now_h <= s.start < horizon_end]

    if len(window) < 2:
        remaining = [s for s in slots if s.start >= now_h]
        if not remaining:
            return now_h, 0.0
        last = max(remaining, key=lambda s: s.start)
        return _slot_start(last.start), min(s.price for s in remaining)

    prices = [s.price for s in window]
    threshold = _percentile(prices, cfg.trough_percentile)
    min_start = now_h + timedelta(hours=cfg.min_horizon_h)
    n = len(window)

    candidates: list[PriceSlot] = []
    for i, s in enumerate(window):
        left = window[i - 1].price if i > 0 else float("inf")
        right = window[i + 1].price if i + 1 < n else float("inf")
        is_local_min = s.price <= left and s.price <= right
        if is_local_min and s.price < threshold and s.start >= min_start:
            candidates.append(s)

    if candidates:
        chosen = min(candidates, key=lambda s: s.start)
        return _slot_start(chosen.start), chosen.price

    # No qualifying local min: fall back to the end of known prices.
    last = max(window, key=lambda s: s.start)
    return _slot_start(last.start), min(prices)


_PV_SURPLUS_THRESHOLD_W = 200.0
"""Minimum net PV surplus (pv_w - load_w) that counts as a meaningful charging
opportunity.  Kept as a module constant rather than a Config field because the
plan does not add a new config entry for it, and 200 W is a practical lower
bound (below this the inverter self-consumption would dominate)."""


def find_next_solar_pickup(
    now: datetime,
    intervals: list[ForecastInterval],
) -> datetime | None:
    """Earliest future hour-start where PV surplus marks the next solar recovery.

    Unlike :func:`find_next_charge_opportunity`, this is PRICE-INDEPENDENT: a
    cheap nighttime price slot is NOT a charge opportunity (it would force a
    nighttime grid buy — exactly the behaviour the economic-only redesign
    removed).  Only a genuine PV pickup — the first interval whose
    ``pv_w - load_w >= _PV_SURPLUS_THRESHOLD_W`` at or after ``now`` — ends the
    ride-out window.

    Returns ``None`` when no interval in the (possibly multi-day) forecast shows
    a surplus, so the reserve spans the full available horizon.
    """
    now_h = _slot_start(now)
    for iv in sorted(intervals, key=lambda i: i.start):
        if _slot_start(iv.start) < now_h:
            continue
        if iv.pv_w - iv.load_w >= _PV_SURPLUS_THRESHOLD_W:
            return _slot_start(iv.start)
    return None


def decide_state(
    plan: PlanState,
    *,
    soc: float,
    now: datetime,
    selected_slots: list[datetime],
    cfg: Config,
    charge_ceiling_soc: float | None = None,
    slot_minutes: int = 60,
) -> PlanState:
    """Next PlanState applying high-SoC guard, dwell, hysteresis, commitment.

    ``charge_ceiling_soc`` is the solar-reservation ceiling (SoC%) for the
    current hour.  When provided, grid charging stops at this ceiling instead of
    ``soc_target`` — leaving room for the forecast solar surplus so the pack is
    not filled from the grid ahead of free solar.  ``None`` (heuristic fallback)
    disables the guard, preserving the prior soc_target-only behaviour.
    """
    # 1. High-SoC guard always wins.
    if soc >= cfg.soc_target - 1.0:
        if plan.state is ControllerState.PASSIVE:
            return plan
        return PlanState(ControllerState.PASSIVE, now, ())

    # 1b. Solar-reservation guard: never grid-charge past this hour's ceiling.
    #     Stops the executor at the ceiling (not soc_target), reserving headroom
    #     for the forecast solar that will land later in the cycle.
    if charge_ceiling_soc is not None and soc >= charge_ceiling_soc - 1e-9:
        if plan.state is ControllerState.PASSIVE:
            return plan
        return PlanState(ControllerState.PASSIVE, now, ())

    dwell_elapsed = (now - plan.state_since) >= timedelta(minutes=cfg.min_dwell_min)
    now_selected = _slot_start(now, slot_minutes) in {
        _slot_start(s, slot_minutes) for s in selected_slots
    }

    if plan.state is ControllerState.FORCING:
        # 2. Within dwell -> stay.
        if not dwell_elapsed:
            return plan
        # 3. Continue ONLY while the current hour is still a worthy charge slot.
        #    `selected_slots` is already price-gated upstream, so a current hour
        #    that is not selected means charging here is not worth it — stop and
        #    accept a partial fill rather than charge through an unworthy hour.
        #    (This makes the price gate apply continuously, not just at entry,
        #    and self-heals a FORCING plan restored across a restart.)
        if now_selected:
            return plan
        return PlanState(ControllerState.PASSIVE, now, ())

    # PASSIVE -> consider entering FORCING.
    if not dwell_elapsed:
        return plan
    if now_selected:
        return PlanState(ControllerState.FORCING, now, tuple(selected_slots))
    return plan


def decide_export_state(
    prev: ExportState,
    *,
    surplus_kwh: float,
    hurdle_clears: bool,
    now: datetime,
    cfg: Config,
) -> ExportState:
    """Next ExportState applying two-sided eps band, dwell, and hurdle gate.

    Engage when: surplus_kwh > cfg.export_eps_hi_kwh AND hurdle_clears AND dwell elapsed.
    Disengage when: surplus_kwh < cfg.export_eps_lo_kwh OR NOT hurdle_clears (dwell-gated).
    Within [eps_lo, eps_hi]: hysteresis dead zone — no state change.
    """
    dwell_elapsed = (now - prev.state_since) >= timedelta(minutes=cfg.export_dwell_min)

    if prev.engaged:
        # Within dwell -> stay.
        if not dwell_elapsed:
            return prev
        # Disengage if hurdle drops or surplus falls below lower band.
        if not hurdle_clears or surplus_kwh < cfg.export_eps_lo_kwh:
            return ExportState(engaged=False, state_since=now)
        # Surplus in dead zone or above: stay engaged.
        return prev

    # Disengaged -> consider engaging.
    if not dwell_elapsed:
        return prev
    if surplus_kwh > cfg.export_eps_hi_kwh and hurdle_clears:
        return ExportState(engaged=True, state_since=now)
    return prev
