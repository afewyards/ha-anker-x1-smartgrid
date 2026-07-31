"""Controller wiring for the per-slot export price curve."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.anker_x1_smartgrid import const
from custom_components.anker_x1_smartgrid import controller as ctrl_mod
from custom_components.anker_x1_smartgrid.models import PlantInputs, PriceSlot
from tests.helpers import BASE as _SEED_BASE, StubHass, make_controller, seed_valid_inputs

BASE = datetime(2026, 7, 31, 10, 0, tzinfo=UTC)

# seed_valid_inputs() hardcodes its forecast timestamps against tests/helpers.py's
# own module-level BASE (2026-06-20 11:00 UTC) — distinct from this file's BASE
# above. The R1 full-tick tests below pin "now" to _SEED_BASE so they line up.
_EXPORT_CURVE_HOURS = 9

_MARKET_ATTR = {
    "prices": [
        {"from": "2026-07-31T10:00:00+00:00", "till": "2026-07-31T10:15:00+00:00", "price": 0.10},
        {"from": "2026-07-31T10:15:00+00:00", "till": "2026-07-31T10:30:00+00:00", "price": 0.12},
    ]
}


def test_resolve_export_slots_reads_market_curve():
    hass = StubHass()
    hass.set_state("sensor.market", "0.10", _MARKET_ATTR)
    ctrl, _ = make_controller(hass=hass, data_overrides={const.CONF_ENT_EXPORT_PRICE: "sensor.market"})
    slots = ctrl._resolve_export_slots()
    assert [s.price for s in slots] == pytest.approx([0.10, 0.12])


def test_resolve_export_slots_empty_without_export_entity():
    hass = StubHass()
    ctrl, _ = make_controller(hass=hass)
    assert ctrl._resolve_export_slots() == []


def test_resolve_export_slots_empty_in_static_mode():
    hass = StubHass()
    hass.set_state("sensor.market", "0.10", _MARKET_ATTR)
    ctrl, _ = make_controller(
        hass=hass,
        data_overrides={
            const.CONF_ENT_EXPORT_PRICE: "sensor.market",
            const.CONF_PRICE_MODE: const.PRICE_MODE_STATIC,
        },
    )
    assert ctrl._resolve_export_slots() == []


async def test_run_compute_decision_forwards_export_slots(monkeypatch):
    """The curve reaches compute_decision as the `export_slots` kwarg."""
    ctrl, _ = make_controller()
    captured: dict = {}

    def _fake_compute_decision(*args, **kwargs):
        captured.update(kwargs)
        return (ctrl.plan, 0.0, BASE, [], "single-day", [])

    monkeypatch.setattr(ctrl_mod, "compute_decision", _fake_compute_decision)
    curve = [PriceSlot(BASE, 0.10, duration_min=60.0)]
    await ctrl._run_compute_decision(
        ctrl.plan,
        None,
        PlantInputs(soc=50.0, meter_w=0.0, now=BASE),
        [PriceSlot(BASE, 0.20, duration_min=60.0)],
        0.0,
        BASE + timedelta(hours=6),
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        export_price=0.10,
        export_price_matches_import=False,
        temp_by_hour={},
        slot_minutes=60,
        dp_out={},
        export_slots=curve,
    )
    assert captured["export_slots"] is curve


async def test_run_compute_decision_defaults_export_slots_to_none(monkeypatch):
    """Omitting the kwarg keeps the legacy None (parity for existing call paths)."""
    ctrl, _ = make_controller()
    captured: dict = {}

    def _fake_compute_decision(*args, **kwargs):
        captured.update(kwargs)
        return (ctrl.plan, 0.0, BASE, [], "single-day", [])

    monkeypatch.setattr(ctrl_mod, "compute_decision", _fake_compute_decision)
    await ctrl._run_compute_decision(
        ctrl.plan,
        None,
        PlantInputs(soc=50.0, meter_w=0.0, now=BASE),
        [PriceSlot(BASE, 0.20, duration_min=60.0)],
        0.0,
        BASE + timedelta(hours=6),
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        export_price=0.10,
        export_price_matches_import=False,
        temp_by_hour={},
        slot_minutes=60,
        dp_out={},
    )
    assert captured["export_slots"] is None


def _export_curve_forecast(hours: int = _EXPORT_CURVE_HOURS, price: float = 0.05) -> dict:
    """Zonneplan-shaped forecast attribute mirroring seed_valid_inputs' own
    9-hour hourly import curve so the DP's all-or-nothing coverage check
    (_export_window_curve) sees every required window slot covered."""
    return {
        "forecast": [
            {
                "datetime": (_SEED_BASE + timedelta(hours=i)).isoformat(),
                "electricity_price": int(price * const.PRICE_SCALE),
            }
            for i in range(hours)
        ]
    }


async def test_tick_surfaces_export_curve_covered_into_last_status(monkeypatch):
    """R1: decision.py writes export_curve_covered/export_curve_slots into
    dp_out (~decision.py:564-565), but nothing copied them into
    self.last_status — sensor.py reads permanent None (~sensor.py:329-330).
    A REAL controller tick (real DP, no last_status stubbing) must land real
    values in last_status."""
    monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: _SEED_BASE)
    hass = StubHass()
    ctrl, _ = make_controller(hass, data_overrides={const.CONF_ENT_EXPORT_PRICE: "sensor.export_price"})
    seed_valid_inputs(hass, soc="50.0")
    hass.set_state("sensor.export_price", "0.05", _export_curve_forecast())

    result = await ctrl.tick()

    assert result["reason"] == "ok"
    assert ctrl.last_status["export_curve_covered"] is True
    assert ctrl.last_status["export_curve_slots"] == _EXPORT_CURVE_HOURS


async def test_tick_disabled_surfaces_export_curve_covered_into_last_status(monkeypatch):
    """R1, shadow/disabled path: same defect, mirrored fix at the
    _shadow_dp_out callsite (~controller.py:1279). Verified via a real
    disabled tick (ctrl.enabled = False), not by stubbing last_status."""
    monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: _SEED_BASE)
    hass = StubHass()
    ctrl, _ = make_controller(hass, data_overrides={const.CONF_ENT_EXPORT_PRICE: "sensor.export_price"})
    ctrl.enabled = False
    seed_valid_inputs(hass, soc="50.0")
    hass.set_state("sensor.export_price", "0.05", _export_curve_forecast())

    result = await ctrl.tick()

    assert result["reason"] == "disabled"
    assert ctrl.last_status["export_curve_covered"] is True
    assert ctrl.last_status["export_curve_slots"] == _EXPORT_CURVE_HOURS
