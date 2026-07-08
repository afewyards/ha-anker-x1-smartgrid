from datetime import datetime, timezone, timedelta
import pytest
from unittest.mock import AsyncMock, MagicMock
from custom_components.anker_x1_smartgrid.models import Config, PlanState, PlantInputs, PriceSlot, ControllerState
from custom_components.anker_x1_smartgrid import controller, const, forecast
from custom_components.anker_x1_smartgrid.forecast import LoadPredictor
from custom_components.anker_x1_smartgrid.hgbr import HGBRQuantileModel

BASE = datetime(2026, 6, 20, 11, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers for tick() tests
# ---------------------------------------------------------------------------

class _StubActuator:
    """Records calls to engage_and_charge / release_to_self."""
    def __init__(self):
        self.calls = []
        self.last_setpoint_w = 0.0
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
    """No-op store."""
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

    def read_load_samples(self, since_iso=None):
        if since_iso is None:
            return list(self._load_samples)
        return [(ts, w) for ts, w in self._load_samples if ts >= since_iso]

    def read_decisions(self, since_iso, until_iso=None):
        rows = [r for r in self.decision_rows if r.get("ts", "") >= since_iso]
        if until_iso:
            rows = [r for r in rows if r.get("ts", "") < until_iso]
        return rows

    def read_feature_rows(self, since_iso=None):
        if since_iso is None:
            return list(self.rows)
        return [r for r in self.rows if r.get("ts", "") >= since_iso]

    def read_hourly_rows(self):
        """Stub for HGBR path — no hourly rows in unit tests."""
        return []

    def read_efficiency_samples(self, since_iso=None):
        """Stub for the measured-efficiency-curve build — no samples in unit tests."""
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


def _make_controller(hass, actuator=None, data_overrides=None):
    """Build a Controller with minimal data config."""
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
    }
    if data_overrides:
        data.update(data_overrides)
    act = actuator or _StubActuator()
    rec = _StubRecorder()
    ctrl = controller.Controller(
        hass=hass,
        data=data,
        recorder=rec,
        actuator=act,
        store=_StubStore(),
    )
    return ctrl, act


def _seed_valid_inputs(hass, *, soc="20.0"):
    """Seed HA states so read_plant_inputs succeeds."""
    hass.set_state("sensor.soc", soc)
    hass.set_state("sensor.phase_l1", "0.0")
    hass.set_state("sensor.phase_l2", "0.0")
    hass.set_state("sensor.phase_l3", "0.0")
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


# ---------------------------------------------------------------------------
# FIX 3 — tick() level tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tick_failsafe_missing_soc():
    """When SoC entity is unavailable, tick() returns failsafe and calls release_to_self."""
    hass = _StubHass()
    ctrl, act = _make_controller(hass)

    # Leave sensor.soc un-seeded → read_plant_inputs returns None
    result = await ctrl.tick()

    assert result["reason"] == "failsafe"
    assert result["state"] == "passive"
    assert result["setpoint_w"] == 0.0
    assert any(c[0] == "release_to_self" for c in act.calls)


@pytest.mark.asyncio
async def test_tick_disabled():
    """When controller.enabled is False, not engaged AND not restart-into-engaged → does NOT call release_to_self."""
    hass = _StubHass()
    ctrl, act = _make_controller(hass)
    ctrl.enabled = False

    result = await ctrl.tick()

    assert result["reason"] == "disabled"
    assert result["state"] == "passive"
    assert not any(c[0] == "release_to_self" for c in act.calls)


@pytest.mark.asyncio
async def test_tick_disabled_after_restart_while_engaged_releases_once():
    """Restart into disabled while physically engaged (persisted export_state,
    fresh actuator.engaged=False) → ONE release on the first disabled tick, then
    hands-off within the same run (no clobber of a later manual mode)."""
    from custom_components.anker_x1_smartgrid.models import ExportState
    hass = _StubHass()
    ctrl, act = _make_controller(hass)
    ctrl.enabled = False
    ctrl.export_state = ExportState(engaged=True, state_since=BASE - timedelta(hours=1))
    act.engaged = False

    r1 = await ctrl.tick()
    assert r1["reason"] == "disabled"
    assert sum(1 for c in act.calls if c[0] == "release_to_self") == 1

    act.calls.clear()
    r2 = await ctrl.tick()
    assert not any(c[0] == "release_to_self" for c in act.calls)


@pytest.mark.asyncio
async def test_tick_disabled_persists_disengaged_so_next_restart_no_release():
    """The first-tick release must PERSIST disengaged/PASSIVE state, so a SECOND
    restart-while-disabled does NOT re-fire release (no repeated manual clobber)."""
    from custom_components.anker_x1_smartgrid.models import ExportState
    hass = _StubHass()
    store = ctrl_store = None
    # Capture what the controller persisted.
    saved = {}
    class _CaptureStore:
        async def async_save(self, data): saved.update(data)
    ctrl, act = _make_controller(hass)
    ctrl._store = _CaptureStore()
    ctrl.enabled = False
    ctrl.export_state = ExportState(engaged=True, state_since=BASE - timedelta(hours=1))
    act.engaged = False
    await ctrl.tick()
    assert saved.get("export_state", {}).get("engaged") is False   # persisted disengaged

    # Simulate a SECOND restart: fresh controller, restore the persisted (disengaged) state.
    ctrl2, act2 = _make_controller(_StubHass())
    ctrl2.restore(saved)
    ctrl2.enabled = False
    act2.engaged = False
    await ctrl2.tick()
    assert not any(c[0] == "release_to_self" for c in act2.calls)


@pytest.mark.asyncio
async def test_tick_forcing_to_passive_calls_release(monkeypatch):
    """FORCING→PASSIVE transition calls release_to_self."""
    hass = _StubHass()
    ctrl, act = _make_controller(hass)

    # Start the controller already in FORCING state
    ctrl.plan = PlanState(ControllerState.FORCING, BASE - timedelta(hours=1), ())

    # Seed valid inputs but with high SoC so decision comes back PASSIVE
    _seed_valid_inputs(hass, soc="98.0")

    result = await ctrl.tick()

    # The outcome should be passive (high SoC ≥ soc_target default 97.0)
    assert result["state"] == "passive"
    # release_to_self must have been called for the FORCING→PASSIVE transition
    assert any(c[0] == "release_to_self" for c in act.calls)


def _slots(prices):
    return [PriceSlot(BASE + timedelta(hours=i), p) for i, p in enumerate(prices)]


def test_decision_forces_when_deficit_and_cheap_now():
    cfg = Config(capacity_kwh=10.0, soc_target=100.0, eta_charge=1.0,
                 min_dwell_min=0, max_charge_w=6000.0)
    inputs = PlantInputs(soc=20.0, phase_import_w=(0.0, 0.0, 0.0), now=BASE)
    slots = _slots([0.05, 0.40, 0.40, 0.40, 0.40, 0.40, 0.40, 0.40, 0.40])
    sunset = BASE + timedelta(hours=8)
    plan = PlanState.initial(BASE - timedelta(hours=1))
    new_plan, setpoint, deadline, _horizon, _, _ = controller.compute_decision(
        plan, inputs, slots, pv_remaining=0.0, sunset=sunset,
        predictor=forecast.LoadPredictor.from_profile({}), cur_temp=None, cfg=cfg,
    )
    # DP selects the cheap slot → FORCING still correct.
    assert new_plan.state is ControllerState.FORCING
    assert setpoint < 0  # charging
    assert setpoint >= -6000.0  # capped by max_charge_w only


def test_decision_passive_when_solar_covers():
    cfg = Config(capacity_kwh=10.0, soc_target=100.0, eta_charge=1.0, min_dwell_min=0)
    inputs = PlantInputs(soc=80.0, phase_import_w=(0.0, 0.0, 0.0), now=BASE)
    slots = _slots([0.05] * 9)
    sunset = BASE + timedelta(hours=8)
    plan = PlanState.initial(BASE - timedelta(hours=1))
    new_plan, setpoint, _, _horizon, _, _ = controller.compute_decision(
        plan, inputs, slots, pv_remaining=20.0, sunset=sunset,
        predictor=forecast.LoadPredictor.from_profile({}), cur_temp=None, cfg=cfg,
    )
    assert new_plan.state is ControllerState.PASSIVE
    assert setpoint == 0.0


def test_decision_passive_high_soc():
    cfg = Config(soc_target=97.0, min_dwell_min=0)
    inputs = PlantInputs(soc=96.5, phase_import_w=(0.0, 0.0, 0.0), now=BASE)
    slots = _slots([0.05] * 9)
    sunset = BASE + timedelta(hours=8)
    plan = PlanState.initial(BASE - timedelta(hours=1))
    new_plan, setpoint, _, _horizon, _, _ = controller.compute_decision(
        plan, inputs, slots, pv_remaining=0.0, sunset=sunset,
        predictor=forecast.LoadPredictor.from_profile({}), cur_temp=None, cfg=cfg,
    )
    assert new_plan.state is ControllerState.PASSIVE
    assert setpoint == 0.0


@pytest.mark.asyncio
async def test_tick_ok_records_phase2_columns():
    """The recorded row must populate pv_w, batt_w, import_price, temp when entities are seeded."""
    hass = _StubHass()
    ctrl, _ = _make_controller(hass)
    _seed_valid_inputs(hass, soc="20.0")

    result = await ctrl.tick()
    assert result["reason"] == "ok"

    assert ctrl._recorder.rows, "Expected at least one recorded row"
    row = ctrl._recorder.rows[-1]
    assert row["pv_w"] == 1200.0, f"pv_w={row['pv_w']}"
    assert row["batt_w"] == -500.0, f"batt_w={row['batt_w']}"
    assert row["import_price"] == 0.05, f"import_price={row['import_price']}"
    assert row["irradiance"] == 350.0, f"irradiance={row['irradiance']}"
    assert row["temp"] == 18.5, f"temp={row['temp']}"


@pytest.mark.asyncio
async def test_tick_ok_records_none_when_entities_absent():
    """When phase-2 entities are missing, the recorded row has None (not an error)."""
    hass = _StubHass()
    ctrl, _ = _make_controller(hass)
    _seed_valid_inputs(hass, soc="20.0")
    # Override pv_power to unavailable
    hass.set_state("sensor.pv_power", "unavailable")
    hass.set_state("weather.home", "unknown")  # no attributes → temp=None

    result = await ctrl.tick()
    assert result["reason"] == "ok"

    row = ctrl._recorder.rows[-1]
    assert row["pv_w"] is None
    assert row["temp"] is None


