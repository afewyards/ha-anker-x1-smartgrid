"""TDD tests for the computed house load (controller._compute_house_load_w).

The integration no longer reads a derived CONF_ENT_HOUSE_LOAD sensor; instead it
computes house load each tick from the Anker X1 meter's signed net-grid scalar:

    house_load_w = pv + meter_w + batt − inverter_loss

pv/batt = read_float(CONF_ENT_PV_POWER / CONF_ENT_BATTERY_POWER); batt is
+ = discharge, − = charge. inverter_loss is treated as 0.0 when its sensor is
unavailable (it genuinely reads 0 while charging/idle and may drop out). If pv
or batt is unavailable, the compute is skipped entirely and the cached
_last_house_load_w is returned instead (N2 telemetry fallback), which is also
refreshed on every successful compute.
"""
from __future__ import annotations

from datetime import datetime, timezone

from custom_components.anker_x1_smartgrid import const
from custom_components.anker_x1_smartgrid.controller import Controller
from custom_components.anker_x1_smartgrid.models import PlantInputs

NOW = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)


class _StateObj:
    def __init__(self, state):
        self.state = state


class _States:
    def __init__(self):
        self._states: dict[str, _StateObj] = {}

    def get(self, entity_id):
        return self._states.get(entity_id)

    def set(self, entity_id, state):
        self._states[entity_id] = _StateObj(state)


class _Hass:
    def __init__(self):
        self.states = _States()


def _make_ctrl(hass, *, last_house_load_w: float = 0.0) -> Controller:
    """Minimal Controller carrying only what _compute_house_load_w touches."""
    ctrl = Controller.__new__(Controller)
    ctrl._hass = hass
    ctrl._data = {
        const.CONF_ENT_PV_POWER: "sensor.pv",
        const.CONF_ENT_BATTERY_POWER: "sensor.batt",
        const.CONF_ENT_INVERTER_LOSS: "sensor.loss",
    }
    ctrl._last_house_load_w = last_house_load_w
    ctrl._house_load_fresh = False
    return ctrl


def test_normal_compute_is_pv_plus_meter_plus_batt_minus_loss():
    hass = _Hass()
    hass.states.set("sensor.pv", "500.0")
    hass.states.set("sensor.batt", "-200.0")  # charging
    hass.states.set("sensor.loss", "30.0")
    ctrl = _make_ctrl(hass)
    inputs = PlantInputs(soc=50.0, meter_w=100.0, now=NOW)

    result = ctrl._compute_house_load_w(inputs)

    assert result == 500.0 + 100.0 + (-200.0) - 30.0
    # Successful compute refreshes the fallback cache.
    assert ctrl._last_house_load_w == result


def test_loss_missing_entity_id_treated_as_zero():
    """The loss entity resolves to a state that isn't present → None → 0.0."""
    hass = _Hass()
    hass.states.set("sensor.pv", "500.0")
    hass.states.set("sensor.batt", "0.0")
    # sensor.loss never set → read_float returns None
    ctrl = _make_ctrl(hass)
    inputs = PlantInputs(soc=50.0, meter_w=100.0, now=NOW)

    result = ctrl._compute_house_load_w(inputs)

    assert result == 500.0 + 100.0 + 0.0 - 0.0


def test_loss_unavailable_state_treated_as_zero():
    """Loss sensor present but reporting 'unavailable' (e.g. idle) → 0.0."""
    hass = _Hass()
    hass.states.set("sensor.pv", "500.0")
    hass.states.set("sensor.batt", "0.0")
    hass.states.set("sensor.loss", "unavailable")
    ctrl = _make_ctrl(hass)
    inputs = PlantInputs(soc=50.0, meter_w=100.0, now=NOW)

    result = ctrl._compute_house_load_w(inputs)

    assert result == 600.0


def test_pv_none_falls_back_to_cached_last_house_load():
    hass = _Hass()
    # sensor.pv never set → read_float returns None
    hass.states.set("sensor.batt", "0.0")
    hass.states.set("sensor.loss", "0.0")
    ctrl = _make_ctrl(hass, last_house_load_w=321.0)
    inputs = PlantInputs(soc=50.0, meter_w=100.0, now=NOW)

    result = ctrl._compute_house_load_w(inputs)

    assert result == 321.0
    # No fresh compute happened, so the cache is left untouched.
    assert ctrl._last_house_load_w == 321.0


def test_batt_none_falls_back_to_cached_last_house_load():
    hass = _Hass()
    hass.states.set("sensor.pv", "500.0")
    # sensor.batt never set → read_float returns None
    hass.states.set("sensor.loss", "0.0")
    ctrl = _make_ctrl(hass, last_house_load_w=88.0)
    inputs = PlantInputs(soc=50.0, meter_w=100.0, now=NOW)

    result = ctrl._compute_house_load_w(inputs)

    assert result == 88.0
    assert ctrl._last_house_load_w == 88.0


def _make_ctrl_no_loss_key(hass, *, last_house_load_w: float = 0.0) -> Controller:
    """Same as _make_ctrl but omits CONF_ENT_INVERTER_LOSS entirely from
    _data — mirrors a pre-upgrade config entry that never persisted this key
    (it only existed at runtime via apply_anker_resolution's in-memory
    mutation). Exercises the DEFAULT_ENTITIES .get() fallback."""
    ctrl = Controller.__new__(Controller)
    ctrl._hass = hass
    ctrl._data = {
        const.CONF_ENT_PV_POWER: "sensor.pv",
        const.CONF_ENT_BATTERY_POWER: "sensor.batt",
    }
    ctrl._last_house_load_w = last_house_load_w
    ctrl._house_load_fresh = False
    return ctrl


