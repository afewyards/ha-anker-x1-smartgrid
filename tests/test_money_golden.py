"""Exact pins on every realised-€ figure, on three fixture days (60-min, 15-min, static tariff)."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from custom_components.anker_x1_smartgrid import carddebug, const, daily_stats, margin, usecost
from custom_components.anker_x1_smartgrid.ledger import CashLedger
from custom_components.anker_x1_smartgrid.models import Config, PlantInputs, PriceSlot
from custom_components.anker_x1_smartgrid.regret import DayData, realized_grid_cost

FIXTURES = Path(__file__).parent / "fixtures"
NAMES = ["money_day_60", "money_day_15", "money_day_static"]
FEE = 0.02
CAP = 10.0


def _load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def _ledger(fx: dict, tz: ZoneInfo, width: int) -> dict:
    cfg = Config(export_fee_eur_per_kwh=FEE)
    states: dict[str, SimpleNamespace] = {}
    hass = SimpleNamespace(states=SimpleNamespace(get=states.get))
    data = {const.CONF_ENT_BATTERY_POWER: "sensor.batt"}
    slots = [PriceSlot(datetime.fromisoformat(h["start"]), h["price"], width) for h in fx["horizon"]]
    ledger = CashLedger(day=datetime.fromisoformat(fx["rows"][0]["ts"]).astimezone(tz).date().isoformat())
    for row in fx["rows"]:
        ts = datetime.fromisoformat(row["ts"])
        states["sensor.batt"] = SimpleNamespace(state=str(row["batt_w"]))
        ledger.accumulate(
            hass,
            data,
            cfg,
            ts,
            PlantInputs(soc=row["soc"], meter_w=row["p1_w"], now=ts),
            slots,
            width,
            row["export_price"],
        )
    return asdict(ledger)


def _regret(fx: dict, tz: ZoneInfo, width: int, export_priced: bool) -> dict:
    day0 = datetime.fromisoformat(fx["rows"][0]["ts"]).astimezone(tz).replace(hour=0, minute=0)
    per_day = 24 * 60 // width
    hz = [
        h for h in fx["horizon"] if day0 <= datetime.fromisoformat(h["start"]).astimezone(tz) < day0 + timedelta(days=1)
    ]
    assert len(hz) == per_day
    day = DayData(
        pv_kwh=tuple(h["pv_kwh"] for h in hz),
        load_kwh=tuple(h["load_kwh"] for h in hz),
        price=tuple(h["price"] for h in hz),
        soc_start=fx["rows"][0]["soc"],
    )
    return realized_grid_cost(
        day,
        [h["grid_charge_kwh"] for h in hz],
        Config(capacity_kwh=CAP, export_fee_eur_per_kwh=FEE),
        realized_export_by_hour=[h["grid_export_kwh"] for h in hz],
        export_price=[round(h["price"] - 0.05, 4) for h in hz] if export_priced else None,
        dt_h=width / 60,
    )


def _plain(v):
    if isinstance(v, dict):
        return {str(k): _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    return v


def figures(name: str) -> dict:
    fx = _load(name)
    tz = ZoneInfo(fx["tz"])
    now = datetime.fromisoformat(fx["now"])
    rows, horizon = fx["rows"], fx["horizon"]
    width = carddebug.slot_minutes(horizon)
    export_priced = name != "money_day_static"
    export_at = lambda _s, p: None if p is None or not export_priced else round(p - 0.05 - FEE, 4)
    today = now.astimezone(tz).date()

    by_day = margin.battery_margin_by_day(rows, CAP, tz, FEE)
    hours = usecost.hourly_series(rows, horizon, tz, now, slot_min=width, export_fee=FEE, export_price_at=export_at)
    day_start = datetime.fromisoformat(rows[0]["ts"]).astimezone(tz).replace(hour=0, minute=0)
    reference = carddebug.merge_reference(
        [(day_start.date().isoformat(), carddebug.snapshot_rows(horizon, day_start))], tz
    )
    slots = carddebug.slot_series(rows, horizon, tz, now, reference=reference, export_fee=FEE)
    actual = daily_stats.aggregate_actual_days(rows, FEE, tz)
    planned = daily_stats.aggregate_planned_days(horizon, export_at, tz, now)
    result = {
        "margin_by_day": by_day,
        "margin_total": margin.margin_total(by_day, today),
        "margin_total_value": margin.margin_total(by_day, today, key="value_eur"),
        "hourly_series": hours,
        "hour_costs": carddebug.hour_costs(slots, reference, tz, width),
        "daily_actual": actual,
        "daily_planned": planned,
        "daily_baseline": daily_stats.baseline_by_day(horizon, tz, export_at),
        "daily_table": daily_stats.merge_days(
            actual,
            planned,
            actual.get(today, daily_stats.new_day_totals()),
            today,
            baseline=daily_stats.baseline_by_day(horizon, tz, export_at),
            margin=by_day,
        ),
        "regret_realized": _regret(fx, tz, width, export_priced),
        "ledger": _ledger(fx, tz, width),
    }
    return json.loads(json.dumps(_plain(result), default=str, sort_keys=True))


@pytest.mark.parametrize("name", NAMES)
def test_money_figures_unchanged(name):
    assert figures(name) == json.loads((FIXTURES / "money_golden.json").read_text())[name]
