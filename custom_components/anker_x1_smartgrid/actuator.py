"""Ordered actuation protocol via HA service calls (spec §3).

Engage protocol (both charge and discharge/export share the same VPP path):
  1. switch turn_on  — enables modbus control (implicitly enters VPP mode)
  2. number set_value — writes the signed setpoint (negative = charge, positive = export)

Release protocol (from any engaged state):
  1. number set_value 0  — zero out the setpoint first
  2. select Self-consumption — restore passive workmode
  3. switch turn_off  — hand control back to firmware
"""
from __future__ import annotations

from homeassistant.core import HomeAssistant

from . import const


class Actuator:
    def __init__(self, hass: HomeAssistant, data: dict) -> None:
        self._hass = hass
        self._data = data
        self.last_setpoint_w: float = 0.0
        self.engaged: bool = False

    async def _engage(self, setpoint_w: float) -> None:
        """Shared engage path: modbus/VPP on, then write setpoint.

        Called by both engage_and_charge (negative) and engage_export (positive).
        The only difference between charge and export is setpoint sign; the
        VPP/modbus engage sequence is identical (A1: no separate export workmode).
        """
        await self._hass.services.async_call(
            "switch", "turn_on",
            {"entity_id": self._data[const.CONF_ENT_ENGAGE]}, blocking=True,
        )
        # Set the control flag BEFORE writing the setpoint.  If set_value fails,
        # engaged=True ensures the next disabled/failsafe tick fires release_to_self
        # (no stuck-in-VPP).  turn_on failing above raises before this line, so a
        # failed engage never reports engaged.
        self.engaged = True
        await self._hass.services.async_call(
            "number", "set_value",
            {"entity_id": self._data[const.CONF_ENT_SETPOINT], "value": setpoint_w},
            blocking=True,
        )
        # Record the setpoint only AFTER the write lands.  If set_value raises,
        # last_setpoint_w keeps its previous value — we didn't actually command
        # the new one, even though engaged is already True (N1).
        self.last_setpoint_w = setpoint_w

    async def engage_and_charge(self, setpoint_w: float) -> None:
        """Engage VPP control for grid charging.  setpoint_w must be <= 0."""
        if setpoint_w > 0:
            raise ValueError("charge-only: setpoint must be <= 0")
        await self._engage(setpoint_w)

    async def engage_export(self, setpoint_w: float) -> None:
        """Engage VPP control for grid export (discharge to grid).

        setpoint_w must be strictly positive.  The X1 firmware interprets a
        positive value as net-export: it serves house load first and exports the
        remainder (A1 hardware result, verified live 2026-06-25).
        """
        if setpoint_w <= 0:
            raise ValueError("export-only: setpoint must be > 0")
        await self._engage(setpoint_w)

    async def release_to_self(self) -> None:
        """Release VPP control and restore passive Self-consumption workmode.

        Safe to call from either a charge (negative setpoint) or export
        (positive setpoint) engaged state.
        """
        await self._hass.services.async_call(
            "number", "set_value",
            {"entity_id": self._data[const.CONF_ENT_SETPOINT], "value": 0.0},
            blocking=True,
        )
        await self._hass.services.async_call(
            "select", "select_option",
            {"entity_id": self._data[const.CONF_ENT_WORKMODE], "option": const.WORKMODE_SELF},
            blocking=True,
        )
        await self._hass.services.async_call(
            "switch", "turn_off",
            {"entity_id": self._data[const.CONF_ENT_ENGAGE]}, blocking=True,
        )
        self.last_setpoint_w = 0.0
        self.engaged = False
