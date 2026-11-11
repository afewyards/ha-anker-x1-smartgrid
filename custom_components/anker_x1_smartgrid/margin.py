"""Per-day battery margin from recorded samples (display only, no HA imports)."""

from __future__ import annotations

from datetime import date, timedelta, tzinfo

from .daily_stats import _parse
from .dataquality import house_load_w

_MAX_DT_S = 300.0
_EMPTY_KWH = 0.01


def battery_margin_by_day(
    rows: list[dict],
    capacity_kwh: float,
    tz: tzinfo,
    export_fee_eur_per_kwh: float = 0.0,
) -> dict[date, dict]:
    """What the battery earned per local day, on weighted-average-cost inventory.

    Rows are recorder ``samples`` dicts, replayed in ``ts`` order; rows without
    ``ts``, ``soc`` or ``import_price`` are dropped. Each row's powers hold until
    the next row; an interval longer than 5 minutes contributes nothing.

    - Inventory: stored kWh ``E = soc/100 x capacity`` and its cost ``C`` (€).
      Seeded at the first row as ``E x import_price``; that guess washes out once
      the pack has cycled, so callers replay a warm-up before the reported days.
    - Charging (``batt_w < 0``): the AC kWh in is split into a solar part (up to
      the PV surplus over house load, valued at the export price it gave up) and
      a grid part (import price). Both are added to ``C``.
    - Discharging (``batt_w > 0``): the AC kWh out is split into a house part (up
      to the load PV does not cover, valued at the import price) and an export
      part (export price). Booked as the day's ``out_kwh`` / ``value_eur``.
    - Cost follows the SoC, so real losses land in it: when ``E`` falls, the
      removed kWh carry ``C/E`` each into the day's ``cost_eur``; when ``E`` rises
      ``C`` is unchanged, so charge losses raise the cost per stored kWh. Across a
      gap the inventory is re-read at unchanged cost per kWh and nothing is booked.

    Export price is the raw feed-in tariff minus ``export_fee_eur_per_kwh``, the
    import price when the row has none. ``margin_eur = value_eur - cost_eur``.
    Energy charged one day and used the next earns its margin on the second day.
    """
    pts = []
    for row in rows:
        ts = _parse(row.get("ts"))
        if ts is not None and row.get("soc") is not None and row.get("import_price") is not None:
            pts.append((ts, row))
    pts.sort(key=lambda p: p[0])

    out: dict[date, dict] = {}
    e = c = 0.0
    for i, (ts, row) in enumerate(pts):
        rec = out.setdefault(ts.astimezone(tz).date(), {"out_kwh": 0.0, "value_eur": 0.0, "cost_eur": 0.0})
        imp = float(row["import_price"])
        raw_exp = row.get("export_price")
        exp = (imp if raw_exp is None else float(raw_exp)) - export_fee_eur_per_kwh
        e_new = float(row["soc"]) / 100.0 * capacity_kwh
        if i == 0:
            c = e_new * imp
        elif (ts - pts[i - 1][0]).total_seconds() > _MAX_DT_S:
            c = c * e_new / e if e > _EMPTY_KWH else e_new * imp
        elif e_new < e:
            moved = c if e_new <= _EMPTY_KWH else c * (e - e_new) / e
            rec["cost_eur"] += moved
            c -= moved
        e = e_new

        if i + 1 == len(pts):
            break
        dt_s = (pts[i + 1][0] - ts).total_seconds()
        load = house_load_w(row)
        if not 0 < dt_s <= _MAX_DT_S or load is None:
            continue
        kwh_per_w = dt_s / 3_600_000.0
        batt = float(row.get("batt_w") or 0.0)
        pv = float(row.get("pv_w") or 0.0)
        if batt < 0:
            ac_in = -batt * kwh_per_w
            solar = min(ac_in, max(pv - load, 0.0) * kwh_per_w)
            c += (ac_in - solar) * imp + solar * exp
        elif batt > 0:
            ac_out = batt * kwh_per_w
            house = min(ac_out, max(load - pv, 0.0) * kwh_per_w)
            rec["out_kwh"] += ac_out
            rec["value_eur"] += house * imp + (ac_out - house) * exp

    for rec in out.values():
        rec["margin_eur"] = rec["value_eur"] - rec["cost_eur"]
    return out


def margin_total(by_day: dict[date, dict] | None, today: date, days: int = 30) -> tuple[float | None, int | None]:
    """Sum of ``margin_eur`` over the ``days`` local days ending ``today``, and how many had samples."""
    if not by_day:
        return None, None
    have = [by_day[d] for d in (today - timedelta(n) for n in range(days)) if d in by_day]
    if not have:
        return None, None
    return round(sum(r["margin_eur"] for r in have), 3), len(have)
