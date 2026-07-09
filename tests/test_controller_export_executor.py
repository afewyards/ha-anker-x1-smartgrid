"""TDD tests for C3: live export executor in controller tick().

Covers:
- surplus + gate clears → engage_export called with POSITIVE setpoint (safety-net)
- setpoint clamped to min(max_export_w, grid_export_limit_w, surplus-limited)
- reserve breach (low surplus) OR hurdle fail → release_to_self
- sub-floor SoC → 0 surplus → never engage, no negative setpoint
- enable_export=false → never engage
- standalone fallback near_peak gate applies when no committed plan
- ExportState persists across ticks (dwell honored)
- mutual exclusion with force-charge (FORCING state → export skipped)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.anker_x1_smartgrid import const
from custom_components.anker_x1_smartgrid import controller as ctrl_mod
from custom_components.anker_x1_smartgrid.controller import Controller
from custom_components.anker_x1_smartgrid.models import (
    Config,
    ControllerState,
    ExportState,
    PlanState,
    PriceSlot,
)

# ---------------------------------------------------------------------------
# Time anchor
# ---------------------------------------------------------------------------

BASE = datetime(2026, 6, 25, 14, 0, tzinfo=timezone.utc)  # 14:00 UTC mid-peak


# ---------------------------------------------------------------------------
# Stubs (mirrors test_controller.py pattern)
# ---------------------------------------------------------------------------


class _StubActuator:
    """Records engage_export / engage_and_charge / release_to_self calls."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.last_setpoint_w: float = 0.0
        self.engaged: bool = False

    async def engage_and_charge(self, setpoint_w: float) -> None:
        self.calls.append(("engage_and_charge", setpoint_w))
        self.last_setpoint_w = setpoint_w
        self.engaged = True

    async def engage_export(self, setpoint_w: float) -> None:
        if setpoint_w <= 0:
            raise ValueError(f"export-only: setpoint must be > 0, got {setpoint_w}")
        self.calls.append(("engage_export", setpoint_w))
        self.last_setpoint_w = setpoint_w
        self.engaged = True

    async def release_to_self(self) -> None:
        self.calls.append(("release_to_self",))
        self.last_setpoint_w = 0.0
        self.engaged = False


class _StubStore:
    """No-op store; records last saved payload."""

    def __init__(self):
        self.saved: dict = {}

    async def async_save(self, data: dict) -> None:
        self.saved = data


class _StubRecorder:
    """Captures appended rows."""

    def __init__(self):
        self.rows: list[dict] = []
        self.decision_rows: list[dict] = []
        self.daily_regret_rows: dict[str, dict] = {}

    def append(self, row: dict) -> None:
        self.rows.append(row)

    def append_decision(self, **kwargs) -> None:
        self.decision_rows.append(kwargs)

    def purge_older_than(self, ts, days) -> None:
        pass

    def purge_decisions_older_than(self, cutoff_iso) -> int:
        return 0

    def rollup_hours(self, now_iso) -> int:
        return 0

    def purge_hourly_older_than(self, cutoff_iso) -> int:
        return 0

    def wal_checkpoint(self) -> None:
        pass

    def read_load_samples(self, since_iso=None):
        return []

    def read_decisions(self, since_iso, until_iso=None):
        return []

    def read_feature_rows(self, since_iso=None):
        return []

    def read_hourly_rows(self):
        return []

    def upsert_daily_regret(self, **kwargs) -> None:
        self.daily_regret_rows[kwargs["day"]] = kwargs

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


class _StubHass:
    """Minimal HA stub with a state registry."""

    def __init__(self):
        self._states: dict = {}

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
        return fn(*args)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _patched_compute_decision(export_request: dict):
    """Factory: returns a stub for compute_decision that injects export_request into _out.

    The stub returns a PASSIVE plan with an empty deficit so the executor path
    reaches C3 without triggering FORCING.  export_request is written verbatim
    into ``_out["export_request"]`` so the executor's committed-plan gate sees
    exactly what the test supplies.
    """
    def _stub(
        plan, inputs, slots, pv_remaining, sunset,
        predictor, cur_temp, cfg,
        tomorrow_total=None, sun_times=None, today_arrays=None, tomorrow_arrays=None,
        today_watts=None, tomorrow_watts=None,
        export_price=None, _out=None, _shadow_dp=False, export_price_matches_import=False,
        estimated_tomorrow=None, past_actuals_by_hour=None, **kwargs,
    ):
        if _out is not None:
            _out["export_request"] = export_request
            _out["dp_selected"] = []
            _out["intervals"] = []
            _out["grid_request"] = {}
        passive = PlanState(ControllerState.PASSIVE, inputs.now, ())
        deadline = inputs.now + timedelta(hours=8)
        # Return (plan, setpoint, deadline, horizon, horizon_mode, intervals_reserve)
        return passive, 0.0, deadline, [], "water_value", []
    return _stub


def _make_export_cfg(**overrides) -> Config:
    """Config with export enabled and simple arithmetic (eta=1.0)."""
    defaults = dict(
        capacity_kwh=10.0,
        soc_floor=10.0,        # 1 kWh floor
        soc_target=97.0,
        max_charge_w=3000.0,
        max_export_w=3000.0,
        grid_export_limit_w=3000.0,
        eta_charge=1.0,
        round_trip_eff=1.0,    # eta_discharge = 1.0 (simplified)
        cycle_cost_eur_per_kwh=0.04,
        export_eps_lo_kwh=0.2,
        export_eps_hi_kwh=0.4,
        export_dwell_min=0,    # no dwell for most tests
        enable_export=True,
    )
    defaults.update(overrides)
    return Config(**defaults)  # type: ignore[arg-type]


def _make_controller(hass, actuator=None, cfg_overrides=None):
    """Build a Controller with minimal data config and export price entity."""
    data = {
        const.CONF_ENT_SOC: "sensor.soc",
        const.CONF_ENT_PHASE: [
            "sensor.phase_l1",
            "sensor.phase_l2",
            "sensor.phase_l3",
        ],
        const.CONF_ENT_PRICE: "sensor.price",
        const.CONF_ENT_PV_TODAY: [],
        const.CONF_ENT_PV_TOMORROW: [],
        const.CONF_ENT_SUN: "sun.sun",
        const.CONF_ENT_BATTERY_POWER: "sensor.battery_power",
        const.CONF_ENT_PV_POWER: "sensor.pv_power",
        const.CONF_ENT_SETPOINT: "number.setpoint",
        const.CONF_ENT_ENGAGE: "switch.engage",
        const.CONF_ENT_WORKMODE: "select.workmode",
        const.CONF_ENT_IRRADIANCE: "sensor.irradiance",
        const.CONF_ENT_TEMP: "weather.home",
        const.CONF_ENT_EXPORT_PRICE: "sensor.export_price",
    }
    act = actuator or _StubActuator()
    store = _StubStore()
    rec = _StubRecorder()
    ctrl = Controller(
        hass=hass,
        data=data,
        recorder=rec,
        actuator=act,
        store=store,
    )
    # Override cfg with export-friendly settings
    cfg = _make_export_cfg(**(cfg_overrides or {}))
    ctrl.cfg = cfg
    return ctrl, act, store


