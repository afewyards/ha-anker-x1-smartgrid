"""Pure planner core: DP window/slot selection, ride-out reserve, and
``compute_decision`` — the module-level pure-Python planning functions
extracted verbatim from ``controller.py`` (Task C2, 2026-07-12 refactor).

No Home Assistant imports here: this module (together with the leaf
modules it imports — optimize, regret, energy, scheduler, guard,
resolution, efficiency, models, const, plan, parsers, forecast,
export_filter) must be importable outside HA — in the addon container, in
backtests, and in unit tests without the HA test harness. The one
exception is ``pricing_store``, which itself imports
``homeassistant.helpers.storage`` — a pre-existing transitive dependency
of ``_apply_price_prior``'s price-prior lookup, unchanged by this move.

``controller.py`` re-exports every name below (``from .decision import
...``) so existing call sites and test imports keep working unchanged.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta

from . import const, energy, guard, optimize as optimize_mod, plan as plan_mod, pricing_store, regret as regret_mod, resolution, scheduler
from .efficiency import EfficiencyCurve
from .export_filter import apply_min_export_block
from .forecast import build_intervals
from .models import Config, ControllerState, ForecastInterval, PlanState, PlantInputs, PriceSlot
from .parsers import build_pv_curve_from_arrays, build_pv_curve_from_watts, build_two_day_pv_curve, synth_pv_curve

_LOGGER = logging.getLogger(__name__)

# Minimum AC kWh in a DP schedule slot to consider it a "selected" charging hour.
# Values below this threshold are rounding / binning artefacts from the DP
# discretisation (BIN_KWH ≈ 0.05 kWh) and are treated as zero charge.
_DP_EPSILON_SCHEDULE_KWH = 0.01

# NOTE: the regret.py oracle has NO charge band — optimize-vs-oracle parity runs
# with chargeable=None — so this look-back is intentionally controller-only and is
# NOT mirrored into regret.py/pricing_store.py (unlike the export peak band).
def _trough_by_hour(
    slots: list[PriceSlot],
    now_h: datetime,
    horizon_edge: datetime,
    lookback_slots: int,
    slot_minutes: int,
) -> dict[datetime, float]:
    """Per-hour windowed-trough reference for the cheap-charge band.

    ``trough[h] = min(real slot prices over [h - lookback_h, horizon_edge))`` for each
    clock hour ``h`` in ``[now_h, horizon_edge)`` — clamped to ``h``'s own calendar day
    via per-day ``day_index`` grouping, so a cheaper trough on a later calendar day
    does not lower the band for earlier hours.  Mirror of the export side's
    ``windowed_peak_prices`` (suffix-MIN), extended ``lookback_h`` hours into the
    elapsed past so an UP-SLOPE hour after the day's trough is judged against that
    trough (blocked by the band) instead of masquerading as the window-local minimum.

    Built on an hourly grid ``[now_h - lookback_h, horizon_edge)`` with per-day
    ``day_index`` derived from real datetimes; hours with no real price are filled
    with ``+inf`` (never lower a trough, keep the grid index-aligned).
    Real day-ahead prices are hourly-contiguous, matching the controller's grid model.
    """
    stride = timedelta(minutes=slot_minutes)
    slot_seconds = slot_minutes * 60
    window_len = max(0, int(round((horizon_edge - now_h).total_seconds() / slot_seconds)))
    if window_len == 0:
        return {}
    price_by_h = resolution.resample_price_map(slots, slot_minutes)
    start_h = now_h - lookback_slots * stride
    grid_len = lookback_slots + window_len
    prices_grid = [
        price_by_h.get(start_h + k * stride, float("inf"))
        for k in range(grid_len)
    ]
    _base_day = start_h.date()
    _day_index = [
        ((start_h + k * stride).date() - _base_day).days
        for k in range(grid_len)
    ]
    trough_grid = regret_mod.windowed_trough_prices(prices_grid, lookback_slots, day_index=_day_index)
    return {
        now_h + j * stride: trough_grid[lookback_slots + j]
        for j in range(window_len)
    }


def _dp_window(
    now: datetime, deadline: datetime, slot_minutes: int,
) -> tuple[datetime, datetime, int]:
    """Single source of truth for the DP's slot-aligned window.

    Returns ``(now_h, deadline_ceil, window_len)`` where ``now_h`` is ``now``
    floored to the slot grid and ``window_len`` is the slot count of
    ``[now_h, deadline_ceil)``. ``_dp_select_slots`` uses this to build its
    per-slot arrays; ``compute_decision`` calls it BEFORE the DP runs (BC1) so
    the reserve-floor list it hands to ``_dp_select_slots`` lines up 1:1 with
    the DP's own slot grid instead of drifting via a separately hour-floored
    ``now_h`` (the exact class of bug T4/T9 already fixed for
    ``selected``/``live_grid_request``/``live_ceiling_by_hour``: reusing an
    hour-floored anchor silently misaligns at sub-hour resolution whenever
    ``now`` sits outside the hour's first slot). At ``slot_minutes=60`` this
    reduces byte-identically to the legacy hour-floor arithmetic.
    """
    stride = timedelta(minutes=slot_minutes)
    slot_seconds = slot_minutes * 60
    now_h = resolution.floor_to_slot(now, slot_minutes)
    deadline_h = resolution.floor_to_slot(deadline, slot_minutes)
    deadline_ceil = deadline_h + stride if deadline > deadline_h else deadline_h
    window_len = max(1, int(round((deadline_ceil - now_h).total_seconds() / slot_seconds)))
    return now_h, deadline_ceil, window_len


def _dp_select_slots(
    inputs: PlantInputs,
    slots: list[PriceSlot],
    deadline: datetime,
    ceiling: float | None,
    cfg: Config,
    export_price: float | None,
    terminal_mode: str = "reserve",
    water_value: float | None = None,
    export_price_matches_import: bool = False,
    reserve_by_hour: list[float] | None = None,
    sun_times=None,
    *,
    intervals: list,
    hedge_drain_by_hour: dict[datetime, float] | None = None,
    slot_minutes: int = 60,
    dt_h: float = 1.0,
    eta_curve: EfficiencyCurve | None = None,
) -> tuple[
    list[datetime], dict[datetime, float], bool, dict[datetime, float], float,
    dict[datetime, float],
]:
    """Run the DP optimizer and return charge-slot datetimes for this tick.

    May raise on any error; the caller (``compute_decision``) wraps in
    try/except and falls back to PASSIVE (``selected=[]``).

    Window construction
    -------------------
    The window spans ``[now_h, deadline_ceil)`` aligned to clock hours.
    PV and load energy from ``intervals`` (P50) is bucketed into 1-hour
    slots via weighted time-overlap so that sub-hourly intervals (e.g. the
    first interval when ``now`` is mid-hour) are distributed correctly.

    Price-gate mask
    ---------------
    Derived from ``ceiling`` (the same value the PASSIVE path computed via
    ``scheduler.charge_price_ceiling``), passed through ``build_charge_mask``.
    Fail-closed semantics are preserved: ``ceiling=None`` → all-False mask →
    no DP charging, matching the PASSIVE path's behaviour when the peak is unknown.

    Export-credit term
    ------------------
    ``export_price`` (live feed-in tariff, €/kWh) seeds a per-hour export
    price forecast for the window.  In static tariff mode
    (``cfg.price_mode == PRICE_MODE_STATIC``), ``export_price`` is the
    configured constant and is always flat-broadcast — never ratio-scaled by
    the import curve's shape, even under an HP/HC import schedule.  In
    sensor mode, when ``export_price_matches_import=True`` (the common case:
    both entities point at the same Zonneplan tariff), ``feed_in =
    window_price`` exactly — the per-hour import curve is reused, so the
    planner sees the real evening peak in the export credit.  When the
    entities differ, the import curve is ratio-scaled by
    ``export_price / window_price[0]`` (current-hour import price), guarding
    against divide-by-zero (falls back to flat broadcast when current import ≈ 0).
    ``None`` → ``feed_in=None`` → credit term is a strict no-op (T0.1b
    parity invariant preserved).

    Co-optimized export (D2)
    ------------------------
    :func:`~optimize.optimize_grid` plans grid-charge and discharge-to-grid in
    a single DP pass.  The returned ``export_schedule`` is converted to the
    ``export_request`` dict: ``{datetime: float}`` where the float is the
    planned export rate in W for that clock hour.  Written to
    ``_out["export_request"]`` by the caller (``compute_decision``).

    Schedule → selected conversion
    -------------------------------
    Any window hour with ``schedule[h] > _DP_EPSILON_SCHEDULE_KWH`` (0.01 kWh)
    becomes a selected slot datetime, passed to ``decide_state`` identically
    to the heuristic path.
    """
    # Build slot-aligned window [now_h, deadline_ceil) — single source of
    # truth in _dp_window (BC1: compute_decision calls it too, to keep the
    # reserve-floor list aligned to this exact grid).
    stride = timedelta(minutes=slot_minutes)
    slot_seconds = slot_minutes * 60
    now_h, deadline_ceil, window_len = _dp_window(inputs.now, deadline, slot_minutes)
    # window_start_h now carries the window_start_slot value: slots since local
    # midnight at this window's own resolution (== wall-clock hour at 60-min).
    window_start_h = now_h.hour * (60 // slot_minutes) + now_h.minute // slot_minutes
    window_start_slot = window_start_h
    slots_per_day = 24 * 60 // slot_minutes
    _day_index = [(window_start_slot + h) // slots_per_day for h in range(window_len)]

    # Convert hedge dict → positional list aligned to [now_h, now_h+window_len).
    # Any forward-hour key works (keys land at the trough hour, not necessarily now_h).
    # None / empty dict → hedge_drain_kwh stays None → optimize_grid parity-safe.
    hedge_drain_kwh: list[float] | None = None
    if hedge_drain_by_hour:
        hedge_drain_kwh = [
            hedge_drain_by_hour.get(now_h + h * stride, 0.0)
            for h in range(window_len)
        ]

    # Price lookup by clock-hour start
    price_by_h: dict[datetime, float] = resolution.resample_price_map(slots, slot_minutes)

    # Bucket PV/load from P50 intervals into 1-hour window slots via weighted overlap.
    # Sub-hourly intervals (e.g. first interval when now is mid-hour) are spread
    # proportionally across the buckets they overlap.  PV is quantile-independent;
    # load is P50 (expected) so the DP exports the expected surplus rather than
    # holding it for worst-case overnight load.  Survival is the firmware's job
    # (5% hard floor); no P80 series is needed here.
    window_pv: list[float] = [0.0] * window_len
    window_load_reserve: list[float] = [0.0] * window_len
    for iv in (intervals or []):
        iv_end = iv.start + timedelta(hours=iv.dt_h)
        for h in range(window_len):
            bucket_start = now_h + h * stride
            bucket_end = bucket_start + stride
            ov_start = max(iv.start, bucket_start)
            ov_end = min(iv_end, bucket_end)
            if ov_end <= ov_start:
                continue
            ov_h = (ov_end - ov_start).total_seconds() / 3600.0
            window_pv[h] += iv.pv_w * ov_h / 1000.0
            window_load_reserve[h] += iv.load_w * ov_h / 1000.0

    # Price array aligned to window buckets
    window_price: list[float] = [
        price_by_h.get(now_h + h * stride, 0.0)
        for h in range(window_len)
    ]

    # Per-hour windowed-trough reference (look-back) for the cheap-charge band.
    _lookback_slots = round(cfg.charge_trough_lookback_h / dt_h)
    _trough_map = _trough_by_hour(slots, now_h, deadline_ceil, _lookback_slots, slot_minutes)
    _trough_list = [
        _trough_map.get(now_h + h * stride) for h in range(window_len)
    ]

    # Chargeability mask: ceiling AND the per-hour look-back trough band.
    # price_valid rejects 0.0-padded phantom-price hours (no real price data at
    # that bucket) so they fail closed instead of silently satisfying the
    # trough band via the injected 0.0 pad.
    _price_valid = [(now_h + h * stride) in price_by_h for h in range(window_len)]
    chargeable = optimize_mod.build_charge_mask(
        window_price, ceiling,
        price_band=cfg.charge_window_price_band,
        trough=_trough_list,
        price_valid=_price_valid,
    )

    # Per-hour export price for the co-optimized DP (optimize_grid)
    # and for the solar-spill credit term (feed_in, full effective price).
    #
    # Strategy (fixes flat-broadcast bug — planner was blind to evening peaks):
    #
    # Case 0 — static tariff mode (cfg.price_mode == PRICE_MODE_STATIC).
    #   export_price is the configured constant (static_price_export), not a
    #   sensor value that tracks the import curve — always flat-broadcast it.
    #   Never ratio-scale by the import curve's shape here: an HP/HC import
    #   schedule would otherwise make the export credit swing between hours
    #   instead of staying the flat configured constant.
    #
    # Case 1 — export entity == import entity (common: both are the Zonneplan
    #   tariff).  Reuse window_price directly so the planner sees the real
    #   per-hour curve, including the evening peak.
    #
    # Case 2 — entities differ.  Ratio-scale the import curve:
    #   window_export_price[h] = window_price[h] × (export_price / cur_import)
    #   where cur_import = window_price[0] (the current-hour import price).
    #   Rationale: the export tariff typically tracks the import tariff at a
    #   fixed fraction (e.g. Zonneplan saldering at ~90%); ratio-scaling
    #   preserves the shape of the import curve while re-anchoring to the
    #   live export price.  Guard: if cur_import ≈ 0, fall back to flat
    #   broadcast of export_price (avoids divide-by-zero).
    #
    # When export_price is None (no export entity configured), both arrays
    # are None/zero — export credit is a strict no-op (T0.1b parity).
    if export_price is None:
        window_export_price: list[float] = [0.0] * window_len
        feed_in: list[float] | None = None
    elif cfg.price_mode == const.PRICE_MODE_STATIC:
        # Static tariff mode: export_price IS the configured constant
        # (static_price_export), not a live sensor tracking the import curve.
        # Always flat-broadcast it — never ratio-scale by the import curve's
        # shape. Ratio-scaling here would make a fixed export credit swing
        # with an HP/HC import schedule (e.g. 0.30 peak / 0.10 offpeak),
        # mirroring the import curve instead of staying the configured flat
        # constant, which the static-mode spec explicitly forbids.
        eff = optimize_mod.effective_export_price(export_price, cfg)
        window_export_price = [eff] * window_len
        feed_in = [eff] * window_len
    elif export_price_matches_import:
        # Same entity → per-hour export prices == per-hour import prices, less the fee.
        window_export_price = [optimize_mod.effective_export_price(p, cfg) for p in window_price]
        feed_in = list(window_export_price)
    else:
        cur_import = window_price[0] if window_price else 0.0
        if cur_import > 1e-9:
            ratio = export_price / cur_import
            window_export_price = [
                optimize_mod.effective_export_price(p * ratio, cfg) for p in window_price
            ]
            feed_in = list(window_export_price)
        else:
            eff = optimize_mod.effective_export_price(export_price, cfg)
            window_export_price = [eff] * window_len
            feed_in = [eff] * window_len

    # Pad a short reserve list up to window_len with the firmware floor value rather
    # than dropping it to None.  A short list arises when compute_decision's
    # _win_len FLOORs the deadline but _dp_select_slots CEILs it (the legacy
    # path with a non-hour-aligned deadline, e.g. sunset-buffer = 14:30).
    # Dropping to None would silently revert to the firmware floor for the
    # extra hour — the per-hour reserve floor must always be enforced.
    _floor_kwh = cfg.soc_floor / 100.0 * cfg.capacity_kwh
    if reserve_by_hour is not None:
        padded_reserve = (
            reserve_by_hour + [_floor_kwh] * max(0, window_len - len(reserve_by_hour))
        )[:window_len]
    else:
        padded_reserve = None

    # Current-cycle solar-reservation ceiling — ALWAYS ON (no config gate).
    # Caps grid charge so the current cycle's remaining forecast solar always
    # has room — prevents filling the pack ahead of free afternoon solar.
    # Uses the MEDIAN-load surplus (window_load_reserve, P50) so it reserves
    # room for the solar that REALISTICALLY arrives — not a conservative load
    # estimate (which would under-reserve and waste afternoon solar).
    cycle_end_idx = optimize_mod.solar_cycle_end_idx(
        now_h, window_len, sun_times, slot_minutes=slot_minutes
    )
    grid_charge_ceiling = optimize_mod.solar_reservation_ceiling(
        window_pv, window_load_reserve, cfg, cycle_end_idx=cycle_end_idx, dt_h=dt_h
    )

    # Single co-optimized DP: grid-charge + discharge-to-grid in one pass.
    # export_price (per-hour effective feed-in) drives the discharge action;
    # feed_in drives the solar-spill credit. The DP's hard CHARGE floor is the
    # firmware soc_floor (scalar); the per-hour ride-out reserve IS a DP
    # constraint — it is the export discharge floor enforced at optimize.py:523-526
    # (reserve_by_hour, passed below). The executor applies the same reserve again
    # as a live clamp (controller.py:1204-1208).
    result = optimize_mod.optimize_grid(
        window_pv,
        # DP economics run on P50 (expected) load.  Survival is the firmware's job
        # (5% hard floor); the P50 export reserve + live executor clamp guard it
        # independently of this load array.
        window_load_reserve,
        window_price,
        soc_start=inputs.soc,
        cfg=cfg,
        window_start_h=window_start_slot,
        window_len=window_len,
        slots_per_day=slots_per_day,
        day_index=_day_index,
        chargeable=chargeable,
        feed_in=feed_in,
        export_price=window_export_price if export_price is not None else None,
        terminal_mode=terminal_mode,
        water_value=water_value,
        reserve_by_hour=padded_reserve,            # UNCHANGED: export discharge floor
        grid_charge_ceiling=grid_charge_ceiling,
        hedge_drain_kwh=hedge_drain_kwh,
        dt_h=dt_h,
        eta_curve=eta_curve,
    )
    schedule: list[float] = result["schedule"]
    export_kwh_by_hour: list[float] = result.get("export_schedule", [0.0] * window_len)

    # §Mechanism: filter sub-threshold export runs from the live decision only.
    # The DP result is left intact (parity invariant); only export_request and
    # export_revenue_eur are sourced from the filtered schedule.
    # exempt_index=0 protects the in-progress clock hour (C1 receding-horizon fix —
    # a run that reaches index 0 was admitted on an earlier tick at its full total
    # and must not be dropped mid-export by horizon shrinkage).
    filtered_ac, filtered_rev = apply_min_export_block(
        export_kwh_by_hour,
        window_export_price if export_price is not None else None,
        cfg,
        0,  # exempt_index: current clock hour is always horizon index 0
        dt_h=dt_h,
    )

    # Validate schedule: reject non-finite values (NaN or ±inf from pathological
    # DP output).  Either would silently corrupt the selected-slot list; raising
    # here triggers the caller's try/except and falls back to PASSIVE.
    if any(not math.isfinite(v) for v in schedule):
        raise ValueError("optimize_grid returned a schedule containing non-finite values")

    # Economic-only: no survival shield.  The DP's own price-gate mask and
    # internal floor constraint govern the schedule; infeasibility is the DP's
    # own verdict (reserve/floor unreachable under the price mask), not a
    # survival-floor residual.  Survival is the firmware's job (5% hard floor).
    infeasible = bool(result.get("infeasible", False))

    # Convert per-hour schedule to selected slot datetimes.
    # Any hour with AC charge > epsilon is treated as a selected charging slot,
    # fed into decide_state identically to the heuristic-chosen slots.
    selected: list[datetime] = [
        now_h + h * stride
        for h, kwh in enumerate(schedule)
        if kwh > _DP_EPSILON_SCHEDULE_KWH
    ]
    grid_request: dict[datetime, float] = {
        now_h + h * stride: schedule[h] * 1000.0 / dt_h
        for h in range(len(schedule))
        if schedule[h] > _DP_EPSILON_SCHEDULE_KWH
    }

    # Build export_request dict from the filtered schedule — mirrors grid_request
    # semantics.  Sub-threshold runs are absent (zeroed by apply_min_export_block);
    # remaining entries are the planned export rate in W (kWh over the slot × 1000 / dt_h → average W).
    _DP_EPSILON_EXPORT_KWH = _DP_EPSILON_SCHEDULE_KWH
    export_request: dict[datetime, float] = {
        now_h + h * stride: filtered_ac[h] * 1000.0 / dt_h
        for h in range(window_len)
        if filtered_ac[h] > _DP_EPSILON_EXPORT_KWH
    }

    # Revenue is always recomputed by the filter (not taken from result["export_revenue_eur"]).
    # When export_min_block_kwh=0.0 the filter is a no-op and the revenue equals
    # result["export_revenue_eur"] within float precision; export_request is byte-identical.
    export_revenue_eur = filtered_rev

    # Net-out pass: a single co-optimised DP can simultaneously emit a grid-charge
    # and an export-to-grid action for the same clock hour (observed live: 19:00 UTC
    # had charge 108.7 W + export 3279.9 W).  Downstream, decide_state treats any
    # hour in `selected` as a FORCING slot → would grid-charge at max_charge_w at
    # the peak price, suppressing the intended export; build_plan_horizon renders
    # both actions, producing a contradictory plan card.
    #
    # We net the two actions to the dominant one so no hour is ever BOTH a charge
    # and an export action.  For each overlapping hour h (charge C W, export E W):
    #   net = E − C
    #   net > ε  → export dominates: export_request[h] = net, remove from grid.
    #   −net > ε → charge dominates: grid_request[h] = −net, keep in selected.
    #   |net| ≤ ε → both cancel → idle hour.
    # Revenue telemetry is adjusted for the removed export AC energy.
    _net_eps_w = _DP_EPSILON_SCHEDULE_KWH * 1000.0 / dt_h  # ε in W over the slot
    _overlap = set(grid_request) & set(export_request)
    if _overlap:
        # Curve-derived at the export cap (a representative scalar reused across
        # every overlapping hour below, mirroring the pre-curve single-scalar
        # shape); static eta_discharge(cfg) when eta_curve is None (parity).
        _eta_d = (
            optimize_mod.eta_discharge(cfg) if eta_curve is None
            else eta_curve.eta_discharge(cfg.max_export_w)
        )
        _cc = cfg.cycle_cost_eur_per_kwh
        for _h in _overlap:
            _C = grid_request.pop(_h)
            _E = export_request.pop(_h)
            if _h in selected:
                selected.remove(_h)
            _net = _E - _C
            # Per-hour effective export price for revenue adjustment
            _idx = round((_h - now_h).total_seconds() / slot_seconds)
            _ep_h = (
                window_export_price[_idx]
                if 0 <= _idx < len(window_export_price)
                else 0.0
            )
            if _net > _net_eps_w:
                # Export dominates: keep net export, remove grid-charge entirely
                export_request[_h] = _net
                _removed_kwh = (_E - _net) / 1000.0 * dt_h   # == C / 1000 * dt_h
            elif -_net > _net_eps_w:
                # Charge dominates: keep net charge, remove export entirely
                grid_request[_h] = -_net
                selected.append(_h)
                _removed_kwh = _E / 1000.0 * dt_h
            else:
                # Within ε: both cancel → idle hour
                _removed_kwh = _E / 1000.0 * dt_h
            export_revenue_eur -= _removed_kwh * (_ep_h - _cc / _eta_d)
        selected = sorted(selected)

    # Per-hour solar-reservation ceiling as a SoC% map, so the executor charge-
    # stop (scheduler.decide_state) and the display (build_plan_horizon) can both
    # ENFORCE it — not just the DP schedule.  Grid charging must stop here, never
    # at soc_target, leaving room for the forecast solar surplus.
    cap = cfg.capacity_kwh if cfg.capacity_kwh > 1e-9 else 1.0
    ceiling_by_hour: dict[datetime, float] = {
        now_h + h * stride: grid_charge_ceiling[h] / cap * 100.0
        for h in range(window_len)
    }
    return selected, grid_request, infeasible, export_request, export_revenue_eur, ceiling_by_hour


def _build_is_cheap_by_hour(
    slots: list[PriceSlot],
    cfg,
    slot_minutes: int = 60,
) -> dict[datetime, bool]:
    """Per-hour cheap-relief map for the ride-to-trough reserve (rev-2).

    ``is_cheap[k] = price[k] <= trough_ref[k] + reserve_cheap_band * max(trough_ref[k], eps)``
    where ``trough_ref[k] = min(prices[k : k+RESERVE_WINDOW_MAX_H])`` — a 24h-capped
    forward min over ``[k, k+RESERVE_WINDOW_MAX_H)`` (inclusive of ``k``, so the trough
    hour itself is cheap), matching the reserve walk's ``h+24h`` backstop; NOT an
    unbounded suffix — a deep >24h-out or negative-price trough must not suppress
    tomorrow's genuine morning relief (still within-24h, so the overnight is judged
    against tomorrow's real morning trough; no per-day reset).  Only hours with a real
    slot price get an entry; hours absent are treated NOT cheap by the walk
    (synthetic-night rows have no price → solar-trough + backstop).  Independent of any
    reserve hour ``h`` ⇒ ``reserve[h]`` monotone-non-increasing.
    """
    if not slots:
        return {}
    price_by_h: dict[datetime, float] = {}
    for s in slots:
        h = resolution.floor_to_slot(s.start, slot_minutes)
        price_by_h.setdefault(h, s.price)
    hours = sorted(price_by_h)
    prices = [price_by_h[h] for h in hours]
    # 24h-capped forward min (inclusive of k) — matches the reserve walk backstop,
    # NOT an unbounded suffix (a deep >24h-out / negative-price trough must not
    # suppress tomorrow's genuine morning relief).  No regret dependency.
    trough_ref = [
        min(prices[k:min(k + const.RESERVE_WINDOW_MAX_H * (60 // slot_minutes), len(prices))])
        for k in range(len(prices))
    ]
    band = cfg.reserve_cheap_band
    eps = const.RESERVE_CHEAP_BAND_EPS
    return {
        h: prices[k] <= trough_ref[k] + band * max(trough_ref[k], eps)
        for k, h in enumerate(hours)
    }


def _next_synthetic_pickup(after: datetime) -> datetime:
    """Next occurrence of ``const.FALLBACK_SOLAR_PICKUP_HOUR_UTC`` strictly after ``after``.

    Zeroes minute/second/microsecond. Rolls to the next day when the pickup
    hour on ``after``'s own day is not strictly later than ``after``.
    """
    pickup = after.replace(
        hour=const.FALLBACK_SOLAR_PICKUP_HOUR_UTC,
        minute=0, second=0, microsecond=0,
    )
    if pickup <= after:
        pickup += timedelta(days=1)
    return pickup


def _synthetic_night_rows(
    start: datetime,
    end: datetime,
    load_w_by_hod: dict[int, float] | None,
    fallback_w: float,
) -> list[ForecastInterval]:
    """Hourly zero-PV rows from ``start`` (inclusive) to ``end`` (exclusive).

    Each row's load is looked up by hour-of-day in ``load_w_by_hod`` (falling
    back to ``fallback_w`` when the dict is ``None`` or missing that hour) —
    this is how synthetic overnight ride-out rows reuse the same predicted
    load the real forecast curve uses.
    """
    rows: list[ForecastInterval] = []
    t = start
    while t < end:
        load_w = (
            load_w_by_hod.get(t.hour, fallback_w) if load_w_by_hod is not None else fallback_w
        )
        rows.append(ForecastInterval(t, 0.0, load_w, 1.0))
        t += timedelta(hours=1)
    return rows


def _build_reserve_by_hour(
    now: datetime,
    slots: list[PriceSlot],
    intervals_reserve: list,
    cfg,
    *,
    is_cheap: dict[datetime, bool] | None = None,
    slot_minutes: int = 60,
    eta_curve: EfficiencyCurve | None = None,
) -> dict[datetime, float]:
    """Build a per-hour ride-out reserve dict (DC kWh) for the horizon.

    For each distinct hour ``h`` that appears in ``slots`` (starting from
    ``_hour(now)``), the reserve is the kWh needed to ride out from ``h`` until
    the first cheap/solar opportunity that starts STRICTLY AFTER ``h``.

    The returned dict is keyed by hour-start ``datetime`` and valued in DC kWh
    (same unit as ``energy.ride_out_reserve_kwh`` — ready to pass directly as
    ``reserve_by_hour`` to ``build_plan_horizon`` / ``build_display_horizon``).

    Algorithm
    ---------
    1. Compute the single global ``next_charge_opportunity`` from ``now`` —
       this is the "first refill" after the current moment.
    2. For each horizon hour ``h``:
       a. The relevant ``next_opp`` for that hour is the first slot opportunity
          whose start is STRICTLY after ``h`` — re-derived by scanning slots
          from ``h + 1h`` onward with the existing helper logic.
       b. The relevant intervals are the suffix of ``intervals_reserve`` whose
          start >= ``h``.
       c. Call ``energy.ride_out_reserve_kwh(now=h, intervals=suffix, cfg=cfg)``.
          ``reserve_by_hour`` represents only the ride-out FLOOR (debit-only drawdown
          to the SoC trough), used purely for the ``reserve_soc`` display line — not
          for SoC simulation.
    3. When step 2.a finds no PV pickup past ``h`` (e.g. day-2 evening beyond the
       last forecast sunset, or a fully-cloudy tail), synthesize a one-night
       ride-out: append zero-PV / hour-of-day-predicted-load rows to the suffix up
       to the next ``FALLBACK_SOLAR_PICKUP_HOUR_UTC`` and anchor ``ride_out_reserve_kwh`` to
       that synthetic sunrise.  This keeps day-2 evening reserves at a real overnight
       ride-out (bounded to one night, capped at pack capacity) instead of the floor.
    4. When the suffix IS empty (horizon hours PAST the last forecast interval),
       do NOT break (which left tail hours with no reserve → firmware-floor default
       → free-drain via export).  Instead, when ``_has_solar`` is True, synthesize
       a fresh one-night ride-out from scratch (``h`` → next
       ``FALLBACK_SOLAR_PICKUP_HOUR_UTC``) so the tail never collapses.
       When ``_has_solar`` is False, ``continue`` — firmware floor is correct.

    Returns an empty dict when no intervals are available.
    """
    if not intervals_reserve or not slots:
        return {}

    now_h = resolution.hour_floor(now)
    # Build interval lookup: hour → ForecastInterval (first one wins)
    iv_by_hour: dict[datetime, object] = {}
    for iv in intervals_reserve:
        h = resolution.hour_floor(iv.start)
        if h not in iv_by_hour:
            iv_by_hour[h] = iv

    # Collect distinct slot hours >= now_h
    seen: set[datetime] = set()
    horizon_hours: list[datetime] = []
    for slot in sorted(slots, key=lambda s: s.start):
        h = resolution.hour_floor(slot.start)
        if h >= now_h and h not in seen:
            seen.add(h)
            horizon_hours.append(h)

    # Hour-of-day predicted-load lookup (W) drawn from the reserve intervals
    # themselves, so synthetic night-2 rows reuse the SAME predicted load the curve
    # uses (and transitively inherit any load-model improvement).  Last value per
    # hour-of-day wins (closest to the synthetic night).
    load_by_hod: dict[int, float] = {}
    for iv in intervals_reserve:
        load_by_hod[iv.start.hour] = iv.load_w

    # Guard: only synthesize a night-2 overnight extension when the system has
    # solar AND we are past the last forecast PV surplus (day-2 evening / cloudy
    # tail).  When the full forecast is PV-free (no solar system, unit test with
    # pv_remaining=0, or all-cloudy with no sunrise in the horizon) we keep the
    # original next_opp=None behaviour so ride_out_reserve_kwh sums only the remaining
    # real intervals rather than piling on a synthetic night that over-reserves
    # and suppresses export.
    _has_solar = scheduler.find_next_solar_pickup(now_h, intervals_reserve) is not None

    reserve_by_hour: dict[datetime, float] = {}
    for h in horizon_hours:
        # Suffix of (two-day) reserve intervals starting at or after this hour.
        suffix = [iv for iv in intervals_reserve if iv.start >= h]
        if not suffix:
            # Past the last forecast interval. No solar -> firmware floor is correct
            # (battery will be refilled by grid before next use).  WITH solar,
            # synthesize a one-night ride-out (h -> next FALLBACK_SOLAR_PICKUP_HOUR_UTC)
            # so the tail does NOT collapse to the floor and free-drain via export.
            if not _has_solar:
                continue
            synthetic_pickup = _next_synthetic_pickup(h)
            syn_suffix = _synthetic_night_rows(
                h, synthetic_pickup, load_by_hod, const.DEFAULT_FALLBACK_LOAD_W
            )
            reserve_by_hour[h] = energy.ride_out_reserve_kwh(
                h, syn_suffix, cfg, is_cheap=is_cheap, slot_minutes=slot_minutes,
                eta_curve=eta_curve,
            )
            continue
        # Ride-out endpoint = the next genuine PV pickup STRICTLY after this hour
        # (price-independent; a cheap night slot is NOT a charge opportunity).
        next_opp = scheduler.find_next_solar_pickup(h + timedelta(hours=1), suffix)
        if next_opp is None and _has_solar:
            # No forecast PV pickup past this hour (day-2 evening, beyond the last
            # forecast sunset, or a fully-cloudy tail), but the system HAS solar.
            # Synthesize a one-night ride-out: extend the suffix with zero-PV /
            # predicted-load rows up to the next FALLBACK_SOLAR_PICKUP_HOUR_UTC
            # strictly after h, then anchor ride_out_reserve_kwh to that synthetic sunrise.
            # Anchoring to h (not suffix_end) prevents double-extension when the
            # degraded-path N2 loop has already appended synthetic rows up to the
            # same 08:00 UTC morning — the while-loop below simply won't fire.
            # Bounds the ride-out to ONE night (reserve_kwh also caps at pack
            # capacity) so day-2 export is reserve-bounded, not suppressed.
            synthetic_pickup = _next_synthetic_pickup(h)
            # Find where the real (+ already-synthetic) intervals end.
            last_iv = suffix[-1]
            suffix_end = last_iv.start + timedelta(hours=last_iv.dt_h)
            syn_start = resolution.hour_floor(suffix_end)
            if suffix_end > syn_start:
                syn_start += timedelta(hours=1)
            suffix = list(suffix) + _synthetic_night_rows(
                syn_start, synthetic_pickup, load_by_hod, const.DEFAULT_FALLBACK_LOAD_W
            )
            next_opp = synthetic_pickup
        reserve_by_hour[h] = energy.ride_out_reserve_kwh(
            h, suffix, cfg, is_cheap=is_cheap, slot_minutes=slot_minutes,
            eta_curve=eta_curve,
        )

    return reserve_by_hour


def _apply_price_prior(
    reserve_by_hour: dict[datetime, float],
    estimated_tomorrow: list[float] | None,
    slots: list[PriceSlot],
    now_h: datetime,
    real_horizon_end: datetime,
    intervals_reserve: list,
    cfg,
) -> None:
    """Plan B (B3+B4): upside-only reserve-raise from the persistence price prior.

    Builds a SEPARATE estimated-slot list bounded to [real_horizon_end, tomorrow
    pickup) and, when the estimate beats tonight's cheapest export hours, RAISES
    `reserve_by_hour` in place for the pre-pickup hours.  Never lowers a reserve;
    never touches `slots`, `intervals_reserve`, or the DP price arrays.  A no-op when
    there is no estimate / no pickup / real prices already cover tonight.
    """
    if estimated_tomorrow is None:
        return
    pickup = scheduler.find_next_solar_pickup(real_horizon_end, intervals_reserve)
    est_slots = pricing_store.build_estimated_slots(
        estimated_tomorrow, real_horizon_end, pickup
    )
    if not est_slots or pickup is None:   # narrows pickup to datetime below
        return
    # Resample-before-consume: `slots` may be a mixed 60+15-min payload during
    # rollout (near-term fine, far-term coarse — detect_slot_minutes is MIN-based
    # so it correctly picks the finest grid present).  compute_anticipation_held_extra
    # applies a SINGLE dt_h_real scalar to every real_slots entry it walks, so a
    # scalar derived from the finest grid is only valid once every entry genuinely
    # spans that grid.  Actually upsample the real slots onto `_sm` (forward-filling
    # coarse entries, mirroring the DP-side resample_price_map usage) before passing
    # them on — otherwise still-60-min slots get counted at a quarter of their true
    # DC.  `horizon_end=real_horizon_end` also expands the chronologically LAST
    # coarse hour to its true span (real_horizon_end - last.start) instead of the
    # resampler's no-successor default of one fine sub-slot, so the boundary hour
    # of the horizon (always the last slot in `slots`, per `real_horizon_end`'s own
    # `max(s.start)` derivation) counts its full DC too.  Identity at uniform
    # resolution (all-60 or all-15) -> byte-identical.
    _sm = resolution.detect_slot_minutes(slots)
    _resampled = resolution.resample_price_map(slots, _sm, horizon_end=real_horizon_end)
    _real_slots = [PriceSlot(start, price) for start, price in sorted(_resampled.items())]
    held = pricing_store.compute_anticipation_held_extra(
        estimated_slots=est_slots,
        real_slots=_real_slots,
        now_h=now_h,
        real_horizon_end=real_horizon_end,
        tomorrow_solar_pickup=pickup,
        base_reserve_by_hour=reserve_by_hour,
        cfg=cfg,
        slot_minutes=_sm,
    )
    if held <= 0.0:
        return
    for h in list(reserve_by_hour):
        if h < pickup:
            reserve_by_hour[h] += held


def compute_decision(
    plan: PlanState,
    inputs: PlantInputs,
    slots: list[PriceSlot],
    pv_remaining: float,
    sunset: datetime,
    predictor,
    cur_temp: float | None,
    cfg: Config,
    tomorrow_total: float | None = None,
    sun_times: tuple[datetime, datetime, datetime] | None = None,
    today_arrays: list[tuple[float, datetime | None]] | None = None,
    tomorrow_arrays: list[tuple[float, datetime | None]] | None = None,
    today_watts: list[list[tuple[datetime, float]]] | None = None,
    tomorrow_watts: list[list[tuple[datetime, float]]] | None = None,
    export_price: float | None = None,
    _out: dict | None = None,
    _shadow_dp: bool = False,
    export_price_matches_import: bool = False,
    estimated_tomorrow: list[float] | None = None,
    temp_by_hour: dict[datetime, float | None] | None = None,
    past_actuals_by_hour: dict | None = None,
    hedge_drain_by_hour: dict[datetime, float] | None = None,
    slot_minutes: int | None = None,
    eta_curve: EfficiencyCurve | None = None,
) -> tuple[PlanState, float, datetime, list, str, list]:
    """Pure wiring of energy + scheduler + guard. Returns (plan, setpoint, deadline, horizon, horizon_mode, intervals_reserve).

    The 6th element ``intervals_reserve`` is the two-day P50 ForecastInterval list
    anchored to tomorrow's PV ramp — used for reserve sizing in the C3 export executor.
    Callers that don't need it can discard it with ``_``.

    ``_out`` is an optional side-channel dict.  When provided, ``compute_decision``
    populates it with DP-run artefacts needed for fictive-plan publication:
        _out["dp_selected"]  — list[datetime] of DP-chosen charge hours (may be []).
        _out["intervals"]    — P50 ForecastInterval list used for horizon building.
    Keys are written only when the DP actually runs and succeeds; the dict is
    left untouched when the DP raises.

    ``_shadow_dp`` enables shadow mode: the DP runs but the returned plan and
    setpoint are discarded by the caller (master-switch-OFF path).  DP artefacts
    are written to ``_out`` so callers can publish a fictive horizon and log
    regret.  Ignored when ``_out`` is None.
    """
    deadline = scheduler.compute_deadline(inputs.now, sunset, slots, cfg)
    now_h = resolution.hour_floor(inputs.now)
    if slot_minutes is None:
        slot_minutes = resolution.resolve_slot_minutes(slots, cfg.slot_resolution)
    dt_h = slot_minutes / 60.0
    if _out is not None:
        _out["slot_minutes"] = slot_minutes
    fallback_load = const.DEFAULT_FALLBACK_LOAD_W

    _, trough_price = scheduler.find_next_trough(inputs.now, slots, cfg)
    # KEEP find_next_trough alive for the terminal water-value reference only.
    # The optimization horizon is the FULL forecast, not [now, trough]: tomorrow's
    # evening peak (best export hour) sits past the trough and must be in-window.
    _last_slot = max((s.start for s in slots), default=now_h)
    horizon_edge = resolution.hour_floor(_last_slot) + timedelta(hours=1)
    terminal_mode = "water_value"
    water_value: float | None = optimize_mod.compute_water_value(trough_price, cfg)

    # --- Water-value curve/interval build over [now, horizon_edge] ---
    # build_display_intervals emits one interval per price-slot hour >= now_h
    # (bounded by horizon_edge), so overnight hours get pv=0 + predicted
    # load — full hourly coverage across the trough window.
    if today_watts is not None or tomorrow_watts is not None:
        # Preferred path: use the real per-15-min watts data from the Open-Meteo sensor.
        # This avoids the monotonic-rise bug in the quarter-sine synthesis.
        _wv_curve = build_pv_curve_from_watts(today_watts, tomorrow_watts, inputs.now, step_h=dt_h)
    elif sun_times is not None:
        _wv_curve = build_two_day_pv_curve(
            today_arrays, tomorrow_arrays, inputs.now, *sun_times
        )
    elif today_arrays:
        _wv_curve = build_pv_curve_from_arrays(today_arrays, inputs.now, horizon_edge)
    else:
        _wv_curve = synth_pv_curve(pv_remaining, inputs.now, horizon_edge)
    # Full horizon: every future slot is in-window (no trough truncation).
    _wv_slots = [s for s in slots if now_h <= s.start < horizon_edge]
    intervals = plan_mod.build_display_intervals(
        _wv_slots, inputs.now, _wv_curve, predictor, cur_temp, fallback_load,
        quantile=0.5, temp_by_hour=temp_by_hour, slot_minutes=slot_minutes,
    )
    # Two-day reserve intervals: extend P50 (expected) load coverage past the price
    # horizon to tomorrow's PV ramp, so the ride-out reserve reaches the next solar
    # pickup even when only tonight's prices are published.  P50 is correct for the
    # export floor: if a higher-than-expected night undershoots, cheap morning import
    # recovers the gap — net-profitable vs. holding back peak-priced export capacity.
    # Falls back to in-horizon P50 intervals when no two-day PV curve is available.
    if sun_times is not None and _wv_curve:
        _rsv_temp = {
            start: (
                temp_by_hour.get(resolution.hour_floor(start), cur_temp)
                if temp_by_hour else cur_temp
            )
            for start, _ in _wv_curve
        }
        intervals_reserve = build_intervals(
            _wv_curve, predictor, fallback_load, cfg, _rsv_temp,
            quantile=0.5,
        )
    else:
        # Degraded-data fallback: sun entity unavailable or two-day PV curve empty.
        # intervals may only span tonight's price horizon (e.g. ending at 00:00),
        # leaving the 23:00→morning overnight load unaccounted for.
        # Extend with synthetic fallback-load intervals from tonight's horizon edge
        # to the next occurrence of FALLBACK_SOLAR_PICKUP_HOUR_UTC (08:00 UTC),
        # so the ride-out reserve computed by _build_reserve_by_hour always covers
        # the full overnight load.  The firmware's 5% hard floor backstops this.
        intervals_reserve = list(intervals)
        _synthetic_pickup = _next_synthetic_pickup(now_h)
        # End of the last real interval (= start of the synthetic gap).
        _iv_horizon = (
            intervals[-1].start
            + timedelta(hours=intervals[-1].dt_h)
            if intervals
            else now_h
        )
        # Only extend when:
        #   (a) there are real P50 intervals (non-empty price window), AND
        #   (b) the real price horizon has NOT yet crossed midnight.
        # Guard (a) prevents extension when intervals is empty (e.g. all prices are
        # in the past, so _iv_horizon falls back to now_h which is always <
        # midnight_next and would otherwise trigger false extension).
        # Guard (b) prevents over-inflation when prices already run past midnight
        # (e.g. 14:00→02:00 next day); the `<=` covers the common night-edge where
        # the horizon ends exactly at midnight (e.g. 22:00→00:00 with only
        # tonight's prices published).
        _midnight_next = (
            now_h.replace(hour=0, minute=0, second=0, microsecond=0)
            + timedelta(days=1)
        )
        if intervals and _iv_horizon <= _midnight_next:
            _syn_start = resolution.hour_floor(_iv_horizon)
            # If the real interval ends mid-hour, advance to the next full hour.
            if _iv_horizon > _syn_start:
                _syn_start += timedelta(hours=1)
            intervals_reserve.extend(
                _synthetic_night_rows(_syn_start, _synthetic_pickup, None, fallback_load)
            )

    horizon_mode = "water-value"

    # PRICE GATE — only grid-charge when it beats the peak after round-trip losses.
    # _ceiling is consumed by _dp_select_slots; not used on the fallback path.
    _wv_prices = [s.price for s in slots if now_h <= s.start < horizon_edge]
    _peak = max(_wv_prices) if _wv_prices else None
    _ceiling = scheduler.charge_price_ceiling(_peak, cfg)
    # On DP exception the fallback is PASSIVE: no heuristic charge-slot selection.
    selected: list[datetime] = []

    # --- DP FORK SEAM ---
    # DP always runs and replaces the empty ``selected`` list.
    # On the shadow/disabled path (_shadow_dp=True) the returned plan and
    # setpoint are discarded (setpoint forced to 0); only ``_out`` artefacts
    # are used for fictive-plan publication and shadow regret logging.
    #
    # Fallback contract: on ANY exception ``selected`` stays [] (PASSIVE) and
    # a WARNING is logged.  The control path NEVER raises out of this block —
    # actuation must proceed.
    #
    # Side-channel: when ``_out`` is provided, a successful DP run writes:
    #   _out["dp_selected"]  — DP-chosen charge-hour datetimes (may be []).
    #   _out["intervals"]    — P50 intervals (for fictive-plan horizon building).
    # The dict is left untouched on any DP exception so callers can use
    # ``"dp_selected" in _out`` as a clean "DP ran successfully" test.
    #
    # Build per-hour reserve dict (DC kWh) anchored to the next solar pickup, over the
    # two-day reserve intervals.  Feeds BOTH the display horizon (reserve_soc line) and
    # the DP export floor (window-aligned list below).
    # rev-2 ride-to-trough: precompute the cheap-relief map ONCE, thread to the walk.
    _is_cheap = (
        _build_is_cheap_by_hour(slots, cfg, slot_minutes)
        if cfg.reserve_anchor == const.RESERVE_ANCHOR_TROUGH else None
    )
    _reserve_by_hour = _build_reserve_by_hour(
        inputs.now, slots, intervals_reserve, cfg, is_cheap=_is_cheap, slot_minutes=slot_minutes,
        eta_curve=eta_curve,
    )
    # Plan B: upside-only reserve-raise from the persistence price prior.  Mutates
    # only _reserve_by_hour (the DP export floor + the reserve_soc display line);
    # estimated prices live in a separate list and never reach `slots`/window_price/
    # _peak.  estimated_tomorrow is non-None ONLY on the live tick (shadow/recompute
    # pass None → byte-identical, parity preserved).  horizon_edge is the real-price
    # horizon end (water-value path); legacy path is left untouched.
    # Price-prior "Plan B" additive raise is redundant under the trough anchor
    # (the walk already holds through expensive stretches until real relief) and is
    # what completed the 100% pin — GATE it off; legacy anchor keeps it for rollback.
    if cfg.reserve_anchor != const.RESERVE_ANCHOR_TROUGH:
        _apply_price_prior(
            _reserve_by_hour, estimated_tomorrow, slots, now_h, horizon_edge,
            intervals_reserve, cfg,
        )
    # Window-aligned per-SLOT reserve floor (DC kWh) for the DP export bound.
    #
    # BC1 fix: _reserve_by_hour is HOUR-keyed (built by _build_reserve_by_hour
    # with hour-floored keys) but _dp_select_slots consumes this list
    # POSITIONALLY, one entry per DP SLOT. At slot_minutes=60 hours == slots so
    # the old `now_h + timedelta(hours=i)` stride happened to line up 1:1. At
    # 15-min it does not: the list only had ~window_len/4 entries at hourly
    # stride, so slots past hour ~12 collapsed to the bare firmware floor.
    #
    # Fix: walk the DP's OWN slot grid (via _dp_window — the same helper
    # _dp_select_slots uses for its window_len) and, for each slot, hour-floor
    # its own timestamp to key into the hour-keyed dict. This also avoids
    # anchoring to this function's hour-floored `now_h` local (T4/T9 already
    # established that anchor silently misaligns sub-hour windows whenever
    # `inputs.now` sits outside the hour's first slot); `_dp_window` re-derives
    # the slot-floored anchor from `inputs.now` directly, matching
    # `_dp_select_slots` exactly so no real slot ever falls back to the floor.
    _floor_kwh = cfg.soc_floor / 100.0 * cfg.capacity_kwh
    _slot_now_h, _, _win_len = _dp_window(inputs.now, horizon_edge, slot_minutes)
    _reserve_stride = timedelta(minutes=slot_minutes)
    _reserve_list = [
        _reserve_by_hour.get(
            resolution.hour_floor(_slot_now_h + i * _reserve_stride),
            _floor_kwh,
        )
        for i in range(_win_len)
    ]
    live_grid_request: dict[datetime, float] | None = None
    live_export_request: dict[datetime, float] | None = None
    live_ceiling_by_hour: dict[datetime, float] | None = None
    try:
        (_dp_selected, _dp_request, _dp_infeasible, _dp_export_request,
         _dp_export_rev, _dp_ceiling) = _dp_select_slots(
            inputs=inputs,
            slots=slots,
            deadline=horizon_edge,        # window spans [now, trough] in new mode
            ceiling=_ceiling,
            cfg=cfg,
            export_price=export_price,
            terminal_mode=terminal_mode,
            water_value=water_value,
            export_price_matches_import=export_price_matches_import,
            reserve_by_hour=_reserve_list,
            sun_times=sun_times,
            intervals=intervals,
            hedge_drain_by_hour=hedge_drain_by_hour,
            slot_minutes=slot_minutes,
            dt_h=dt_h,
            eta_curve=eta_curve,
        )
        # Live path: DP replaces heuristic selected slots.
        selected = _dp_selected
        live_grid_request = _dp_request
        live_export_request = _dp_export_request
        live_ceiling_by_hour = _dp_ceiling
        # Expose DP artefacts for fictive-plan publication (T0.6a).
        if _out is not None:
            _out["dp_selected"] = _dp_selected
            _out["grid_request"] = _dp_request
            _out["export_request"] = _dp_export_request
            _out["intervals"] = intervals  # P50 intervals for horizon building
            _out["dp_infeasible"] = _dp_infeasible
            _out["export_revenue_eur"] = _dp_export_rev
    except Exception:  # noqa: BLE001 — safety: never block actuation
        _LOGGER.warning(
            "DP optimizer path failed; falling back to PASSIVE (no charge slots selected)",
            exc_info=True,
        )
        # ``selected`` stays [] → decide_state yields PASSIVE.

    # Edge hysteresis (new path, DP-ran only): hold the current-hour charge decision
    # unless the DP's current-hour charge moves more than end_soc_deadband (kWh).
    # Guard: only when the DP actually ran this tick (live_grid_request is not None).
    # When the DP did not run (exception fallback), live_grid_request stays None,
    # this block is skipped, and selected stays [] → PASSIVE.
    committed_cur_kwh = plan.committed_charge_kwh
    if live_grid_request is not None:
        cur_h = resolution.floor_to_slot(inputs.now, slot_minutes)
        # live_grid_request holds average W over the slot (T7); recover per-slot
        # AC kWh by scaling back with dt_h (identity at dt_h=1.0 / slot_minutes=60).
        # Review 1.3: a commit belongs to ONE slot. A stale carry-over from the
        # previous slot must not seed the deadband compare (it bypassed the DP
        # price mask and forced full-rate charge at peak).
        prev_cur_kwh = (
            plan.committed_charge_kwh
            if plan.committed_charge_slot == cur_h else 0.0
        )
        dp_cur_kwh = live_grid_request.get(cur_h, 0.0) / 1000.0 * dt_h
        if abs(dp_cur_kwh - prev_cur_kwh) <= cfg.end_soc_deadband:
            # Within deadband: keep the previous current-hour membership.
            committed_cur_kwh = prev_cur_kwh
            if prev_cur_kwh > _DP_EPSILON_SCHEDULE_KWH and cur_h not in selected:
                selected = sorted(set(selected) | {cur_h})
            elif prev_cur_kwh <= _DP_EPSILON_SCHEDULE_KWH and cur_h in selected:
                selected = [h for h in selected if h != cur_h]
        else:
            committed_cur_kwh = dp_cur_kwh

    # Anti-fight guard: the edge-hysteresis block above can re-inject the
    # current hour into `selected` purely from a stale `prev_cur_kwh` held
    # within `end_soc_deadband` of a fresh DP charge of 0 (e.g. an evening
    # price peak where the chargeable-price mask already zeroed this hour's
    # charge).  If the DP also committed a genuine export for this hour, that
    # re-injection bypasses the DP's own price mask and pushes decide_state
    # into FORCING — grid-charging straight through the committed export and
    # producing the live "W"-shaped SoC oscillation (charge/discharge/charge
    # stealing the export).  When the current hour is both `selected` and has
    # a live committed export, the export wins: drop it back out.
    # NOTE (T9): `selected`/`live_export_request` are keyed on the SLOT grid
    # (see `_dp_select_slots`'s own slot-floored `now_h`), not the wall-clock
    # hour.  `now_h` here is compute_decision's hour-floor (unchanged by T4,
    # which only slot-floored `_dp_select_slots`'s internal scope) — reusing it
    # would silently disable this guard at sub-hour resolution whenever `now`
    # is outside the hour's FIRST slot (the hour-floor key predates the DP's
    # window start and is simply absent from the slot-keyed dicts). Slot-floor
    # `inputs.now` directly so `cur_h` always names the actual current slot.
    cur_h = resolution.floor_to_slot(inputs.now, slot_minutes)
    _export_eps_w = _DP_EPSILON_SCHEDULE_KWH * 1000.0 / dt_h  # ε in W over the slot
    if (
        cur_h in selected
        and live_export_request is not None
        and live_export_request.get(cur_h, 0.0) > _export_eps_w
    ):
        selected = [h for h in selected if h != cur_h]
        committed_cur_kwh = 0.0

    new_plan = scheduler.decide_state(
        plan, soc=inputs.soc, now=inputs.now,
        selected_slots=selected, cfg=cfg,
        # Hard charge-stop at the solar-reservation ceiling for THIS hour: the
        # executor must stop at the ceiling (leaving room for forecast solar),
        # not at soc_target.  None on the heuristic-fallback path (no DP ceiling).
        # NOTE (T9 review): `live_ceiling_by_hour` is keyed on the SLOT grid
        # (see `_dp_select_slots`'s own slot-floored `now_h`), same as
        # `selected`/`live_grid_request`/`live_export_request` above — the
        # hour-floored `now_h` local misses it for 3/4 of ticks at sub-hour
        # resolution.  Slot-floor `inputs.now` directly, matching the
        # anti-fight guard's `cur_h` just above.
        charge_ceiling_soc=(
            live_ceiling_by_hour.get(resolution.floor_to_slot(inputs.now, slot_minutes))
            if live_ceiling_by_hour else None
        ),
        slot_minutes=slot_minutes,
    )
    new_plan.committed_charge_kwh = committed_cur_kwh
    new_plan.committed_charge_slot = (
        resolution.floor_to_slot(inputs.now, slot_minutes)
        if live_grid_request is not None else plan.committed_charge_slot
    )

    if new_plan.state is ControllerState.FORCING:
        prev = 0.0  # controller tracks prev separately when actuating
        setpoint = guard.command_setpoint(cfg.max_charge_w, prev, cfg)
    else:
        setpoint = 0.0
    if sun_times is not None:
        # Synthesize per-array display lists so build_display_horizon always receives arrays.
        disp_today = today_arrays if today_arrays else (
            [(pv_remaining, None)] if pv_remaining else None
        )
        disp_tomorrow = tomorrow_arrays if tomorrow_arrays else (
            [(tomorrow_total, None)] if tomorrow_total else None
        )
        horizon = plan_mod.build_display_horizon(
            slots, inputs.now, disp_today, disp_tomorrow, sun_times,
            predictor, cur_temp, fallback_load, inputs.soc, selected, deadline, cfg,
            grid_request_by_hour=live_grid_request,
            export_request_by_hour=live_export_request,
            reserve_by_hour=_reserve_by_hour if _reserve_by_hour else None,
            ceiling_by_hour=live_ceiling_by_hour,
            today_watts=today_watts,
            tomorrow_watts=tomorrow_watts,
            past_actuals_by_hour=past_actuals_by_hour,
            hedge_drain_by_hour=hedge_drain_by_hour,
            temp_by_hour=temp_by_hour,
            eta_curve=eta_curve,
        )
    else:
        horizon = plan_mod.build_plan_horizon(
            slots, intervals, selected, inputs.soc, deadline, cfg,
            grid_request_by_hour=live_grid_request,
            export_request_by_hour=live_export_request,
            reserve_by_hour=_reserve_by_hour if _reserve_by_hour else None,
            ceiling_by_hour=live_ceiling_by_hour,
            past_actuals_by_hour=past_actuals_by_hour,
            hedge_drain_by_hour=hedge_drain_by_hour,
            eta_curve=eta_curve,
        )
    return new_plan, setpoint, horizon_edge, horizon, horizon_mode, intervals_reserve
