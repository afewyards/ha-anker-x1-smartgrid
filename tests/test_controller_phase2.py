import pytest
from datetime import datetime, timezone, timedelta, UTC
from custom_components.anker_x1_smartgrid.models import Config, PlanState, PlantInputs, PriceSlot, ControllerState
from custom_components.anker_x1_smartgrid import controller, forecast, const
from tests.helpers import StubActuator, StubHass as _T6Hass, StubRecorder, StubStore


# ---------------------------------------------------------------------------
# P1-T6: shared infrastructure for hourly-rollup tick() tests
# ---------------------------------------------------------------------------


class _RollupRecorder:
    """Tracks rollup_hours and purge_hourly_older_than calls; stubs all other recorder API."""

    def __init__(self):
        self.rollup_calls: list = []  # now_iso args received
        self.purge_hourly_calls: list = []  # cutoff_iso args received

    # --- required stubs (called by tick / regret / retrain paths) ---
    def append(self, row):
        pass

    def append_decision(self, **kwargs):
        pass

    def purge_older_than(self, ts, days):
        pass

    def purge_decisions_older_than(self, cutoff):
        pass

    def read_load_samples(self, since_iso=None):
        return []

    def read_feature_rows(self, since_iso=None):
        return []

    def upsert_daily_regret(self, **kwargs):
        pass

    def read_daily_regret_range(self, *a, **kw):
        return []

    def read_latest_daily_regret(self):
        return None

    def read_decisions(self, *a, **kw):
        return []

    # --- methods under test ---
    def rollup_hours(self, now_iso: str) -> int:
        self.rollup_calls.append(now_iso)
        return 0

    def purge_hourly_older_than(self, cutoff_iso: str) -> int:
        self.purge_hourly_calls.append(cutoff_iso)
        return 0


# _T6Hass = helpers.StubHass (imported above): async_add_executor_job calls
# the callable directly (no thread pool) so the tracker on _RollupRecorder
# picks up the calls synchronously in tests.

# The test hour must NOT be a multiple of 6 to avoid the 6-hourly raw-purge
# guard also firing and potentially masking issues.  Hour 11 is clean.
_T6_NOW = datetime(2026, 6, 20, 11, 0, tzinfo=UTC)


def _make_t6_controller(hass, rec):
    """Build a Controller wired for P1-T6 rollup tests (disabled state, minimal data)."""
    from custom_components.anker_x1_smartgrid.controller import Controller

    data = {
        const.CONF_ENT_SOC: "sensor.soc",
        const.CONF_ENT_METER_POWER: "sensor.meter",
        const.CONF_ENT_PRICE: "sensor.price",
        const.CONF_ENT_PV_TODAY: [],
        const.CONF_ENT_PV_TOMORROW: [],
        const.CONF_ENT_SUN: "sun.sun",
        const.CONF_ENT_BATTERY_POWER: "sensor.batt",
        const.CONF_ENT_PV_POWER: "sensor.pv",
        const.CONF_ENT_IRRADIANCE: "sensor.irr",
    }

    ctrl = Controller(
        hass=hass,
        data=data,
        recorder=rec,
        actuator=StubActuator(),
        store=StubStore(),
    )
    # Use disabled path so we avoid needing full coordinator state seeding;
    # the rollup guard runs before the enabled/disabled branch.
    ctrl.enabled = False
    return ctrl


BASE = datetime(2026, 6, 20, 11, 0, tzinfo=UTC)


def _slots(prices):
    return [PriceSlot(BASE + timedelta(hours=i), p) for i, p in enumerate(prices)]


def test_compute_decision_accepts_predictor():
    cfg = Config(capacity_kwh=10.0, soc_target=100.0, eta_charge=1.0, min_dwell_min=0)
    inputs = PlantInputs(soc=20.0, meter_w=0.0, now=BASE)
    slots = _slots([0.05] + [0.40] * 8)
    sunset = BASE + timedelta(hours=8)
    predictor = forecast.LoadPredictor.from_profile({})
    plan = PlanState.initial(BASE - timedelta(hours=1))
    new_plan, setpoint, _, _horizon, _, _ = controller.compute_decision(
        plan,
        inputs,
        slots,
        pv_remaining=0.0,
        sunset=sunset,
        predictor=predictor,
        cur_temp=None,
        cfg=cfg,
    )
    assert new_plan.state is ControllerState.FORCING


