"""End-to-end pin: a doubled 15-min Frank Energie attribute drives native dt=15.

Reproduces the live France-box shape verified 2026-07-31: 192 entries / 96
distinct 15-min slots, every slot published exactly twice, tz-aware ISO
datetimes, plain €/kWh floats.
"""

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.anker_x1_smartgrid import const, coordinator, resolution
from tests.conftest import ANKER_TEST_ENTITIES

FRANK_ALL_IN = "sensor.frank_energie_electricity_prices_current_electricity_price_all_in"
FRANK_MARKET = "sensor.frank_energie_electricity_prices_current_electricity_market_price"

_DAY_START = datetime(2026, 7, 31, 0, 0, tzinfo=UTC)


def _frank_prices(offset: float) -> list[dict]:
    """96 distinct 15-min slots, each entry emitted twice (the upstream quirk)."""
    entries = []
    for i in range(96):
        start = _DAY_START + timedelta(minutes=15 * i)
        entries.append(
            {
                "from": start.isoformat(),
                "till": (start + timedelta(minutes=15)).isoformat(),
                "price": round(offset + 0.001 * i, 6),
            }
        )
    return entries + entries


def _data(**overrides):
    d = {**const.DEFAULT_ENTITIES, **ANKER_TEST_ENTITIES}
    d[const.CONF_ENT_PRICE] = FRANK_ALL_IN
    d[const.CONF_ENT_EXPORT_PRICE] = FRANK_MARKET
    d.update(overrides)
    return d


async def test_doubled_15min_attribute_yields_96_slots_at_15_minutes(hass):
    d = _data()
    raw = _frank_prices(0.20)
    assert len(raw) == 192
    hass.states.async_set(FRANK_ALL_IN, "0.20", {"prices": raw})

    slots = coordinator.read_price_slots(hass, d)
    assert len(slots) == 96
    assert [s.duration_min for s in slots] == [15.0] * 96
    assert slots[0].price == pytest.approx(0.20)
    assert slots[-1].price == pytest.approx(0.20 + 0.001 * 95)
    assert resolution.resolve_slot_minutes(slots, const.SLOT_RESOLUTION_AUTO) == 15


async def test_export_market_curve_parses_at_same_resolution(hass, monkeypatch):
    d = _data()
    hass.states.async_set(FRANK_ALL_IN, "0.20", {"prices": _frank_prices(0.20)})
    hass.states.async_set(FRANK_MARKET, "0.08", {"prices": _frank_prices(0.08)})
    # Pin "now" into the i=0 slot, where the fixture's scalar state (0.08) agrees
    # with the curve — the cross-check in read_export_price_slots compares the
    # slot covering "now" against the entity's own scalar state.
    monkeypatch.setattr(coordinator.dt_util, "utcnow", lambda: _DAY_START)

    export_slots = coordinator.read_export_price_slots(hass, d)
    assert len(export_slots) == 96
    assert resolution.resolve_slot_minutes(export_slots, const.SLOT_RESOLUTION_AUTO) == 15
    # Import and export curves share the same slot grid → all-or-nothing coverage holds.
    assert [s.start for s in export_slots] == [s.start for s in coordinator.read_price_slots(hass, d)]
