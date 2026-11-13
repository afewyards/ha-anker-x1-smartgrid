"""Per-day house use cost, battery saving and an hourly series for the power card.

Display only, no HA imports. Two weighted-average inventories follow the
battery level the way ``margin.py`` does: ``c_free`` prices solar charge at
zero (what the house's use cost), ``c_exp`` at the export price given up (what
the battery earned, identical to ``battery_margin_eur`` plus
``battery_solar_gain_eur``). ``c_free`` is also the grid part of ``c_exp``: both
seed and price grid charge alike and release by the same fraction, so
``c_exp - c_free`` is what the stored solar cost.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from itertools import pairwise

from .daily_stats import _parse
from .dataquality import house_load_w
from .margin import _EMPTY_KWH, _MAX_DT_S, sun_share

_BAND_W = 50.0
_MOVED_KWH = 0.05
_ACTIONS = ("charge_grid", "charge_solar", "house", "export")
_GRID_KEYS = ("grid_in_house_kwh", "grid_in_charge_kwh", "grid_out_kwh")
_PLAN_COLS = (
    ("charge_grid", "grid_charge_kwh"),
    ("charge_solar", "solar_charge_kwh"),
    ("house", "self_discharge_kwh"),
    ("export", "grid_export_kwh"),
)


@dataclass
class Inventory:
    e_kwh: float
    c_free: float
    c_exp: float
    # AC kWh charged from the grid and from solar still in stock, released with the cost.
    grid_kwh: float
    sun_kwh: float


def _day(out: dict, day: date) -> dict:
    return out.setdefault(
        day,
        {
            "use_kwh": 0.0,
            "use_cost_eur": 0.0,
            "saved_eur": 0.0,
            "solar_gain_eur": 0.0,
            "solar_kwh": 0.0,
            "price_eur_h": 0.0,
            "price_h": 0.0,
        },
    )


def _fall(inv: Inventory, e_new: float) -> tuple[float, float]:
    """Release the cost of a level fall to ``e_new`` from both inventories."""
    f = 1.0 if e_new <= _EMPTY_KWH else (inv.e_kwh - e_new) / inv.e_kwh
    free, exp = inv.c_free * f, inv.c_exp * f
    inv.c_free -= free
    inv.c_exp -= exp
    inv.grid_kwh -= inv.grid_kwh * f
    inv.sun_kwh -= inv.sun_kwh * f
    return free, exp


def _earn(rec: dict, inv: Inventory, value: float) -> None:
    """Book battery output's value as saved and solar gain by the solar share of the stock."""
    sun = sun_share(inv.grid_kwh, inv.sun_kwh)
    rec["saved_eur"] += value * (1.0 - sun)
    rec["solar_gain_eur"] += value * sun


def _seed(inv: Inventory, e_kwh: float, price: float) -> None:
    inv.c_free = inv.c_exp = e_kwh * price
    inv.grid_kwh, inv.sun_kwh = e_kwh, 0.0


def _finish(out: dict[date, dict]) -> dict[date, dict]:
    for rec in out.values():
        rec["avg_price"] = rec["price_eur_h"] / rec["price_h"] if rec["price_h"] > 0 else None
    return out