def test_compute_decision_propagates_cur_temp_to_predictor():
    """Regression: cur_temp must reach predictor.predict() — not silently dropped.

    Under the old code (temp_by_start built from a different pv_curve in tick()),
    keys never aligned, so temps were always None at the predictor.  This test
    verifies the fix: compute_decision builds temp_by_start from its own pv_curve,
    guaranteeing all keys match and cur_temp propagates through.
    """

    class _SpyPredictor:
        def __init__(self):
            self.temps_seen = []

        def predict(self, when, temp, fallback_w, *, quantile=0.5):
            self.temps_seen.append(temp)
            return fallback_w

    cfg = Config(capacity_kwh=10.0, soc_target=100.0, eta_charge=1.0, min_dwell_min=0)
    inputs = PlantInputs(soc=20.0, meter_w=0.0, now=BASE)
    slots = _slots([0.05] + [0.40] * 8)
    sunset = BASE + timedelta(hours=8)
    spy = _SpyPredictor()
    plan = PlanState.initial(BASE - timedelta(hours=1))
    # pv_remaining>0 so synth_pv_curve produces a non-empty curve → predictor.predict IS called.
    controller.compute_decision(
        plan,
        inputs,
        slots,
        pv_remaining=5.0,
        sunset=sunset,
        predictor=spy,
        cur_temp=4.0,
        cfg=cfg,
    )
    assert spy.temps_seen, "predictor.predict was never called (pv_curve was empty?)"
    assert all(t == 4.0 for t in spy.temps_seen), f"Expected all temps to be 4.0 (not None), got {spy.temps_seen}"


# _Rec kept local (not migrated to helpers.StubRecorder): constructor-seeded
# with fixed feature/hourly rows and ignores since_iso entirely — a different
# contract than StubRecorder's accumulate-then-filter-by-since_iso semantics,
# needed here to pin exact retrain-tier selection.
class _Rec:
    def __init__(self, rows):
        self._rows = rows

    def read_feature_rows(self, since_iso=None):
        return self._rows

    def read_hourly_rows(self, since_iso=None):
        # Tier 2 (bucketed) now trains on hourly rollups: >=48 rows clears
        # DEFAULT_MIN_TRAIN_HOURS while staying far under HGBR's ~28-day
        # coverage gate, so is_ready() still returns False naturally and the
        # bucketed tier is the one that engages below.
        return _cold_warm_hourly_rows()

    def append(self, row):
        pass

    def purge_older_than(self, *a):
        return 0


