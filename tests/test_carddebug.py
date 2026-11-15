from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from custom_components.anker_x1_smartgrid import carddebug

NOW = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)
AMS = ZoneInfo("Europe/Amsterdam")


def _samples(hours=6, **extra):
    t0 = NOW - timedelta(hours=hours)
    return [
        {
            "ts": (t0 + timedelta(minutes=k)).isoformat(),
            "soc": 50.0,
            "batt_w": 400.0,
            "pv_w": 0.0,
            "load_w": 400.0,
            "p1_w": 0.0,
            "import_price": 0.25,
            "export_price": 0.10,
            "state": "passive",
        }
        | extra
        for k in range(hours * 60)
    ]


def _horizon(step=15, hours=6, start=NOW, **extra):
    return [
        {
            "start": (start + timedelta(minutes=step * k)).isoformat(),
            "price": 0.25,
            "soc": 40.0,
            "load_kwh": 0.1,
            "pv_kwh": 0.0,
            "grid_charge_kwh": 0.0,
            "solar_charge_kwh": 0.0,
            "self_discharge_kwh": 0.0,
            "grid_export_kwh": 0.0,
            "mode": "idle",
            "estimated": False,
        }
        | extra
        for k in range(hours * 60 // step)
    ]


def _delivered(_start):
    return 0.0


def test_slot_minutes():
    assert carddebug.slot_minutes(_horizon(15)) == 15
    assert carddebug.slot_minutes(_horizon(60)) == 60
    assert carddebug.slot_minutes(None) == 60
    assert carddebug.slot_minutes(_horizon(15)[:1]) == 60
    assert carddebug.slot_minutes(_horizon(45)) == 60
    assert carddebug.slot_minutes(_horizon(120, hours=12)) == 60
    rows = _horizon(15)
    assert carddebug.slot_minutes([rows[0], rows[0], *rows[1:]]) == 60


def test_snapshot_rows_skip_estimated_and_past():
    rows = _horizon(60, hours=4, start=NOW - timedelta(hours=2))
    rows[0]["mode"] = rows[1]["mode"] = "actual"
    rows[3]["estimated"] = True
    out = carddebug.snapshot_rows(rows, NOW)
    assert [r["start"] for r in out] == [rows[2]["start"]]
    assert set(out[0]) == set(carddebug.SNAPSHOT_KEYS)


def test_merge_reference_own_day_wins():
    a = _horizon(15, hours=24, start=datetime(2026, 11, 13, 20, 0, tzinfo=UTC))
    b = _horizon(15, hours=6, start=datetime(2026, 11, 14, 5, 0, tzinfo=UTC))
    for r in a:
        r["soc"] = 1.0
    for r in b:
        r["soc"] = 2.0
    snaps = [
        ("2026-11-13", carddebug.snapshot_rows(a, NOW - timedelta(days=1))),
        ("2026-11-14", carddebug.snapshot_rows(b, NOW - timedelta(days=1))),
    ]
    out = {r["start"]: r["soc"] for r in carddebug.merge_reference(snaps, AMS)}
    assert out[datetime(2026, 11, 14, 4, 45, tzinfo=UTC).isoformat()] == 1.0
    assert out[datetime(2026, 11, 14, 5, 0, tzinfo=UTC).isoformat()] == 2.0
    starts = [r["start"] for r in carddebug.merge_reference(snaps, AMS)]
    assert len(starts) == len(set(starts)) and starts == sorted(starts)


def test_merge_reference_empty():
    assert carddebug.merge_reference([], AMS) == []


def test_slot_series_hourly_tariff_one_row_per_hour():
    out = carddebug.slot_series(_samples(), _horizon(60), AMS, NOW, _delivered)
    starts = [datetime.fromisoformat(r["start"]) for r in out]
    assert all(b - a == timedelta(hours=1) for a, b in pairwise(starts))


def test_slot_series_setpoint_and_battery_sign():
    rows = _samples(setpoint_w=3000.0, batt_w=-2900.0, export_setpoint_w=0.0)
    past = carddebug.slot_series(rows, _horizon(15), AMS, NOW, _delivered)[0]
    assert past["setpoint_w"] == 3000 and past["batt_w"] == 2900
    rows = _samples(setpoint_w=0.0, export_setpoint_w=4000.0)
    past = carddebug.slot_series(rows, _horizon(15), AMS, NOW, _delivered)[0]
    assert past["setpoint_w"] == -4000
    plan = carddebug.slot_series([], _horizon(15, grid_export_w=4000.0), AMS, NOW, _delivered)
    row = next(r for r in plan if r["plan"])
    assert row["setpoint_w"] == -4000 and row["batt_w"] is None


def test_slot_series_gap_gives_none():
    lone = NOW - timedelta(hours=3)
    rows = [r for r in _samples() if not lone <= datetime.fromisoformat(r["ts"]) < lone + timedelta(hours=1)]
    rows.append({**_samples(1)[0], "ts": (lone + timedelta(minutes=10)).isoformat()})
    out = carddebug.slot_series(rows, _horizon(60), AMS, NOW, _delivered)
    gap = next(r for r in out if datetime.fromisoformat(r["start"]) == lone)
    assert gap["setpoint_w"] is None and gap["batt_w"] is None


def test_slot_series_skips_samples_without_house_load():
    rows = _samples(setpoint_w=3000.0, batt_w=-2900.0)
    bad = {"load_w": None, "p1_w": None, "pv_w": None}
    start = NOW - timedelta(hours=3)
    for r in rows:
        if start <= datetime.fromisoformat(r["ts"]) < start + timedelta(minutes=30):
            r.update(bad, setpoint_w=0.0, batt_w=0.0)
    out = carddebug.slot_series(rows, _horizon(60), AMS, NOW, _delivered)
    row = next(r for r in out if datetime.fromisoformat(r["start"]) == start)
    assert row["setpoint_w"] == 3000 and row["batt_w"] == 2900


def test_slot_series_dst():
    now = datetime(2026, 10, 25, 3, 0, tzinfo=UTC)
    t0 = now - timedelta(hours=8)
    rows = [{**_samples(1)[0], "ts": (t0 + timedelta(minutes=k)).isoformat()} for k in range(8 * 60)]
    horizon = _horizon(15, hours=4, start=now)
    out = carddebug.slot_series(rows, horizon, AMS, now, _delivered)
    starts = [r["start"] for r in out]
    assert len(starts) == len(set(starts))


def test_internals_omits_missing():
    cfg = SimpleNamespace(
        cycle_cost_eur_per_kwh=0.03, charge_margin_eur_per_kwh=0.0, idle_drain_w=100.0, soc_target=100.0
    )
    assert carddebug.internals({}, cfg, None, {}) == {
        "target_soc": 100.0,
        "cycle_cost_eur_per_kwh": 0.03,
        "charge_margin_eur_per_kwh": 0.0,
        "idle_drain_w": 100.0,
    }
    full = carddebug.internals(
        {
            "active_model": "remote",
            "load_adapt_ratio": 0.9,
            "hold_stored_value_eur_per_kwh": None,
            "terminal_v_hi": 0.2,
            "plan": {"horizon": [{"mode": "actual", "reserve_soc": 9.0}, {"mode": "idle", "reserve_soc": 12.0}]},
        },
        cfg,
        "t",
        {"eta_charge": 0.93},
    )
    assert full["built_at"] == "t" and full["forecast_source"] == "remote"
    assert full["reserve_soc"] == 12.0 and full["end_value_eur_per_kwh"] == 0.2 and full["eta_charge"] == 0.93


def _past_reference(hours=6, now=NOW):
    rows = _horizon(15, hours=hours, start=now - timedelta(hours=hours))
    for k, r in enumerate(rows):
        r["load_kwh"] = 0.1 + k * 0.001
    return rows


def _by_start(out):
    return {datetime.fromisoformat(r["start"]): r for r in out}


def test_slot_series_prices_the_plan_and_the_measured_grid_at_the_slots_own_prices():
    ref = _past_reference()
    out = carddebug.slot_series(_samples(p1_w=600.0), _horizon(15), AMS, NOW, _delivered, ref, 0.02)
    slots = _by_start(out)
    for r in ref:
        slot = slots[datetime.fromisoformat(r["start"])]
        assert slot["export_price"] == 0.08
        assert slot["plan_eur"] == round(r["load_kwh"] * 0.25, 4)
        grid_in = slot["grid_in_house_kwh"] + slot["grid_in_charge_kwh"]
        assert grid_in > 0 and slot["real_eur"] == round(grid_in * 0.25 - slot["grid_out_kwh"] * 0.08, 4)
    assert all(s["plan_eur"] is None and s["real_eur"] is None for s in out if s["plan"])


def test_slot_series_credits_planned_and_measured_export_after_the_fee():
    ref = _past_reference(1)
    for r in ref:
        r["grid_export_kwh"] = 0.2
    rows = _samples(1, p1_w=-400.0, export_price=0.12)
    slot = carddebug.slot_series(rows, _horizon(15), AMS, NOW, _delivered, ref, 0.02)[0]
    assert slot["export_price"] == 0.1
    assert slot["plan_eur"] == round(ref[0]["load_kwh"] * 0.25 - 0.2 * 0.1, 4)
    assert slot["real_eur"] == pytest.approx(-0.1 * 0.1, abs=1e-3)


def test_slot_costs_are_none_on_any_missing_side():
    ref = _past_reference()
    gone = datetime.fromisoformat(ref[0]["start"])
    ref = ref[1:]
    no_meter = NOW - timedelta(hours=3)
    rows = [
        r | {"p1_w": None} if no_meter <= datetime.fromisoformat(r["ts"]) < no_meter + timedelta(minutes=15) else r
        for r in _samples(p1_w=600.0)
    ]
    slots = _by_start(carddebug.slot_series(rows, _horizon(15), AMS, NOW, _delivered, ref, 0.02))
    for start in (gone, no_meter):
        assert slots[start]["plan_eur"] is None and slots[start]["real_eur"] is None
    assert slots[gone + timedelta(minutes=15)]["plan_eur"] is not None
    assert carddebug.slot_series(rows, _horizon(15), AMS, NOW, _delivered)[0]["plan_eur"] is None


def test_slot_costs_need_an_export_price_only_when_something_is_exported():
    ref = _past_reference(1)
    rows = _samples(1, p1_w=600.0, export_price=None)
    slot = carddebug.slot_series(rows, _horizon(15), AMS, NOW, _delivered, ref, 0.02)[0]
    assert slot["export_price"] is None and slot["plan_eur"] is not None and slot["real_eur"] is not None
    ref[0]["grid_export_kwh"] = 0.2
    slot = carddebug.slot_series(rows, _horizon(15), AMS, NOW, _delivered, ref, 0.02)[0]
    assert slot["plan_eur"] is None and slot["real_eur"] is None


def test_slot_costs_on_a_fall_back_day_keep_each_hour_its_own_plan():
    now = datetime(2026, 10, 25, 3, 0, tzinfo=UTC)
    t0 = now - timedelta(hours=4)
    rows = [{**_samples(1)[0], "ts": (t0 + timedelta(minutes=k)).isoformat(), "p1_w": 600.0} for k in range(4 * 60)]
    ref = _past_reference(4, now)
    out = carddebug.slot_series(rows, _horizon(15, hours=4, start=now), AMS, now, _delivered, ref, 0.02)
    slots = _by_start(out)
    assert len([s for s in out if s["start"].startswith("2026-10-25T02:")]) == 8
    for r in ref:
        assert slots[datetime.fromisoformat(r["start"])]["plan_eur"] == round(r["load_kwh"] * 0.25, 4)


def _cfg():
    return SimpleNamespace(
        cycle_cost_eur_per_kwh=0.03, charge_margin_eur_per_kwh=0.0, idle_drain_w=100.0, soc_target=100.0
    )


def test_internals_say_how_fresh_the_inputs_are():
    horizon = _horizon(60, hours=3)
    horizon[2]["estimated"] = True
    rows = _samples(1)
    rows[-1]["soc"] = rows[-2]["soc"] = None
    out = carddebug.internals(
        {"plan": {"horizon": horizon}}, _cfg(), NOW.isoformat(), {}, rows, NOW - timedelta(minutes=40)
    )
    assert out["prices_until"] == (NOW + timedelta(hours=2)).isoformat()
    assert out["tomorrow_estimated"] is True
    assert out["battery_sample_age_s"] == 180
    assert out["pv_forecast_age_s"] == 2400
    out = carddebug.internals({"plan": {"horizon": horizon[:2]}}, _cfg(), NOW.isoformat(), {}, rows)
    assert out["tomorrow_estimated"] is False and "pv_forecast_age_s" not in out