def replay(
    rows: list[dict], capacity_kwh: float, tz: tzinfo, export_fee: float = 0.0
) -> tuple[dict[date, dict], Inventory]:
    """Replay recorder ``samples`` into per-local-day figures and the closing inventory.

    Row filtering, the 5-minute gap rule, seeding and the level-follows-SoC
    release are ``margin.battery_margin_by_day``'s. Per interval:

    - house use is the whole load; grid-direct is ``max(load - pv, 0)`` minus
      what the battery covered, at the import price;
    - battery output is counted on the AC side (``load - pv - p1``); its house
      part is ``min(output, max(load - pv, 0))``;
    - a level fall releases cost from both inventories; ``use_cost_eur`` books
      the house share of the free one (all of it when nothing came out),
      ``saved_eur`` and ``solar_gain_eur`` are the margin's two parts: output
      value minus released ``c_exp``, split between grid and solar charge.
    """
    pts = []
    for row in rows:
        ts = _parse(row.get("ts"))
        if ts is not None and row.get("soc") is not None and row.get("import_price") is not None:
            pts.append((ts, row))
    pts.sort(key=lambda p: p[0])

    out: dict[date, dict] = {}
    inv = Inventory(0.0, 0.0, 0.0, 0.0, 0.0)
    share = 1.0
    for i, (ts, row) in enumerate(pts):
        rec = _day(out, ts.astimezone(tz).date())
        imp = float(row["import_price"])
        raw_exp = row.get("export_price")
        exp = (imp if raw_exp is None else float(raw_exp)) - export_fee
        e_new = float(row["soc"]) / 100.0 * capacity_kwh
        if i == 0:
            _seed(inv, e_new, imp)
        elif (ts - pts[i - 1][0]).total_seconds() > _MAX_DT_S:
            if inv.e_kwh > _EMPTY_KWH:
                # shortcut: a gap rise keeps the pre-gap grid/sun ratio;
                # split it by PV surplus if restarts start showing up in the split.
                _fall(inv, e_new)  # a rise too: the level is re-read at unchanged cost per kWh
            else:
                _seed(inv, e_new, imp)
        elif e_new < inv.e_kwh:
            free, spent = _fall(inv, e_new)
            rec["use_cost_eur"] += free * share
            rec["saved_eur"] -= free  # c_free is the grid part of c_exp: same seed, grid price and release
            rec["solar_gain_eur"] -= spent - free
        inv.e_kwh = e_new
        share = 1.0

        if i + 1 == len(pts):
            break
        dt_s = (pts[i + 1][0] - ts).total_seconds()
        load = house_load_w(row)
        if not 0 < dt_s <= _MAX_DT_S or load is None:
            continue
        kwh_per_w = dt_s / 3_600_000.0
        batt = float(row.get("batt_w") or 0.0)
        pv = float(row.get("pv_w") or 0.0)
        deficit = max(load - pv, 0.0) * kwh_per_w
        rec["use_kwh"] += max(load, 0.0) * kwh_per_w
        rec["solar_kwh"] += pv * kwh_per_w
        rec["price_eur_h"] += imp * dt_s / 3600.0
        rec["price_h"] += dt_s / 3600.0
        house = 0.0
        if batt < 0:
            ac_in = -batt * kwh_per_w
            solar = min(ac_in, max(pv - load, 0.0) * kwh_per_w)
            inv.c_free += (ac_in - solar) * imp
            inv.c_exp += (ac_in - solar) * imp + solar * exp
            inv.grid_kwh += ac_in - solar
            inv.sun_kwh += solar
        elif batt > 0:
            dc_out = batt * kwh_per_w
            dc_house = min(dc_out, deficit)
            _earn(rec, inv, dc_house * imp + (dc_out - dc_house) * exp)
            p1 = row.get("p1_w")
            ac_out = dc_out if p1 is None else max(load - pv - float(p1), 0.0) * kwh_per_w
            house = min(ac_out, deficit)
            share = house / ac_out if ac_out > 0 else 1.0
        if row.get("state") == "hold":
            # Held: the pack is parked, so the meter carries the house AND the
            # inverter standby. p1 is the grid cost; the derived load is not,
            # because the controller takes the standby off it on held ticks.
            rec["use_cost_eur"] += max(float(row.get("p1_w") or 0.0), 0.0) * kwh_per_w * imp
        else:
            rec["use_cost_eur"] += (deficit - house) * imp
    return _finish(out), inv


def _slots(horizon: list[dict] | None) -> list[tuple[datetime, dict, float]]:
    """Horizon rows sorted by start, each with its length in hours (from the next start)."""
    rows = sorted(
        ((start, row) for row in horizon or [] if (start := _parse(row.get("start"))) is not None),
        key=lambda p: p[0],
    )
    hours = [min((b[0] - a[0]).total_seconds() / 3600.0, 1.0) for a, b in pairwise(rows)]
    hours.append(hours[-1] if hours else 1.0)
    return [(start, row, h) for (start, row), h in zip(rows, hours, strict=False)]


def _ahead(horizon: list[dict] | None, now: datetime) -> Iterator[tuple[datetime, dict, float, float]]:
    """Plan rows not over at ``now``, with their hours and the fraction of them still ahead."""
    for start, row, h in _slots(horizon):
        end = start + timedelta(hours=h)
        if end > now and row.get("mode") != "actual":
            yield start, row, h, 1.0 if start >= now else (end - now) / (end - start)