def _seed_passive_inputs(hass, *, soc="80.0", export_price="0.30"):
    """Seed HA states so read_plant_inputs gives PASSIVE (high SoC = no deficit)."""
    hass.set_state("sensor.soc", soc)
    hass.set_state("sensor.phase_l1", "100.0")
    hass.set_state("sensor.phase_l2", "100.0")
    hass.set_state("sensor.phase_l3", "100.0")
    hass.set_state("sensor.pv_power", "2000.0")
    hass.set_state("sensor.battery_power", "0.0")
    hass.set_state("sensor.irradiance", "600.0")
    hass.set_state("weather.home", "sunny", {"temperature": 22.0})
    hass.set_state("sensor.export_price", export_price)
    # Sun: sunset 6h from now so sunset is valid
    sunset_iso = (BASE + timedelta(hours=6)).isoformat()
    hass.set_state("sun.sun", "above_horizon", {"next_setting": sunset_iso})
    # Price forecast: flat expensive → controller stays PASSIVE (no cheap slots)
    hass.set_state("sensor.price", "0.30", {
        "forecast": [
            {
                "datetime": (BASE + timedelta(hours=i)).isoformat(),
                "electricity_price": int(0.30 * const.PRICE_SCALE),
            }
            for i in range(12)
        ]
    })


# ---------------------------------------------------------------------------
# C3-1: surplus + gate clears → engage_export with POSITIVE setpoint (safety-net)
# ---------------------------------------------------------------------------


class TestExportEngagePositiveSetpoint:
    """Surplus above hi-eps AND hurdle clears → engage_export with positive W."""

    @pytest.mark.asyncio
    async def test_engage_export_called_with_positive_setpoint(self, monkeypatch):
        """SAFETY-NET: engage_export must be called with a strictly positive setpoint.

        Scenario: SoC=90% (9 kWh), export_price=0.40 €/kWh, DP commits current
        hour for export at 3000W.  Surplus >> eps_hi → should engage.
        Clock is frozen at BASE so scheduler helpers return deterministic results.

        Under the committed-plan contract, the executor only fires when the DP has
        committed the current hour.  We inject a committed plan via
        _patched_compute_decision so the gate clears.
        """
        monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)

        hass = _StubHass()
        ctrl, act, store = _make_controller(hass)

        hass.set_state("sensor.soc", "90.0")
        hass.set_state("sensor.phase_l1", "100.0")
        hass.set_state("sensor.phase_l2", "100.0")
        hass.set_state("sensor.phase_l3", "100.0")
        hass.set_state("sensor.pv_power", "2000.0")
        hass.set_state("sensor.battery_power", "0.0")
        hass.set_state("sensor.irradiance", "600.0")
        hass.set_state("weather.home", "sunny", {"temperature": 22.0})
        hass.set_state("sensor.export_price", "0.40")
        sunset_iso = (BASE + timedelta(hours=6)).isoformat()
        hass.set_state("sun.sun", "above_horizon", {"next_setting": sunset_iso})
        hass.set_state("sensor.price", "0.40", {
            "forecast": [
                {
                    "datetime": (BASE + timedelta(hours=i)).isoformat(),
                    "electricity_price": int(0.40 * const.PRICE_SCALE),
                }
                for i in range(12)
            ]
        })

        # Inject committed plan: current clock-hour committed at 3000W.
        cur_h = BASE.replace(minute=0, second=0, microsecond=0)
        monkeypatch.setattr(
            ctrl_mod, "compute_decision",
            _patched_compute_decision(export_request={cur_h: 3000.0}),
        )
        # Force export_state to already-engaged to skip dwell
        ctrl.export_state = ExportState(engaged=True, state_since=BASE - timedelta(hours=1))

        await ctrl.tick()

        export_calls = [c for c in act.calls if c[0] == "engage_export"]
        assert len(export_calls) >= 1, (
            f"engage_export must be called when committed plan + surplus present; calls={act.calls}"
        )
        sp = export_calls[-1][1]
        assert sp > 0, (
            f"SAFETY-NET FAILED: engage_export setpoint must be > 0, got {sp!r}. "
            "Negative setpoint would charge, not export — sign convention error."
        )

    @pytest.mark.asyncio
    async def test_setpoint_clamped_to_max_export_w(self, monkeypatch):
        """Setpoint is clamped to cfg.max_export_w even when committed rate and surplus allow more.

        Committed rate = 5000W >> cap = 500W; surplus = 8kWh = 8000W.
        min(5000, 8000, 500, 3000) = 500W → engage_export must be called at ≤ 500W.
        Asserted unconditionally — if engage_export is not called the test fails.
        """
        monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
        hass = _StubHass()
        # Set max_export_w low so we hit the cap
        ctrl, act, _ = _make_controller(hass, cfg_overrides={"max_export_w": 500.0})
        _seed_passive_inputs(hass, soc="90.0", export_price="0.30")
        # Inject committed plan with rate >> cap so max_export_w is the binding constraint.
        cur_h = BASE.replace(minute=0, second=0, microsecond=0)
        monkeypatch.setattr(
            ctrl_mod, "compute_decision",
            _patched_compute_decision(export_request={cur_h: 5000.0}),
        )
        ctrl.export_state = ExportState(engaged=True, state_since=BASE - timedelta(hours=1))

        await ctrl.tick()

        export_calls = [c for c in act.calls if c[0] == "engage_export"]
        assert export_calls, f"engage_export must be called with committed plan + ample surplus; calls={act.calls}"
        sp = export_calls[-1][1]
        assert sp > 0, f"setpoint must be positive, got {sp}"
        assert sp <= 500.0, f"setpoint must be ≤ max_export_w (500), got {sp}"

    @pytest.mark.asyncio
    async def test_setpoint_clamped_to_grid_export_limit_w(self, monkeypatch):
        """Setpoint is clamped to grid_export_limit_w even when committed rate and max_export_w are higher.

        Committed rate = 5000W, max_export_w = 3000W, grid_export_limit_w = 600W; surplus = 8kWh.
        min(5000, 8000, 3000, 600) = 600W → engage_export must be called at ≤ 600W.
        Asserted unconditionally — if engage_export is not called the test fails.
        """
        monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
        hass = _StubHass()
        ctrl, act, _ = _make_controller(
            hass,
            cfg_overrides={"max_export_w": 3000.0, "grid_export_limit_w": 600.0},
        )
        _seed_passive_inputs(hass, soc="90.0", export_price="0.30")
        # Inject committed plan with rate >> both caps so grid_export_limit_w is the binding constraint.
        cur_h = BASE.replace(minute=0, second=0, microsecond=0)
        monkeypatch.setattr(
            ctrl_mod, "compute_decision",
            _patched_compute_decision(export_request={cur_h: 5000.0}),
        )
        ctrl.export_state = ExportState(engaged=True, state_since=BASE - timedelta(hours=1))

        await ctrl.tick()

        export_calls = [c for c in act.calls if c[0] == "engage_export"]
        assert export_calls, f"engage_export must be called with committed plan + ample surplus; calls={act.calls}"
        sp = export_calls[-1][1]
        assert sp > 0, f"setpoint must be positive, got {sp}"
        assert sp <= 600.0, f"setpoint must be ≤ grid_export_limit_w (600), got {sp}"

    @pytest.mark.asyncio
    async def test_export_setpoint_surfaced_state_stays_passive(self, monkeypatch):
        """Sensor-only observability (E2): export_setpoint_w surfaces in
        last_status during an active export tick; state correctly stays
        "passive" — consistent with the recorded smartcharge_state column
        (_record_sample runs before this point and always records the plan
        state, never an export state)."""
        monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
        hass = _StubHass()
        ctrl, act, store = _make_controller(hass)
        _seed_passive_inputs(hass, soc="90.0", export_price="0.40")
        cur_h = BASE.replace(minute=0, second=0, microsecond=0)
        monkeypatch.setattr(ctrl_mod, "compute_decision",
                            _patched_compute_decision(export_request={cur_h: 3000.0}))
        ctrl.export_state = ExportState(engaged=True, state_since=BASE - timedelta(hours=1))
        result = await ctrl.tick()
        assert any(c[0] == "engage_export" for c in act.calls)
        assert result["state"] == "passive"
        assert result["export_setpoint_w"] is not None and result["export_setpoint_w"] > 0

    @pytest.mark.asyncio
    async def test_setpoint_saturates_at_real_grid_export_default(self, monkeypatch):
        """max_export_w raised above the firmware cap so grid_export_limit_w's real
        6000 W default binds; setpoint saturates at exactly 6000."""
        monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
        hass = _StubHass()
        ctrl, act, _ = _make_controller(hass, cfg_overrides={
            "max_export_w": 9000.0, "grid_export_limit_w": 6000.0})
        _seed_passive_inputs(hass, soc="97.0", export_price="0.40")
        cur_h = BASE.replace(minute=0, second=0, microsecond=0)
        monkeypatch.setattr(ctrl_mod, "compute_decision",
                            _patched_compute_decision(export_request={cur_h: 8000.0}))
        ctrl.export_state = ExportState(engaged=True, state_since=BASE - timedelta(hours=1))
        await ctrl.tick()
        sp = [c for c in act.calls if c[0] == "engage_export"][-1][1]
        assert sp == pytest.approx(6000.0)   # exact, not just <=

    @pytest.mark.asyncio
    async def test_executor_reserve_value_bounds_setpoint(self, monkeypatch):
        monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
        monkeypatch.setattr(ctrl_mod.energy, "ride_out_reserve_kwh",
                            lambda now, ivs, cfg, **kw: 3.0)
        hass = _StubHass()
        ctrl, act, _ = _make_controller(hass, cfg_overrides={
            "capacity_kwh": 10.0, "soc_floor": 5.0, "eta_charge": 1.0,
            "round_trip_eff": 1.0, "max_export_w": 9000.0, "grid_export_limit_w": 9000.0})
        _seed_passive_inputs(hass, soc="80.0", export_price="0.40")
        cur_h = BASE.replace(minute=0, second=0, microsecond=0)
        monkeypatch.setattr(ctrl_mod, "compute_decision",
                            _patched_compute_decision(export_request={cur_h: 9000.0}))
        ctrl.export_state = ExportState(engaged=True, state_since=BASE - timedelta(hours=1))
        await ctrl.tick()
        sp = [c for c in act.calls if c[0] == "engage_export"][-1][1]
        # 8 kWh pack − 3 kWh reserve = 5 kWh exportable this hour → 5000 W (eta=1.0).
        assert sp == pytest.approx(5000.0)


