"""TDD tests for T5b: live tick measured-ΔSoC drift-hedge accumulator + persistence.

Covers all 11 reviewer cases from the plan:
1.  Inert when off        — soc_hedge_fraction=0.0 → compute_decision gets hedge_drain_by_hour=None;
                            drift fields NOT mutated.
2.  Persistence roundtrip — six drift fields survive _persist / restore (datetime, float, bool).
3.  Underdelivery rises   — flat SoC while forecast expects surplus → _soc_drift_kwh > 0.
4.  Grid recovery shrinks — SoC jumps up after positive drift → accumulator drops (closed loop).
5.  Export add-back       — intentional export is duration-scaled and add-back; _last_export reset to 0
                            after each step. Paired: real shortfall during export STILL raises drift.
6.  No batt_w cancel      — SoC drops exactly as load deficit predicts → expected ≈ measured → drift ≈ 0.
7.  Fidelity              — accumulator uses interval (pv_w, load_w), not predictor; verified by giving
                            the stub intervals values that differ from what predictor would return.
8.  Hysteresis            — once engaged, stays on between release-band and engage-band; releases below release.
9.  Rail gate             — soc ≥ soc_target−1 ⇒ step gated; accumulator unchanged.
10. Hedge at trough       — with drift above deadband, LIVE compute_decision receives a non-None dict
                            keyed at the cheapest forward price slot.
11. New-day reset         — yesterday _soc_drift_day → day rollover clears accumulator AND SoC anchor.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, UTC

import pytest

from custom_components.anker_x1_smartgrid import const
from custom_components.anker_x1_smartgrid import controller as ctrl_mod
from custom_components.anker_x1_smartgrid.controller import Controller
from custom_components.anker_x1_smartgrid.models import (
    Config,
    ControllerState,
    ForecastInterval,
    PlanState,
)
from tests.helpers import StubHass as _StubHass

UTC = UTC
# 14:00 UTC on 2026-06-29 — keeps local date June 29 even in deeply-west zones.
BASE = datetime(2026, 6, 29, 14, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Stubs (mirror test_controller_export_executor.py pattern) — _StubHass
# imported from tests/helpers.py (B2a). _StubActuator/_StubStore/_StubRecorder
# stay local: _StubActuator has no .engaged tracking and engage_export skips
# the setpoint>0 validation helpers.StubActuator enforces; _StubStore
# captures the saved payload (helpers.StubStore is a no-op); _StubRecorder's
# read_load_samples/read_decisions/read_feature_rows/read_hourly_rows are
# hardcoded to return [] regardless of appended rows. Genuine behavior
# differences, not copy-paste dupes.
# ---------------------------------------------------------------------------


class _StubActuator:
    def __init__(self):
        self.calls: list[tuple] = []
        self.last_setpoint_w: float = 0.0
        self.engaged = False

    async def engage_and_charge(self, setpoint_w: float) -> None:
        self.calls.append(("engage_and_charge", setpoint_w))
        self.last_setpoint_w = setpoint_w

    async def engage_export(self, setpoint_w: float) -> None:
        self.calls.append(("engage_export", setpoint_w))
        self.last_setpoint_w = setpoint_w

    async def release_to_self(self) -> None:
        self.calls.append(("release_to_self",))
        self.last_setpoint_w = 0.0


class _StubStore:
    def __init__(self):
        self.saved: dict = {}

    async def async_save(self, data: dict) -> None:
        self.saved = data


class _StubRecorder:
    def __init__(self):
        self.rows: list[dict] = []
        self.decision_rows: list[dict] = []
        self.daily_regret_rows: dict = {}

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

    def append_device_samples(self, ts, rows):
        pass

    def rollup_device_hours(self, now_iso):
        return 0

    def purge_device_samples(self, cutoff_iso):
        return 0

    def purge_device_hourly(self, cutoff_iso):
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
        return None

    def read_daily_regret_range(self, since_day, until_day=None):
        return []


# ---------------------------------------------------------------------------
# Config / controller helpers
# ---------------------------------------------------------------------------


def _drift_cfg(**overrides) -> Config:
    """Config with soc_hedge_fraction=0.5 (feature enabled) and simple 1:1 η."""
    defaults = dict(
        capacity_kwh=10.0,
        soc_floor=10.0,
        soc_target=97.0,
        max_charge_w=3000.0,
        max_export_w=3000.0,
        eta_charge=1.0,
        round_trip_eff=1.0,
        cycle_cost_eur_per_kwh=0.04,
        export_eps_lo_kwh=0.2,
        export_eps_hi_kwh=0.4,
        export_dwell_min=0,
        enable_export=False,
        soc_hedge_fraction=0.5,
        soc_drift_deadband_kwh=0.3,
        soc_drift_decay_halflife_h=0.0,
    )
    defaults.update(overrides)
    return Config(**defaults)  # type: ignore[arg-type]


def _make_ctrl(hass, cfg: Config | None = None):
    """Build an enabled Controller with a minimal data config."""
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
    act = _StubActuator()
    store = _StubStore()
    rec = _StubRecorder()
    ctrl = Controller(hass=hass, data=data, recorder=rec, actuator=act, store=store)
    ctrl.cfg = cfg or _drift_cfg()
    ctrl.enabled = True
    return ctrl, act, store


def _seed_inputs(hass, *, soc: str = "60.0", now: datetime = BASE):
    """Seed HA state for the tick's read_plant_inputs path."""
    hass.set_state("sensor.soc", soc)
    hass.set_state("sensor.meter_power", "300.0")
    hass.set_state("sensor.pv_power", "0.0")
    hass.set_state("sensor.battery_power", "0.0")
    hass.set_state("sensor.irradiance", "600.0")
    hass.set_state("weather.home", "sunny", {"temperature": 22.0})
    sunset_iso = (now + timedelta(hours=8)).isoformat()
    hass.set_state("sun.sun", "above_horizon", {"next_setting": sunset_iso})
    hass.set_state(
        "sensor.price",
        "0.25",
        {
            "forecast": [
                {
                    "datetime": (now + timedelta(hours=i)).isoformat(),
                    "electricity_price": int(0.25 * const.PRICE_SCALE),
                }
                for i in range(12)
            ]
        },
    )