def _battery_kwh(row: dict, delivered_kwh: float, falls: bool) -> dict[str, float]:
    """A plan row's battery kWh per action, less the grid charge already delivered in it.

    plan.py plans house use from the battery at the SoC floor too, so a row
    whose level does not fall put nothing out of it.
    """
    kwh = {action: float(row.get(col) or 0.0) for action, col in _PLAN_COLS}
    kwh["charge_grid"] = max(kwh["charge_grid"] - delivered_kwh, 0.0)
    # shortcut: the row that reaches the floor keeps its full planned output; bound it by the fall if that row matters.
    if not falls:
        kwh["house"] = kwh["export"] = 0.0
    return kwh


def _nothing_delivered(_start: datetime) -> float:
    return 0.0


def roll_plan(
    horizon: list[dict] | None,
    inv: Inventory,
    capacity_kwh: float,
    tz: tzinfo,
    now: datetime,
    export_price_at: Callable[[datetime, float | None], float | None],
    export_fee: float = 0.0,
    delivered_at: Callable[[datetime], float] = _nothing_delivered,
) -> dict[date, dict]:
    """Roll both inventories forward over the plan from ``now``.

    Same day dict as :func:`replay`. The level comes from each row's ``soc``
    (end of slot); planned grid charge adds at the slot price, solar charge at
    zero / at ``export_price_at`` (post-fee, as in ``daily_stats``; ``None``
    falls back to the import price minus the fee, as the margin does).
    ``self_discharge_kwh`` is the battery's house part, ``grid_export_kwh``
    its export part. Estimated-tail rows add use and solar only and mark the
    day ``estimated``, so its cost and saved publish as unknown.

    The slot in progress counts from ``now``: its battery columns already
    cover only that part, bar the grid charge ``delivered_at(start)`` says
    was delivered; its whole-slot load and PV are cut to the part left.
    The drift hedge's ``hedge_kwh`` is a display debit on the level, not
    drain: its share of the cost is dropped, not booked as use.
    """
    inv = replace(inv)
    out: dict[date, dict] = {}
    for start, row, h, left in _ahead(horizon, now):
        rec = _day(out, start.astimezone(tz).date())
        load = float(row.get("load_kwh") or 0.0) * left
        pv = float(row.get("pv_kwh") or 0.0) * left
        rec["use_kwh"] += load
        rec["solar_kwh"] += pv
        if row.get("estimated"):
            rec["estimated"] = True
            continue
        if row.get("price") is None or row.get("soc") is None:
            continue
        imp = float(row["price"])
        exp = export_price_at(start, imp)
        exp = imp - export_fee if exp is None else float(exp)
        e_new = float(row["soc"]) / 100.0 * capacity_kwh
        kwh = _battery_kwh(row, delivered_at(start), e_new < inv.e_kwh)
        house, export = kwh["house"], kwh["export"]
        if (hedge := float(row.get("hedge_kwh") or 0.0)) > 0:
            e_mid = max(inv.e_kwh - hedge, 0.0)
            _fall(inv, e_mid)
            inv.e_kwh = e_mid
        # Output is valued by the stock it left, before this slot's own charge joins it.
        _earn(rec, inv, house * imp + export * exp)
        inv.c_free += kwh["charge_grid"] * imp
        inv.c_exp += kwh["charge_grid"] * imp + kwh["charge_solar"] * exp
        inv.grid_kwh += kwh["charge_grid"]
        inv.sun_kwh += kwh["charge_solar"]
        if e_new < inv.e_kwh:
            free, spent = _fall(inv, e_new)
            rec["use_cost_eur"] += free * (house / (house + export) if house + export > 0 else 1.0)
            rec["saved_eur"] -= free  # c_free is the grid part of c_exp: same seed, grid price and release
            rec["solar_gain_eur"] -= spent - free
        inv.e_kwh = e_new
        # A held slot also buys the standby the pack would have paid; past hours
        # carry it from the meter instead. Already sized to this slot's modelled
        # remainder by plan.py, so it is NOT scaled by `left` again.
        rec["use_cost_eur"] += (max(load - pv - house, 0.0) + float(row.get("hold_import_kwh") or 0.0)) * imp
        rec["price_eur_h"] += imp * h * left
        rec["price_h"] += h * left
    return _finish(out)


def cheapest_window(rows: list[dict], n: int = 3) -> dict | None:
    """The cheapest ``n`` consecutive priced hours of ``rows`` still ahead (``plan``), earliest on a tie.

    ``rows`` are one day's rows of :func:`hourly_series`; ``end`` is the end of the last hour.
    """
    # The running hour counts as ahead: its row is already ``plan``.
    ahead = [(datetime.fromisoformat(r["start"]), r["price"]) for r in rows if r["plan"] and r["price"] is not None]
    best = None
    for i in range(len(ahead) - n + 1):
        win = ahead[i : i + n]
        if any(b[0] - a[0] != timedelta(hours=1) for a, b in pairwise(win)):
            continue
        total = sum(p for _, p in win)
        if best is None or total < best[0]:
            best = (total, win)
    if best is None:
        return None
    win = best[1]
    return {
        "start": win[0][0].isoformat(),
        "end": (win[-1][0] + timedelta(hours=1)).isoformat(),
        "price": round(best[0] / n, 4),
    }


