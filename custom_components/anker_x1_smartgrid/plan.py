"""Pure builder for the forward plan horizon (display only)."""

from __future__ import annotations

from datetime import datetime

from . import const
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

    Display-only. pv_w = the pv_curve value for that slot (0.0 when no curve point,
    e.g. overnight); load_w = predictor.predict(slot, h_temp, fallback_w, quantile=quantile)
    where h_temp is looked up from temp_by_hour (per-hour forecast, HOUR-floored — the temp
    forecast is intrinsically hourly) falling back to cur_temp.  dt_h = slot_minutes / 60.0.
    Slots before floor_to_slot(now, slot_minutes) are omitted (left null in the horizon; the card
    clips them).  At slot_minutes=60 this reduces byte-identically to the legacy hourly build.
    """
    if not slots:
        return []
    pv_by_slot: dict[datetime, float] = {}
    for start, watts in pv_curve:
        h = floor_to_slot(start, slot_minutes)
        pv_by_slot[h] = pv_by_slot.get(h, 0.0) + watts
    now_h = floor_to_slot(now, slot_minutes)
    dt_h = slot_minutes / 60.0
    out: list[ForecastInterval] = []
    seen: set[datetime] = set()
    for slot in sorted(slots, key=lambda s: s.start):
        h = floor_to_slot(slot.start, slot_minutes)
        if h < now_h or h in seen:
            continue
        seen.add(h)
        # D1: temp forecast is per-hour — keep the temp lookup HOUR-floored even
        # though the PV/dedup grid is per-slot (else 3-of-4 quarters fall back to
        # cur_temp instead of the actual hourly-forecast temp).
        h_temp = temp_by_hour.get(hour_floor(slot.start), cur_temp) if temp_by_hour else cur_temp
        out.append(
            ForecastInterval(
                h,
                pv_by_slot.get(h, 0.0),
                predictor.predict(h, h_temp, fallback_w, quantile=quantile),
                dt_h,
            )
        )
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
    *,
    eta_curve=None,
) -> list[dict]:
    """Join price/PV/load/charge-plan into an hourly horizon for visualization.

    Read-only and derived: never affects control.  Each hour splits battery
    charging into two coexisting AC components under the SHARED inverter rate
    cap (solar first, grid fills the remainder):

        solar_charge_w = min(solar_surplus, max_charge_w, headroom_w)
        grid_charge_w  = max(0, min(grid_request, max_charge_w - solar_charge_w,
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
    iv_by_hour = {hour_floor(iv.start): iv for iv in intervals}
    selected_set = {hour_floor(s) for s in selected}
    req_by_hour = {hour_floor(k): v for k, v in (grid_request_by_hour or {}).items()}
    exp_by_hour = {hour_floor(k): v for k, v in (export_request_by_hour or {}).items()}
    rsv_by_hour = {hour_floor(k): v for k, v in (reserve_by_hour or {}).items()}
    ceil_by_hour = {hour_floor(k): v for k, v in (ceiling_by_hour or {}).items()}
    hedge_by_hour = {hour_floor(k): v for k, v in (hedge_drain_by_hour or {}).items()}
    cap_wh = cfg.capacity_kwh * 1000.0
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
            out.append(
                {
                    "start": slot.start.isoformat(),
                    "price": slot.price,
                    "pv_w": act["pv_w"],
                    "load_w": act["load_w"],
                    "solar_charge_w": solar_charge_w,
                    "grid_charge_w": grid_charge_w,
                    "mode": "actual",
                    "soc": act["soc"],
                    "charge_w": round(solar_charge_w + grid_charge_w, 1),
                    "is_past_horizon": slot.start >= horizon_edge,
                    "grid_export_w": act["grid_export_w"],
                    "self_discharge_w": 0.0,
                    "reserve_soc": round(reserve_soc, 1),
                    "pv_kwh": act.get("pv_kwh"),
                    "load_kwh": act.get("load_kwh"),
                    "solar_charge_kwh": act.get("solar_charge_kwh"),
                    "grid_charge_kwh": act.get("grid_charge_kwh"),
                    "grid_export_kwh": act.get("grid_export_kwh"),
                }
            )
            continue
        iv = iv_by_hour.get(hour)
        pv_w = iv.pv_w if iv is not None else None
        load_w = iv.load_w if iv is not None else None
        dt_h = iv.dt_h if iv is not None else slot_minutes / 60.0
        is_grid = hour in selected_set
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
            grid_request_w = req_by_hour.get(hour, cfg.max_charge_w)
            # GRID stops at the solar-reservation ceiling for this hour (leave room
            # for forecast solar); SOLAR may still fill to soc_target above it.
            # When no ceiling is supplied, fall back to soc_target (prior behaviour).
            ceil_soc = ceil_by_hour.get(hour, cfg.soc_target)
            if cap_wh > 0:
                ceil_headroom_w = max(0.0, (ceil_soc - soc_sim) / 100.0 * cap_wh / (eta * dt_h))
            else:
                ceil_headroom_w = 0.0
            grid_charge_w = max(
                0.0,
                min(grid_request_w, cfg.max_charge_w - solar_charge_w, ceil_headroom_w - solar_charge_w),
            )
        elif iv is not None and iv.load_w > iv.pv_w:
            self_discharge_w = min(iv.load_w - iv.pv_w, cfg.max_charge_w)
        # Export to grid (NET-EXPORT: positive setpoint = export after serving house).
        grid_export_w = exp_by_hour.get(hour, 0.0)
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
            soc_sim -= cfg.kwh_to_pct(hedge_by_hour.get(hour, 0.0))
        soc_sim = min(max(soc_sim, const.FIRMWARE_SOC_FLOOR), cfg.soc_target)
        # reserve_soc: ride-out reserve as % on the SoC axis, or cfg.soc_floor as default.
        if rsv_by_hour:
            rsv_kwh = rsv_by_hour.get(hour, cfg.floor_kwh)
            reserve_soc = cfg.kwh_to_pct(rsv_kwh) if cfg.capacity_kwh > 0 else cfg.soc_floor
        else:
            reserve_soc = cfg.soc_floor
        if is_grid:
            mode = "grid"
        elif iv is not None and iv.pv_w > iv.load_w:
            mode = "solar"
        else:
            mode = "idle"
        out.append(
            {
                "start": slot.start.isoformat(),
                "price": slot.price,
                "pv_w": pv_w,
                "load_w": load_w,
                "solar_charge_w": round(solar_charge_w, 1),
                "grid_charge_w": round(grid_charge_w, 1),
                "mode": mode,
                "soc": round(soc_sim, 1),
                "charge_w": round(total_w, 1),
                "is_past_horizon": slot.start >= horizon_edge,
                "grid_export_w": round(grid_export_w, 1),
                "self_discharge_w": round(self_discharge_w, 1),
                "reserve_soc": round(reserve_soc, 1),
                "pv_kwh": round(pv_w * dt_h / 1000.0, 3) if pv_w is not None else None,
                "load_kwh": round(load_w * dt_h / 1000.0, 3) if load_w is not None else None,
                "solar_charge_kwh": round(solar_charge_w * dt_h / 1000.0, 3),
                "grid_charge_kwh": round(grid_charge_w * dt_h / 1000.0, 3),
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
    *,
    eta_curve=None,
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
    """
    if sun_times is None:
        return []
    today_sunset, tomorrow_sunrise, tomorrow_sunset = sun_times
    if today_watts is not None or tomorrow_watts is not None:
        # Preferred path: real per-15-min Open-Meteo watts → correct midday bell.
        curve = build_pv_curve_from_watts(today_watts, tomorrow_watts, now)
    else:
        # Fallback: synthetic quarter-sine from daily kWh totals (tests + degraded data).
        curve = build_two_day_pv_curve(
            today_arrays,
            tomorrow_arrays,
            now,
            today_sunset,
            tomorrow_sunrise,
            tomorrow_sunset,
            step_h=1.0,
        )
    ivals = build_display_intervals(
        slots,
        now,
        curve,
        predictor,
        cur_temp,
        fallback_w,
        temp_by_hour=temp_by_hour,
    )
    return build_plan_horizon(
        slots,
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
        eta_curve=eta_curve,
    )
