"""Pure builders for the power card's debug payload. Display only, no HA imports."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta, tzinfo
from itertools import pairwise

from .daily_stats import planned_house_flows
from .dataquality import house_load_w
from .money import MAX_DT_S, ahead, grid_eur, hour_key, parse_ts, slots
from .usecost import nothing_delivered, hourly_series

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
    real = [start for start, row, _h in slots(horizon) if not row.get("estimated")]
    width = round((real[1] - real[0]).total_seconds() / 60) if len(real) > 1 else 60
    return width if 0 < width <= 60 and 60 % width == 0 else 60


def snapshot_rows(horizon: list[dict] | None, now: datetime) -> list[dict]:
    return [
        {k: row.get(k) for k in SNAPSHOT_KEYS} for _s, row, _h, _l in ahead(horizon, now) if not row.get("estimated")
    ]


def merge_reference(snapshots: list[tuple[str, list[dict]]], tz: tzinfo) -> list[dict]:
    """One row per start: the slot's own day's snapshot when it holds it, else the latest earlier one.

    Each row gains ``minutes``, its width in the snapshot it comes from.
    """
    chosen: dict[datetime, dict] = {}
    for day, rows in snapshots:
        for start, row, h in slots(rows):
            own = start.astimezone(tz).date().isoformat() == day
            if own or start not in chosen or not chosen[start]["own"]:
                chosen[start] = {"row": row | {"minutes": round(h * 60)}, "own": own}
    return [chosen[start]["row"] for start in sorted(chosen)]


def _grid_eur(kwh: tuple[float, float] | None, price: float | None, export_price: float | None) -> float | None:
    eur = grid_eur(kwh, price, export_price)
    return None if eur is None else round(eur, 4)


def _measured(slot: dict) -> tuple[float, float] | None:
    if slot["plan"] or slot["grid_out_kwh"] is None:
        return None
    return slot["grid_in_house_kwh"] + slot["grid_in_charge_kwh"], slot["grid_out_kwh"]


def _time_mean(spans: list[tuple[float, dict]], key: str) -> float | None:
    if not spans or any(slot[key] is None for _w, slot in spans):
        return None
    return sum(w * slot[key] for w, slot in spans) / sum(w for w, _slot in spans)


def slot_series(
    rows: list[dict],
    horizon: list[dict] | None,
    tz: tzinfo,
    now: datetime,
    delivered_at: Callable[[datetime], float] = nothing_delivered,
    reference: Sequence[dict] = (),
    export_fee: float = 0.0,
) -> list[dict]:
    """``hourly_series`` at the plan's slot width, plus charge-positive ``setpoint_w`` and ``batt_w`` (W).

    ``export_price`` is the recorded tariff less ``export_fee``. ``plan_eur`` and
    ``real_eur`` are the slot's grid cost under the ``reference`` row of the same
    start and width and as measured, both at the slot's recorded prices; ``None``
    for both when either side or a price it needs is missing, and for slots not yet over.
    """
    width = slot_minutes(horizon)
    pts = sorted(((ts, r) for r in rows if (ts := parse_ts(r.get("ts"))) is not None), key=lambda p: p[0])
    acc: dict[datetime, list[float]] = {}
    for (ts, r), (nxt, _) in pairwise(pts):
        dt = (nxt - ts).total_seconds()
        if not 0 < dt <= MAX_DT_S:
            continue
        if house_load_w(r) is None:
            continue
        a = acc.setdefault(hour_key(ts, tz, width), [0.0] * 6)
        a[0] += dt
        a[1] += (float(r.get("setpoint_w") or 0.0) - float(r.get("export_setpoint_w") or 0.0)) * dt
        if r.get("batt_w") is not None:
            a[2] += dt
            a[3] += -float(r["batt_w"]) * dt
        if r.get("export_price") is not None:
            a[4] += dt
            a[5] += float(r["export_price"]) * dt
    plan = {
        hour_key(start, tz, width): float(row.get("grid_charge_w") or 0.0) - float(row.get("grid_export_w") or 0.0)
        for start, row, _h, _l in ahead(horizon, now)
        if not row.get("estimated")
    }
    planned = {
        parse_ts(r["start"]).astimezone(UTC): planned_house_flows(r) for r in reference if r.get("minutes") == width
    }
    out = []
    for row in hourly_series(rows, horizon, tz, now, delivered_at, width):
        key = parse_ts(row["start"]).astimezone(UTC)
        a = acc.get(key)
        setpoint = a[1] / a[0] if a and a[0] else plan.get(key)
        batt = a[3] / a[2] if a and a[2] else None
        export_price = round(a[5] / a[4] - export_fee, 4) if a and a[4] else None
        plan_eur = _grid_eur(planned.get(key), row["price"], export_price)
        real_eur = _grid_eur(_measured(row), row["price"], export_price)
        if plan_eur is None or real_eur is None:
            plan_eur = real_eur = None
        out.append(
            {k: v for k, v in row.items() if k not in ("grid_bought_eur", "grid_sold_eur")}
            | {"setpoint_w": setpoint, "batt_w": batt, "export_price": export_price}
            | {"plan_eur": plan_eur, "real_eur": real_eur}
        )
    return out


def hour_costs(slots: list[dict], reference: Sequence[dict], tz: tzinfo, width: int) -> dict[str, dict]:
    """Per local hour with a slot over, keyed by its start: ``plan_eur`` and ``real_eur`` as in :func:`slot_series`.

    Works at any ``reference`` width: each reference row is priced at the time
    mean of the prices of the ``width``-minute slots it spans. ``None`` for both
    unless every slot of the hour is over and priced, and the hour's reference
    rows cover it end to end.
    """
    step = timedelta(minutes=width)
    over = {parse_ts(s["start"]).astimezone(UTC): s for s in slots if not s["plan"]}
    ref = sorted(((parse_ts(r["start"]).astimezone(UTC), r) for r in reference), key=lambda p: p[0])
    out = {}
    for hour in sorted({hour_key(start, tz) for start in over}):
        end = hour + timedelta(hours=1)
        mine = [(hour + k * step, over.get(hour + k * step)) for k in range(60 // width)]
        real = [_grid_eur(_measured(s), s["price"], s["export_price"]) if s else None for _t, s in mine]
        rows = [(start, start + timedelta(minutes=r["minutes"]), r) for start, r in ref if hour <= start < end]
        tiled = bool(rows) and rows[0][0] == hour and rows[-1][1] == end
        tiled = tiled and all(a[1] == b[0] for a, b in pairwise(rows))
        key = hour.astimezone(tz).isoformat()
        out[key] = {"plan_eur": None, "real_eur": None}
        if not tiled or None in real:
            continue
        plan = []
        for start, stop, r in rows:
            spans = [
                ((min(stop, t + step) - max(start, t)).total_seconds(), s)
                for t, s in mine
                if t < stop and t + step > start
            ]
            plan.append(
                _grid_eur(planned_house_flows(r), _time_mean(spans, "price"), _time_mean(spans, "export_price"))
            )
        if None not in plan:
            out[key] = {"plan_eur": round(sum(plan), 4), "real_eur": round(sum(real), 4)}
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
    real = [(start, h) for start, row, h in slots(horizon) if not row.get("estimated")]
    built = parse_ts(built_at)
    battery_at = next((parse_ts(r.get("ts")) for r in reversed(rows) if r.get("soc") is not None), None)

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
