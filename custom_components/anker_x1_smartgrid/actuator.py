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

import logging

from homeassistant.core import HomeAssistant

from . import const

_LOGGER = logging.getLogger(__name__)


class Actuator:
    def __init__(self, hass: HomeAssistant, data: dict) -> None:
        self._hass = hass
        self._data = data
        self.last_setpoint_w: float = 0.0
        self.engaged: bool = False

    def _clamp_to_live_limits(self, setpoint_w: float) -> float:
        """Clamp *setpoint_w* into the setpoint entity's LIVE min/max attributes.

        The inverter BMS min/max (exposed as the ``number.*`` entity's `min`/
        `max` attributes) float over time and can be tighter than the static
        SETPOINT_MIN_W/SETPOINT_MAX_W guard constants — e.g. a live max of
        6600 W or a live min of -5910 W. Writing a value outside the live
        range raises ``ServiceValidationError`` and aborts the whole
        set_value call, half-engaging the inverter (modbus switch already on,
        setpoint never written). Clamp the signed value toward zero into
        [live_min, live_max], then re-floor the MAGNITUDE toward zero onto
        the SETPOINT_STEP_W grid so the clamped value stays on-grid (e.g.
        -6000 with live min -5910 → -5900).

        Returns *setpoint_w* unchanged if the entity state is missing/
        unavailable/unknown or the min/max attributes are absent or
        non-numeric — the static guard clamp (SETPOINT_MIN_W/SETPOINT_MAX_W)
        has already been applied upstream, so this is a best-effort
        tightening, not a required safety net.

        Sign guard: a pathological live limit (e.g. live_min >= 0 for a
        charge request, or live_max <= 0 for an export request) must never
        flip the commanded direction — that would command discharge where
        charge was requested (or vice versa). Charge is clamped into
        [live_min, 0.0] and export into [0.0, live_max], so a wrong-signed
        live limit collapses to 0.0 (honest idle) instead of a sign flip.
        """
        if setpoint_w == 0.0:
            return setpoint_w
        state = self._hass.states.get(self._data[const.CONF_ENT_SETPOINT])
        if state is None or state.state in ("unavailable", "unknown"):
            return setpoint_w
        live_min = state.attributes.get("min")
        live_max = state.attributes.get("max")
        if not isinstance(live_min, (int, float)) or isinstance(live_min, bool):
            return setpoint_w
        if not isinstance(live_max, (int, float)) or isinstance(live_max, bool):
            return setpoint_w

        if setpoint_w < 0.0:
            clamped = min(max(setpoint_w, float(live_min)), 0.0)
        else:
            clamped = max(min(setpoint_w, float(live_max)), 0.0)

        mag = abs(clamped)
        stepped_mag = (mag // const.SETPOINT_STEP_W) * const.SETPOINT_STEP_W
        stepped = -stepped_mag if clamped < 0.0 else stepped_mag

        if stepped != setpoint_w:
            _LOGGER.warning(
                "Setpoint clamped to live inverter limits: requested=%.1f clamped=%.1f (live min=%s max=%s)",
                setpoint_w,
                stepped,
                live_min,
                live_max,
            )
        return stepped

    async def _engage(self, setpoint_w: float) -> None:
        """Shared engage path: modbus/VPP on, then write setpoint.

        Called by both engage_and_charge (negative) and engage_export (positive).
        The only difference between charge and export is setpoint sign; the
        VPP/modbus engage sequence is identical (A1: no separate export workmode).
        """
        await self._hass.services.async_call(
            "switch",
            "turn_on",
            {"entity_id": self._data[const.CONF_ENT_ENGAGE]},
            blocking=True,
        )
        # Set the control flag BEFORE writing the setpoint.  If set_value fails,
        # engaged=True ensures the next disabled/failsafe tick fires release_to_self
        # (no stuck-in-VPP).  turn_on failing above raises before this line, so a
        # failed engage never reports engaged.
        self.engaged = True
        setpoint_w = self._clamp_to_live_limits(setpoint_w)
        await self._hass.services.async_call(
            "number",
            "set_value",
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
            "number",
            "set_value",
            {"entity_id": self._data[const.CONF_ENT_SETPOINT], "value": 0.0},
            blocking=True,
        )
        await self._hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": self._data[const.CONF_ENT_WORKMODE], "option": const.WORKMODE_SELF},
            blocking=True,
        )
        await self._hass.services.async_call(
            "switch",
            "turn_off",
            {"entity_id": self._data[const.CONF_ENT_ENGAGE]},
            blocking=True,
        )
        self.last_setpoint_w = 0.0
        self.engaged = False
