"""Pure builder for the forward plan horizon (display only)."""

from __future__ import annotations

from datetime import datetime, timedelta

from . import const
from .interp import MidpointLinear
from .models import Config, ForecastInterval, PriceSlot
from .parsers import build_pv_curve_from_watts, build_two_day_pv_curve
from .resolution import floor_to_slot, hour_floor


def build_display_intervals(
    slots: list[PriceSlot],
    now: datetime,
    pv_curve: list[tuple[datetime, float]],
    predictor,
    cur_temp: float | None,
    fallback_w: float,
    *,
    quantile: float = 0.5,
    temp_by_hour: dict[datetime, float | None] | None = None,
    slot_minutes: int = 60,
) -> list[ForecastInterval]:
    """One ForecastInterval per distinct price-slot at the slot_minutes grid, >= now's slot.

    Display-only. pv_w = a step-function lookup on pv_curve (0.0 when no curve point
    covers the slot, e.g. overnight) — see the D2 comment below;
    load_w = a midpoint-anchored linear interpolation (D3) of the per-HOUR
    predictor.predict(hour, h_temp, fallback_w, quantile=quantile) values, where
    h_temp is looked up from temp_by_hour (per-hour forecast, HOUR-floored) falling
    back to cur_temp.  predict is called once per hour, not once per slot.  At
    slot_minutes=60 the slot centre is the hour anchor, so this is byte-identical
    to the legacy per-slot call.  dt_h = slot_minutes / 60.0.
    Slots before floor_to_slot(now, slot_minutes) are omitted (left null in the horizon; the card
    clips them).  At slot_minutes=60 this reduces byte-identically to the legacy hourly build.
    """
    if not slots:
        return []
    pv_sorted = sorted(pv_curve, key=lambda p: p[0])
    pv_n = len(pv_sorted)
    now_h = floor_to_slot(now, slot_minutes)
    dt_h = slot_minutes / 60.0
    # Pass 1: the emitted slot grid (dedup + >= now filter), keeping each row's
    # ORIGINAL slot.start for the PV cursor below — flooring it would change the
    # D2 lookup for non-slot-aligned price starts.
    rows: list[tuple[datetime, datetime]] = []
    seen: set[datetime] = set()
    for slot in sorted(slots, key=lambda s: s.start):
        h = floor_to_slot(slot.start, slot_minutes)
        if h < now_h or h in seen:
            continue
        seen.add(h)
        rows.append((h, slot.start))
    if not rows:
        return []
    # D1: the temp forecast is per-hour — keep the temp lookup HOUR-floored even
    # though the PV/dedup grid is per-slot (else 3-of-4 quarters fall back to
    # cur_temp instead of the actual hourly-forecast temp).
    # D3: the load model is hour-bucketed too, so predict ONCE per hour (with
    # that hour's own temp) and read each slot from a midpoint-anchored linear
    # interpolation of those hourly values — the hourly value is the hour's
    # MEAN, anchored at the hour centre, and each slot reads at ITS own centre.
    # At slot_minutes=60 the slot centre IS the anchor, so this is an exact
    # identity. Anchors are only the hours that actually have emitted slots: no
    # predict() probe past the horizon, whose fallback would ramp the final
    # hour toward a value no row displays. Consequence: the last hour's late
    # quarters stay flat at that hour's value.
    load_points: list[tuple[datetime, float]] = []
    for hour in sorted({hour_floor(start) for _, start in rows}):
        h_temp = temp_by_hour.get(hour, cur_temp) if temp_by_hour else cur_temp
        load_points.append((hour, predictor.predict(hour, h_temp, fallback_w, quantile=quantile)))
    load_curve = MidpointLinear(load_points)
    half = timedelta(minutes=slot_minutes / 2)
    out: list[ForecastInterval] = []
    pv_idx = 0
    for h, slot_start in rows:
        # D2: pv_curve is a step function, not a per-hour sum — a curve point's
        # watts hold from its own timestamp until the NEXT point supersedes it (or
        # it goes stale after 1h with no successor, e.g. overnight). Walk a forward
        # cursor (rows and pv_curve are both processed start-ascending, so the
        # cursor only ever advances) to the last point at or before this slot's
        # start, then use its watts iff that point is < 1h old, else 0.0. This
        # reproduces the legacy hourly-fan behavior byte-identically for
        # HOUR-ALIGNED one-point-per-hour curves while giving dense sub-hourly
        # curves (from_watts output at the live slot width) each slot's own value
        # instead of the hour's SUM fanned across every quarter.
        # Caveat (accepted 2026-08-01 final review): non-hour-aligned hourly
        # curves (synth_pv_curve / arrays anchored at now/sunrise, degraded-data
        # fallback paths only) read one point LATER than the old hour-sum did —
        # a <=1-slot temporal shift, energy-conserved.
        # Precondition: pv_curve is expected to carry at most one point per
        # timestamp (all four parsers.py builders guarantee this). Points sharing
        # a timestamp are NOT summed here; the cursor keeps only the last one.
        while pv_idx + 1 < pv_n and pv_sorted[pv_idx + 1][0] <= slot_start:
            pv_idx += 1
        if pv_idx < pv_n and pv_sorted[pv_idx][0] <= slot_start and (
            slot_start - pv_sorted[pv_idx][0] < timedelta(hours=1)
        ):
            pv_w = pv_sorted[pv_idx][1]
        else:
            pv_w = 0.0
        load_w = load_curve.at(h + half)
        out.append(ForecastInterval(h, pv_w, load_w if load_w is not None else fallback_w, dt_h))
    return out