# ---------------------------------------------------------------------------
# FIX C1 — rolling load profile wired into controller
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_refresh_profile_populates_profile():
    """After refresh_profile, controller.profile is non-empty and predict_load_w uses it."""
    import dataclasses
    from custom_components.anker_x1_smartgrid import forecast as forecast_mod

    hass = _StubHass()
    ctrl, _ = _make_controller(hass)

    # Two samples in the SAME (weekend, hour-10) bucket. Anchor them to the most
    # recent weekend relative to *now* so they never age out of the rolling
    # lookback window — hardcoded calendar dates rot as real time advances.
    ctrl.cfg = dataclasses.replace(ctrl.cfg, lookback_days=30)
    now = datetime.now(timezone.utc)
    last_sunday = (now - timedelta(days=(now.weekday() + 1) % 7)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )  # weekday: Mon=0..Sun=6
    last_saturday = last_sunday - timedelta(days=1)
    ctrl._recorder._load_samples = [
        (last_saturday.isoformat(), 1200.0),
        (last_sunday.isoformat(), 800.0),
    ]

    await ctrl.refresh_profile()

    assert ctrl.profile, "Expected non-empty profile after refresh"
    assert ctrl._last_profile_refresh is not None

    # Both samples are weekends, hour 10 → same (True, 10) bucket → mean of both
    learned = forecast_mod.predict_load_w(ctrl.profile, last_sunday, fallback_w=400.0)
    assert learned != 400.0, "Expected a learned value, not the 400 W fallback"
    assert abs(learned - 1000.0) < 1.0  # average of 1200 and 800


@pytest.mark.asyncio
async def test_refresh_profile_predictor_returns_p80_for_spread_distribution():
    """After refresh_profile, the profile-tier predictor returns P80 > P50 for a spread distribution.

    This is the key regression guard for Step 1: the profile fallback must no longer be
    a no-op for quantile > 0.5 once enough samples have been collected.
    """
    import dataclasses

    hass = _StubHass()
    ctrl, _ = _make_controller(hass)

    # 10 weekday samples at hour 8 in one (weekday, hour-8) bucket with a spread
    # distribution so P80 > P50. Anchor to the most recent weekdays relative to
    # *now* so they never age out of the lookback window — hardcoded calendar
    # dates rot as real time advances. Values 100..1000: P50=550 (interpolated),
    # P80=820 (pos=0.8*9=7.2 → 800+0.2*100). Quantiles sort the values, so the
    # date↔value pairing is irrelevant.
    now = datetime.now(timezone.utc)
    weekdays = []
    d = now.replace(hour=8, minute=0, second=0, microsecond=0)
    while len(weekdays) < 10:
        if d.weekday() < 5:  # Mon–Fri
            weekdays.append(d)
        d -= timedelta(days=1)
    load_values = [100.0, 200.0, 300.0, 400.0, 500.0, 600.0, 700.0, 800.0, 900.0, 1000.0]
    ctrl._recorder._load_samples = [
        (wd.isoformat(), w) for wd, w in zip(weekdays, load_values)
    ]

    # Lookback wide enough to cover all 10 weekdays (~2 calendar weeks)
    ctrl.cfg = dataclasses.replace(ctrl.cfg, lookback_days=30)

    await ctrl.refresh_profile()

    when = weekdays[0]  # a weekday at hour 8 → (False, 8)
    p50 = ctrl._profile_predictor.predict(when, temp=None, fallback_w=0.0, quantile=0.5)
    p80 = ctrl._profile_predictor.predict(when, temp=None, fallback_w=0.0, quantile=0.8)

    assert p50 == 550.0, f"P50 must be mean=550, got {p50}"
    assert p80 > p50, f"P80={p80} must exceed P50={p50} after profile refresh"
    assert abs(p80 - 820.0) < 1e-9, f"P80 must be 820.0 (interpolated), got {p80}"


@pytest.mark.asyncio
async def test_tick_triggers_profile_refresh():
    """First tick sets _last_profile_refresh and populates profile when samples exist."""
    hass = _StubHass()
    ctrl, _ = _make_controller(hass)
    _seed_valid_inputs(hass, soc="20.0")

    # Seed recent load samples (relative to now, not hardcoded dates) so they
    # stay inside the lookback window (now - lookback_days) as real time advances.
    recent = datetime.now(timezone.utc)
    ctrl._recorder._load_samples = [
        ((recent - timedelta(hours=2)).isoformat(), 900.0),
        ((recent - timedelta(hours=1)).isoformat(), 750.0),
    ]

    result = await ctrl.tick()
    assert result["reason"] == "ok"
    assert ctrl._last_profile_refresh is not None, "Expected profile refresh to have run"
    assert ctrl.profile, "Expected non-empty profile after first tick"


# ---------------------------------------------------------------------------
# FIX M4 — all-PV-unavailable triggers tick() failsafe
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tick_failsafe_when_all_pv_unavailable():
    """tick() returns failsafe when all PV-today sensors are unavailable."""
    hass = _StubHass()
    # Use two PV today entities, both unavailable
    ctrl, act = _make_controller(hass, data_overrides={
        const.CONF_ENT_PV_TODAY: ["sensor.pv1", "sensor.pv2"],
    })
    _seed_valid_inputs(hass, soc="20.0")
    hass.set_state("sensor.pv1", "unavailable")
    hass.set_state("sensor.pv2", "unavailable")

    result = await ctrl.tick()
    assert result["reason"] == "failsafe", f"Expected failsafe, got {result['reason']}"
    assert any(c[0] == "release_to_self" for c in act.calls)


@pytest.mark.asyncio
async def test_tick_ok_when_pv_genuinely_zero():
    """tick() does NOT trigger failsafe when PV reads 0.0 (night-time)."""
    hass = _StubHass()
    ctrl, act = _make_controller(hass, data_overrides={
        const.CONF_ENT_PV_TODAY: ["sensor.pv1"],
    })
    _seed_valid_inputs(hass, soc="20.0")
    hass.set_state("sensor.pv1", "0.0")

    result = await ctrl.tick()
    assert result["reason"] == "ok", f"Expected ok for genuine 0 PV, got {result['reason']}"


# ---------------------------------------------------------------------------
# E1 — export_price records the REAL feed-in tariff (not import price)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_record_sample_stores_real_export_price_from_entity():
    """When ent_export_price is configured, export_price records its float value (not import price)."""
    hass = _StubHass()
    ctrl, _ = _make_controller(
        hass,
        data_overrides={const.CONF_ENT_EXPORT_PRICE: "sensor.export_price"},
    )
    _seed_valid_inputs(hass, soc="20.0")
    hass.set_state("sensor.export_price", "0.03")  # feed-in = 3c, import might be 5c

    result = await ctrl.tick()
    assert result["reason"] == "ok"

    row = ctrl._recorder.rows[-1]
    import_price = row.get("import_price")
    export_price = row.get("export_price")
    assert export_price == pytest.approx(0.03), (
        f"export_price should be 0.03 (real feed-in), got {export_price}"
    )
    assert export_price != import_price, (
        f"export_price must NOT equal import_price {import_price}; the placeholder bug is not fixed"
    )


@pytest.mark.asyncio
async def test_record_sample_export_price_none_when_entity_unset():
    """When ent_export_price is absent/empty, export_price is None (never mirrors import price)."""
    hass = _StubHass()
    # No CONF_ENT_EXPORT_PRICE in overrides → key absent from data
    ctrl, _ = _make_controller(hass)
    _seed_valid_inputs(hass, soc="20.0")

    result = await ctrl.tick()
    assert result["reason"] == "ok"

    row = ctrl._recorder.rows[-1]
    assert row.get("export_price") is None, (
        f"export_price must be None when entity unset, got {row.get('export_price')}"
    )


@pytest.mark.asyncio
async def test_record_sample_export_price_none_when_entity_empty_string():
    """Empty string ent_export_price is treated as unset → export_price is None."""
    hass = _StubHass()
    ctrl, _ = _make_controller(
        hass,
        data_overrides={const.CONF_ENT_EXPORT_PRICE: ""},
    )
    _seed_valid_inputs(hass, soc="20.0")

    result = await ctrl.tick()
    assert result["reason"] == "ok"

    row = ctrl._recorder.rows[-1]
    assert row.get("export_price") is None, (
        f"export_price must be None when conf is empty string, got {row.get('export_price')}"
    )


# ---------------------------------------------------------------------------
# FIX CONF_ENT_TEMP — missing key does not KeyError
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tick_ok_without_conf_ent_temp():
    """tick() must not raise KeyError when CONF_ENT_TEMP is absent from config."""
    hass = _StubHass()
    data_overrides = {const.CONF_ENT_TEMP: None}  # None signals no temp entity
    # We need to actually remove the key to test the .get() guard
    ctrl, _ = _make_controller(hass)
    del ctrl._data[const.CONF_ENT_TEMP]  # simulate old config entry missing the key
    _seed_valid_inputs(hass, soc="20.0")

    result = await ctrl.tick()
    assert result["reason"] == "ok"
    row = ctrl._recorder.rows[-1]
    assert row["temp"] is None


# ---------------------------------------------------------------------------
# Task 4 — two-day display forecast wired into controller horizon
# ---------------------------------------------------------------------------

def test_compute_decision_horizon_spans_two_days_when_sun_times_present():
    from datetime import datetime, timezone, timedelta
    from custom_components.anker_x1_smartgrid.controller import compute_decision
    from custom_components.anker_x1_smartgrid.models import Config, PlanState, PlantInputs, PriceSlot
    from custom_components.anker_x1_smartgrid.forecast import LoadPredictor

    now = datetime(2026, 6, 20, 17, 0, tzinfo=timezone.utc)
    inputs = PlantInputs(50.0, (0.0, 0.0, 0.0), now)
    # 30 hourly slots from now → into tomorrow afternoon
    slots = [PriceSlot(now + timedelta(hours=i), 0.30) for i in range(30)]
    sunset = datetime(2026, 6, 20, 20, 0, tzinfo=timezone.utc)
    sun_times = (
        sunset,
        datetime(2026, 6, 21, 6, 0, tzinfo=timezone.utc),
        datetime(2026, 6, 21, 20, 0, tzinfo=timezone.utc),
    )
    predictor = LoadPredictor.from_profile({})  # fallback predictor
    plan0 = PlanState.initial(now)

    _, _, _, horizon, _, _ = compute_decision(
        plan0, inputs, slots, 1.0, sunset, predictor, 15.0, Config(),
        tomorrow_total=6.0, sun_times=sun_times,
    )
    future = [e for e in horizon if e["start"] >= now.isoformat()]
    # load_w predicted for every future hour (incl. tomorrow), not just up to deadline
    assert all(e["load_w"] is not None for e in future)
    # at least one tomorrow-daytime hour carries PV > 0
    assert any(e["pv_w"] and e["pv_w"] > 0 for e in future)


