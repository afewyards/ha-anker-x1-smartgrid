"""Pure builders for the power card's debug payload. Display only, no HA imports."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, tzinfo
from itertools import pairwise

from .margin import _MAX_DT_S
from .usecost import _ahead, _hour_key, _nothing_delivered, _parse, _slots, hourly_series

SNAPSHOT_KEYS = (
    "start",
    "soc",
    "load_kwh",
    "pv_kwh",
    "grid_charge_kwh",
    "solar_charge_kwh",
    "grid_export_kwh",
    "self_discharge_kwh",
)


def slot_minutes(horizon: list[dict] | None) -> int:
    real = [start for start, row, _h in _slots(horizon) if not row.get("estimated")]
    return round((real[1] - real[0]).total_seconds() / 60) if len(real) > 1 else 60


def snapshot_rows(horizon: list[dict] | None, now: datetime) -> list[dict]:
    return [
        {k: row.get(k) for k in SNAPSHOT_KEYS} for _s, row, _h, _l in _ahead(horizon, now) if not row.get("estimated")
    ]


def merge_reference(snapshots: list[tuple[str, list[dict]]], tz: tzinfo) -> list[dict]:
    """One row per start: the slot's own day's snapshot when it holds it, else the latest earlier one."""
    chosen: dict[datetime, dict] = {}
    for day, rows in snapshots:
        for row in rows:
            start = _parse(row["start"])
            if start is None:
                continue
            own = start.astimezone(tz).date().isoformat() == day
            if own or start not in chosen or not chosen[start]["own"]:
                chosen[start] = {"row": row, "own": own}
    return [chosen[start]["row"] for start in sorted(chosen)]


def slot_series(
    rows: list[dict],
    horizon: list[dict] | None,
    tz: tzinfo,
    now: datetime,
    delivered_at: Callable[[datetime], float] = _nothing_delivered,
) -> list[dict]:
    """``hourly_series`` at the plan's slot width, plus charge-positive ``setpoint_w`` and ``batt_w`` (W)."""
    width = slot_minutes(horizon)
    pts = sorted(((ts, r) for r in rows if (ts := _parse(r.get("ts"))) is not None), key=lambda p: p[0])
    acc: dict[datetime, list[float]] = {}
    for (ts, r), (nxt, _) in pairwise(pts):
        dt = (nxt - ts).total_seconds()
        if not 0 < dt <= _MAX_DT_S:
            continue
        a = acc.setdefault(_hour_key(ts, tz, width), [0.0, 0.0, 0.0, 0.0])
        a[0] += dt
        a[1] += (float(r.get("setpoint_w") or 0.0) - float(r.get("export_setpoint_w") or 0.0)) * dt
        if r.get("batt_w") is not None:
            a[2] += dt
            a[3] += -float(r["batt_w"]) * dt
    plan = {
        _hour_key(start, tz, width): float(row.get("grid_charge_w") or 0.0) - float(row.get("grid_export_w") or 0.0)
        for start, row, _h, _l in _ahead(horizon, now)
        if not row.get("estimated")
    }
    out = []
    for row in hourly_series(rows, horizon, tz, now, delivered_at, width):
        key = _parse(row["start"]).astimezone(UTC)
        a = acc.get(key)
        setpoint = a[1] / a[0] if a and a[0] else plan.get(key)
        batt = a[3] / a[2] if a and a[2] else None
        out.append(row | {"setpoint_w": setpoint, "batt_w": batt})
    return out


def internals(status: dict, cfg, built_at: str | None) -> dict:
    """Label to value for the figures the plan runs on; only those present."""
    horizon = (status.get("plan") or {}).get("horizon") or []
    reserve = next((r.get("reserve_soc") for r in horizon if r.get("mode") != "actual"), None)
    stored = status.get("hold_stored_value_eur_per_kwh")
    out = {
        "built_at": built_at,
        "forecast_source": status.get("active_model"),
        "load_correction_ratio": status.get("load_adapt_ratio"),
        "reserve_soc": reserve,
        "ceiling_soc": getattr(cfg, "soc_target", None),
        "end_value_eur_per_kwh": status.get("terminal_v_hi") if stored is None else stored,
        "cycle_cost_eur_per_kwh": cfg.cycle_cost_eur_per_kwh,
        "charge_margin_eur_per_kwh": cfg.charge_margin_eur_per_kwh,
        "idle_drain_w": cfg.idle_drain_w,
    }
    return {k: v for k, v in out.items() if v is not None}