def merge_days(
    actual: dict[date, dict], planned: dict[date, dict], today: date, hours: list[dict] | None = None
) -> list[dict]:
    """Yesterday (measured), today (measured so far + planned rest), tomorrow (planned).

    The grid totals are the sum of that day's rows of :func:`hourly_series`.
    """
    out = []
    for day in (today - timedelta(days=1), today, today + timedelta(days=1)):
        a = actual.get(day) if day <= today else None
        p = planned.get(day) if day >= today else None
        parts = [r for r in (a, p) if r is not None]
        priced = bool(parts) and not any(r.get("estimated") for r in parts)
        price_h = sum(r["price_h"] for r in parts)

        def total(key, parts=parts):
            return round(sum(r[key] for r in parts), 3) + 0.0

        grid = [h for h in hours or () if h["start"][:10] == day.isoformat() and h["grid_out_kwh"] is not None]
        grid = grid if priced else []

        def grid_total(*keys, grid=grid):
            return round(sum(h[k] for h in grid for k in keys), 3) if grid else None

        out.append(
            {
                "date": day.isoformat(),
                "use_kwh": total("use_kwh") if parts else None,
                "use_cost_eur": total("use_cost_eur") if priced else None,
                "saved_eur": total("saved_eur") if priced else None,
                "solar_gain_eur": total("solar_gain_eur") if priced else None,
                "avg_price": round(sum(r["price_eur_h"] for r in parts) / price_h, 4) if priced and price_h else None,
                "solar_kwh": total("solar_kwh") if parts else None,
                "planned": "none" if p is None else "partial" if a is not None else "full",
                "grid_in_kwh": grid_total("grid_in_house_kwh", "grid_in_charge_kwh"),
                "grid_out_kwh": grid_total("grid_out_kwh"),
                "cheap": cheapest_window([h for h in hours or () if h["start"][:10] == day.isoformat()])
                if priced and day >= today
                else None,
            }
        )
    return out


def _hour(hours: dict, ts: datetime, tz: tzinfo) -> dict:
    """The bucket of the local clock hour holding ``ts``, keyed by its start in UTC."""
    loc = ts.astimezone(tz)
    key = ts.astimezone(UTC) - timedelta(minutes=loc.minute, seconds=loc.second, microseconds=loc.microsecond)
    return hours.setdefault(
        key,
        {"price_eur_h": 0.0, "price_h": 0.0, "soc": None, "use_kwh": 0.0, "solar_kwh": 0.0, "held_h": 0.0, "grid": None}
        | dict.fromkeys(_ACTIONS, 0.0),
    )


