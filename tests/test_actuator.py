import pytest
from custom_components.anker_x1_smartgrid import const
from custom_components.anker_x1_smartgrid.actuator import Actuator
from tests.conftest import ANKER_TEST_ENTITIES


class _Recorder:
    def __init__(self):
        self.calls = []

    async def _svc(self, domain, service, data, blocking=True):
        self.calls.append((domain, service, data))


class _State:
    def __init__(self, state, attributes=None):
        self.state = state
        self.attributes = attributes or {}


class _States:
    def __init__(self, parent):
        self._parent = parent

    def get(self, entity_id):
        return self._parent._states.get(entity_id)


class _NoStates:
    """Minimal ``hass.states`` stand-in for bespoke test doubles that don't
    otherwise track entity state: ``.get()`` always reports "missing", which
    is the live-limit clamp's unchanged-value path."""

    def get(self, entity_id):
        return None


@pytest.fixture
def hass_stub():
    class _H:
        def __init__(self):
            self.services = _Recorder()
            self.services.async_call = self.services._svc
            self._states = {}

        def set_state(self, entity_id, state, attributes=None):
            self._states[entity_id] = _State(state, attributes)

        @property
        def states(self):
            return _States(self)

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
            self.states = _NoStates()

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
            self.states = _NoStates()

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
            self.states = _NoStates()

    act = Actuator(_H(), {**const.DEFAULT_ENTITIES, **ANKER_TEST_ENTITIES})
    act.last_setpoint_w = -1500.0  # previous known-good commanded setpoint
    with pytest.raises(RuntimeError):
        await act.engage_and_charge(-3000.0)
    assert act.last_setpoint_w == -1500.0, (
        "last_setpoint_w must keep its previous value when set_value raises "
        f"(the new setpoint was never actually written); got {act.last_setpoint_w}"
    )
    assert act.engaged is True


# ---------------------------------------------------------------------------
# Live-limit clamp: the setpoint entity's min/max attributes are LIVE
# inverter BMS limits that float over time and can be tighter than the
# static SETPOINT_MIN_W/SETPOINT_MAX_W guard constants. Writing outside the
# live range raises ServiceValidationError and half-engages the inverter
# (modbus on, setpoint never written) — see executor.py's FORCING recovery.
# ---------------------------------------------------------------------------

_SETPOINT_ENTITY = ANKER_TEST_ENTITIES[const.CONF_ENT_SETPOINT]


async def test_engage_and_charge_clamped_to_live_min(hass_stub):
    """-6000 W with live min -5910 -> clamped to -5910, then floored toward
    zero onto the 100 W step grid -> -5900."""
    hass_stub.set_state(_SETPOINT_ENTITY, "0.0", {"min": -5910.0, "max": 6600.0, "step": 100.0})
    act = Actuator(hass_stub, {**const.DEFAULT_ENTITIES, **ANKER_TEST_ENTITIES})
    await act.engage_and_charge(-6000.0)
    assert hass_stub.services.calls[1][2]["value"] == -5900.0
    assert act.last_setpoint_w == -5900.0


async def test_engage_and_charge_in_range_setpoint_unchanged(hass_stub):
    """A setpoint already within the live min/max (and already grid-aligned)
    passes through unchanged."""
    hass_stub.set_state(_SETPOINT_ENTITY, "0.0", {"min": -5910.0, "max": 6600.0, "step": 100.0})
    act = Actuator(hass_stub, {**const.DEFAULT_ENTITIES, **ANKER_TEST_ENTITIES})
    await act.engage_and_charge(-3000.0)
    assert hass_stub.services.calls[1][2]["value"] == -3000.0
    assert act.last_setpoint_w == -3000.0


async def test_engage_and_charge_missing_entity_state_leaves_value_unchanged(hass_stub):
    """No state registered for the setpoint entity (missing/never seen) ->
    the static guard clamp already applied upstream is left as-is."""
    act = Actuator(hass_stub, {**const.DEFAULT_ENTITIES, **ANKER_TEST_ENTITIES})
    await act.engage_and_charge(-6000.0)
    assert hass_stub.services.calls[1][2]["value"] == -6000.0
    assert act.last_setpoint_w == -6000.0


async def test_engage_and_charge_unavailable_entity_state_leaves_value_unchanged(hass_stub):
    """Entity state present but 'unavailable' -> value unchanged."""
    hass_stub.set_state(_SETPOINT_ENTITY, "unavailable", {"min": -5910.0, "max": 6600.0})
    act = Actuator(hass_stub, {**const.DEFAULT_ENTITIES, **ANKER_TEST_ENTITIES})
    await act.engage_and_charge(-6000.0)
    assert hass_stub.services.calls[1][2]["value"] == -6000.0
    assert act.last_setpoint_w == -6000.0