# ---------------------------------------------------------------------------
# C3-2: reserve breach OR hurdle fail → release_to_self
# ---------------------------------------------------------------------------


class TestExportReleaseConditions:
    """When export guard conditions fail, release_to_self must be called."""

    @pytest.mark.asyncio
    async def test_low_soc_no_surplus_release_to_self(self, monkeypatch):
        """Sub-floor SoC → surplus=0 → release_to_self, never engage_export."""
        hass = _StubHass()
        ctrl, act, _ = _make_controller(hass, cfg_overrides={"soc_floor": 20.0})
        # SoC=15% is below floor (20%) → export_surplus_kwh returns 0
        _seed_passive_inputs(hass, soc="15.0", export_price="0.30")
        # Start engaged so we can verify the release happens
        ctrl.export_state = ExportState(engaged=True, state_since=BASE - timedelta(hours=1))

        await ctrl.tick()

        # Must never call engage_export (zero surplus)
        assert not any(c[0] == "engage_export" for c in act.calls), (
            f"Sub-floor SoC must NOT engage export; calls={act.calls}"
        )

    @pytest.mark.asyncio
    async def test_hurdle_fails_due_to_zero_export_price(self, monkeypatch):
        """export_price=0 → hurdle never clears → no engage_export."""
        hass = _StubHass()
        ctrl, act, _ = _make_controller(hass)
        _seed_passive_inputs(hass, soc="80.0", export_price="0.0")
        ctrl.export_state = ExportState(engaged=True, state_since=BASE - timedelta(hours=1))

        await ctrl.tick()

        assert not any(c[0] == "engage_export" for c in act.calls), (
            f"Zero export price → hurdle fails → must not engage; calls={act.calls}"
        )

    @pytest.mark.asyncio
    async def test_surplus_below_lo_eps_triggers_disengage(self, monkeypatch):
        """Surplus below export_eps_lo → decide_export_state disengages."""
        hass = _StubHass()
        # Make soc barely above floor: soc=12%, floor=10%, surplus tiny
        # capacity=10kWh: soc_kwh=1.2, floor_kwh=1.0, surplus=0.2 kWh == lo_eps → disengage
        ctrl, act, _ = _make_controller(
            hass,
            cfg_overrides={
                "soc_floor": 10.0,      # 1.0 kWh
                "export_eps_lo_kwh": 0.5,  # raise lo threshold
                "export_eps_hi_kwh": 1.0,
                "export_dwell_min": 0,
            },
        )
        # soc=12% → soc_kwh=1.2, reserve≈1.0 → surplus=0.2 < lo_eps=0.5 → disengage
        _seed_passive_inputs(hass, soc="12.0", export_price="0.30")
        ctrl.export_state = ExportState(engaged=True, state_since=BASE - timedelta(hours=1))

        await ctrl.tick()

        assert not any(c[0] == "engage_export" for c in act.calls), (
            f"Surplus < lo_eps → must not engage; calls={act.calls}"
        )


# ---------------------------------------------------------------------------
# C3-3: enable_export=false → never engage
# ---------------------------------------------------------------------------


