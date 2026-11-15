"""Pure builders for the power card's debug payload. Display only, no HA imports."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta, tzinfo
from itertools import pairwise

from .daily_stats import planned_house_flows
from .dataquality import house_load_w
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
    width = round((real[1] - real[0]).total_seconds() / 60) if len(real) > 1 else 60
    return width if 0 < width <= 60 and 60 % width == 0 else 60


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


def _grid_eur(kwh: tuple[float, float] | None, price: float | None, export_price: float | None) -> float | None:
    if kwh is None or price is None or (kwh[1] > 1e-6 and export_price is None):
        return None
    return round(kwh[0] * price - kwh[1] * (export_price or 0.0), 4)


def slot_series(
    rows: list[dict],
    horizon: list[dict] | None,
    tz: tzinfo,
    now: datetime,
    delivered_at: Callable[[datetime], float] = _nothing_delivered,
    reference: Sequence[dict] = (),
    export_fee: float = 0.0,
) -> list[dict]:
    """``hourly_series`` at the plan's slot width, plus charge-positive ``setpoint_w`` and ``batt_w`` (W).

    ``export_price`` is the recorded tariff less ``export_fee``. ``plan_eur`` and
    ``real_eur`` are the slot's grid cost under its ``reference`` row and as
    measured, both at the slot's recorded prices; ``None`` for both when either
    side or a price it needs is missing, and for slots not yet over.
    """
    width = slot_minutes(horizon)
    pts = sorted(((ts, r) for r in rows if (ts := _parse(r.get("ts"))) is not None), key=lambda p: p[0])
    acc: dict[datetime, list[float]] = {}
    for (ts, r), (nxt, _) in pairwise(pts):
        dt = (nxt - ts).total_seconds()
        if not 0 < dt <= _MAX_DT_S:
            continue
        if house_load_w(r) is None:
            continue
        a = acc.setdefault(_hour_key(ts, tz, width), [0.0] * 6)
        a[0] += dt
        a[1] += (float(r.get("setpoint_w") or 0.0) - float(r.get("export_setpoint_w") or 0.0)) * dt
        if r.get("batt_w") is not None:
            a[2] += dt
            a[3] += -float(r["batt_w"]) * dt
        if r.get("export_price") is not None:
            a[4] += dt
            a[5] += float(r["export_price"]) * dt
    plan = {
        _hour_key(start, tz, width): float(row.get("grid_charge_w") or 0.0) - float(row.get("grid_export_w") or 0.0)
        for start, row, _h, _l in _ahead(horizon, now)
        if not row.get("estimated")
    }
    # shortcut: matched by start only, so a saved plan at another slot width prices only the slots it shares.
    planned = {_parse(r["start"]).astimezone(UTC): planned_house_flows(r) for r in reference}
    out = []
    for row in hourly_series(rows, horizon, tz, now, delivered_at, width):
        key = _parse(row["start"]).astimezone(UTC)
        a = acc.get(key)
        setpoint = a[1] / a[0] if a and a[0] else plan.get(key)
        batt = a[3] / a[2] if a and a[2] else None
        export_price = round(a[5] / a[4] - export_fee, 4) if a and a[4] else None
        measured = None
        if not row["plan"] and row["grid_out_kwh"] is not None:
            measured = (row["grid_in_house_kwh"] + row["grid_in_charge_kwh"], row["grid_out_kwh"])
        plan_eur = _grid_eur(planned.get(key), row["price"], export_price)
        real_eur = _grid_eur(measured, row["price"], export_price)
        if plan_eur is None or real_eur is None:
            plan_eur = real_eur = None
        out.append(
            row
            | {"setpoint_w": setpoint, "batt_w": batt, "export_price": export_price}
            | {"plan_eur": plan_eur, "real_eur": real_eur}
        )
    return out


def internals(
    status: dict, cfg, built_at: str | None, eta: dict, rows: Sequence[dict] = (), pv_at: datetime | None = None
) -> dict:
    """Label to value for the figures the plan runs on; only those present.

    Ages are seconds before ``built_at``: the newest sample with a level, and ``pv_at``.
    """
    horizon = (status.get("plan") or {}).get("horizon") or []
    reserve = next((r.get("reserve_soc") for r in horizon if r.get("mode") != "actual"), None)
    stored = status.get("hold_stored_value_eur_per_kwh")
    real = [(start, h) for start, row, h in _slots(horizon) if not row.get("estimated")]
    built = _parse(built_at)
    battery_at = next((_parse(r.get("ts")) for r in reversed(rows) if r.get("soc") is not None), None)

    def _age(at: datetime | None) -> int | None:
        # The PV states are read after the build, so one may be newer than it.
        return max(round((built - at).total_seconds()), 0) if built and at else None

    out = {
        "built_at": built_at,
        "prices_until": (real[-1][0] + timedelta(hours=real[-1][1])).isoformat() if real else None,
        "tomorrow_estimated": any(r.get("estimated") for r in horizon) if horizon else None,
        "battery_sample_age_s": _age(battery_at),
        "pv_forecast_age_s": _age(pv_at),
        "forecast_source": status.get("active_model"),
        "load_correction_ratio": status.get("load_adapt_ratio"),
        "reserve_soc": reserve,
        "target_soc": getattr(cfg, "soc_target", None),
        "end_value_eur_per_kwh": status.get("terminal_v_hi") if stored is None else stored,
        **eta,
        "cycle_cost_eur_per_kwh": cfg.cycle_cost_eur_per_kwh,
        "charge_margin_eur_per_kwh": cfg.charge_margin_eur_per_kwh,
        "idle_drain_w": cfg.idle_drain_w,
    }
    return {k: v for k, v in out.items() if v is not None}
