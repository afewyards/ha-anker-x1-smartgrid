"""Aggregate recorded per-tick samples into per-clock-hour measured actuals.

Display-only: feeds the past slots of the plan horizon so the dashboard shows
real recent history. Never affects control.

Energy keys (``pv_kwh``, ``load_kwh``, ``solar_charge_kwh``, ``grid_charge_kwh``,
``grid_export_kwh``) are true integrals of power over time: they sum the
recorder's v9 per-tick kWh delta columns (``pv_kwh``, ``house_load_kwh``,
``batt_charge_kwh``, ``grid_export_kwh``) for the hour, rather than deriving
energy from the mean power. Pre-v9 rows (and the first tick after a restart)
have NULL deltas; for those, energy falls back to mean-W-derived (mean power x
1h). The W keys (``pv_w``, ``load_w``, ``soc``, ``solar_charge_w``,
``grid_charge_w``, ``grid_export_w``) remain unchanged naive means — they are
still consumed by ``load_adapt`` and must not be altered by this change.

Note: a hour bucket with fewer ticks than a full hour (e.g. the current,
still-in-progress hour) yields "energy so far" for that partial hour, by
design — the energy keys are not extrapolated to a full-hour total.
"""
from __future__ import annotations

from datetime import datetime

from .dataquality import house_load_w


def _hour(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def _mean(vals: list[float]) -> float | None:
    return sum(vals) / len(vals) if vals else None


def _kwh_sum(group: list[dict], col: str) -> float | None:
    """Sum the v9 per-tick kWh delta column ``col`` across ``group``.

    Returns ``None`` (rather than 0.0) when every row in ``group`` has a NULL
    value for ``col``, so callers can distinguish "no v9 data, fall back to
    mean-W-derived" from "v9 data present and it summed to zero".
    """
    vals = [float(r[col]) for r in group if r.get(col) is not None]
    return sum(vals) if vals else None


def aggregate_past_actuals(rows: list[dict]) -> dict[datetime, dict]:
    """Group recorder sample rows by clock-hour into measured-actuals records.

    Each row is a column-keyed dict from ``DataRecorder.read_feature_rows``
    (keys include ``ts, soc, pv_w, batt_w, p1_w, load_w`` and, since v9, the
    per-tick kWh deltas ``pv_kwh, house_load_kwh, batt_charge_kwh,
    grid_export_kwh``). Returns ``{hour_start: {pv_w, load_w, soc,
    solar_charge_w, grid_charge_w, grid_export_w, pv_kwh, load_kwh,
    solar_charge_kwh, grid_charge_kwh, grid_export_kwh}}``.

    Sign conventions (verified live 2026-06-29): ``batt_w`` negative = charging
    so ``charge_w = max(0, -batt_w)``; ``p1_w`` positive = import so
    ``grid_export_w = max(0, -p1_w)``. Battery charge is attributed solar-first
    (PV surplus then grid) to mirror ``plan.build_plan_horizon``. Hours with no
    rows are absent from the result.

    Energy keys (``pv_kwh, load_kwh, solar_charge_kwh, grid_charge_kwh,
    grid_export_kwh``) sum the v9 per-tick kWh deltas (true energy, not
    mean-power-derived) with a mean-W x 1h fallback for pre-v9 rows; see
    module docstring for details. The W keys are unchanged naive means.
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

        # Energy (kWh): sum the v9 per-tick deltas (true integral of power over
        # time) with a mean-W x 1h fallback for pre-v9 rows / restart gaps.
        pv_kwh = _kwh_sum(group, "pv_kwh")
        pv_kwh = pv_kwh if pv_kwh is not None else pv_w / 1000.0
        load_kwh = _kwh_sum(group, "house_load_kwh")
        if load_kwh is None and load_w is not None:
            load_kwh = load_w / 1000.0
        charge_kwh = _kwh_sum(group, "batt_charge_kwh")
        charge_kwh = charge_kwh if charge_kwh is not None else charge_w / 1000.0
        export_kwh = _kwh_sum(group, "grid_export_kwh")
        export_kwh = export_kwh if export_kwh is not None else grid_export_w / 1000.0

        surplus_kwh = max(0.0, pv_kwh - (load_kwh or 0.0))
        solar_charge_kwh = min(charge_kwh, surplus_kwh)
        grid_charge_kwh = max(0.0, charge_kwh - solar_charge_kwh)

        out[hour] = {
            "pv_w": round(pv_w, 1),
            "load_w": round(load_w, 1) if load_w is not None else None,
            "soc": round(soc, 1) if soc is not None else None,
            "solar_charge_w": round(solar_charge_w, 1),
            "grid_charge_w": round(grid_charge_w, 1),
            "grid_export_w": round(grid_export_w, 1),
            "pv_kwh": round(pv_kwh, 3),
            "load_kwh": round(load_kwh, 3) if load_kwh is not None else None,
            "solar_charge_kwh": round(solar_charge_kwh, 3),
            "grid_charge_kwh": round(grid_charge_kwh, 3),
            "grid_export_kwh": round(export_kwh, 3),
        }
    return out
