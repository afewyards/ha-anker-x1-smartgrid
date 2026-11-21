from __future__ import annotations

import copy
import json
import logging
import os
import re
import shutil
import subprocess
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from custom_components.anker_x1_smartgrid import optimize as optimize_mod
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


def test_sensor_publishes_its_attributes_unrecorded():
    card = {"built_at": NOW.isoformat(), "battery": {"soc": 50}, "live": {"pv": []}, "days": [], "hours": []}
    sensor = X1PowerCardSensor(_Stub(card), "entry")
    assert sensor.native_value == NOW.isoformat()
    assert sensor.extra_state_attributes == {
        "battery": {"soc": 50},
        "live": {"pv": []},
        "days": [],
        "hours": [],
        "pack": None,
    }
    assert X1PowerCardSensor._unrecorded_attributes == frozenset({"battery", "live", "days", "hours", "pack"})


def test_sensor_without_a_card_publishes_nothing():
    sensor = X1PowerCardSensor(_Stub(None), "entry")
    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {
        "battery": None,
        "live": None,
        "days": None,
        "hours": None,
        "pack": None,
    }


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
    eta_d = optimize_mod.eta_discharge_at(ctrl.cfg.max_export_w, ctrl.cfg, ctrl._planner_curve())
    assert battery["break_even_eur_per_kwh"] == round((0.25 + ctrl.cfg.cycle_cost_eur_per_kwh) / eta_d, 4)
    assert card["break_even_eur_per_kwh"] == battery["break_even_eur_per_kwh"]
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


async def test_break_even_is_none_on_an_empty_battery():
    from tests.helpers import make_controller

    ctrl, _act = make_controller()
    ctrl._recorder.read_feature_rows = lambda since: []
    await ctrl._refresh_battery_margin(NOW)
    await ctrl._publish_power_card(NOW, card_inputs(48.0), "hold", False, _rebuild())
    assert ctrl.power_card["battery"]["stored_cost_eur_per_kwh"] is None
    assert ctrl.power_card["battery"]["break_even_eur_per_kwh"] is None


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


async def test_payload_fits_our_32_kb_dashboard_budget():
    # Our own guard against the payload growing unnoticed. Home Assistant has no limit for unrecorded
    # attributes; the rows reach an open dashboard when they change and on page load.
    ctrl, _hours = await _card_with_reasons(0.30)
    attrs = X1PowerCardSensor(_Stub(ctrl.power_card), "entry").extra_state_attributes
    assert len(json.dumps(attrs, separators=(",", ":"))) < 32_000


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


def _slots(n=4, start=NOW, minutes=15):
    from custom_components.anker_x1_smartgrid.models import PriceSlot

    return [PriceSlot(start + timedelta(minutes=minutes * i), 0.10 + 0.01 * i, float(minutes)) for i in range(n)]


class _Registry:
    def __init__(self, ids):
        self._ids = ids

    def async_get_entity_id(self, domain, platform, unique_id):
        return self._ids.get(unique_id)


@pytest.fixture(autouse=True)
def registered(monkeypatch):
    from custom_components.anker_x1_smartgrid import controller as ctrl_mod

    ids = {}
    monkeypatch.setattr(ctrl_mod.er, "async_get", lambda hass: _Registry(ids))
    return ids


async def _live(slots, *, now=NOW, overrides=None, inputs=True):
    from tests.helpers import make_controller

    ctrl, _act = make_controller(data_overrides=overrides)
    ctrl.power_card = stale_card()
    ctrl._card_slots = slots
    await ctrl._publish_power_card(now, card_inputs(50.0) if inputs else None, "hold", False)
    return ctrl.power_card["live"]


async def test_live_names_the_configured_pv_and_meter_sensors():
    live = await _live(_slots(), overrides={"ent_pv_power": ["sensor.a", "sensor.b"]})
    assert live["pv"] == ["sensor.a", "sensor.b"]
    assert live["meter"] == "sensor.meter_power"


async def test_live_names_the_battery_power_sensor():
    live = await _live(_slots(), overrides={"ent_battery_power": "sensor.my_battery"})
    assert live["battery"] == "sensor.my_battery"


async def test_live_range_is_todays_lowest_and_highest_slot_price():
    from custom_components.anker_x1_smartgrid.models import PriceSlot

    from homeassistant.util import dt as dt_util

    local = dt_util.as_local(NOW)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    slots = [
        PriceSlot(
            midnight + timedelta(minutes=15 * i), 0.5 if i < 96 else 0.1 + 0.001 * (i - 96) if i < 192 else 0.9, 15.0
        )
        for i in range(288)
    ]
    live = await _live(slots)
    assert live["range"] == {"lo": 0.1, "hi": 0.195}


