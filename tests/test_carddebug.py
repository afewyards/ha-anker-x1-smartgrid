from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise
from types import SimpleNamespace
from zoneinfo import ZoneInfo

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
    assert carddebug.internals({}, cfg, None) == {
        "ceiling_soc": 100.0,
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
    )
    assert full["built_at"] == "t" and full["forecast_source"] == "remote"
    assert full["reserve_soc"] == 12.0 and full["end_value_eur_per_kwh"] == 0.2