def _make_stub(
    intervals: list | None = None,
    capture: dict | None = None,
    deadline_offset_h: float = 8.0,
):
    """
    Factory for a compute_decision stub that:
    - Captures hedge_drain_by_hour into `capture` dict (key "hedge_drain_by_hour")
    - Writes intervals into _out["intervals"]
    - Returns PASSIVE plan, deadline=now+deadline_offset_h, empty horizon
    """

    def _stub(
        plan,
        inputs,
        slots,
        pv_remaining,
        sunset,
        predictor,
        cur_temp,
        cfg,
        tomorrow_total=None,
        sun_times=None,
        today_arrays=None,
        tomorrow_arrays=None,
        today_watts=None,
        tomorrow_watts=None,
        export_price=None,
        _out=None,
        _shadow_dp=False,
        export_price_matches_import=False,
        estimated_tomorrow=None,
        past_actuals_by_slot=None,
        hedge_drain_by_hour=None,
        temp_by_hour=None,
        **kwargs,
    ):
        if capture is not None:
            capture["hedge_drain_by_hour"] = hedge_drain_by_hour
        if _out is not None:
            _out["dp_selected"] = []
            _out["intervals"] = intervals or []
            _out["grid_request"] = {}
            _out["export_request"] = {}
        passive = PlanState(ControllerState.PASSIVE, inputs.now, ())
        deadline = inputs.now + timedelta(hours=deadline_offset_h)
        return passive, 0.0, deadline, [], "water_value", []

    return _stub


# ---------------------------------------------------------------------------
# 1. Inert when off
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inert_when_soc_hedge_fraction_zero(monkeypatch):
    """With soc_hedge_fraction=0.0, hedge_drain_by_hour=None passed to compute_decision.

    The drift block is gated entirely OFF — no fields mutated, no extra persist of drift keys.
    """
    hass = _StubHass()
    ctrl, _, store = _make_ctrl(hass, cfg=_drift_cfg(soc_hedge_fraction=0.0))
    monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
    _seed_inputs(hass)
    capture: dict = {}
    monkeypatch.setattr(ctrl_mod, "compute_decision", _make_stub(capture=capture))

    await ctrl.tick()

    assert capture.get("hedge_drain_by_hour") is None
    # Drift fields must remain at __init__ defaults (block gated off)
    assert ctrl._soc_drift_kwh == 0.0
    assert ctrl._soc_drift_last_soc_pct is None
    assert ctrl._soc_drift_last_update is None
    # Intervals cache must NOT be set — off→on toggle leaves it None so H1 gate fires
    assert ctrl._soc_drift_last_intervals is None


# ---------------------------------------------------------------------------
# 2. Persistence roundtrip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persistence_roundtrip():
    """Six drift fields survive _persist / restore.

    - _soc_drift_last_update round-trips through isoformat → parse_datetime.
    - _soc_drift_engaged is bool-restored.
    - _soc_drift_last_export_kwh_dc floats cleanly.
    """
    hass = _StubHass()
    ctrl, _, store = _make_ctrl(hass)

    ctrl._soc_drift_kwh = 1.5
    ctrl._soc_drift_day = "2026-06-29"
    ctrl._soc_drift_last_update = BASE
    ctrl._soc_drift_last_soc_pct = 65.0
    ctrl._soc_drift_engaged = True
    ctrl._soc_drift_last_export_kwh_dc = 0.25

    await ctrl._persist()

    hass2 = _StubHass()
    ctrl2, _, _ = _make_ctrl(hass2)
    ctrl2.restore(store.saved)

    assert ctrl2._soc_drift_kwh == pytest.approx(1.5)
    assert ctrl2._soc_drift_day == "2026-06-29"
    assert ctrl2._soc_drift_last_update is not None
    assert abs((ctrl2._soc_drift_last_update - BASE).total_seconds()) < 1
    assert ctrl2._soc_drift_last_soc_pct == pytest.approx(65.0)
    assert ctrl2._soc_drift_engaged is True
    assert ctrl2._soc_drift_last_export_kwh_dc == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# 3. Underdelivery increases drift
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_underdelivery_increases_drift(monkeypatch):
    """Flat SoC while forecast expects surplus → _soc_drift_kwh > 0.

    Tick 1 seeds the anchor (gated on first sample).
    Tick 2 (5 min later, dt_h≈0.083) sees no SoC change while the covering
    ForecastInterval promises 3000 W PV vs 500 W load → expected_dc >> 0 → drift positive.
    """
    hass = _StubHass()
    cfg = _drift_cfg()  # eta_charge=1.0, eta_discharge=1.0
    ctrl, _, _ = _make_ctrl(hass, cfg=cfg)

    # ForecastInterval: 3000 W PV, 500 W load; covers the next 2 h from BASE
    ivs = [ForecastInterval(BASE, 3000.0, 500.0, 2.0)]

    # Tick 1 — seeds _soc_drift_last_soc_pct and _soc_drift_last_update; step is gated (first sample)
    monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
    _seed_inputs(hass, soc="60.0", now=BASE)
    monkeypatch.setattr(ctrl_mod, "compute_decision", _make_stub(intervals=ivs))
    await ctrl.tick()

    assert ctrl._soc_drift_kwh == pytest.approx(0.0)  # gated on first sample

    # Tick 2 — 5 min later, same flat SoC=60%
    t2 = BASE + timedelta(minutes=5)
    monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: t2)
    _seed_inputs(hass, soc="60.0", now=t2)
    monkeypatch.setattr(ctrl_mod, "compute_decision", _make_stub(intervals=ivs))
    await ctrl.tick()

    # expected_dc = (3000−500) * (5/60) / 1000 * 1.0 ≈ +0.208 kWh; measured = 0 → drift > 0
    assert ctrl._soc_drift_kwh > 0.0