async def test_live_range_is_none_without_slots():
    live = await _live([], inputs=False)
    assert live["range"] is None


async def test_live_price_is_the_current_slot_and_the_next():
    live = await _live(_slots(), now=NOW + timedelta(minutes=20))
    assert live["price"] == 0.11
    assert live["next"] == {"price": 0.12, "at": (NOW + timedelta(minutes=30)).isoformat()}


async def test_live_next_is_none_at_the_end_of_known_prices():
    live = await _live(_slots(2), now=NOW + timedelta(minutes=20))
    assert live["price"] == 0.11 and live["next"] is None


async def test_live_price_is_none_without_prices():
    live = await _live([], inputs=False)
    assert live["price"] is None and live["next"] is None
    assert live["pv"] == ["sensor.pv_power"]


async def test_a_failing_live_line_still_names_the_sensors(monkeypatch):
    from custom_components.anker_x1_smartgrid import resolution

    def _boom(*_a, **_k):
        raise RuntimeError("unhappy")

    monkeypatch.setattr(resolution, "price_at", _boom)
    live = await _live(_slots(), overrides={"ent_pv_power": ["sensor.a"]})
    assert live == {
        "pv": ["sensor.a"],
        "meter": "sensor.meter_power",
        "battery": "sensor.battery_power",
        "range": None,
        "price": None,
        "next": None,
    }


async def test_a_failing_live_line_leaves_battery_days_and_hours(monkeypatch, caplog):
    from custom_components.anker_x1_smartgrid import resolution
    from tests.helpers import make_controller

    ctrl, _act = make_controller()
    ctrl.power_card = stale_card()
    ctrl._card_slots = _slots()
    monkeypatch.setattr(resolution, "price_at", lambda *_a, **_k: 1 / 0)
    for _ in range(2):
        await ctrl._publish_power_card(NOW, card_inputs(50.0), "hold", False)
    card = ctrl.power_card
    assert card is not None and card["battery"]["soc"] == 50.0
    assert "days" in card and "hours" in card and card["live"]["price"] is None
    assert sum("power card live" in r.getMessage() for r in caplog.records) == 1


async def test_a_tick_publishes_the_current_slot_price(monkeypatch):
    from tests.test_controller_export_executor import BASE

    ctrl, _act, _ = _tick_setup(monkeypatch)
    await ctrl.tick()
    live = ctrl.power_card["live"]
    assert live["price"] == 0.30
    assert live["next"]["price"] == 0.30
    assert live["next"]["at"] == (BASE + timedelta(hours=1)).isoformat()


async def test_a_disabled_tick_publishes_the_current_slot_price(monkeypatch):
    ctrl, _act, _ = _tick_setup(monkeypatch, enabled=False)
    ctrl.power_card = stale_card()
    await ctrl.tick()
    assert ctrl.power_card["live"]["price"] == 0.30


async def test_a_static_tariff_tick_prices_the_live_line_from_the_synthesized_slots(monkeypatch):
    from custom_components.anker_x1_smartgrid import const

    ctrl, _act, _ = _tick_setup(monkeypatch)
    ctrl._data[const.CONF_PRICE_MODE] = const.PRICE_MODE_STATIC
    await ctrl.tick()
    price = ctrl.power_card["live"]["price"]
    # The fixture leaves the static tariff at its default: flat import price.
    assert price == const.DEFAULT_STATIC_PRICE_IMPORT
    assert ctrl.power_card["live"]["next"]["price"] == const.DEFAULT_STATIC_PRICE_IMPORT


def test_trim_rows_drops_the_defaults_the_card_assumes():
    row = {"start": "s", "state": "idle", "price": None, "soc": None, "use_kwh": 0.0, "solar_kwh": 0, "plan": False}
    row |= {"grid_in_house_kwh": None, "grid_in_charge_kwh": None, "grid_out_kwh": None, "batt_house_kwh": 0.0}
    assert usecost.trim_rows([row]) == [{"start": "s", "state": "idle", "batt_house_kwh": 0.0}]


def test_trim_rows_keeps_a_null_use_or_solar_figure():
    row = {"start": "s", "use_kwh": None, "solar_kwh": None}
    assert usecost.trim_rows([row]) == [row]


