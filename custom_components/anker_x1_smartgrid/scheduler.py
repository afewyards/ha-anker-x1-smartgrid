"""Pure scheduling logic: deadline, peak detection, slot selection, state machine."""

from __future__ import annotations

import logging
import statistics
from datetime import datetime, timedelta

from . import const, dp_common
from .models import Config, ControllerState, ExportState, ForecastInterval, PlanState, PriceSlot
from .dp_common import eta_charge_at, eta_discharge_at

_LOGGER = logging.getLogger(__name__)


def detect_evening_peak(now: datetime, slots: list[PriceSlot], cfg: Config) -> datetime | None:
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


def compute_deadline(now: datetime, sunset: datetime, slots: list[PriceSlot], cfg: Config) -> datetime:
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
    eta_c = eta_charge_at(cfg.max_charge_w, cfg, eta_curve)
    eta_d = eta_discharge_at(const.DEFAULT_FALLBACK_LOAD_W, cfg, eta_curve)
    return peak * eta_c * eta_d


def _slot_start(dt: datetime, slot_minutes: int = 60) -> datetime:
    """Floor `dt` to the `slot_minutes` grid. At 60 == byte-identical hour floor."""
    minute = (dt.minute // slot_minutes) * slot_minutes
    return dt.replace(minute=minute, second=0, microsecond=0)


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


_DWELL_CLAMP_LOGGED: set[tuple[str, int, int]] = set()
"""(label, configured_min, slot_minutes) combos already warned about, so a
stable misconfiguration logs once instead of every tick."""


def _effective_dwell_min(configured_min: int, slot_minutes: int, *, label: str) -> int:
    """Clamp ``configured_min`` strictly below ``slot_minutes`` (N1 fix).

    The dwell short-circuit in ``decide_state``/``decide_export_state``
    ("within dwell -> stay") skips the per-slot price/hurdle re-check entirely
    while dwell hasn't elapsed. If the configured dwell is >= the slot length,
    that short-circuit can span an entire unselected slot without the gate
    ever firing. Clamping the EFFECTIVE dwell to ``slot_minutes - 1`` (floor 1
    minute) guarantees at least one re-check per slot boundary crossed.

    At ``slot_minutes=60`` with the 15-min default this never binds (15 <
    59) -- byte-identical to the pre-fix behaviour. Logs once per distinct
    (label, configured_min, slot_minutes) combo when it does bind.
    """
    ceiling = max(1, slot_minutes - 1)
    effective = min(configured_min, ceiling)
    if effective != configured_min:
        key = (label, configured_min, slot_minutes)
        if key not in _DWELL_CLAMP_LOGGED:
            _DWELL_CLAMP_LOGGED.add(key)
            _LOGGER.warning(
                "%s=%d min >= the %d-min slot length; clamping the effective dwell to "
                "%d min so the per-slot price gate cannot be bypassed",
                label,
                configured_min,
                slot_minutes,
                effective,
            )
    return effective


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
    if soc >= cfg.charge_stop_pct:
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

    _effective_min_dwell = _effective_dwell_min(cfg.min_dwell_min, slot_minutes, label="min_dwell_min")
    dwell_elapsed = (now - plan.state_since) >= timedelta(minutes=_effective_min_dwell)
    now_selected = _slot_start(now, slot_minutes) in {_slot_start(s, slot_minutes) for s in selected_slots}

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
    slot_minutes: int = 60,
) -> ExportState:
    """Next ExportState applying two-sided eps band, dwell, and hurdle gate.

    Engage when: surplus_kwh > cfg.export_eps_hi_kwh AND hurdle_clears AND dwell elapsed.
    Disengage when: surplus_kwh < cfg.export_eps_lo_kwh OR NOT hurdle_clears (dwell-gated).
    Within [eps_lo, eps_hi]: hysteresis dead zone — no state change.

    ``slot_minutes`` (default 60, legacy-identical) clamps the effective dwell
    strictly below the slot length -- see :func:`_effective_dwell_min` (N1).
    """
    _effective_export_dwell = _effective_dwell_min(cfg.export_dwell_min, slot_minutes, label="export_dwell_min")
    dwell_elapsed = (now - prev.state_since) >= timedelta(minutes=_effective_export_dwell)

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


def decide_hold_state(
    prev: ExportState,
    *,
    discharge_value: float | None,
    stored_value: float | None,
    now: datetime,
    cfg: Config,
    slot_minutes: int = 60,
    dp_holds: bool = True,
    paused: bool = False,
) -> ExportState:
    """Next low-power-hold state (``engaged`` = holding), on the export dwell pattern.

    ``discharge_value``/``stored_value`` are €/DC-kWh; either ``None`` means a
    hold is not allowed this tick and releases at once, without dwell. Engage
    when discharging is worth less than the stored kWh; release only once it
    beats the stored kWh by ``LOW_POWER_HOLD_RELEASE_MARGIN_EUR_PER_KWH``.
    Both economic flips wait out ``export_dwell_min``.

    ``dp_holds`` False means the DP weighed a hold here and declined: treated
    as an economic flip, not a blocker, so a DP that changes its mind between
    ticks cannot flap the work mode.

    ``paused`` marks the block as a forced charge, which has its own dwell: it
    starts none here, so a burst that reaches the charge stop hands straight
    back to the hold instead of draining the pack until the dwell runs out.
    """
    _dwell = timedelta(minutes=_effective_dwell_min(cfg.export_dwell_min, slot_minutes, label="export_dwell_min"))
    if discharge_value is None or stored_value is None:
        if not prev.engaged:
            return prev
        return ExportState(engaged=False, state_since=min(prev.state_since, now - _dwell) if paused else now)
    if now - prev.state_since < _dwell:
        return prev
    if prev.engaged:
        if not dp_holds or discharge_value > stored_value + const.LOW_POWER_HOLD_RELEASE_MARGIN_EUR_PER_KWH:
            return ExportState(engaged=False, state_since=now)
        return prev
    if dp_holds and discharge_value < stored_value:
        return ExportState(engaged=True, state_since=now)
    return prev


def backup_reserve_band(cfg: Config) -> tuple[float, float]:
    """The planner's snapped backup reserve as SoC %, and the SoC % the reserve hold releases at."""
    reserve = cfg.kwh_to_pct(dp_common.reserve_wall_kwh(cfg))
    return reserve, reserve + 1.0


def backup_reserve_hold(
    prev: ExportState,
    *,
    soc: float,
    pv_surplus: bool,
    now: datetime,
    cfg: Config,
    slot_minutes: int = 60,
    dips: bool = False,
) -> ExportState | None:
    """Hold state that keeps the pack at the backup reserve; ``None`` = no opinion.

    Engages at once at or under the planner's snapped reserve and releases only
    once SoC is a full point above it and ``export_dwell_min`` has run, so a
    pack hovering at the reserve cannot flap the work mode. PV covering the
    house is no opinion, after the same dwell: the pack should charge from it.
    ``dips`` (the plan runs the pack under the reserve in this slot) is no
    opinion the same way.
    """
    if cfg.backup_reserve_soc <= 0.0:
        return None
    dwell = timedelta(minutes=_effective_dwell_min(cfg.export_dwell_min, slot_minutes, label="export_dwell_min"))
    if pv_surplus or dips:
        return prev if prev.engaged and now - prev.state_since < dwell else None
    reserve, release = backup_reserve_band(cfg)
    if soc <= reserve:
        return prev if prev.engaged else ExportState(engaged=True, state_since=now)
    if not prev.engaged:
        return None
    if soc >= release and now - prev.state_since >= dwell:
        return ExportState(engaged=False, state_since=now)
    return prev
