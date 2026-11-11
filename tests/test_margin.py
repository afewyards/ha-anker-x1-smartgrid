from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.anker_x1_smartgrid.margin import battery_margin_by_day

AMS = ZoneInfo("Europe/Amsterdam")
CAP = 20.0


def _phase(start, minutes, soc0, soc1, batt_w, price, *, pv=0.0, load=0.0, export=None):
    """One row per minute; SoC moves linearly from soc0 to soc1 over the phase."""
    return [
        {
            "ts": (start + timedelta(minutes=k)).isoformat(),
            "soc": soc0 + (soc1 - soc0) * k / minutes,
            "batt_w": batt_w,
            "pv_w": pv,
            "load_w": load,
            "import_price": price,
            "export_price": export,
        }
        for k in range(minutes)
    ]


def _end(start, soc):
    return [{"ts": start.isoformat(), "soc": soc, "batt_w": 0.0, "pv_w": 0.0, "load_w": 0.0, "import_price": 0.5}]


T0 = datetime(2026, 11, 10, 8, 0, tzinfo=UTC)
DAY = date(2026, 11, 10)


def _cycle(charge_w, discharge_w, *, pv=0.0, load_out=None, export=None):
    t1, t2 = T0 + timedelta(hours=1), T0 + timedelta(hours=2)
    return (
        _phase(T0, 60, 0, 50, -charge_w, 0.30, pv=pv, export=export)
        + _phase(t1, 60, 50, 0, discharge_w, 0.50, load=discharge_w if load_out is None else load_out, export=export)
        + _end(t2, 0)
    )


def test_lossless_cycle_earns_the_spread():
    rec = battery_margin_by_day(_cycle(10_000, 10_000), CAP, AMS)[DAY]
    assert rec["out_kwh"] == pytest.approx(10.0)
    assert rec["value_eur"] == pytest.approx(5.0)
    assert rec["cost_eur"] == pytest.approx(3.0)
    assert rec["margin_eur"] == pytest.approx(2.0)


def test_losses_raise_the_cost_per_stored_kwh():
    rec = battery_margin_by_day(_cycle(12_500, 9_000), CAP, AMS)[DAY]
    assert rec["value_eur"] == pytest.approx(4.50)
    assert rec["cost_eur"] == pytest.approx(3.75)
    assert rec["margin_eur"] == pytest.approx(0.75)


def test_overnight_energy_earns_on_the_day_it_is_used():
    # 22:00-23:00 UTC is 23:00-24:00 Amsterdam on Nov 10; discharge runs into Nov 11.
    t0 = datetime(2026, 11, 10, 22, 0, tzinfo=UTC)
    rows = (
        _phase(t0, 60, 0, 50, -10_000, 0.30)
        + _phase(t0 + timedelta(hours=1), 60, 50, 0, 10_000, 0.50, load=10_000)
        + _end(t0 + timedelta(hours=2), 0)
    )
    out = battery_margin_by_day(rows, CAP, AMS)
    assert out[date(2026, 11, 10)]["margin_eur"] == pytest.approx(0.0)
    assert out[date(2026, 11, 11)]["margin_eur"] == pytest.approx(2.0)


def test_solar_charge_costs_the_export_price():
    # 4 kW PV surplus covers 4 of the 10 kWh charged; the rest comes from the grid.
    rec = battery_margin_by_day(_cycle(10_000, 10_000, pv=4_000, export=0.10), CAP, AMS)[DAY]
    assert rec["cost_eur"] == pytest.approx(6 * 0.30 + 4 * 0.10)
    assert rec["margin_eur"] == pytest.approx(5.0 - 2.2)


def test_discharge_beyond_house_load_is_valued_at_export_price():
    rec = battery_margin_by_day(_cycle(10_000, 10_000, load_out=4_000, export=0.20), CAP, AMS)[DAY]
    assert rec["value_eur"] == pytest.approx(4 * 0.50 + 6 * 0.20)
    assert rec["margin_eur"] == pytest.approx(3.2 - 3.0)


def test_gap_over_five_minutes_contributes_nothing():
    rows = [
        {"ts": T0.isoformat(), "soc": 50, "batt_w": 10_000, "pv_w": 0, "load_w": 10_000, "import_price": 0.5},
        {
            "ts": (T0 + timedelta(minutes=10)).isoformat(),
            "soc": 40,
            "batt_w": 0,
            "pv_w": 0,
            "load_w": 0,
            "import_price": 0.5,
        },
    ]
    rec = battery_margin_by_day(rows, CAP, AMS)[DAY]
    assert rec == {"out_kwh": 0.0, "value_eur": 0.0, "cost_eur": 0.0, "margin_eur": 0.0}


def test_merge_days_publishes_margin_for_measured_days_only():
    from custom_components.anker_x1_smartgrid import daily_stats

    d0, d1, d2 = date(2026, 11, 9), date(2026, 11, 10), date(2026, 11, 11)
    m = {"out_kwh": 1.23456, "value_eur": 0.5, "cost_eur": 0.3, "margin_eur": 0.2}
    rows = daily_stats.merge_days(
        {d0: daily_stats.new_day_totals()},
        {d2: daily_stats.new_day_totals()},
        daily_stats.new_day_totals(),
        d1,
        margin={d1: m, d2: m},
    )
    by_day = {r["date"]: r for r in rows}
    assert by_day[d0.isoformat()]["battery_margin_eur"] is None
    assert by_day[d1.isoformat()]["battery_out_kwh"] == 1.235
    assert by_day[d1.isoformat()]["battery_margin_eur"] == 0.2
    assert by_day[d2.isoformat()]["battery_margin_eur"] is None