def test_trim_rows_keeps_zero_and_false_where_they_are_measurements():
    row = {"start": "s", "price": 0.0, "soc": 0.0, "grid_in_house_kwh": 0.0, "grid_in_charge_kwh": 0.0}
    row |= {"grid_out_kwh": 0.0, "use_kwh": 1.2, "solar_kwh": 0.4, "plan": True, "why": {}}
    assert usecost.trim_rows([row]) == [row]


def test_the_sensor_trims_a_copy_and_leaves_the_controllers_rows():
    hour = {"start": "2026-11-12T00:00:00+01:00", "price": None, "plan": False, "use_kwh": 0.0}
    card = {"built_at": "x", "battery": {}, "live": {}, "days": [], "hours": [hour]}
    assert X1PowerCardSensor(_Stub(card), "entry").extra_state_attributes["hours"] == [{"m": 0}]
    assert card["hours"] == [{"start": "2026-11-12T00:00:00+01:00", "price": None, "plan": False, "use_kwh": 0.0}]


def test_the_card_defaults_match_what_the_backend_drops():
    root = Path(usecost.__file__).parent / "frontend"
    body = re.search(r"const ROW_DEFAULTS = \{(.*?)\};", (root / "smartgrid-power-card.js").read_text()).group(1)
    want = {k: None for k in usecost._NULL_KEYS} | {k: 0 for k in usecost._ZERO_KEYS} | {"plan": False}
    got = {k: json.loads(v) for k, v in re.findall(r"(\w+):\s*(null|0|false)", body)}
    assert got == want
    gone = re.search(r"const gone = \{(.*?)\};", (root / "harness.html").read_text()).group(1)
    assert {k: json.loads(v) for k, v in re.findall(r"(\w+):\s*(null|0|false)", gone)} == want


def _unpack(rows, pack):
    """The card's rule, in Python; grid positions that were None are left out as trim_rows leaves them."""
    out, off, k = [], pack["base"][-6:], 0
    tz = pack.get("tz", [])
    base = datetime.fromisoformat(pack["base"])
    for i, p in enumerate(rows):
        while k < len(tz) and tz[k][0] <= i:
            off = tz[k][1]
            k += 1
        sign = -1 if off[0] == "-" else 1
        zone = timezone(sign * timedelta(hours=int(off[1:3]), minutes=int(off[4:6])))
        r = {key: v for key, v in p.items() if key not in ("m", "g")}
        r["start"] = (base + timedelta(minutes=p["m"])).astimezone(zone).isoformat()
        r |= {key: v for key, v in zip(usecost._GRID_KEYS, p.get("g", ()), strict=False) if v is not None}
        if "kf" in p.get("why", {}):
            r["why"] = {key: v for key, v in p["why"].items() if key != "kf"} | {"kept_for": pack["kf"][p["why"]["kf"]]}
        out.append(r)
    return out


def _local_rows():
    tz = ZoneInfo("Europe/Amsterdam")
    start = datetime(2026, 10, 24, 22, tzinfo=UTC)
    rows = []
    for n in (*range(0, 8), *range(11, 14)):  # a gap of three hours, across the October clock change
        t = (start + timedelta(hours=n)).astimezone(tz)
        rows.append({"start": t.isoformat(), "price": 0.2, "use_kwh": 0.3, "state": "idle"})
    grid = [(None, None, None), (1.0, None, None), (None, 0.0, None), (None, None, 2.5), (0.5, 0.5, 0.5)]
    for r, g in zip(rows, grid, strict=False):
        r |= dict(zip(usecost._GRID_KEYS, g, strict=True))
    shared = {"start": rows[0]["start"], "end": None, "kind": "next_day"}
    rows[2]["why"] = {"kind": "hold", "keep_eur": 0.3, "use_eur": 0.1, "kept_for": shared}
    rows[3]["why"] = {"kind": "hold", "keep_eur": 0.3, "use_eur": 0.2, "kept_for": dict(shared)}
    rows[4]["why"] = {"kind": "house", "keep_eur": 0.3, "use_eur": 0.2, "kept_for": None}
    return rows


async def test_packed_rows_unpack_to_the_trimmed_rows():
    _ctrl, hours = await _card_with_reasons(0.30)
    for rows in (hours, _local_rows()):
        before = copy.deepcopy(rows)
        trimmed = usecost.trim_rows(rows)
        packed, pack = usecost.pack_rows(trimmed)
        assert rows == before
        assert _unpack(packed, pack) == trimmed
    assert pack["tz"] == [[5, "+01:00"]] or pack["tz"][0][1] == "+01:00"
    assert len(pack["kf"]) == 1
    assert usecost.pack_rows([]) == ([], None)


