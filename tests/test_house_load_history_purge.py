import logging

import pytest
from homeassistant.components.recorder.services import SERVICE_PURGE_ENTITIES_SCHEMA
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.anker_x1_smartgrid as x1_init
from custom_components.anker_x1_smartgrid import _purge_house_load_history
from custom_components.anker_x1_smartgrid.const import DEFAULT_ENTITIES, DOMAIN
from tests.conftest import ANKER_TEST_ENTITIES

UID = "anker_x1_smartgrid_house_load_w"


def _register_entity(hass, domain="sensor", platform=DOMAIN, uid=UID):
    return er.async_get(hass).async_get_or_create(domain, platform, uid, suggested_object_id="renamed_load")


def _register_service(hass, error=None):
    calls = []

    async def _handler(call):
        calls.append(dict(call.data))
        if error:
            raise error

    hass.services.async_register("recorder", "purge_entities", _handler, schema=SERVICE_PURGE_ENTITIES_SCHEMA)
    return calls


async def test_purges_resolved_entity_with_keep_days(hass):
    _register_entity(hass)
    calls = _register_service(hass)
    await _purge_house_load_history(hass, 3)
    assert calls == [{"entity_id": ["sensor.renamed_load"], "keep_days": 3, "entity_globs": [], "domains": []}]


async def test_skips_entity_of_other_platform(hass):
    _register_entity(hass, platform="other")
    calls = _register_service(hass)
    await _purge_house_load_history(hass, 3)
    assert calls == []


async def test_skips_when_off(hass):
    _register_entity(hass)
    calls = _register_service(hass)
    await _purge_house_load_history(hass, 0)
    assert calls == []


async def test_skips_when_service_missing(hass):
    _register_entity(hass)
    await _purge_house_load_history(hass, 3)


async def test_service_error_is_logged_not_raised(hass, caplog):
    _register_entity(hass)
    _register_service(hass, error=RuntimeError("boom"))
    with caplog.at_level(logging.WARNING):
        await _purge_house_load_history(hass, 3)
    assert "house-load recorder purge failed" in caplog.text


@pytest.mark.parametrize(("extra", "scheduled"), [({}, True), ({"history_days": "0"}, False)])
async def test_setup_schedules_nightly_purge(hass, monkeypatch, extra, scheduled):
    calls = []
    monkeypatch.setattr(x1_init, "async_track_time_change", lambda h, cb, **kw: calls.append(kw) or (lambda: None))
    entry = MockConfigEntry(domain=DOMAIN, data={**DEFAULT_ENTITIES, **ANKER_TEST_ENTITIES, **extra})
    entry.add_to_hass(hass)
    hass.states.async_set(entry.data["ent_soc"], "50")
    hass.states.async_set(entry.data["ent_meter_power"], "0")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert calls == ([{"hour": 4, "minute": 30, "second": 0}] if scheduled else [])
    assert await hass.config_entries.async_unload(entry.entry_id)