def hourly_series(
    rows: list[dict],
    horizon: list[dict] | None,
    tz: tzinfo,
    now: datetime,
    delivered_at: Callable[[datetime], float] = _nothing_delivered,
) -> list[dict]:
    """One row per local hour from yesterday 00:00 local to the end of the horizon.

    Past hours come from samples, the rest from the plan from ``now`` (cut
    as in :func:`roll_plan`); the running hour merges both. ``soc`` is the level at the
    end of the hour, ``price`` the time mean. The state is the battery action
    that moved the most energy; with nothing above 0.05 kWh it is ``hold`` when
    held for over half the hour, else ``idle``. A sample is held when its
    recorded state is ``hold``, otherwise classified by ``batt_w`` with a 50 W
    dead band: discharge into an exporting meter is export, charge splits into
    solar and grid by the PV surplus as in ``margin.py``. Estimated-tail rows
    add use and solar only.

    Grid kWh: past hours split the meter's import into grid charge (the
    charge beyond the PV surplus, at most the import) and house; plan rows
    import their grid charge plus the deficit the battery does not cover, as
    :func:`roll_plan` books it, and export the battery export plus unstored
    PV. Unknown (``None``) without a meter reading, and for estimated-tail rows.
    """
    since = datetime.combine(now.astimezone(tz).date() - timedelta(days=1), time(), tzinfo=tz)
    pts = sorted(
        ((ts, row) for row in rows if (ts := _parse(row.get("ts"))) is not None and ts >= since),
        key=lambda p: p[0],
    )
    hours: dict[datetime, dict] = {}
    level = None
    for i, (ts, row) in enumerate(pts):
        b = _hour(hours, ts, tz)
        if row.get("soc") is not None:
            b["soc"] = level = float(row["soc"])
        dt_s = (pts[i + 1][0] - ts).total_seconds() if i + 1 < len(pts) else 0.0
        load = house_load_w(row)
        if not 0 < dt_s <= _MAX_DT_S or load is None:
            continue
        h = dt_s / 3600.0
        pv = float(row.get("pv_w") or 0.0)
        batt = float(row.get("batt_w") or 0.0)
        b["use_kwh"] += max(load, 0.0) * h / 1000.0
        b["solar_kwh"] += pv * h / 1000.0
        if row.get("import_price") is not None:
            b["price_eur_h"] += float(row["import_price"]) * h
            b["price_h"] += h
        if (p1 := row.get("p1_w")) is not None:
            meter = float(p1) * h / 1000.0
            charge = min(max(-batt - max(pv - load, 0.0), 0.0) * h / 1000.0, max(meter, 0.0))
            _add_grid(b, max(meter, 0.0) - charge, charge, max(-meter, 0.0))
        if row.get("state") == "hold":
            b["held_h"] += h
        elif batt < -_BAND_W:
            solar = min(-batt, max(pv - load, 0.0)) * h / 1000.0
            b["charge_solar"] += solar
            b["charge_grid"] += -batt * h / 1000.0 - solar
        elif batt > _BAND_W:
            b["export" if float(row.get("p1_w") or 0.0) < 0 else "house"] += batt * h / 1000.0

    for start, row, h, left in _ahead(horizon, now):
        b = _hour(hours, start, tz)
        load = float(row.get("load_kwh") or 0.0) * left
        pv = float(row.get("pv_kwh") or 0.0) * left
        b["use_kwh"] += load
        b["solar_kwh"] += pv
        if row.get("estimated"):
            continue
        if row.get("price") is not None:
            b["price_eur_h"] += float(row["price"]) * h * left
            b["price_h"] += h * left
        soc = None if row.get("soc") is None else float(row["soc"])
        kwh = _battery_kwh(row, delivered_at(start), soc is None or level is None or soc < level)
        if soc is not None:
            b["soc"] = level = soc
        for action in _ACTIONS:
            b[action] += kwh[action]
        _add_grid(
            b,
            max(load - pv - kwh["house"], 0.0) + float(row.get("hold_import_kwh") or 0.0),
            kwh["charge_grid"],
            kwh["export"] + max(pv - load - kwh["charge_solar"], 0.0),
        )
        if row.get("mode") == "hold":
            b["held_h"] += h * left

    out = []
    for key in sorted(hours):
        b = hours[key]
        moved = max(_ACTIONS, key=b.__getitem__)
        state = moved if b[moved] > _MOVED_KWH else "hold" if b["held_h"] > 0.5 else "idle"
        out.append(
            {
                "start": key.astimezone(tz).isoformat(),
                "price": round(b["price_eur_h"] / b["price_h"], 4) if b["price_h"] > 0 else None,
                "soc": round(b["soc"], 1) if b["soc"] is not None else None,
                "state": state,
                "use_kwh": round(b["use_kwh"], 3),
                "solar_kwh": round(b["solar_kwh"], 3),
                "plan": key + timedelta(hours=1) > now,
            }
            | dict(zip(_GRID_KEYS, [round(v, 2) for v in b["grid"]] if b["grid"] else (None,) * 3, strict=True))
        )
    return out


def _add_grid(bucket: dict, *kwh: float) -> None:
    bucket["grid"] = [a + x for a, x in zip(bucket["grid"] or (0.0,) * 3, kwh, strict=True)]


def live_state(recorded_state: str, exporting: bool, batt_w: float | None, p1_w: float | None) -> str:
    """The battery's action this tick, in the series' vocabulary."""
    if recorded_state == "forcing":
        return "charge_grid"
    if recorded_state == "disabled":
        return "disabled"
    if recorded_state == "hold":
        return "hold"
    if exporting:
        return "export"
    if batt_w is None or abs(batt_w) <= _BAND_W:
        return "idle"
    if batt_w < 0:
        return "charge_solar"
    return "export" if (p1_w or 0.0) < 0 else "house"
