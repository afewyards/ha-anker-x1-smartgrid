from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.anker_x1_smartgrid import usecost
from custom_components.anker_x1_smartgrid.sensor import X1PowerCardSensor
from tests.helpers import card_inputs, stale_card

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


async def test_controller_builds_the_card_from_one_sample_read():
    from tests.helpers import make_controller

    ctrl, _act = make_controller()
    reads = []
    rows = _samples()
    ctrl._recorder.read_feature_rows = lambda since: reads.append(since) or rows
    await ctrl._refresh_battery_margin(NOW)
    await ctrl._publish_power_card(NOW, card_inputs(48.0), "hold", False, _rebuild())

    assert len(reads) == 1
    card = ctrl.power_card
    assert card["built_at"] == NOW.isoformat()
    battery = card["battery"]
    assert battery["state"] == "hold"
    assert battery["soc"] == 48.0
    assert battery["stored_kwh"] == round(0.48 * ctrl.cfg.capacity_kwh, 2)
    assert battery["stored_cost_eur_per_kwh"] == 0.25
    assert battery["margin_30d_eur"] is None and battery["solar_gain_30d_eur"] is None
    assert [d["planned"] for d in card["days"]] == ["none", "partial", "full"]
    today = card["days"][1]
    grid_in = sum(
        h["grid_in_house_kwh"] + h["grid_in_charge_kwh"] for h in card["hours"] if h["start"][:10] == today["date"]
    )
    assert today["grid_in_kwh"] == pytest.approx(grid_in) and grid_in > 0
    assert today["grid_bill_eur"] == round(today["grid_bought_eur"] - today["grid_sold_eur"], 2)
    assert today["grid_bought_eur"] > 0
    assert not any("grid_bought_eur" in h or "grid_sold_eur" in h for h in card["hours"])
    assert card["hours"][0]["plan"] is False and card["hours"][-1]["plan"] is True

    # A later tick without a fresh sample read keeps the series and refreshes the live line.
    days = card["days"]
    await ctrl._publish_power_card(NOW + timedelta(minutes=1), card_inputs(47.5), "forcing", False, _rebuild())
    assert ctrl.power_card["days"] is days
    assert ctrl.power_card["battery"]["state"] == "charge_grid"
    assert ctrl.power_card["battery"]["soc"] == 47.5


def _charge_then_export(export=0.60):
    horizon = _horizon()
    for k in range(4):
        horizon[k] |= {"mode": "grid", "grid_charge_kwh": 1.0, "soc": 45.0 + 5 * k}
        horizon[4 + k] |= {"mode": "export", "grid_export_kwh": 0.5, "soc": 55.0 - 5 * k}
    return lambda: (horizon, lambda _s, _p: export, lambda _s: 0.0)


async def _card_with_reasons(terminal_v_hi, hold_stored=None, export=0.60):
    from tests.helpers import make_controller

    ctrl, _act = make_controller()
    ctrl._recorder.read_feature_rows = lambda since: _samples()
    ctrl.last_status["terminal_v_hi"] = terminal_v_hi
    ctrl.last_status["hold_stored_value_eur_per_kwh"] = hold_stored
    await ctrl._refresh_battery_margin(NOW)
    await ctrl._publish_power_card(NOW, card_inputs(48.0), "hold", False, _charge_then_export(export))
    return ctrl, ctrl.power_card["hours"]


async def test_planned_hours_carry_the_planners_reason():
    from custom_components.anker_x1_smartgrid import optimize

    ctrl, hours = await _card_with_reasons(0.30)
    cfg, curve = ctrl.cfg, ctrl._planner_curve()
    sell = 0.60 * optimize.eta_discharge_at(cfg.max_export_w, cfg, curve) - cfg.cycle_cost_eur_per_kwh
    cost = 0.25 / optimize.eta_charge_at(cfg.max_charge_w, cfg, curve) + cfg.charge_margin_eur_per_kwh
    charge, export, *rest = [h for h in hours if h["plan"]]
    assert all("why" not in h for h in hours if not h["plan"])
    assert charge["why"] == {
        "kind": "charge",
        "keep_eur": round(sell, 3),
        "use_eur": round(cost, 3),
        "kept_for": {"start": export["start"], "end": rest[0]["start"], "kind": "export"},
    }
    assert export["why"]["use_eur"] == round(sell, 3) and export["why"]["kept_for"]["kind"] == "next_day"
    use = round(optimize.discharge_value(0.25, 400.0, cfg, curve), 3)
    held = {"kind": "hold", "keep_eur": 0.3, "use_eur": use, "kept_for": export["why"]["kept_for"]}
    assert all(h["why"] == held for h in rest)


