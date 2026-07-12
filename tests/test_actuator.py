import pytest
from custom_components.anker_x1_smartgrid import const
from custom_components.anker_x1_smartgrid.actuator import Actuator
from tests.conftest import ANKER_TEST_ENTITIES


class _Recorder:
    def __init__(self):
        self.calls = []

    async def _svc(self, domain, service, data, blocking=True):
        self.calls.append((domain, service, data))


@pytest.fixture
def hass_stub():
    class _H:
        def __init__(self):
            self.services = _Recorder()
            self.services.async_call = self.services._svc

    return _H()


async def test_engage_and_charge_orders_calls(hass_stub):
    act = Actuator(hass_stub, {**const.DEFAULT_ENTITIES, **ANKER_TEST_ENTITIES})
    await act.engage_and_charge(-3000.0)
    domains = [(c[0], c[1]) for c in hass_stub.services.calls]
    assert domains[0] == ("switch", "turn_on")
    assert domains[1] == ("number", "set_value")
    assert hass_stub.services.calls[1][2]["value"] == -3000.0
    assert act.last_setpoint_w == -3000.0


async def test_engage_rejects_positive_setpoint(hass_stub):
    act = Actuator(hass_stub, {**const.DEFAULT_ENTITIES, **ANKER_TEST_ENTITIES})
    with pytest.raises(ValueError):
        await act.engage_and_charge(1000.0)


async def test_release_orders_calls(hass_stub):
    act = Actuator(hass_stub, {**const.DEFAULT_ENTITIES, **ANKER_TEST_ENTITIES})
    await act.release_to_self()
    seq = [(c[0], c[1]) for c in hass_stub.services.calls]
    assert seq == [("number", "set_value"), ("select", "select_option"), ("switch", "turn_off")]
    assert act.last_setpoint_w == 0.0


async def test_engaged_defaults_false(hass_stub):
    act = Actuator(hass_stub, {**const.DEFAULT_ENTITIES, **ANKER_TEST_ENTITIES})
    assert act.engaged is False


async def test_engaged_true_after_engage_and_charge(hass_stub):
    act = Actuator(hass_stub, {**const.DEFAULT_ENTITIES, **ANKER_TEST_ENTITIES})
    await act.engage_and_charge(-3000.0)
    assert act.engaged is True


async def test_engaged_false_after_release_to_self(hass_stub):
    act = Actuator(hass_stub, {**const.DEFAULT_ENTITIES, **ANKER_TEST_ENTITIES})
    await act.engage_and_charge(-3000.0)
    await act.release_to_self()
    assert act.engaged is False


# ---------------------------------------------------------------------------
# engage_export tests (A4)
# ---------------------------------------------------------------------------


async def test_engage_export_orders_calls(hass_stub):
    """engage_export: VPP/modbus on → setpoint=+P (positive) in order."""
    act = Actuator(hass_stub, {**const.DEFAULT_ENTITIES, **ANKER_TEST_ENTITIES})
    await act.engage_export(3000.0)
    domains = [(c[0], c[1]) for c in hass_stub.services.calls]
    assert domains[0] == ("switch", "turn_on")
    assert domains[1] == ("number", "set_value")
    assert hass_stub.services.calls[1][2]["value"] == 3000.0
    assert act.last_setpoint_w == 3000.0
    assert act.engaged is True


async def test_engage_export_rejects_nonpositive_setpoint(hass_stub):
    """engage_export raises ValueError for setpoint <= 0."""
    act = Actuator(hass_stub, {**const.DEFAULT_ENTITIES, **ANKER_TEST_ENTITIES})
    with pytest.raises(ValueError):
        await act.engage_export(-1000.0)
    with pytest.raises(ValueError):
        await act.engage_export(0.0)