class TestExportDisabled:
    """When cfg.enable_export is False the executor must never fire."""

    @pytest.mark.asyncio
    async def test_no_engage_when_enable_export_false(self, monkeypatch):
        """enable_export=False → no engage_export regardless of surplus and hurdle."""
        hass = _StubHass()
        ctrl, act, _ = _make_controller(hass, cfg_overrides={"enable_export": False})
        _seed_passive_inputs(hass, soc="90.0", export_price="0.50")
        ctrl.export_state = ExportState(engaged=True, state_since=BASE - timedelta(hours=1))

        await ctrl.tick()

        assert not any(c[0] == "engage_export" for c in act.calls), (
            f"enable_export=False must prevent all engage_export calls; got {act.calls}"
        )


# ---------------------------------------------------------------------------
# C3-4: ExportState persists across ticks via _persist / restore
# ---------------------------------------------------------------------------


class TestExportStatePersistence:
    """ExportState must survive a save/restore cycle."""

    def test_export_state_saved_and_restored(self):
        """to_dict / from_dict round-trip preserves engaged + state_since."""
        state = ExportState(engaged=True, state_since=BASE)
        d = state.to_dict()
        restored = ExportState.from_dict(d)
        assert restored.engaged is True
        assert restored.state_since == BASE

    @pytest.mark.asyncio
    async def test_persist_includes_export_state(self, monkeypatch):
        """_persist writes export_state key to the store."""
        hass = _StubHass()
        ctrl, act, store = _make_controller(hass)
        _seed_passive_inputs(hass, soc="80.0", export_price="0.30")

        ctrl.export_state = ExportState(engaged=True, state_since=BASE - timedelta(hours=1))
        await ctrl.tick()

        saved = store.saved
        assert "export_state" in saved, (
            f"_persist must save export_state; saved keys={list(saved.keys())}"
        )
        assert saved["export_state"]["engaged"] in (True, False)

    @pytest.mark.asyncio
    async def test_restore_loads_export_state(self, monkeypatch):
        """restore() loads export_state from the saved dict."""
        hass = _StubHass()
        ctrl, act, _ = _make_controller(hass)

        saved_state = {
            "plan": PlanState.initial(BASE).to_dict(),
            "enabled": True,
            "export_state": ExportState(engaged=True, state_since=BASE).to_dict(),
        }
        ctrl.restore(saved_state)
        assert ctrl.export_state.engaged is True
        assert ctrl.export_state.state_since == BASE

    @pytest.mark.asyncio
    async def test_restore_without_export_state_key_is_graceful(self, monkeypatch):
        """restore() with no export_state key uses initial (disengaged) state."""
        hass = _StubHass()
        ctrl, act, _ = _make_controller(hass)

        saved_state = {
            "plan": PlanState.initial(BASE).to_dict(),
            "enabled": True,
            # no export_state key
        }
        ctrl.restore(saved_state)
        assert ctrl.export_state.engaged is False


# ---------------------------------------------------------------------------
# C3-5: dwell — no flap within export_dwell_min
# ---------------------------------------------------------------------------


class TestExportDwellHonored:
    """Dwell prevents engage/disengage within export_dwell_min minutes."""

    @pytest.mark.asyncio
    async def test_dwell_blocks_transition_within_window(self, monkeypatch):
        """State transition is blocked when dwell has NOT elapsed."""
        # Freeze time at BASE so dwell arithmetic is deterministic:
        # state_since = BASE-5min, dwell=30min → 5min < 30min → blocked.
        monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
        hass = _StubHass()
        ctrl, act, _ = _make_controller(
            hass,
            cfg_overrides={
                "export_dwell_min": 30,  # 30-minute dwell
                "export_eps_hi_kwh": 0.4,
                "export_eps_lo_kwh": 0.2,
            },
        )
        _seed_passive_inputs(hass, soc="80.0", export_price="0.30")
        # Disengaged but started only 5 minutes ago → dwell not elapsed
        ctrl.export_state = ExportState(
            engaged=False, state_since=BASE - timedelta(minutes=5)
        )

        await ctrl.tick()

        # Surplus is large but dwell hasn't elapsed → no engage
        assert not any(c[0] == "engage_export" for c in act.calls), (
            f"Dwell must block engage within 30-minute window; calls={act.calls}"
        )


# ---------------------------------------------------------------------------
# C3-6: mutual exclusion with force-charge
# ---------------------------------------------------------------------------


class TestMutualExclusionWithForceCharge:
    """When the controller is in FORCING state, export must be skipped entirely."""

    @pytest.mark.asyncio
    async def test_forcing_state_no_export(self, monkeypatch):
        """FORCING → engage_and_charge, NOT engage_export.

        Controller decides FORCING when deficit > 0 and a cheap slot is now.
        The export executor must be skipped in this case.
        Time is frozen at BASE so the seeded slots align with inputs.now.
        Uses legacy deadline planner for a predictable FORCING trigger.
        """
        # Freeze time at BASE so coordinator's dt_util.utcnow() returns BASE,
        # making inputs.now == BASE and aligning slot selection with the seeded prices.
        from custom_components.anker_x1_smartgrid import controller as ctrl_module
        monkeypatch.setattr(ctrl_module.dt_util, "utcnow", lambda: BASE)

        hass = _StubHass()
        ctrl, act, _ = _make_controller(
            hass,
            cfg_overrides={
                "soc_target": 97.0,
                "soc_floor": 10.0,
                "min_dwell_min": 0,
            },
        )
        # Low SoC + cheap price → FORCING
        hass.set_state("sensor.soc", "15.0")
        hass.set_state("sensor.phase_l1", "0.0")
        hass.set_state("sensor.phase_l2", "0.0")
        hass.set_state("sensor.phase_l3", "0.0")
        hass.set_state("sensor.pv_power", "0.0")
        hass.set_state("sensor.battery_power", "0.0")
        hass.set_state("sensor.irradiance", "0.0")
        hass.set_state("weather.home", "clear", {"temperature": 15.0})
        hass.set_state("sensor.export_price", "0.30")
        sunset_iso = (BASE + timedelta(hours=8)).isoformat()
        hass.set_state("sun.sun", "above_horizon", {"next_setting": sunset_iso})
        # Price: cheap now (0.05), expensive later (0.30) → BASE hour selected
        hass.set_state("sensor.price", "0.05", {
            "forecast": [
                {
                    "datetime": (BASE + timedelta(hours=i)).isoformat(),
                    "electricity_price": int(
                        (0.05 if i < 3 else 0.30) * const.PRICE_SCALE
                    ),
                }
                for i in range(12)
            ]
        })
        ctrl.export_state = ExportState(engaged=True, state_since=BASE - timedelta(hours=1))

        await ctrl.tick()

        # engage_and_charge must be called (FORCING), engage_export must NOT be called
        assert any(c[0] == "engage_and_charge" for c in act.calls), (
            f"FORCING state must call engage_and_charge; calls={act.calls}"
        )
        assert not any(c[0] == "engage_export" for c in act.calls), (
            f"FORCING state must NOT call engage_export; calls={act.calls}"
        )


