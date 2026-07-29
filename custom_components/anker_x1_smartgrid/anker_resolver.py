"""Resolve an anker_x1 device_id into the entity roles + scalars x1 needs."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from . import const

_LOGGER = logging.getLogger(__name__)


def _anker_entry_id(hass: HomeAssistant, entries) -> str | None:
    """Return the anker_x1 config entry id owning these device entities."""
    for ent in entries:
        if ent.config_entry_id is None:
            continue
        ce = hass.config_entries.async_get_entry(ent.config_entry_id)
        if ce is not None and ce.domain == const.ANKER_X1_DOMAIN:
            return ent.config_entry_id
    return None


def resolve_anker_config(hass: HomeAssistant, device_id: str) -> tuple[dict[str, Any], list[str]]:
    """Map an anker_x1 device_id to x1 config values.

    Returns (resolved_values, missing_roles):
      * resolved_values: {CONF_ENT_*: entity_id, [CONF_CAPACITY_KWH: float]}.
      * missing_roles: CONF_ENT_* keys (of the 5 hard roles) with no exact match.
    Soft roles (meter power, inverter loss — ANKER_SOFT_ROLE_SUFFIXES) and
    capacity behave the same way: a miss is omitted from resolved_values and
    never added to missing_roles.
    """
    reg = er.async_get(hass)
    entries = er.async_entries_for_device(reg, device_id, include_disabled_entities=True)
    anker_entry_id = _anker_entry_id(hass, entries)
    if anker_entry_id is None:
        return {}, list(const.ANKER_ROLE_SUFFIXES)

    by_uid = {ent.unique_id: ent for ent in entries}
    resolved: dict[str, Any] = {}
    missing: list[str] = []
    for conf_key, suffix in const.ANKER_ROLE_SUFFIXES.items():
        ent = by_uid.get(f"{anker_entry_id}_{suffix}")
        if ent is None:
            missing.append(conf_key)
        else:
            resolved[conf_key] = ent.entity_id

    for conf_key, suffix in const.ANKER_SOFT_ROLE_SUFFIXES.items():
        ent = by_uid.get(f"{anker_entry_id}_{suffix}")
        if ent is None:
            _LOGGER.debug(
                "Anker device %s: soft role %s (%s) not found; DEFAULT_ENTITIES will apply",
                device_id,
                conf_key,
                suffix,
            )
        else:
            resolved[conf_key] = ent.entity_id

    cap = by_uid.get(f"{anker_entry_id}_{const.ANKER_CAPACITY_SUFFIX}")
    if cap is not None:
        st = hass.states.get(cap.entity_id)
        if st is not None and st.state not in ("unknown", "unavailable"):
            try:
                val = float(st.state)
            except (TypeError, ValueError):
                val = None
            if val is not None and val > 0:
                resolved[const.CONF_CAPACITY_KWH] = val

    _resolve_power_limits(hass, resolved)
    return resolved, missing


def _numeric(value: Any) -> float | None:
    """Return *value* as a float, rejecting bool (an int subclass) and non-numerics."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _resolve_power_limits(hass: HomeAssistant, resolved: dict[str, Any]) -> None:
    """Derive the charge/export ceilings from the setpoint entity's live bounds.

    The X1's nominal charge/discharge capability is hardware, not user
    preference — and it CHANGES when battery modules are added (2 modules:
    ±6000 W; 4 modules: min -12000 / max 13200).  The number entity publishes
    the true bounds as its ``min``/``max`` attributes, which
    ``Actuator._clamp_to_live_limits`` already reads; deriving the planner's
    ceilings from the same source keeps them in step with the hardware instead
    of frozen at DEFAULT_MAX_CHARGE_W / DEFAULT_MAX_EXPORT_W.

    Sign convention (per the entity's own help text): negative = charge,
    positive = discharge/export.  Soft like capacity — anything missing,
    non-numeric or wrong-signed is omitted so the const default stands, and
    each limit resolves independently of the other.  Mutates *resolved*.
    """
    ent_id = resolved.get(const.CONF_ENT_SETPOINT)
    if not ent_id:
        return
    st = hass.states.get(ent_id)
    if st is None or st.state in ("unknown", "unavailable"):
        return

    live_min = _numeric(st.attributes.get("min"))
    if live_min is not None and live_min < 0:
        resolved[const.CONF_MAX_CHARGE_W] = abs(live_min)
    live_max = _numeric(st.attributes.get("max"))
    if live_max is not None and live_max > 0:
        resolved[const.CONF_MAX_EXPORT_W] = live_max


def apply_anker_resolution(hass: HomeAssistant, data: dict[str, Any]) -> None:
    """Re-resolve the configured Anker device IN MEMORY (reload self-heal).

    Mutates ``data`` in place; NEVER persists.  MUST NOT call
    ``async_update_entry`` — that fires the options update-listener and
    reload-loops.  An unresolved role keeps its last-stored value.
    """
    device_id = data.get(const.CONF_ANKER_DEVICE)
    if not device_id:
        return
    resolved, missing = resolve_anker_config(hass, device_id)
    if missing:
        _LOGGER.warning(
            "Anker device %s: roles %s unresolved on reload; keeping stored ids",
            device_id,
            missing,
        )
    # ent_pv_power is a user-configurable options multi-select (config_flow),
    # not an Anker device role like the others: the device's own PV sensor is
    # only a default for an empty config, never an override of a stored
    # choice, because AC-coupled sites (PV behind a separate inverter) read
    # ~0 W from the Anker-native PV sensor.
    if const.normalize_pv_power_entities(data.get(const.CONF_ENT_PV_POWER)):
        resolved.pop(const.CONF_ENT_PV_POWER, None)
    data.update(resolved)