# ---------------------------------------------------------------------------
# 4. Grid-charge recovery shrinks drift (closed loop)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grid_recovery_shrinks_drift(monkeypatch):
    """After positive drift, a SoC jump (grid charge) drives the accumulator down."""
    hass = _StubHass()
    cfg = _drift_cfg()
    ctrl, _, _ = _make_ctrl(hass, cfg=cfg)
    ivs = [ForecastInterval(BASE, 3000.0, 500.0, 3.0)]

    # Build up positive drift over two ticks
    for soc_str, t in [("60.0", BASE), ("60.0", BASE + timedelta(minutes=5))]:
        monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda _t=t: _t)
        _seed_inputs(hass, soc=soc_str, now=t)
        monkeypatch.setattr(ctrl_mod, "compute_decision", _make_stub(intervals=ivs))
        await ctrl.tick()

    drift_before = ctrl._soc_drift_kwh
    assert drift_before > 0.0, "precondition: drift must be positive before recovery tick"

    # Tick 3: SoC jumps from 60% to 70% (grid-charge recovery = +1.0 kWh DC on 10 kWh battery)
    t3 = BASE + timedelta(minutes=10)
    monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: t3)
    _seed_inputs(hass, soc="70.0", now=t3)
    monkeypatch.setattr(ctrl_mod, "compute_decision", _make_stub(intervals=ivs))
    await ctrl.tick()

    # measured_dc = +1.0 kWh >> expected_dc (~+0.208 kWh) → per_step < 0 → accumulator shrinks
    assert ctrl._soc_drift_kwh < drift_before


@pytest.mark.asyncio
async def test_booked_grid_charge_is_not_drift(monkeypatch):
    """A SoC rise explained by ledger-booked grid charge leaves the accumulator on the forecast step only."""
    hass = _StubHass()
    ctrl, _, _ = _make_ctrl(hass, cfg=_drift_cfg())
    ivs = [ForecastInterval(BASE, 3000.0, 500.0, 3.0)]

    for soc_str, t in [("60.0", BASE), ("60.0", BASE + timedelta(minutes=5))]:
        monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda _t=t: _t)
        _seed_inputs(hass, soc=soc_str, now=t)
        monkeypatch.setattr(ctrl_mod, "compute_decision", _make_stub(intervals=ivs))
        await ctrl.tick()
    drift_before = ctrl._soc_drift_kwh

    # One 60-s ledger booking, extrapolated over the 5-min step → 1.0 kWh.
    ctrl.today_grid_charge_kwh = 0.2
    t3 = BASE + timedelta(minutes=10)
    monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: t3)
    _seed_inputs(hass, soc="70.0", now=t3)
    monkeypatch.setattr(ctrl_mod, "compute_decision", _make_stub(intervals=ivs))
    await ctrl.tick()

    # measured +1.0 kWh fully booked as grid charge → step = expected (~+0.208), not -0.79
    assert ctrl._soc_drift_kwh == pytest.approx(drift_before + 2500.0 * (5 / 60) / 1000.0, abs=0.01)


async def _two_ticks(monkeypatch, hass, ctrl, ivs, soc_to, minutes, booked_ac_kwh, prev_state):
    monkeypatch.setattr(ctrl_mod, "compute_decision", _make_stub(intervals=ivs))
    monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
    _seed_inputs(hass, soc="60.0", now=BASE)
    await ctrl.tick()
    ctrl.plan = PlanState(prev_state, BASE, ())
    ctrl.today_grid_charge_kwh += booked_ac_kwh
    t2 = BASE + timedelta(minutes=minutes)
    monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: t2)
    _seed_inputs(hass, soc=soc_to, now=t2)
    await ctrl.tick()


@pytest.mark.asyncio
async def test_stalled_step_scales_booked_grid_charge(monkeypatch):
    """The ledger books 60 s per tick: a 5-min step at 3 kW carries one 0.05 kWh booking
    but measures +0.25 kWh. The add-back is extrapolated like the export field."""
    hass = _StubHass()
    ctrl, _, _ = _make_ctrl(hass, cfg=_drift_cfg())
    ivs = [ForecastInterval(BASE, 0.0, 0.0, 2.0)]  # expected 0
    await _two_ticks(monkeypatch, hass, ctrl, ivs, "62.5", 5, 0.05, ControllerState.FORCING)
    assert ctrl._soc_drift_kwh == pytest.approx(0.0, abs=0.005)