# ---------------------------------------------------------------------------
# Per-array peaked PV curve — Component 3 tests
# ---------------------------------------------------------------------------

def test_compute_decision_display_horizon_shoulder_lift_with_arrays():
    """Display horizon via compute_decision with E/W tomorrow_arrays shows shoulder lift."""
    now = datetime(2026, 6, 20, 23, 0, tzinfo=timezone.utc)  # night
    inputs = PlantInputs(soc=50.0, phase_import_w=(0.0, 0.0, 0.0), now=now)
    # 30 slots — covers tomorrow daytime
    slots = [PriceSlot(now + timedelta(hours=i), 0.30) for i in range(30)]
    sunset = now + timedelta(hours=1)  # minimal today window → deadline ≈ now + 1h
    sun_times = (
        datetime(2026, 6, 20, 20, 0, tzinfo=timezone.utc),   # today_sunset (past)
        datetime(2026, 6, 21, 6, 0, tzinfo=timezone.utc),    # tomorrow_sunrise
        datetime(2026, 6, 21, 20, 0, tzinfo=timezone.utc),   # tomorrow_sunset
    )
    early_peak = datetime(2026, 6, 21, 9, 0, tzinfo=timezone.utc)
    late_peak = datetime(2026, 6, 21, 17, 0, tzinfo=timezone.utc)
    mid_hour = datetime(2026, 6, 21, 13, 0, tzinfo=timezone.utc)

    plan0 = PlanState.initial(now - timedelta(hours=1))
    cfg = Config(capacity_kwh=10.0, soc_target=97.0, eta_charge=1.0,
                 max_charge_w=5000.0, min_dwell_min=0, deadline_buffer_min=0)
    predictor = forecast.LoadPredictor.from_profile({})

    ew_arrays = [(3.0, early_peak), (3.0, late_peak)]
    centered_arrays = [(6.0, None)]  # peaks at midpoint ≈ 13:00

    def _decide(tomorrow_arrays):
        _, _, _, horizon, _, _ = controller.compute_decision(
            plan0, inputs, slots, pv_remaining=0.0, sunset=sunset,
            predictor=predictor, cur_temp=None, cfg=cfg,
            sun_times=sun_times, tomorrow_arrays=tomorrow_arrays,
        )
        return horizon

    horizon_ew = _decide(ew_arrays)
    horizon_centered = _decide(centered_arrays)

    assert horizon_ew, "E/W horizon must be non-empty"

    def _pv_at(horizon, dt):
        key = dt.isoformat()
        for e in horizon:
            if e["start"] == key:
                return e["pv_w"]
        return None

    ew_at_early = _pv_at(horizon_ew, early_peak)
    ew_at_late = _pv_at(horizon_ew, late_peak)
    ew_at_mid = _pv_at(horizon_ew, mid_hour)
    centered_at_early = _pv_at(horizon_centered, early_peak)
    centered_at_late = _pv_at(horizon_centered, late_peak)
    centered_at_mid = _pv_at(horizon_centered, mid_hour)

    assert ew_at_early is not None and centered_at_early is not None
    assert ew_at_late is not None and centered_at_late is not None
    assert ew_at_mid is not None and centered_at_mid is not None

    # Shoulder lift: E/W raises early and late hours vs centred
    assert ew_at_early > centered_at_early, (
        f"E/W pv_w at 09:00 ({ew_at_early:.1f}) must exceed centred ({centered_at_early:.1f})"
    )
    assert ew_at_late > centered_at_late, (
        f"E/W pv_w at 17:00 ({ew_at_late:.1f}) must exceed centred ({centered_at_late:.1f})"
    )
    # Midday is lower in E/W (valley between the two lobes)
    assert ew_at_mid < centered_at_mid, (
        f"E/W pv_w at 13:00 ({ew_at_mid:.1f}) must be below centred ({centered_at_mid:.1f})"
    )

    # Energy conservation: sum of pv_w ≈ 6 kWh = 6000 Wh (tomorrow only, no today PV)
    total_pv_wh = sum(e["pv_w"] for e in horizon_ew if e["pv_w"])
    assert abs(total_pv_wh - 6000) < 300, (
        f"Expected ~6000 Wh total PV energy, got {total_pv_wh:.1f} Wh"
    )


# ---------------------------------------------------------------------------
# Task 2 — persist enabled flag across restarts
# ---------------------------------------------------------------------------

class _CapturingStore:
    def __init__(self):
        self.saved = None
    async def async_save(self, data):
        self.saved = data


@pytest.mark.asyncio
async def test_persist_writes_wrapped_payload():
    hass = _StubHass()
    ctrl, _ = _make_controller(hass)
    ctrl._store = _CapturingStore()
    await ctrl._persist()
    assert set(ctrl._store.saved.keys()) == {
        "plan", "enabled", "export_state",
        "today_export_pnl_eur", "export_pnl_day",
        "soc_drift_kwh", "soc_drift_day", "soc_drift_last_update",
        "soc_drift_last_soc_pct", "soc_drift_engaged", "soc_drift_last_export_kwh_dc",
    }
    assert ctrl._store.saved["enabled"] is True


@pytest.mark.asyncio
async def test_set_enabled_persists():
    hass = _StubHass()
    ctrl, _ = _make_controller(hass)
    ctrl._store = _CapturingStore()
    await ctrl.set_enabled(False)
    assert ctrl.enabled is False
    assert ctrl._store.saved["enabled"] is False


def test_restore_wrapped_payload():
    hass = _StubHass()
    ctrl, _ = _make_controller(hass)
    plan_dict = PlanState.initial(BASE).to_dict()
    ctrl.restore({"plan": plan_dict, "enabled": False})
    assert ctrl.enabled is False


def test_restore_legacy_bare_plan_defaults_enabled_true():
    hass = _StubHass()
    ctrl, _ = _make_controller(hass)
    ctrl.enabled = True
    plan_dict = PlanState.initial(BASE).to_dict()
    ctrl.restore(plan_dict)  # legacy: bare plan dict, no wrapper
    assert ctrl.enabled is True


# ---------------------------------------------------------------------------
# Task 1 — record physical sample while disabled
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tick_disabled_records_sample(monkeypatch):
    monkeypatch.setattr(controller.dt_util, "utcnow", lambda: BASE)
    hass = _StubHass()
    ctrl, act = _make_controller(hass, data_overrides={const.CONF_SOC_FLOOR: 10.0})
    # Pin soc_floor=10 (above the new 5% default) so soc=5 is BELOW the floor →
    # the water-value survival-floor deficit is non-zero, keeping this assertion
    # meaningful. (Default floor is now the firmware 5%; soc cannot go below it.)
    _seed_valid_inputs(hass, soc="5.0")
    ctrl.enabled = False

    result = await ctrl.tick()

    assert result["reason"] == "disabled"
    assert len(ctrl._recorder.rows) == 1
    row = ctrl._recorder.rows[0]
    assert row["state"] == "disabled"
    assert row["setpoint_w"] == 0.0
    # Task 3 (P80-survival-removal): deficit_kwh removed from recorder rows.
    assert "deficit_kwh" not in row
    assert row["soc"] == 5.0
    assert row["pv_w"] == 1200.0      # from _seed_valid_inputs
    assert row["batt_w"] == -500.0


@pytest.mark.asyncio
async def test_tick_disabled_no_inputs_skips_record():
    hass = _StubHass()
    ctrl, act = _make_controller(hass)
    # sensor.soc un-seeded -> read_plant_inputs returns None
    ctrl.enabled = False

    result = await ctrl.tick()

    assert result["reason"] == "disabled"
    assert ctrl._recorder.rows == []


# ---------------------------------------------------------------------------
# Task 3 — disabled path refreshes predictor + publishes horizon
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tick_disabled_publishes_self_consumption_horizon():
    hass = _StubHass()
    ctrl, _ = _make_controller(hass, data_overrides={
        const.CONF_ENT_PV_TODAY: ["sensor.pv_today"],
        const.CONF_ENT_PV_TOMORROW: ["sensor.pv_tomorrow"],
    })
    _seed_valid_inputs(hass, soc="50.0")
    # read_sun_times needs next_rising too (sunrise tomorrow) + a tomorrow sunset
    rising_iso = (BASE + timedelta(hours=15)).isoformat()
    setting_iso = (BASE + timedelta(hours=3)).isoformat()
    hass.set_state("sun.sun", "above_horizon",
                   {"next_setting": setting_iso, "next_rising": rising_iso})
    hass.set_state("sensor.pv_today", "1.0")
    hass.set_state("sensor.pv_tomorrow", "6.0")
    ctrl.enabled = False

    result = await ctrl.tick()

    assert result["reason"] == "disabled"
    plan = ctrl.last_status.get("plan")
    assert plan is not None, "disabled tick must publish a plan"
    assert plan["planned_grid_hours"] == 0
    assert plan["horizon"], "horizon must be non-empty"
    assert all(e["mode"] != "grid" for e in plan["horizon"])


@pytest.mark.asyncio
async def test_tick_disabled_forwards_temp_by_hour_to_display_horizon(monkeypatch):
    """Regression: the disabled-path self-consumption publish call (controller.py
    ~1680) must forward temp_by_hour to build_display_horizon too, mirroring the
    enabled path (compute_decision, controller.py ~1099) — before the fix the kwarg
    was omitted entirely (not just empty)."""
    hass = _StubHass()
    ctrl, _ = _make_controller(hass, data_overrides={
        const.CONF_ENT_PV_TODAY: ["sensor.pv_today"],
        const.CONF_ENT_PV_TOMORROW: ["sensor.pv_tomorrow"],
    })
    _seed_valid_inputs(hass, soc="50.0")
    rising_iso = (BASE + timedelta(hours=15)).isoformat()
    setting_iso = (BASE + timedelta(hours=3)).isoformat()
    hass.set_state("sun.sun", "above_horizon",
                   {"next_setting": setting_iso, "next_rising": rising_iso})
    hass.set_state("sensor.pv_today", "1.0")
    hass.set_state("sensor.pv_tomorrow", "6.0")
    ctrl.enabled = False

    captured_kwargs: dict = {}
    real = controller.plan_mod.build_display_horizon

    def spy(*a, **kw):
        captured_kwargs.update(kw)
        return real(*a, **kw)

    monkeypatch.setattr(controller.plan_mod, "build_display_horizon", spy)

    result = await ctrl.tick()

    assert result["reason"] == "disabled"
    assert "temp_by_hour" in captured_kwargs, (
        "build_display_horizon called without a temp_by_hour kwarg on the disabled path"
    )