# ---------------------------------------------------------------------------
# C3-7: recorder signals populated on export tick
# ---------------------------------------------------------------------------


class TestRecorderExportSignals:
    """export_setpoint_w, export_kwh, reserve_kwh, surplus_kwh must be non-None on export ticks."""

    @pytest.mark.asyncio
    async def test_export_signals_non_none_when_engaged(self, monkeypatch):
        """When export is engaged, recorder row must have non-None export signals.

        Committed plan injected so export actually fires — asserted unconditionally.
        """
        monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
        hass = _StubHass()
        rec = _StubRecorder()
        ctrl, act, _ = _make_controller(hass)
        ctrl._recorder = rec
        _seed_passive_inputs(hass, soc="80.0", export_price="0.30")
        # Inject committed plan so the executor actually engages.
        cur_h = BASE.replace(minute=0, second=0, microsecond=0)
        monkeypatch.setattr(
            ctrl_mod, "compute_decision",
            _patched_compute_decision(export_request={cur_h: 3000.0}),
        )
        ctrl.export_state = ExportState(engaged=True, state_since=BASE - timedelta(hours=1))

        await ctrl.tick()

        assert rec.rows, "Recorder must have at least one row"
        row = rec.rows[-1]

        # Export must have fired — assert unconditionally.
        export_calls = [c for c in act.calls if c[0] == "engage_export"]
        assert export_calls, f"engage_export must fire with committed plan; calls={act.calls}"
        assert row.get("export_setpoint_w") is not None, (
            "export_setpoint_w must be set in recorder row on export tick"
        )
        assert row.get("reserve_kwh") is not None, (
            "reserve_kwh must be set in recorder row on export tick"
        )
        assert row.get("surplus_kwh") is not None, (
            "surplus_kwh must be set in recorder row on export tick"
        )

    @pytest.mark.asyncio
    async def test_export_signals_none_when_not_engaged(self, monkeypatch):
        """When export is NOT engaged, recorder row has None for export signals."""
        hass = _StubHass()
        rec = _StubRecorder()
        ctrl, act, _ = _make_controller(hass, cfg_overrides={"enable_export": False})
        ctrl._recorder = rec
        _seed_passive_inputs(hass, soc="50.0", export_price="0.0")

        await ctrl.tick()

        assert rec.rows, "Recorder must have at least one row"
        row = rec.rows[-1]
        assert row.get("export_setpoint_w") is None, (
            f"export_setpoint_w must be None when not engaged; got {row.get('export_setpoint_w')}"
        )


# ---------------------------------------------------------------------------
# C3-8: standalone fallback near_peak gate
# ---------------------------------------------------------------------------


class TestStandaloneFallbackNearPeak:
    """When no committed plan exists, the executor must not export.

    Under the committed-plan contract the DP decides which hours to export;
    the executor follows.  With no committed plan (empty export_request), the
    hurdle is False regardless of price, so export never fires.
    """

    @pytest.mark.asyncio
    async def test_near_peak_blocks_export_when_price_below_threshold(self, monkeypatch):
        """No committed export plan → no engage even with ample surplus and price.

        Historically this tested the near_peak standalone gate; under the new
        committed-plan contract the absence of a plan is the gate (strictly safer).
        We inject an EMPTY export_request (no committed hours) via the patched
        compute_decision stub, then verify the executor never fires.

        Code evidence: lines 1527-1528 of controller.py —
            _committed_export = _dp_out.get("export_request") or {}
            _hurdle = _cur_h in _committed_export
        With export_request={}, _hurdle=False → decide_export_state returns
        prev (disengaged) → no engage_export call.
        """
        # Freeze time at BASE so slot datetimes align with inputs.now.
        monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
        hass = _StubHass()
        ctrl, act, _ = _make_controller(
            hass,
            cfg_overrides={
                "export_dwell_min": 0,
                "export_eps_hi_kwh": 0.1,   # low threshold so surplus clears easily
                "export_eps_lo_kwh": 0.05,
                "enable_export": True,
            },
        )
        _seed_passive_inputs(hass, soc="80.0", export_price="0.20")
        # Set the price forecast so max is 0.30 (window_max > current)
        hass.set_state("sensor.price", "0.20", {
            "forecast": [
                {
                    "datetime": (BASE + timedelta(hours=i)).isoformat(),
                    "electricity_price": int(
                        (0.20 if i < 4 else 0.30) * const.PRICE_SCALE
                    ),
                }
                for i in range(12)
            ]
        })
        # Inject EMPTY export plan (no committed hours) — the retired near_peak
        # gate is replaced by: no committed rate ⇒ no export.
        monkeypatch.setattr(
            ctrl_mod, "compute_decision",
            _patched_compute_decision(export_request={}),
        )
        # Dwell=0 and state_since=1h ago → dwell elapsed; only the committed-plan
        # gate prevents export.
        ctrl.export_state = ExportState(engaged=False, state_since=BASE - timedelta(hours=1))

        await ctrl.tick()

        # Under the committed-plan contract: no committed rate → no export.
        # The near_peak threshold test is now a special case of "no committed plan ⇒ no
        # export" (the DP decides which hours to export; the executor only follows it).
        assert not any(c[0] == "engage_export" for c in act.calls), (
            f"No committed plan → must not engage; calls={act.calls}"
        )


# ---------------------------------------------------------------------------
# C3-9: committed export plan — executor follows the DP decision + live clamp
# ---------------------------------------------------------------------------


class TestCommittedPlanExecutor:
    """Executor reads the committed export VALUE from the DP plan, not near_peak.

    New contract (Task 7):
    - committed rate present → GATE only; engage decisively at min(max_export_w, grid_export_limit_w), stopping at the live reserve
    - no committed rate → never export (strictly safer than standalone gate)
    """

    @pytest.mark.asyncio
    async def test_committed_is_gate_not_rate_cap(self, monkeypatch):
        """Committed plan present (gate ON) + ample surplus → engage DECISIVELY at
        the export cap, NOT throttled to the committed magnitude.

        committed = 1000 W but caps = 3000 W (defaults). New contract: committed is
        an on/off GATE; the decisive drain runs at min(max_export_w, grid_export_limit_w).
        """
        monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
        hass = _StubHass()
        ctrl, act, _ = _make_controller(hass)  # max/grid default 3000 in _make_export_cfg
        _seed_passive_inputs(hass, soc="90.0", export_price="0.40")
        cur_h = BASE.replace(minute=0, second=0, microsecond=0)
        monkeypatch.setattr(
            ctrl_mod, "compute_decision",
            _patched_compute_decision(export_request={cur_h: 1000.0}),
        )
        ctrl.export_state = ExportState(engaged=True, state_since=BASE - timedelta(hours=1))
        await ctrl.tick()
        export_calls = [c for c in act.calls if c[0] == "engage_export"]
        assert export_calls, f"expected engage_export; calls={act.calls}"
        assert export_calls[-1][1] >= 3000.0 - 1e-6, (
            f"committed must be a GATE not a rate cap; expected decisive >=3000, got {export_calls[-1][1]}"
        )

    @pytest.mark.asyncio
    async def test_no_committed_rate_never_exports(self, monkeypatch):
        """Empty export plan → no export, even at high surplus + high price (safer fallback)."""
        monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
        hass = _StubHass()
        ctrl, act, _ = _make_controller(hass)
        _seed_passive_inputs(hass, soc="95.0", export_price="0.60")
        monkeypatch.setattr(ctrl_mod, "compute_decision", _patched_compute_decision(export_request={}))
        ctrl.export_state = ExportState(engaged=True, state_since=BASE - timedelta(hours=1))
        await ctrl.tick()
        assert not [c for c in act.calls if c[0] == "engage_export"], (
            f"no committed rate ⇒ no export; calls={act.calls}"
        )