async def test_release_from_export_restores_self_consumption(hass_stub):
    """release_to_self after export: setpoint→0, workmode→Self-consumption, switch off."""
    act = Actuator(hass_stub, {**const.DEFAULT_ENTITIES, **ANKER_TEST_ENTITIES})
    await act.engage_export(3000.0)
    hass_stub.services.calls.clear()
    await act.release_to_self()
    seq = [(c[0], c[1]) for c in hass_stub.services.calls]
    assert seq == [("number", "set_value"), ("select", "select_option"), ("switch", "turn_off")]
    assert hass_stub.services.calls[0][2]["value"] == 0.0
    assert act.last_setpoint_w == 0.0
    assert act.engaged is False


async def test_engage_and_charge_still_works_unchanged(hass_stub):
    """engage_and_charge (negative setpoint) still works after _engage refactor."""
    act = Actuator(hass_stub, {**const.DEFAULT_ENTITIES, **ANKER_TEST_ENTITIES})
    await act.engage_and_charge(-5000.0)
    domains = [(c[0], c[1]) for c in hass_stub.services.calls]
    assert domains[0] == ("switch", "turn_on")
    assert domains[1] == ("number", "set_value")
    assert hass_stub.services.calls[1][2]["value"] == -5000.0
    assert act.last_setpoint_w == -5000.0
    assert act.engaged is True


# ---------------------------------------------------------------------------
# H3: engaged flag set before set_value (partial-failure releasability)
# ---------------------------------------------------------------------------


async def test_engaged_true_when_set_value_fails():
    """set_value raises (turn_on ok) → engaged MUST already be True so the next
    disabled/failsafe tick can release (no stuck-in-VPP)."""
    calls = []

    class _Svc:
        async def async_call(self, domain, service, data, blocking=True):
            calls.append((domain, service))
            if domain == "number" and service == "set_value":
                raise RuntimeError("set_value failed")

    class _H:
        def __init__(self):
            self.services = _Svc()

    act = Actuator(_H(), {**const.DEFAULT_ENTITIES, **ANKER_TEST_ENTITIES})
    with pytest.raises(RuntimeError):
        await act.engage_and_charge(-3000.0)
    assert act.engaged is True
    assert calls[0] == ("switch", "turn_on")  # turn_on ran before the failure


async def test_engaged_false_when_turn_on_fails():
    """turn_on raises → exception precedes the flag → engaged stays False (no false-engage)."""

    class _Svc:
        async def async_call(self, domain, service, data, blocking=True):
            if domain == "switch" and service == "turn_on":
                raise RuntimeError("turn_on failed")

    class _H:
        def __init__(self):
            self.services = _Svc()

    act = Actuator(_H(), {**const.DEFAULT_ENTITIES, **ANKER_TEST_ENTITIES})
    with pytest.raises(RuntimeError):
        await act.engage_and_charge(-3000.0)
    assert act.engaged is False


# ---------------------------------------------------------------------------
# N1: last_setpoint_w recorded only after the write lands (partial-failure
# telemetry accuracy — do not claim a setpoint we never actually commanded)
# ---------------------------------------------------------------------------


async def test_last_setpoint_w_keeps_previous_value_when_set_value_fails():
    """set_value raises → last_setpoint_w must NOT change to the new value (we
    never actually commanded it), even though engaged is already True."""
    calls = []

    class _Svc:
        async def async_call(self, domain, service, data, blocking=True):
            calls.append((domain, service))
            if domain == "number" and service == "set_value":
                raise RuntimeError("set_value failed")

    class _H:
        def __init__(self):
            self.services = _Svc()

    act = Actuator(_H(), {**const.DEFAULT_ENTITIES, **ANKER_TEST_ENTITIES})
    act.last_setpoint_w = -1500.0  # previous known-good commanded setpoint
    with pytest.raises(RuntimeError):
        await act.engage_and_charge(-3000.0)
    assert act.last_setpoint_w == -1500.0, (
        "last_setpoint_w must keep its previous value when set_value raises "
        f"(the new setpoint was never actually written); got {act.last_setpoint_w}"
    )
    assert act.engaged is True