@pytest.mark.asyncio
async def test_tick_disabled_missing_forecast_skips_horizon_but_records():
    hass = _StubHass()
    ctrl, _ = _make_controller(hass)  # CONF_ENT_PV_TODAY = [] -> pv_remaining 0.0; sun has no next_rising
    _seed_valid_inputs(hass, soc="50.0")  # sun.sun has next_setting only -> read_sun_times None
    ctrl.enabled = False

    result = await ctrl.tick()

    assert result["reason"] == "disabled"
    assert len(ctrl._recorder.rows) == 1            # still records
    assert ctrl.last_status.get("plan") is None     # no horizon without sun_times


@pytest.mark.asyncio
async def test_tick_disabled_refreshes_profile():
    hass = _StubHass()
    ctrl, _ = _make_controller(hass)
    _seed_valid_inputs(hass, soc="50.0")
    # Recent samples relative to now (see test_tick_triggers_profile_refresh):
    # hardcoded absolute dates rot past the lookback window over time.
    recent = datetime.now(timezone.utc)
    ctrl._recorder._load_samples = [
        ((recent - timedelta(hours=2)).isoformat(), 900.0),
        ((recent - timedelta(hours=1)).isoformat(), 750.0),
    ]
    ctrl.enabled = False

    await ctrl.tick()

    assert ctrl._last_profile_refresh is not None, "disabled tick must refresh the profile"
    assert ctrl.profile, "profile populated from recorded samples while disabled"


@pytest.mark.asyncio
async def test_tick_disabled_skips_horizon_when_pv_remaining_none():
    """When pv_remaining is None (all PV sensors unavailable), disabled tick must not publish a plan."""
    hass = _StubHass()
    ctrl, _ = _make_controller(hass, data_overrides={
        const.CONF_ENT_PV_TODAY: ["sensor.pv_today"],
        const.CONF_ENT_PV_TOMORROW: ["sensor.pv_tomorrow"],
    })
    _seed_valid_inputs(hass, soc="50.0")
    # Seed sun_times so the only missing piece is pv_remaining
    rising_iso = (BASE + timedelta(hours=15)).isoformat()
    setting_iso = (BASE + timedelta(hours=3)).isoformat()
    hass.set_state("sun.sun", "above_horizon",
                   {"next_setting": setting_iso, "next_rising": rising_iso})
    hass.set_state("sensor.pv_today", "unavailable")   # makes pv_remaining None
    hass.set_state("sensor.pv_tomorrow", "6.0")
    ctrl.enabled = False

    result = await ctrl.tick()

    assert result["reason"] == "disabled"
    assert len(ctrl._recorder.rows) == 1         # sample still recorded
    assert ctrl.last_status.get("plan") is None  # no horizon when pv_remaining is None


# ---------------------------------------------------------------------------
# Solar charge surfacing and identity tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tick_ok_surfaces_solar_charge_kwh(monkeypatch):
    """tick() 'ok' path populates solar_charge_kwh > 0 when PV partially covers the gap.

    Time is frozen at BASE so the seeded next_setting (BASE+8h) stays in the future,
    giving build_pv_curve_from_arrays a valid 7-hour window.  With 5 kWh PV seeded, the
    half-sine curve peaks at ~1114 W >> 400 W fallback load, generating ~2.3 kWh of net
    battery charge.  With default soc_target=97 and soc=20 (required=7.7 kWh) this is
    partial coverage: 0 < solar_charge_kwh < required_kwh.
    The test would fail (solar_charge_kwh == 0.0) if the 'ok' path omitted the
    solar_charge=solar_charge kwarg to _status.

    Task 3: deficit removed from controller; solar_charge_kwh == required_kwh directly.
    """
    monkeypatch.setattr(controller.dt_util, "utcnow", lambda: BASE)
    hass = _StubHass()
    ctrl, _ = _make_controller(hass, data_overrides={
        const.CONF_ENT_PV_TODAY: ["sensor.pv_today"],
    })
    _seed_valid_inputs(hass, soc="20.0")
    hass.set_state("sensor.pv_today", "5.0")  # 5 kWh remaining → partial solar coverage

    result = await ctrl.tick()
    assert result["reason"] == "ok"

    soc = 20.0
    cfg = ctrl.cfg
    required_kwh = max(0.0, (cfg.soc_target - soc) / 100.0 * cfg.capacity_kwh)
    # Task 3: solar_charge = required_kwh (deficit subtraction removed).
    expected_solar_charge = round(required_kwh, 3)

    assert "solar_charge_kwh" in result, "solar_charge_kwh must be present in status"
    assert result["solar_charge_kwh"] > 0.0, (
        f"Expected solar_charge_kwh > 0 with 5 kWh PV seeded, got {result['solar_charge_kwh']}"
    )
    assert result["solar_charge_kwh"] == expected_solar_charge
    assert "deficit_kwh" not in result, "deficit_kwh must not appear in status after Task 3"


# ---------------------------------------------------------------------------
# A1 — Shadow grid-charge decision in disabled path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tick_disabled_shadow_records_sample(monkeypatch):
    """Disabled tick records a sample row with state='disabled' and setpoint=0.

    Clock is frozen at BASE so the seeded next_setting (BASE+8h) is in the
    future and the PV window is valid.  The actuator must never be engaged.
    Task 3: deficit_kwh removed from recorder rows entirely.
    """
    monkeypatch.setattr(controller.dt_util, "utcnow", lambda: BASE)
    hass = _StubHass()
    ctrl, act = _make_controller(hass, data_overrides={
        const.CONF_ENT_PV_TODAY: ["sensor.pv_today"],
    })
    _seed_valid_inputs(hass, soc="20.0")
    hass.set_state("sensor.pv_today", "1.0")  # small PV → real deficit remains
    ctrl.enabled = False

    result = await ctrl.tick()

    assert result["reason"] == "disabled"
    assert len(ctrl._recorder.rows) == 1
    row = ctrl._recorder.rows[0]
    assert row["state"] == "disabled"
    assert row["setpoint_w"] == 0.0
    # Task 3 (P80-survival-removal): deficit_kwh removed from recorder row.
    assert "deficit_kwh" not in row, f"deficit_kwh must not appear in recorder rows after Task 3"
    # Actuator must stay released — no FORCING in disabled mode
    assert not any(c[0] == "engage_and_charge" for c in act.calls)


@pytest.mark.asyncio
async def test_tick_enabled_populates_last_decision_and_behavior_unchanged(monkeypatch):
    """Enabled tick populates last_decision with active=True (FORCING) and does NOT
    change external behavior: actuation, status, and plan state are identical to before.

    Updated for water-value default: only 1 cheap slot (hour 0) so select_charge_slots
    unambiguously picks the current hour as the unique cheap slot, ensuring now_selected=True.
    With 2 cheap slots the tie-break in new mode preferred hour 1 (latest-first), leaving
    hour 0 unselected → PASSIVE.  horizon_mode is now "water-value" (new-mode default).
    """
    monkeypatch.setattr(controller.dt_util, "utcnow", lambda: BASE)
    hass = _StubHass()
    ctrl, act = _make_controller(hass, data_overrides={
        const.CONF_ENT_PV_TODAY: ["sensor.pv_today"],
    })
    _seed_valid_inputs(hass, soc="20.0")
    # Cheap now (hour 0 only) + expensive peak → ceiling > cheap → FORCING.
    # Single cheap slot ensures select_charge_slots picks BASE+0h (current hour),
    # so now_selected=True and decide_state enters FORCING.
    cheap_slots = [
        {"datetime": (BASE + timedelta(hours=i)).isoformat(),
         "electricity_price": int(0.05 * const.PRICE_SCALE)}
        for i in range(1)
    ]
    peak_slots = [
        {"datetime": (BASE + timedelta(hours=i)).isoformat(),
         "electricity_price": int(0.40 * const.PRICE_SCALE)}
        for i in range(1, 9)
    ]
    hass.set_state("sensor.price", "0.05", {"forecast": cheap_slots + peak_slots})
    hass.set_state("sensor.pv_today", "0.0")  # no PV → FORCING
    # Ensure dwell (default 15 min) has elapsed so decide_state can enter FORCING.
    ctrl.plan = PlanState(ControllerState.PASSIVE, BASE - timedelta(minutes=20), ())

    result = await ctrl.tick()

    # Enabled-path external behavior unchanged
    assert result["reason"] == "ok"
    assert ctrl.plan.state is ControllerState.FORCING
    assert any(c[0] == "engage_and_charge" for c in act.calls)

    # last_decision correctly populated
    ld = ctrl.last_decision
    assert ld, "last_decision must be populated after an enabled tick"
    assert ld["active"] is True
    assert ld["state"] == "forcing"
    # Task 3 (P80-survival-removal): deficit_kwh removed from last_decision entirely.
    assert "deficit_kwh" not in ld
    assert ld["setpoint_w"] < 0.0, "setpoint_w should be negative (charging)"
    assert isinstance(ld["committed_hours"], list)
    assert len(ld["committed_hours"]) > 0
    # Water-value mode reports "water-value" (not "single-day" as in legacy mode).
    assert ld["horizon_mode"] == "water-value"


# ---------------------------------------------------------------------------
# A3b — per-tick decision write
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tick_disabled_calls_append_decision(monkeypatch):
    """Disabled tick with valid inputs calls recorder.append_decision once."""
    monkeypatch.setattr(controller.dt_util, "utcnow", lambda: BASE)
    hass = _StubHass()
    ctrl, act = _make_controller(hass, data_overrides={
        const.CONF_ENT_PV_TODAY: ["sensor.pv_today"],
    })
    _seed_valid_inputs(hass, soc="20.0")
    hass.set_state("sensor.pv_today", "1.0")
    ctrl.enabled = False
    ctrl.plan = PlanState(ControllerState.PASSIVE, BASE - timedelta(minutes=20), ())

    await ctrl.tick()

    assert len(ctrl._recorder.decision_rows) == 1, (
        "Expected append_decision called once for disabled tick"
    )
    dr = ctrl._recorder.decision_rows[0]
    assert dr["ts"] == BASE.isoformat()
    assert dr["active"] is False
    assert dr["state"] == "disabled"