@pytest.mark.asyncio
async def test_forcing_step_expects_no_house_drain(monkeypatch):
    """While FORCING the grid serves the house deficit: the pack gains only the booked charge."""
    hass = _StubHass()
    ctrl, _, _ = _make_ctrl(hass, cfg=_drift_cfg())
    ivs = [ForecastInterval(BASE, 0.0, 3000.0, 2.0)]  # a passive pack would drain 0.05 kWh/min
    await _two_ticks(monkeypatch, hass, ctrl, ivs, "60.5", 1, 0.05, ControllerState.FORCING)
    assert ctrl._soc_drift_kwh == pytest.approx(0.0, abs=0.005)


@pytest.mark.asyncio
async def test_forcing_step_keeps_pv_surplus_expectation(monkeypatch):
    """PV surplus still reaches the pack while FORCING: a flat SoC beside it is still drift."""
    hass = _StubHass()
    ctrl, _, _ = _make_ctrl(hass, cfg=_drift_cfg())
    ivs = [ForecastInterval(BASE, 3500.0, 500.0, 2.0)]  # +0.05 kWh/min of surplus
    await _two_ticks(monkeypatch, hass, ctrl, ivs, "60.0", 1, 0.0, ControllerState.FORCING)
    assert ctrl._soc_drift_kwh == pytest.approx(0.05, abs=0.005)


# ---------------------------------------------------------------------------
# 5. Export add-back: neutral case + real-shortfall case; re-zero assertion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_export_addback_neutral_and_real_shortfall(monkeypatch):
    """Intentional export is duration-scaled and added back; real shortfall still raises drift.

    Uses 1-minute ticks (dt_h = tick_h = 1/60) so export_dc_step = last_export_kwh_dc × 1.

    Case A (neutral): SoC drops by exactly the commanded export → per_step ≈ 0 → drift unchanged.
    Case B (shortfall): same export, but SoC drops MORE → real shortfall captured → drift rises.
    Both cases: _soc_drift_last_export_kwh_dc == 0.0 after the step (consumed + reset).
    """
    # ── Shared fixture: forecast expects no surplus (pv=0, load=0 → expected_dc=0) ──
    ivs_zero = [ForecastInterval(BASE, 0.0, 0.0, 2.0)]

    # ── Case A: neutral ──
    hass_a = _StubHass()
    ctrl_a, _, _ = _make_ctrl(hass_a, cfg=_drift_cfg())

    # Tick 1 at BASE: seeds anchor
    monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
    _seed_inputs(hass_a, soc="60.0", now=BASE)
    monkeypatch.setattr(ctrl_mod, "compute_decision", _make_stub(intervals=ivs_zero))
    await ctrl_a.tick()

    # Simulate export that happened last tick: commanded 0.05 kWh DC over 1 nominal tick
    ctrl_a._soc_drift_last_export_kwh_dc = 0.05

    # Tick 2 at BASE+1min: SoC drops 0.5% = 0.05 kWh DC (exactly the commanded export)
    t2 = BASE + timedelta(minutes=1)
    monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: t2)
    _seed_inputs(hass_a, soc="59.5", now=t2)  # 60% − 0.5% = 59.5%  (−0.05 kWh on 10 kWh)
    monkeypatch.setattr(ctrl_mod, "compute_decision", _make_stub(intervals=ivs_zero))
    await ctrl_a.tick()

    # per_step = 0 − (−0.05 + 0.05) = 0 → drift ≈ 0
    assert ctrl_a._soc_drift_kwh == pytest.approx(0.0, abs=0.01)
    # Export field consumed and re-zeroed
    assert ctrl_a._soc_drift_last_export_kwh_dc == pytest.approx(0.0)

    # ── Case B: real shortfall during export ──
    hass_b = _StubHass()
    ctrl_b, _, _ = _make_ctrl(hass_b, cfg=_drift_cfg())

    monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
    _seed_inputs(hass_b, soc="60.0", now=BASE)
    monkeypatch.setattr(ctrl_mod, "compute_decision", _make_stub(intervals=ivs_zero))
    await ctrl_b.tick()

    ctrl_b._soc_drift_last_export_kwh_dc = 0.05

    # SoC drops MORE than the export accounts for (real shortfall 0.05 kWh extra)
    monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: t2)
    _seed_inputs(hass_b, soc="59.0", now=t2)  # 60% − 1.0% = 59.0% (−0.10 kWh total)
    monkeypatch.setattr(ctrl_mod, "compute_decision", _make_stub(intervals=ivs_zero))
    await ctrl_b.tick()

    # per_step = 0 − (−0.10 + 0.05) = +0.05 > 0 → drift rises
    assert ctrl_b._soc_drift_kwh > 0.0
    assert ctrl_b._soc_drift_last_export_kwh_dc == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 6. No batt_w blanket cancel (SoC drops exactly as load deficit predicts → drift ≈ 0)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_batt_blanket_cancel(monkeypatch):
    """Battery serving house load already appears in load_w forecast → expected ≈ measured → drift ≈ 0.

    Forecast: pv=0, load=600 W.  dt_h=1/60 h (1-min tick).
    expected_dc = −(600 × 1/60 / 1000) / 1.0 ≈ −0.01 kWh  (pure load discharge, η=1)
    SoC drops 60% → 59.9%: measured_dc = −0.01 kWh on 10 kWh battery.
    per_step = expected − measured = 0 → drift stays ≈ 0.
    """
    hass = _StubHass()
    cfg = _drift_cfg()
    ctrl, _, _ = _make_ctrl(hass, cfg=cfg)

    # Forecast: pv=0, load=600 W  (exactly what the battery serves — no grid)
    ivs = [ForecastInterval(BASE, 0.0, 600.0, 2.0)]

    # Tick 1 seeds the anchor
    monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
    _seed_inputs(hass, soc="60.0", now=BASE)
    monkeypatch.setattr(ctrl_mod, "compute_decision", _make_stub(intervals=ivs))
    await ctrl.tick()

    # Tick 2: SoC drops exactly as forecast predicts
    # expected_dc = -(600 * (1/60) / 1000) / 1.0 = -0.01 kWh
    # soc_drop = 0.01 kWh / 10 kWh * 100% = 0.1%   →  60.0 - 0.1 = 59.9%
    t2 = BASE + timedelta(minutes=1)
    monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: t2)
    _seed_inputs(hass, soc="59.9", now=t2)
    monkeypatch.setattr(ctrl_mod, "compute_decision", _make_stub(intervals=ivs))
    await ctrl.tick()

    # drift should stay near 0 (expected ≈ measured, no correction needed)
    assert abs(ctrl._soc_drift_kwh) < 0.05