async def test_the_build_ups_price_the_kept_value_with_the_planners_figures(monkeypatch):
    from custom_components.anker_x1_smartgrid import optimize, why

    built = []
    real = why.hour_reasons
    monkeypatch.setattr(why, "hour_reasons", lambda *a, **k: built.append(real(*a, **k)) or built[-1])
    ctrl, hours = await _card_with_reasons(0.30)
    cfg, curve = ctrl.cfg, ctrl._planner_curve()
    eta_d = optimize.eta_discharge_at(cfg.max_export_w, cfg, curve)
    eta_c = optimize.eta_charge_at(cfg.max_charge_w, cfg, curve)
    fee, wear = cfg.export_fee_eur_per_kwh, cfg.cycle_cost_eur_per_kwh
    rows = {r["kind"]: r for r in reversed(built[-1].values())}
    (charge,), (sell,), (house, _) = (rows[k]["beats"] for k in ("charge", "export", "hold"))
    assert sell["price"] == round(0.60 + fee, 4)
    assert (sell["beat_eur"] - fee) * eta_d - wear == pytest.approx(sell["parts"][0]["eur"], abs=1e-6)
    assert charge["beat_eur"] / eta_c + cfg.charge_margin_eur_per_kwh == pytest.approx(
        charge["parts"][0]["eur"], abs=1e-6
    )
    assert optimize.discharge_value(house["beat_eur"], 400.0, cfg, curve) == pytest.approx(
        house["parts"][0]["eur"], abs=1e-6
    )
    for b, kind in ((sell, "export"), (charge, "charge"), (house, "hold")):
        assert b["parts"][0]["eur"] == pytest.approx(rows[kind]["keep_eur"], abs=5e-4)
    assert not any("beats" in h.get("why", {}) for h in hours)


async def test_the_kept_value_is_the_one_the_hold_compares_with():
    _ctrl, hours = await _card_with_reasons(0.30, hold_stored=0.35)
    _charge, export, hold, *_ = [h for h in hours if h["plan"]]
    assert export["why"]["keep_eur"] == hold["why"]["keep_eur"] == 0.35


async def test_no_terminal_value_leaves_the_hours_without_reasons():
    _ctrl, hours = await _card_with_reasons(None)
    assert not any("why" in h for h in hours)


async def test_an_export_hour_without_an_export_price_has_no_reason():
    ctrl, hours = await _card_with_reasons(0.30, export=None)
    _charge, export, *_ = [h for h in hours if h["plan"]]
    assert export["state"] == "export" and "why" not in export
    assert len(ctrl.power_card["days"]) == 3