@pytest.mark.asyncio
async def test_tick_enabled_calls_append_decision(monkeypatch):
    """Enabled tick calls recorder.append_decision once.

    Updated for water-value default: single cheap slot at hour 0 ensures current
    hour is selected (no tie-break ambiguity) → FORCING → active=True in the
    decision row.
    """
    monkeypatch.setattr(controller.dt_util, "utcnow", lambda: BASE)
    hass = _StubHass()
    ctrl, act = _make_controller(hass, data_overrides={
        const.CONF_ENT_PV_TODAY: ["sensor.pv_today"],
    })
    _seed_valid_inputs(hass, soc="20.0")
    # Single cheap slot at hour 0 → unambiguous current-hour selection → FORCING.
    cheap_slots = [
        {"datetime": (BASE + timedelta(hours=i)).isoformat(),
         "electricity_price": int(0.05 * const.PRICE_SCALE)}
        for i in range(1)
    ]
    peak_slots = [
        {"datetime": (BASE + timedelta(hours=i)).isoformat(),
         "electricity_price": int(0.40 * const.PRICE_SCALE)}
        for i in range(1, 9)
    ]
    hass.set_state("sensor.price", "0.05", {"forecast": cheap_slots + peak_slots})
    hass.set_state("sensor.pv_today", "0.0")
    ctrl.plan = PlanState(ControllerState.PASSIVE, BASE - timedelta(minutes=20), ())

    await ctrl.tick()

    assert len(ctrl._recorder.decision_rows) == 1, (
        "Expected append_decision called once for enabled tick"
    )
    dr = ctrl._recorder.decision_rows[0]
    assert dr["active"] is True
    assert dr["ts"] == BASE.isoformat()


@pytest.mark.asyncio
async def test_tick_no_inputs_skips_append_decision():
    """When inputs are unavailable, tick must not call append_decision."""
    hass = _StubHass()
    ctrl, _ = _make_controller(hass)
    # SoC not seeded → inputs = None → last_decision stays empty
    ctrl.enabled = False

    await ctrl.tick()

    assert ctrl._recorder.decision_rows == [], (
        "append_decision must not be called when inputs are unavailable"
    )


def _seed_regret_samples(ctrl, day: str, *, soc_start: float,
                          load_w: float, batt_w: float, pv_w: float,
                          import_price: float, state: str = "passive"):
    """Seed 24 hourly samples (one per UTC hour) for the given local/UTC day."""
    for h in range(24):
        ctrl._recorder.rows.append({
            "ts": f"{day}T{h:02d}:00:00+00:00",
            "soc": soc_start if h == 0 else soc_start,
            "pv_w": pv_w,
            "batt_w": batt_w,
            "p1_w": load_w,
            "import_price": import_price,
            "state": state,
            "setpoint_w": batt_w if state == "forcing" else 0.0,
        })


@pytest.mark.asyncio
async def test_daily_regret_job_under_buy_scenario(monkeypatch):
    """Drain day (no deliberate charging, battery drains) → day is scored, under_buy_kwh=0.

    Under the economic-only redesign (A1) the regret oracle uses water_value terminal
    mode.  In water_value mode the optimal DP does NOT need to deliberately charge to
    hold the floor: floor-hit house load is served by direct grid imports at the spot
    rate (cheaper than pre-charging through eta_charge < 1).  So optimal_kwh (deliberate
    charges only) ≈ 0 for a uniform-price day.

    The realized path also has zero deliberate charging.  Forced floor-hit imports are
    included in realized_kwh but NOT in optimal_kwh, so over_buy_kwh > 0 (realized
    imported more total kWh) and under_buy_kwh = 0 (no deliberate-charge shortfall).

    Clock frozen at BASE (2026-06-20 11:00 UTC).  as_local patched to UTC so
    local date == UTC date — samples seeded as UTC hours of 2026-06-19.
    """
    yesterday = "2026-06-19"
    monkeypatch.setattr(controller.dt_util, "utcnow", lambda: BASE)
    # Make local == UTC in tests so date arithmetic is deterministic.
    monkeypatch.setattr(controller.dt_util, "as_local", lambda dt: dt)
    hass = _StubHass()
    ctrl, _ = _make_controller(hass)
    _seed_valid_inputs(hass, soc="60.0")
    ctrl._last_regret_day = yesterday

    # 24 samples: p1_w=500W load, batt_w=0 (not charging), pv=0, no forcing.
    # Battery starts at 50% and drains to floor (5%); forced floor-hit imports
    # then serve remaining load hours directly from grid.
    _seed_regret_samples(ctrl, yesterday, soc_start=50.0, load_w=500.0,
                         batt_w=0.0, pv_w=0.0, import_price=0.10)

    result = await ctrl.tick()

    row = ctrl._recorder.daily_regret_rows.get(yesterday)
    assert row is not None, "Expected daily_regret row for yesterday"
    assert row.get("infeasible", 0) == 0, "Should be feasible with default config"
    # Economic-only (A1): water_value oracle also chooses 0 deliberate charging at
    # uniform 0.10/kWh → under_buy_kwh=0 (no deliberate-charge shortfall).
    assert row["under_buy_kwh"] == 0.0, (
        f"Expected under_buy_kwh=0 (optimal also does not pre-charge at uniform price), "
        f"got {row['under_buy_kwh']}"
    )
    # last_status must expose regret_eur (from the daily job that ran this tick).
    assert "regret_eur" in result, "regret_eur must appear in tick() return value"
    assert result["regret_eur"] is not None, "regret_eur must not be None for feasible day"


@pytest.mark.asyncio
async def test_daily_regret_job_over_buy_scenario(monkeypatch):
    """Over-buy day (grid-charged when battery was already nearly full) → over_buy_kwh > 0."""
    yesterday = "2026-06-19"
    monkeypatch.setattr(controller.dt_util, "utcnow", lambda: BASE)
    monkeypatch.setattr(controller.dt_util, "as_local", lambda dt: dt)
    hass = _StubHass()
    ctrl, _ = _make_controller(hass)
    _seed_valid_inputs(hass, soc="60.0")
    ctrl._last_regret_day = yesterday

    # Hour 0: battery charging at 2 kW from grid (batt_w=-2000, p1_w=2000).
    # Hours 1-23: idle (batt_w=0, p1_w=0, no load).
    # Starting SoC=95% (9.5 kWh).  Optimal only needs 0.2 kWh DC to reach target.
    # Realized pays for 2.0 kWh AC → over_buy_kwh should be positive.
    ctrl._recorder.rows.append({
        "ts": f"{yesterday}T00:00:00+00:00",
        "soc": 95.0, "pv_w": 0.0, "batt_w": -2000.0, "p1_w": 2000.0,
        "import_price": 0.10, "state": "forcing", "setpoint_w": -2000.0,
    })
    for h in range(1, 24):
        ctrl._recorder.rows.append({
            "ts": f"{yesterday}T{h:02d}:00:00+00:00",
            "soc": 97.0, "pv_w": 0.0, "batt_w": 0.0, "p1_w": 0.0,
            "import_price": 0.10, "state": "passive", "setpoint_w": 0.0,
        })

    result = await ctrl.tick()

    row = ctrl._recorder.daily_regret_rows.get(yesterday)
    assert row is not None
    assert row.get("infeasible", 0) == 0
    assert isinstance(row["over_buy_kwh"], float)
    assert row["over_buy_kwh"] > 0.0, (
        f"Expected over_buy_kwh > 0 (charged 2 kWh when ~0.2 kWh needed), "
        f"got {row['over_buy_kwh']}"
    )
    assert "regret_eur" in result
    assert result["regret_eur"] is not None


@pytest.mark.asyncio
async def test_daily_regret_job_heavy_load_still_scored(monkeypatch):
    """Heavy-load day (battery instantly exhausted) → day IS scored, infeasible=0.

    Under the economic-only redesign (A1) the regret oracle uses water_value terminal
    mode.  In water_value mode infeasible is NEVER set True: even with max_charge_w=100W
    and 5 kW load the DP finds a feasible path (drain to firmware floor, import below
    floor directly at the spot rate).  The day is fully scored with real metrics.

    This replaces the old "infeasible day → null metrics" contract which only fired in
    reserve terminal mode when the DP could not reach the reserve target.
    """
    yesterday = "2026-06-19"
    monkeypatch.setattr(controller.dt_util, "utcnow", lambda: BASE)
    monkeypatch.setattr(controller.dt_util, "as_local", lambda dt: dt)
    hass = _StubHass()
    # max_charge_w=100W → 0.092 kWh DC/h max — far below the 5 kWh/h load.
    ctrl, _ = _make_controller(hass, data_overrides={const.CONF_MAX_CHARGE_W: 100.0})
    _seed_valid_inputs(hass, soc="60.0")
    ctrl._last_regret_day = yesterday

    # 24 samples: very heavy load (5 kW), no charging, soc_start=50%.
    # Battery drains to floor quickly; remaining load served by forced floor-hit
    # imports at the spot rate.
    _seed_regret_samples(ctrl, yesterday, soc_start=50.0, load_w=5000.0,
                         batt_w=0.0, pv_w=0.0, import_price=0.10)

    result = await ctrl.tick()

    row = ctrl._recorder.daily_regret_rows.get(yesterday)
    assert row is not None, "A row must be written for every scored day"
    # Economic-only (A1): water_value terminal mode → infeasible=0 always.
    assert row.get("infeasible", 0) == 0, (
        f"Expected infeasible=0 (water_value mode never sets infeasible), got {row.get('infeasible')}"
    )
    # Day is fully scored: real metrics populated (not NULL).
    assert row["regret_eur"] is not None, "regret_eur must be computed for heavy-load day"
    assert row["over_buy_kwh"] is not None
    assert row["under_buy_kwh"] is not None
    # last_status must expose a real regret_eur (not None).
    assert result.get("regret_eur") is not None, (
        f"regret_eur in last_status must not be None for scored day, got {result.get('regret_eur')}"
    )