# ---------------------------------------------------------------------------
# 7. Fidelity: accumulator uses interval (pv_w, load_w), not predictor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fidelity_uses_intervals_not_predictor(monkeypatch):
    """Drift is computed from ForecastInterval values, not from predictor.predict().

    We give the stub intervals with a distinctive (pv_w=5000, load_w=100) forecast.
    The accumulator must report drift that matches that interval, not a predictor call.
    We verify: after tick 2 with flat SoC, drift ≈ expected_dc(5000, 100, dt_h).
    """
    hass = _StubHass()
    cfg = _drift_cfg()  # eta_charge=1.0
    ctrl, _, _ = _make_ctrl(hass, cfg=cfg)

    dt_h = 5.0 / 60.0  # 5-minute tick
    # Distinctive interval: 5000 W PV, 100 W load
    pv_w, load_w = 5000.0, 100.0
    ivs = [ForecastInterval(BASE, pv_w, load_w, 2.0)]

    # Tick 1: seed anchor
    monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
    _seed_inputs(hass, soc="60.0", now=BASE)
    monkeypatch.setattr(ctrl_mod, "compute_decision", _make_stub(intervals=ivs))
    await ctrl.tick()

    # Tick 2: flat SoC → drift = expected_dc from interval (not predictor)
    t2 = BASE + timedelta(minutes=5)
    monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: t2)
    _seed_inputs(hass, soc="60.0", now=t2)
    monkeypatch.setattr(ctrl_mod, "compute_decision", _make_stub(intervals=ivs))
    await ctrl.tick()

    # expected_dc from the interval (eta=1.0): (5000−100) * dt_h / 1000 = 4900 * (5/60) / 1000
    expected_dc = (pv_w - load_w) * dt_h / 1000.0  # eta_charge=1.0 on surplus
    # measured_dc = 0 (flat SoC); per_step = expected_dc; drift = expected_dc
    assert ctrl._soc_drift_kwh == pytest.approx(expected_dc, abs=0.005)


# ---------------------------------------------------------------------------
# 8. Hysteresis: stay engaged between bands; release below release band
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hysteresis_stays_engaged_between_bands(monkeypatch):
    """Once engaged, drift stays active until accumulator falls below release band (0.5×deadband).

    Three sub-cases tested in isolation (fresh controller each, step gated via rail so
    accumulator stays at the pre-seeded value and only the hysteresis output changes):

    A) acc=0.5 > engage=0.3 → engaged=True, hedge>0 (sanity check)
    B) acc=0.2, engaged_prev=True: above release (0.15) but below engage (0.3) → stays True
    C) acc=0.1, engaged_prev=True: below release (0.15) → releases to False
    """
    ivs: list = []  # forecast doesn't matter; step is gated by SoC rail

    for acc_value, prev_engaged, expected_engaged in [
        (0.5, False, True),  # A: above engage band → engages
        (0.2, True, True),  # B: above release, below engage, was engaged → stays on
        (0.1, True, False),  # C: below release → releases
    ]:
        hass = _StubHass()
        cfg = _drift_cfg()
        ctrl, _, _ = _make_ctrl(hass, cfg=cfg)

        # Seed accumulator and engaged state directly
        ctrl._soc_drift_kwh = acc_value
        ctrl._soc_drift_engaged = prev_engaged
        # Seed day key to today so reset_if_new_day doesn't zero the accumulator
        ctrl._soc_drift_day = ctrl_mod.dt_util.as_local(BASE).date().isoformat()
        # Seed anchor so gating on "first sample" doesn't apply
        ctrl._soc_drift_last_soc_pct = 60.0
        ctrl._soc_drift_last_update = BASE - timedelta(minutes=5)

        # SoC at top rail (≥ soc_target−1.0) to gate the accumulator step
        # → accumulator stays at acc_value, only hysteresis output changes.
        monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
        soc_at_rail = str(cfg.soc_target)  # 97% ≥ soc_target-1.0=96%
        _seed_inputs(hass, soc=soc_at_rail, now=BASE)
        monkeypatch.setattr(ctrl_mod, "compute_decision", _make_stub(intervals=ivs))

        await ctrl.tick()

        assert ctrl._soc_drift_engaged is expected_engaged, (
            f"acc={acc_value}, prev_engaged={prev_engaged}: "
            f"expected _soc_drift_engaged={expected_engaged}, got {ctrl._soc_drift_engaged}"
        )