def build_plan_horizon(
    slots: list[PriceSlot],
    intervals: list[ForecastInterval],
    selected: list[datetime],
    soc: float,
    horizon_edge: datetime,
    cfg: Config,
    grid_request_by_hour: dict[datetime, float] | None = None,
    export_request_by_hour: dict[datetime, float] | None = None,
    reserve_by_hour: dict[datetime, float] | None = None,
    ceiling_by_hour: dict[datetime, float] | None = None,
    past_actuals_by_hour: dict[datetime, dict] | None = None,
    hedge_drain_by_hour: dict[datetime, float] | None = None,
    slot_minutes: int = 60,
    delivered_by_hour: dict[datetime, dict] | None = None,
    *,
    eta_curve=None,
    est_starts: frozenset | None = None,
    terminal_need_kwh: float = 0.0,
) -> list[dict]:
    """Join price/PV/load/charge-plan into an hourly horizon for visualization.

    Read-only and derived: never affects control.  Each hour splits battery
    charging into two coexisting AC components under the SHARED inverter rate
    cap (solar first, grid fills the remainder), with the GRID component
    additionally bounded by the grid connection's own rating
    (``grid_import_limit_w`` — mirrors ``regret._max_grid_dc``; solar is not
    grid import and keeps the full inverter rate):

        solar_charge_w = min(solar_surplus, max_charge_w, headroom_w)
        grid_charge_w  = max(0, min(grid_request, max_charge_w - solar_charge_w,
                                    grid_import_limit_w,
                                    headroom_w - solar_charge_w))   # grid hours only

    ``grid_request_by_hour`` maps an hour-start datetime to the requested grid
    AC watts (DP schedule).  When ``None``, each selected hour requests
    ``max_charge_w`` (heuristic "charge as hard as possible").  ``mode`` is
    derived exactly as before (grid if selected, else solar if pv>load, else
    idle) so ``planned_grid_hours`` semantics are unchanged.  ``charge_w`` is
    retained as ``solar_charge_w + grid_charge_w`` (DEPRECATED back-compat).

    ``export_request_by_hour`` maps an hour-start datetime to the planned AC
    export-to-grid watts (NET-EXPORT semantics: serves house first, exports
    remainder).  Export DRAINS the SoC simulation so the projected-SoC line
    reflects export hours correctly.

    ``delivered_by_hour`` maps an hour-start datetime to a partial-actuals record
    (``{"grid_charge_kwh": ...}``, same shape as ``past_actuals_by_hour``) for grid
    energy ALREADY DELIVERED in a slot that is still in progress.  In practice only
    the current slot ever has an entry.  Without it, an in-progress grid charge
    disappears from the card as it is delivered: ``grid_charge_w`` below is the
    energy still needed to reach ``ceiling_by_hour``, computed from the LIVE SoC, so
    it decays to 0 exactly as the charge completes (live 2026-07-29: a 2.5 kWh
    grid charge rendered as ``mode="grid", grid_charge_w=0``).  The delivered kWh is
    ADDED to the modelled remainder for display only — the two are disjoint (the
    modelled part is headroom-limited to what is left), so this reconstructs the
    slot total without double counting.  It deliberately does NOT feed ``soc_sim``:
    the live ``soc`` this simulation starts from already contains it.

    ``reserve_by_hour`` maps an hour-start datetime to the ride-out reserve in
    DC kWh (from ``energy.ride_out_reserve_kwh``).  Converted to a ``reserve_soc`` %
    on the SoC axis.  When ``None``, ``reserve_soc`` defaults to ``cfg.soc_floor``.

    The projected-SoC simulation (``soc_sim``) is clamped to the PHYSICAL range
    ``[const.FIRMWARE_SOC_FLOOR, cfg.soc_target]``, not ``[cfg.soc_floor, cfg.soc_target]``.
    ``cfg.soc_floor`` is a soft planning margin — nothing force-charges to hold it, so on
    a deficit night the real battery keeps sagging past it down to the firmware's hard
    discharge cutoff (``const.FIRMWARE_SOC_FLOOR``, 5%). Clamping the sim at ``cfg.soc_floor``
    would flat-line the display above where the battery actually settles (or, for
    ``cfg.soc_floor`` < 5, show an unreachable value below the firmware cutoff).
    ``reserve_soc`` (the ride-out-reserve display line) is unaffected and still derives
    from ``cfg.soc_floor`` / ``reserve_by_hour`` as before.

    ``eta_curve`` is an optional measured ``EfficiencyCurve`` (see
    ``efficiency.py``).  When ``None`` (default), the SoC sim uses the static
    ``cfg.eta_charge`` / round-trip-derived scalars exactly as before (parity-safe).
    When supplied, charge/self-discharge/export each look up a power-dependent
    eta from the curve instead.

    ``est_starts`` marks hour-floored slot starts as "estimated" rows (e.g. a
    tomorrow price tail appended beyond the forecast horizon by a later stage):
    those rows get ``"estimated": True``, ``mode="estimated"`` (takes precedence
    over grid/solar/idle), and a ``reserve_soc`` line showing the firmware floor
    plus ``terminal_need_kwh`` converted to SoC (clamped to ``cfg.soc_target``)
    instead of the usual ``reserve_by_hour`` / ``cfg.soc_floor`` value. All other
    rows get ``"estimated": False``. This flags rows only — appending the tail
    slots themselves is a separate change; est hours are absent from
    ``selected_set``/``exp_by_hour``/``delivered_by_hour``/``ceil_by_hour`` by
    construction, so charge/export stay 0 and the SoC walk + firmware-floor
    clamp run unmodified.

    Each slot also carries ``pv_kwh``, ``load_kwh``, ``solar_charge_kwh``,
    ``grid_charge_kwh`` and ``grid_export_kwh`` — the per-slot ENERGY in the
    DP's native unit, for planning/charting (e.g. the Lovelace energy card).
    For future (planned) slots these are ``watts * dt_h / 1000`` derived from
    the corresponding ``*_w`` field. For past slots they are the measured
    ``∫P dt`` energy sums passed through verbatim from ``past_actuals_by_hour``
    (``None`` when a cached actual predates these keys). The ``*_w`` fields
    are retained unchanged for back-compat (average power over the slot).
    """
    if not slots:
        return []
    # iv_by_hour/selected_set/req/exp/ceil are keyed on the SLOT grid, not the
    # hour: they come from the DP schedule (_dp_select_slots's own slot-floored
    # keys, see decision.py T9 notes) or from build_display_intervals (also
    # slot-floored — see D1 there).  Flooring their keys with hour_floor
    # collapsed 4 distinct 15-min entries onto one hour key (last-write-wins),
    # so a committed 4000 W quarter got silently overwritten by a later, unset
    # sibling quarter and mode="grid" painted onto all 4 (finding A2).
    # floor_to_slot(..., 60) == hour_floor, so this is byte-identical at the
    # legacy 60-min resolution.
    iv_by_hour = {floor_to_slot(iv.start, slot_minutes): iv for iv in intervals}
    selected_set = {floor_to_slot(s, slot_minutes) for s in selected}
    req_by_hour = {floor_to_slot(k, slot_minutes): v for k, v in (grid_request_by_hour or {}).items()}
    exp_by_hour = {floor_to_slot(k, slot_minutes): v for k, v in (export_request_by_hour or {}).items()}
    ceil_by_hour = {floor_to_slot(k, slot_minutes): v for k, v in (ceiling_by_hour or {}).items()}
    # rsv_by_hour/deliv_by_hour stay HOUR-keyed on purpose: their producers
    # (decision._build_reserve_by_hour, controller._get_current_delivered) hand
    # back ONE value per clock-hour by design (out of this fix's scope to make
    # slot-granular) — floor_to_slot on the lookup key would miss the dict for
    # 3-of-4 quarters and silently fall back to the default instead of the real
    # value. hour_floor at slot_minutes=60 IS floor_to_slot, so this is still
    # byte-identical there.
    #
    # hedge_by_hour is SLOT-keyed, unlike the two above, because its producer
    # (controller._apply_drift_hedge) emits a ONE-SHOT kWh debit parked on a
    # single clock-hour — `{trough_hour: hedge_kwh}` — not a per-hour rate.
    # The DP consumes it on slot-stride keys (decision.py's
    # `hedge_drain_by_hour.get(now_h + h * stride)`), so it lands on exactly one
    # slot; hour-keying it here re-applied the FULL debit to all four quarters
    # of that hour, sinking the published SoC curve ~4x the intended kWh while
    # the DP's own track (and the charge columns below) stayed correct.
    # "Missing" 3-of-4 quarters is the CORRECT behaviour for a one-shot debit.
    rsv_by_hour = {hour_floor(k): v for k, v in (reserve_by_hour or {}).items()}
    hedge_by_hour = {floor_to_slot(k, slot_minutes): v for k, v in (hedge_drain_by_hour or {}).items()}
    deliv_by_hour = {hour_floor(k): v for k, v in (delivered_by_hour or {}).items()}
    est_set = {hour_floor(s) for s in (est_starts or ())}
    cap_wh = cfg.capacity_kwh * 1000.0
    cap_kwh = cap_wh / 1000.0
    _slot_frac = slot_minutes / 60.0
    eta = cfg.eta_charge_safe()
    # NOTE: guard applied to the whole expression (not just eta_charge in the
    # divisor) — diverges from Config.eta_discharge_static() in the
    # eta_charge<=1e-9 degenerate case, so intentionally left un-unified
    # (byte-identical parity rule; see D4/D5 refactor report).
    eta_discharge = min(cfg.round_trip_eff / cfg.eta_charge, 1.0) if cfg.eta_charge > 1e-9 else 1.0
    soc_sim = soc
    out: list[dict] = []
    for slot in sorted(slots, key=lambda s: s.start):
        hour = hour_floor(slot.start)
        # Slot-grid key for the 5 genuinely slot-keyed lookups above (iv/selected/
        # req/exp/ceil); `hour` (above) stays the lookup key for past_actuals/
        # est_set/rsv/hedge/deliv, which are genuinely per-clock-hour. Equal to
        # `hour` at slot_minutes=60.
        slot_key = floor_to_slot(slot.start, slot_minutes)
        act = past_actuals_by_hour.get(hour) if past_actuals_by_hour else None
        if act is not None:
            # Past slot: emit recorded actuals verbatim and DO NOT advance soc_sim,
            # so the forward projection from the current SoC at now_h is unchanged.
            if rsv_by_hour:
                rsv_kwh = rsv_by_hour.get(hour, cfg.floor_kwh)
                reserve_soc = cfg.kwh_to_pct(rsv_kwh) if cfg.capacity_kwh > 0 else cfg.soc_floor
            else:
                reserve_soc = cfg.soc_floor
            solar_charge_w = act["solar_charge_w"]
            grid_charge_w = act["grid_charge_w"]
            # A3: aggregate_past_actuals bucketing is genuinely per-clock-hour (the
            # recorder samples are hour-keyed), so every slot row sharing this hour
            # looks up the SAME hour-total actual. At 15-min that stamped the FULL
            # hour's energy onto each of the 4 quarter rows (4x inflated column
            # totals when summed). Split it evenly across the hour's slot rows
            # instead — _slot_frac = slot_minutes/60 is 1.0 (no-op) at the legacy
            # 60-min resolution.
            _pv_kwh = act.get("pv_kwh")
            _load_kwh = act.get("load_kwh")
            _solar_charge_kwh = act.get("solar_charge_kwh")
            _grid_charge_kwh = act.get("grid_charge_kwh")
            _grid_export_kwh = act.get("grid_export_kwh")
            out.append(
                {
                    "start": slot.start.isoformat(),
                    "price": slot.price,
                    "pv_w": act["pv_w"],
                    "load_w": act["load_w"],
                    "solar_charge_w": solar_charge_w,
                    "grid_charge_w": grid_charge_w,
                    "mode": "actual",
                    "estimated": False,  # est_starts hours are future-only by construction
                    "soc": act["soc"],
                    "charge_w": round(solar_charge_w + grid_charge_w, 1),
                    "is_past_horizon": slot.start >= horizon_edge,
                    "grid_export_w": act["grid_export_w"],
                    "self_discharge_w": 0.0,
                    "reserve_soc": round(reserve_soc, 1),
                    "pv_kwh": _pv_kwh * _slot_frac if _pv_kwh is not None else None,
                    "load_kwh": _load_kwh * _slot_frac if _load_kwh is not None else None,
                    "solar_charge_kwh": _solar_charge_kwh * _slot_frac if _solar_charge_kwh is not None else None,
                    "grid_charge_kwh": _grid_charge_kwh * _slot_frac if _grid_charge_kwh is not None else None,
                    "grid_export_kwh": _grid_export_kwh * _slot_frac if _grid_export_kwh is not None else None,
                }
            )
            continue
        iv = iv_by_hour.get(slot_key)
        pv_w = iv.pv_w if iv is not None else None
        load_w = iv.load_w if iv is not None else None
        dt_h = iv.dt_h if iv is not None else slot_minutes / 60.0
        is_grid = slot_key in selected_set
        is_est = hour in est_set
        solar_surplus = max(0.0, iv.pv_w - iv.load_w) if iv is not None else 0.0
        if cap_wh > 0:
            headroom_w = max(0.0, (cfg.soc_target - soc_sim) / 100.0 * cap_wh / (eta * dt_h))
        else:
            headroom_w = 0.0
        # Solar fills first (free); both bars share the rate + headroom budget.
        solar_charge_w = min(solar_surplus, cfg.max_charge_w, headroom_w)
        grid_charge_w = 0.0
        self_discharge_w = 0.0
        if is_grid:
            grid_request_w = req_by_hour.get(slot_key, cfg.max_charge_w)
            # GRID stops at the solar-reservation ceiling for this hour (leave room
            # for forecast solar); SOLAR may still fill to soc_target above it.
            # When no ceiling is supplied, fall back to soc_target (prior behaviour).
            ceil_soc = ceil_by_hour.get(slot_key, cfg.soc_target)
            if cap_wh > 0:
                ceil_headroom_w = max(0.0, (ceil_soc - soc_sim) / 100.0 * cap_wh / (eta * dt_h))
            else:
                ceil_headroom_w = 0.0
            # Grid connection cap (grid_import_limit_w) bounds only the GRID portion —
            # solar_charge_w above already got the full inverter rate and is untouched.
            # Mirrors regret._max_grid_dc so the displayed plan matches the DP decision.
            grid_charge_w = max(
                0.0,
                min(
                    grid_request_w,
                    cfg.max_charge_w - solar_charge_w,
                    cfg.grid_import_limit_w,
                    ceil_headroom_w - solar_charge_w,
                ),
            )
        elif iv is not None and iv.load_w > iv.pv_w:
            self_discharge_w = min(iv.load_w - iv.pv_w, cfg.max_charge_w)
        # Export to grid (NET-EXPORT: positive setpoint = export after serving house).
        grid_export_w = exp_by_hour.get(slot_key, 0.0)
        total_w = solar_charge_w + grid_charge_w
        if cap_wh > 0:
            # eta_curve (measured, power-dependent) overrides the static scalars
            # when supplied; eta_curve=None keeps this byte-identical to before.
            _eta_c = cfg.eta_charge if eta_curve is None else eta_curve.eta_charge(total_w)
            soc_sim += total_w * _eta_c * dt_h / cap_wh * 100.0
            # max_charge_w is used as an approximate discharge cap by design (no separate max_discharge_w config).
            _eta_d = eta_discharge if eta_curve is None else eta_curve.eta_discharge(self_discharge_w)
            soc_sim -= (self_discharge_w / max(_eta_d, 1e-6)) * dt_h / cap_wh * 100.0
            if self_discharge_w > 0:
                # Constant inverter-standby DC drain (cfg.idle_drain_w, ~130 W live) paid
                # whenever the battery passively discharges to cover a net AC deficit.
                # DC-side term: NOT divided by eta_discharge. Not paid on charge/export
                # slots (self_discharge_w is 0 there). idle_drain_w=0.0 default -> no-op.
                soc_sim -= cfg.kwh_to_pct(cfg.idle_drain_w * dt_h / 1000.0)
            # Export drains the SoC simulation (must happen after charge credits).
            _eta_de = eta_discharge if eta_curve is None else eta_curve.eta_discharge(grid_export_w)
            soc_sim -= (grid_export_w / max(_eta_de, 1e-6)) * dt_h / cap_wh * 100.0
        # SoC drift-hedge debit (display): mirror the DP's forward SoC sag. Past slots
        # `continue` above (excluded). Empty/None → no change (parity-safe).
        if hedge_by_hour and cfg.capacity_kwh > 0:
            soc_sim -= cfg.kwh_to_pct(hedge_by_hour.get(slot_key, 0.0))
        soc_sim = min(max(soc_sim, const.FIRMWARE_SOC_FLOOR), cfg.soc_target)
        # reserve_soc: ride-out reserve as % on the SoC axis, or cfg.soc_floor as default.
        if rsv_by_hour:
            rsv_kwh = rsv_by_hour.get(hour, cfg.floor_kwh)
            reserve_soc = cfg.kwh_to_pct(rsv_kwh) if cfg.capacity_kwh > 0 else cfg.soc_floor
        else:
            reserve_soc = cfg.soc_floor
        if is_est:
            # Estimated tail rows show the firmware floor plus the still-needed
            # terminal energy converted to SoC, instead of the usual reserve line.
            need_pct = terminal_need_kwh / cap_kwh * 100.0 if cap_kwh > 0 else 0.0
            reserve_soc = min(const.FIRMWARE_SOC_FLOOR + need_pct, cfg.soc_target)
        if is_est:
            mode = "estimated"
        elif is_grid:
            mode = "grid"
        elif grid_export_w > 0:
            mode = "export"
        elif iv is not None and iv.pv_w > iv.load_w:
            mode = "solar"
        else:
            mode = "idle"
        # Display-only: fold energy already delivered in this (in-progress) slot
        # back in, so an active grid charge stays on the card instead of decaying
        # to 0 as the live SoC eats the modelled headroom. Applied AFTER soc_sim
        # (which must keep advancing on the modelled remainder alone — the live
        # SoC it started from already contains the delivered energy).
        grid_charge_w_disp = grid_charge_w
        if deliv_by_hour and dt_h > 0:
            _deliv_kwh = (deliv_by_hour.get(hour) or {}).get("grid_charge_kwh")
            if _deliv_kwh:
                grid_charge_w_disp += float(_deliv_kwh) * 1000.0 / dt_h
        out.append(
            {
                "start": slot.start.isoformat(),
                "price": slot.price,
                "pv_w": pv_w,
                "load_w": load_w,
                "solar_charge_w": round(solar_charge_w, 1),
                "grid_charge_w": round(grid_charge_w_disp, 1),
                "mode": mode,
                "estimated": is_est,
                "soc": round(soc_sim, 1),
                "charge_w": round(total_w, 1),
                "is_past_horizon": slot.start >= horizon_edge,
                "grid_export_w": round(grid_export_w, 1),
                "self_discharge_w": round(self_discharge_w, 1),
                "reserve_soc": round(reserve_soc, 1),
                "pv_kwh": round(pv_w * dt_h / 1000.0, 3) if pv_w is not None else None,
                "load_kwh": round(load_w * dt_h / 1000.0, 3) if load_w is not None else None,
                "solar_charge_kwh": round(solar_charge_w * dt_h / 1000.0, 3),
                "grid_charge_kwh": round(grid_charge_w_disp * dt_h / 1000.0, 3),
                "grid_export_kwh": round(grid_export_w * dt_h / 1000.0, 3),
            }
        )
    return out


