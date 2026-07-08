"""Master enable switch."""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN


class X1EnableSwitch(SwitchEntity):
    _attr_has_entity_name = True
    _attr_name = "SmartGrid enabled"

    def __init__(self, controller, entry_id: str) -> None:
        self._controller = controller
        self._attr_unique_id = "anker_x1_smartgrid_enabled"

    @property
    def is_on(self) -> bool:
        return self._controller.enabled

    async def async_turn_on(self, **kwargs) -> None:
        await self._controller.set_enabled(True)
        if self.hass is not None:
            self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        await self._controller.set_enabled(False)
        if self.hass is not None:
            self.async_write_ha_state()


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    controller = hass.data[DOMAIN][entry.entry_id]["controller"]
    async_add_entities([X1EnableSwitch(controller, entry.entry_id)])