async def test_failing_reasons_leave_the_card_and_warn_once(monkeypatch, caplog):
    from custom_components.anker_x1_smartgrid import why
    from tests.helpers import make_controller

    ctrl, _act = make_controller()
    ctrl._recorder.read_feature_rows = lambda since: _samples()
    ctrl.last_status["terminal_v_hi"] = 0.30

    def _boom(*_a, **_k):
        raise RuntimeError("unhappy")

    monkeypatch.setattr(why, "hour_reasons", _boom)
    with caplog.at_level(logging.WARNING):
        for minutes in (0, 6):
            await ctrl._refresh_battery_margin(NOW + timedelta(minutes=minutes))
            await ctrl._publish_power_card(
                NOW + timedelta(minutes=minutes), card_inputs(48.0), "idle", False, _rebuild()
            )
    card = ctrl.power_card
    assert len(card["days"]) == 3 and card["hours"] and not any("why" in h for h in card["hours"])
    assert sum("power card" in r.getMessage() for r in caplog.records) == 1


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
            await ctrl._publish_power_card(
                NOW + timedelta(minutes=minutes), card_inputs(48.0), "idle", False, _rebuild()
            )
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
        "solar_gain_eur": 0.123,
        "avg_price": 0.2345,
        "solar_kwh": 3.456,
        "planned": "partial",
        "grid_in_kwh": 12.345,
        "grid_out_kwh": 3.456,
        "grid_bought_eur": 13.7,
        "grid_sold_eur": 7.3,
        "grid_bill_eur": 6.4,
    }
    battery = {
        "soc": 55.5,
        "stored_kwh": 11.37,
        "stored_cost_eur_per_kwh": 0.1987,
        "state": "charge_solar",
        "margin_30d_eur": 12.345,
        "solar_gain_30d_eur": 1.234,
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
    assert all("solar_gain_eur" in d for d in card["days"])
    margin, solar = ctrl.last_status["battery_margin_30d"], ctrl.last_status["battery_solar_gain_30d"]
    assert card["battery"]["margin_30d_eur"] == margin[0] and card["battery"]["solar_gain_30d_eur"] == solar[0]


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


async def test_a_disabled_tick_keeps_the_battery_line_live(monkeypatch):
    ctrl, _act, _ = _tick_setup(monkeypatch, enabled=False)
    ctrl.power_card = stale_card()
    await ctrl.tick()
    battery = ctrl.power_card["battery"]
    assert (battery["soc"], battery["state"]) == (50.0, "disabled")
    assert battery["stored_kwh"] == round(0.5 * ctrl.cfg.capacity_kwh, 2)


async def test_a_failsafe_tick_without_inputs_marks_the_battery_line_unknown(monkeypatch):
    ctrl, _act, _ = _tick_setup(monkeypatch, seed=False)
    ctrl.power_card = stale_card()
    await ctrl.tick()
    assert ctrl.power_card["battery"]["state"] is None and ctrl.power_card["battery"]["soc"] is None


async def test_where_a_kept_value_is_earned_reaches_the_debug_reasons_not_the_sensor():
    from datetime import datetime

    from tests.helpers import make_controller

    ctrl, _act = make_controller()
    ctrl._recorder.read_feature_rows = lambda since: _samples()
    ctrl.last_status["terminal_v_hi"] = 0.30
    rebuild = _charge_then_export()
    starts = [datetime.fromisoformat(r["start"]) for r in rebuild()[0]]
    ctrl._kept_by_slot = dict.fromkeys(starts, 0.5)
    ctrl._kept_src_by_slot = dict.fromkeys(starts, ("refill", starts[-1]))
    await ctrl._refresh_battery_margin(NOW)
    await ctrl._publish_power_card(NOW, card_inputs(48.0), "hold", False, rebuild)
    sensor = [h["why"] for h in ctrl.power_card["hours"] if h.get("why", {}).get("kind") == "hold"]
    debug = [r for r in ctrl._card_reasons.values() if r["kind"] == "hold"]
    assert sensor and debug
    assert all("kept_price" not in w and "kept_estimated" not in w for w in sensor)
    assert all(r["kept_for"]["kind"] == "refill" and "kept_price" in r for r in debug)


async def test_reasons_get_the_snapped_backup_reserve_on_a_20_kwh_pack(monkeypatch):
    import dataclasses

    from custom_components.anker_x1_smartgrid import why
    from custom_components.anker_x1_smartgrid.dp_common import reserve_wall_kwh
    from tests.helpers import make_controller

    seen = []
    monkeypatch.setattr(why, "hour_reasons", lambda *a, **k: seen.append(k["backup_reserve_soc"]) or {})
    ctrl, _act = make_controller()
    ctrl.cfg = dataclasses.replace(ctrl.cfg, capacity_kwh=20.48, backup_reserve_soc=25.0)
    ctrl._recorder.read_feature_rows = lambda since: _samples()
    ctrl.last_status["terminal_v_hi"] = 0.30
    await ctrl._refresh_battery_margin(NOW)
    await ctrl._publish_power_card(NOW, card_inputs(48.0), "hold", False, _charge_then_export())
    snapped = ctrl.cfg.kwh_to_pct(reserve_wall_kwh(ctrl.cfg))
    assert snapped > 25.0
    assert seen == [pytest.approx(snapped)]
