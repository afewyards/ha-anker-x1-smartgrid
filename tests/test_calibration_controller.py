"""Calibration override at the controller boundary: isolation + active path."""

import dataclasses
from datetime import timedelta

import pytest

from custom_components.anker_x1_smartgrid import calibration
from tests.helpers import BASE, StubHass, make_controller, seed_valid_inputs


@pytest.mark.asyncio
async def test_disabled_never_consults_the_policy(monkeypatch):
    """calibration_enabled=False => behaviour identical to today."""
    hass = StubHass()
    ctrl, _act = make_controller(hass)
    seed_valid_inputs(hass, soc="50.0")
    called = False

    def _spy(*a, **k):
        nonlocal called
        called = True
        return None

    monkeypatch.setattr(calibration, "calibration_action", _spy)
    result = await ctrl.tick()
    assert called is False, "policy must not even be consulted when disabled"
    assert result["state"] == "passive"


@pytest.mark.asyncio
async def test_active_calibration_forces_charge(monkeypatch):
    """Active calibration flips the plan to FORCING regardless of the DP.

    soc=98 would otherwise decide PASSIVE (see
    test_controller.py::test_tick_forcing_to_passive_calls_release), so a
    FORCING outcome here can only come from the override.
    """
    hass = StubHass()
    ctrl, act = make_controller(hass)
    seed_valid_inputs(hass, soc="98.0")
    ctrl.cfg = dataclasses.replace(ctrl.cfg, calibration_enabled=True)
    action = calibration.CalibAction(
        phase="holding",
        window_start=BASE - timedelta(hours=1),
        window_end=BASE + timedelta(hours=2),
    )
    monkeypatch.setattr(calibration, "calibration_action", lambda *a, **k: action)

    result = await ctrl.tick()

    assert result["state"] == "forcing"
    assert ctrl.last_status["calibration_state"] == "holding"
    assert any(c[0] == "engage_and_charge" for c in act.calls)