def _cold_warm_rows(n_per=1500):
    rows = []
    base = datetime(2026, 6, 1, 8, tzinfo=UTC)
    for d in range(20):
        for _ in range(n_per // 20 + 1):
            ts = base + timedelta(days=d)
            cold = d % 2 == 0
            rows.append(
                {
                    "ts": ts.isoformat(),
                    "p1_w": 1000.0 if cold else 300.0,
                    "batt_w": 0.0,
                    "pv_w": 0.0,
                    "temp": 2.0 if cold else 18.0,
                }
            )
    return rows


def _cold_warm_hourly_rows(days=20):
    """Hourly-rollup equivalent of _cold_warm_rows: one row per hour per day,
    alternating cold/warm by day (mirrors the per-tick fixture's intent so the
    bucketed model still sees two distinct per-hour/temp cells). 20 days = 480
    hourly rows: clears DEFAULT_MIN_TRAIN_HOURS (48) with room to spare, but
    only ~13 lag-complete dates (20 - 7) — well under HGBR's is_ready()
    min_days=21 default, so the HGBR coverage gate still fails naturally.
    """
    rows = []
    base = datetime(2026, 6, 1, tzinfo=UTC)
    for d in range(days):
        cold = d % 2 == 0
        for h in range(24):
            ts = base + timedelta(days=d, hours=h)
            rows.append(
                {
                    "hour_ts": ts.isoformat(),
                    "house_load_kwh_sum": 1.0 if cold else 0.3,
                    "temp_mean": 2.0 if cold else 18.0,
                }
            )
    return rows


async def test_retrain_switches_to_model_with_enough_data():
    from custom_components.anker_x1_smartgrid.controller import Controller

    data = {"use_learned_model": True, "min_train_samples": 100, "train_days": 14, "backtest_test_days": 3}
    ctl = Controller.__new__(Controller)
    # minimal manual init for the unit under test
    ctl._hass = None
    ctl._data = data
    ctl._recorder = _Rec(_cold_warm_rows())
    ctl.cfg = Config.from_dict(data)
    ctl.profile = {}
    ctl.predictor = forecast.LoadPredictor.from_profile({})
    ctl._profile_predictor = forecast.LoadPredictor.from_profile({})
    ctl.backtest_result = None
    await ctl.retrain()
    assert ctl.backtest_result is not None
    assert ctl.predictor._model is not None


# ---------------------------------------------------------------------------
# P1-T2: _record_sample writes 4 weather-forecast columns
# ---------------------------------------------------------------------------

# _MinimalHass = a fresh, unseeded helpers.StubHass(): its ``_states`` dict
# starts empty, so states.get(entity_id) returns None for everything —
# identical behaviour to the old bespoke stub.
# _CaptureRecorder = helpers.StubRecorder(): append()/`.rows` surface matches
# exactly (only .rows is ever inspected below).


def _make_recording_controller():
    """Build a Controller instance with just enough wiring for _record_sample."""
    from custom_components.anker_x1_smartgrid.controller import Controller

    ctl = Controller.__new__(Controller)
    ctl._hass = _T6Hass()
    ctl._data = {
        const.CONF_ENT_PV_POWER: "sensor.pv",
        const.CONF_ENT_BATTERY_POWER: "sensor.batt",
        const.CONF_ENT_PRICE: "sensor.price",
        const.CONF_ENT_IRRADIANCE: "sensor.irr",
        # No CONF_ENT_TEMP → temp=None (tests the optional-entity path too)
    }
    ctl._recorder = StubRecorder()
    # __new__ bypasses __init__: seed the N2 house-load cache __init__ would set,
    # since _compute_house_load_w falls back to it when pv/batt read None
    # (this fixture's unseeded StubHass always returns None for every state).
    ctl._last_house_load_w = 0.0
    return ctl


async def test_record_sample_with_weather_entry_persists_four_columns():
    """_record_sample with a matching forecast entry stores all 4 weather columns."""
    ctl = _make_recording_controller()
    now = datetime(2026, 6, 22, 10, 30, tzinfo=UTC)
    inputs = PlantInputs(soc=50.0, meter_w=0.0, now=now)
    entry = {
        "datetime": datetime(2026, 6, 22, 10, 0, tzinfo=UTC),
        "temp_forecast": 19.5,
        "cloud_cover": 25.0,
        "humidity": 70.0,
        "wind_speed": 3.1,
    }

    await ctl._record_sample(now, inputs, setpoint=0.0, state="passive", weather_entry=entry)

    assert len(ctl._recorder.rows) == 1
    row = ctl._recorder.rows[0]
    assert row["temp_forecast"] == pytest.approx(19.5)
    assert row["cloud_cover"] == pytest.approx(25.0)
    assert row["humidity"] == pytest.approx(70.0)
    assert row["wind_speed"] == pytest.approx(3.1)


async def test_record_sample_with_no_weather_entry_stores_none_for_all_four():
    """_record_sample with weather_entry=None (empty forecast) stores None for all 4 columns."""
    ctl = _make_recording_controller()
    now = datetime(2026, 6, 22, 10, 30, tzinfo=UTC)
    inputs = PlantInputs(soc=50.0, meter_w=0.0, now=now)

    await ctl._record_sample(now, inputs, setpoint=0.0, state="passive", weather_entry=None)

    assert len(ctl._recorder.rows) == 1
    row = ctl._recorder.rows[0]
    assert row["temp_forecast"] is None
    assert row["cloud_cover"] is None
    assert row["humidity"] is None
    assert row["wind_speed"] is None


# ---------------------------------------------------------------------------
# P1-T6: tick() wires hourly rollup + purge into controller maintenance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rollup_fires_once_then_not_again_in_same_hour(monkeypatch):
    """rollup_hours fires on the first tick for a given clock-hour; repeats within the
    same hour do NOT trigger another rollup (idempotent hour-guard)."""
    rec = _RollupRecorder()
    hass = _T6Hass()
    ctrl = _make_t6_controller(hass, rec)
    monkeypatch.setattr(controller.dt_util, "utcnow", lambda: _T6_NOW)

    # First tick: clock-hour 11, previously unseen (-1) → rollup fires.
    await ctrl.tick()
    assert len(rec.rollup_calls) == 1, "rollup must fire once on first hour advance"
    assert len(rec.purge_hourly_calls) == 1, "purge_hourly must fire alongside rollup"

    # Two more ticks within the same hour → no additional rollup calls.
    await ctrl.tick()
    await ctrl.tick()
    assert len(rec.rollup_calls) == 1, "rollup must NOT fire again within the same hour"
    assert len(rec.purge_hourly_calls) == 1, "purge_hourly must NOT fire again within same hour"


@pytest.mark.asyncio
async def test_rollup_fires_again_on_next_hour_advance(monkeypatch):
    """After the hour-guard fires once, advancing to the next clock-hour triggers a second rollup."""
    rec = _RollupRecorder()
    hass = _T6Hass()
    ctrl = _make_t6_controller(hass, rec)

    # Tick in hour 11.
    monkeypatch.setattr(controller.dt_util, "utcnow", lambda: _T6_NOW)
    await ctrl.tick()
    assert len(rec.rollup_calls) == 1

    # Advance to hour 12 → guard sees a new hour → rollup fires again.
    now_h12 = _T6_NOW.replace(hour=12)
    monkeypatch.setattr(controller.dt_util, "utcnow", lambda: now_h12)
    await ctrl.tick()
    assert len(rec.rollup_calls) == 2, "rollup must fire again when clock-hour advances"
    assert len(rec.purge_hourly_calls) == 2


@pytest.mark.asyncio
async def test_rollup_purge_cutoff_equals_now_minus_retention_hourly_days(monkeypatch):
    """purge_hourly_older_than receives cutoff = now − Config.retention_hourly_days."""
    rec = _RollupRecorder()
    hass = _T6Hass()
    ctrl = _make_t6_controller(hass, rec)
    monkeypatch.setattr(controller.dt_util, "utcnow", lambda: _T6_NOW)

    await ctrl.tick()

    expected_cutoff = (_T6_NOW - timedelta(days=const.DEFAULT_RETENTION_HOURLY_DAYS)).isoformat()
    assert rec.purge_hourly_calls[0] == expected_cutoff


@pytest.mark.asyncio
async def test_rollup_runs_via_executor_path(monkeypatch):
    """rollup_hours and purge_hourly_older_than are dispatched through async_add_executor_job.

    In tests, async_add_executor_job calls the callable inline; we verify the calls reach
    the recorder (proving they went through the executor path, not a direct call bypassing it).
    """
    executor_calls: list = []

    class _TrackingHass(_T6Hass):
        async def async_add_executor_job(self, fn, *args):
            executor_calls.append((fn.__name__, args))
            return fn(*args)

    rec = _RollupRecorder()
    hass = _TrackingHass()
    ctrl = _make_t6_controller(hass, rec)
    monkeypatch.setattr(controller.dt_util, "utcnow", lambda: _T6_NOW)

    await ctrl.tick()

    # At least one executor call must be the rollup sync method.
    rollup_dispatches = [name for name, _ in executor_calls if "rollup" in name]
    assert rollup_dispatches, (
        f"_rollup_hourly_sync was not dispatched via async_add_executor_job; executor calls: {executor_calls}"
    )
    # The recorder must have registered both calls (proving the executor ran them).
    assert len(rec.rollup_calls) == 1
    assert len(rec.purge_hourly_calls) == 1


# ---------------------------------------------------------------------------
# P3-T3: HGBR → bucketed → profile retrain chain
# ---------------------------------------------------------------------------


# _HGBRRec kept local (not migrated to helpers.StubRecorder): same reason as
# _Rec above — constructor-seeded fixed rows, since_iso ignored, needed to
# pin exact HGBR/bucketed/profile tier selection per test.
class _HGBRRec:
    """Recorder stub for HGBR chain tests; returns configurable feature + hourly rows."""

    def __init__(self, feature_rows=None, hourly_rows=None):
        self._feature_rows = feature_rows or []
        self._hourly_rows = hourly_rows or []

    def read_feature_rows(self, since_iso=None):
        return self._feature_rows

    def read_hourly_rows(self, since_iso=None):
        return self._hourly_rows

    def append(self, row):
        pass

    def purge_older_than(self, *a):
        return 0


def _make_retrain_ctl(rec, *, use_learned_model=True, min_train_samples=100):
    """Build a minimal Controller wired only for _retrain_sync tests."""
    from custom_components.anker_x1_smartgrid.controller import Controller

    data = {
        "use_learned_model": use_learned_model,
        "min_train_samples": min_train_samples,
        "train_days": 14,
        "backtest_test_days": 3,
    }
    ctl = Controller.__new__(Controller)
    ctl._hass = None
    ctl._data = data
    ctl._recorder = rec
    ctl.cfg = Config.from_dict(data)
    ctl.profile = {}
    ctl.predictor = forecast.LoadPredictor.from_profile({})
    ctl._profile_predictor = forecast.LoadPredictor.from_profile({})
    ctl.backtest_result = None
    ctl.active_model_name = "profile"
    return ctl


def _fake_promote_metrics():
    """A metrics dict that ``should_promote`` would accept as a winner."""
    return {
        "model_mae": 50.0,
        "baseline_mae": 100.0,
        "model_rmse": 60.0,
        "baseline_rmse": 120.0,
        "n_test": 10,
        "improvement_pct": 50.0,
        "horizon_energy_mae_24h": 1.0,
        "baseline_horizon_energy_mae_24h": 2.0,
        "horizon_energy_mae_12h": 0.5,
        "pinball_p50": 10.0,
        "pinball_p80": 20.0,
    }


def test_retrain_hgbr_ready_and_promotes_selects_hgbr(monkeypatch):
    """is_ready=True + should_promote=True → predictor holds HGBR, active_model_name='hgbr'."""
    from custom_components.anker_x1_smartgrid import hgbr as hgbr_mod
    from custom_components.anker_x1_smartgrid import backtest as bt_mod

    metrics = _fake_promote_metrics()
    monkeypatch.setattr(hgbr_mod.HGBRQuantileModel, "is_ready", lambda self, rows, **kw: True)
    monkeypatch.setattr(bt_mod, "walk_forward_hgbr", lambda *a, **kw: metrics)
    monkeypatch.setattr(bt_mod, "should_promote", lambda m: True)

    def _fake_fit(self, rows, **kw):
        self._fitted = True
        return self

    monkeypatch.setattr(hgbr_mod.HGBRQuantileModel, "fit", _fake_fit)

    rec = _HGBRRec(hourly_rows=[{"hour_ts": "2025-01-01T00:00:00+00:00", "house_load_mean": 800.0}])
    ctl = _make_retrain_ctl(rec)
    ctl._retrain_sync("2025-01-01T00:00:00+00:00")

    assert ctl.active_model_name == "hgbr"
    assert ctl.backtest_result is metrics
    assert ctl.predictor._model is not None


def test_retrain_hgbr_promotion_fits_only_p50_quantile(monkeypatch):
    """Task 14: the promoted-model fit must request only q=0.5.

    Live control never reads q=0.8 (that was P80-survival scaffolding, already
    removed); fitting it at retrain doubled cost for no reader. The display
    metric pinball_p80 comes from walk_forward_hgbr's own separate fits, not
    this promoted model, so it is unaffected.
    """
    from custom_components.anker_x1_smartgrid import hgbr as hgbr_mod
    from custom_components.anker_x1_smartgrid import backtest as bt_mod

    metrics = _fake_promote_metrics()
    monkeypatch.setattr(hgbr_mod.HGBRQuantileModel, "is_ready", lambda self, rows, **kw: True)
    monkeypatch.setattr(bt_mod, "walk_forward_hgbr", lambda *a, **kw: metrics)
    monkeypatch.setattr(bt_mod, "should_promote", lambda m: True)

    def _fake_fit(self, rows, quantiles=(0.5, 0.8)):
        self._fitted = True
        # Mirror the real fit(): populate _models with one entry per requested quantile.
        self._models = {float(q): object() for q in quantiles}
        return self

    monkeypatch.setattr(hgbr_mod.HGBRQuantileModel, "fit", _fake_fit)

    rec = _HGBRRec(hourly_rows=[{"hour_ts": "2025-01-01T00:00:00+00:00", "house_load_mean": 800.0}])
    ctl = _make_retrain_ctl(rec)
    ctl._retrain_sync("2025-01-01T00:00:00+00:00")

    assert ctl.active_model_name == "hgbr"
    fitted = ctl.predictor._model
    assert set(fitted._models.keys()) == {0.5}, (
        f"promoted HGBR must fit only q=0.5 (live control never reads 0.8); got {sorted(fitted._models.keys())}"
    )


def test_retrain_hgbr_ready_but_not_promoted_falls_to_bucketed(monkeypatch):
    """is_ready=True but should_promote=False → falls back to bucketed when data available."""
    from custom_components.anker_x1_smartgrid import hgbr as hgbr_mod
    from custom_components.anker_x1_smartgrid import backtest as bt_mod

    metrics = _fake_promote_metrics()
    monkeypatch.setattr(hgbr_mod.HGBRQuantileModel, "is_ready", lambda self, rows, **kw: True)
    monkeypatch.setattr(bt_mod, "walk_forward_hgbr", lambda *a, **kw: metrics)
    monkeypatch.setattr(bt_mod, "should_promote", lambda m: False)

    rec = _HGBRRec(feature_rows=_cold_warm_rows(), hourly_rows=_cold_warm_hourly_rows())
    ctl = _make_retrain_ctl(rec, min_train_samples=100)
    ctl._retrain_sync("2025-01-01T00:00:00+00:00")

    assert ctl.active_model_name == "bucketed"


def test_retrain_hgbr_not_ready_falls_to_bucketed(monkeypatch):
    """is_ready=False → HGBR skipped entirely; bucketed wins when enough hourly rows."""
    from custom_components.anker_x1_smartgrid import hgbr as hgbr_mod

    monkeypatch.setattr(hgbr_mod.HGBRQuantileModel, "is_ready", lambda self, rows, **kw: False)

    rec = _HGBRRec(feature_rows=_cold_warm_rows(), hourly_rows=_cold_warm_hourly_rows())
    ctl = _make_retrain_ctl(rec, min_train_samples=100)
    ctl._retrain_sync("2025-01-01T00:00:00+00:00")

    assert ctl.active_model_name == "bucketed"


def test_retrain_use_learned_model_false_goes_to_profile(monkeypatch):
    """use_learned_model=False → profile fallback; HGBR is_ready is never consulted."""
    from custom_components.anker_x1_smartgrid import hgbr as hgbr_mod

    is_ready_calls: list = []

    def _spy_is_ready(self, rows, **kw):
        is_ready_calls.append(1)
        return False

    monkeypatch.setattr(hgbr_mod.HGBRQuantileModel, "is_ready", _spy_is_ready)

    rec = _HGBRRec(feature_rows=_cold_warm_rows(), hourly_rows=[])
    ctl = _make_retrain_ctl(rec, use_learned_model=False)
    ctl._retrain_sync("2025-01-01T00:00:00+00:00")

    assert ctl.active_model_name == "profile"
    assert not is_ready_calls, "is_ready must not be called when use_learned_model=False"


def test_retrain_empty_recorder_no_crash():
    """Empty recorder (no rows at all) → profile fallback, no exception raised."""
    rec = _HGBRRec(feature_rows=[], hourly_rows=[])
    ctl = _make_retrain_ctl(rec)
    # Must complete without raising.
    ctl._retrain_sync("2025-01-01T00:00:00+00:00")
    # With no hourly rows, is_ready([]) returns False → HGBR skipped; and
    # clean_h=[] so len(clean_h) < DEFAULT_MIN_TRAIN_HOURS → profile.
    assert ctl.active_model_name == "profile"


def test_retrain_bucketed_path_unchanged(monkeypatch):
    """Bucketed path (HGBR not ready) sets backtest_result exactly as before."""
    from custom_components.anker_x1_smartgrid import hgbr as hgbr_mod

    monkeypatch.setattr(hgbr_mod.HGBRQuantileModel, "is_ready", lambda self, rows, **kw: False)

    rec = _HGBRRec(feature_rows=_cold_warm_rows(), hourly_rows=_cold_warm_hourly_rows())
    ctl = _make_retrain_ctl(rec, min_train_samples=100)
    ctl._retrain_sync("2025-01-01T00:00:00+00:00")

    assert ctl.active_model_name == "bucketed"
    bt = ctl.backtest_result
    assert bt is not None
    # Verify the standard walk_forward keys are present (bucketed backtest unchanged).
    for key in ("model_mae", "baseline_mae", "n_test", "improvement_pct"):
        assert key in bt, f"missing key in bucketed backtest_result: {key}"


@pytest.mark.asyncio
async def test_rollup_skips_when_recorder_none(monkeypatch):
    """tick() must not crash and must not call rollup when recorder is None.

    Uses the disabled path with no plant inputs seeded so _record_sample is
    skipped (inputs=None); the regret backfill is safe because it wraps in
    try/except.  The rollup guard is the only thing explicitly tested here.
    """
    hass = _T6Hass()
    rec = _RollupRecorder()
    ctrl = _make_t6_controller(hass, rec)
    # Null out the recorder AFTER construction to simulate absent-recorder scenario.
    ctrl._recorder = None  # type: ignore[assignment]
    monkeypatch.setattr(controller.dt_util, "utcnow", lambda: _T6_NOW)

    # Must complete without AttributeError.
    result = await ctrl.tick()

    assert result["reason"] == "disabled"