# ---------------------------------------------------------------------------
# C1d: executor reserve anchored to solar pickup (not cheap night slot)
# ---------------------------------------------------------------------------


class TestExecutorReserveAnchoredToSolarPickup:
    """Live export executor uses ride_out_reserve_kwh (trough-anchored), not the old reserve_kwh.

    The executor's ride-out reserve is now sized by energy.ride_out_reserve_kwh, which
    walks forward to the deepest signed-trajectory trough (price-independent, debit-only,
    floor-stacked), matching the DP's planned export floor.  We spy on
    energy.ride_out_reserve_kwh to prove the executor invokes the new function.
    """

    @pytest.mark.asyncio
    async def test_executor_reserve_anchored_to_solar_pickup(self, monkeypatch):
        """Executor calls energy.ride_out_reserve_kwh (trough-anchored) for the reserve."""
        # Freeze time so slot datetimes align with inputs.now and dwell is deterministic.
        monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)

        calls: dict = {}
        _real_ride_out = ctrl_mod.energy.ride_out_reserve_kwh

        def _spy_ride_out(now, intervals, cfg, **kw):
            calls["used_ride_out_reserve"] = True
            calls["is_cheap_kw"] = kw.get("is_cheap")
            calls["now_arg"] = now
            return _real_ride_out(now, intervals, cfg, **kw)

        # Spy on the module-level energy attribute used by the executor.
        monkeypatch.setattr(ctrl_mod.energy, "ride_out_reserve_kwh", _spy_ride_out)

        hass = _StubHass()
        ctrl, act, _ = _make_controller(hass)
        _seed_passive_inputs(hass, soc="80.0", export_price="0.30")

        # Inject a committed export plan for the current clock-hour so the executor
        # path reaches the reserve computation (enable_export=True + _export_price>0
        # + committed plan present).
        cur_h = BASE.replace(minute=0, second=0, microsecond=0)
        monkeypatch.setattr(
            ctrl_mod, "compute_decision",
            _patched_compute_decision(export_request={cur_h: 3000.0}),
        )
        # Already engaged so dwell does not gate the tick.
        ctrl.export_state = ExportState(engaged=True, state_since=BASE - timedelta(hours=1))

        await ctrl.tick()

        # The executor must have invoked energy.ride_out_reserve_kwh (trough-anchored,
        # floor-stacked, eta_discharge) — not the old reserve_kwh / find_next_solar_pickup path.
        assert calls.get("used_ride_out_reserve") is True, (
            "Executor must call energy.ride_out_reserve_kwh for the ride-out "
            "reserve; spy was never invoked — old reserve_kwh path may still be active."
        )
        assert calls["is_cheap_kw"] is not None, "executor must thread the is_cheap map"
        assert calls["now_arg"] == BASE.replace(minute=0, second=0, microsecond=0), \
            "executor must hour-align now before ride_out_reserve_kwh"

    @pytest.mark.asyncio
    async def test_executor_hour_align_and_is_cheap_gated_to_trough_anchor(self, monkeypatch):
        """Hour-alignment + is_cheap threading apply ONLY under reserve_anchor=trough.

        Pre-rev-2 the executor passed the raw (possibly mid-hour) `now` straight
        through, so the walk's ``if iv.start < now: continue`` skipped the current
        hour's interval on every non-on-the-hour tick. Unconditionally hour-aligning
        `now` would change that skip behavior for the legacy anchor too, breaking its
        byte-identical rollback guarantee. So a mid-hour tick must show: trough anchor
        truncates `now` to the hour and threads is_cheap; legacy anchor keeps the raw
        mid-hour `now` and passes is_cheap=None (unchanged from before rev-2).
        """
        mid_hour_now = BASE + timedelta(minutes=37)
        cur_h = mid_hour_now.replace(minute=0, second=0, microsecond=0)
        # Sanity: BASE is on-the-hour (file-level time anchor), so +37m stays inside
        # BASE's hour — the seeded fixtures (sunset/price forecast keyed off BASE)
        # remain valid for this slightly-offset clock.
        assert cur_h == BASE
        monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: mid_hour_now)
        _real_ride_out = ctrl_mod.energy.ride_out_reserve_kwh

        cases = (
            # (reserve_anchor, expected now_arg passed to ride_out_reserve_kwh, is_cheap must be None)
            ("trough", cur_h, False),
            ("legacy", mid_hour_now, True),
        )
        for anchor, expected_now, expect_is_cheap_none in cases:
            calls: dict = {}

            def _spy_ride_out(now, intervals, cfg, **kw):
                calls["now_arg"] = now
                calls["is_cheap_kw"] = kw.get("is_cheap")
                return _real_ride_out(now, intervals, cfg, **kw)

            monkeypatch.setattr(ctrl_mod.energy, "ride_out_reserve_kwh", _spy_ride_out)

            hass = _StubHass()
            ctrl, act, _ = _make_controller(hass, cfg_overrides={"reserve_anchor": anchor})
            _seed_passive_inputs(hass, soc="80.0", export_price="0.30")
            monkeypatch.setattr(
                ctrl_mod, "compute_decision",
                _patched_compute_decision(export_request={cur_h: 3000.0}),
            )
            # Already engaged so dwell does not gate the tick.
            ctrl.export_state = ExportState(engaged=True, state_since=mid_hour_now - timedelta(hours=1))

            await ctrl.tick()

            assert calls["now_arg"] == expected_now, (
                f"{anchor} anchor: now_arg was {calls['now_arg']!r}, expected {expected_now!r}"
            )
            if expect_is_cheap_none:
                assert calls["is_cheap_kw"] is None, f"{anchor} anchor must NOT thread is_cheap"
            else:
                assert calls["is_cheap_kw"] is not None, f"{anchor} anchor must thread is_cheap"

    def test_executor_reserve_equals_plan_reserve(self):
        """Executor helper (energy.ride_out_reserve_kwh) matches the plan helper
        (controller._build_reserve_by_hour) at the current hour, given the SAME
        is_cheap map — proves the live floor and the planned floor agree.

        NB: this equivalence holds for the common case exercised here; it does not
        cover the case where the current hour itself needs synthetic night-extension
        (that path in _build_reserve_by_hour bypasses this direct comparison)."""
        from datetime import datetime, timedelta, timezone
        from custom_components.anker_x1_smartgrid import controller as c, energy
        from custom_components.anker_x1_smartgrid.models import Config, ForecastInterval, PriceSlot
        cur = datetime(2026, 7, 1, 16, 0, tzinfo=timezone.utc)
        cfg = Config(capacity_kwh=10.0, soc_floor=10.0, eta_charge=1.0,
                     round_trip_eff=1.0, reserve_cheap_band=0.20, reserve_anchor="trough")
        prices = [0.30, 0.30, 0.30, 0.13, 0.13, 0.13, 0.13, 0.13]
        slots = [PriceSlot(cur + timedelta(hours=i), p) for i, p in enumerate(prices)]
        ivs = ([ForecastInterval(cur + timedelta(hours=i), 0.0, 500.0, 1.0) for i in range(3)]
               + [ForecastInterval(cur + timedelta(hours=3), 3000.0, 200.0, 1.0)])
        plan_r = c._build_reserve_by_hour(cur, slots, ivs, cfg,
                                          is_cheap=c._build_is_cheap_by_hour(slots, cfg))[cur]
        exec_r = energy.ride_out_reserve_kwh(cur, ivs, cfg,
                                             is_cheap=c._build_is_cheap_by_hour(slots, cfg))
        assert exec_r == pytest.approx(plan_r, abs=1e-9)


