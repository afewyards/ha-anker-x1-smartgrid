#!/usr/bin/env python3
"""Offline DP replay harness — Task 1 of the 2026-08-01 terminal-piecewise-credit wave.

Rebuilds ``decision.compute_decision``'s pre-DP wiring (per-hour ride-out
``reserve_by_hour``, the trough ``is_cheap`` map, ``water_value`` /
``water_value_hi`` / ``overnight_need_kwh``) directly from a plan-sensor fixture
+ an options fixture, then calls ``decision._dp_select_slots`` — monkeypatching
``optimize.optimize_grid`` and ``optimize.select_end_state`` to capture exactly
what reaches the DP core — to answer the open verification question in
``docs/superpowers/specs/2026-08-01-terminal-piecewise-credit-design.md``:

    The fixture's export ends at 5.0 % SoC, below the displayed reserve line
    (11.9 %) and the 10 % soft floor.  Is the DP-internal floor/reserve the
    firmware floor (1.0 kWh on a 20 kWh pack) with a collapsed reserve (then
    5.0 % is internally consistent), does the DP violate its own
    ``max(floor, reserve)`` export floor, or is the *displayed* SoC hedge-debited
    below a compliant internal trajectory?

Usage
-----
    .venv/bin/python scripts/replay_dp.py \\
        --plan tests/fixtures/plan-sensor-2026-08-01-morning.json \\
        --options tests/fixtures/options-2026-08-01.json

Optional overrides: ``--now ISO``, ``--soc PCT``, ``--capacity-kwh KWH``,
``--max-charge-w W``, ``--max-export-w W`` (see ``_ALWAYS_DERIVED_DEFAULTS``
below for why these three have no fixture value to read).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from custom_components.anker_x1_smartgrid import const
from custom_components.anker_x1_smartgrid import decision
from custom_components.anker_x1_smartgrid import resolution, scheduler
from custom_components.anker_x1_smartgrid.efficiency import BinStat, EfficiencyCurve
from custom_components.anker_x1_smartgrid.models import Config, ForecastInterval, PlantInputs, PriceSlot

optimize_mod = decision.optimize_mod  # the exact module object _dp_select_slots calls into

# ``capacity_kwh`` / ``max_charge_w`` / ``max_export_w`` are ALWAYS-DERIVED from the
# Anker device at config-entry setup (config_flow.py:233, controller.py:687) — they
# never appear in a persisted options dict, hence absent from options-2026-08-01.json.
# Defaults below mirror the live lab pack (memory `battery-20kwh-startup-race`: 4
# modules / 20 kWh) and the rates implied by the fixture's own observed
# grid_charge_w/grid_export_w peaks (9.3 kW / 11.8 kW, the latter pinned near the
# configured grid_export_limit_w=12000 in options-2026-08-01.json).
CAPACITY_KWH_DEFAULT = 20.0
MAX_CHARGE_W_DEFAULT = 12000.0
MAX_EXPORT_W_DEFAULT = 12000.0


def _load_fixtures(plan_path: Path, options_path: Path) -> tuple[dict, dict, dict]:
    with open(plan_path) as f:
        plan_root = json.load(f)
    with open(options_path) as f:
        options = json.load(f)
    attrs = plan_root.get("attributes", plan_root)
    return plan_root, attrs, options


def _build_eta_curve(curve_dict: dict) -> EfficiencyCurve:
    """Reconstruct EfficiencyCurve from the plan sensor's ``efficiency_curve`` attr.

    Mirrors ``tests/test_overnight_terminal_replay.py::_build_eta_curve`` — this
    is the exact round-trip of ``EfficiencyCurve.as_attributes()`` (controller.py
    publishes it verbatim onto the plan sensor).
    """

    def _bins(raw: list[dict], direction: str) -> list[BinStat]:
        return [
            BinStat(
                lo_w=b["lo_w"],
                hi_w=float("inf") if b["hi_w"] is None else b["hi_w"],
                direction=direction,
                eta=b["eta"],
                measured=b.get("measured"),
                n_runs=b.get("n_runs", 0),
                dc_kwh=b.get("dc_kwh", 0.0),
                confident=b.get("confident", False),
                fallback_reason=b.get("fallback_reason", ""),
            )
            for b in raw
        ]

    charge_bins = _bins(curve_dict["charge"], "charge")
    discharge_bins = _bins(curve_dict["discharge"], "discharge")
    fc = charge_bins[0].eta if charge_bins else 0.92
    fd = discharge_bins[0].eta if discharge_bins else 0.92
    return EfficiencyCurve(charge_bins, discharge_bins, fc, fd)


def _split_horizon(horizon: list[dict]) -> tuple[list[PriceSlot], dict, dict]:
    """Split fixture horizon rows into (real PriceSlots, estimated-tail price
    map, row-by-start lookup).

    Rows with ``estimated: true`` are the ``pricing_store.build_estimated_slots``
    display tail — display-only, NEVER fed into the DP's own ``slots`` (see
    ``pricing_store.build_estimated_slots`` docstring + the DP-isolation pin
    ``tests/test_decision_overnight_terminal.py::test_dp_never_sees_estimated_slots``).
    Their ``price`` values are, however, exactly the ``est_price_by_hour`` the
    live tick fed to ``overnight_terminal_params`` — reused here instead of
    requiring a raw ``estimated_tomorrow`` 24-entry array (not present in either
    fixture).
    """
    real_slots: list[PriceSlot] = []
    est_price_by_hour: dict[datetime, float] = {}
    row_by_start: dict[datetime, dict] = {}
    for row in horizon:
        start = datetime.fromisoformat(row["start"])
        row_by_start[start] = row
        price = row.get("price")
        if price is None:
            continue
        if row.get("estimated"):
            est_price_by_hour[start] = price
        else:
            real_slots.append(PriceSlot(start=start, price=price))
    real_slots.sort(key=lambda s: s.start)
    return real_slots, est_price_by_hour, row_by_start


def _prev_row_soc(row_by_start: dict[datetime, dict], now_h: datetime) -> float:
    """SoC of the horizon row immediately BEFORE ``now_h``.

    Horizon row ``soc`` is end-of-slot on UTC labels (row X's soc == SoC at the
    START of row X+1), so the row before ``now_h`` carries the SoC AT ``now_h``.
    """
    candidates = [
        (start, row["soc"]) for start, row in row_by_start.items() if start < now_h and row.get("soc") is not None
    ]
    if not candidates:
        raise ValueError(f"No horizon row before {now_h.isoformat()} carries a soc value")
    return max(candidates, key=lambda c: c[0])[1]


def _build_intervals(
    row_by_start: dict[datetime, dict],
    now_h: datetime,
    horizon_edge: datetime,
    fallback_load_w: float,
) -> list[ForecastInterval]:
    """Hourly ForecastIntervals over ``[now_h, horizon_edge)`` straight from the
    fixture's own load_w/pv_w — bypassing compute_decision's predictor/PV-curve
    synthesis entirely, since the fixture already carries the exact forecast the
    live tick used (mirrors ``tests/test_overnight_terminal_replay.py``'s
    ``_build_slots_and_intervals``, extended to substitute a fallback for the
    ``now`` row's occasional null load_w/pv_w — the in-progress hour is
    sometimes un-refreshed at capture time).
    """
    intervals: list[ForecastInterval] = []
    t = now_h
    while t < horizon_edge:
        row = row_by_start.get(t)
        load_w = row.get("load_w") if row else None
        pv_w = row.get("pv_w") if row else None
        intervals.append(
            ForecastInterval(
                start=t,
                pv_w=pv_w if pv_w is not None else 0.0,
                load_w=load_w if load_w is not None else fallback_load_w,
                dt_h=1.0,
            )
        )
        t += timedelta(hours=1)
    return intervals


def replay(
    plan_path: Path,
    options_path: Path,
    *,
    now_override: str | None,
    soc_override: float | None,
    capacity_kwh: float,
    max_charge_w: float,
    max_export_w: float,
) -> dict:
    plan_root, attrs, options_raw = _load_fixtures(plan_path, options_path)
    options = {
        **options_raw,
        "capacity_kwh": capacity_kwh,
        "max_charge_w": max_charge_w,
        "max_export_w": max_export_w,
    }
    cfg = Config.from_dict(options)

    horizon = attrs["horizon"]
    real_slots, est_price_by_hour, row_by_start = _split_horizon(horizon)

    if now_override:
        now = datetime.fromisoformat(now_override)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
    else:
        raw_last_updated = plan_root.get("last_updated") or plan_root.get("last_changed")
        now = datetime.fromisoformat(raw_last_updated).replace(minute=0, second=0, microsecond=0)
    now_h = resolution.hour_floor(now)

    soc_start = soc_override if soc_override is not None else _prev_row_soc(row_by_start, now_h)

    last_slot_start = max(s.start for s in real_slots)
    horizon_edge = resolution.hour_floor(last_slot_start) + timedelta(hours=1)

    slot_minutes = resolution.resolve_slot_minutes(real_slots, cfg.slot_resolution)
    dt_h = slot_minutes / 60.0

    eta_curve = _build_eta_curve(attrs["efficiency_curve"]) if attrs.get("efficiency_curve") else None

    intervals = _build_intervals(row_by_start, now_h, horizon_edge, const.DEFAULT_FALLBACK_LOAD_W)
    # Degraded-data fallback intervals_reserve (mirrors compute_decision when
    # sun_times is unavailable — no PV-forecast/sun-times fixture exists for this
    # replay).  _build_reserve_by_hour synthesizes its OWN per-hour night
    # extension internally (guarded on `_has_solar`), and `is_cheap` depends only
    # on `real_slots` — so this simplification does not change the reserve
    # values at the hours under investigation (17-20 UTC); see task-1-report.md.
    intervals_reserve = list(intervals)

    remaining_prices = [s.price for s in real_slots if s.start >= now_h]
    water_value = optimize_mod.compute_water_value(min(remaining_prices), cfg)

    is_cheap = (
        decision._build_is_cheap_by_hour(real_slots, cfg, slot_minutes)
        if cfg.reserve_anchor == const.RESERVE_ANCHOR_TROUGH
        else None
    )

    reserve_by_hour = decision._build_reserve_by_hour(
        now,
        real_slots,
        intervals_reserve,
        cfg,
        is_cheap=is_cheap,
        slot_minutes=slot_minutes,
        eta_curve=eta_curve,
    )
    # _apply_price_prior is gated OFF under the trough anchor (decision.py:1059);
    # options-2026-08-01.json sets reserve_anchor="trough" so it never fires live either.

    floor_kwh = cfg.floor_kwh
    slot_now_h, _, win_len = decision._dp_window(now, horizon_edge, slot_minutes)
    reserve_stride = timedelta(minutes=slot_minutes)
    reserve_list = [
        reserve_by_hour.get(resolution.hour_floor(slot_now_h + i * reserve_stride), floor_kwh) for i in range(win_len)
    ]

    export_price_matches_import = options_raw.get("ent_export_price") == options_raw.get("ent_price")
    export_price = next((s.price for s in real_slots if s.start == now_h), remaining_prices[0])

    water_value_hi: float | None = None
    overnight_need_kwh = 0.0
    if cfg.terminal_overnight_credit:
        pickup = scheduler.find_next_solar_pickup(horizon_edge, intervals_reserve) or decision._next_synthetic_pickup(
            horizon_edge
        )
        load_by_hod: dict[int, float] = {}
        for iv in intervals_reserve:
            load_by_hod[iv.start.hour] = iv.load_w

        eta_d_static = cfg.eta_discharge_static()
        win_slots = [s for s in real_slots if slot_now_h <= s.start < horizon_edge]
        win_prices = [s.price for s in win_slots]
        if cfg.price_mode == const.PRICE_MODE_STATIC:
            eff = [optimize_mod.effective_export_price(export_price, cfg)]
        elif export_price_matches_import:
            eff = [optimize_mod.effective_export_price(p, cfg) for p in win_prices]
        else:
            cur_import = win_prices[0] if win_prices else 0.0
            if cur_import > 1e-9:
                ratio = export_price / cur_import
                eff = [optimize_mod.effective_export_price(p * ratio, cfg) for p in win_prices]
            else:
                eff = [optimize_mod.effective_export_price(export_price, cfg)]
        max_export_dc_value = (max(eff) * eta_d_static - cfg.cycle_cost_eur_per_kwh) if eff else water_value

        water_value_hi, overnight_need_kwh = optimize_mod.overnight_terminal_params(
            gap_start=horizon_edge,
            pickup=pickup,
            est_price_by_hour=est_price_by_hour,
            load_w_by_hod=load_by_hod,
            v_lo=water_value,
            max_export_dc_value=max_export_dc_value,
            cfg=cfg,
            eta_curve=eta_curve,
        )

    peak = max((s.price for s in real_slots if now_h <= s.start < horizon_edge), default=None)
    ceiling = scheduler.charge_price_ceiling(peak, cfg)

    inputs = PlantInputs(soc=soc_start, meter_w=0.0, now=now)

    captured: dict = {}
    orig_optimize_grid = optimize_mod.optimize_grid
    orig_select_end_state = optimize_mod.select_end_state

    def _capturing_optimize_grid(*args, **kwargs):
        captured["optimize_grid_args"] = args
        captured["optimize_grid_kwargs"] = kwargs
        result = orig_optimize_grid(*args, **kwargs)
        captured["optimize_grid_result"] = result
        return result

    def _capturing_select_end_state(*args, **kwargs):
        result = orig_select_end_state(*args, **kwargs)
        captured["select_end_state_kwargs"] = kwargs
        captured["select_end_state_result"] = result
        return result

    optimize_mod.optimize_grid = _capturing_optimize_grid
    optimize_mod.select_end_state = _capturing_select_end_state
    try:
        (
            selected,
            grid_request,
            infeasible,
            export_request,
            export_revenue_eur,
            ceiling_by_hour,
        ) = decision._dp_select_slots(
            inputs=inputs,
            slots=real_slots,
            deadline=horizon_edge,
            ceiling=ceiling,
            cfg=cfg,
            export_price=export_price,
            terminal_mode="water_value",
            water_value=water_value,
            export_price_matches_import=export_price_matches_import,
            reserve_by_hour=reserve_list,
            sun_times=None,
            intervals=intervals,
            hedge_drain_by_hour=None,
            slot_minutes=slot_minutes,
            dt_h=dt_h,
            eta_curve=eta_curve,
            water_value_hi=water_value_hi,
            overnight_need_kwh=overnight_need_kwh,
            export_slots=None,
        )
    finally:
        optimize_mod.optimize_grid = orig_optimize_grid
        optimize_mod.select_end_state = orig_select_end_state

    from_bin = captured["select_end_state_kwargs"]["from_bin"]
    best_end_b, best_cost, dp_infeasible = captured["select_end_state_result"]
    end_dc_kwh = from_bin(best_end_b) if best_end_b != -1 else None
    end_soc_pct = cfg.kwh_to_pct(end_dc_kwh) if end_dc_kwh is not None else None

    return {
        "now": now,
        "now_h": now_h,
        "horizon_edge": horizon_edge,
        "slot_minutes": slot_minutes,
        "soc_start": soc_start,
        "cfg": cfg,
        "water_value": water_value,
        "water_value_hi": water_value_hi,
        "overnight_need_kwh": overnight_need_kwh,
        "reserve_by_hour": reserve_by_hour,
        "reserve_list": reserve_list,
        "slot_now_h": slot_now_h,
        "selected": selected,
        "grid_request": grid_request,
        "infeasible": infeasible,
        "export_request": export_request,
        "export_revenue_eur": export_revenue_eur,
        "ceiling_by_hour": ceiling_by_hour,
        "captured": captured,
        "end_dc_kwh": end_dc_kwh,
        "end_soc_pct": end_soc_pct,
        "dp_infeasible": dp_infeasible,
        "best_cost": best_cost,
    }


def _fmt(v, nd=4):
    return "None" if v is None else f"{v:.{nd}f}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plan", required=True, type=Path)
    ap.add_argument("--options", required=True, type=Path)
    ap.add_argument("--now", default=None, help="ISO datetime override (default: fixture last_updated floored to hour)")
    ap.add_argument("--soc", type=float, default=None, help="Starting SoC %% override")
    ap.add_argument("--capacity-kwh", type=float, default=CAPACITY_KWH_DEFAULT)
    ap.add_argument("--max-charge-w", type=float, default=MAX_CHARGE_W_DEFAULT)
    ap.add_argument("--max-export-w", type=float, default=MAX_EXPORT_W_DEFAULT)
    args = ap.parse_args()

    r = replay(
        args.plan,
        args.options,
        now_override=args.now,
        soc_override=args.soc,
        capacity_kwh=args.capacity_kwh,
        max_charge_w=args.max_charge_w,
        max_export_w=args.max_export_w,
    )
    cfg: Config = r["cfg"]

    print("=" * 78)
    print("DP REPLAY —", args.plan)
    print("=" * 78)
    print(f"now={r['now'].isoformat()}  soc_start={r['soc_start']:.2f}%  slot_minutes={r['slot_minutes']}")
    print(f"horizon_edge={r['horizon_edge'].isoformat()}")
    print(
        f"cfg: capacity_kwh={cfg.capacity_kwh} soc_floor={cfg.soc_floor}% (floor_kwh={cfg.floor_kwh:.3f}) "
        f"firmware_floor_kwh={cfg.firmware_floor_kwh:.3f} (FIRMWARE_SOC_FLOOR={const.FIRMWARE_SOC_FLOOR}%) "
        f"soc_target={cfg.soc_target}% cycle_cost={cfg.cycle_cost_eur_per_kwh} reserve_anchor={cfg.reserve_anchor}"
    )
    print(f"water_value(v_lo)={_fmt(r['water_value'])}  water_value_hi={_fmt(r['water_value_hi'])}  "
          f"overnight_need_kwh={_fmt(r['overnight_need_kwh'])}")
    print()

    print("--- (a) reserve_by_hour (DC kWh) at 17/18/19/20 UTC, raw dict vs padded DP list ---")
    kwargs = r["captured"]["optimize_grid_kwargs"]
    dp_reserve_list = kwargs.get("reserve_by_hour")
    slot_now_h = r["slot_now_h"]
    slot_minutes = r["slot_minutes"]
    stride = timedelta(minutes=slot_minutes)
    for h in (17, 18, 19, 20):
        hour_dt = r["now_h"].replace(hour=h) if r["now_h"].hour <= h else r["now_h"].replace(hour=h) + timedelta(days=1)
        raw = r["reserve_by_hour"].get(hour_dt)
        idx = round((hour_dt - slot_now_h).total_seconds() / (slot_minutes * 60))
        padded = dp_reserve_list[idx] if dp_reserve_list is not None and 0 <= idx < len(dp_reserve_list) else None
        pct = cfg.kwh_to_pct(raw) if raw is not None else None
        print(
            f"  {hour_dt.isoformat()}  raw_dict={_fmt(raw, 3)} kWh ({_fmt(pct, 1)}%)  "
            f"padded_dp_list[{idx}]={_fmt(padded, 3)} kWh  export_floor=max(floor_kwh, reserve)="
            f"{_fmt(max(cfg.floor_kwh, raw) if raw is not None else cfg.floor_kwh, 3)} kWh"
        )
    print()

    print("--- (b) floor args passed to optimize_grid ---")
    print(f"  cfg.floor_kwh (soft, soc_floor={cfg.soc_floor}%) = {cfg.floor_kwh:.3f} kWh")
    print(
        f"  cfg.firmware_floor_kwh (const.FIRMWARE_SOC_FLOOR={const.FIRMWARE_SOC_FLOOR}%) = "
        f"{cfg.firmware_floor_kwh:.3f} kWh"
    )
    print(
        f"  terminal_mode={kwargs.get('terminal_mode')!r}  water_value_hi passed={_fmt(kwargs.get('water_value_hi'))}  "
        f"overnight_need_kwh passed={_fmt(kwargs.get('overnight_need_kwh'))}"
    )
    print(f"  reserve_by_hour passed: len={len(dp_reserve_list) if dp_reserve_list is not None else 0}  "
          f"min={_fmt(min(dp_reserve_list), 3) if dp_reserve_list else 'None'}  "
          f"max={_fmt(max(dp_reserve_list), 3) if dp_reserve_list else 'None'}")
    print()

    print("--- (c) DP-internal end state vs displayed 5.0% ---")
    print(f"  DP best_end_b -> end_dc_kwh={_fmt(r['end_dc_kwh'], 3)}  end_soc_pct={_fmt(r['end_soc_pct'], 2)}%")
    print(f"  dp_infeasible={r['dp_infeasible']}  best_cost={_fmt(r['best_cost'], 4)}")
    print(f"  compare: firmware_floor_kwh={cfg.firmware_floor_kwh:.3f} kWh ({const.FIRMWARE_SOC_FLOOR}%)  "
          f"soft floor_kwh={cfg.floor_kwh:.3f} kWh ({cfg.soc_floor}%)")
    print()

    print("--- backtracked schedule (selected charge slots / export request) ---")
    print("  selected (charge) slots:", [s.isoformat() for s in r["selected"]])
    print("  grid_request (W):", {k.isoformat(): round(v, 1) for k, v in r["grid_request"].items()})
    print("  export_request (W):", {k.isoformat(): round(v, 1) for k, v in r["export_request"].items()})
    print(f"  export_revenue_eur={r['export_revenue_eur']:.4f}  infeasible(shield)={r['infeasible']}")
    print()

    schedule = r["captured"]["optimize_grid_result"]["schedule"]
    export_schedule = r["captured"]["optimize_grid_result"].get("export_schedule", [0.0] * len(schedule))
    print("--- full per-slot DP schedule (AC kWh) ---")
    for i in range(len(schedule)):
        t = slot_now_h + i * stride
        if schedule[i] > 1e-6 or export_schedule[i] > 1e-6:
            print(f"  [{i:3d}] {t.isoformat()}  charge_ac={schedule[i]:.3f}  export_ac={export_schedule[i]:.3f}")


if __name__ == "__main__":
    main()
