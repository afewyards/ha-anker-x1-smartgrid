"""Why the plan does what it does each hour: a stored kWh used now versus kept for later.

Display only, no HA imports. Values are € per stored (DC) kWh from the
callables the controller wires to the planner's own pricing.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime, timedelta, tzinfo

from .const import FIRMWARE_SOC_FLOOR
from .usecost import _MOVED_KWH, _ahead, _battery_kwh, _hour_key, _slots

_H = timedelta(hours=1)
_SUMS = ("h", "price", "deficit", "export", "eur", "house", "charge", "solar", "top", "sell_h", "sell_kwh", "under")


def hour_reasons(
    horizon: list[dict] | None,
    now: datetime,
    tz: tzinfo,
    *,
    soc: float | None,
    use_value: Callable[[float, float], float | None],
    export_value: Callable[[datetime, float], float | None],
    charge_cost: Callable[[float], float | None],
    terminal_value: float | None,
    export_price: Callable[[datetime, float], float | None],
    export_fee: float,
    eta_charge: float,
    eta_discharge: float,
    cycle_cost: float,
    charge_margin: float,
    kept_value: Callable[[datetime], float | None] | None = None,
    kept_source: Callable[[datetime], tuple[str, datetime | None] | None] | None = None,
    backup_reserve_soc: float = 0.0,
) -> dict[str, dict]:
    """``{kind, keep_eur, use_eur, kept_for, beats}`` per planned hour, keyed by the hour's
    start as :func:`usecost.hourly_series` emits it.

    ``kind`` is how the hour is priced: ``export`` (over 0.05 kWh exported),
    else ``charge`` (grid charge), else ``house`` (battery to house), else
    ``hold``; an hour that only charges from the sun gets no reason, nor one
    where the sun covers the house and the battery does nothing. Use is an
    export's ``export_value`` per slot weighted by its kWh, a charge's
    ``charge_cost``, otherwise ``use_value`` at the hour's mean price and
    deficit (load less PV, W). The level starts at ``soc``, as in
    :func:`usecost.hourly_series`; an hour that stays at or below the firmware
    floor or ``backup_reserve_soc`` holds nothing and gets no reason unless it
    grid-charges.

    A hold or charge keeps for the higher of the richest release (export, else
    house use) before the next grid-charge run, ``kept_for`` that release's run
    of hours, and, when no grid charge follows, ``terminal_value`` past the
    horizon (``next_day``). With no release before it, that run's cheapest
    ``charge_cost`` is the keep (``refill``): using it now means buying it back.
    A charge hour looks past its own run. An export or house hour, whose richer later hours
    the plan already spends, keeps for the cheapest later grid charge below
    ``terminal_value``, ``kept_for`` its run, else ``terminal_value``. An hour
    whose action the comparison contradicts gets no reason, nor any hour
    without ``terminal_value`` or a value a callable returns as ``None``.
    A hold or house hour keeps for ``kept_value`` instead, when it gives one for
    the hour's slots (their mean): the value the hold rule weighs a kept kWh at,
    and ``kept_for`` is then the slot ``kept_source`` names for it, with its price
    as ``kept_price`` and ``kept_estimated``. An export hour weighs it the same way
    unless it exceeds the sale.
    Estimated slots count as not planned.

    ``beats`` turns the comparison into the tariff the hour would have to
    reach: ``{q, price, beat_eur, parts}``, where ``price`` is the hour's raw
    tariff (``export_price`` before ``export_fee`` for ``sell``) and ``parts``
    sum to ``beat_eur``, ``conversion`` being the remainder. An export sells, a
    charge charges, a hold or house hour uses it for the house and, when the
    hour has an export price, sells; a ``sell`` the price clears in such an
    hour is ``blocked`` by the ``reserve`` when its level is at or under the
    row's ``reserve_soc``, else by a ``rule``. Each also carries ``now_eur``,
    what a stored kWh is worth used that way now, and ``now_parts``, the signed
    steps from ``price`` to it, so ``price`` plus the parts is ``now_eur``.
    """
    if terminal_value is None:
        return {}
    hours: dict[datetime, dict] = {}
    empty_soc = max(FIRMWARE_SOC_FLOOR, backup_reserve_soc)
    rows_at = {start: row for start, row, _h in _slots(horizon)}
    level, edge = soc, None
    for start, row, h, left in _ahead(horizon, now):
        if row.get("estimated"):
            continue
        before, soc = level, row.get("soc")
        kwh = _battery_kwh(row, 0.0, soc is None or level is None or soc < level)
        level = level if soc is None else soc
        edge = start + timedelta(hours=h)
        if (price := row.get("price")) is None:
            continue
        b = hours.setdefault(_hour_key(start, tz), dict.fromkeys((*_SUMS, "kept", "kept_h"), 0.0) | {"src": None})
        b["top"] = max(b["top"], *(math.inf if x is None else x for x in (before, level)))
        raw = math.nan if (x := export_price(start, price)) is None else x
        reserve = row.get("reserve_soc")
        b["under"] = max(b["under"], float(reserve is not None and level is not None and level <= reserve))
        b["h"] += h * left
        if kept_value is not None and (k := kept_value(start)) is not None:
            b["kept"] += k * h * left
            b["kept_h"] += h * left
            b["src"] = (kept_source(start) if kept_source else None) or b["src"]
        b["price"] += price * h * left
        b["sell_h"] += raw * h * left
        b["deficit"] += (float(row.get("load_kwh") or 0.0) - float(row.get("pv_kwh") or 0.0)) * left * 1000.0
        if kwh["export"] > 0.0:
            b["export"] += kwh["export"]
            sell = export_value(start, price)
            b["eur"] += math.nan if sell is None else sell * kwh["export"]
            b["sell_kwh"] += raw * kwh["export"]
        b["house"] += kwh["house"]
        b["charge"] += kwh["charge_grid"]
        b["solar"] += kwh["charge_solar"]

    keys = [k for k in sorted(hours) if hours[k]["h"] > 0.0]
    kinds, use, release, released, refill = [], [], [], [], []
    for key in keys:
        b = hours[key]
        price, deficit_w = b["price"] / b["h"], b["deficit"] / b["h"]
        house = use_value(price, deficit_w)
        moved = {k: b[k] > _MOVED_KWH for k in ("export", "charge", "house", "solar")}
        cost = charge_cost(price) if moved["charge"] else None
        if moved["export"]:
            kind, value = "export", b["eur"] / b["export"]
        elif moved["charge"]:
            kind, value = "charge", cost
        elif moved["house"]:
            kind, value = "house", house
        else:
            kind, value = None if moved["solar"] or deficit_w <= 0.0 else "hold", house
        if value is None or math.isnan(value) or (b["top"] <= empty_soc and not moved["charge"]):
            kind = None
        gives = value if moved["export"] else house if moved["house"] else None
        kinds.append(kind)
        use.append(value)
        release.append(gives)
        released.append(None if kind is None or gives is None else "export" if moved["export"] else "house")
        refill.append(cost)
    refilled = ["refill" if c is not None and c < terminal_value else None for c in refill]

    out = {}
    for i, key in enumerate(keys):
        if kinds[i] is None:
            continue
        next_day = (terminal_value, {"start": _iso(edge, tz), "end": None, "kind": "next_day"})
        releasing = kinds[i] in ("export", "house")
        if releasing:
            j = min((j for j in range(i + 1, len(keys)) if refilled[j]), key=refill.__getitem__, default=None)
            keep, kept_for = next_day if j is None else (refill[j], _run(refilled, keys, i, j, tz))
        else:
            n = len(keys)
            first = _charge_run_end(refill, keys, i) + 1 if kinds[i] == "charge" else i + 1
            stop = next((j for j in range(first, n) if refill[j] is not None), n)
            j = max((j for j in range(i + 1, stop) if released[j]), key=release.__getitem__, default=None)
            if j is not None:
                options = [(release[j], _run(released, keys, i, j, tz))]
            elif stop < n:
                z = _charge_run_end(refill, keys, stop)
                refilled_by = {"start": _iso(keys[stop], tz), "end": _iso(keys[z] + _H, tz), "kind": "refill"}
                options = [(min(refill[stop : z + 1]), refilled_by)]
            else:
                options = []
            if stop == n:
                options.append(next_day)
            keep, kept_for = max(options, key=lambda o: o[0])
        b = hours[key]
        extra = {}
        kept = b["kept"] / b["kept_h"] if b["kept_h"] > 0.0 else None
        # An export whose kept value beats the sale has sold into a later, richer sale: keep the run's own logic.
        if kept is not None and (kinds[i] in ("hold", "house") or (kinds[i] == "export" and use[i] >= kept)):
            keep = kept
            if b["src"] is not None:
                kind, at = b["src"]
                if at is None:
                    kept_for = next_day[1]
                else:
                    row = rows_at.get(at, {})
                    price = row.get("price")
                    if kind == "export" and price is not None:
                        price = export_price(at, price)
                    first = _hour_key(at, tz)
                    kept_for = {"start": _iso(first, tz), "end": _iso(first + _H, tz), "kind": kind}
                    extra = {
                        "kept_price": None if kind == "house" else price,
                        "kept_estimated": bool(row.get("estimated")),
                    }
        if use[i] >= keep if releasing else keep >= use[i]:
            price, deficit_w = b["price"] / b["h"], b["deficit"] / b["h"]
            sell_at = (keep + cycle_cost) / eta_discharge + export_fee
            selling = {"kept": keep, "wear": cycle_cost, "conversion": None, "fee": export_fee}
            sold = {"fee": -export_fee, "conversion": None, "wear": -cycle_cost}
            if kinds[i] == "export":
                raw = b["sell_kwh"] / b["export"]
                beats = [
                    _beat("sell", raw, sell_at, selling)
                    | _worth(raw, (raw - export_fee) * eta_discharge - cycle_cost, sold)
                ]
            elif kinds[i] == "charge":
                charging = {"kept": keep, "margin": -charge_margin, "conversion": None}
                beats = [
                    _beat("charge", price, eta_charge * (keep - charge_margin), charging)
                    | _worth(price, charge_cost(price), {"conversion": None, "margin": charge_margin})
                ]
            else:
                # use_value is linear in price, so its values at 0 and 1 €/kWh invert it.
                v0, v1 = use_value(0.0, deficit_w), use_value(1.0, deficit_w)
                using = {"kept": keep, "wear": cycle_cost, "conversion": None}
                beats = (
                    []
                    if v0 is None or v1 is None or v1 <= v0
                    else [
                        _beat("house", price, (keep - v0) / (v1 - v0), using)
                        | _worth(price, use_value(price, deficit_w), {"conversion": None, "wear": -cycle_cost})
                    ]
                )
                raw = b["sell_h"] / b["h"]
                beats.append(
                    _beat("sell", raw, sell_at, selling)
                    | _worth(raw, (raw - export_fee) * eta_discharge - cycle_cost, sold)
                )
                if beats[-1]["price"] > beats[-1]["beat_eur"]:
                    beats[-1]["blocked"] = "reserve" if b["under"] else "rule"
            out[_iso(key, tz)] = {
                "kind": kinds[i],
                "keep_eur": round(keep, 3),
                "use_eur": round(use[i], 3),
                "kept_for": kept_for,
                "beats": [x for x in beats if not math.isnan(x["price"])],
            } | extra
    return out


def _beat(q: str, price: float, beat: float, parts: dict[str, float | None]) -> dict:
    """A build-up whose ``None`` part is what ``beat`` leaves after the others."""
    beat = round(beat, 6)
    rest = beat - sum(round(eur, 6) for eur in parts.values() if eur is not None)
    return {
        "q": q,
        "price": round(price, 4),
        "beat_eur": beat,
        "parts": [{"key": key, "eur": round(rest if eur is None else eur, 6)} for key, eur in parts.items()],
    }


def _worth(price: float, now: float, parts: dict[str, float | None]) -> dict:
    """What a stored kWh is worth if used now, built down (or up) from ``price`` by signed ``parts``."""
    if math.isnan(price) or now is None:
        return {}
    price = round(price, 4)
    rest = now - price - sum(round(eur, 6) for eur in parts.values() if eur is not None)
    return {
        "now_eur": round(now, 6),
        "now_parts": [{"key": key, "eur": round(rest if eur is None else eur, 6)} for key, eur in parts.items()],
    }


def _run(marks: list, keys: list[datetime], i: int, j: int, tz: tzinfo) -> dict:
    """``kept_for`` over hour ``j``'s run of adjacent hours with its mark, after hour ``i``."""
    a = z = j
    while a - 1 > i and marks[a - 1] == marks[j] and keys[a - 1] + _H == keys[a]:
        a -= 1
    while z + 1 < len(keys) and marks[z + 1] == marks[j] and keys[z] + _H == keys[z + 1]:
        z += 1
    return {"start": _iso(keys[a], tz), "end": _iso(keys[z] + _H, tz), "kind": marks[j]}


def _charge_run_end(refill: list, keys: list[datetime], j: int) -> int:
    """The last hour of the run of adjacent grid-charge hours from hour ``j``."""
    while j + 1 < len(keys) and refill[j + 1] is not None and keys[j] + _H == keys[j + 1]:
        j += 1
    return j


def _iso(ts: datetime, tz: tzinfo) -> str:
    return ts.astimezone(tz).isoformat()
