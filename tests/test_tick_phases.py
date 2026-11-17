import logging

from custom_components.anker_x1_smartgrid import tick_inputs, tick_publish
from custom_components.anker_x1_smartgrid.models import ControllerState
from tests.helpers import StubHass, make_controller, seed_valid_inputs


async def test_phase_exception_reaches_tick_wrapper(monkeypatch, caplog):
    hass = StubHass()
    ctrl, act = make_controller(hass)
    seed_valid_inputs(hass, soc="20.0")
    boom = RuntimeError("publish phase failed")
    calls = []

    def _raise(*args, **kwargs):
        calls.append(args)
        raise boom

    monkeypatch.setattr(tick_publish, "publish", _raise)

    with caplog.at_level(logging.ERROR, logger="custom_components.anker_x1_smartgrid.controller"):
        result = await ctrl.tick()

    assert len(calls) == 1
    records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(records) == 1
    assert records[0].name == "custom_components.anker_x1_smartgrid.controller"
    assert records[0].levelno == logging.ERROR
    assert records[0].getMessage() == "tick failed; releasing to self-consumption"
    assert records[0].exc_info[1] is boom
    assert act.calls[-1] == ("release_to_self",)
    assert ctrl.plan.state is ControllerState.PASSIVE
    assert result is ctrl.last_status
    assert result["state"] == "failsafe"
    assert result["reason"] == "failsafe"
    assert result["setpoint_w"] == 0.0


async def _assert_phase_failure_reaches_wrapper(monkeypatch, caplog, patch_target):
    hass = StubHass()
    ctrl, act = make_controller(hass)
    seed_valid_inputs(hass, soc="20.0")
    boom = RuntimeError("tick phase failed")
    calls = []

    async def _raise(*args, **kwargs):
        calls.append(args)
        raise boom

    monkeypatch.setattr(*patch_target(ctrl), _raise)

    with caplog.at_level(logging.ERROR, logger="custom_components.anker_x1_smartgrid.controller"):
        result = await ctrl.tick()

    assert len(calls) == 1
    records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(records) == 1
    assert records[0].name == "custom_components.anker_x1_smartgrid.controller"
    assert records[0].levelno == logging.ERROR
    assert records[0].getMessage() == "tick failed; releasing to self-consumption"
    assert records[0].exc_info[1] is boom
    assert act.calls[-1] == ("release_to_self",)
    assert ctrl.plan.state is ControllerState.PASSIVE
    assert result is ctrl.last_status
    assert result["state"] == "failsafe"
    assert result["reason"] == "failsafe"
    assert result["setpoint_w"] == 0.0


async def test_gather_exception_reaches_tick_wrapper(monkeypatch, caplog):
    await _assert_phase_failure_reaches_wrapper(monkeypatch, caplog, lambda ctrl: (tick_inputs, "gather"))


async def test_decide_exception_reaches_tick_wrapper(monkeypatch, caplog):
    await _assert_phase_failure_reaches_wrapper(monkeypatch, caplog, lambda ctrl: (ctrl, "_tick_decide"))
