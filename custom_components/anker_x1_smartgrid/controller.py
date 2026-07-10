"""Control loop: gather inputs, decide, actuate, record."""
from __future__ import annotations

import asyncio
import functools
import importlib.util
import json
import logging
import math
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from . import backtest as bt
from . import const, coordinator, energy, forecast as forecast_mod, guard, load_adapt, optimize as optimize_mod, past_actuals as past_actuals_mod, plan as plan_mod, pricing_store, regret as regret_mod, remote_forecast, resolution, scheduler, soc_drift
from .remote_forecast import RemoteForecastPredictor, build_hours_payload, fetch_forecast
from .actuator import Actuator
from .efficiency import EfficiencyCurve
from .export_filter import apply_min_export_block
from .dataquality import clean_hourly_rows, house_load_w as _house_load_w
from .forecast import build_intervals, LoadPredictor
from .hgbr import HGBRQuantileModel
from .loadmodel import BucketedLoadModel
from .models import Config, ControllerState, ExportState, ForecastInterval, PlanState, PlantInputs, PriceSlot
from .parsers import build_pv_curve_from_arrays, build_pv_curve_from_watts, build_two_day_pv_curve, synth_pv_curve
from .recorder import DataRecorder

_LOGGER = logging.getLogger(__name__)

# Sentinel: distinguishes "not passed" from "passed as None" in _record_sample.
_UNSET = object()

# Minimum AC kWh in a DP schedule slot to consider it a "selected" charging hour.
# Values below this threshold are rounding / binning artefacts from the DP
# discretisation (BIN_KWH ≈ 0.05 kWh) and are treated as zero charge.
_DP_EPSILON_SCHEDULE_KWH = 0.01

# Bounded wait for the tick lock during unload/reload so release()/recorder.close
# never interleave with an in-flight tick's engage_*; unblocks if a tick wedges.
_SHUTDOWN_LOCK_TIMEOUT_S = 15.0

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
    price forecast for the window.  When ``export_price_matches_import=True``
    (the common case: both entities point at the same Zonneplan tariff),
    ``feed_in = window_price`` exactly — the per-hour import curve is reused,
    so the planner sees the real evening peak in the export credit.  When the
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

    now_h = now.replace(minute=0, second=0, microsecond=0)
    # Build interval lookup: hour → ForecastInterval (first one wins)
    iv_by_hour: dict[datetime, object] = {}
    for iv in intervals_reserve:
        h = iv.start.replace(minute=0, second=0, microsecond=0)
        if h not in iv_by_hour:
            iv_by_hour[h] = iv

    # Collect distinct slot hours >= now_h
    seen: set[datetime] = set()
    horizon_hours: list[datetime] = []
    for slot in sorted(slots, key=lambda s: s.start):
        h = slot.start.replace(minute=0, second=0, microsecond=0)
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
            synthetic_pickup = h.replace(
                hour=const.FALLBACK_SOLAR_PICKUP_HOUR_UTC,
                minute=0, second=0, microsecond=0,
            )
            if synthetic_pickup <= h:
                synthetic_pickup += timedelta(days=1)
            syn_suffix = []
            t = h
            while t < synthetic_pickup:
                syn_suffix.append(
                    ForecastInterval(
                        t, 0.0,
                        load_by_hod.get(t.hour, const.DEFAULT_FALLBACK_LOAD_W),
                        1.0,
                    )
                )
                t += timedelta(hours=1)
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
            synthetic_pickup = h.replace(
                hour=const.FALLBACK_SOLAR_PICKUP_HOUR_UTC,
                minute=0, second=0, microsecond=0,
            )
            if synthetic_pickup <= h:
                synthetic_pickup += timedelta(days=1)
            # Find where the real (+ already-synthetic) intervals end.
            last_iv = suffix[-1]
            suffix_end = last_iv.start + timedelta(hours=last_iv.dt_h)
            syn_start = suffix_end.replace(minute=0, second=0, microsecond=0)
            if suffix_end > syn_start:
                syn_start += timedelta(hours=1)
            suffix = list(suffix)
            t = syn_start
            while t < synthetic_pickup:
                suffix.append(
                    ForecastInterval(
                        t, 0.0,
                        load_by_hod.get(t.hour, const.DEFAULT_FALLBACK_LOAD_W),
                        1.0,
                    )
                )
                t += timedelta(hours=1)
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
    now_h = inputs.now.replace(minute=0, second=0, microsecond=0)
    if slot_minutes is None:
        slot_minutes = resolution.resolve_slot_minutes(slots, cfg.slot_resolution)
    dt_h = slot_minutes / 60.0
    if _out is not None:
        _out["slot_minutes"] = slot_minutes
    fallback_load = const.DEFAULT_FALLBACK_LOAD_W

    trough_dt, trough_price = scheduler.find_next_trough(inputs.now, slots, cfg)
    # KEEP find_next_trough alive for the terminal water-value reference only.
    # The optimization horizon is the FULL forecast, not [now, trough]: tomorrow's
    # evening peak (best export hour) sits past the trough and must be in-window.
    _last_slot = max((s.start for s in slots), default=now_h)
    horizon_edge = _last_slot.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
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
                temp_by_hour.get(start.replace(minute=0, second=0, microsecond=0), cur_temp)
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
        _synthetic_pickup = now_h.replace(
            hour=const.FALLBACK_SOLAR_PICKUP_HOUR_UTC,
            minute=0, second=0, microsecond=0,
        )
        if _synthetic_pickup <= now_h:
            _synthetic_pickup += timedelta(days=1)
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
            _syn_start = _iv_horizon.replace(minute=0, second=0, microsecond=0)
            # If the real interval ends mid-hour, advance to the next full hour.
            if _iv_horizon > _syn_start:
                _syn_start += timedelta(hours=1)
            while _syn_start < _synthetic_pickup:
                intervals_reserve.append(
                    ForecastInterval(_syn_start, 0.0, fallback_load, 1.0)
                )
                _syn_start += timedelta(hours=1)

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
            (_slot_now_h + i * _reserve_stride).replace(minute=0, second=0, microsecond=0),
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


