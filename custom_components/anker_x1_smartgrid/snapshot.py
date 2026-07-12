"""Decision/status snapshot builders (Task C4).

Pure functions extracted verbatim from controller.py's
``_build_decision_snapshot`` / ``_status`` / ``_occ_status_attrs``: no
``self``, all inputs passed explicitly. Controller keeps thin wrapper
methods (same names/signatures as before) that gather the relevant instance
state and delegate here — existing call sites (internal and test) keep
working unchanged.
"""
from __future__ import annotations

import json
from datetime import datetime

from . import occupancy


def build_decision_snapshot(
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


def occ_status_attrs(
    occ_table,
    persons_home_now: int | None,
    now: datetime,
    occ_persistence_h: float,
    occ_adapt_fraction: float,
) -> dict:
    """Occupancy-corrector observability attrs (Layer B)."""
    return {
        # Clamped to the same state bin multiplier() uses (0..STATE_MAX), so
        # occ_state_now and occ_expected_state are directly comparable.
        "occ_state_now": (
            min(occupancy.STATE_MAX, max(0, int(persons_home_now)))
            if persons_home_now is not None else None
        ),
        "occ_expected_state": (
            occ_table.climo_state.get(occupancy.band_of(now))
            if occ_table is not None else None
        ),
        "occ_multiplier": round(
            occupancy.multiplier(
                occ_table, persons_home_now, now, now,
                occ_persistence_h, occ_adapt_fraction,
            ), 3,
        ),
        "occ_cells_ready": (
            occ_table.cells_ready if occ_table is not None else 0
        ),
    }


def build_status(
    *,
    now: datetime,
    setpoint,
    deadline,
    reason,
    solar_charge: float = 0.0,
    plan_state: str,
    backtest_result: dict | None,
    active_model_name: str,
    load_adapt_ratio: float | None,
    load_adapt_matched: int,
    occ_table,
    persons_home_now: int | None,
    occ_persistence_h: float,
    occ_adapt_fraction: float,
    regret: dict | None,
    dp_regret_7d: float | None,
    today_export_pnl_eur: float,
    today_charge_cost_eur: float,
    today_export_revenue_eur: float,
    total_net_eur: float,
    planned_export_revenue_eur: float,
    slot_minutes: int,
    efficiency_curve_attrs: dict,
    use_measured_eta: bool,
) -> dict:
    _regret = regret or {}
    _bt = backtest_result or {}
    return {
        "state": plan_state,
        "solar_charge_kwh": round(solar_charge, 3),
        "setpoint_w": setpoint,
        "deadline": deadline.isoformat() if deadline else None,
        "reason": reason,
        "load_mae": _bt.get("model_mae"),
        "horizon_energy_mae_24h": _bt.get("horizon_energy_mae_24h"),
        "horizon_energy_mae_12h": _bt.get("horizon_energy_mae_12h"),
        "pinball_p50": _bt.get("pinball_p50"),
        "pinball_p80": _bt.get("pinball_p80"),
        "active_model": active_model_name,
        "load_adapt_ratio": (
            round(load_adapt_ratio, 3)
            if load_adapt_ratio is not None else None
        ),
        "load_adapt_matched_hours": load_adapt_matched,
        **occ_status_attrs(
            occ_table, persons_home_now, now, occ_persistence_h, occ_adapt_fraction,
        ),
        "regret_eur": _regret.get("regret_eur"),
        "over_buy_kwh": _regret.get("over_buy_kwh"),
        "under_buy_kwh": _regret.get("under_buy_kwh"),
        # 7-day rolling DP-vs-heuristic regret delta (T0.5c).
        # Negative = DP was cheaper over past 7 days; None until first day scored.
        "dp_regret_7d": dp_regret_7d,
        # E3: realized export PnL for the current local day (€).
        # Accumulated per tick when the C3 export executor fires.
        # Resets to 0.0 on local-day rollover.  G2 reads this key.
        "today_export_pnl_eur": round(today_export_pnl_eur, 6),
        # Cash ledger (spec 2026-07-10): realized battery cash flows.
        # battery_net_today/total drive the two €-sensors; components are
        # exposed for the today-sensor's attributes.
        "today_charge_cost_eur": round(today_charge_cost_eur, 6),
        "today_export_revenue_eur": round(today_export_revenue_eur, 6),
        "battery_net_today_eur": round(
            today_export_revenue_eur - today_charge_cost_eur, 6
        ),
        "battery_net_total_eur": round(total_net_eur, 6),
        # C4: the DP's PLANNED export revenue (€) for the current horizon. Drives
        # the card's arbitrage_pnl so it reflects the plan, not just realized ticks.
        "planned_export_revenue_eur": round(planned_export_revenue_eur, 6),
        "slot_minutes": slot_minutes,
        # T18: measured efficiency curve bin table, for observability only
        # (does not drive behaviour — that's gated by use_measured_eta below).
        "efficiency_curve": efficiency_curve_attrs,
        "use_measured_eta": use_measured_eta,
    }
