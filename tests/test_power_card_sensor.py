from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.anker_x1_smartgrid import usecost
from custom_components.anker_x1_smartgrid.sensor import X1PowerCardSensor

NOW = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)


class _Stub:
    def __init__(self, card):
        self.power_card = card


def _samples(hours=30):
    t0 = NOW - timedelta(hours=hours)
    return [
        {
            "ts": (t0 + timedelta(minutes=k)).isoformat(),
            "soc": 50.0 - k * 0.01,
            "batt_w": 400.0,
            "pv_w": 0.0,
            "load_w": 400.0,
            "p1_w": 0.0,
            "import_price": 0.25,
            "export_price": 0.10,
            "state": "passive",
        }
        for k in range(hours * 60)
    ]


def _horizon(hours=39):
    return [
        {
            "start": (NOW + timedelta(minutes=15 * k)).isoformat(),
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
        for k in range(hours * 4)
    ]


def test_sensor_publishes_the_three_attributes_unrecorded():
    card = {"built_at": NOW.isoformat(), "battery": {"soc": 50}, "days": [], "hours": []}
    sensor = X1PowerCardSensor(_Stub(card), "entry")
    assert sensor.native_value == NOW.isoformat()
    assert sensor.extra_state_attributes == {"battery": {"soc": 50}, "days": [], "hours": []}
    assert X1PowerCardSensor._unrecorded_attributes == frozenset({"battery", "days", "hours"})


def test_sensor_without_a_card_publishes_nothing():
    sensor = X1PowerCardSensor(_Stub(None), "entry")
    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {"battery": None, "days": None, "hours": None}


def _rebuild(horizon=None):
    return lambda: (horizon or _horizon(), lambda _s, _p: 0.08, lambda _s: 0.0)


def _inputs(soc):
    return SimpleNamespace(soc=soc, meter_w=0.0)


async def test_controller_builds_the_card_from_one_sample_read():
    from tests.helpers import make_controller

    ctrl, _act = make_controller()
    reads = []
    rows = _samples()
    ctrl._recorder.read_feature_rows = lambda since: reads.append(since) or rows
    await ctrl._refresh_battery_margin(NOW)
    await ctrl._publish_power_card(NOW, _inputs(48.0), "hold", False, _rebuild())

    assert len(reads) == 1
    card = ctrl.power_card
    assert card["built_at"] == NOW.isoformat()
    battery = card["battery"]
    assert battery["state"] == "hold"
    assert battery["soc"] == 48.0
    assert battery["stored_kwh"] == round(0.48 * ctrl.cfg.capacity_kwh, 2)
    assert battery["stored_cost_eur_per_kwh"] == 0.25
    assert battery["margin_30d_eur"] is None
    assert [d["planned"] for d in card["days"]] == ["none", "partial", "full"]
    today = card["days"][1]
    grid_in = sum(
        h["grid_in_house_kwh"] + h["grid_in_charge_kwh"] for h in card["hours"] if h["start"][:10] == today["date"]
    )
    assert today["grid_in_kwh"] == pytest.approx(grid_in) and grid_in > 0
    assert card["hours"][0]["plan"] is False and card["hours"][-1]["plan"] is True

    # A later tick without a fresh sample read keeps the series and refreshes the live line.
    days = card["days"]
    await ctrl._publish_power_card(NOW + timedelta(minutes=1), _inputs(47.5), "forcing", False, _rebuild())
    assert ctrl.power_card["days"] is days
    assert ctrl.power_card["battery"]["state"] == "charge_grid"
    assert ctrl.power_card["battery"]["soc"] == 47.5


@pytest.mark.parametrize("stage", ["replay", "roll_plan", "merge_days", "hourly_series"])
async def test_a_failing_build_clears_the_card_and_warns_once(monkeypatch, caplog, stage):
    from tests.helpers import make_controller

    ctrl, _act = make_controller()
    ctrl._recorder.read_feature_rows = lambda since: _samples(2)
    ctrl.power_card = {"built_at": "x", "battery": {}, "days": [], "hours": []}

    def _boom(*_a, **_k):
        raise RuntimeError("unhappy")

    monkeypatch.setattr(usecost, stage, _boom)
    with caplog.at_level(logging.WARNING):
        for minutes in (0, 6):
            await ctrl._refresh_battery_margin(NOW + timedelta(minutes=minutes))
            await ctrl._publish_power_card(NOW + timedelta(minutes=minutes), _inputs(48.0), "idle", False, _rebuild())
    assert ctrl.power_card is None
    assert ctrl._battery_margin is not None, "the margin does not depend on the card"
    assert sum("power card" in r.getMessage() for r in caplog.records) == 1


def test_payload_fits_the_16_kb_attribute_budget():
    hour = {
        "start": "2026-11-12T00:00:00+01:00",
        "price": 0.2345,
        "soc": 55.5,
        "state": "charge_solar",
        "use_kwh": 1.234,
        "solar_kwh": 2.345,
        "plan": False,
        "grid_in_house_kwh": 1.23,
        "grid_in_charge_kwh": 2.35,
        "grid_out_kwh": 3.46,
    }
    day = {
        "date": "2026-11-12",
        "use_kwh": 12.345,
        "use_cost_eur": 2.345,
        "saved_eur": 0.456,
        "avg_price": 0.2345,
        "solar_kwh": 3.456,
        "planned": "partial",
        "grid_in_kwh": 12.345,
        "grid_out_kwh": 3.456,
    }
    battery = {
        "soc": 55.5,
        "stored_kwh": 11.37,
        "stored_cost_eur_per_kwh": 0.1987,
        "state": "charge_solar",
        "margin_30d_eur": 12.345,
    }
    card = {"built_at": NOW.isoformat(), "battery": battery, "days": [day] * 3, "hours": [hour] * 72}
    sensor = X1PowerCardSensor(_Stub(card), "entry")
    # Home Assistant serialises attributes compactly (orjson).
    assert len(json.dumps(sensor.extra_state_attributes, separators=(",", ":"))) < 16_000


async def test_a_tick_publishes_the_card(monkeypatch):
    from tests.helpers import StubHass
    from tests.test_controller_export_executor import BASE, _make_controller, _seed_passive_inputs, ctrl_mod

    monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
    hass = StubHass()
    ctrl, _act, _ = _make_controller(hass)
    _seed_passive_inputs(hass, soc="50.0", export_price="0.10")
    await ctrl.tick()

    card = ctrl.power_card
    assert card is not None and card["built_at"] == BASE.isoformat()
    assert card["battery"]["soc"] == 50.0
    assert card["battery"]["state"] in {"charge_grid", "charge_solar", "house", "export", "hold", "idle"}
    assert len(card["days"]) == 3


def _tick_setup(monkeypatch, *, enabled=True, seed=True):
    from tests.helpers import StubHass
    from tests.test_controller_export_executor import BASE, _make_controller, _seed_passive_inputs, ctrl_mod

    monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
    hass = StubHass()
    ctrl, act, _ = _make_controller(hass)
    ctrl.enabled = enabled
    if seed:
        _seed_passive_inputs(hass, soc="50.0", export_price="0.10")
    return ctrl, act, ctrl_mod


async def test_a_failing_card_build_never_fails_the_tick(monkeypatch):
    ctrl, _act, _ = _tick_setup(monkeypatch)

    def _boom(*_a, **_k):
        raise RuntimeError("unhappy")

    monkeypatch.setattr(usecost, "hourly_series", _boom)
    status = await ctrl.tick()
    assert status["state"] != "failsafe"
    assert ctrl.power_card is None


async def test_the_card_is_built_after_the_tick_actuates(monkeypatch):
    ctrl, _act, ctrl_mod = _tick_setup(monkeypatch)
    order = []
    actuate, build = ctrl_mod.executor.run_forcing_and_export, ctrl._build_power_card_sync

    async def _actuate(*a, **k):
        order.append("actuate")
        return await actuate(*a, **k)

    def _build(*a, **k):
        order.append("card")
        return build(*a, **k)

    monkeypatch.setattr(ctrl_mod.executor, "run_forcing_and_export", _actuate)
    monkeypatch.setattr(ctrl, "_build_power_card_sync", _build)
    await ctrl.tick()
    assert order == ["actuate", "card"]


def _stale_card():
    battery = {"soc": 10.0, "stored_kwh": 1.0, "stored_cost_eur_per_kwh": 0.2, "state": "house", "margin_30d_eur": None}
    return {"built_at": "x", "stored_cost_eur_per_kwh": 0.2, "battery": battery, "days": [], "hours": []}


async def test_a_disabled_tick_keeps_the_battery_line_live(monkeypatch):
    ctrl, _act, _ = _tick_setup(monkeypatch, enabled=False)
    ctrl.power_card = _stale_card()
    await ctrl.tick()
    battery = ctrl.power_card["battery"]
    assert (battery["soc"], battery["state"]) == (50.0, "idle")
    assert battery["stored_kwh"] == round(0.5 * ctrl.cfg.capacity_kwh, 2)


async def test_a_failsafe_tick_without_inputs_marks_the_battery_line_unknown(monkeypatch):
    ctrl, _act, _ = _tick_setup(monkeypatch, seed=False)
    ctrl.power_card = _stale_card()
    await ctrl.tick()
    assert ctrl.power_card["battery"]["state"] is None and ctrl.power_card["battery"]["soc"] is None
