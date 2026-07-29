"""Tests for RACE 1: retrying device-derived limit resolution on tick.

2026-07-29 (2-module -> 4-module battery upgrade): apply_anker_resolution
(anker_resolver.py) re-derives capacity_kwh and the charge/export ceilings
from the anker_x1 device's own entities, but __init__.py only calls it ONCE,
at config-entry setup. If the anker_x1 integration has not yet populated the
battery_nominal_capacity sensor or the setpoint number's min/max attributes
at that exact moment -- a live restart race (Supervisor log: Core reached
RUNNING ~90s after those entities got their first state, following an
add-battery-module restart) -- the values are silently omitted (they are
"soft" roles; see anker_resolver._resolve_power_limits) and the stale/
default capacity_kwh / max_charge_w / max_export_w stick FOREVER, with no
log line, until a manual integration reload. That is exactly what happened:
the planner ran all evening sized for 10 kWh instead of 20 kWh, and at 6 kW
charge instead of 12 kW.

Controller._maybe_reresolve_anker retries resolution on later ticks while
genuinely unresolved, then stops -- these tests lock that behaviour using
the REAL anker_resolver functions against a real entity registry (not
mocked), mirroring tests/test_anker_resolver.py's device-registration
pattern.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.anker_x1_smartgrid import const
from custom_components.anker_x1_smartgrid.controller import Controller
from tests.helpers import StubActuator, StubRecorder, StubStore


def _register_anker_device(hass, *, capacity_state="10.0", setpoint_state=None, setpoint_attrs=None):
    """Minimal anker_x1 device + setpoint/capacity entities.

    Mirrors tests/test_anker_resolver.py::_register_anker_device, trimmed to
    just the two soft roles this race concerns (capacity + setpoint
    min/max). ``setpoint_state`` defaults to None (no state written) so the
    power-limit resolution path stays dormant unless a test opts in --
    mirroring an anker_x1 integration that has not populated the number
    entity yet.
    """
    src = MockConfigEntry(domain=const.ANKER_X1_DOMAIN, title="Anker X1")
    src.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=src.entry_id,
        identifiers={(const.ANKER_X1_DOMAIN, "SER-TEST-1")},
    )
    reg = er.async_get(hass)
    setpoint = reg.async_get_or_create(
        "number",
        const.ANKER_X1_DOMAIN,
        f"{src.entry_id}_battery_setpoint",
        config_entry=src,
        device_id=device.id,
        suggested_object_id="anker_x1_battery_setpoint_charge_discharge",
    )
    if setpoint_state is not None:
        hass.states.async_set(setpoint.entity_id, setpoint_state, setpoint_attrs or {})
    if capacity_state is not None:
        cap = reg.async_get_or_create(
            "sensor",
            const.ANKER_X1_DOMAIN,
            f"{src.entry_id}_{const.ANKER_CAPACITY_SUFFIX}",
            config_entry=src,
            device_id=device.id,
            suggested_object_id="anker_x1_battery_nominal_capacity",
        )
        hass.states.async_set(cap.entity_id, capacity_state)
    return device.id


def _make_controller(hass, device_id=None, **data_overrides):
    data = {}
    if device_id is not None:
        data[const.CONF_ANKER_DEVICE] = device_id
    data.update(data_overrides)
    return Controller(
        hass=hass,
        data=data,
        recorder=StubRecorder(),
        actuator=StubActuator(),
        store=StubStore(),
    )


async def test_pending_false_when_no_anker_device(hass):
    """No CONF_ANKER_DEVICE configured at all -> nothing to ever retry."""
    ctrl = _make_controller(hass, device_id=None)
    assert ctrl._anker_resolution_pending is False

    with patch("custom_components.anker_x1_smartgrid.controller.resolve_anker_config") as mock_resolve:
        ctrl._maybe_reresolve_anker()

    mock_resolve.assert_not_called()


async def test_retry_stays_pending_while_unresolved(hass):
    """Setpoint entity has no state yet (anker_x1 not populated) -> the
    stale const values stick and the pending flag stays True across ticks,
    matching the 2026-07-29 race (planner sized for the old hardware for the
    whole evening)."""
    device_id = _register_anker_device(hass, capacity_state="unavailable", setpoint_state=None)
    ctrl = _make_controller(
        hass,
        device_id,
        capacity_kwh=10.0,
        max_charge_w=6000.0,
        max_export_w=6000.0,
    )
    assert ctrl._anker_resolution_pending is True

    ctrl._maybe_reresolve_anker()

    assert ctrl._anker_resolution_pending is True, "must keep retrying — nothing resolved yet"
    assert ctrl.cfg.capacity_kwh == 10.0
    assert ctrl.cfg.max_charge_w == 6000.0
    assert ctrl.cfg.max_export_w == 6000.0


async def test_retry_resolves_and_logs_once_live_state_appears(hass, caplog):
    """A later tick after the anker_x1 integration finally reports state:
    cfg picks up the new capacity/limits without a reload, and the change is
    logged at INFO (previously totally silent)."""
    device_id = _register_anker_device(hass, capacity_state="unavailable", setpoint_state=None)
    ctrl = _make_controller(
        hass,
        device_id,
        capacity_kwh=10.0,
        max_charge_w=6000.0,
        max_export_w=6000.0,
    )
    ctrl._maybe_reresolve_anker()
    assert ctrl._anker_resolution_pending is True  # sanity: still stuck before

    # anker_x1 now reports state (module upgrade complete + integration ready).
    hass.states.async_set("sensor.anker_x1_battery_nominal_capacity", "20.0")
    hass.states.async_set(
        "number.anker_x1_battery_setpoint_charge_discharge",
        "0",
        {"min": -12000.0, "max": 13200.0},
    )

    caplog.set_level(logging.INFO)
    ctrl._maybe_reresolve_anker()

    assert ctrl._anker_resolution_pending is False
    assert ctrl.cfg.capacity_kwh == 20.0
    assert ctrl.cfg.max_charge_w == 12000.0
    assert ctrl.cfg.max_export_w == 13200.0
    assert "battery capacity resolved late: 10.0 -> 20.0 kWh" in caplog.text
    assert "max charge power resolved late: 6000.0 -> 12000.0 W" in caplog.text
    assert "max export power resolved late: 6000.0 -> 13200.0 W" in caplog.text


async def test_retry_stops_after_resolution(hass):
    """Once resolved, later ticks must not keep re-deriving the same values
    — 'genuinely unresolved' means the retry stops for good once every
    device-derived limit has landed at least once."""
    device_id = _register_anker_device(
        hass,
        capacity_state="20.0",
        setpoint_state="0",
        setpoint_attrs={"min": -12000.0, "max": 13200.0},
    )
    ctrl = _make_controller(hass, device_id)
    ctrl._maybe_reresolve_anker()
    assert ctrl._anker_resolution_pending is False
    assert ctrl.cfg.capacity_kwh == 20.0

    with patch("custom_components.anker_x1_smartgrid.controller.resolve_anker_config") as mock_resolve:
        ctrl._maybe_reresolve_anker()

    mock_resolve.assert_not_called()


async def test_retry_partial_resolution_applies_independently(hass):
    """Capacity resolves but the setpoint entity is still stateless (only
    one of the two anker_x1 entities came up so far). The capacity fix must
    apply immediately rather than waiting on the unrelated setpoint entity —
    they're independent anker_x1 entities that can populate at different
    times — while the retry stays pending for the still-stale power limits."""
    device_id = _register_anker_device(hass, capacity_state="20.0", setpoint_state=None)
    ctrl = _make_controller(hass, device_id, capacity_kwh=10.0, max_charge_w=6000.0, max_export_w=6000.0)

    ctrl._maybe_reresolve_anker()

    assert ctrl.cfg.capacity_kwh == 20.0, "capacity landed — must not wait on the setpoint entity"
    assert ctrl.cfg.max_charge_w == 6000.0
    assert ctrl.cfg.max_export_w == 6000.0
    assert ctrl._anker_resolution_pending is True, "power limits still unresolved — must keep retrying"


async def test_retry_completes_on_later_tick_after_partial_resolution(hass, caplog):
    """Continuation of the partial-resolution scenario: once the setpoint
    entity ALSO reports state on a later tick, the retry finishes (pending
    clears) and only the newly-landed power limits are logged — the
    already-applied capacity value is unchanged, so it must not log again."""
    device_id = _register_anker_device(hass, capacity_state="20.0", setpoint_state=None)
    ctrl = _make_controller(hass, device_id, capacity_kwh=10.0, max_charge_w=6000.0, max_export_w=6000.0)
    ctrl._maybe_reresolve_anker()
    assert ctrl.cfg.capacity_kwh == 20.0  # sanity: capacity already landed

    hass.states.async_set(
        "number.anker_x1_battery_setpoint_charge_discharge",
        "0",
        {"min": -12000.0, "max": 13200.0},
    )
    caplog.set_level(logging.INFO)
    caplog.clear()
    ctrl._maybe_reresolve_anker()

    assert ctrl._anker_resolution_pending is False
    assert ctrl.cfg.max_charge_w == 12000.0
    assert ctrl.cfg.max_export_w == 13200.0
    assert "max charge power resolved late: 6000.0 -> 12000.0 W" in caplog.text
    assert "battery capacity" not in caplog.text, "unchanged capacity must not log again"
