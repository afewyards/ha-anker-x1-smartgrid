"""Websocket command serving the power card debug payload."""

from unittest.mock import AsyncMock, MagicMock

import aiohttp.connector
import pytest
from aiohttp.resolver import ThreadedResolver

from custom_components.anker_x1_smartgrid.const import DOMAIN
from custom_components.anker_x1_smartgrid.websocket import async_register


@pytest.fixture(autouse=True)
def _no_pycares_thread(monkeypatch):
    # pycares starts a thread HA's cleanup check rejects; loopback needs no async DNS.
    monkeypatch.setattr(aiohttp.connector, "DefaultResolver", ThreadedResolver)


PAYLOAD = {"slots": [1], "reference": [], "internals": {}}


async def _call(hass, hass_ws_client):
    async_register(hass)
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "anker_x1_smartgrid/power_card_debug"})
    return await client.receive_json()


async def test_command_returns_payload(hass, hass_ws_client):
    stub = MagicMock(power_card_debug=AsyncMock(return_value=PAYLOAD))
    hass.data[DOMAIN] = {"e": {"controller": stub}}
    msg = await _call(hass, hass_ws_client)
    assert msg["success"] is True
    assert msg["result"] == PAYLOAD


async def test_command_without_entry_errors(hass, hass_ws_client):
    msg = await _call(hass, hass_ws_client)
    assert msg["success"] is False
    assert msg["error"]["code"] == "not_found"


async def test_command_reports_a_failed_build(hass, hass_ws_client):
    stub = MagicMock(power_card_debug=AsyncMock(side_effect=RuntimeError("boom")))
    hass.data[DOMAIN] = {"e": {"controller": stub}}
    msg = await _call(hass, hass_ws_client)
    assert msg["success"] is False
    assert msg["error"]["code"] == "unknown_error"


async def test_registering_twice_is_harmless(hass, hass_ws_client):
    stub = MagicMock(power_card_debug=AsyncMock(return_value=PAYLOAD))
    hass.data[DOMAIN] = {"e": {"controller": stub}}
    async_register(hass)
    msg = await _call(hass, hass_ws_client)
    assert msg["success"] is True
