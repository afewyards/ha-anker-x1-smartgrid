"""Aggregate recorded per-tick samples into per-clock-hour measured actuals.

Display-only: feeds the past slots of the plan horizon so the dashboard shows
real recent history. Never affects control.
"""
from __future__ import annotations

from datetime import datetime

from .dataquality import house_load_w


def _hour(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def _mean(vals: list[float]) -> float | None:
    return sum(vals) / len(vals) if vals else None


def aggregate_past_actuals(rows: list[dict]) -> dict[datetime, dict]:
    """Group recorder sample rows by clock-hour into measured-actuals records.

    Each row is a column-keyed dict from ``DataRecorder.read_feature_rows``
    (keys include ``ts, soc, pv_w, batt_w, p1_w, load_w``). Returns
    ``{hour_start: {pv_w, load_w, soc, solar_charge_w, grid_charge_w, grid_export_w}}``.

    Sign conventions (verified live 2026-06-29): ``batt_w`` negative = charging
    so ``charge_w = max(0, -batt_w)``; ``p1_w`` positive = import so
    ``grid_export_w = max(0, -p1_w)``. Battery charge is attributed solar-first
    (PV surplus then grid) to mirror ``plan.build_plan_horizon``. Hours with no
    rows are absent from the result.
    """
    buckets: dict[datetime, list[dict]] = {}
    for row in rows:
        ts_raw = row.get("ts")
        if not ts_raw:
            continue
        try:
            ts = datetime.fromisoformat(str(ts_raw))
        except (ValueError, TypeError):
            continue
        buckets.setdefault(_hour(ts), []).append(row)

    out: dict[datetime, dict] = {}
    for hour, group in buckets.items():
        pv_vals = [float(r["pv_w"]) for r in group if r.get("pv_w") is not None]
        load_vals = [v for v in (house_load_w(r) for r in group) if v is not None]
        soc_vals = [float(r["soc"]) for r in group if r.get("soc") is not None]
        charge_vals = [max(0.0, -float(r["batt_w"])) for r in group if r.get("batt_w") is not None]
        export_vals = [max(0.0, -float(r["p1_w"])) for r in group if r.get("p1_w") is not None]

        pv_w = _mean(pv_vals) or 0.0
        load_w = _mean(load_vals)
        soc = _mean(soc_vals)
        charge_w = _mean(charge_vals) or 0.0
        grid_export_w = _mean(export_vals) or 0.0

        surplus = max(0.0, pv_w - (load_w or 0.0))
        solar_charge_w = min(charge_w, surplus)
        grid_charge_w = max(0.0, charge_w - solar_charge_w)

        out[hour] = {
            "pv_w": round(pv_w, 1),
            "load_w": round(load_w, 1) if load_w is not None else None,
            "soc": round(soc, 1) if soc is not None else None,
            "solar_charge_w": round(solar_charge_w, 1),
            "grid_charge_w": round(grid_charge_w, 1),
            "grid_export_w": round(grid_export_w, 1),
        }
    return out