def _series(zone, start_utc, step_min, count, **over):
    tz = ZoneInfo(zone)
    start = datetime.fromisoformat(start_utc).replace(tzinfo=UTC)
    return [
        {"start": (start + timedelta(minutes=step_min * n)).astimezone(tz).isoformat(), "state": "idle", "use_kwh": 0.3}
        | over
        for n in range(count)
    ]


def _with_why_and_grid():
    rows = _series("Europe/Amsterdam", "2026-11-12T10:00:00", 60, 6)
    shared = {"start": rows[0]["start"], "end": None, "kind": "next_day"}
    rows[0]["why"] = {"kind": "hold", "keep_eur": 0.3, "use_eur": 0.1, "kept_for": shared}
    rows[1]["why"] = {"kind": "house", "keep_eur": 0.3, "use_eur": 0.2, "kept_for": None}
    rows[2]["why"] = {"kind": "hold", "keep_eur": 0.3, "use_eur": 0.2, "kept_for": dict(shared)}
    for r, g in zip(rows, [(None,) * 3, (1.0, None, None), (None, 0.0, None), (None, None, 2.5), (0.5, 0.5, 0.5)]):
        r |= dict(zip(usecost._GRID_KEYS, g, strict=True))
    return rows


_ROW_SETS = {
    "autumn_15min": lambda: _series("Europe/Amsterdam", "2026-10-24T23:00:00", 15, 24),
    "spring_hourly": lambda: _series("Europe/Amsterdam", "2027-03-27T21:00:00", 60, 10),
    "kolkata_off_the_hour": lambda: _series("Asia/Kolkata", "2026-11-12T00:15:00", 60, 5),
    "gap": lambda: (
        _series("Europe/Amsterdam", "2026-11-12T00:00:00", 60, 3)
        + _series("Europe/Amsterdam", "2026-11-12T09:00:00", 60, 3)
    ),
    "unsorted": lambda: _series("Europe/Amsterdam", "2026-11-12T00:00:00", 60, 4)[::-1],
    "single": lambda: _series("Europe/Amsterdam", "2026-11-12T00:00:00", 60, 1),
    "empty": list,
    "why_and_grid": _with_why_and_grid,
}


def _node_unpack(rows):
    trimmed = usecost.trim_rows(rows)
    packed, pack = usecost.pack_rows(trimmed)
    js = (Path(usecost.__file__).parent / "frontend" / "smartgrid-power-card.js").read_text()
    fn = re.search(r"function unpackRows[\s\S]*?\n}\n", js).group(0)
    script = (
        fn
        + "const d = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
        + "console.log(JSON.stringify(unpackRows(d.hours, d.pack)));"
    )
    out = subprocess.run(
        ["node", "-e", script],
        input=json.dumps({"hours": packed, "pack": pack}),
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "TZ": "Pacific/Auckland"},
    ).stdout
    # The card fills a packed-away grid figure with null, which ROW_DEFAULTS treats as absent.
    got = [{k: v for k, v in r.items() if not (k in usecost._GRID_KEYS and v is None)} for r in json.loads(out)]
    return got, trimmed


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize("name", list(_ROW_SETS))
def test_the_cards_unpack_reads_what_the_backend_packs(name):
    got, trimmed = _node_unpack(_ROW_SETS[name]())
    assert got == trimmed


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
async def test_the_cards_unpack_reads_the_controllers_rows():
    _ctrl, hours = await _card_with_reasons(0.30)
    got, trimmed = _node_unpack(hours)
    assert got == trimmed
    assert any("kept_for" in r.get("why", {}) for r in got)


async def test_live_names_the_smoothed_sensors_when_registered(registered):
    registered |= {
        "anker_x1_smartgrid_solar_power_w": "sensor.my_solar",
        "anker_x1_smartgrid_grid_power_w": "sensor.my_grid",
        "anker_x1_smartgrid_battery_power_w": "sensor.my_batt",
    }
    live = await _live(_slots(), overrides={"ent_pv_power": ["sensor.a", "sensor.b"]})
    assert (live["pv"], live["meter"], live["battery"]) == (["sensor.my_solar"], "sensor.my_grid", "sensor.my_batt")
