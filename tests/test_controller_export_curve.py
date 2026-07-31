"""Controller wiring for the per-slot export price curve."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.anker_x1_smartgrid import const
from custom_components.anker_x1_smartgrid import controller as ctrl_mod
from custom_components.anker_x1_smartgrid.models import PlantInputs, PriceSlot
from tests.helpers import StubHass, make_controller

BASE = datetime(2026, 7, 31, 10, 0, tzinfo=UTC)

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