# ---------------------------------------------------------------------------
# T3: net/gross split + live house-load add-back
# ---------------------------------------------------------------------------


class TestExportHouseLoadCompensation:
    """Gross export setpoint = net_target + live house load (firmware nets it out)."""

    @pytest.mark.asyncio
    async def test_setpoint_adds_house_load(self, monkeypatch):
        monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
        hass = _StubHass()
        # max_export_w=3000 so net_target binds at 3000; house load 300 added on top.
        ctrl, act, _ = _make_controller(hass, cfg_overrides={"max_export_w": 3000.0})
        _seed_passive_inputs(hass, soc="90.0", export_price="0.40")
        hass.set_state(const.DEFAULT_ENT_HOUSE_LOAD, "300.0")
        cur_h = BASE.replace(minute=0, second=0, microsecond=0)
        monkeypatch.setattr(
            ctrl_mod, "compute_decision",
            _patched_compute_decision(export_request={cur_h: 3000.0}),
        )
        ctrl.export_state = ExportState(engaged=True, state_since=BASE - timedelta(hours=1))

        await ctrl.tick()

        export_calls = [c for c in act.calls if c[0] == "engage_export"]
        assert export_calls, f"expected engage_export; calls={act.calls}"
        sp = export_calls[-1][1]
        assert sp == 3300.0, f"gross must be net_target(3000) + load(300); got {sp}"

    @pytest.mark.asyncio
    async def test_factor_zero_is_legacy(self, monkeypatch):
        monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
        hass = _StubHass()
        ctrl, act, _ = _make_controller(
            hass, cfg_overrides={"max_export_w": 3000.0, "export_load_comp_factor": 0.0}
        )
        _seed_passive_inputs(hass, soc="90.0", export_price="0.40")
        hass.set_state(const.DEFAULT_ENT_HOUSE_LOAD, "300.0")
        cur_h = BASE.replace(minute=0, second=0, microsecond=0)
        monkeypatch.setattr(
            ctrl_mod, "compute_decision",
            _patched_compute_decision(export_request={cur_h: 3000.0}),
        )
        ctrl.export_state = ExportState(engaged=True, state_since=BASE - timedelta(hours=1))

        await ctrl.tick()

        sp = [c for c in act.calls if c[0] == "engage_export"][-1][1]
        assert sp == 3000.0, f"factor=0 ⇒ net_target only; got {sp}"

    @pytest.mark.asyncio
    async def test_missing_house_load_treated_as_zero(self, monkeypatch):
        monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
        hass = _StubHass()
        ctrl, act, _ = _make_controller(hass, cfg_overrides={"max_export_w": 3000.0})
        _seed_passive_inputs(hass, soc="90.0", export_price="0.40")
        # house-load entity NOT seeded → read_float → None → 0.0
        cur_h = BASE.replace(minute=0, second=0, microsecond=0)
        monkeypatch.setattr(
            ctrl_mod, "compute_decision",
            _patched_compute_decision(export_request={cur_h: 3000.0}),
        )
        ctrl.export_state = ExportState(engaged=True, state_since=BASE - timedelta(hours=1))

        await ctrl.tick()

        sp = [c for c in act.calls if c[0] == "engage_export"][-1][1]
        assert sp == 3000.0, f"missing load ⇒ net_target only; got {sp}"

    @pytest.mark.asyncio
    async def test_gross_survives_guard_above_max_export_w(self, monkeypatch):
        """net_target=2000, load=1500 → gross 3500 must NOT be re-clamped to max_export_w=2000."""
        monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
        hass = _StubHass()
        ctrl, act, _ = _make_controller(hass, cfg_overrides={"max_export_w": 2000.0})
        _seed_passive_inputs(hass, soc="90.0", export_price="0.40")
        hass.set_state(const.DEFAULT_ENT_HOUSE_LOAD, "1500.0")
        cur_h = BASE.replace(minute=0, second=0, microsecond=0)
        monkeypatch.setattr(
            ctrl_mod, "compute_decision",
            _patched_compute_decision(export_request={cur_h: 5000.0}),
        )
        ctrl.export_state = ExportState(engaged=True, state_since=BASE - timedelta(hours=1))

        await ctrl.tick()

        sp = [c for c in act.calls if c[0] == "engage_export"][-1][1]
        assert sp == 3500.0, f"gross net(2000)+load(1500) must survive; got {sp}"


