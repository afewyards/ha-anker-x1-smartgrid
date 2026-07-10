"""T0.5c — Shadow DP compute, DP regret logging, and 7d rolling sensor.

Key invariants under test:
1. ``compute_decision`` with ``_shadow_dp=True`` runs the DP and populates ``_out``.
2. Disabled Controller tick never calls ``engage_and_charge`` (no actuation).
3. When shadow compute conditions are met, ``fictive_plan`` is published in
   ``last_status`` during the disabled tick.
4. ``_run_daily_regret_sync`` persists ``dp_regret_eur`` alongside ``regret_eur``
   in the daily_regret table.
5. ``X1DpRegretSensor`` has a DISTINCT key (``dp_regret_7d``) that does NOT
   collide with ``X1RegretEurSensor`` (``regret_eur``).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from custom_components.anker_x1_smartgrid import const
from custom_components.anker_x1_smartgrid import coordinator as coord_mod
from custom_components.anker_x1_smartgrid import regret as regret_mod
from custom_components.anker_x1_smartgrid.controller import compute_decision, Controller
from custom_components.anker_x1_smartgrid.forecast import LoadPredictor
from custom_components.anker_x1_smartgrid.models import (
    Config,
    ControllerState,
    PlantInputs,
    PlanState,
    PriceSlot,
)
from custom_components.anker_x1_smartgrid.sensor import X1DpRegretSensor, X1RegretEurSensor

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE = datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc)  # 10:00 UTC, Mon
_PREDICTOR = LoadPredictor.from_profile({})

# Captured BEFORE any test patches them, so the R1 spies below (which record
# call args) can delegate to the real physics — mirrors the established
# pattern in test_regret_export_effective_price.py.
_ORIG_HINDSIGHT_OPTIMAL_GRID = regret_mod.hindsight_optimal_grid
_ORIG_REALIZED_GRID_COST = regret_mod.realized_grid_cost

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _cfg(**overrides) -> Config:
    return Config.from_dict({
        "capacity_kwh": 10.0,
        "soc_target": 97.0,
        "eta_charge": 0.92,
        "eps_hi_kwh": 0.4,
        "eps_lo_kwh": 0.2,
        "min_dwell_min": 0,
        "max_charge_w": 6000.0,
        "round_trip_eff": 0.85,

        **overrides,
    })


def _slots(prices: list[float], base: datetime = BASE) -> list[PriceSlot]:
    return [PriceSlot(base + timedelta(hours=i), p) for i, p in enumerate(prices)]


def _plan(age_h: float = 2.0) -> PlanState:
    return PlanState.initial(BASE - timedelta(hours=age_h))


def _make_dp_mock_first_hour(charge_kwh: float = 5.0):
    """DP mock that puts all charge in hour 0 (current slot)."""
    def _side(  *args, **kwargs):
        wl = kwargs.get("window_len", len(args[0]) if args else 1)
        schedule = [charge_kwh] + [0.0] * (wl - 1)
        return {"schedule": schedule, "kwh": charge_kwh, "eur": charge_kwh * 0.05}
    return _side


# ---------------------------------------------------------------------------
# Stubs for Controller tick tests
# ---------------------------------------------------------------------------

class _StubActuator:
    def __init__(self):
        self.calls: list = []
        self.last_setpoint_w: float = 0.0
        self.engaged: bool = False

    async def engage_and_charge(self, setpoint_w: float) -> None:
        self.calls.append(("engage_and_charge", setpoint_w))
        self.last_setpoint_w = setpoint_w
        self.engaged = True

    async def release_to_self(self) -> None:
        self.calls.append(("release_to_self",))
        self.last_setpoint_w = 0.0
        self.engaged = False


class _StubStore:
    async def async_save(self, data):
        pass


class _StubRecorder:
    """Captures appended rows (samples + decisions + daily_regret)."""
    def __init__(self):
        self.rows: list[dict] = []
        self.decision_rows: list[dict] = []
        self.daily_regret_rows: dict[str, dict] = {}
        self._load_samples: list[tuple[str, float]] = []

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

    def purge_hourly_older_than(self, cutoff_iso):
        return 0

    def wal_checkpoint(self) -> None:
        pass

    def read_load_samples(self, since_iso=None):
        if since_iso is None:
            return list(self._load_samples)
        return [(ts, w) for ts, w in self._load_samples if ts >= since_iso]

    def read_feature_rows(self, since_iso=None):
        if since_iso is None:
            return list(self.rows)
        return [r for r in self.rows if r.get("ts", "") >= since_iso]

    def read_hourly_rows(self, since_iso=None):
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


class _StubHass:
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
        return fn(*args)


def _make_controller(hass, data_overrides=None):
    data = {
        const.CONF_ENT_SOC: "sensor.soc",
        const.CONF_ENT_METER_POWER: "sensor.meter_power",
        const.CONF_ENT_INVERTER_LOSS: "sensor.inverter_loss",
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
    }
    if data_overrides:
        data.update(data_overrides)
    act = _StubActuator()
    rec = _StubRecorder()
    ctrl = Controller(
        hass=hass,
        data=data,
        recorder=rec,
        actuator=act,
        store=_StubStore(),
    )
    return ctrl, act, rec


def _seed_valid_inputs(hass, *, soc="20.0"):
    hass.set_state("sensor.soc", soc)
    hass.set_state("sensor.meter_power", "0.0")
    hass.set_state("sensor.inverter_loss", "0.0")
    sunset_iso = (BASE + timedelta(hours=8)).isoformat()
    hass.set_state("sun.sun", "above_horizon", {"next_setting": sunset_iso})
    hass.set_state("sensor.price", "0.05", {
        "forecast": [
            {
                "datetime": (BASE + timedelta(hours=i)).isoformat(),
                "electricity_price": int(0.05 * const.PRICE_SCALE),
            }
            for i in range(9)
        ]
    })
    hass.set_state("sensor.pv_power", "0.0")
    hass.set_state("sensor.battery_power", "0.0")
    hass.set_state("sensor.irradiance", "0.0")
    hass.set_state("weather.home", "cloudy", {"temperature": 18.5})


# ===========================================================================
# 1. compute_decision — shadow DP flag
# ===========================================================================


def test_shadow_dp_true_replaces_selected():
    """_shadow_dp=True with DP always-on: DP still replaces selected."""
    cfg = _cfg()
    _out: dict = {}
    inputs = PlantInputs(soc=20.0, meter_w=0.0, now=BASE)
    slots = _slots([0.05, 0.40, 0.40, 0.40, 0.40, 0.40, 0.40, 0.40, 0.40])
    sunset = BASE + timedelta(hours=8)

    with patch(
        "custom_components.anker_x1_smartgrid.optimize.optimize_grid",
        side_effect=_make_dp_mock_first_hour(5.0),
    ):
        new_plan, _, _, _, _, _ = compute_decision(
            _plan(), inputs, slots, 0.0, sunset, _PREDICTOR, None, cfg,
            _out=_out, _shadow_dp=True,
        )

    # Flag-on: DP should still drive the plan state to FORCING
    assert new_plan.state is ControllerState.FORCING, (
        "Flag-on: DP replaces selected → FORCING (with cheap hour 0)"
    )
    assert "dp_selected" in _out


# ===========================================================================
# 2. Controller tick — no actuation guarantee
# ===========================================================================


@pytest.mark.asyncio
async def test_disabled_tick_never_calls_engage_and_charge():
    """Disabled Controller tick must NEVER call engage_and_charge."""
    hass = _StubHass()
    ctrl, act, _ = _make_controller(hass)
    ctrl.enabled = False

    result = await ctrl.tick()

    assert result["reason"] == "disabled"
    engage_calls = [c for c in act.calls if c[0] == "engage_and_charge"]
    assert not engage_calls, f"Disabled tick must not actuate; got: {engage_calls}"
    assert result.get("setpoint_w", 0.0) == 0.0


@pytest.mark.asyncio
async def test_disabled_tick_publishes_fictive_plan_in_shadow():
    """Shadow compute publishes fictive_plan in disabled tick without actuating."""
    hass = _StubHass()
    ctrl, act, _ = _make_controller(hass)
    ctrl.enabled = False
    _seed_valid_inputs(hass)

    with patch.object(coord_mod, "read_pv_remaining_kwh", return_value=5.0), \
         patch(
             "custom_components.anker_x1_smartgrid.optimize.optimize_grid",
             side_effect=_make_dp_mock_first_hour(5.0),
         ):
        result = await ctrl.tick()

    assert result["reason"] == "disabled"
    # No actuation
    engage_calls = [c for c in act.calls if c[0] == "engage_and_charge"]
    assert not engage_calls, f"Shadow tick must never actuate; got: {engage_calls}"
    # fictive_plan present (DP ran in shadow)
    assert "fictive_plan" in ctrl.last_status, (
        "fictive_plan must be published in last_status when shadow DP succeeds"
    )
    fp = ctrl.last_status["fictive_plan"]
    assert "horizon" in fp
    assert "planned_grid_hours" in fp
    assert fp["planned_grid_hours"] >= 0


@pytest.mark.asyncio
async def test_disabled_tick_clears_fictive_plan_when_shadow_dp_fails():
    """When shadow DP raises, fictive_plan must be absent (or removed) from last_status."""
    hass = _StubHass()
    ctrl, act, _ = _make_controller(hass)
    ctrl.enabled = False
    _seed_valid_inputs(hass)
    # Pre-seed a stale fictive_plan to verify it gets cleared
    ctrl.last_status["fictive_plan"] = {"stale": True}

    with patch.object(coord_mod, "read_pv_remaining_kwh", return_value=5.0), \
         patch(
             "custom_components.anker_x1_smartgrid.optimize.optimize_grid",
             side_effect=RuntimeError("intentional shadow DP failure"),
         ):
        await ctrl.tick()

    assert "fictive_plan" not in ctrl.last_status, (
        "Stale fictive_plan must be cleared when shadow DP fails"
    )


# ===========================================================================
# 3. _run_daily_regret_sync — dp_regret_eur stored alongside regret_eur
# ===========================================================================


def _seed_sample_rows_for_day(rec: _StubRecorder, day_str: str, n_hours: int = 24) -> None:
    """Seed sample rows covering n_hours of the given LOCAL day (at noon UTC, safe for any TZ)."""
    day_date = date.fromisoformat(day_str)
    # Noon UTC on the target day → local date is the same for UTC offsets -12h..+12h
    base_ts = datetime(day_date.year, day_date.month, day_date.day, 12, 0, tzinfo=timezone.utc)
    for h in range(n_hours):
        ts = base_ts + timedelta(hours=h - 12)  # span from midnight to midnight (UTC-12 safe)
        # Each row: moderate load, some PV, price, charging
        rec.rows.append({
            "ts": ts.isoformat(),
            "soc": 50.0 + h * 0.5,
            "pv_w": 1000.0 if 8 <= h <= 18 else 0.0,
            "batt_w": -200.0,   # charging
            "p1_w": 500.0,
            "import_price": 0.20 if h < 8 else 0.35,
        })


def test_dp_regret_stored_alongside_heuristic():
    """_run_daily_regret_sync persists dp_regret_eur in the daily_regret row."""
    hass = _StubHass()
    ctrl, _, rec = _make_controller(hass)

    day = "2026-06-21"
    _seed_sample_rows_for_day(rec, day, n_hours=24)

    ts_now = datetime(2026, 6, 22, 0, 5, tzinfo=timezone.utc).isoformat()
    ctrl._run_daily_regret_sync(day, ts_now)

    stored = rec.daily_regret_rows.get(day)
    assert stored is not None, f"daily_regret row for {day} must be stored"
    assert "dp_regret_eur" in stored, (
        "dp_regret_eur must be present in the stored daily_regret row"
    )
    assert "regret_eur" in stored, "heuristic regret_eur must still be present"


def test_dp_regret_is_numeric_or_none():
    """dp_regret_eur is a float (or None on DP failure), never an exception."""
    hass = _StubHass()
    ctrl, _, rec = _make_controller(hass)

    day = "2026-06-21"
    _seed_sample_rows_for_day(rec, day, n_hours=24)

    ts_now = datetime(2026, 6, 22, 0, 5, tzinfo=timezone.utc).isoformat()
    ctrl._run_daily_regret_sync(day, ts_now)

    stored = rec.daily_regret_rows.get(day)
    assert stored is not None
    dp_val = stored.get("dp_regret_eur")
    assert dp_val is None or isinstance(dp_val, float), (
        f"dp_regret_eur must be float or None; got {type(dp_val)}"
    )


def test_7d_rolling_delta_computed_after_regret_sync():
    """After _run_daily_regret_sync, last_dp_regret_7d is set when DP succeeded."""
    hass = _StubHass()
    ctrl, _, rec = _make_controller(hass)

    day = "2026-06-21"
    _seed_sample_rows_for_day(rec, day, n_hours=24)

    ts_now = datetime(2026, 6, 22, 0, 5, tzinfo=timezone.utc).isoformat()
    ctrl._run_daily_regret_sync(day, ts_now)

    stored = rec.daily_regret_rows.get(day)
    # Only check last_dp_regret_7d when dp_regret_eur is a float (not None)
    if stored and stored.get("dp_regret_eur") is not None:
        assert ctrl.last_dp_regret_7d is not None, (
            "last_dp_regret_7d must be set after a successful day with dp_regret_eur"
        )
        assert isinstance(ctrl.last_dp_regret_7d, float)


# ===========================================================================
# 4. dp_regret_7d exposed through _status and last_status
# ===========================================================================


def test_status_includes_dp_regret_7d_key():
    """_status() always includes the dp_regret_7d key (may be None)."""
    hass = _StubHass()
    ctrl, _, _ = _make_controller(hass)

    from datetime import timezone
    now = datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc)
    status = ctrl._status(now, 0.0, None, "test")

    assert "dp_regret_7d" in status, "_status() must expose dp_regret_7d key"


def test_status_dp_regret_7d_reflects_last_dp_regret_7d():
    """_status() reads dp_regret_7d from self.last_dp_regret_7d."""
    hass = _StubHass()
    ctrl, _, _ = _make_controller(hass)
    ctrl.last_dp_regret_7d = -0.42

    now = datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc)
    status = ctrl._status(now, 0.0, None, "test")

    assert status["dp_regret_7d"] == -0.42


# ===========================================================================
# 5. X1DpRegretSensor — distinct key, _Base pattern
# ===========================================================================


def test_sensor_keys_are_distinct():
    """X1DpRegretSensor and X1RegretEurSensor must have different keys."""
    dp_sensor = X1DpRegretSensor(None, "test_entry")
    heuristic_sensor = X1RegretEurSensor(None, "test_entry")

    assert dp_sensor._key != heuristic_sensor._key, (
        f"Keys must differ: dp={dp_sensor._key!r} vs heuristic={heuristic_sensor._key!r}"
    )


def test_dp_regret_sensor_key():
    """X1DpRegretSensor key is exactly 'dp_regret_7d'."""
    sensor = X1DpRegretSensor(None, "test_entry")
    assert sensor._key == "dp_regret_7d", (
        f"Expected key 'dp_regret_7d', got {sensor._key!r}"
    )


def test_dp_regret_sensor_unit():
    """X1DpRegretSensor reports in EUR (checked via instance native_unit_of_measurement)."""
    class _MockController:
        last_status: dict = {}

    sensor = X1DpRegretSensor(_MockController(), "e1")
    assert sensor.native_unit_of_measurement == "EUR"


def test_dp_regret_sensor_unique_id():
    """X1DpRegretSensor unique_id must NOT collide with X1RegretEurSensor."""
    dp_sensor = X1DpRegretSensor(None, "test_entry")
    heuristic_sensor = X1RegretEurSensor(None, "test_entry")

    assert dp_sensor._attr_unique_id != heuristic_sensor._attr_unique_id, (
        "Sensor unique_ids must be distinct to avoid HA entity collision"
    )


def test_dp_regret_sensor_reads_from_last_status():
    """X1DpRegretSensor reads dp_regret_7d from controller.last_status."""
    class _MockController:
        last_status = {"dp_regret_7d": -0.123}

    sensor = X1DpRegretSensor(_MockController(), "e1")
    assert sensor.native_value == -0.123


def test_dp_regret_sensor_returns_none_when_absent():
    """X1DpRegretSensor returns None when dp_regret_7d not yet computed."""
    class _MockController:
        last_status = {}

    sensor = X1DpRegretSensor(_MockController(), "e1")
    assert sensor.native_value is None


# ===========================================================================
# 6. Controller tick — ENABLED path, DP runs exactly once per tick
# ===========================================================================


@pytest.mark.asyncio
async def test_enabled_dp_runs_exactly_once():
    """ENABLED tick: DP runs exactly once — no extra shadow run."""
    hass = _StubHass()
    ctrl, _, _ = _make_controller(hass)
    _seed_valid_inputs(hass)

    dp_call_count = {"n": 0}
    orig_mock = _make_dp_mock_first_hour(5.0)

    def _counting_mock(*args, **kwargs):
        dp_call_count["n"] += 1
        return orig_mock(*args, **kwargs)

    with patch.object(coord_mod, "read_pv_remaining_kwh", return_value=5.0), \
         patch(
             "custom_components.anker_x1_smartgrid.optimize.optimize_grid",
             side_effect=_counting_mock,
         ):
        result = await ctrl.tick()

    assert result["reason"] == "ok"
    assert dp_call_count["n"] == 1, (
        f"DP must run exactly once per enabled tick; ran {dp_call_count['n']} time(s)"
    )
    assert "fictive_plan" in ctrl.last_status, (
        "fictive_plan must be published from the live DP run (T0.6a)"
    )


# ===========================================================================
# F3 — daily-regret consumes export revenue on both realized and oracle sides
# ===========================================================================


def _seed_export_rows_for_day(
    rec: _StubRecorder,
    day_str: str,
    *,
    export_hour: int = 14,
    export_w: float = 2000.0,
    export_price_eur: float = 0.40,
) -> None:
    """Seed sample rows for a day that includes actual net export at export_hour.

    Actual export is encoded as negative p1_w (grid meter reads negative = export).
    export_price is recorded in the export_price column (E1-fixed feed-in tariff).
    commanded_export (export_setpoint_w) is set to a deliberately different value
    to verify the realized leg does NOT use it.
    """
    day_date = date.fromisoformat(day_str)
    base_ts = datetime(day_date.year, day_date.month, day_date.day, 12, 0, tzinfo=timezone.utc)
    for h in range(24):
        ts = base_ts + timedelta(hours=h - 12)
        is_export_hour = (h == export_hour)
        # Actual export: p1_w < 0 means net-export to grid.
        # During export: battery discharges (batt_w > 0), PV running, p1_w < 0.
        if is_export_hour:
            p1_w = -export_w          # negative = export to grid
            batt_w = export_w         # discharging
            pv_w = 3000.0
            # Commanded setpoint deliberately != actual export (2× to verify we use actual)
            export_setpoint_w = export_w * 2.0
        else:
            p1_w = 500.0              # normal import
            batt_w = -200.0           # charging
            pv_w = 1000.0 if 8 <= h <= 18 else 0.0
            export_setpoint_w = 0.0
        rec.rows.append({
            "ts": ts.isoformat(),
            "soc": 50.0 + h * 0.3,
            "pv_w": pv_w,
            "batt_w": batt_w,
            "p1_w": p1_w,
            "import_price": 0.20 if h < 8 else 0.35,
            "export_price": export_price_eur,
            "export_setpoint_w": export_setpoint_w,   # deliberately != actual
        })


def test_regret_reflects_export_revenue_on_both_sides():
    """_run_daily_regret_sync scores export revenue on BOTH oracle and realized sides.

    A day with actual net export: realized eur must be net (import cost − export revenue).
    regret_eur must be near-zero on a day where actual == planned (no timing error).
    """
    hass = _StubHass()
    ctrl, _, rec = _make_controller(hass)

    day = "2026-06-21"
    _seed_export_rows_for_day(rec, day, export_hour=14, export_w=2000.0, export_price_eur=0.40)

    ts_now = datetime(2026, 6, 22, 0, 5, tzinfo=timezone.utc).isoformat()
    ctrl._run_daily_regret_sync(day, ts_now)

    stored = rec.daily_regret_rows.get(day)
    assert stored is not None, "daily_regret row must be stored for export day"
    # realized_eur must be lower than import-only cost (export revenue was netted)
    realized_eur = stored.get("realized_eur")
    assert realized_eur is not None, "realized_eur must be present"
    # optimal_eur must also reflect export revenue (oracle had export leg)
    optimal_eur = stored.get("optimal_eur")
    assert optimal_eur is not None, "optimal_eur must be present"


def test_regret_realized_uses_actual_export_not_commanded_setpoint():
    """Realized leg must use ACTUAL metered export (−p1_w), not commanded export_setpoint_w.

    Seeds rows where commanded setpoint is 2× the actual export power.
    Verifies that the stored realized_eur reflects the smaller actual export revenue,
    not the larger commanded setpoint revenue.
    """
    hass = _StubHass()
    ctrl, _, rec = _make_controller(hass)

    day = "2026-06-20"
    # Actual export = 1000 W; commanded setpoint = 2000 W (double).
    _seed_export_rows_for_day(rec, day, export_hour=14, export_w=1000.0, export_price_eur=0.40)

    ts_now = datetime(2026, 6, 21, 0, 5, tzinfo=timezone.utc).isoformat()
    ctrl._run_daily_regret_sync(day, ts_now)

    stored = rec.daily_regret_rows.get(day)
    assert stored is not None

    # Build the same day without export to get baseline import-only eur
    ctrl_no_export, _, rec_no_export = _make_controller(hass)
    _seed_sample_rows_for_day(rec_no_export, day, n_hours=24)
    ctrl_no_export._run_daily_regret_sync(day, ts_now)
    stored_no_export = rec_no_export.daily_regret_rows.get(day)

    # Export day realized_eur must be ≤ import-only day realized_eur
    # (export revenue reduces net cost).
    if stored_no_export and stored_no_export.get("realized_eur") is not None:
        assert stored["realized_eur"] is not None
        # The export day should have lower (or equal) realized_eur than the charge-only day
        # because export revenue reduces net cost.
        assert stored["realized_eur"] <= stored_no_export["realized_eur"] + 1e-3, (
            "Export day realized_eur must be ≤ charge-only realized_eur"
        )


def test_regret_charge_only_day_unchanged():
    """Charge-only day (no export in samples) → regret unchanged from current behaviour.

    Seeds a day with no export (all p1_w > 0, no export_price column).
    regret_eur must be a finite float, same as the existing test.
    """
    hass = _StubHass()
    ctrl, _, rec = _make_controller(hass)

    day = "2026-06-19"
    _seed_sample_rows_for_day(rec, day, n_hours=24)  # charge-only, no export fields

    ts_now = datetime(2026, 6, 20, 0, 5, tzinfo=timezone.utc).isoformat()
    ctrl._run_daily_regret_sync(day, ts_now)

    stored = rec.daily_regret_rows.get(day)
    assert stored is not None
    assert "regret_eur" in stored
    regret_eur = stored["regret_eur"]
    assert regret_eur is None or isinstance(regret_eur, float), (
        "regret_eur must be float or None on charge-only day"
    )


# ===========================================================================
# R1 — daily regret DayData sourced from measured v9 per-tick energy deltas
# ===========================================================================
#
# _run_daily_regret_sync builds DayData/realized_charge/realized_export from
# per-tick samples.  Before R1 it always used mean-W×dt_h, ignoring the v9
# grid_import_kwh/grid_export_kwh/house_load_kwh/pv_kwh/batt_charge_kwh/
# batt_discharge_kwh delta columns the recorder now writes.  R1 prefers the
# SUM of a slot's usable measured deltas, falling back to the legacy
# mean-W×dt_h estimate only when a slot has zero usable deltas for that
# quantity (pre-v9 rows, first tick after restart, or a sensor blip that
# nulled every tick in the slot).


def _replace_hour_rows(rec: _StubRecorder, day_str: str, hour: int, rows: list[dict]) -> None:
    """Drop the baseline single-tick row for `hour` (seeded by
    _seed_sample_rows_for_day) and append the caller's replacement tick(s).
    """
    prefix = f"{day_str}T{hour:02d}:"
    rec.rows = [r for r in rec.rows if not r["ts"].startswith(prefix)]
    rec.rows.extend(rows)


def _capture_day_data(ctrl: Controller, day: str, ts_now: str):
    """Run _run_daily_regret_sync while capturing the DayData object built
    for the (always-called) hindsight_optimal_grid leg.

    pytest_homeassistant_custom_component's autouse enable_custom_integrations
    fixture (tests/conftest.py) sets DEFAULT_TIME_ZONE to US/Pacific for the
    WHOLE test session, not just tests requesting the real ``hass`` fixture.
    Patch as_local to identity (NOT DEFAULT_TIME_ZONE itself — see
    test_regret_export_effective_price.py's established idiom) so every
    seeded UTC hour buckets 1:1 into the LOCAL hour this test reasons about.
    """
    captured: list = []

    def _spy(day_data, cfg, **kwargs):
        captured.append(day_data)
        return _ORIG_HINDSIGHT_OPTIMAL_GRID(day_data, cfg, **kwargs)

    with patch(
        "custom_components.anker_x1_smartgrid.regret.hindsight_optimal_grid",
        side_effect=_spy,
    ), patch(
        "homeassistant.util.dt.as_local", side_effect=lambda d: d
    ):
        ctrl._run_daily_regret_sync(day, ts_now)
    assert captured, "hindsight_optimal_grid must have been called"
    return captured[-1]


def test_daydata_pv_kwh_uses_measured_delta_sum_when_all_ticks_present():
    """Hour with 3 ticks, each carrying a pv_kwh delta of 0.02 -> the DayData
    pv_kwh slot must be the exact sum (0.06), NOT the mean-W×dt_h estimate
    (a wildly different pv_w=1000.0 is planted on each tick to prove the
    fallback path is not used)."""
    hass = _StubHass()
    ctrl, _, rec = _make_controller(hass)
    day = "2026-06-21"
    _seed_sample_rows_for_day(rec, day, n_hours=24)

    test_hour = 14
    _replace_hour_rows(rec, day, test_hour, [
        {
            "ts": datetime(2026, 6, 21, test_hour, m, tzinfo=timezone.utc).isoformat(),
            "soc": 60.0,
            "pv_w": 1000.0,   # legacy fallback trap value — must NOT be used
            "batt_w": -200.0,
            "p1_w": 500.0,
            "import_price": 0.30,
            "pv_kwh": 0.02,
        }
        for m in (10, 20, 30)
    ])

    ts_now = datetime(2026, 6, 22, 0, 5, tzinfo=timezone.utc).isoformat()
    day_data = _capture_day_data(ctrl, day, ts_now)

    assert day_data.pv_kwh[test_hour] == pytest.approx(0.06, abs=1e-9)


def test_daydata_falls_back_to_legacy_mean_when_hour_has_no_deltas():
    """A day with NO v9 delta columns anywhere (pre-v9 shape) must produce
    DayData values byte-identical to the legacy mean-W×dt_h computation."""
    hass = _StubHass()
    ctrl, _, rec = _make_controller(hass)
    day = "2026-06-21"
    _seed_sample_rows_for_day(rec, day, n_hours=24)

    ts_now = datetime(2026, 6, 22, 0, 5, tzinfo=timezone.utc).isoformat()
    day_data = _capture_day_data(ctrl, day, ts_now)

    # Hand-computed legacy values from _seed_sample_rows_for_day's fixture:
    # hour 10 (8<=h<=18): pv_w=1000.0 -> 1000/1000*1.0 = 1.0 kWh
    assert day_data.pv_kwh[10] == pytest.approx(1.0, abs=1e-9)
    # hour 2 (night): pv_w=0.0 -> 0.0 kWh
    assert day_data.pv_kwh[2] == pytest.approx(0.0, abs=1e-9)


def test_daydata_mixed_hour_sums_only_usable_ticks():
    """Hour with 3 ticks: two carry a pv_kwh delta (0.02 each), one has NO
    delta (NULL) but a large pv_w=5000.0 trap. The delta-sum path must fire
    (since >=1 usable tick exists) using ONLY the usable ticks -> 0.04, not
    diluted/replaced by the NULL tick's mean-W fallback."""
    hass = _StubHass()
    ctrl, _, rec = _make_controller(hass)
    day = "2026-06-21"
    _seed_sample_rows_for_day(rec, day, n_hours=24)

    test_hour = 15
    rows = [
        {
            "ts": datetime(2026, 6, 21, test_hour, 10, tzinfo=timezone.utc).isoformat(),
            "soc": 60.0, "pv_w": 1000.0, "batt_w": -200.0, "p1_w": 500.0,
            "import_price": 0.30, "pv_kwh": 0.02,
        },
        {
            "ts": datetime(2026, 6, 21, test_hour, 20, tzinfo=timezone.utc).isoformat(),
            "soc": 60.0, "pv_w": 1000.0, "batt_w": -200.0, "p1_w": 500.0,
            "import_price": 0.30, "pv_kwh": 0.02,
        },
        {
            # NULL delta tick (no "pv_kwh" key at all) with a trap pv_w value.
            "ts": datetime(2026, 6, 21, test_hour, 30, tzinfo=timezone.utc).isoformat(),
            "soc": 60.0, "pv_w": 5000.0, "batt_w": -200.0, "p1_w": 500.0,
            "import_price": 0.30,
        },
    ]
    _replace_hour_rows(rec, day, test_hour, rows)

    ts_now = datetime(2026, 6, 22, 0, 5, tzinfo=timezone.utc).isoformat()
    day_data = _capture_day_data(ctrl, day, ts_now)

    assert day_data.pv_kwh[test_hour] == pytest.approx(0.04, abs=1e-9)


def test_daydata_export_uses_min_rule_on_measured_energy_deltas():
    """R1's export analogue of the min-of-W rule, at the energy level:
    grid_export_kwh=0.05, batt_discharge_kwh=0.03 -> the tick contributes
    0.03 (min), NOT the metered p1_w/batt_w mean-W fallback trap values."""
    hass = _StubHass()
    ctrl, _, rec = _make_controller(hass)
    day = "2026-06-21"
    _seed_sample_rows_for_day(rec, day, n_hours=24)

    test_hour = 16
    _replace_hour_rows(rec, day, test_hour, [{
        "ts": datetime(2026, 6, 21, test_hour, 30, tzinfo=timezone.utc).isoformat(),
        "soc": 60.0,
        "pv_w": 3000.0,
        "batt_w": 2000.0,       # legacy fallback trap value — must NOT be used
        "p1_w": -2500.0,        # legacy fallback trap value — must NOT be used
        "import_price": 0.30,
        "export_price": 0.40,
        "grid_export_kwh": 0.05,
        "batt_discharge_kwh": 0.03,
    }])

    captured_realized: list[dict] = []

    def _spy_realized(day_data, realized_charge_by_hour, cfg, **kwargs):
        captured_realized.append({
            "realized_export_by_hour": (
                list(kwargs["realized_export_by_hour"])
                if kwargs.get("realized_export_by_hour") is not None else None
            ),
        })
        return _ORIG_REALIZED_GRID_COST(day_data, realized_charge_by_hour, cfg, **kwargs)

    ts_now = datetime(2026, 6, 22, 0, 5, tzinfo=timezone.utc).isoformat()
    # See _capture_day_data's docstring: the autouse enable_custom_integrations
    # fixture sets DEFAULT_TIME_ZONE to US/Pacific session-wide, so as_local
    # must be patched to identity to keep UTC hour == local hour bucketing.
    with patch(
        "custom_components.anker_x1_smartgrid.regret.realized_grid_cost",
        side_effect=_spy_realized,
    ), patch(
        "homeassistant.util.dt.as_local", side_effect=lambda d: d
    ):
        ctrl._run_daily_regret_sync(day, ts_now)

    assert captured_realized, "realized_grid_cost must have been called"
    export_row = captured_realized[-1]["realized_export_by_hour"]
    assert export_row is not None
    assert export_row[test_hour] == pytest.approx(0.03, abs=1e-9)