# ---------------------------------------------------------------------------
# 9. Rail gate: step gated when SoC ≥ soc_target − 1.0
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rail_gate_at_soc_target(monkeypatch):
    """When inputs.soc ≥ soc_target−1.0, the accumulator step is skipped entirely."""
    hass = _StubHass()
    cfg = _drift_cfg(soc_target=97.0)
    ctrl, _, _ = _make_ctrl(hass, cfg=cfg)

    # High-surplus forecast: would produce large positive drift if unguarded
    ivs = [ForecastInterval(BASE, 5000.0, 100.0, 3.0)]

    # Tick 1: SoC=96.5% ≥ 97−1=96 → gated (rail); seeds anchor too
    monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
    _seed_inputs(hass, soc="96.5", now=BASE)
    monkeypatch.setattr(ctrl_mod, "compute_decision", _make_stub(intervals=ivs))
    await ctrl.tick()

    assert ctrl._soc_drift_last_soc_pct == pytest.approx(96.5)

    # Tick 2: still near target — step stays gated
    t2 = BASE + timedelta(minutes=5)
    monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: t2)
    _seed_inputs(hass, soc="96.5", now=t2)
    monkeypatch.setattr(ctrl_mod, "compute_decision", _make_stub(intervals=ivs))
    await ctrl.tick()

    # Accumulator must not have moved
    assert ctrl._soc_drift_kwh == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_flips_across_the_charge_stop_book_no_drift(monkeypatch):
    """99 -> 98 at a 100 % target is the pack leaving the charge stop, not forecast drift."""
    hass = _StubHass()
    ctrl, _, _ = _make_ctrl(hass, cfg=_drift_cfg(soc_target=100.0))
    ivs = [ForecastInterval(BASE, 0.0, 0.0, 3.0)]  # nothing expected to move the pack
    for i, soc in enumerate(("99", "98", "99", "98")):
        t = BASE + timedelta(minutes=i)
        monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda t=t: t)
        _seed_inputs(hass, soc=soc, now=t)
        monkeypatch.setattr(ctrl_mod, "compute_decision", _make_stub(intervals=ivs))
        await ctrl.tick()

    assert ctrl._soc_drift_kwh == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 10. Hedge flows to LIVE compute_decision at the cheapest forward slot (trough)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hedge_keyed_at_trough_slot(monkeypatch):
    """With drift above deadband, compute_decision receives hedge_drain_by_hour keyed at the
    cheapest forward price slot (not necessarily now_h).

    Price layout: 0.40 at BASE+0h, 0.15 at BASE+2h (cheapest), 0.35 at BASE+4h.
    The hedge must be keyed at the 0.15 slot (BASE+2h aligned to hour).
    """
    hass = _StubHass()
    cfg = _drift_cfg(soc_drift_deadband_kwh=0.1)  # lower deadband so hedge fires sooner
    ctrl, _, _ = _make_ctrl(hass, cfg=cfg)

    # Surplus forecast: will produce positive drift
    ivs = [ForecastInterval(BASE, 4000.0, 200.0, 6.0)]

    # Price forecast with a clear cheap slot at +2h
    cheap_h = (BASE + timedelta(hours=2)).replace(minute=0, second=0, microsecond=0)
    hass.set_state(
        "sensor.price",
        "0.40",
        {
            "forecast": [
                {
                    "datetime": (BASE + timedelta(hours=i)).isoformat(),
                    "electricity_price": int(price * const.PRICE_SCALE),
                }
                for i, price in enumerate([0.40, 0.40, 0.15, 0.40, 0.35, 0.40, 0.40, 0.40])
            ]
        },
    )
    hass.set_state("sensor.soc", "60.0")
    hass.set_state("sensor.meter_power", "300.0")
    hass.set_state("sensor.pv_power", "0.0")
    hass.set_state("sensor.battery_power", "0.0")
    hass.set_state("sensor.irradiance", "600.0")
    hass.set_state("weather.home", "sunny", {"temperature": 22.0})
    hass.set_state("sun.sun", "above_horizon", {"next_setting": (BASE + timedelta(hours=8)).isoformat()})

    # Tick 1: seed anchor (gated, first sample)
    monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
    monkeypatch.setattr(ctrl_mod, "compute_decision", _make_stub(intervals=ivs))
    await ctrl.tick()

    # Manually push accumulator above deadband (simulate several underdelivery ticks)
    ctrl._soc_drift_kwh = 0.5  # well above deadband=0.1

    # Tick 2: capture the hedge_drain_by_hour kwargs
    t2 = BASE + timedelta(minutes=5)
    monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: t2)
    # Re-seed soc (stays flat for this tick)
    hass.set_state("sensor.soc", "60.0")
    capture: dict = {}
    monkeypatch.setattr(ctrl_mod, "compute_decision", _make_stub(intervals=ivs, capture=capture))
    await ctrl.tick()

    hedge = capture.get("hedge_drain_by_hour")
    assert hedge is not None, "Expected non-None hedge_drain_by_hour with drift above deadband"
    assert cheap_h in hedge, f"Hedge must be keyed at cheapest slot {cheap_h}; got keys={list(hedge.keys())}"
    assert hedge[cheap_h] > 0.0