def test_loss_key_absent_from_data_falls_back_to_default_entity():
    """CONF_ENT_INVERTER_LOSS key missing from _data (pre-upgrade config
    entry) → falls back to the DEFAULT_ENTITIES entity id and reads its live
    numeric state instead of raising KeyError."""
    hass = _Hass()
    hass.states.set("sensor.pv", "500.0")
    hass.states.set("sensor.batt", "-200.0")  # charging
    hass.states.set(const.DEFAULT_ENTITIES[const.CONF_ENT_INVERTER_LOSS], "30.0")
    ctrl = _make_ctrl_no_loss_key(hass)
    inputs = PlantInputs(soc=50.0, meter_w=100.0, now=NOW)

    result = ctrl._compute_house_load_w(inputs)

    assert result == 500.0 + 100.0 + (-200.0) - 30.0
    assert ctrl._last_house_load_w == result


def test_loss_key_absent_and_default_entity_unavailable_treated_as_zero():
    """CONF_ENT_INVERTER_LOSS key missing AND the DEFAULT_ENTITIES fallback
    entity has no live state → loss = 0.0 (no KeyError anywhere)."""
    hass = _Hass()
    hass.states.set("sensor.pv", "500.0")
    hass.states.set("sensor.batt", "0.0")
    # DEFAULT_ENTITIES fallback entity never set → read_float returns None
    ctrl = _make_ctrl_no_loss_key(hass)
    inputs = PlantInputs(soc=50.0, meter_w=100.0, now=NOW)

    result = ctrl._compute_house_load_w(inputs)

    assert result == 600.0


# ---------------------------------------------------------------------------
# Clamp: house load cannot physically be negative (B).
# ---------------------------------------------------------------------------


def test_negative_formula_clamped_to_zero():
    """Cross-read skew (heavy charging outpacing pv+meter this tick) can make
    the raw formula go negative; the compute must clamp it to 0.0."""
    hass = _Hass()
    hass.states.set("sensor.pv", "0.0")
    hass.states.set("sensor.batt", "-500.0")  # charging hard
    hass.states.set("sensor.loss", "0.0")
    ctrl = _make_ctrl(hass)
    inputs = PlantInputs(soc=50.0, meter_w=100.0, now=NOW)
    # raw = 0 + 100 + (-500) - 0 = -400 → clamped to 0.0

    result = ctrl._compute_house_load_w(inputs)

    assert result == 0.0
    # The clamped (non-negative) value is what gets cached, not the raw negative.
    assert ctrl._last_house_load_w == 0.0


# ---------------------------------------------------------------------------
# Fresh-flag semantics (A): distinguishes a live compute from a cache-fallback.
# ---------------------------------------------------------------------------


def test_successful_compute_marks_fresh_true():
    hass = _Hass()
    hass.states.set("sensor.pv", "500.0")
    hass.states.set("sensor.batt", "0.0")
    hass.states.set("sensor.loss", "0.0")
    ctrl = _make_ctrl(hass)
    inputs = PlantInputs(soc=50.0, meter_w=100.0, now=NOW)

    ctrl._compute_house_load_w(inputs)

    assert ctrl._house_load_fresh is True


def test_pv_none_marks_fresh_false():
    hass = _Hass()
    # sensor.pv never set → read_float returns None
    hass.states.set("sensor.batt", "0.0")
    hass.states.set("sensor.loss", "0.0")
    ctrl = _make_ctrl(hass, last_house_load_w=321.0)
    inputs = PlantInputs(soc=50.0, meter_w=100.0, now=NOW)

    ctrl._compute_house_load_w(inputs)

    assert ctrl._house_load_fresh is False


def test_batt_none_marks_fresh_false():
    hass = _Hass()
    hass.states.set("sensor.pv", "500.0")
    # sensor.batt never set → read_float returns None
    hass.states.set("sensor.loss", "0.0")
    ctrl = _make_ctrl(hass, last_house_load_w=88.0)
    inputs = PlantInputs(soc=50.0, meter_w=100.0, now=NOW)

    ctrl._compute_house_load_w(inputs)

    assert ctrl._house_load_fresh is False


def test_fresh_flag_recovers_true_after_a_prior_cache_fallback_tick():
    """A cache-fallback tick marks fresh False; the NEXT tick with both sensors
    back should mark it fresh True again (the flag reflects THIS call only)."""
    hass = _Hass()
    ctrl = _make_ctrl(hass, last_house_load_w=50.0)
    inputs = PlantInputs(soc=50.0, meter_w=100.0, now=NOW)

    # Tick 1: pv/batt unavailable → cache fallback.
    ctrl._compute_house_load_w(inputs)
    assert ctrl._house_load_fresh is False

    # Tick 2: sensors back → live compute.
    hass.states.set("sensor.pv", "500.0")
    hass.states.set("sensor.batt", "0.0")
    hass.states.set("sensor.loss", "0.0")
    ctrl._compute_house_load_w(inputs)
    assert ctrl._house_load_fresh is True