@pytest.mark.asyncio
async def test_daily_regret_job_skips_sparse_day(monkeypatch):
    """A day with fewer than 12 hourly samples must be skipped (no row written)."""
    yesterday = "2026-06-19"
    monkeypatch.setattr(controller.dt_util, "utcnow", lambda: BASE)
    monkeypatch.setattr(controller.dt_util, "as_local", lambda dt: dt)
    hass = _StubHass()
    ctrl, _ = _make_controller(hass)
    _seed_valid_inputs(hass, soc="60.0")
    ctrl._last_regret_day = yesterday

    # Only 6 distinct hours of samples (< 12 threshold → skip).
    for h in range(6):
        ctrl._recorder.rows.append({
            "ts": f"{yesterday}T{h:02d}:00:00+00:00",
            "soc": 50.0, "pv_w": 0.0, "batt_w": 0.0, "p1_w": 500.0,
            "import_price": 0.10, "state": "passive", "setpoint_w": 0.0,
        })

    await ctrl.tick()

    assert ctrl._recorder.daily_regret_rows.get(yesterday) is None, (
        "Sparse day (< 12 h) must not produce a daily_regret row"
    )


@pytest.mark.asyncio
async def test_daily_regret_backfills_on_restart(monkeypatch):
    """First tick after restart (last_regret_day=None) backfills recent days.

    Simulates a fresh start: _last_regret_day is None, yesterday has 24h of
    samples.  The backfill should score yesterday even though there was no
    explicit midnight transition.
    """
    yesterday = "2026-06-19"
    monkeypatch.setattr(controller.dt_util, "utcnow", lambda: BASE)
    monkeypatch.setattr(controller.dt_util, "as_local", lambda dt: dt)
    hass = _StubHass()
    ctrl, _ = _make_controller(hass)
    _seed_valid_inputs(hass, soc="60.0")
    # Do NOT set _last_regret_day — leave it None to simulate restart.

    _seed_regret_samples(ctrl, yesterday, soc_start=50.0, load_w=500.0,
                         batt_w=0.0, pv_w=0.0, import_price=0.10)

    await ctrl.tick()

    row = ctrl._recorder.daily_regret_rows.get(yesterday)
    assert row is not None, (
        "Backfill must score yesterday on first tick after restart "
        "(even without an explicit midnight transition)"
    )
    assert row.get("infeasible", 0) == 0
    assert isinstance(row["regret_eur"], float)


@pytest.mark.asyncio
async def test_daily_regret_backfills_gap_from_latest_scored(monkeypatch):
    """Backfill scores every unscored day between the latest DB entry and yesterday.

    Scenario: latest_daily_regret.day = 2026-06-16 (3 days before yesterday 2026-06-19).
    Gap days 2026-06-17, 2026-06-18, 2026-06-19 all have 24h of samples.
    After one tick the three gap rows must all appear in daily_regret_rows.
    """
    monkeypatch.setattr(controller.dt_util, "utcnow", lambda: BASE)  # 2026-06-20
    monkeypatch.setattr(controller.dt_util, "as_local", lambda dt: dt)
    hass = _StubHass()
    ctrl, _ = _make_controller(hass)
    _seed_valid_inputs(hass, soc="60.0")

    # Pre-seed the DB with the last scored row (3 days before yesterday).
    ctrl._recorder.daily_regret_rows["2026-06-16"] = {
        "day": "2026-06-16", "regret_eur": 0.05, "over_buy_kwh": 0.0,
        "over_buy_eur": 0.0, "under_buy_kwh": 0.0, "cost_regret_eur": 0.0,
        "optimal_kwh": 1.0, "optimal_eur": 0.10, "realized_kwh": 1.0,
        "realized_eur": 0.15, "infeasible": 0,
        "computed_ts": "2026-06-17T00:01:00+00:00",
    }
    # _last_regret_day=None (startup) triggers backfill.

    # Seed 24 hourly samples for each gap day.
    for gap_day in ["2026-06-17", "2026-06-18", "2026-06-19"]:
        _seed_regret_samples(ctrl, gap_day, soc_start=50.0, load_w=500.0,
                             batt_w=0.0, pv_w=0.0, import_price=0.10)

    await ctrl.tick()

    for gap_day in ["2026-06-17", "2026-06-18", "2026-06-19"]:
        row = ctrl._recorder.daily_regret_rows.get(gap_day)
        assert row is not None, f"Backfill must score gap day {gap_day}"
        assert row.get("infeasible", 1) == 0, f"{gap_day}: expected feasible row"
        assert isinstance(row["regret_eur"], float), f"{gap_day}: regret_eur must be float"


# ---------------------------------------------------------------------------
# R2 — daily-regret path uses recorded load_w with derive fallback
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_daily_regret_uses_recorded_load_w_when_present(monkeypatch):
    """Regret load reconstruction uses load_w when present in sample rows.

    Scenario: samples have load_w=300 but p1_w=800, batt_w=0, pv_w=0.
    Derived load would be 800W; recorded load is 300W.
    Two parallel controllers: one seeded with load_w=300, one without load_w
    (p1_w=300 to match). Under-buy kWh must be equal — both compute 300W load.
    """
    yesterday = "2026-06-19"
    monkeypatch.setattr(controller.dt_util, "utcnow", lambda: BASE)
    monkeypatch.setattr(controller.dt_util, "as_local", lambda dt: dt)

    # Controller A: samples carry load_w=300, p1_w=800 (derive would give 800)
    hass_a = _StubHass()
    ctrl_a, _ = _make_controller(hass_a)
    _seed_valid_inputs(hass_a, soc="60.0")
    ctrl_a._last_regret_day = yesterday
    for h in range(24):
        ctrl_a._recorder.rows.append({
            "ts": f"{yesterday}T{h:02d}:00:00+00:00",
            "soc": 50.0, "pv_w": 0.0, "batt_w": 0.0,
            "p1_w": 800.0,   # derive would give 800
            "load_w": 300.0, # recorded value overrides derive
            "import_price": 0.10, "state": "passive", "setpoint_w": 0.0,
        })

    # Controller B: samples carry no load_w, p1_w=300 (derive gives 300)
    hass_b = _StubHass()
    ctrl_b, _ = _make_controller(hass_b)
    _seed_valid_inputs(hass_b, soc="60.0")
    ctrl_b._last_regret_day = yesterday
    for h in range(24):
        ctrl_b._recorder.rows.append({
            "ts": f"{yesterday}T{h:02d}:00:00+00:00",
            "soc": 50.0, "pv_w": 0.0, "batt_w": 0.0,
            "p1_w": 300.0,   # derive gives 300 (no load_w key → fallback)
            "import_price": 0.10, "state": "passive", "setpoint_w": 0.0,
        })

    await ctrl_a.tick()
    await ctrl_b.tick()

    row_a = ctrl_a._recorder.daily_regret_rows.get(yesterday)
    row_b = ctrl_b._recorder.daily_regret_rows.get(yesterday)

    assert row_a is not None, "Controller A must write a regret row"
    assert row_b is not None, "Controller B must write a regret row"

    # Both controllers should have computed the same load (300W), so their
    # under_buy_kwh must be equal. If load_w were ignored, ctrl_a would use 800W.
    assert row_a.get("infeasible", 0) == 0, "ctrl_a must be feasible"
    assert row_b.get("infeasible", 0) == 0, "ctrl_b must be feasible"
    assert row_a["under_buy_kwh"] == pytest.approx(row_b["under_buy_kwh"], abs=0.05), (
        f"load_w=300 with p1_w=800 must compute same regret as p1_w=300 with no load_w; "
        f"got ctrl_a under_buy={row_a['under_buy_kwh']}, ctrl_b under_buy={row_b['under_buy_kwh']}"
    )


@pytest.mark.asyncio
async def test_daily_regret_falls_back_to_derive_when_load_w_null(monkeypatch):
    """When sample has no load_w, regret falls back to p1+batt+pv derive (old behavior)."""
    yesterday = "2026-06-19"
    monkeypatch.setattr(controller.dt_util, "utcnow", lambda: BASE)
    monkeypatch.setattr(controller.dt_util, "as_local", lambda dt: dt)
    hass = _StubHass()
    ctrl, _ = _make_controller(hass)
    _seed_valid_inputs(hass, soc="60.0")
    ctrl._last_regret_day = yesterday

    # Seed with no load_w key at all (old-style rows).
    _seed_regret_samples(ctrl, yesterday, soc_start=50.0, load_w=500.0,
                         batt_w=0.0, pv_w=0.0, import_price=0.10)

    await ctrl.tick()

    row = ctrl._recorder.daily_regret_rows.get(yesterday)
    assert row is not None, "Must write a regret row with derived load"
    assert row.get("infeasible", 0) == 0