# ---------------------------------------------------------------------------
# 11. New-day reset: accumulator cleared AND SoC anchor cleared on day rollover
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_day_resets_accumulator_and_soc_anchor(monkeypatch):
    """When _soc_drift_day ≠ today's local date, the accumulator resets to 0.0 AND
    _soc_drift_last_soc_pct is cleared to None (so the NEXT step is gated — no step spans midnight).
    """
    hass = _StubHass()
    ctrl, _, _ = _make_ctrl(hass, cfg=_drift_cfg())

    # Seed yesterday's state
    ctrl._soc_drift_kwh = 3.0
    ctrl._soc_drift_day = "2025-01-01"  # clearly different from BASE's date
    ctrl._soc_drift_last_soc_pct = 55.0
    ctrl._soc_drift_engaged = True

    monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
    _seed_inputs(hass, soc="60.0", now=BASE)
    monkeypatch.setattr(ctrl_mod, "compute_decision", _make_stub())
    await ctrl.tick()

    # Accumulator must have been reset to 0.0 (new day)
    assert ctrl._soc_drift_kwh == pytest.approx(0.0)
    # SoC anchor must be cleared (step is gated next tick — no step spans the day reset)
    assert ctrl._soc_drift_last_soc_pct is None
    # Day key updated
    assert ctrl._soc_drift_day is not None and ctrl._soc_drift_day != "2025-01-01"


# ---------------------------------------------------------------------------
# 12. Post-restart gate: first tick after restore must NOT accumulate
#     (H1 fix — _soc_drift_last_intervals is None after restore)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_restart_first_tick_gated(monkeypatch):
    """After an HA restart, _soc_drift_last_intervals is None (not persisted).
    Even if _soc_drift_last_soc_pct and _soc_drift_last_update were restored
    (non-None), the first post-restart tick must be gated so no spurious drift
    accumulates and the export add-back is not misapplied.

    Simulate restart by:
      - Restoring a controller with last_soc_pct=60.0, last_update=BASE-1min, last_export=0.1
        (as if 0.1 kWh DC export happened last tick before the restart).
      - _soc_drift_last_intervals stays None (not persisted — default from __init__).
      - Running one tick with a high-surplus forecast that would produce large positive drift
        if the step were not gated.

    Assert: _soc_drift_kwh stays at 0.0 (step gated), export field consumed and zeroed.
    """
    hass = _StubHass()
    cfg = _drift_cfg()
    ctrl, _, _ = _make_ctrl(hass, cfg=cfg)

    # Simulate persisted state restored from store (intervals NOT persisted — stays None)
    ctrl._soc_drift_last_soc_pct = 60.0
    ctrl._soc_drift_last_update = BASE - timedelta(minutes=1)
    ctrl._soc_drift_last_export_kwh_dc = 0.1  # export was in-flight before restart
    ctrl._soc_drift_day = ctrl_mod.dt_util.as_local(BASE).date().isoformat()
    # _soc_drift_last_intervals intentionally left as None (default from __init__)

    # High-surplus forecast: would produce large drift if step ran
    ivs = [ForecastInterval(BASE, 5000.0, 100.0, 3.0)]

    monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
    _seed_inputs(hass, soc="60.0", now=BASE)
    monkeypatch.setattr(ctrl_mod, "compute_decision", _make_stub(intervals=ivs))
    await ctrl.tick()

    # Step must be gated — no spurious drift, export add-back not applied
    assert ctrl._soc_drift_kwh == pytest.approx(0.0), "Post-restart tick must not accumulate drift"
    # Export field must be consumed and zeroed (regardless of gating)
    assert ctrl._soc_drift_last_export_kwh_dc == pytest.approx(0.0)
    # After the gated tick, intervals are now cached — next tick can step
    assert ctrl._soc_drift_last_intervals is not None


# ---------------------------------------------------------------------------
# 13. Idle drain flows into expected_soc_delta_kwh at the controller callsite
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idle_drain_passed_to_expected_soc_delta(monkeypatch):
    """The controller callsite must pass cfg.idle_drain_w into expected_soc_delta_kwh."""
    hass = _StubHass()
    cfg = _drift_cfg(idle_drain_w=130.0)
    ctrl, _, _ = _make_ctrl(hass, cfg=cfg)

    ivs = [ForecastInterval(BASE, 0.0, 600.0, 2.0)]

    captured: dict = {}
    real_fn = ctrl_mod.soc_drift.expected_soc_delta_kwh

    def _spy(*args, **kwargs):
        captured["idle_drain_w"] = kwargs.get("idle_drain_w")
        return real_fn(*args, **kwargs)

    monkeypatch.setattr(ctrl_mod.soc_drift, "expected_soc_delta_kwh", _spy)

    # Tick 1: seed anchor
    monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
    _seed_inputs(hass, soc="60.0", now=BASE)
    monkeypatch.setattr(ctrl_mod, "compute_decision", _make_stub(intervals=ivs))
    await ctrl.tick()

    # Tick 2: the drift step actually runs — spy must have captured idle_drain_w.
    t2 = BASE + timedelta(minutes=5)
    monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: t2)
    _seed_inputs(hass, soc="60.0", now=t2)
    monkeypatch.setattr(ctrl_mod, "compute_decision", _make_stub(intervals=ivs))
    await ctrl.tick()

    assert captured.get("idle_drain_w") == pytest.approx(130.0)