class TestExportPnlMeteredNet:
    """PnL/_export_kwh use metered net (gross − load), and recorded load_w is the read value."""

    @pytest.mark.asyncio
    async def test_recorded_export_kwh_uses_metered_net(self, monkeypatch):
        monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
        hass = _StubHass()
        ctrl, act, store = _make_controller(hass, cfg_overrides={"max_export_w": 3000.0})
        _seed_passive_inputs(hass, soc="90.0", export_price="0.40")
        hass.set_state(const.DEFAULT_ENT_HOUSE_LOAD, "300.0")
        cur_h = BASE.replace(minute=0, second=0, microsecond=0)
        monkeypatch.setattr(
            ctrl_mod, "compute_decision",
            _patched_compute_decision(export_request={cur_h: 3000.0}),
        )
        ctrl.export_state = ExportState(engaged=True, state_since=BASE - timedelta(hours=1))

        await ctrl.tick()

        row = ctrl._recorder.rows[-1]
        # gross setpoint 3300 recorded; metered net = 3300 − 300 = 3000 W.
        assert row["export_setpoint_w"] == 3300.0
        expected_kwh = 3000.0 / 1000.0 * (const.TICK_SECONDS / 3600.0)
        assert abs(row["export_kwh"] - expected_kwh) < 1e-9, row["export_kwh"]
        assert row["load_w"] == 300.0

    @pytest.mark.asyncio
    async def test_recorded_load_w_none_when_unavailable(self, monkeypatch):
        monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
        hass = _StubHass()
        ctrl, act, _ = _make_controller(hass)
        _seed_passive_inputs(hass, soc="90.0", export_price="0.40")
        # house-load entity NOT seeded → None preserved in record (not 0.0)
        monkeypatch.setattr(
            ctrl_mod, "compute_decision",
            _patched_compute_decision(export_request={}),
        )

        await ctrl.tick()

        row = ctrl._recorder.rows[-1]
        assert row["load_w"] is None, f"unavailable load must record NULL; got {row['load_w']}"

    @pytest.mark.asyncio
    async def test_metered_net_floored_at_zero_when_load_exceeds_gross(self, monkeypatch):
        """Metered-net floor: max(0, gross − load) when load > gross setpoint.

        Scenario: factor=0.0 → gross == net_target (3000 W).  House load is
        5000 W (larger than gross).  Metered net = max(0, 3000 − 5000) = 0 W,
        so export_kwh must be 0.0 even though the setpoint was committed.
        """
        monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
        hass = _StubHass()
        ctrl, act, _ = _make_controller(
            hass,
            cfg_overrides={"export_load_comp_factor": 0.0, "max_export_w": 3000.0},
        )
        _seed_passive_inputs(hass, soc="90.0", export_price="0.40")
        hass.set_state(const.DEFAULT_ENT_HOUSE_LOAD, "5000.0")
        cur_h = BASE.replace(minute=0, second=0, microsecond=0)
        monkeypatch.setattr(
            ctrl_mod, "compute_decision",
            _patched_compute_decision(export_request={cur_h: 3000.0}),
        )
        ctrl.export_state = ExportState(engaged=True, state_since=BASE - timedelta(hours=1))

        await ctrl.tick()

        row = ctrl._recorder.rows[-1]
        # factor=0 → gross == net target; no load added to setpoint
        assert row["export_setpoint_w"] == 3000.0, row["export_setpoint_w"]
        # metered net = max(0, 3000 − 5000) = 0 → no export energy recorded
        assert row["export_kwh"] == 0.0, row["export_kwh"]


# ---------------------------------------------------------------------------
# M3: export_state reset on all release/fall-through paths
# ---------------------------------------------------------------------------


class _RaisingExportActuator(_StubActuator):
    async def engage_export(self, setpoint_w):
        if setpoint_w <= 0:
            raise ValueError("export-only: setpoint must be > 0")
        self.calls.append(("engage_export_attempt", setpoint_w))
        raise RuntimeError("set_value failed mid-engage")


@pytest.mark.asyncio
async def test_engage_export_failure_resets_export_state_and_releases(monkeypatch):
    monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
    hass = _StubHass()
    act = _RaisingExportActuator()
    ctrl, act, _ = _make_controller(hass, actuator=act)
    ctrl.enabled = True
    hass.set_state("sensor.soc", "90.0")
    hass.set_state("sensor.phase_l1", "100.0")
    hass.set_state("sensor.phase_l2", "100.0")
    hass.set_state("sensor.phase_l3", "100.0")
    hass.set_state("sensor.pv_power", "2000.0")
    hass.set_state("sensor.battery_power", "0.0")
    hass.set_state("sensor.irradiance", "600.0")
    hass.set_state("weather.home", "sunny", {"temperature": 22.0})
    hass.set_state("sensor.export_price", "0.40")
    hass.set_state("sun.sun", "above_horizon",
                   {"next_setting": (BASE + timedelta(hours=6)).isoformat()})
    hass.set_state("sensor.price", "0.40", {
        "forecast": [
            {"datetime": (BASE + timedelta(hours=i)).isoformat(),
             "electricity_price": int(0.40 * const.PRICE_SCALE)}
            for i in range(12)
        ]
    })
    cur_h = BASE.replace(minute=0, second=0, microsecond=0)
    monkeypatch.setattr(ctrl_mod, "compute_decision",
                        _patched_compute_decision(export_request={cur_h: 3000.0}))
    ctrl.export_state = ExportState(engaged=True, state_since=BASE - timedelta(hours=1))

    await ctrl.tick()

    # engage_export raised → must NOT report engaged, and a release was attempted.
    assert ctrl.export_state.engaged is False
    assert any(c[0] == "release_to_self" for c in act.calls)


@pytest.mark.asyncio
async def test_failsafe_path_resets_export_state(monkeypatch):
    monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
    hass = _StubHass()
    ctrl, act, _ = _make_controller(hass)
    ctrl.enabled = True
    ctrl.export_state = ExportState(engaged=True, state_since=BASE - timedelta(hours=1))
    # No states seeded → read_plant_inputs returns None → failsafe branch.
    await ctrl.tick()
    assert ctrl.export_state.engaged is False
    assert any(c[0] == "release_to_self" for c in act.calls)


@pytest.mark.asyncio
async def test_disabled_path_resets_export_state(monkeypatch):
    monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
    hass = _StubHass()
    ctrl, act, _ = _make_controller(hass)
    ctrl.enabled = False
    act.engaged = True  # so the disabled-path release fires
    ctrl.export_state = ExportState(engaged=True, state_since=BASE - timedelta(hours=1))
    await ctrl.tick()
    assert ctrl.export_state.engaged is False


@pytest.mark.asyncio
async def test_forcing_engage_failure_publishes_honest_state(monkeypatch):
    """A FORCING engage failure must publish setpoint 0 + state 'passive' for
    THIS tick (not phantom 'forcing'+max_charge_w), WITHOUT releasing hardware
    and WITHOUT resetting self.plan (intent retries next tick)."""
    from custom_components.anker_x1_smartgrid.models import PlanState
    from custom_components.anker_x1_smartgrid.controller import ControllerState

    class _RaisingChargeActuator(_StubActuator):
        async def engage_and_charge(self, setpoint_w):
            self.calls.append(("engage_and_charge", setpoint_w))
            raise RuntimeError("modbus busy")

    monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
    hass = _StubHass()
    act = _RaisingChargeActuator()
    ctrl, act, _ = _make_controller(hass, actuator=act)
    _seed_passive_inputs(hass, soc="30.0", export_price="0.10")
    deadline = BASE + timedelta(hours=2)
    monkeypatch.setattr(ctrl_mod, "compute_decision", lambda *a, **k: (
        PlanState(ControllerState.FORCING, BASE, ()), 0.0, deadline, [], "single-day", []))

    result = await ctrl.tick()
    assert result["state"] == "passive"                       # not "forcing"
    assert result["setpoint_w"] == 0.0                        # not max_charge_w
    assert not any(c[0] == "release_to_self" for c in act.calls)   # no hardware release
    assert ctrl.plan.state is ControllerState.FORCING         # intent preserved for retry