@pytest.mark.asyncio
async def test_purge_block_purges_decisions(monkeypatch):
    """6-hourly purge must call purge_decisions_older_than alongside sample purge."""
    purge_calls: list[str] = []

    class _PurgingRecorder(_StubRecorder):
        def purge_older_than(self, now_iso, retention_days):
            pass  # ignore sample purge

        def purge_decisions_older_than(self, cutoff_iso):
            purge_calls.append(cutoff_iso)
            return 0

    # Freeze clock at hour 0 (0 % 6 == 0) so purge fires on the tick.
    tick_time = BASE.replace(hour=0, minute=0)
    monkeypatch.setattr(controller.dt_util, "utcnow", lambda: tick_time)
    hass = _StubHass()
    rec = _PurgingRecorder()
    data = {
        const.CONF_ENT_SOC: "sensor.soc",
        const.CONF_ENT_PHASE: ["sensor.phase_l1", "sensor.phase_l2", "sensor.phase_l3"],
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
    ctrl = controller.Controller(hass=hass, data=data, recorder=rec,
                                 actuator=_StubActuator(), store=_StubStore())
    _seed_valid_inputs(hass, soc="60.0")

    await ctrl.tick()

    assert len(purge_calls) == 1, (
        f"purge_decisions_older_than must be called once in the 6-hourly block; "
        f"got {len(purge_calls)} calls"
    )



# ---------------------------------------------------------------------------
# P80 cushion vs P50 display — quantile-aware deficit (P4-T2)
# ---------------------------------------------------------------------------

class _StubHGBR(HGBRQuantileModel):
    """Stub HGBR that returns distinct load per quantile without needing a fitted model.

    Inherits from HGBRQuantileModel so that LoadPredictor.predict passes the
    quantile kwarg through — that isinstance() check is what we're exercising.
    """

    def __init__(self, load_by_quantile: dict) -> None:
        super().__init__()
        self._load_by_quantile = load_by_quantile

    def predict_load_w(self, when, temp, fallback_w, *, quantile=0.5):
        return self._load_by_quantile.get(quantile, fallback_w)


def test_compute_decision_display_uses_p50():
    """Task 3 (P80-survival-removal): deficit removed; display horizon still uses P50 loads.

    The deficit return slot was removed entirely in Task 3.  The display horizon
    must still reflect P50 loads (200 W), not the P80 value.
    """
    inputs = PlantInputs(soc=0.0, phase_import_w=(0.0, 0.0, 0.0), now=BASE)
    slots = _slots([0.05] * 10)
    sunset = BASE + timedelta(hours=8)
    plan = PlanState.initial(BASE - timedelta(hours=1))
    base_cfg = dict(capacity_kwh=10.0, soc_target=100.0, eta_charge=1.0, min_dwell_min=0)

    hgbr_p80 = _StubHGBR({0.5: 200.0, 0.8: 1500.0})
    _, _, _, horizon_p80, _, _ = controller.compute_decision(
        plan, inputs, slots, pv_remaining=5.0, sunset=sunset,
        predictor=LoadPredictor.from_model(hgbr_p80), cur_temp=None,
        cfg=Config(**base_cfg),
    )

    # Display horizon (build_plan_horizon receives P50 intervals) must carry P50 load (200 W).
    p80_run_loads = [e["load_w"] for e in horizon_p80 if e.get("load_w") is not None]
    assert p80_run_loads, "Expected at least one horizon entry with a load_w value"
    assert all(w == 200.0 for w in p80_run_loads), (
        f"Display horizon must use P50 load (200 W), got: {p80_run_loads}"
    )


@pytest.mark.asyncio
async def test_tick_disabled_not_engaged_does_not_release():
    """Disabled + actuator.engaged=False must NOT call release_to_self.

    This is the core correctness guarantee: when the controller is disabled
    and has NOT been actively charging, it must never write to the hardware
    so a user-set manual modbus mode is preserved between ticks.
    """
    hass = _StubHass()
    ctrl, act = _make_controller(hass)
    ctrl.enabled = False
    # Fresh actuator: engaged=False (default)
    assert act.engaged is False

    result = await ctrl.tick()

    assert result["reason"] == "disabled"
    assert not any(c[0] == "release_to_self" for c in act.calls), (
        "release_to_self must NOT be called when disabled and not engaged"
    )


@pytest.mark.asyncio
async def test_tick_disabled_engaged_releases_once_then_stops():
    """Disabled + actuator.engaged=True → release_to_self called exactly once.

    After the first tick releases (which sets engaged=False on the stub),
    a second tick must NOT call release_to_self again.
    """
    hass = _StubHass()
    ctrl, act = _make_controller(hass)
    ctrl.enabled = False
    # Simulate: we were actively forcing before being disabled
    act.engaged = True

    await ctrl.tick()

    release_calls = [c for c in act.calls if c[0] == "release_to_self"]
    assert len(release_calls) == 1, (
        f"Expected exactly 1 release_to_self on first disabled tick, got {len(release_calls)}"
    )
    # After release, engaged must be False (the stub's release_to_self sets it)
    assert act.engaged is False

    # Second tick: must NOT call release_to_self again
    act.calls.clear()
    await ctrl.tick()

    release_calls_2 = [c for c in act.calls if c[0] == "release_to_self"]
    assert len(release_calls_2) == 0, (
        f"Expected 0 release_to_self calls on second disabled tick, got {len(release_calls_2)}"
    )


# ---------------------------------------------------------------------------
# T5 — controller records load_w from sensor.power_usage each tick
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_record_sample_stores_load_w_from_house_load_entity():
    """When sensor.power_usage is available, appended row["load_w"] == its float value."""
    hass = _StubHass()
    ctrl, _ = _make_controller(
        hass,
        data_overrides={const.CONF_ENT_HOUSE_LOAD: "sensor.power_usage"},
    )
    _seed_valid_inputs(hass)
    hass.set_state("sensor.power_usage", "210.0")

    await ctrl.tick()

    rec = ctrl._recorder
    assert len(rec.rows) >= 1, "expected at least one appended row"
    last_row = rec.rows[-1]
    assert last_row["load_w"] == pytest.approx(210.0), (
        f"Expected load_w=210.0, got {last_row.get('load_w')}"
    )


@pytest.mark.asyncio
async def test_record_sample_stores_persons_home_count():
    """When person.* entities are configured, appended row["persons_home"] == count in 'home'."""
    hass = _StubHass()
    ctrl, _ = _make_controller(
        hass,
        data_overrides={const.CONF_PERSON_ENTITIES: ["person.alice", "person.bob"]},
    )
    _seed_valid_inputs(hass)
    hass.set_state("person.alice", "home")
    hass.set_state("person.bob", "home")

    await ctrl.tick()

    rec = ctrl._recorder
    assert len(rec.rows) >= 1, "expected at least one appended row"
    last_row = rec.rows[-1]
    assert last_row["persons_home"] == 2, (
        f"Expected persons_home=2, got {last_row.get('persons_home')}"
    )


@pytest.mark.asyncio
async def test_record_sample_stores_none_load_w_when_entity_unavailable():
    """When house-load entity is unavailable, load_w is None and tick does not crash."""
    hass = _StubHass()
    ctrl, _ = _make_controller(
        hass,
        data_overrides={const.CONF_ENT_HOUSE_LOAD: "sensor.power_usage"},
    )
    _seed_valid_inputs(hass)
    # Do NOT set sensor.power_usage → unavailable → None

    await ctrl.tick()

    rec = ctrl._recorder
    assert len(rec.rows) >= 1, "expected at least one appended row"
    last_row = rec.rows[-1]
    assert last_row["load_w"] is None, (
        f"Expected load_w=None when entity unavailable, got {last_row.get('load_w')}"
    )


@pytest.mark.asyncio
async def test_record_sample_uses_default_house_load_entity_when_conf_absent():
    """When CONF_ENT_HOUSE_LOAD is absent from config data, controller falls back to
    DEFAULT_ENT_HOUSE_LOAD ("sensor.power_usage") and records a non-null load_w.

    This is the regression test for the v6 upgrade bug: existing config entries that
    were created before ent_house_load was added have no key in their data dict, so
    self._data.get(CONF_ENT_HOUSE_LOAD) returns None, which suppresses the sensor read
    and leaves load_w NULL forever. The fix is to apply the default at read-time.
    """
    hass = _StubHass()
    # No CONF_ENT_HOUSE_LOAD in data_overrides — simulates a pre-v6 config entry
    ctrl, _ = _make_controller(hass)
    _seed_valid_inputs(hass)
    # The default entity (sensor.power_usage) is alive with a known value
    hass.set_state("sensor.power_usage", "350.0")

    await ctrl.tick()

    rec = ctrl._recorder
    assert len(rec.rows) >= 1, "expected at least one appended row"
    last_row = rec.rows[-1]
    assert last_row["load_w"] == pytest.approx(350.0), (
        f"Expected load_w=350.0 via DEFAULT_ENT_HOUSE_LOAD fallback, "
        f"got {last_row.get('load_w')}"
    )


@pytest.mark.asyncio
async def test_record_sample_stores_none_load_w_when_conf_absent_and_sensor_unavailable():
    """When CONF_ENT_HOUSE_LOAD is absent and sensor.power_usage is unavailable,
    load_w is still None — the default is applied but the sensor read returns None."""
    hass = _StubHass()
    # No CONF_ENT_HOUSE_LOAD in data_overrides → uses DEFAULT_ENT_HOUSE_LOAD
    ctrl, _ = _make_controller(hass)
    _seed_valid_inputs(hass)
    # Do NOT seed sensor.power_usage → unavailable → read_float returns None

    await ctrl.tick()

    rec = ctrl._recorder
    assert len(rec.rows) >= 1, "expected at least one appended row"
    last_row = rec.rows[-1]
    assert last_row["load_w"] is None, (
        f"Expected load_w=None when default entity is unavailable, "
        f"got {last_row.get('load_w')}"
    )


@pytest.mark.asyncio
async def test_record_sample_stores_none_load_w_when_conf_empty_string():
    """When CONF_ENT_HOUSE_LOAD is an empty string, load_w is None (treated as unset)."""
    hass = _StubHass()
    ctrl, _ = _make_controller(
        hass,
        data_overrides={const.CONF_ENT_HOUSE_LOAD: ""},  # empty string → treat as absent
    )
    _seed_valid_inputs(hass)

    await ctrl.tick()

    rec = ctrl._recorder
    assert len(rec.rows) >= 1, "expected at least one appended row"
    last_row = rec.rows[-1]
    assert last_row["load_w"] is None, (
        f"Expected load_w=None when conf is empty string, got {last_row.get('load_w')}"
    )


# ---------------------------------------------------------------------------
# R5 — profile fallback: load_w-null rows build profile via derive fallback
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_profile_built_from_derive_fallback_when_load_w_null(tmp_path):
    """Recorder rows with load_w=NULL but valid p1_w/batt_w/pv_w build a non-empty
    load profile via the derive fallback (not the flat 400W DEFAULT_FALLBACK_LOAD_W).

    Uses the real DataRecorder (not _StubRecorder) to exercise the full path.
    """
    from custom_components.anker_x1_smartgrid import forecast as forecast_mod
    from custom_components.anker_x1_smartgrid.recorder import DataRecorder

    hass = _StubHass()
    real_rec = DataRecorder(str(tmp_path / "test.db"))

    # Use a recent date (2 days ago) so rows stay inside the 14-day lookback.
    recent = (datetime.now(timezone.utc) - timedelta(days=2)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )

    # Seed rows; no load_w.
    # p1_w=600, batt_w=100, pv_w=50 → derived load = 750W (not 400W).
    for i in range(5):
        ts = (recent + timedelta(minutes=i * 10)).isoformat()
        real_rec.append({
            "ts": ts,
            "p1_w": 600.0, "batt_w": 100.0, "pv_w": 50.0,
            "load_w": None,  # explicit NULL → derive fallback must be used
        })

    ctrl, _ = _make_controller(hass)
    ctrl._recorder = real_rec  # swap in real recorder

    await ctrl.refresh_profile()
    real_rec.close()

    assert ctrl.profile, "Profile must be non-empty when p1_w rows exist (via derive fallback)"

    lookup_dt = recent  # same day-type + hour as seeded rows
    from custom_components.anker_x1_smartgrid import const as _const
    learned = forecast_mod.predict_load_w(
        ctrl.profile, lookup_dt, fallback_w=_const.DEFAULT_FALLBACK_LOAD_W
    )
    assert learned != _const.DEFAULT_FALLBACK_LOAD_W, (
        "Expected derived load value, not the 400W fallback — derive path must have built the profile"
    )
    assert abs(learned - 750.0) < 1.0, (
        f"Expected ~750W (600+100+50), got {learned}"
    )


@pytest.mark.asyncio
async def test_profile_empty_when_both_load_w_and_p1_null_returns_fallback(tmp_path):
    """Recorder rows with both load_w=NULL and p1_w=NULL → no valid profile samples →
    predictor returns DEFAULT_FALLBACK_LOAD_W without crashing.
    """
    from custom_components.anker_x1_smartgrid import forecast as forecast_mod
    from custom_components.anker_x1_smartgrid.recorder import DataRecorder

    hass = _StubHass()
    real_rec = DataRecorder(str(tmp_path / "test.db"))

    # Use a recent date so rows reach the SQL query (inside lookback window).
    recent = (datetime.now(timezone.utc) - timedelta(days=2)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    # Rows with no load value at all — neither load_w nor p1_w.
    for i in range(3):
        ts = (recent + timedelta(minutes=i * 10)).isoformat()
        real_rec.append({
            "ts": ts,
            "batt_w": 100.0, "pv_w": 50.0,
            # p1_w=None (omitted), load_w=None (omitted) → no valid house load
        })

    ctrl, _ = _make_controller(hass)
    ctrl._recorder = real_rec  # swap in real recorder

    await ctrl.refresh_profile()

    # Profile must be empty (no valid samples).
    assert ctrl.profile == {}, (
        f"Expected empty profile when all rows have null load_w and p1_w, got {ctrl.profile}"
    )

    # Tick must complete gracefully with an empty profile; predictor returns fallback.
    _seed_valid_inputs(hass)
    result = await ctrl.tick()
    real_rec.close()  # close AFTER tick() so rollup_hours can still access DB
    assert result is not None, "tick() must not crash with an empty profile"


# ---------------------------------------------------------------------------
# T4 — Controller._resolve_slot_minutes (per-refresh detection + UTC-day latch)
# ---------------------------------------------------------------------------

def test_resolve_slot_minutes_explicit_override_bypasses_latch(monkeypatch):
    """An explicit slot_resolution override hard-pins the value on every call
    and never touches the latch, even when the slots would auto-detect finer."""
    hass = _StubHass()
    ctrl, _ = _make_controller(hass, data_overrides={"slot_resolution": "30"})
    now = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(controller.dt_util, "utcnow", lambda: now)

    # Hourly-spaced slots would auto-detect 60, but the override always wins.
    hourly_slots = [PriceSlot(now + timedelta(hours=i), 0.20) for i in range(4)]

    assert ctrl._resolve_slot_minutes(hourly_slots) == 30
    assert ctrl._res_latch is None  # override path never latches
    assert ctrl._resolve_slot_minutes(hourly_slots) == 30
    assert ctrl._res_latch is None
    assert ctrl._detected_slot_minutes == 30


def test_resolve_slot_minutes_auto_latches_finest_seen_today(monkeypatch):
    """'auto' latches to the finest slot length seen so far this UTC day and
    does not un-latch when a later read the same day is coarser."""
    hass = _StubHass()
    ctrl, _ = _make_controller(hass, data_overrides={"slot_resolution": const.SLOT_RESOLUTION_AUTO})
    day1 = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(controller.dt_util, "utcnow", lambda: day1)

    quarter_slots = [PriceSlot(day1 + timedelta(minutes=15 * i), 0.20) for i in range(4)]
    assert ctrl._resolve_slot_minutes(quarter_slots) == 15
    assert ctrl._detected_slot_minutes == 15

    # Same day, coarser (hourly) slots — the latch must stay at the finest (15).
    hourly_slots = [PriceSlot(day1 + timedelta(hours=i), 0.20) for i in range(4)]
    assert ctrl._resolve_slot_minutes(hourly_slots) == 15
    assert ctrl._detected_slot_minutes == 15


def test_resolve_slot_minutes_day_rollover_resets_latch(monkeypatch):
    """A UTC-day rollover resets the latch so the new day's own (coarser)
    detection is honoured instead of sticking to yesterday's finest."""
    hass = _StubHass()
    ctrl, _ = _make_controller(hass, data_overrides={"slot_resolution": const.SLOT_RESOLUTION_AUTO})
    day1 = datetime(2026, 8, 1, 23, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(controller.dt_util, "utcnow", lambda: day1)
    quarter_slots = [PriceSlot(day1 + timedelta(minutes=15 * i), 0.20) for i in range(4)]
    assert ctrl._resolve_slot_minutes(quarter_slots) == 15

    day2 = datetime(2026, 8, 2, 0, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(controller.dt_util, "utcnow", lambda: day2)
    hourly_slots = [PriceSlot(day2 + timedelta(hours=i), 0.20) for i in range(4)]
    assert ctrl._resolve_slot_minutes(hourly_slots) == 60


# ---------------------------------------------------------------------------
# T16 — measured efficiency curve: build + cache + planner gate
# ---------------------------------------------------------------------------

def test_controller_builds_efficiency_curve_and_gates_off_by_default():
    """_refresh_efficiency_curve always builds a curve; _planner_curve only
    surfaces it to the DP/reserve when cfg.use_measured_eta is on (default OFF,
    the byte-identical parity path)."""
    from dataclasses import replace
    from custom_components.anker_x1_smartgrid.efficiency import EfficiencyCurve

    hass = _StubHass()
    ctrl, _ = _make_controller(hass)

    ctrl._refresh_efficiency_curve(BASE)
    assert ctrl._eta_curve is not None
    assert isinstance(ctrl._eta_curve, EfficiencyCurve)
    assert ctrl._eta_curve_built_at == BASE
    assert ctrl._planner_curve() is None  # flag OFF by default

    ctrl.cfg = replace(ctrl.cfg, use_measured_eta=True)
    assert ctrl._planner_curve() is ctrl._eta_curve


def test_refresh_efficiency_curve_is_cached_within_window():
    """A second refresh within EFFICIENCY_CACHE_SECONDS is a no-op (same object,
    ``_eta_curve_built_at`` unchanged) — avoids re-querying the recorder every tick."""
    hass = _StubHass()
    ctrl, _ = _make_controller(hass)

    ctrl._refresh_efficiency_curve(BASE)
    curve_after_first = ctrl._eta_curve
    built_at_first = ctrl._eta_curve_built_at

    soon = BASE + timedelta(seconds=const.EFFICIENCY_CACHE_SECONDS - 1)
    ctrl._refresh_efficiency_curve(soon)
    assert ctrl._eta_curve is curve_after_first
    assert ctrl._eta_curve_built_at == built_at_first

    later = BASE + timedelta(seconds=const.EFFICIENCY_CACHE_SECONDS + 1)
    ctrl._refresh_efficiency_curve(later)
    assert ctrl._eta_curve_built_at == later


def test_refresh_efficiency_curve_falls_back_to_static_on_recorder_error():
    """A recorder read failure must never raise into the tick — falls back to
    the static scalar curve instead."""
    hass = _StubHass()
    ctrl, _ = _make_controller(hass)

    def _boom(since_iso=None):
        raise RuntimeError("recorder unavailable")

    ctrl._recorder.read_efficiency_samples = _boom
    ctrl._refresh_efficiency_curve(BASE)
    assert ctrl._eta_curve is not None
    assert ctrl._eta_curve_built_at == BASE


# ---------------------------------------------------------------------------
# T17 — measured efficiency curve threaded into the live executor/soc_drift/PnL
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_executor_runs_with_measured_eta_on(monkeypatch):
    """Full tick with use_measured_eta=True (and export enabled) must complete
    without raising, exercising the eta_curve threaded into
    ride_out_reserve_kwh/export_net_target_w (C3 executor) and the PnL/net-out
    eta_discharge call sites.

    Also a genuine (not merely smoke) regression gate: it spies on
    energy.ride_out_reserve_kwh to prove the LIVE executor's reserve call
    actually receives the measured curve — before T17's wiring, that call
    site never passed eta_curve, silently ignoring the measured curve even
    with the flag ON."""
    from dataclasses import replace
    from custom_components.anker_x1_smartgrid.efficiency import EfficiencyCurve

    hass = _StubHass()
    ctrl, act = _make_controller(
        hass, data_overrides={const.CONF_ENT_EXPORT_PRICE: "sensor.export_price"}
    )
    ctrl.cfg = replace(ctrl.cfg, use_measured_eta=True, enable_export=True)
    curve = EfficiencyCurve.static(ctrl.cfg)
    ctrl._eta_curve = curve
    ctrl._eta_curve_built_at = BASE
    # Pin "now" to BASE so _refresh_efficiency_curve's cache window sees the
    # curve as fresh and does not rebuild/replace it mid-tick (this test
    # asserts on the exact preset curve's identity reaching the executor).
    monkeypatch.setattr(controller.dt_util, "utcnow", lambda: BASE)

    _seed_valid_inputs(hass, soc="80.0")
    hass.set_state("sensor.export_price", "0.30")

    seen_reserve_kwargs = []
    _orig_reserve = controller.energy.ride_out_reserve_kwh

    def _spy_reserve(*args, **kwargs):
        seen_reserve_kwargs.append(kwargs)
        return _orig_reserve(*args, **kwargs)

    monkeypatch.setattr(controller.energy, "ride_out_reserve_kwh", _spy_reserve)

    result = await ctrl.tick()

    assert result["reason"] == "ok"
    assert isinstance(ctrl.today_export_pnl_eur, float)
    # The C3 live executor's reserve call must receive the measured curve object,
    # not the parity-path None (this is the call site T17 wires up).
    assert seen_reserve_kwargs, "C3 export executor did not call ride_out_reserve_kwh"
    assert any(kw.get("eta_curve") is curve for kw in seen_reserve_kwargs)


# ---------------------------------------------------------------------------
# T18 — measured efficiency curve: bin table exposed via last_status
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_status_exposes_efficiency_curve_table():
    """last_status must expose the efficiency curve bin table + the
    use_measured_eta flag so the sensor layer can surface them as attributes
    for observability (flag stays OFF by default — parity-safe)."""
    hass = _StubHass()
    ctrl, act = _make_controller(hass)
    _seed_valid_inputs(hass, soc="50.0")

    await ctrl.tick()

    status = ctrl.last_status
    assert "efficiency_curve" in status
    assert len(status["efficiency_curve"]["charge"]) == 6
    assert len(status["efficiency_curve"]["discharge"]) == 6
    assert status["efficiency_curve"]["any_over_unity"] is False
    assert status["use_measured_eta"] is False
