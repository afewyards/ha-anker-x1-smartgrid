"""Canonical shared test doubles + factories.

Single source of truth for the StubHass/StubActuator/StubRecorder/StubStore
family that was previously copy-pasted (often byte-identically) across
dozens of test files, plus the ``make_config``/``make_controller`` factory
pair. See docs/superpowers/plans/2026-07-12-refactor-audit-execution.md
Task B1/B2.

Nothing in the test suite imports this module yet (Task B1 only creates
it); Task B2 migrates call sites over in batches.

Provenance (do not silently drift — these are verbatim/near-verbatim
copies of the richest existing implementation, not new behavior):
- StubHass (+ _StateObj/_States): tests/test_controller.py::_StubHass
- StubActuator: tests/test_controller_export_executor.py::_StubActuator
  (superset of tests/test_controller.py::_StubActuator — adds
  engage_export), extended with an injectable ``fail_on`` hook that
  reproduces tests/test_controller_export_executor.py's
  _RaisingExportActuator / _RaisingChargeActuator, and is a drop-in
  replacement for tests/test_controller_phase2.py::_NoopActuator.
- StubRecorder: tests/test_controller.py::_StubRecorder, plus
  read_persons_home_samples (verbatim from
  tests/test_controller_remote.py::_StubRecorder / tests/test_sensor.py)
  so the stub is a superset of every variant it replaces.
- StubStore: tests/test_controller.py::_StubStore
- CapturingStore: tests/test_controller.py::_CapturingStore
- make_config: tests/test_optimize_parity.py::make_cfg (verbatim, renamed)
- make_controller / seed_valid_inputs: tests/test_controller.py::
  _make_controller / _seed_valid_inputs
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from custom_components.anker_x1_smartgrid import const, controller
from custom_components.anker_x1_smartgrid.models import Config

# Anchor used by seed_valid_inputs for forecast/sunset timestamps — matches
# tests/test_controller.py's module-level BASE verbatim.
BASE = datetime(2026, 6, 20, 11, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# StubActuator — capturing, with injectable failure hooks
# ---------------------------------------------------------------------------


class StubActuator:
    """Records engage_and_charge / engage_export / release_to_self calls.

    ``fail_on`` is an optional set of method names ({"engage_and_charge",
    "engage_export", "release_to_self"}) that should raise instead of
    completing normally — this expresses the _RaisingExportActuator /
    _RaisingChargeActuator variants from test_controller_export_executor.py
    without needing a bespoke subclass per test. The failing call is still
    recorded in ``calls`` before the exception propagates, mirroring those
    variants. With no fail_on (the default), this is also a drop-in
    replacement for the _NoopActuator pattern (callers that don't inspect
    ``calls``/``engaged`` can ignore them).
    """

    def __init__(self, fail_on: set[str] | None = None):
        self.calls: list[tuple] = []
        self.last_setpoint_w: float = 0.0
        self.engaged: bool = False
        self.fail_on = fail_on or set()

    async def engage_and_charge(self, setpoint_w: float) -> None:
        if "engage_and_charge" in self.fail_on:
            self.calls.append(("engage_and_charge", setpoint_w))
            raise RuntimeError("modbus busy")
        self.calls.append(("engage_and_charge", setpoint_w))
        self.last_setpoint_w = setpoint_w
        self.engaged = True

    async def engage_export(self, setpoint_w: float) -> None:
        if setpoint_w <= 0:
            raise ValueError(f"export-only: setpoint must be > 0, got {setpoint_w}")
        if "engage_export" in self.fail_on:
            self.calls.append(("engage_export_attempt", setpoint_w))
            raise RuntimeError("set_value failed mid-engage")
        self.calls.append(("engage_export", setpoint_w))
        self.last_setpoint_w = setpoint_w
        self.engaged = True

    async def release_to_self(self) -> None:
        if "release_to_self" in self.fail_on:
            self.calls.append(("release_to_self_attempt",))
            raise RuntimeError("release failed")
        self.calls.append(("release_to_self",))
        self.last_setpoint_w = 0.0
        self.engaged = False


# ---------------------------------------------------------------------------
# StubStore / CapturingStore
# ---------------------------------------------------------------------------


class StubStore:
    """No-op store."""

    async def async_save(self, data):
        pass


class CapturingStore:
    """Store that records the last saved payload."""

    def __init__(self):
        self.saved = None

    async def async_save(self, data):
        self.saved = data


# ---------------------------------------------------------------------------
# StubRecorder
# ---------------------------------------------------------------------------


class StubRecorder:
    """Captures appended rows (samples + decisions + daily_regret)."""

    def __init__(self):
        self.rows: list[dict] = []
        self.decision_rows: list[dict] = []
        self.daily_regret_rows: dict[str, dict] = {}
        self._load_samples: list[tuple[str, float]] = []
        self._hourly_rows: list[dict] = []

    def append(self, row):
        self.rows.append(row)

    def append_decision(self, **kwargs):
        self.decision_rows.append(kwargs)

    def purge_older_than(self, ts, days):
        pass

    def purge_decisions_older_than(self, cutoff_iso):
        return 0

    def rollup_hours(self, now_iso):
        return 0

    def wal_checkpoint(self):
        pass

    def purge_hourly_older_than(self, cutoff_iso):
        return 0

    def read_load_samples(self, since_iso=None):
        if since_iso is None:
            return list(self._load_samples)
        return [(ts, w) for ts, w in self._load_samples if ts >= since_iso]

    def read_persons_home_samples(self, since_iso=None):
        return []

    def read_decisions(self, since_iso, until_iso=None):
        rows = [r for r in self.decision_rows if r.get("ts", "") >= since_iso]
        if until_iso:
            rows = [r for r in rows if r.get("ts", "") < until_iso]
        return rows

    def read_feature_rows(self, since_iso=None):
        if since_iso is None:
            return list(self.rows)
        return [r for r in self.rows if r.get("ts", "") >= since_iso]

    def read_hourly_rows(self, since_iso=None):
        """Stub for HGBR/bucketed/profile paths — empty unless a test seeds _hourly_rows."""
        if since_iso is None:
            return list(self._hourly_rows)
        return [r for r in self._hourly_rows if r.get("hour_ts", "") >= since_iso]

    def read_efficiency_samples(self, since_iso=None):
        self.efficiency_calls = getattr(self, "efficiency_calls", 0) + 1
        return []

    def upsert_daily_regret(self, **kwargs):
        day = kwargs["day"]
        self.daily_regret_rows[day] = kwargs

    def read_latest_daily_regret(self):
        if not self.daily_regret_rows:
            return None
        latest_day = max(self.daily_regret_rows.keys())
        return self.daily_regret_rows[latest_day]

    def read_daily_regret_range(self, since_day, until_day=None):
        rows = [v for k, v in self.daily_regret_rows.items() if k >= since_day]
        if until_day is not None:
            rows = [r for r in rows if r["day"] < until_day]
        return sorted(rows, key=lambda r: r["day"])


# ---------------------------------------------------------------------------
# StubHass (+ _StateObj / _States)
# ---------------------------------------------------------------------------


class StubHass:
    """Minimal hass stub with a state registry."""
    def __init__(self):
        self._states = {}

    class _StateObj:
        def __init__(self, state, attributes=None):
            self.state = state
            self.attributes = attributes or {}

    def set_state(self, entity_id, state, attributes=None):
        self._states[entity_id] = self._StateObj(state, attributes)

    class _States:
        def __init__(self, parent):
            self._parent = parent
        def get(self, entity_id):
            return self._parent._states.get(entity_id)

    @property
    def states(self):
        return self._States(self)

    async def async_add_executor_job(self, fn, *args):
        """Run synchronous callables directly (no thread pool in tests)."""
        return fn(*args)


# ---------------------------------------------------------------------------
# make_config — verbatim promotion of test_optimize_parity.py:make_cfg
# ---------------------------------------------------------------------------


def make_config(**overrides) -> Config:
    """Return a Config with clean parity-test defaults.

    eta_charge=1.0 so AC kWh == DC kWh — simplifies arithmetic and allows
    exact (not just approximate) bin arithmetic.  Override any field via kwargs.
    """
    defaults = dict(
        capacity_kwh=10.0,
        soc_floor=20.0,   # 2 kWh floor
        soc_target=80.0,  # 8 kWh target / end-reserve
        max_charge_w=3000.0,  # 3 kWh/h AC
        eta_charge=1.0,   # AC == DC (exact bin arithmetic)
    )
    defaults.update(overrides)
    return Config(**defaults)


# ---------------------------------------------------------------------------
# make_controller / seed_valid_inputs — from test_controller.py's harness
# ---------------------------------------------------------------------------


def make_controller(hass=None, actuator=None, data_overrides=None):
    """Build a Controller with minimal data config.

    hass defaults to a fresh StubHass() when not supplied.
    """
    if hass is None:
        hass = StubHass()
    data = {
        const.CONF_ENT_SOC: "sensor.soc",
        const.CONF_ENT_METER_POWER: "sensor.meter_power",
        const.CONF_ENT_PRICE: "sensor.price",
        const.CONF_ENT_PV_TODAY: [],
        const.CONF_ENT_PV_TOMORROW: [],
        const.CONF_ENT_SUN: "sun.sun",
        const.CONF_ENT_BATTERY_POWER: "sensor.battery_power",
        const.CONF_ENT_PV_POWER: "sensor.pv_power",
        const.CONF_ENT_INVERTER_LOSS: "sensor.inverter_loss",
        const.CONF_ENT_SETPOINT: "number.setpoint",
        const.CONF_ENT_ENGAGE: "switch.engage",
        const.CONF_ENT_WORKMODE: "select.workmode",
        const.CONF_ENT_IRRADIANCE: "sensor.irradiance",
        const.CONF_ENT_TEMP: "weather.home",
    }
    if data_overrides:
        data.update(data_overrides)
    act = actuator or StubActuator()
    rec = StubRecorder()
    ctrl = controller.Controller(
        hass=hass,
        data=data,
        recorder=rec,
        actuator=act,
        store=StubStore(),
    )
    return ctrl, act


def seed_valid_inputs(hass, *, soc="20.0"):
    """Seed HA states so read_plant_inputs succeeds."""
    hass.set_state("sensor.soc", soc)
    hass.set_state("sensor.meter_power", "0.0")
    # Price with a forecast attribute so parse_price_curve gets called
    sunset_iso = (BASE + timedelta(hours=8)).isoformat()
    hass.set_state("sun.sun", "above_horizon", {"next_setting": sunset_iso})
    # Price: provide a simple forecast list so slots are non-empty
    hass.set_state("sensor.price", "0.05", {
        "forecast": [
            {"datetime": (BASE + timedelta(hours=i)).isoformat(), "electricity_price": int(0.05 * const.PRICE_SCALE)}
            for i in range(9)
        ]
    })
    # Phase-2 data entities
    hass.set_state("sensor.pv_power", "1200.0")
    hass.set_state("sensor.battery_power", "-500.0")
    hass.set_state("sensor.irradiance", "350.0")
    hass.set_state("weather.home", "cloudy", {"temperature": 18.5})
