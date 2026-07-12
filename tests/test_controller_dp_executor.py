"""C2: compute_decision must be dispatched through async_add_executor_job."""
import functools
from datetime import timedelta

import pytest

from tests.helpers import StubHass as _StubHass
from tests.test_controller_export_executor import (
    _make_controller, _seed_passive_inputs, BASE, ctrl_mod,
)
from custom_components.anker_x1_smartgrid.models import PlanState
from custom_components.anker_x1_smartgrid.controller import ControllerState


@pytest.mark.asyncio
async def test_compute_decision_dispatched_through_executor(monkeypatch):
    """The DP must be dispatched THROUGH async_add_executor_job as a partial
    wrapping compute_decision — NOT called inline on the event loop."""
    monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
    hass = _StubHass()
    ctrl, act, _ = _make_controller(hass)
    _seed_passive_inputs(hass, soc="50.0", export_price="0.10")

    def _cd(*a, **k):
        return (PlanState(ControllerState.PASSIVE, BASE, ()), 0.0,
                BASE + timedelta(hours=1), [], "single-day", [])
    monkeypatch.setattr(ctrl_mod, "compute_decision", _cd)

    dispatched = []
    _real = hass.async_add_executor_job

    async def _record(func, *a):
        dispatched.append(func)
        return await _real(func, *a)
    monkeypatch.setattr(hass, "async_add_executor_job", _record)

    await ctrl.tick()
    assert any(isinstance(f, functools.partial) and f.func is _cd for f in dispatched), \
        "compute_decision was not dispatched through async_add_executor_job"