async def test_engage_and_charge_missing_min_max_attributes_leaves_value_unchanged(hass_stub):
    """State present but min/max attributes absent -> value unchanged."""
    hass_stub.set_state(_SETPOINT_ENTITY, "0.0", {})
    act = Actuator(hass_stub, {**const.DEFAULT_ENTITIES, **ANKER_TEST_ENTITIES})
    await act.engage_and_charge(-6000.0)
    assert hass_stub.services.calls[1][2]["value"] == -6000.0
    assert act.last_setpoint_w == -6000.0


async def test_engage_and_charge_non_numeric_min_max_attributes_leaves_value_unchanged(hass_stub):
    """State present but min/max attributes are non-numeric -> value unchanged."""
    hass_stub.set_state(_SETPOINT_ENTITY, "0.0", {"min": "n/a", "max": None})
    act = Actuator(hass_stub, {**const.DEFAULT_ENTITIES, **ANKER_TEST_ENTITIES})
    await act.engage_and_charge(-6000.0)
    assert hass_stub.services.calls[1][2]["value"] == -6000.0
    assert act.last_setpoint_w == -6000.0


async def test_engage_export_clamped_to_live_max(hass_stub):
    """+7000 W with live max 6600 -> clamped to 6600 (already grid-aligned)."""
    hass_stub.set_state(_SETPOINT_ENTITY, "0.0", {"min": -5910.0, "max": 6600.0, "step": 100.0})
    act = Actuator(hass_stub, {**const.DEFAULT_ENTITIES, **ANKER_TEST_ENTITIES})
    await act.engage_export(7000.0)
    assert hass_stub.services.calls[1][2]["value"] == 6600.0
    assert act.last_setpoint_w == 6600.0


async def test_engage_export_clamped_to_live_max_floored_to_step(hass_stub):
    """+7000 W with a live max of 6550 (not grid-aligned) -> clamped to 6550,
    then floored toward zero onto the 100 W step grid -> 6500."""
    hass_stub.set_state(_SETPOINT_ENTITY, "0.0", {"min": -5910.0, "max": 6550.0, "step": 100.0})
    act = Actuator(hass_stub, {**const.DEFAULT_ENTITIES, **ANKER_TEST_ENTITIES})
    await act.engage_export(7000.0)
    assert hass_stub.services.calls[1][2]["value"] == 6500.0
    assert act.last_setpoint_w == 6500.0


async def test_engage_and_charge_clamp_logs_warning_when_value_changes(hass_stub, caplog):
    """A clamp that actually changes the commanded value logs a WARNING with
    both the requested and clamped values."""
    hass_stub.set_state(_SETPOINT_ENTITY, "0.0", {"min": -5910.0, "max": 6600.0, "step": 100.0})
    act = Actuator(hass_stub, {**const.DEFAULT_ENTITIES, **ANKER_TEST_ENTITIES})
    with caplog.at_level("WARNING"):
        await act.engage_and_charge(-6000.0)
    assert "clamp" in caplog.text.lower()
    assert "-6000" in caplog.text
    assert "-5900" in caplog.text


async def test_engage_and_charge_in_range_clamp_does_not_log_warning(hass_stub, caplog):
    """No warning is logged when the value is unchanged by clamping."""
    hass_stub.set_state(_SETPOINT_ENTITY, "0.0", {"min": -5910.0, "max": 6600.0, "step": 100.0})
    act = Actuator(hass_stub, {**const.DEFAULT_ENTITIES, **ANKER_TEST_ENTITIES})
    with caplog.at_level("WARNING"):
        await act.engage_and_charge(-3000.0)
    assert "clamp" not in caplog.text.lower()


# ---------------------------------------------------------------------------
# Sign guard: a pathological live limit must never flip the commanded
# direction (charge -> discharge or vice-versa). The clamp must collapse to
# 0.0 (honest idle) instead of writing a sign-flipped setpoint.
# ---------------------------------------------------------------------------


async def test_engage_and_charge_pathological_live_min_clamps_to_zero_not_flipped(hass_stub):
    """Charge request (-6000) with a wrong-signed live min (+300, >= 0) must
    NOT flip to a positive (discharge) command -> clamps to 0.0 instead."""
    hass_stub.set_state(_SETPOINT_ENTITY, "0.0", {"min": 300.0, "max": 6600.0, "step": 100.0})
    act = Actuator(hass_stub, {**const.DEFAULT_ENTITIES, **ANKER_TEST_ENTITIES})
    await act.engage_and_charge(-6000.0)
    assert hass_stub.services.calls[1][2]["value"] == 0.0
    assert act.last_setpoint_w == 0.0


async def test_engage_export_pathological_live_max_clamps_to_zero_not_flipped(hass_stub):
    """Export request (+7000) with a wrong-signed live max (-300, <= 0) must
    NOT flip to a negative (charge) command -> clamps to 0.0 instead."""
    hass_stub.set_state(_SETPOINT_ENTITY, "0.0", {"min": -5910.0, "max": -300.0, "step": 100.0})
    act = Actuator(hass_stub, {**const.DEFAULT_ENTITIES, **ANKER_TEST_ENTITIES})
    await act.engage_export(7000.0)
    assert hass_stub.services.calls[1][2]["value"] == 0.0
    assert act.last_setpoint_w == 0.0
