"""coordinator.read_export_price_slots — per-slot export (feed-in) curve reader."""

from datetime import UTC, datetime

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

# 2026-07-31T10:05:00+02:00 == 08:05 UTC — inside the first _MARKET_ATTR slot.
_NOW_IN_FIRST_SLOT = datetime(2026, 7, 31, 8, 5, tzinfo=UTC)
# Outside both _MARKET_ATTR slots (they cover 08:00-08:30 UTC).
_NOW_OUTSIDE_SLOTS = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)


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


async def test_read_export_price_slots_matching_state_keeps_curve(hass, monkeypatch):
    """Curve slot covering 'now' agrees with the entity's scalar state — curve kept."""
    d = _data(**{const.CONF_ENT_PRICE: FRANK_ALL_IN, const.CONF_ENT_EXPORT_PRICE: FRANK_MARKET})
    hass.states.async_set(FRANK_MARKET, "0.10", _MARKET_ATTR)
    monkeypatch.setattr(coordinator.dt_util, "utcnow", lambda: _NOW_IN_FIRST_SLOT)
    slots = coordinator.read_export_price_slots(hass, d)
    assert [s.price for s in slots] == pytest.approx([0.10, 0.12])


async def test_read_export_price_slots_unit_mismatch_discards_curve(hass, monkeypatch, caplog):
    """Curve carrying the wrong series/units (e.g. values ~100x the scalar state) is untrustworthy."""
    d = _data(**{const.CONF_ENT_PRICE: FRANK_ALL_IN, const.CONF_ENT_EXPORT_PRICE: FRANK_MARKET})
    mismatched_attr = {
        "prices": [
            {"from": "2026-07-31T10:00:00+02:00", "till": "2026-07-31T10:15:00+02:00", "price": 10.0},
            {"from": "2026-07-31T10:15:00+02:00", "till": "2026-07-31T10:30:00+02:00", "price": 12.0},
        ]
    }
    hass.states.async_set(FRANK_MARKET, "0.05", mismatched_attr)
    monkeypatch.setattr(coordinator.dt_util, "utcnow", lambda: _NOW_IN_FIRST_SLOT)
    with caplog.at_level("WARNING"):
        slots = coordinator.read_export_price_slots(hass, d)
    assert slots == []
    assert any(FRANK_MARKET in rec.getMessage() for rec in caplog.records)


async def test_read_export_price_slots_unavailable_state_keeps_curve(hass, monkeypatch):
    """No scalar state to cross-check against — the guard is a cross-check, not a gate."""
    d = _data(**{const.CONF_ENT_PRICE: FRANK_ALL_IN, const.CONF_ENT_EXPORT_PRICE: FRANK_MARKET})
    hass.states.async_set(FRANK_MARKET, "unavailable", _MARKET_ATTR)
    monkeypatch.setattr(coordinator.dt_util, "utcnow", lambda: _NOW_IN_FIRST_SLOT)
    slots = coordinator.read_export_price_slots(hass, d)
    assert [s.price for s in slots] == pytest.approx([0.10, 0.12])


async def test_read_export_price_slots_no_slot_covers_now_keeps_curve(hass, monkeypatch):
    """'now' falls outside every slot — nothing to compare against, curve kept."""
    d = _data(**{const.CONF_ENT_PRICE: FRANK_ALL_IN, const.CONF_ENT_EXPORT_PRICE: FRANK_MARKET})
    hass.states.async_set(FRANK_MARKET, "999.0", _MARKET_ATTR)
    monkeypatch.setattr(coordinator.dt_util, "utcnow", lambda: _NOW_OUTSIDE_SLOTS)
    slots = coordinator.read_export_price_slots(hass, d)
    assert [s.price for s in slots] == pytest.approx([0.10, 0.12])


_SINGLE_SLOT_ATTR = {
    "prices": [
        {"from": "2026-07-31T10:00:00+02:00", "till": "2026-07-31T10:15:00+02:00", "price": 0.10},
    ]
}


async def test_read_export_price_slots_single_slot_matching_state_kept(hass, monkeypatch):
    """A single-entry curve (duration_min unknowable) whose lone price agrees
    with the scalar state is kept — same as any other unverifiable curve."""
    d = _data(**{const.CONF_ENT_PRICE: FRANK_ALL_IN, const.CONF_ENT_EXPORT_PRICE: FRANK_MARKET})
    hass.states.async_set(FRANK_MARKET, "0.10", _SINGLE_SLOT_ATTR)
    monkeypatch.setattr(coordinator.dt_util, "utcnow", lambda: _NOW_IN_FIRST_SLOT)
    slots = coordinator.read_export_price_slots(hass, d)
    assert [s.price for s in slots] == pytest.approx([0.10])
    assert slots[0].duration_min is None


async def test_read_export_price_slots_single_slot_mismatch_kept_and_logged(hass, monkeypatch, caplog):
    """R2: a single-entry curve has no derivable duration (parse_price_curve
    leaves duration_min=None), so _slot_covering_now can never find a window
    containing 'now' — the cross-check can't evaluate it. Route chosen:
    unverifiable-but-kept (matches the existing "nothing covers now" philosophy
    — the guard never blocks what it can't evaluate) WITH a debug log so the
    skip is traceable instead of silently indistinguishable from a genuine
    no-coverage result. Even a wildly-mismatched scalar state does not
    discard the curve."""
    d = _data(**{const.CONF_ENT_PRICE: FRANK_ALL_IN, const.CONF_ENT_EXPORT_PRICE: FRANK_MARKET})
    hass.states.async_set(FRANK_MARKET, "999.0", _SINGLE_SLOT_ATTR)
    monkeypatch.setattr(coordinator.dt_util, "utcnow", lambda: _NOW_IN_FIRST_SLOT)
    with caplog.at_level("DEBUG"):
        slots = coordinator.read_export_price_slots(hass, d)
    assert [s.price for s in slots] == pytest.approx([0.10])
    assert any(FRANK_MARKET in rec.getMessage() for rec in caplog.records)


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
