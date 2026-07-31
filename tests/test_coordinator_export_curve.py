"""coordinator.read_export_price_slots — per-slot export (feed-in) curve reader."""

import pytest

from custom_components.anker_x1_smartgrid import const, coordinator
from tests.conftest import ANKER_TEST_ENTITIES

FRANK_ALL_IN = "sensor.frank_energie_electricity_prices_current_electricity_price_all_in"
FRANK_MARKET = "sensor.frank_energie_electricity_prices_current_electricity_market_price"

_MARKET_ATTR = {
    "prices": [
        {"from": "2026-07-31T10:00:00+02:00", "till": "2026-07-31T10:15:00+02:00", "price": 0.10},
        {"from": "2026-07-31T10:15:00+02:00", "till": "2026-07-31T10:30:00+02:00", "price": 0.12},
    ]
}


def _data(**overrides):
    d = {**const.DEFAULT_ENTITIES, **ANKER_TEST_ENTITIES}
    d.update(overrides)
    return d


async def test_read_export_price_slots_parses_market_curve(hass):
    d = _data(**{const.CONF_ENT_PRICE: FRANK_ALL_IN, const.CONF_ENT_EXPORT_PRICE: FRANK_MARKET})
    hass.states.async_set(FRANK_MARKET, "0.10", _MARKET_ATTR)
    slots = coordinator.read_export_price_slots(hass, d)
    assert [s.price for s in slots] == pytest.approx([0.10, 0.12])
    assert slots[0].duration_min == 15.0


async def test_read_export_price_slots_empty_when_same_entity_as_import(hass):
    """Same entity → the DP already reuses the import curve; keep that path byte-identical."""
    d = _data(**{const.CONF_ENT_PRICE: FRANK_MARKET, const.CONF_ENT_EXPORT_PRICE: FRANK_MARKET})
    hass.states.async_set(FRANK_MARKET, "0.10", _MARKET_ATTR)
    assert coordinator.read_export_price_slots(hass, d) == []


async def test_read_export_price_slots_empty_in_static_mode(hass):
    d = _data(
        **{
            const.CONF_PRICE_MODE: const.PRICE_MODE_STATIC,
            const.CONF_ENT_PRICE: FRANK_ALL_IN,
            const.CONF_ENT_EXPORT_PRICE: FRANK_MARKET,
        }
    )
    hass.states.async_set(FRANK_MARKET, "0.10", _MARKET_ATTR)
    assert coordinator.read_export_price_slots(hass, d) == []


async def test_read_export_price_slots_empty_when_unconfigured_or_missing(hass):
    assert coordinator.read_export_price_slots(hass, _data()) == []
    d = _data(**{const.CONF_ENT_PRICE: FRANK_ALL_IN, const.CONF_ENT_EXPORT_PRICE: "sensor.ghost"})
    assert coordinator.read_export_price_slots(hass, d) == []


async def test_read_export_price_slots_empty_when_no_curve_attribute(hass):
    d = _data(**{const.CONF_ENT_PRICE: FRANK_ALL_IN, const.CONF_ENT_EXPORT_PRICE: FRANK_MARKET})
    hass.states.async_set(FRANK_MARKET, "0.10", {"unit_of_measurement": "EUR/kWh"})
    assert coordinator.read_export_price_slots(hass, d) == []


async def test_read_export_price_slots_reads_zonneplan_forecast_shape(hass):
    """Shape-agnostic: a `forecast`-shaped export entity works too."""
    d = _data(**{const.CONF_ENT_PRICE: FRANK_ALL_IN, const.CONF_ENT_EXPORT_PRICE: "sensor.other_export"})
    hass.states.async_set(
        "sensor.other_export",
        "0.09",
        {"forecast": [{"datetime": "2026-07-31T08:00:00Z", "electricity_price": 900000}]},
    )
    slots = coordinator.read_export_price_slots(hass, d)
    assert [s.price for s in slots] == pytest.approx([0.09])