def build_display_horizon(
    slots: list[PriceSlot],
    now: datetime,
    today_arrays: list[tuple[float, datetime | None]] | None,
    tomorrow_arrays: list[tuple[float, datetime | None]] | None,
    sun_times: tuple[datetime, datetime, datetime] | None,
    predictor,
    cur_temp: float | None,
    fallback_w: float,
    soc: float,
    selected: list[datetime],
    horizon_edge: datetime,
    cfg: Config,
    grid_request_by_hour: dict[datetime, float] | None = None,
    export_request_by_hour: dict[datetime, float] | None = None,
    reserve_by_hour: dict[datetime, float] | None = None,
    ceiling_by_hour: dict[datetime, float] | None = None,
    today_watts: list[list[tuple[datetime, float]]] | None = None,
    tomorrow_watts: list[list[tuple[datetime, float]]] | None = None,
    past_actuals_by_hour: dict[datetime, dict] | None = None,
    hedge_drain_by_hour: dict[datetime, float] | None = None,
    temp_by_hour: dict[datetime, float | None] | None = None,
    delivered_by_hour: dict[datetime, dict] | None = None,
    slot_minutes: int = 60,
    *,
    eta_curve=None,
    est_slots: list[PriceSlot] | None = None,
    terminal_need_kwh: float = 0.0,
) -> list[dict]:
    """Two-day self-consumption display horizon (PV + load + discharge-aware SoC).

    Returns [] when sun_times is None. Shared by the enabled path (real
    selected/horizon_edge) and the disabled path (selected=[], horizon_edge=now).

    Args:
        today_arrays: Per-array [(kwh, peak_dt)] for today's remaining PV, or None to skip.
            Used as a fallback when today_watts is not provided.
        tomorrow_arrays: Per-array [(kwh, peak_dt)] for tomorrow's PV, or None to skip.
            Used as a fallback when tomorrow_watts is not provided.
        today_watts: Per-source lists of sub-hourly (datetime_utc, watts) samples for
            today, returned by coordinator.read_pv_today_watts.  When provided, takes
            precedence over today_arrays for curve building (each source is resampled
            to the hourly grid independently, then summed).
        tomorrow_watts: Per-source lists of sub-hourly (datetime_utc, watts) samples
            for tomorrow.
        export_request_by_hour: Per-hour planned export-to-grid watts (NET-EXPORT).
            Export drains the SoC simulation so the projected-SoC line reflects
            export hours correctly.  Mirrors build_plan_horizon semantics.
        reserve_by_hour: Per-hour ride-out reserve in DC kWh.  Converted to a
            ``reserve_soc`` % on the SoC axis.  When None, ``reserve_soc``
            defaults to ``cfg.soc_floor``.  Mirrors build_plan_horizon semantics.
        eta_curve: Optional measured ``EfficiencyCurve``.  ``None`` (default)
            preserves the static-scalar parity path.  Passed straight through
            to ``build_plan_horizon``.
        temp_by_hour: Per-hour forecast temperature (hour-start UTC datetime ->
            °C), passed straight through to ``build_display_intervals``.  Hours
            absent from the map (or when the map itself is ``None``) fall back
            to ``cur_temp`` — same semantics as ``build_display_intervals``.
        delivered_by_hour: Grid energy already delivered in an in-progress slot
            (in practice only the current one), passed straight through to
            ``build_plan_horizon`` — see its docstring for the contract.
        est_slots: Optional estimated-tomorrow tail ``PriceSlot`` list (e.g. from
            ``pricing_store.build_estimated_slots``), appended to ``slots`` BEFORE
            building intervals — so pv/load for the tail hours come from this same
            two-day PV curve + predictor. Their hour-floored starts become
            ``est_starts`` for ``build_plan_horizon`` (flags those rows
            ``estimated``). ``None``/empty (default) is a no-op — byte-identical.
        terminal_need_kwh: Forwarded straight through to ``build_plan_horizon``'s
            ``terminal_need_kwh`` (only meaningful together with ``est_slots``).
        slot_minutes: The live/resolved slot width in minutes (15/30/60), passed
            straight through to ``build_display_intervals`` and
            ``build_plan_horizon``.  Without this, both callees fell back to
            their own 60-min default regardless of the caller's actual
            resolution, so every row got ``dt_h=1.0`` at 15-min — a 4x energy-
            integration inflation (finding A1).  Default 60 is byte-identical
            to the pre-fix behaviour.
    """
    if sun_times is None:
        return []
    today_sunset, tomorrow_sunrise, tomorrow_sunset = sun_times
    if today_watts is not None or tomorrow_watts is not None:
        # Preferred path: real sub-hourly watts -> correct midday bell.  step_h
        # MUST be the live slot width: without it this built the curve at the
        # 1.0h default and build_display_intervals then fanned one hourly mean
        # across every quarter, so the card drew a coarser staircase than the
        # curve the DP was actually optimizing on (decision.py passes dt_h).
        curve = build_pv_curve_from_watts(today_watts, tomorrow_watts, now, step_h=slot_minutes / 60.0)
    else:
        # Fallback: synthetic quarter-sine from daily kWh totals (tests + degraded data).
        # Same slot-width rule as above, else the staircase returns whenever the
        # watts source drops out.
        curve = build_two_day_pv_curve(
            today_arrays,
            tomorrow_arrays,
            now,
            today_sunset,
            tomorrow_sunrise,
            tomorrow_sunset,
            step_h=slot_minutes / 60.0,
        )
    # Estimated tomorrow tail: appended to the price-slot list BEFORE building
    # intervals, so pv/load for those hours are derived from this same two-day
    # PV curve + predictor (not left None/0). `est_slots=None`/`[]` is a no-op —
    # `all_slots is slots` and `est_starts` stays `None`, byte-identical.
    all_slots = slots + est_slots if est_slots else slots
    est_starts = frozenset(hour_floor(s.start) for s in est_slots) if est_slots else None
    ivals = build_display_intervals(
        all_slots,
        now,
        curve,
        predictor,
        cur_temp,
        fallback_w,
        temp_by_hour=temp_by_hour,
        slot_minutes=slot_minutes,
    )
    return build_plan_horizon(
        all_slots,
        ivals,
        selected,
        soc,
        horizon_edge,
        cfg,
        grid_request_by_hour=grid_request_by_hour,
        export_request_by_hour=export_request_by_hour,
        reserve_by_hour=reserve_by_hour,
        ceiling_by_hour=ceiling_by_hour,
        past_actuals_by_hour=past_actuals_by_hour,
        hedge_drain_by_hour=hedge_drain_by_hour,
        delivered_by_hour=delivered_by_hour,
        slot_minutes=slot_minutes,
        eta_curve=eta_curve,
        est_starts=est_starts,
        terminal_need_kwh=terminal_need_kwh,
    )