@pytest.mark.asyncio
async def test_idle_drain_absorbs_standby_debit_from_drift(monkeypatch):
    """With cfg.idle_drain_w matching the real standby drain, a SoC drop that includes
    that constant drain must NOT register as forecast drift (no double-compensation
    with whatever else already models idle drain, e.g. the DP physics).
    """
    hass = _StubHass()
    cfg = _drift_cfg(idle_drain_w=130.0)
    ctrl, _, _ = _make_ctrl(hass, cfg=cfg)

    # Forecast: pv=0, load=600 W. Real drain includes unmodeled 130 W idle standby → 730 W total.
    ivs = [ForecastInterval(BASE, 0.0, 600.0, 2.0)]

    # Tick 1: seed anchor
    monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
    _seed_inputs(hass, soc="60.0", now=BASE)
    monkeypatch.setattr(ctrl_mod, "compute_decision", _make_stub(intervals=ivs))
    await ctrl.tick()

    # Tick 2, 15 minutes later (dt_h=0.25): SoC drops by the FULL 730 W drain rate
    # (600 W forecast load + 130 W unmodeled idle standby), not just the 600 W forecast.
    # drain_kwh = 730 * 0.25 / 1000 = 0.1825 kWh -> 1.825% on a 10 kWh battery.
    t2 = BASE + timedelta(minutes=15)
    monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: t2)
    _seed_inputs(hass, soc="58.175", now=t2)
    monkeypatch.setattr(ctrl_mod, "compute_decision", _make_stub(intervals=ivs))
    await ctrl.tick()

    # Idle-modeled expectation matches the real drain exactly -> no spurious drift.
    assert ctrl._soc_drift_kwh == pytest.approx(0.0, abs=0.005)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("soc_from", "soc_to"),
    [
        (9.0, 9.0 - 1.5 / const.DISCHARGE_DERATE_5_10),  # bottom band: the gauge falls 1/f faster
        (60.0, 58.5),
    ],
)
async def test_bottom_band_drain_is_not_drift(monkeypatch, soc_from, soc_to):
    """An on-model discharge step drifts ~0 whether or not it sits in the bottom band."""
    hass = _StubHass()
    ctrl, _, _ = _make_ctrl(hass, cfg=_drift_cfg(soc_floor=5.0))  # rail gate at 6%, not 11%
    ivs = [ForecastInterval(BASE, 0.0, 600.0, 2.0)]  # 600 W * 15 min = 1.5% of 10 kWh at eta 1

    for soc, t in [(soc_from, BASE), (soc_to, BASE + timedelta(minutes=15))]:
        monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda _t=t: _t)
        _seed_inputs(hass, soc=str(soc), now=t)
        monkeypatch.setattr(ctrl_mod, "compute_decision", _make_stub(intervals=ivs))
        await ctrl.tick()

    assert ctrl._soc_drift_kwh == pytest.approx(0.0, abs=0.005)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("soc_from", "soc_to"),
    [
        (9.0, 9.0 - 0.5 / const.DISCHARGE_DERATE_5_10),  # in the band the gauge falls 1/f faster
        (60.0, 59.5),
    ],
)
async def test_on_model_export_step_is_not_drift_in_band(monkeypatch, soc_from, soc_to):
    hass = _StubHass()
    ctrl, _, _ = _make_ctrl(hass, cfg=_drift_cfg(soc_floor=5.0))
    ivs = [ForecastInterval(BASE, 0.0, 0.0, 2.0)]

    monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
    _seed_inputs(hass, soc=str(soc_from), now=BASE)
    monkeypatch.setattr(ctrl_mod, "compute_decision", _make_stub(intervals=ivs))
    await ctrl.tick()

    ctrl._soc_drift_last_export_kwh_dc = 0.05  # 0.5% of 10 kWh
    t2 = BASE + timedelta(minutes=1)
    monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: t2)
    _seed_inputs(hass, soc=str(soc_to), now=t2)
    monkeypatch.setattr(ctrl_mod, "compute_decision", _make_stub(intervals=ivs))
    await ctrl.tick()

    assert ctrl._soc_drift_kwh == pytest.approx(0.0, abs=1e-4)


@pytest.mark.asyncio
async def test_restored_anchor_without_grid_charge_marker_is_gated(monkeypatch):
    """The charge marker is in-memory only: without it a booked charge could read as drift."""
    hass = _StubHass()
    ctrl, _, _ = _make_ctrl(hass, cfg=_drift_cfg())
    ctrl._soc_drift_last_soc_pct = 60.0
    ctrl._soc_drift_last_update = BASE - timedelta(minutes=1)
    ctrl._soc_drift_last_intervals = [ForecastInterval(BASE, 5000.0, 100.0, 3.0)]
    ctrl._soc_drift_day = ctrl_mod.dt_util.as_local(BASE).date().isoformat()
    assert ctrl._soc_drift_last_grid_charge_kwh is None

    monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
    _seed_inputs(hass, soc="60.0", now=BASE)
    monkeypatch.setattr(
        ctrl_mod, "compute_decision", _make_stub(intervals=[ForecastInterval(BASE, 5000.0, 100.0, 3.0)])
    )
    await ctrl.tick()

    assert ctrl._soc_drift_kwh == pytest.approx(0.0)
    assert ctrl._soc_drift_last_grid_charge_kwh is not None