class Controller:
    def __init__(
        self,
        hass: HomeAssistant,
        data: dict,
        recorder: DataRecorder,
        actuator: Actuator,
        store,
        price_store=None,
    ) -> None:
        self._hass = hass
        self._data = data
        self._recorder = recorder
        self._actuator = actuator
        self._store = store
        self._price_store = price_store        # PriceHistoryStore | None (Plan B)
        # Re-entrancy guard (review 1.2): serializes tick() so a slow tick (e.g. an
        # ML retrain) cannot overlap the next timer fire and race the actuator.
        self._tick_lock = asyncio.Lock()
        self._price_history_day: str | None = None
        self.cfg = Config.from_dict(data)
        if self.cfg.soc_floor > const.DEFAULT_SOC_FLOOR + 1e-9:
            _LOGGER.warning(
                "soc_floor=%.1f%% > firmware floor %.0f%%: DP floor-import pricing "
                "assumes the firmware floor; the [%.0f%%, %.1f%%] band is priced as "
                "phantom grid imports (review 4.2 / L1 caveat)",
                self.cfg.soc_floor, const.DEFAULT_SOC_FLOOR,
                const.DEFAULT_SOC_FLOOR, self.cfg.soc_floor,
            )
        self.plan = PlanState.initial(dt_util.utcnow())
        self.enabled = True
        self.profile: dict = {}
        self._profile_predictor: LoadPredictor = LoadPredictor.from_profile(self.profile)
        self.last_status: dict = {}
        self._res_latch: tuple[int, "date"] | None = None
        self._detected_slot_minutes: int = 60
        # Last LATCHED slot_minutes used to build self.plan's committed state
        # (committed_slots / committed_charge_kwh).  Compared each live tick;
        # a change clears committed state so hour-keyed state from the old
        # resolution cannot mis-align with the new quarter-slot keys and
        # mis-fire the hysteresis/anti-fight guards.  Init 60 == the initial
        # resolution, so a stable-60 deployment never sees a mismatch (parity).
        self._committed_slot_minutes: int = 60
        self._last_purge_hour = -1
        self._last_rollup_hour = -1
        self._last_wal_checkpoint_hour = -1
        self._last_weather_hour = -1
        self._weather_forecast: list[dict] = []
        self._first_tick_after_start = True
        self._learned_model_warned = False
        self._last_remote_forecast_hour = -1
        self._remote_forecast_map: dict | None = None
        self._last_profile_refresh: datetime | None = None
        self.predictor = self._profile_predictor
        self.backtest_result: dict | None = None
        self.active_model_name: str = "profile"
        self._last_retrain: datetime | None = None
        self.last_decision: dict = {}
        self._last_regret_day: str | None = None
        self.last_regret: dict | None = None
        # 7-day rolling mean of (dp_regret_eur - heuristic_regret_eur).
        # Negative = DP was cheaper; set by _run_daily_regret_sync after each day.
        self.last_dp_regret_7d: float | None = None
        # Edge-trigger flag for the low-SoC infeasible WARNING (Acceptance §7).
        # True while the infeasible-at-floor condition is sustained; cleared when
        # the condition ends so the next episode logs again.
        self._infeasible_at_floor_warned: bool = False
        # C3 — export dwell/hysteresis state (persisted across ticks).
        # Initial state: disengaged, state_since = construction time.
        self.export_state: ExportState = ExportState.initial(dt_util.utcnow())
        # E3 — realized-arbitrage PnL ledger.
        # Accumulated export PnL for the current local day (euros).
        # Reset to 0.0 on local-day rollover; G2 reads this for the sensor attribute.
        self.today_export_pnl_eur: float = 0.0
        # C4: planned export revenue (€) from the current DP horizon — refreshed
        # every tick the DP runs.  Drives the card's arbitrage_pnl attribute so it
        # shows the plan, not just realized ticks (which stay 0.0 until export fires).
        self.planned_export_revenue_eur: float = 0.0
        # Local-date string of the day the accumulator covers (YYYY-MM-DD).
        # None on first tick so the day-rollover logic fires immediately to initialise.
        self._export_pnl_day: str | None = None
        # Past-actuals cache: per-clock-hour measured values for the display horizon.
        # Refreshed at most once per clock-hour (past hours never change).
        self._past_actuals_cache: dict | None = None
        self._past_actuals_hour: datetime | None = None
        # N2: last known COMPUTED house load (W) — fallback cache used whenever
        # pv/batt sensors are unavailable (skips the compute for that tick).
        # NOT persisted.  Recorder load_w / metered-net PnL / last_status all use
        # this cache-fallback value regardless of freshness.  The actuation
        # gross-setpoint compensation is the one consumer that must NOT act on a
        # stale cache hit — it gates on `self._house_load_fresh` instead (0.0
        # when not fresh; under-export is the safe direction there).
        self._last_house_load_w: float = 0.0
        # True only when the most recent _compute_house_load_w call did a live
        # compute (pv AND batt both available this tick); False when it fell back
        # to the cache above.  Set on every call; read by the export executor.
        self._house_load_fresh: bool = False
        # ── SoC drift-hedge accumulator state ─────────────────────────────────────────
        # Whole block gated on cfg.soc_hedge_fraction > 0 (default 0.0 = OFF / parity-safe).
        self._soc_drift_kwh: float = 0.0
        self._soc_drift_day: str | None = None
        # ── Layer A intraday residual corrector state (load_adapt.py) ─────────────────
        self._load_adapt_log = load_adapt.PredictionLog()
        self._load_adapt_ratio: float | None = None
        self._load_adapt_matched: int = 0
        self._soc_drift_last_update: datetime | None = None
        self._soc_drift_last_soc_pct: float | None = None
        self._soc_drift_engaged: bool = False
        self._soc_drift_last_export_kwh_dc: float = 0.0
        # Previous tick's P50 intervals (for forecast_rate_at on the NEXT tick's accumulator step).
        # Intervals are built inside compute_decision, so we cache the result to use next tick.
        # Not persisted — rebuilt from DP output every successful tick.
        self._soc_drift_last_intervals: list | None = None
        # ── Measured efficiency curve (gated by cfg.use_measured_eta, default OFF) ────
        # Built from the static fallback until the first successful recorder read;
        # refreshed at most once per EFFICIENCY_CACHE_SECONDS (see _refresh_efficiency_curve).
        self._eta_curve: EfficiencyCurve = EfficiencyCurve.static(self.cfg)
        self._eta_curve_built_at: datetime | None = None

    async def _refresh_efficiency_curve(self, now: datetime) -> None:
        """Rebuild the measured efficiency curve from recent recorder samples.

        Skipped entirely when ``use_measured_eta`` is off (the default): the
        planner uses the static scalar curve via ``_planner_curve()`` returning
        None, so no read is needed. When on, cached for
        ``EFFICIENCY_CACHE_SECONDS`` and the SQLite read runs off-loop.
        """
        if not self.cfg.use_measured_eta:
            return
        if (
            self._eta_curve_built_at is not None
            and (now - self._eta_curve_built_at).total_seconds() < const.EFFICIENCY_CACHE_SECONDS
        ):
            return
        try:
            since = (now - timedelta(days=const.EFFICIENCY_WINDOW_DAYS)).isoformat()
            rows = await self._hass.async_add_executor_job(
                self._recorder.read_efficiency_samples, since
            )
            self._eta_curve = EfficiencyCurve.build(rows, self.cfg, now)
        except Exception:
            _LOGGER.warning("efficiency curve build failed; using static fallback", exc_info=True)
            self._eta_curve = EfficiencyCurve.static(self.cfg)
        self._eta_curve_built_at = now

    def _planner_curve(self) -> EfficiencyCurve | None:
        """The measured curve to pass to the DP planner/reserve, gated by cfg.

        ``None`` when ``use_measured_eta`` is off (default) — every downstream
        eta_curve consumer treats ``None`` as "use the static scalar eta",
        which is the byte-identical parity path.
        """
        return self._eta_curve if self.cfg.use_measured_eta else None

    def _eta_d_at(self, power_w: float) -> float:
        """Discharge efficiency at ``power_w``, gated by ``cfg.use_measured_eta``.

        Mirrors ``_planner_curve()``'s gate: the static scalar
        ``optimize.eta_discharge(cfg)`` when the flag is off (byte-identical
        parity), the measured curve's power-dependent value when on.
        """
        c = self._planner_curve()
        return optimize_mod.eta_discharge(self.cfg) if c is None else c.eta_discharge(power_w)

    async def _get_past_actuals(self, now) -> dict:
        """Measured actuals per past clock-hour for the display horizon.

        Built once per clock-hour from the recorder (completed past hours never
        change) and filtered to hours strictly before now_h so the forward
        projection is untouched. Returns {} on error (past slots stay empty).
        """
        now_h = now.replace(minute=0, second=0, microsecond=0)
        if self._past_actuals_hour == now_h and self._past_actuals_cache is not None:
            return self._past_actuals_cache
        try:
            since_iso = (now - timedelta(hours=48)).isoformat()
            rows = await self._hass.async_add_executor_job(
                self._recorder.read_feature_rows, since_iso
            )
            actuals = past_actuals_mod.aggregate_past_actuals(rows)
            actuals = {h: v for h, v in actuals.items() if h < now_h}
            self._past_actuals_cache = actuals
            self._past_actuals_hour = now_h
            return actuals
        except Exception:
            _LOGGER.warning("past-actuals build failed; horizon past slots stay empty", exc_info=True)
            return {}

    def _update_load_adapt(self, now, cur_temp, past_actuals):
        """Update the base-prediction log + residual ratio; return the predictor
        for the LIVE plan (base tier unless a correction applies).

        Never raises; any failure returns the unwrapped base predictor.
        Shadow/fictive/disabled paths keep ``self.predictor`` — this wrapper is
        live-only (same pattern as estimated_tomorrow).
        """
        base = self.predictor
        now_h = now.replace(minute=0, second=0, microsecond=0)
        try:
            base_p50 = base.predict(
                now_h, cur_temp, const.DEFAULT_FALLBACK_LOAD_W, quantile=0.5,
            )
            self._load_adapt_log.record(now_h, base_p50)
        except Exception:  # noqa: BLE001 — never block the tick on logging
            pass
        try:
            ratio, matched = load_adapt.compute_ratio(
                self._load_adapt_log, past_actuals or {}, now_h,
                self.cfg.load_adapt_window_h,
            )
        except Exception:  # noqa: BLE001
            ratio, matched = None, 0
        self._load_adapt_ratio = ratio
        self._load_adapt_matched = matched
        if self.cfg.load_adapt_fraction <= 0.0 or ratio is None:
            return base
        return load_adapt.AdaptivePredictor(
            base, ratio, now, self.cfg.load_adapt_fade_h,
            self.cfg.load_adapt_fraction,
        )

    async def refresh_profile(self) -> None:
        """Read load samples from recorder and update the rolling load profile.

        Called on first tick and roughly hourly.  On error, keeps the existing
        profile and logs — never raises into tick().
        """
        try:
            now = dt_util.utcnow()
            since_iso = (now - timedelta(days=self.cfg.lookback_days)).isoformat()
            samples = await self._hass.async_add_executor_job(
                self._recorder.read_load_samples, since_iso
            )
            self.profile = forecast_mod.rolling_load_profile(
                samples, self.cfg.lookback_days, now
            )
            # Build a quantile-aware predictor from the raw samples so the profile tier
            # CAN return empirical quantiles above P50 if ever requested; live control
            # currently requests only P50 (see the P80-scaffolding note in _retrain_sync).
            self._profile_predictor = LoadPredictor.from_profile_samples(
                samples, self.cfg.lookback_days, now
            )
            self._last_profile_refresh = now
        except Exception:
            _LOGGER.warning("refresh_profile failed; keeping existing profile", exc_info=True)

    async def _snapshot_prices_on_rollover(self, now, slots) -> None:
        """Plan B (B1): on local-day rollover, snapshot the just-finished day's
        realized prices.  Date-keyed write ⇒ restart-idempotent.  `slots` carries
        ~24 h of elapsed hours, so yesterday is fully present."""
        if self._price_store is None:
            return
        today = dt_util.as_local(now).date()
        if self._price_history_day == today.isoformat():
            return
        self._price_history_day = today.isoformat()
        yday = today - timedelta(days=1)
        await self._price_store.async_snapshot(
            yday.isoformat(), pricing_store.extract_realized_day(slots, yday)
        )

    def _retrain_sync(self, since_iso: str) -> None:
        """Synchronous body of retrain — safe to run in an executor thread.

        Four-tier fallback chain:

        0. **Remote** (Tier-0) — when ``addon_enabled`` is True and a non-empty
           forecast map has been fetched this clock-hour, use it and return
           immediately.  The existing HGBR/bucketed/profile chain is skipped
           entirely.
        1. **HGBR** — tried first when the coverage gate (``is_ready``) and
           quality gate (``should_promote``) both pass.  Falls through on any
           failure so the next tier always gets a chance.
        2. **Bucketed** — BucketedLoadModel, now trained on hourly energy
           rollups (``samples_hourly`` → ``clean_hourly_rows``) instead of
           per-tick W samples; gated on ``DEFAULT_MIN_TRAIN_HOURS``.
        3. **Profile** — rolling profile fallback when all else fails.
        """
        # ------------------------------------------------------------------
        # Tier 0: Remote ML add-on (when add-on is enabled + map available)
        # ------------------------------------------------------------------
        if self.cfg.addon_enabled and self._remote_forecast_map:
            self.predictor = RemoteForecastPredictor(self._remote_forecast_map)
            self.active_model_name = "remote"
            return

        hourly_rows = self._recorder.read_hourly_rows(since_iso=since_iso)
        clean_h = clean_hourly_rows(hourly_rows)
        if self.cfg.use_learned_model:
            # ------------------------------------------------------------------
            # Tier 1: HistGBR (coverage + quality gated)
            # ------------------------------------------------------------------
            try:
                hourly = self._recorder.read_hourly_rows()
                hgbr = HGBRQuantileModel()
                if hgbr.is_ready(hourly):
                    metrics = bt.walk_forward_hgbr(
                        hourly,
                        train_days=self.cfg.train_days,
                        test_days=self.cfg.backtest_test_days,
                        fallback_w=const.DEFAULT_FALLBACK_LOAD_W,
                    )
                    if bt.should_promote(metrics):
                        # Live control consumes only P50 (review: P80 scaffolding);
                        # fitting the second quantile doubled retrain cost for no reader.
                        hgbr.fit(hourly, quantiles=(0.5,))
                        if hgbr._fitted:
                            self.predictor = LoadPredictor.from_model(hgbr)
                            self.active_model_name = "hgbr"
                            self.backtest_result = metrics
                            return
            except Exception:  # noqa: BLE001 — bad HGBR path must not crash
                _LOGGER.warning(
                    "HGBR retrain path failed; falling through to bucketed",
                    exc_info=True,
                )
            # ------------------------------------------------------------------
            # Tier 2: BucketedLoadModel — trained on hourly energy rollups
            # (samples_hourly), one FeatureRow per hour; gated on
            # DEFAULT_MIN_TRAIN_HOURS rather than the old per-tick sample count.
            # ------------------------------------------------------------------
            if len(clean_h) >= const.DEFAULT_MIN_TRAIN_HOURS:
                model = BucketedLoadModel.fit(clean_h)
                self.backtest_result = bt.walk_forward(
                    clean_h,
                    train_days=self.cfg.train_days,
                    test_days=self.cfg.backtest_test_days,
                    fallback_w=const.DEFAULT_FALLBACK_LOAD_W,
                )
                self.predictor = LoadPredictor.from_model(model)
                self.active_model_name = "bucketed"
                return
        # ------------------------------------------------------------------
        # Tier 3: rolling profile fallback (unchanged behaviour)
        # ------------------------------------------------------------------
        self.predictor = self._profile_predictor
        self.active_model_name = "profile"

    async def retrain(self, now: datetime | None = None) -> None:
        """Fit or refresh the load predictor from recorded feature rows.

        Runs a walk-forward backtest and upgrades to the learned model when
        ``use_learned_model`` is enabled and enough samples are available.
        Never raises — any error keeps the previous predictor.
        """
        try:
            if now is None:
                now = dt_util.utcnow()
            window_days = self.cfg.train_days + self.cfg.backtest_test_days * 2
            since_iso = (now - timedelta(days=window_days)).isoformat()
            if self._hass is not None:
                await self._hass.async_add_executor_job(self._retrain_sync, since_iso)
            else:
                self._retrain_sync(since_iso)
        except Exception:  # noqa: BLE001 - never break the loop on training error
            pass

    async def tick(self) -> dict:
        # Re-entrancy guard (review 1.2): the 60s timer fires regardless of the
        # previous tick; a slow retrain tick must not overlap and race the actuator.
        if self._tick_lock.locked():
            _LOGGER.warning("tick overlap: previous tick still running; skipping")
            return self.last_status
        async with self._tick_lock:
            try:
                return await self._tick_impl()
            except Exception:  # noqa: BLE001 — whole-tick failsafe (review 1.1)
                _LOGGER.exception("tick failed; releasing to self-consumption")
                now = dt_util.utcnow()
                try:
                    await self._actuator.release_to_self()
                except Exception:  # noqa: BLE001
                    _LOGGER.error("release_to_self failed in tick failsafe", exc_info=True)
                if self.export_state.engaged:
                    self.export_state = ExportState(engaged=False, state_since=now)
                self.plan = PlanState(ControllerState.PASSIVE, now, ())
                status = self._status(now, 0.0, None, "failsafe")
                status["state"] = "failsafe"
                return status

    async def _tick_impl(self) -> dict:
        now = dt_util.utcnow()
        _first_tick = self._first_tick_after_start
        self._first_tick_after_start = False
        # Refresh the measured efficiency curve off-loop (cached; cheap no-op most
        # ticks). Skipped entirely when use_measured_eta is off (default) — the
        # planner uses the static scalar; the curve rebuilds on the first tick after
        # the flag is flipped on.
        await self._refresh_efficiency_curve(now)
        # Hour-gate: the hourly forecast changes at most hourly, and an unbounded
        # await here (a hung weather integration) would otherwise wedge every 60 s
        # tick with the inverter parked. Fetch once per clock-hour; keep the last
        # good forecast if a refresh returns [] (transient failure).
        if now.hour != self._last_weather_hour:
            self._last_weather_hour = now.hour
            _fetched = await coordinator.read_hourly_weather_forecast(self._hass, self._data)
            if _fetched:
                self._weather_forecast = _fetched
        _wf_list = self._weather_forecast
        _now_hour = now.replace(minute=0, second=0, microsecond=0)
        _weather_entry = coordinator.get_forecast_for_hour(_wf_list, _now_hour)
        # Home-presence count for this tick (on-loop state reads).
        _persons_home_now = coordinator.count_persons_home(self._hass, self._data)
        # Per-hour temperature map derived from the hourly weather forecast.
        # Keys are hour-start UTC datetimes; values are temp_forecast (float | None).
        # Passed to compute_decision so each forecast interval uses its own hourly
        # temperature rather than the flat current-temperature scalar.
        _temp_by_hour: dict[datetime, float | None] = {
            e["datetime"].replace(minute=0, second=0, microsecond=0): e.get("temp_forecast")
            for e in (_wf_list or [])
            if e.get("datetime") is not None
        }

        # Hourly rollup: aggregate completed clock-hours into samples_hourly once per
        # clock-hour, regardless of enabled/disabled state.  Guard against a missing
        # recorder (partial initialisation or tests without one — the backfill sync path
        # uses try/except, but here we guard explicitly to keep the tick clean).
        if self._recorder is not None and now.hour != self._last_rollup_hour:
            self._last_rollup_hour = now.hour
            _hourly_cutoff = (
                now - timedelta(days=self.cfg.retention_hourly_days)
            ).isoformat()
            await self._hass.async_add_executor_job(
                self._rollup_hourly_sync, now.isoformat(), _hourly_cutoff
            )

        # H3a: periodic WAL checkpoint so a read-only immutable reader (the addon,
        # mounted config:ro) sees recent rows. Once per clock-hour, off-loop.
        if self._recorder is not None and now.hour != self._last_wal_checkpoint_hour:
            self._last_wal_checkpoint_hour = now.hour
            await self._hass.async_add_executor_job(self._recorder.wal_checkpoint)

        # A4: DEFAULT_USE_LEARNED_MODEL is True, but sklearn is NOT an integration
        # requirement (musl is why the addon exists) and the addon defaults off — a
        # stock install then silently falls back to the bucketed model forever.
        if (
            not self._learned_model_warned
            and self.cfg.use_learned_model
            and not self.cfg.addon_enabled
            and importlib.util.find_spec("sklearn") is None
        ):
            self._learned_model_warned = True
            _LOGGER.warning(
                "use_learned_model is on but scikit-learn is unavailable in the "
                "integration and the forecast add-on is disabled — falling back to "
                "the bucketed load model. Enable the Anker X1 Forecast add-on to "
                "use the learned model."
            )

        # Remote forecast fetch: once per clock-hour when the add-on is enabled.
        # Uses the weather forecast already fetched above as the feature payload.
        # A fetch failure (network error, add-on dormant, non-200, bad JSON) silently
        # returns None — the map is then left unchanged so the next successful fetch
        # will update it.  This never raises; any exception is swallowed here as a
        # final backstop even though fetch_forecast already guarantees non-raising.
        if self.cfg.addon_enabled and now.hour != self._last_remote_forecast_hour:
            self._last_remote_forecast_hour = now.hour
            try:
                _persons_by_ts = None
                if self._recorder is not None:
                    _ph_since = (
                        now - timedelta(days=remote_forecast.PERSONS_HOW_LOOKBACK_DAYS)
                    ).isoformat()
                    _ph_samples = await self._hass.async_add_executor_job(
                        self._recorder.read_persons_home_samples, _ph_since
                    )
                    _ph_means = remote_forecast.persons_home_hour_of_week_means(_ph_samples)
                    _ph_hour_starts = [
                        e["datetime"] for e in (_wf_list or []) if e.get("datetime") is not None
                    ]
                    _persons_by_ts = remote_forecast.project_persons_home(
                        now, _persons_home_now, _ph_means, _ph_hour_starts
                    )
                _payload = build_hours_payload(_wf_list, _persons_by_ts)
                _fetched_map = await fetch_forecast(
                    async_get_clientsession(self._hass),
                    self.cfg.addon_url,
                    self.cfg.addon_timeout,
                    _payload,
                )
                if _fetched_map is not None:
                    self._remote_forecast_map = _fetched_map
            except Exception:  # noqa: BLE001 — belt-and-suspenders; fetch_forecast never raises
                _LOGGER.debug("remote_forecast fetch raised unexpectedly", exc_info=True)

        if not self.enabled:
            # Hand control back to the X1 ONCE if we were actively engaged, then stay
            # hands-off — re-asserting self-consumption every tick would clobber a
            # user-set manual/modbus mode while disabled.
            # Derive "was engaged" from PERSISTED state on the first tick after a
            # (re)start — actuator.engaged is in-memory only and resets to False on
            # restart, so a crash while exporting/FORCING would otherwise leave the
            # inverter executing its last VPP command forever. Fire ONE release; on
            # every later disabled tick fall back to the live actuator flag so we do
            # not clobber a user-set manual/modbus mode.
            _was_engaged = self._actuator.engaged or (
                _first_tick
                and (self.plan.state is ControllerState.FORCING or self.export_state.engaged)
            )
            if _was_engaged:
                try:
                    await self._actuator.release_to_self()
                except Exception:
                    _LOGGER.error("Actuator release_to_self failed (disabled path)", exc_info=True)
            # Reset export dwell state so a later re-enable starts clean (mirror FORCING/C3).
            if self.export_state.engaged:
                self.export_state = ExportState(engaged=False, state_since=now)
            # Save the previous plan for state-machine continuity in the shadow compute,
            # then reset to PASSIVE so no committed slots are carried forward.
            _prev_plan = self.plan
            self.plan = PlanState(ControllerState.PASSIVE, now, ())
            # Persist the disengaged/PASSIVE state: the disabled branch otherwise
            # never writes the store, so a mid-disable restart would re-derive
            # "was engaged" from stale persisted export_state and re-release,
            # clobbering a user-set manual mode. Guarded on _first_tick so we do it
            # once per (re)start, not every disabled tick.
            if _first_tick:
                await self._persist()
            inputs = coordinator.read_plant_inputs(self._hass, self._data)

            # Read all forecast/schedule data needed for shadow compute and display horizon.
            slots = coordinator.read_price_slots(self._hass, self._data)
            _slot_minutes = self._resolve_slot_minutes(slots)
            pv_remaining = coordinator.read_pv_remaining_kwh(self._hass, self._data)
            tomorrow_total = coordinator.read_pv_tomorrow_kwh(self._hass, self._data)
            sun_times = coordinator.read_sun_times(self._hass, self._data)
            today_arrays = coordinator.read_pv_today_arrays(self._hass, self._data)
            tomorrow_arrays = coordinator.read_pv_tomorrow_arrays(self._hass, self._data)
            today_watts, tomorrow_watts = self._read_forecast_bundle()
            sunset = coordinator.read_sunset(self._hass, self._data)
            _temp_ent = self._data.get(const.CONF_ENT_TEMP)
            cur_temp = (
                coordinator.read_attr(self._hass, _temp_ent, "temperature")
                if _temp_ent is not None
                else None
            )

            # Read live feed-in tariff (same logic as the enabled path below).
            _shadow_export_price, _shadow_export_matches_import = self._resolve_export_price()

            # Shadow compute: run the real decision logic but NEVER actuate.
            # Use _prev_plan so the dwell / state-machine history is preserved.
            # _shadow_dp_out receives DP artefacts (dp_selected, intervals) when
            # the DP succeeds in shadow mode — used to publish fictive_plan below.
            shadow_deadline: datetime | None = None
            shadow_plan = self.plan
            _shadow_hm = "single-day"
            _shadow_dp_out: dict = {}
            if inputs is not None and slots and sunset is not None and pv_remaining is not None:
                try:
                    shadow_plan, _, shadow_deadline, _, _shadow_hm, _ = await self._hass.async_add_executor_job(
                        functools.partial(
                            compute_decision,
                            _prev_plan, inputs, slots, pv_remaining, sunset,
                            self.predictor, cur_temp, self.cfg,
                            tomorrow_total, sun_times, today_arrays, tomorrow_arrays,
                            today_watts=today_watts,
                            tomorrow_watts=tomorrow_watts,
                            export_price=_shadow_export_price,
                            _out=_shadow_dp_out,
                            _shadow_dp=True,
                            export_price_matches_import=_shadow_export_matches_import,
                            temp_by_hour=_temp_by_hour,
                            slot_minutes=_slot_minutes,
                            eta_curve=self._planner_curve(),
                        )
                    )
                except Exception:
                    _LOGGER.warning("Shadow compute_decision failed (disabled path)", exc_info=True)

            if inputs is not None:
                await self._record_sample(
                    now, inputs, setpoint=0.0, state="disabled",
                    weather_entry=_weather_entry, persons_home=_persons_home_now,
                )

            # Keep the predictor warming while disabled so the SoC/load curve
            # sharpens as collected data accumulates (same cadence as enabled).
            if (
                self._last_profile_refresh is None
                or (now - self._last_profile_refresh) >= timedelta(hours=1)
            ):
                await self.refresh_profile()
            if self._last_retrain is None or (now - self._last_retrain) >= timedelta(
                hours=self.cfg.retrain_hours
            ):
                await self.retrain(now)
                self._last_retrain = now

            # Stash decision snapshot for persistence by the recorder writer (A3).
            if inputs is not None:
                _price_window = [
                    (s.start.isoformat(), s.price)
                    for s in slots
                    if shadow_deadline is not None and now <= s.start < shadow_deadline
                ]
                self.last_decision = self._build_decision_snapshot(
                    now=now,
                    active=False,
                    soc=inputs.soc,
                    deadline=shadow_deadline,
                    committed_slots=shadow_plan.committed_slots,
                    pv_remaining=pv_remaining,
                    tomorrow_total=tomorrow_total,
                    price_window=_price_window,
                    setpoint=0.0,
                    state="disabled",
                    horizon_mode=_shadow_hm,
                )

            # A3b: persist decision snapshot to decisions table.
            await self._persist_decision_snapshot()

            # A3b: daily regret job — run on first tick after LOCAL midnight, and
            # also on first tick after restart (_last_regret_day is None).
            # _backfill_regret_sync handles both: scores yesterday + any missed days.
            await self._backfill_regret(now)

            # Shadow decision is recorded to samples + decision log for learning,
            # but live sensors stay 0 while disabled.
            # Build status AFTER the daily regret job so regret keys are fresh.
            status = self._status(now, 0.0, None, "disabled")

            # Publish a self-consumption display horizon (no grid charging) so the
            # card still renders PV + load + projected SoC while disabled.
            if inputs is not None and slots and pv_remaining is not None and sun_times is not None:
                horizon = plan_mod.build_display_horizon(
                    slots, now, today_arrays, tomorrow_arrays, sun_times,
                    self.predictor, cur_temp, const.DEFAULT_FALLBACK_LOAD_W,
                    inputs.soc, [], now, self.cfg,
                    today_watts=today_watts,
                    tomorrow_watts=tomorrow_watts,
                    temp_by_hour=_temp_by_hour,
                    eta_curve=self._planner_curve(),
                )
                if horizon:
                    self.last_status["plan"] = {
                        "horizon": horizon,
                        "deadline": now.isoformat(),
                        "planned_grid_hours": 0,
                    }

            # Publish the DP's proposed horizon as a fictive plan so the
            # dashboard shows DP intentions during the shadow period (T0.5c).
            # The DP ran purely for observation — no setpoint was ever issued.
            # Mirrors the enabled-path publication (T0.6a) with identical schema.
            if (
                _shadow_dp_out.get("dp_selected") is not None
                and shadow_deadline is not None
                and inputs is not None
            ):
                _fictive_h = plan_mod.build_plan_horizon(
                    slots,
                    _shadow_dp_out["intervals"],
                    _shadow_dp_out["dp_selected"],
                    inputs.soc,
                    shadow_deadline,
                    self.cfg,
                    grid_request_by_hour=_shadow_dp_out.get("grid_request"),
                    eta_curve=self._planner_curve(),
                )
                self.last_status["fictive_plan"] = {
                    "horizon": _fictive_h,
                    "deadline": shadow_deadline.isoformat(),
                    "planned_grid_hours": sum(1 for e in _fictive_h if e["mode"] == "grid"),
                }
            else:
                # DP did not run or failed — remove any stale fictive_plan key.
                self.last_status.pop("fictive_plan", None)

            return status

        inputs = coordinator.read_plant_inputs(self._hass, self._data)
        slots = coordinator.read_price_slots(self._hass, self._data)
        sunset = coordinator.read_sunset(self._hass, self._data)
        pv_remaining = coordinator.read_pv_remaining_kwh(self._hass, self._data)
        tomorrow_total = coordinator.read_pv_tomorrow_kwh(self._hass, self._data)
        sun_times = coordinator.read_sun_times(self._hass, self._data)

        # FIX M4 — treat all-PV-unavailable as failsafe (pv_remaining is None).
        if inputs is None or not slots or sunset is None or pv_remaining is None:
            try:
                await self._actuator.release_to_self()
            except Exception:
                _LOGGER.error("Actuator release_to_self failed (failsafe path)", exc_info=True)
            if self.export_state.engaged:
                self.export_state = ExportState(engaged=False, state_since=now)
            self.plan = PlanState(ControllerState.PASSIVE, now, ())
            return self._status(now, 0.0, None, "failsafe")

        _slot_minutes = self._resolve_slot_minutes(slots)

        # Committed-state clear on LATCHED resolution change (live/persisted path
        # only — the disabled/shadow branch above already resets self.plan to
        # PASSIVE/() every tick and never persists committed state).  Hour-keyed
        # committed_slots/committed_charge_kwh from the old resolution cannot be
        # allowed to mis-align with quarter-slot keys under the new resolution
        # (would mis-fire the hysteresis/anti-fight guards).  Modeled on the
        # existing new-day-style resets (e.g. PlanState(..., now, ()) above).
        # At a stable resolution (always 60 today) _slot_minutes never differs
        # from the init value, so this never fires — parity-safe.
        if _slot_minutes != self._committed_slot_minutes:
            self.plan = PlanState(self.plan.state, self.plan.state_since, ())
            self.plan.committed_charge_kwh = 0.0
            self.plan.committed_charge_slot = None
            self._committed_slot_minutes = _slot_minutes

        await self._snapshot_prices_on_rollover(now, slots)

        # FIX C1 — refresh load profile on first tick and roughly hourly.
        _refresh_needed = (
            self._last_profile_refresh is None
            or (now - self._last_profile_refresh) >= timedelta(hours=1)
        )
        if _refresh_needed:
            await self.refresh_profile()

        # periodic retrain
        if self._last_retrain is None or (now - self._last_retrain) >= timedelta(hours=self.cfg.retrain_hours):
            await self.retrain(now)
            self._last_retrain = now

        # Read temp the same way the recorder does (attribute, not state text).
        _temp_ent = self._data.get(const.CONF_ENT_TEMP)
        cur_temp = (
            coordinator.read_attr(self._hass, _temp_ent, "temperature")
            if _temp_ent is not None
            else None
        )

        today_arrays = coordinator.read_pv_today_arrays(self._hass, self._data)
        tomorrow_arrays = coordinator.read_pv_tomorrow_arrays(self._hass, self._data)
        today_watts, tomorrow_watts = self._read_forecast_bundle()

        # Read live feed-in tariff for the export-credit term in the DP optimizer.
        # Empty ent_export_price → None → export credit disabled (default behaviour).
        _export_price, _export_matches_import = self._resolve_export_price()

        # _dp_out receives DP artefacts when the DP succeeds:
        # {"dp_selected": [...], "intervals": [...]}.  Left empty on DP failure
        # — used below to publish / clear fictive_plan.
        _dp_out: dict = {}
        # Plan B: compute the blended tomorrow price estimate for the reserve prior.
        # Passed ONLY to the live compute_decision call; shadow + recompute keep None
        # so parity/telemetry paths are byte-identical.
        _estimated_tomorrow = None
        if self._price_store is not None:
            _tom = (dt_util.as_local(now) + timedelta(days=1)).date()
            _estimated_tomorrow = pricing_store.blend_price_prior(
                self._price_store.history, _tom,
                weight_today=self.cfg.price_blend_weight_today,
            )
        past_actuals = await self._get_past_actuals(now)

        # Layer A: residual-corrected predictor for the LIVE plan only (shadow,
        # fictive and disabled paths keep the base tier — parity preserved).
        _plan_predictor = self._update_load_adapt(now, cur_temp, past_actuals)

        # ── SoC drift-hedge (LIVE/enabled path; whole block gated OFF unless fraction>0) ──
        # Default None = byte-identical to pre-hedge (parity preserved at soc_hedge_fraction=0.0).
        hedge_drain_by_hour: dict[datetime, float] | None = None
        if self.cfg.soc_hedge_fraction > 0.0:
            _today_key = dt_util.as_local(now).date().isoformat()
            _prev_day = self._soc_drift_day
            self._soc_drift_kwh, self._soc_drift_day = soc_drift.reset_if_new_day(
                self._soc_drift_kwh, self._soc_drift_day, _today_key,
            )
            _new_day = self._soc_drift_day != _prev_day
            # "Real rollover" = day changed AND we had a previous day (not first-ever tick).
            # On first-ever start _prev_day is None; no step to gate, anchor should be written.
            _real_rollover = _new_day and _prev_day is not None
            if _real_rollover:
                # No step spans the day reset — clear the SoC anchor.
                self._soc_drift_last_soc_pct = None
            _dt_h = (
                (now - self._soc_drift_last_update).total_seconds() / 3600.0
                if self._soc_drift_last_update is not None else 0.0
            )
            _soc_now = inputs.soc
            _gated = (
                not (0.0 < _dt_h <= soc_drift.MAX_DRIFT_STEP_H)
                or self._soc_drift_last_soc_pct is None
                or self._soc_drift_last_intervals is None
                or _soc_now >= self.cfg.soc_target - 1.0
                or _soc_now <= self.cfg.soc_floor + 1.0
            )
            if not _gated:
                # _gated guards _soc_drift_last_soc_pct is None and _soc_drift_last_intervals is None;
                # assert for Pyright narrowing (runtime: impossible to fail here).
                assert self._soc_drift_last_soc_pct is not None
                assert self._soc_drift_last_intervals is not None
                # Use the P50 intervals cached from the PREVIOUS tick's DP run.
                # Intervals change at most hourly; stale-by-one-tick is functionally identical.
                _fc_pv_w, _fc_load_w = soc_drift.forecast_rate_at(
                    self._soc_drift_last_intervals, now
                )
                # Curve-derived discharge eta at the forecast deficit power (only used
                # by expected_soc_delta_kwh on the deficit branch); static scalar when
                # the flag is off (_eta_d_at's own gate) — byte-identical parity.
                _eta_d = self._eta_d_at(max(0.0, _fc_load_w - _fc_pv_w))
                _expected_dc = soc_drift.expected_soc_delta_kwh(
                    _fc_pv_w, _fc_load_w, _dt_h, self.cfg.eta_charge, _eta_d,
                )
                _measured_dc = soc_drift.measured_soc_delta_kwh(
                    _soc_now, self._soc_drift_last_soc_pct, self.cfg.capacity_kwh,
                )
                _tick_h = const.TICK_SECONDS / 3600.0
                # Duration-scale the export add-back: _last_export_kwh_dc is sized over
                # TICK_SECONDS but this step integrates _dt_h (may differ on missed ticks).
                _export_dc_step = (
                    self._soc_drift_last_export_kwh_dc * _dt_h / _tick_h
                    if _tick_h > 0 else 0.0
                )
                self._soc_drift_kwh = soc_drift.accumulate(
                    self._soc_drift_kwh,
                    soc_drift.per_step_drift_kwh(_expected_dc, _measured_dc, _export_dc_step),
                    dt_h=_dt_h, halflife_h=self.cfg.soc_drift_decay_halflife_h,
                )
                self._soc_drift_kwh = soc_drift.cap_accumulator(
                    self._soc_drift_kwh, self.cfg.capacity_kwh,
                )
            # Consume the export field; C3 re-sets it if THIS tick fires an export.
            self._soc_drift_last_export_kwh_dc = 0.0
            # On a REAL rollover (prev day known → today) leave anchor None so the very
            # next step cannot span midnight.  On fresh start or normal ticks, record now.
            if not _real_rollover:
                self._soc_drift_last_soc_pct = _soc_now
            self._soc_drift_last_update = now
            # State is flushed by the single end-of-tick _persist() call (line ~1738).
            _drift, self._soc_drift_engaged = soc_drift.drift_kwh(
                self._soc_drift_kwh, self.cfg.soc_drift_deadband_kwh,
                0.5 * self.cfg.soc_drift_deadband_kwh, self._soc_drift_engaged,
            )
            _hedge = self.cfg.soc_hedge_fraction * _drift
            if _hedge > 0.0:
                # Front-load the debit to the cheapest forward clock-hour (the trough)
                # so any over-buy lands at the cheapest tariff.
                _now_h = now.replace(minute=0, second=0, microsecond=0)
                _hedge_deadline = scheduler.compute_deadline(now, sunset, slots, self.cfg)
                _fwd = [
                    s for s in slots
                    if s.start.replace(minute=0, second=0, microsecond=0) >= _now_h
                    and s.start <= _hedge_deadline
                ]
                _trough_h = (
                    min(_fwd, key=lambda s: s.price).start.replace(
                        minute=0, second=0, microsecond=0
                    )
                    if _fwd else _now_h
                )
                hedge_drain_by_hour = {_trough_h: _hedge}

        new_plan, _, deadline, horizon, _horizon_mode_e, _ivs_reserve = await self._hass.async_add_executor_job(
            functools.partial(
                compute_decision,
                self.plan, inputs, slots, pv_remaining, sunset,
                _plan_predictor, cur_temp, self.cfg,
                tomorrow_total, sun_times, today_arrays, tomorrow_arrays,
                today_watts=today_watts,
                tomorrow_watts=tomorrow_watts,
                export_price=_export_price,
                _out=_dp_out,
                export_price_matches_import=_export_matches_import,
                estimated_tomorrow=_estimated_tomorrow,
                temp_by_hour=_temp_by_hour,
                past_actuals_by_hour=past_actuals,
                hedge_drain_by_hour=hedge_drain_by_hour,
                slot_minutes=_slot_minutes,
                eta_curve=self._planner_curve(),
            )
        )
        # Cache P50 intervals for next tick's drift accumulator step (only when hedging
        # is enabled; off→on toggle leaves it None so the H1 gate fires on the first
        # hedging tick, then sets the cache — correct first-step behaviour).
        if self.cfg.soc_hedge_fraction > 0.0:
            self._soc_drift_last_intervals = _dp_out.get("intervals")
        # C4: capture the DP's planned export revenue so the card always reflects
        # the plan (not just realized ticks which stay 0.0 until export fires).
        self.planned_export_revenue_eur = float(_dp_out.get("export_revenue_eur", 0.0))

        # Edge-triggered WARNING: fire ONCE when the battery first drains to the
        # firmware floor; re-arm when SoC recovers above the floor so the next
        # episode (e.g., the following night) logs again.
        # Only on the live/enabled path — the shadow/disabled path never reaches here.
        _at_floor = inputs.soc <= self.cfg.soc_floor + 1.0
        if _at_floor and not self._infeasible_at_floor_warned:
            self._infeasible_at_floor_warned = True
            _LOGGER.warning(
                "Battery drained to firmware floor (soc=%.1f%% <= floor %.1f%%): "
                "short on carryover charge from yesterday. Holding the reserve; "
                "will recharge at the next price-worthy slot "
                "(economic-only, no force-charge).",
                inputs.soc,
                self.cfg.soc_floor,
            )
        elif not _at_floor and self._infeasible_at_floor_warned:
            # Re-arm: SoC has recovered above floor, so next drain episode logs again.
            self._infeasible_at_floor_warned = False

        # ── C3 export executor variables (populated below when export fires) ──
        _export_setpoint_w: float | None = None
        _export_kwh: float | None = None
        _reserve_kwh_val: float | None = None
        _surplus_kwh_val: float | None = None

        # Live house load for export compensation (computed once; reused by C3 and
        # _record_sample to avoid actuation/log divergence across awaits).
        # house_load_w = pv + meter_w (signed net grid, + = import) + batt
        # (+ = discharge, − = charge) − inverter_loss, clamped to ≥ 0.  pv/batt
        # unavailable → skip the compute and fall back to the cached last-known
        # value (N2); self._house_load_fresh is set False in that case so the
        # export gross-setpoint compensation below knows not to act on it.
        # NB: distinct name from the module-level `house_load_w as _house_load_w`
        # import (a function) to avoid shadowing it within tick().
        _house_load_now_w = self._compute_house_load_w(inputs)

        # E3: reset the per-day PnL accumulator on local-day rollover, BEFORE
        # accumulating this tick's PnL.  Ensures the first export tick of a new day
        # starts from 0.0 rather than carrying yesterday's total forward.
        # _export_pnl_day is None on first tick after (re)start — treated as rollover.
        _today_for_pnl = dt_util.as_local(now).date().isoformat()
        if _today_for_pnl != self._export_pnl_day:
            self.today_export_pnl_eur = 0.0
            self._export_pnl_day = _today_for_pnl

        _engage_failed = False
        if new_plan.state is ControllerState.FORCING:
            setpoint = guard.command_setpoint(
                self.cfg.max_charge_w, self._actuator.last_setpoint_w, self.cfg,
            )
            try:
                await self._actuator.engage_and_charge(setpoint)
            except Exception:
                # Publish truth for THIS tick without a hardware release or plan
                # reset: the inverter never engaged, so setpoint 0 + PASSIVE is
                # honest; self.plan stays FORCING so the next tick retries.
                _LOGGER.error("Actuator engage_and_charge failed (FORCING path); publishing passive/0", exc_info=True)
                _engage_failed = True
                setpoint = 0.0
            # Mutual exclusion: export executor is skipped entirely while force-charging.
            # Release export state so we transition cleanly after force-charge ends.
            if self.export_state.engaged:
                self.export_state = ExportState(engaged=False, state_since=now)
        else:
            setpoint = 0.0
            if self.plan.state is ControllerState.FORCING:
                try:
                    await self._actuator.release_to_self()
                except Exception:
                    _LOGGER.error("Actuator release_to_self failed (FORCING→PASSIVE transition)", exc_info=True)

            # ── C3: live export executor ──────────────────────────────────────
            # Only fires when export is enabled and an export price is available.
            # A1 = NET-EXPORT: setpoint is export_rate directly (inverter serves
            # house load first, exports the remainder); no house_load_now term.
            if self.cfg.enable_export and _export_price is not None and _export_price > 0.0:
                # Compute ride-out reserve and battery surplus above it.
                # _ivs_reserve is the TWO-DAY reserve interval list from compute_decision
                # (6th return element).  Trough-anchored: ride_out_reserve_kwh walks
                # forward to the deepest signed-trajectory point, matching the DP floor.
                # rev-2: under the trough anchor, hour-align now + thread the SAME
                # cheap-relief map as the plan so the live export floor matches the
                # planned floor at this hour. Legacy anchor keeps the raw `now` and
                # no map → byte-identical rollback behavior (unchanged from pre-rev-2).
                if self.cfg.reserve_anchor == const.RESERVE_ANCHOR_TROUGH:
                    _cur_h_reserve = resolution.floor_to_slot(now, _slot_minutes)
                    _reserve_is_cheap = _build_is_cheap_by_hour(slots, self.cfg, _slot_minutes)
                else:
                    _cur_h_reserve = now
                    _reserve_is_cheap = None
                _reserve = energy.ride_out_reserve_kwh(
                    _cur_h_reserve, _ivs_reserve, self.cfg, is_cheap=_reserve_is_cheap,
                    slot_minutes=_slot_minutes, eta_curve=self._planner_curve(),
                )
                _surplus = energy.export_surplus_kwh(inputs.soc, _reserve, self.cfg)

                # Economic hurdle: does exporting now beat holding for later use?
                _keep_value = optimize_mod.compute_water_value(
                    # Use trough price as keep_value proxy (reuse existing helper).
                    # find_next_trough returns (dt, price); price is in €/kWh.
                    scheduler.find_next_trough(now, slots, self.cfg)[1],
                    self.cfg,
                )
                # Economic decision (which hours, how much) = the DP's committed plan.
                # Read the committed export RATE (W) for the current clock-hour; plan
                # membership is the hurdle gate.  No committed rate ⇒ no export (strictly
                # safer than the old ungated surplus-dump).  Real-time adaptation = the
                # live surplus clamp below + inverter net-export (house served first).
                # export_request is keyed on the slot grid (see _dp_select_slots);
                # slot-floor `now` so the lookup names the actual current slot.
                _cur_h = resolution.floor_to_slot(now, _slot_minutes)
                _committed_export = _dp_out.get("export_request") or {}
                _hurdle = _cur_h in _committed_export

                # Decide next export dwell/hysteresis state.
                _new_export_state = scheduler.decide_export_state(
                    self.export_state,
                    surplus_kwh=_surplus,
                    hurdle_clears=_hurdle,
                    now=now,
                    cfg=self.cfg,
                )

                if _new_export_state.engaged:
                    # NET target: drain the live surplus-above-reserve decisively
                    # over cfg.export_drain_window_h (default 0.0 → one tick → at the
                    # export cap, stopping at the live reserve on the final tick).
                    # _hurdle gates WHETHER to export (DP plan membership); committed
                    # rate no longer throttles HOW FAST.
                    _net_target_w = energy.export_net_target_w(
                        _surplus, self.cfg, eta_curve=self._planner_curve(),
                    )
                    # GROSS setpoint must cover house load (firmware serves house
                    # first, exports the remainder).  Bounded only by SETPOINT_MAX_W
                    # via discharge_cap_w (max_export_w already capped net_target).
                    # Only compensate with a FRESH read (A: fix for a safety
                    # regression) — a stale cached value (pv/batt sensor blip this
                    # tick, soc+meter still live so no failsafe) must not inflate
                    # the gross setpoint beyond the reserve-aware target;
                    # under-compensating (0.0) is the safe direction here.
                    _load_comp_w = (
                        self.cfg.export_load_comp_factor * _house_load_now_w
                        if self._house_load_fresh else 0.0
                    )
                    _gross_w = _net_target_w + _load_comp_w
                    _export_sp = guard.command_setpoint(
                        -_gross_w,
                        self._actuator.last_setpoint_w,
                        self.cfg,
                        discharge_cap_w=const.SETPOINT_MAX_W,
                    )
                    # command_setpoint returns positive value for discharge; engage_export
                    # validates > 0, so a sign error here fails loudly (safety-net).
                    if _export_sp > 0:
                        try:
                            await self._actuator.engage_export(_export_sp)
                            _export_setpoint_w = _export_sp
                            # Metered net to grid = gross setpoint − house load
                            # (firmware serves house first).  Drives PnL + record.
                            # TELEMETRY ONLY: uses _house_load_now_w regardless of
                            # freshness (N2 cache-fallback covers a pv/batt blip) —
                            # unlike the actuation gross-setpoint compensation above,
                            # which zeroes out on a stale (non-fresh) read.
                            # _compute_house_load_w always returns a float, never
                            # None (cache-fallback on pv/batt unavailable).
                            _metered_net_w = max(0.0, _export_sp - _house_load_now_w)
                            _export_kwh = (
                                _metered_net_w / 1000.0 * (const.TICK_SECONDS / 3600.0)
                            )
                            _reserve_kwh_val = _reserve
                            _surplus_kwh_val = _surplus
                            # E3: accumulate realized PnL for this export interval.
                            # Price at the effective (post-fee) rate so PnL matches
                            # the DP's objective (gross − export_fee).
                            # PnL uses the DC-stored basis export_pnl_eur expects:
                            # convert AC metered export to DC drawn (AC / eta_discharge)
                            # so revenue = AC * price (the helper's eta_discharge cancels
                            # — no spurious second factor); cost/opportunity scale on DC
                            # energy actually dispatched. Telemetry-only; export_pnl_eur
                            # and _export_kwh (recorded AC) are unchanged.
                            _eta_d = self._eta_d_at(_metered_net_w)
                            _export_kwh_dc = (
                                _export_kwh / _eta_d if _eta_d > 1e-9 else _export_kwh
                            )
                            # Retain for the NEXT tick's drift add-back (duration-scaled).
                            # The drift step re-zeros this field at its start; C3 re-sets
                            # it here only when an export actually fired this tick.
                            # Restart-gap caveat: if the process restarts between C3 and the
                            # next tick's end-of-tick _persist(), this value is lost and the
                            # add-back for that export window is skipped — self-correcting
                            # (one missed add-back → slight over-count in accumulator for
                            # one step). Do NOT add an extra _persist() here; that risks
                            # double-counting if C3 fires multiple times per tick.
                            self._soc_drift_last_export_kwh_dc = _export_kwh_dc
                            _eff_export_price = optimize_mod.effective_export_price(
                                _export_price, self.cfg
                            )
                            _tick_pnl = optimize_mod.export_pnl_eur(
                                _export_kwh_dc, _eff_export_price, _keep_value, self.cfg
                            )
                            self.today_export_pnl_eur += _tick_pnl
                        except Exception:
                            _LOGGER.error("Actuator engage_export failed (C3 path)", exc_info=True)
                            # Engage failed → do NOT report engaged. Force a clean
                            # disengaged state and best-effort release so the next
                            # tick starts from self-consumption (mirror FORCING L1409).
                            _new_export_state = ExportState(engaged=False, state_since=now)
                            try:
                                await self._actuator.release_to_self()
                            except Exception:
                                _LOGGER.error(
                                    "Actuator release_to_self failed (engage_export except)",
                                    exc_info=True,
                                )
                    else:
                        # Surplus too small to quantize to a valid step — release.
                        _new_export_state = ExportState(engaged=False, state_since=now)
                        if self.export_state.engaged:
                            try:
                                await self._actuator.release_to_self()
                            except Exception:
                                _LOGGER.error(
                                    "Actuator release_to_self failed (C3 zero-rate path)",
                                    exc_info=True,
                                )
                else:
                    # Gate fail or surplus below lo-eps: release if currently engaged.
                    if self.export_state.engaged:
                        try:
                            await self._actuator.release_to_self()
                        except Exception:
                            _LOGGER.error(
                                "Actuator release_to_self failed (C3 disengage path)",
                                exc_info=True,
                            )

                self.export_state = _new_export_state
            else:
                # Export disabled or no export price: release if engaged.
                if self.export_state.engaged:
                    self.export_state = ExportState(engaged=False, state_since=now)
                    try:
                        await self._actuator.release_to_self()
                    except Exception:
                        _LOGGER.error(
                            "Actuator release_to_self failed (export disabled path)",
                            exc_info=True,
                        )

        self.plan = new_plan
        await self._persist()
        await self._record_sample(
            now, inputs,
            setpoint=setpoint,
            state="passive" if _engage_failed else new_plan.state.value,
            weather_entry=_weather_entry,
            export_setpoint_w=_export_setpoint_w,
            export_kwh=_export_kwh,
            reserve_kwh=_reserve_kwh_val,
            surplus_kwh=_surplus_kwh_val,
            house_load_w=_house_load_now_w,
            persons_home=_persons_home_now,
        )

        # Stash decision snapshot for persistence by the recorder writer (A3).
        _price_window_e = [
            (s.start.isoformat(), s.price)
            for s in slots
            if deadline is not None and now <= s.start < deadline
        ]
        self.last_decision = self._build_decision_snapshot(
            now=now,
            active=new_plan.state is ControllerState.FORCING and not _engage_failed,
            soc=inputs.soc,
            deadline=deadline,
            committed_slots=new_plan.committed_slots,
            pv_remaining=pv_remaining,
            tomorrow_total=tomorrow_total,
            price_window=_price_window_e,
            setpoint=setpoint,
            state="passive" if _engage_failed else new_plan.state.value,
            horizon_mode=_horizon_mode_e,
        )

        # A3b: persist decision snapshot to decisions table.
        await self._persist_decision_snapshot()

        # A3b: daily regret job — run on first tick after LOCAL midnight, and
        # also on first tick after restart (_last_regret_day is None).
        await self._backfill_regret(now)

        if now.hour % 6 == 0 and now.hour != self._last_purge_hour:
            self._last_purge_hour = now.hour
            await self._hass.async_add_executor_job(
                self._recorder.purge_older_than, now.isoformat(), self.cfg.retention_days
            )
            # Purge stale decision rows on the same 6-hour schedule; else the
            # decisions table grows at ~1440 rows/day indefinitely.
            _cutoff = (now - timedelta(days=self.cfg.retention_days)).isoformat()
            await self._hass.async_add_executor_job(
                self._recorder.purge_decisions_older_than, _cutoff
            )
        required_kwh = max(0.0, (self.cfg.soc_target - inputs.soc) / 100.0 * self.cfg.capacity_kwh)
        # Full kWh required to reach soc_target from current SoC. Used as the
        # solar_charge_kwh status key so the dashboard shows the charge-to-target gap.
        solar_charge = required_kwh
        result = self._status(now, setpoint, deadline, "ok", solar_charge=solar_charge)
        if _engage_failed:
            # Override the published state only (self.plan intentionally left FORCING).
            result["state"] = "passive"
        # E2: surface the live export setpoint for observability only.
        # _status publishes 0.0/"passive" because export runs in the non-FORCING
        # branch with setpoint=0.0 — state is intentionally left untouched.
        # The recorder's smartcharge_state column (_record_sample, called above
        # at line 2201) already always records the plan state ("passive" during
        # export), so leaving last_status["state"] alone matches the recorded
        # history instead of diverging from it. A dedicated sensor reads the key.
        self.last_status["export_setpoint_w"] = _export_setpoint_w
        # T16: surface the live per-tick house load for observability. Mirrors
        # export_setpoint_w above — this line only runs in the enabled/"ok" tick
        # path. last_status is a persistent dict mutated in place (same as
        # export_setpoint_w), so a disabled/failsafe tick does NOT remove this
        # key — it simply leaves whatever value the last enabled tick wrote.
        # A dedicated sensor reads the key.
        self.last_status["house_load_w"] = _house_load_now_w
        self.last_status["plan"] = {
            "horizon": horizon,
            "deadline": deadline.isoformat() if deadline else None,
            "planned_grid_hours": sum(1 for e in horizon if e["mode"] == "grid"),
        }
        # Publish the DP optimizer's proposed horizon as a second (fictive) plan so
        # shadow mode is legible on the dashboard (T0.6a).  The fictive horizon is
        # always built via build_plan_horizon — identical per-entry schema to "plan".
        if _dp_out.get("dp_selected") is not None:
            # DP ran successfully — publish live DP's plan (T0.6a).
            # Published only when the DP succeeded; absent (key removed) otherwise so
            # consumers never see stale data.
            _fictive_h = plan_mod.build_plan_horizon(
                slots,
                _dp_out["intervals"],
                _dp_out["dp_selected"],
                inputs.soc,
                deadline,
                self.cfg,
                grid_request_by_hour=_dp_out.get("grid_request"),
                eta_curve=self._planner_curve(),
            )
            self.last_status["fictive_plan"] = {
                "horizon": _fictive_h,
                "deadline": deadline.isoformat() if deadline else None,
                "planned_grid_hours": sum(1 for e in _fictive_h if e["mode"] == "grid"),
            }
        else:
            # DP failed — remove stale key to prevent consumers from reading an
            # outdated fictive plan from a previous tick.
            self.last_status.pop("fictive_plan", None)
        return result

    def _write_decision_sync(self, snapshot: dict) -> None:
        """Synchronous: persist one decision snapshot to the decisions table.

        Safe to run in an executor thread.  The caller guarantees snapshot is
        a complete 12-key dict matching append_decision's kwargs signature.
        """
        self._recorder.append_decision(**snapshot)

    def _rollup_hourly_sync(self, now_iso: str, cutoff_iso: str) -> None:
        """Synchronous: roll up completed clock-hours into samples_hourly and purge old rows.

        Called once per clock-hour via async_add_executor_job (blocking sqlite calls).
        ``now_iso``    — current UTC time as ISO string (upper bound for rollup).
        ``cutoff_iso`` — delete samples_hourly rows whose hour_ts < this value.
        """
        self._recorder.rollup_hours(now_iso)
        self._recorder.purge_hourly_older_than(cutoff_iso)

    def _run_daily_regret_sync(self, day: str, computed_ts: str) -> None:
        """Synchronous: compute and persist the regret score for a completed local calendar day.

        Reads yesterday's samples from the recorder, buckets them by LOCAL hour,
        calls regret.hindsight_optimal_grid / realized_grid_cost / score_regret,
        then upserts the result into daily_regret.
        Never raises — any error is logged and the run is silently skipped.

        Parameters
        ----------
        day          : YYYY-MM-DD LOCAL calendar day string.
        computed_ts  : ISO-8601 UTC timestamp to store as computed_ts in the row.
        """
        try:
            # Build a UTC read window wide enough to cover the local day in any timezone.
            # We anchor at UTC midnight of the *same YYYY-MM-DD string* (not the actual
            # local midnight, which differs by the TZ offset).  The ±14h / +38h buffer
            # covers every real-world UTC offset (max ±14h), so every tick that local-date
            # belongs to *day* is captured.  Per-sample bucketing (dt_util.as_local below)
            # then ensures only ticks whose local date == *day* are counted — the wide
            # window merely avoids missing anything at the edges.
            # NOTE v1 limitation: triggering is now based on LOCAL midnight
            # (dt_util.as_local), but the read window below is still anchored at UTC
            # midnight of the date string.  A future improvement could anchor the window
            # at the actual local midnight (ref_utc = dt_util.as_local → to_utc) for
            # tighter reads.  The current approach is correct but reads a wider window.
            year, month, dom = int(day[:4]), int(day[5:7]), int(day[8:10])
            ref_utc = datetime(year, month, dom, tzinfo=timezone.utc)
            since_iso = (ref_utc - timedelta(hours=14)).isoformat()
            until_iso = (ref_utc + timedelta(hours=38)).isoformat()

            samples = self._recorder.read_feature_rows(since_iso=since_iso)
            # Filter to UTC samples that fall inside the over-sized window.
            samples = [s for s in samples if s.get("ts", "") < until_iso]

            if not samples:
                _LOGGER.debug("Daily regret skipped for %s: no samples in window", day)
                return

            # Slot resolution for the LOCAL-day buckets below. At 60-min
            # _slot_minutes=60, _spd=24 -> byte-identical to the legacy hourly
            # ledger; at 15-min _spd=96 so each bucket is a quarter-hour.
            _slot_minutes = self._detected_slot_minutes
            _spd = 24 * 60 // _slot_minutes
            # Known cosmetic limitation: this assumes exactly 24h/day, so DST
            # fold/gap days (~2/yr) blur one bucket at the transition boundary.
            # Hours per slot: 1.0 at 60-min (identity), 0.25 at 15-min. Used to
            # convert average-W buckets to per-slot kWh below, and threaded into
            # every DP-optimal / realized-cost call so the OPTIMAL and REALIZED
            # sides share the same slot width (BC3).
            _dt_h = _slot_minutes / 60.0

            # Aggregate raw per-tick samples into LOCAL per-slot buckets.
            pv_by_hour: dict[int, list[float]] = defaultdict(list)
            load_by_hour: dict[int, list[float]] = defaultdict(list)
            price_by_hour: dict[int, list[float]] = defaultdict(list)
            charge_by_hour: dict[int, list[float]] = defaultdict(list)
            # F3: aggregate actual export (from p1_w < 0) and feed-in price per hour.
            actual_export_by_hour: dict[int, list[float]] = defaultdict(list)
            export_price_by_hour: dict[int, list[float]] = defaultdict(list)
            hours_with_data: set[int] = set()

            soc_start: float | None = None
            for s in samples:
                ts_str = s.get("ts", "")
                if not ts_str:
                    continue
                try:
                    ts_dt = datetime.fromisoformat(ts_str)
                except ValueError:
                    continue
                # Convert to local time and skip if it doesn't belong to *day*.
                local_dt = dt_util.as_local(ts_dt)
                if local_dt.date().isoformat() != day:
                    continue
                h = (local_dt.hour * 60 + local_dt.minute) // _slot_minutes
                hours_with_data.add(h)
                if soc_start is None and s.get("soc") is not None:
                    soc_start = float(s["soc"])

                pv_raw = float(s["pv_w"]) if s.get("pv_w") is not None else 0.0
                batt_raw = float(s["batt_w"]) if s.get("batt_w") is not None else 0.0
                p1_raw = float(s["p1_w"]) if s.get("p1_w") is not None else 0.0

                # House load: prefer the recorded load_w column (now computed
                # per-tick from pv+meter+batt−loss by _compute_house_load_w,
                # not read from sensor.power_usage); fall back to AC energy
                # balance p1+batt+pv for older rows that predate that column.
                # Clamped to 0 to avoid negative "load" during export.
                _raw_load = _house_load_w(s)
                load_w = max(0.0, _raw_load) if _raw_load is not None else max(0.0, p1_raw + batt_raw + pv_raw)

                # Grid charge = battery charging current minus the solar surplus
                # that the battery could absorb without pulling from grid.
                battery_charge_w = max(0.0, -batt_raw)       # >0 only when charging
                solar_surplus_w = max(0.0, -(p1_raw + batt_raw))  # surplus to meter
                grid_charge_w = max(0.0, battery_charge_w - solar_surplus_w)

                # F3 (review 2.2): realized export must be BATTERY-ONLY to match
                # the oracle and shadow-DP (battery discharge actions).  Metered
                # −p1_w includes PV spill the oracle can't credit → sunny-day
                # regret went artificially negative.  batt_raw > 0 = discharging.
                actual_export_w = min(max(0.0, -p1_raw), max(0.0, batt_raw))

                pv_by_hour[h].append(pv_raw)
                load_by_hour[h].append(load_w)
                if s.get("import_price") is not None:
                    price_by_hour[h].append(float(s["import_price"]))
                charge_by_hour[h].append(grid_charge_w)
                # Actual export and feed-in price (E1-fixed export_price column).
                actual_export_by_hour[h].append(actual_export_w)
                if s.get("export_price") is not None:
                    export_price_by_hour[h].append(float(s["export_price"]))

            # Sparse-day guard: require at least half the day's slots present.
            if len(hours_with_data) < _spd // 2:
                _LOGGER.debug(
                    "Daily regret skipped for %s: only %d slots with data (< %d)",
                    day, len(hours_with_data), _spd // 2,
                )
                return

            if soc_start is None:
                _LOGGER.debug("Daily regret skipped for %s: no SoC data", day)
                return

            def _mean(lst: list[float], fallback: float = 0.0) -> float:
                return sum(lst) / len(lst) if lst else fallback

            pv_kwh = tuple(_mean(pv_by_hour[h]) / 1000.0 * _dt_h for h in range(_spd))
            load_kwh = tuple(
                _mean(load_by_hour[h], const.DEFAULT_FALLBACK_LOAD_W) / 1000.0 * _dt_h
                for h in range(_spd)
            )
            price = tuple(_mean(price_by_hour[h], 0.20) for h in range(_spd))
            # Realized grid charge per slot: mean of per-sample grid_charge_w → AC kWh.
            realized_charge = [_mean(charge_by_hour[h]) / 1000.0 * _dt_h for h in range(_spd)]

            # F3: build per-slot actual export (AC kWh) and feed-in price arrays.
            # Actual export is derived from metered −p1_w, capped at the battery's
            # discharge power (NOT commanded setpoint) — see actual_export_w above.
            # Only pass to the scoring functions when at least one export slot has
            # a known feed-in price (otherwise no revenue can be computed).
            _realized_export_kwh: list[float] | None = None
            _export_price_tuple: tuple[float, ...] | None = None
            if export_price_by_hour:
                # Mean actual export W → AC kWh; 0.0 for slots with no export ticks.
                _realized_export_kwh = [
                    _mean(actual_export_by_hour[h]) / 1000.0 * _dt_h for h in range(_spd)
                ]
                # Mean feed-in price per slot; 0.0 for slots without export_price data.
                _export_price_tuple = tuple(
                    _mean(export_price_by_hour[h]) if export_price_by_hour[h] else 0.0
                    for h in range(_spd)
                )

            # Water-value terminal: value end-SoC by the realized day's trough so
            # the oracle shares the live planner's objective (regret stays
            # internally consistent).
            _terminal_mode = "water_value"
            _water_value = optimize_mod.compute_water_value(min(price), self.cfg)

            # Build fee-adjusted export prices for the nightly regret scorer below.
            # Raw feed-in prices are reduced by cfg.export_fee_eur_per_kwh so that the
            # oracle and DP both see the same effective (net) price that the live planner
            # uses when deciding to export.  None when export is disabled or data absent.
            eff_export: list[float] | None = None
            if _export_price_tuple is not None and self.cfg.enable_export:
                eff_export = [
                    optimize_mod.effective_export_price(p, self.cfg)
                    for p in _export_price_tuple
                ]

            day_data = regret_mod.DayData(
                pv_kwh=pv_kwh,
                load_kwh=load_kwh,
                price=price,
                soc_start=soc_start,
            )
            optimal = regret_mod.hindsight_optimal_grid(
                day_data, self.cfg,
                terminal_mode=_terminal_mode, water_value=_water_value,
                export_price=eff_export,
                dt_h=_dt_h,
            )

            # Shadow DP regret: compute the DP schedule on realized data (perfect
            # foresight) and score it against the oracle. This inline scorer is the
            # single implementation (the standalone walk_forward_regret test harness
            # was removed in Task 13). Stored alongside the heuristic regret for
            # 7-day comparison.
            # Never raises — failure → dp_regret_eur stays None.
            dp_regret_eur: float | None = None
            if not optimal.get("infeasible", False):
                try:
                    _dp_result = optimize_mod.optimize_grid(
                        list(pv_kwh),
                        list(load_kwh),
                        list(price),
                        soc_start,
                        self.cfg,
                        window_start_h=0,
                        window_len=_spd,
                        slots_per_day=_spd,
                        terminal_mode=_terminal_mode,
                        water_value=_water_value,
                        export_price=eff_export,
                        dt_h=_dt_h,
                    )
                    _dp_export = _dp_result.get("export_schedule")
                    _dp_realized = regret_mod.realized_grid_cost(
                        day_data, _dp_result["schedule"], self.cfg,
                        realized_export_by_hour=_dp_export,
                        export_price=eff_export,
                        dt_h=_dt_h,
                    )
                    _dp_score = regret_mod.score_regret(_dp_realized, optimal)
                    dp_regret_eur = _dp_score["regret_eur"]
                except Exception:  # noqa: BLE001 — shadow DP failure must not block heuristic regret
                    _LOGGER.debug(
                        "Shadow DP regret computation failed for %s", day, exc_info=True
                    )

            # INFEASIBLE policy: upsert a marker row but leave metric fields NULL.
            if optimal.get("infeasible", False):
                self._recorder.upsert_daily_regret(
                    day=day,
                    regret_eur=None,
                    over_buy_kwh=None,
                    over_buy_eur=None,
                    under_buy_kwh=None,
                    cost_regret_eur=None,
                    optimal_kwh=None,
                    optimal_eur=None,
                    realized_kwh=None,
                    realized_eur=None,
                    infeasible=1,
                    computed_ts=computed_ts,
                    dp_regret_eur=None,
                )
                self.last_regret = {
                    "day": day,
                    "regret_eur": None,
                    "over_buy_kwh": None,
                    "under_buy_kwh": None,
                }
                _LOGGER.info("Daily regret for %s: infeasible day — null metrics", day)
                return

            realized = regret_mod.realized_grid_cost(
                day_data, realized_charge, self.cfg,
                realized_export_by_hour=_realized_export_kwh,
                export_price=eff_export,
                dt_h=_dt_h,
            )
            score = regret_mod.score_regret(realized, optimal)

            self._recorder.upsert_daily_regret(
                day=day,
                regret_eur=score["regret_eur"],
                over_buy_kwh=score["over_buy_kwh"],
                over_buy_eur=score["over_buy_eur"],
                under_buy_kwh=score["under_buy_kwh"],
                cost_regret_eur=score["cost_regret_eur"],
                optimal_kwh=optimal["kwh"],
                optimal_eur=optimal["eur"],
                realized_kwh=realized["kwh"],
                realized_eur=realized["eur"],
                infeasible=0,
                computed_ts=computed_ts,
                dp_regret_eur=dp_regret_eur,
            )
            self.last_regret = {
                "day": day,
                "regret_eur": score["regret_eur"],
                "over_buy_kwh": score["over_buy_kwh"],
                "under_buy_kwh": score["under_buy_kwh"],
            }
            _LOGGER.info(
                "Daily regret for %s: regret_eur=%.4f over_buy_kwh=%.3f under_buy_kwh=%.3f"
                " dp_regret_eur=%s",
                day, score["regret_eur"], score["over_buy_kwh"], score["under_buy_kwh"],
                f"{dp_regret_eur:.4f}" if dp_regret_eur is not None else "n/a",
            )

            # Compute the 7-day rolling DP-vs-heuristic regret delta.
            # Negative means DP was cheaper over the window; None until ≥1 day.
            # This runs in the executor thread so the DB read is blocking-safe.
            try:
                _since_7d = (date.fromisoformat(day) - timedelta(days=6)).isoformat()
                _rows_7d = self._recorder.read_daily_regret_range(_since_7d)
                _valid = [
                    (r["dp_regret_eur"], r["regret_eur"])
                    for r in _rows_7d
                    if r.get("dp_regret_eur") is not None
                    and r.get("regret_eur") is not None
                    and not r.get("infeasible", 0)
                ]
                if _valid:
                    self.last_dp_regret_7d = (
                        sum(dp - h for dp, h in _valid) / len(_valid)
                    )
            except Exception:  # noqa: BLE001 — 7d delta failure must not block regret logging
                _LOGGER.debug("7d DP-vs-heuristic delta computation failed", exc_info=True)

        except Exception:
            _LOGGER.warning("Daily regret computation failed for %s", day, exc_info=True)

    def _backfill_regret_sync(self, today_str: str, computed_ts: str) -> None:
        """Score any regret days missed since the last scored entry (up to 7 days back).

        Called on the first tick after LOCAL midnight (or first tick ever after startup).
        Uses read_latest_daily_regret to find the last scored day, then scores each gap
        day in [from_day, yesterday] that is not already present in daily_regret.

        The upsert-by-day is idempotent, so concurrent or repeated calls are safe.
        Capped at 7 days back to bound compute time on a long outage.
        """
        try:
            today_date = date.fromisoformat(today_str)

            latest = self._recorder.read_latest_daily_regret()
            if latest is None:
                # No scored days yet: backfill up to 7 days.
                from_date = today_date - timedelta(days=7)
            else:
                # Start from the day after the last scored day.
                from_date = date.fromisoformat(latest["day"]) + timedelta(days=1)
                # Never look back more than 7 days regardless of the gap.
                min_date = today_date - timedelta(days=7)
                if from_date < min_date:
                    from_date = min_date

            # Fetch already-scored rows in the window so we stay idempotent.
            from_day_str = from_date.isoformat()
            scored = self._recorder.read_daily_regret_range(from_day_str, today_str)
            scored_set = {r["day"] for r in scored}

            # Score each unscored day from from_date through yesterday.
            yesterday = today_date - timedelta(days=1)
            current = from_date
            while current <= yesterday:
                day_str = current.isoformat()
                if day_str not in scored_set:
                    self._run_daily_regret_sync(day_str, computed_ts)
                current += timedelta(days=1)
        except Exception:
            _LOGGER.warning("Regret backfill failed", exc_info=True)

    def _build_decision_snapshot(
        self,
        *,
        now: datetime,
        active: bool,
        soc: float,
        deadline: datetime | None,
        committed_slots: tuple,
        pv_remaining: float | None,
        tomorrow_total: float | None,
        price_window: list,
        setpoint: float,
        state: str,
        horizon_mode: str,
    ) -> dict:
        """Build the self.last_decision dict with identical 12-key schema for both paths.

        Called from the disabled (shadow) path and the enabled path so the keys/types
        can never silently diverge — A3 calls append_decision(**self.last_decision).
        """
        return {
            "ts": now.isoformat(),
            "active": active,
            "start_soc": float(soc),
            "deadline": deadline.isoformat() if deadline else None,
            "committed_hours": [h.isoformat() for h in committed_slots],
            "horizon_mode": horizon_mode,
            "pv_today_forecast_kwh": float(pv_remaining) if pv_remaining is not None else None,
            "pv_tomorrow_forecast_kwh": float(tomorrow_total) if tomorrow_total is not None else None,
            "predicted_load_json": None,
            "price_window_json": json.dumps(price_window) if price_window else None,
            "setpoint_w": float(setpoint),
            "state": state,
        }

    def _status(self, now, setpoint, deadline, reason, solar_charge: float = 0.0) -> dict:
        _regret = self.last_regret or {}
        _bt = self.backtest_result or {}
        self.last_status = {
            "state": self.plan.state.value,
            "solar_charge_kwh": round(solar_charge, 3),
            "setpoint_w": setpoint,
            "deadline": deadline.isoformat() if deadline else None,
            "reason": reason,
            "load_mae": _bt.get("model_mae"),
            "horizon_energy_mae_24h": _bt.get("horizon_energy_mae_24h"),
            "horizon_energy_mae_12h": _bt.get("horizon_energy_mae_12h"),
            "pinball_p50": _bt.get("pinball_p50"),
            "pinball_p80": _bt.get("pinball_p80"),
            "active_model": self.active_model_name,
            "load_adapt_ratio": (
                round(self._load_adapt_ratio, 3)
                if self._load_adapt_ratio is not None else None
            ),
            "load_adapt_matched_hours": self._load_adapt_matched,
            "regret_eur": _regret.get("regret_eur"),
            "over_buy_kwh": _regret.get("over_buy_kwh"),
            "under_buy_kwh": _regret.get("under_buy_kwh"),
            # 7-day rolling DP-vs-heuristic regret delta (T0.5c).
            # Negative = DP was cheaper over past 7 days; None until first day scored.
            "dp_regret_7d": self.last_dp_regret_7d,
            # E3: realized export PnL for the current local day (€).
            # Accumulated per tick when the C3 export executor fires.
            # Resets to 0.0 on local-day rollover.  G2 reads this key.
            "today_export_pnl_eur": round(self.today_export_pnl_eur, 6),
            # C4: the DP's PLANNED export revenue (€) for the current horizon. Drives
            # the card's arbitrage_pnl so it reflects the plan, not just realized ticks.
            "planned_export_revenue_eur": round(self.planned_export_revenue_eur, 6),
            "slot_minutes": self._detected_slot_minutes,
            # T18: measured efficiency curve bin table, for observability only
            # (does not drive behaviour — that's gated by use_measured_eta below).
            "efficiency_curve": self._eta_curve.as_attributes(),
            "use_measured_eta": self.cfg.use_measured_eta,
        }
        return self.last_status

    async def release(self) -> None:
        """Release actuator control back to self (best-effort; never raises).

        Waited behind the tick lock (bounded by _SHUTDOWN_LOCK_TIMEOUT_S) so an
        unload/reload cannot interleave the release with an in-flight tick's
        engage_* calls, and so __init__.async_unload_entry's recorder.close runs
        only after the in-flight tick's awaited recorder writes have drained.
        """
        acquired = False
        try:
            await asyncio.wait_for(
                self._tick_lock.acquire(), timeout=_SHUTDOWN_LOCK_TIMEOUT_S
            )
            acquired = True
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001 — never block teardown
            _LOGGER.warning(
                "release: tick lock not acquired within %ss; releasing anyway",
                _SHUTDOWN_LOCK_TIMEOUT_S,
            )
        try:
            await self._actuator.release_to_self()
        except Exception:
            _LOGGER.warning("release_to_self failed during controller release", exc_info=True)
        finally:
            if acquired:
                self._tick_lock.release()

    async def _persist(self) -> None:
        await self._store.async_save({
            "plan": self.plan.to_dict(),
            "enabled": self.enabled,
            "export_state": self.export_state.to_dict(),
            # E3: persist the per-day PnL accumulator so a mid-day HA restart
            # does not silently zero today's total.
            "today_export_pnl_eur": self.today_export_pnl_eur,
            "export_pnl_day": self._export_pnl_day,
            # SoC drift-hedge accumulator: persist so a restart resumes from the
            # same closed-loop state rather than re-accumulating from scratch.
            "soc_drift_kwh": self._soc_drift_kwh,
            "soc_drift_day": self._soc_drift_day,
            "soc_drift_last_update": (
                self._soc_drift_last_update.isoformat()
                if self._soc_drift_last_update is not None else None
            ),
            "soc_drift_last_soc_pct": self._soc_drift_last_soc_pct,
            "soc_drift_engaged": self._soc_drift_engaged,
            "soc_drift_last_export_kwh_dc": self._soc_drift_last_export_kwh_dc,
        })

    async def set_enabled(self, value: bool) -> None:
        """Set the master enable flag and persist it immediately."""
        self.enabled = bool(value)
        await self._persist()

    def restore(self, saved: dict) -> None:
        """Restore plan + enabled + export_state from a saved store payload (back-compatible).

        New format is {"plan": <dict>, "enabled": <bool>, "export_state": <dict>};
        a legacy bare plan dict (no "plan" key) restores the plan only and leaves
        enabled and export_state at their defaults.
        Missing "export_state" key is silently ignored — preserves the existing
        initial (disengaged) state so upgrades from pre-C3 stores are safe.
        """
        try:
            if "plan" in saved:
                self.plan = PlanState.from_dict(saved["plan"])
                self.enabled = bool(saved.get("enabled", False))
            else:
                self.plan = PlanState.from_dict(saved)
        except (KeyError, ValueError, TypeError):
            pass
        # Restore export_state if present; silently skip if absent (back-compat).
        try:
            if "export_state" in saved:
                self.export_state = ExportState.from_dict(saved["export_state"])
        except (KeyError, ValueError, TypeError):
            pass
        # E3: restore per-day PnL accumulator if present (silently skip on upgrade
        # from pre-E3 stores — leaves the defaults from __init__ in place).
        try:
            if "today_export_pnl_eur" in saved:
                self.today_export_pnl_eur = float(saved["today_export_pnl_eur"])
            if "export_pnl_day" in saved and saved["export_pnl_day"] is not None:
                self._export_pnl_day = str(saved["export_pnl_day"])
        except (KeyError, ValueError, TypeError):
            pass
        # SoC drift-hedge accumulator (silently skip on upgrade from pre-drift stores).
        try:
            if "soc_drift_kwh" in saved:
                self._soc_drift_kwh = float(saved["soc_drift_kwh"])
            if "soc_drift_day" in saved and saved["soc_drift_day"] is not None:
                self._soc_drift_day = str(saved["soc_drift_day"])
            if "soc_drift_last_update" in saved and saved["soc_drift_last_update"] is not None:
                self._soc_drift_last_update = dt_util.parse_datetime(
                    saved["soc_drift_last_update"]
                )
            if "soc_drift_last_soc_pct" in saved and saved["soc_drift_last_soc_pct"] is not None:
                self._soc_drift_last_soc_pct = float(saved["soc_drift_last_soc_pct"])
            if "soc_drift_engaged" in saved:
                self._soc_drift_engaged = bool(saved["soc_drift_engaged"])
            if "soc_drift_last_export_kwh_dc" in saved:
                self._soc_drift_last_export_kwh_dc = float(saved["soc_drift_last_export_kwh_dc"])
        except (KeyError, ValueError, TypeError):
            pass

    async def _record_sample(
        self,
        now,
        inputs,
        *,
        setpoint: float,
        state: str,
        weather_entry: dict | None = None,
        export_setpoint_w: float | None = None,
        export_kwh: float | None = None,
        reserve_kwh: float | None = None,
        surplus_kwh: float | None = None,
        house_load_w=_UNSET,
        persons_home: int | None = None,
    ) -> None:
        """Read the live physical sample and append one recorder row.

        Used by both the active path (state=plan.state.value) and the disabled
        path (state="disabled", setpoint 0).

        ``weather_entry`` is the hourly forecast dict for the current clock-hour
        (from coordinator.get_forecast_for_hour).  None → all 4 weather columns
        stored as NULL.

        C3 export signal columns: populated on export ticks; None on non-export ticks.
        ``export_setpoint_w`` — the positive inverter setpoint sent to engage_export.
        ``export_kwh``        — approximate kWh per tick (setpoint/1000 / ticks_per_hour).
        ``reserve_kwh``       — DC kWh the battery must retain (ride-out reserve).
        ``surplus_kwh``       — DC kWh available above the reserve (available to export).
        """
        pv_w = coordinator.read_float(self._hass, self._data[const.CONF_ENT_PV_POWER])
        batt_w = coordinator.read_float(self._hass, self._data[const.CONF_ENT_BATTERY_POWER])
        import_price = coordinator.read_float(self._hass, self._data.get(const.CONF_ENT_PRICE, ""))
        irradiance = coordinator.read_float(self._hass, self._data.get(const.CONF_ENT_IRRADIANCE, ""))
        temp_ent = self._data.get(const.CONF_ENT_TEMP)
        temp = (
            coordinator.read_attr(self._hass, temp_ent, "temperature")
            if temp_ent is not None
            else None
        )
        # House load: use the value threaded from the active tick if provided
        # (single compute, consistent with actuation); otherwise compute it here
        # (disabled path, which does not pass house_load_w).
        if house_load_w is _UNSET:
            house_load = self._compute_house_load_w(inputs)
        else:
            house_load = house_load_w
        # Read the live feed-in tariff from the configured export-price entity.
        # Fallback: None (stored as NULL) when entity is absent or empty string.
        # Rationale: recording NULL is safer than mirroring the import price, which
        # would over-credit export by the full energy-tax component of the import rate.
        # Post-hoc analysis of NULL rows simply skips export-revenue attribution.
        _rec_export_price_ent = self._data.get(const.CONF_ENT_EXPORT_PRICE, "")
        rec_export_price = (
            coordinator.read_float(self._hass, _rec_export_price_ent)
            if _rec_export_price_ent
            else None
        )
        row = {
            "ts": now.isoformat(),
            "hour": now.hour,
            "weekday": now.weekday(),
            "soc": inputs.soc,
            # Legacy 3-phase columns retired (the Anker X1 meter reports one signed
            # scalar, not per-phase import); schema unchanged, so these stay NULL.
            "p1_l1": None,
            "p1_l2": None,
            "p1_l3": None,
            "p1_w": inputs.meter_w,
            "state": state,
            "setpoint_w": setpoint,
            "pv_w": pv_w,
            "batt_w": batt_w,
            "import_price": import_price,
            # Real feed-in tariff from CONF_ENT_EXPORT_PRICE entity (v7 fix).
            # NULL when entity is unconfigured; never mirrors import price.
            "export_price": rec_export_price,
            "irradiance": irradiance,
            "temp": temp,
            # Weather-forecast columns (all None when forecast unavailable).
            "temp_forecast": weather_entry.get("temp_forecast") if weather_entry else None,
            "cloud_cover": weather_entry.get("cloud_cover") if weather_entry else None,
            "humidity": weather_entry.get("humidity") if weather_entry else None,
            "wind_speed": weather_entry.get("wind_speed") if weather_entry else None,
            # Ground-truth house load, computed per-tick by _compute_house_load_w
            # (pv + meter_w + batt − inverter_loss), which clamps to ≥ 0 itself.
            # Never NULL in practice from the two tick() call sites — cache
            # fallback (N2) covers a pv/batt sensor blip.
            "load_w": house_load,
            # v7 export-arbitrage signal columns.
            # Populated by the C3 export executor on export ticks; None otherwise.
            "export_setpoint_w": export_setpoint_w,
            "export_kwh": export_kwh,
            "reserve_kwh": reserve_kwh,
            "surplus_kwh": surplus_kwh,
            "persons_home": persons_home,
        }
        # Row dict built on the loop thread (HA state reads above must stay on-loop).
        # Only the blocking SQLite write moves off-loop, mirroring _write_decision_sync.
        await self._hass.async_add_executor_job(self._recorder.append, row)

    # ── tick() extraction helpers ─────────────────────────────────────────────
    # Shared read/persist logic extracted from the enabled and disabled tick()
    # branches.  Behaviour-identical: callers assign the return values to their
    # own local names (e.g. _shadow_export_price vs _export_price) so the
    # distinct naming in each branch is preserved.

    def _compute_house_load_w(self, inputs) -> float:
        """Live house load (W): pv + meter_w (signed net grid, + = import) +
        batt (+ = discharge, − = charge) − inverter_loss, clamped to ≥ 0 (house
        load cannot physically be negative; cross-read skew between the pv/
        meter/batt sensors on a given tick can otherwise yield a small negative
        value).

        inverter_loss reads 0.0 when its sensor is unavailable (it genuinely
        reads 0 while charging/idle and may drop out).  pv or batt unavailable
        → skip the compute for this tick entirely and fall back to the cached
        last-known value (N2) rather than publish a number built from a stale
        mixture of reads.  On a successful compute, refresh the cache so later
        unavailable ticks fall back to this fresher value.

        Also sets ``self._house_load_fresh`` (True on a live compute, False on
        a cache-fallback) so callers that must not act on stale data — the
        export gross-setpoint compensation — can tell the two apart.
        """
        pv_w = coordinator.read_float(self._hass, self._data[const.CONF_ENT_PV_POWER])
        batt_w = coordinator.read_float(self._hass, self._data[const.CONF_ENT_BATTERY_POWER])
        if pv_w is None or batt_w is None:
            self._house_load_fresh = False
            return self._last_house_load_w
        loss_w = coordinator.read_float(
            self._hass,
            self._data.get(
                const.CONF_ENT_INVERTER_LOSS,
                const.DEFAULT_ENTITIES[const.CONF_ENT_INVERTER_LOSS],
            ),
        )
        house_load_w = max(
            0.0, pv_w + inputs.meter_w + batt_w - (loss_w if loss_w is not None else 0.0)
        )
        self._last_house_load_w = house_load_w
        self._house_load_fresh = True
        return house_load_w

    def _read_forecast_bundle(self) -> tuple[list | None, list | None]:
        """Per-day PV watts arrays; warn when exactly one day is available."""
        today_watts = coordinator.read_pv_today_watts(self._hass, self._data)
        tomorrow_watts = coordinator.read_pv_tomorrow_watts(self._hass, self._data)
        if (today_watts is None) != (tomorrow_watts is None):
            _LOGGER.warning(
                "PV watts available for only one day (today=%s, tomorrow=%s); "
                "that day's PV will be absent from the plan",
                today_watts is not None,
                tomorrow_watts is not None,
            )
        return today_watts, tomorrow_watts

    def _resolve_export_price(self) -> tuple[float | None, bool]:
        """Live feed-in tariff + whether it points at the same entity as import.

        Static tariff mode bypasses the sensor path entirely: it returns the
        configured constant ``static_price_export`` (None when <= 0, i.e. no
        export credit) and never mirrors the import price.
        """
        if self.cfg.price_mode == const.PRICE_MODE_STATIC:
            px = self.cfg.static_price_export
            return (px if px > 0.0 else None), False
        export_ent = self._data.get(const.CONF_ENT_EXPORT_PRICE, "")
        import_ent = self._data.get(const.CONF_ENT_PRICE, "")
        price = coordinator.read_float(self._hass, export_ent) if export_ent else None
        matches = bool(export_ent and export_ent == import_ent)
        return price, matches

    def _resolve_slot_minutes(self, slots) -> int:
        """Per-refresh detected slot length, latched to the finest seen this UTC day.

        `slot_resolution` override hard-pins (no latch).  Stores the effective value
        on `self._detected_slot_minutes` for the diagnostic.  At 60-min detection is
        a stable 60 → no latch change → parity-safe.
        """
        detected = resolution.resolve_slot_minutes(slots, self.cfg.slot_resolution)
        if self.cfg.slot_resolution != const.SLOT_RESOLUTION_AUTO:
            self._detected_slot_minutes = detected
            return detected
        now_utc = dt_util.utcnow()
        effective, self._res_latch = resolution.latch_finest(detected, now_utc, self._res_latch)
        self._detected_slot_minutes = effective
        return effective

    async def _persist_decision_snapshot(self) -> None:
        """Persist self.last_decision to the decisions table (off-loop) if it has a ts."""
        if self.last_decision.get("ts"):
            await self._hass.async_add_executor_job(
                self._write_decision_sync, self.last_decision
            )

    async def _backfill_regret(self, now) -> None:
        """Run the daily-regret backfill on the first tick of a new local day / after restart."""
        today = dt_util.as_local(now).date().isoformat()
        if today != self._last_regret_day:
            await self._hass.async_add_executor_job(
                self._backfill_regret_sync, today, now.isoformat()
            )
        self._last_regret_day = today
