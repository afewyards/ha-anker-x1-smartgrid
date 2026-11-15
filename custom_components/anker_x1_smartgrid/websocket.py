"""Websocket command serving the power card debug payload."""

from __future__ import annotations

import logging

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


@websocket_api.websocket_command({"type": f"{DOMAIN}/power_card_debug"})
@websocket_api.async_response
async def _power_card_debug(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    entry = next(iter(hass.data.get(DOMAIN, {}).values()), None)
    if entry is None:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "no loaded entry")
        return
    try:
        payload = await entry["controller"].power_card_debug()
    except Exception as err:
        _LOGGER.warning("power card debug payload failed: %s", err)
        connection.send_error(msg["id"], websocket_api.ERR_UNKNOWN_ERROR, "debug payload failed")
        return
    connection.send_result(msg["id"], payload)


def async_register(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, _power_card_debug)
