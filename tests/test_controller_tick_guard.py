"""Tick wrapper: overlap skip + exception failsafe (review 2026-07-02 findings 1.1/1.2)."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.anker_x1_smartgrid.models import ExportState
from tests.helpers import StubHass as _StubHass
from tests.test_controller_export_executor import _make_controller

# _make_controller stays imported from test_controller_export_executor (not
# tests.helpers.make_controller): it returns a (ctrl, act, store) 3-tuple
# built from export-specific Config overrides (_make_export_cfg), which
# tests.helpers.make_controller's 2-tuple/plain-Config signature doesn't
# match — see the same precedent in commit 0d4068a.


class _StubActuator:
    """Minimal actuator stub exposing release_calls/engaged for failsafe assertions.

    Genuinely differs from tests.helpers.StubActuator (which tracks a
    `calls` list instead of a release_calls counter) — this file's overlap/
    failsafe assertions need the counter, so it stays local rather than
    being replaced."""

    def __init__(self):
        self.engaged: bool = False
        self.release_calls: int = 0

    async def release_to_self(self) -> None:
        self.release_calls += 1
        self.engaged = False

    async def engage_and_charge(self, setpoint_w: float) -> None:
        self.engaged = True

    async def engage_export(self, setpoint_w: float) -> None:
        self.engaged = True


@pytest.fixture
def actuator():
    return _StubActuator()


@pytest.fixture
def controller(actuator):
    hass = _StubHass()
    ctrl, _act, _store = _make_controller(hass, actuator=actuator)
    return ctrl


@pytest.mark.asyncio
async def test_overlapping_tick_is_skipped(controller):
    gate = asyncio.Event()
    entered = 0
    async def slow_impl():
        nonlocal entered
        entered += 1
        await gate.wait()
        return {"state": "slow"}
    controller._tick_impl = slow_impl
    controller.last_status = {"state": "prev"}
    t1 = asyncio.create_task(controller.tick())
    await asyncio.sleep(0)              # let t1 acquire the lock
    result2 = await controller.tick()   # second tick while first is parked
    assert result2 == {"state": "prev"} # returns last_status, does not enter
    assert entered == 1
    gate.set()
    assert await t1 == {"state": "slow"}

@pytest.mark.asyncio
async def test_release_waits_for_in_flight_tick(controller, actuator):
    """release() must not release_to_self until an in-flight tick (holding the
    tick lock) completes — otherwise a reload interleaves release with engage_*."""
    gate = asyncio.Event()
    async def slow_impl():
        await gate.wait()
        return {"state": "slow"}
    controller._tick_impl = slow_impl
    t1 = asyncio.create_task(controller.tick())
    await asyncio.sleep(0)                     # let t1 acquire the tick lock
    rel = asyncio.create_task(controller.release())
    await asyncio.sleep(0)
    assert actuator.release_calls == 0         # release is blocked behind the lock
    gate.set()
    await t1
    await rel
    assert actuator.release_calls == 1         # released only after the tick finished
    assert controller._tick_lock.locked() is False


@pytest.mark.asyncio
async def test_tick_exception_releases_actuator(controller, actuator):
    async def boom():
        raise RuntimeError("malformed forecast")
    controller._tick_impl = boom
    # Seed an ENGAGED export state so the failsafe's guarded reset branch
    # (controller.py: `if self.export_state.engaged: self.export_state = ...`)
    # actually has something to disengage — otherwise the assertion below is
    # vacuously true against the fixture's already-disengaged default.
    controller.export_state = ExportState(
        engaged=True, state_since=datetime.now(timezone.utc) - timedelta(minutes=5)
    )
    status = await controller.tick()
    assert actuator.release_calls >= 1          # release_to_self fired
    assert status["state"] == "failsafe"
    assert controller.export_state.engaged is False
    assert controller.plan.state.value == "passive"

    # The exception path must release the tick lock: a later tick can still enter.
    assert controller._tick_lock.locked() is False
    async def ok():
        return {"reason": "ok"}
    controller._tick_impl = ok
    result2 = await controller.tick()
    assert result2.get("reason") != "disabled"  # actually entered (not the overlap-skip return)
