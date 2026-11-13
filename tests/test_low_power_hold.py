"""Low-power hold: rule, dwell/hysteresis, executor actuation, simulation."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.anker_x1_smartgrid import const, executor, optimize, plan, scheduler
from custom_components.anker_x1_smartgrid import controller as ctrl_mod
from custom_components.anker_x1_smartgrid.actuator import Actuator
from custom_components.anker_x1_smartgrid.models import (
    Config,
    ControllerState,
    ExportState,
    ForecastInterval,
    PlanState,
    PlantInputs,
    PriceSlot,
)
from tests.conftest import ANKER_TEST_ENTITIES
from tests.helpers import StubActuator, StubHass, make_config, make_controller
from tests.test_actuator import hass_stub

T = datetime(2026, 11, 12, 1, 0, tzinfo=UTC)
MARGIN = const.LOW_POWER_HOLD_RELEASE_MARGIN_EUR_PER_KWH


class _Curve:
    """Low powers discharge at 0.6, everything else at 0.95."""

    def eta_discharge(self, w: float) -> float:
        return 0.6 if w < 500.0 else 0.95

    def eta_charge(self, _w: float) -> float:
        return 1.0


# ── rule ────────────────────────────────────────────────────────────────────


def test_rule_holds_below_stored_value_not_at_boundary():
    cfg = Config()
    # 0.35 × 0.6 = 0.21 €/DC-kWh
    assert optimize.discharge_value(0.35, 250.0, cfg, _Curve()) == pytest.approx(0.21)
    assert optimize.holds_at(0.35, 250.0, 0.22, cfg, _Curve())
    assert not optimize.holds_at(0.35, 250.0, 0.21, cfg, _Curve())
    # Same price at a large load discharges efficiently: no hold.
    assert not optimize.holds_at(0.35, 2000.0, 0.22, cfg, _Curve())


def test_rule_never_holds_without_a_deficit():
    assert not optimize.holds_at(0.35, 0.0, 10.0, Config(), _Curve())
    assert not optimize.holds_at(0.35, -300.0, 10.0, Config(), _Curve())


# ── dwell / hysteresis ──────────────────────────────────────────────────────


def _decide(prev, dv, sv, now=T, **cfg):
    return scheduler.decide_hold_state(
        prev, discharge_value=dv, stored_value=sv, now=now, cfg=Config(export_dwell_min=15, **cfg)
    )


def test_engages_below_stored_value_after_dwell():
    prev = ExportState(engaged=False, state_since=T - timedelta(hours=1))
    assert _decide(prev, 0.20, 0.25) == ExportState(engaged=True, state_since=T)
    assert _decide(prev, 0.25, 0.25) is prev


def test_release_needs_the_margin():
    prev = ExportState(engaged=True, state_since=T - timedelta(hours=1))
    assert _decide(prev, 0.25 + MARGIN, 0.25) is prev
    assert _decide(prev, 0.25 + MARGIN + 0.001, 0.25) == ExportState(engaged=False, state_since=T)


def test_dwell_holds_both_economic_flips():
    held = ExportState(engaged=True, state_since=T - timedelta(minutes=5))
    idle = ExportState(engaged=False, state_since=T - timedelta(minutes=5))
    assert _decide(held, 0.90, 0.25) is held
    assert _decide(idle, 0.01, 0.25) is idle


def test_blocked_releases_immediately_inside_dwell():
    held = ExportState(engaged=True, state_since=T - timedelta(minutes=1))
    assert _decide(held, None, 0.25) == ExportState(engaged=False, state_since=T)
    assert _decide(held, 0.10, None) == ExportState(engaged=False, state_since=T)
    idle = ExportState(engaged=False, state_since=T - timedelta(hours=1))
    assert _decide(idle, None, 0.25) is idle


# ── hold_values gating ──────────────────────────────────────────────────────

_SLOTS = [PriceSlot(T, 0.35)]


def _gate_ctrl(**over):
    c = SimpleNamespace(
        cfg=Config(low_power_hold=True, soc_floor=5.0),
        export_state=ExportState(engaged=False, state_since=T - timedelta(hours=1)),
        _hold_deficit_w=[(T - timedelta(minutes=2), 250.0), (T, 250.0)],
        _planner_curve=lambda: _Curve(),
    )
    for k, v in over.items():
        setattr(c, k, v)
    return c


def _values(c=None, *, plan_state=ControllerState.PASSIVE, dp_out=None, slots=_SLOTS):
    return executor.hold_values(
        c or _gate_ctrl(),
        T,
        PlanState(plan_state, T, ()),
        slots,
        {"water_value": 0.25} if dp_out is None else dp_out,
        60,
    )


def test_values_compare_price_times_eta_at_mean_deficit():
    assert _values() == (pytest.approx(0.21), 0.25)


def test_rolling_mean_ignores_spikes_and_stale_samples():
    deficits = [
        (T - timedelta(minutes=30), 5000.0),  # outside the window
        (T - timedelta(minutes=3), 300.0),
        (T - timedelta(minutes=2), 300.0),
        (T - timedelta(minutes=1), 600.0),  # kettle
        (T, 300.0),
    ]
    # mean 375 W stays in the low-power bin
    assert _values(_gate_ctrl(_hold_deficit_w=deficits))[0] == pytest.approx(0.35 * 0.6)


@pytest.mark.parametrize(
    "ctrl_over, kw",
    [
        ({"cfg": Config(low_power_hold=False)}, {}),
        ({}, {"plan_state": ControllerState.FORCING}),
        ({"export_state": ExportState(engaged=True, state_since=T)}, {}),
        ({}, {"dp_out": {"water_value": 0.25, "export_request": {T: 800.0}}}),
        ({"_hold_deficit_w": [(T - timedelta(minutes=1), 250.0), (T, -300.0)]}, {}),  # PV covers the house on the mean
        ({"_hold_deficit_w": [(T - timedelta(minutes=6), 250.0)]}, {}),  # house load stale for the whole window
        ({"_hold_deficit_w": []}, {}),
        ({}, {"slots": []}),
    ],
)
def test_blockers(ctrl_over, kw):
    assert _values(_gate_ctrl(**ctrl_over), **kw)[0] is None


def test_passing_blips_keep_the_hold():
    # One PV-surplus sample or a few ticks without a fresh house-load sample don't release.
    assert _values(_gate_ctrl(_hold_deficit_w=[(T - timedelta(minutes=1), 250.0), (T, -50.0)]))[0] is not None
    assert _values(_gate_ctrl(_hold_deficit_w=[(T - timedelta(minutes=4), 250.0)]))[0] is not None


def test_no_water_value_blocks():
    assert _values(dp_out={}) == (None, None)


# ── executor actuation ──────────────────────────────────────────────────────


def _exec_ctrl(act=None, **cfg_over):
    ctrl, act = make_controller(StubHass(), actuator=act)
    ctrl.cfg = Config(
        low_power_hold=True,
        soc_floor=5.0,
        export_dwell_min=0,
        enable_export=False,
        round_trip_eff=0.6,  # static eta_d 0.6 with eta_charge 1.0
        eta_charge=1.0,
        **cfg_over,
    )
    ctrl._house_load_fresh = True
    ctrl._hold_deficit_w.append((T, 250.0))
    ctrl.hold_state = ctrl.export_state = ExportState(engaged=False, state_since=T - timedelta(hours=1))
    return ctrl, act


async def _run(ctrl, *, water_value=0.25, new_state=ControllerState.PASSIVE):
    return await executor.run_forcing_and_export(
        ctrl,
        T,
        PlanState(new_state, T, ()),
        PlantInputs(soc=50.0, meter_w=250.0, now=T),
        _SLOTS,
        {"water_value": water_value},
        [],
        60,
        None,
        250.0,
    )


async def test_hold_engages_without_release():
    ctrl, act = _exec_ctrl()
    await _run(ctrl)
    assert act.calls == [("engage_hold",)]
    assert ctrl.hold_state.engaged
    assert ctrl.last_hold_values == (pytest.approx(0.21), 0.25)


async def test_forcing_to_hold_skips_the_release():
    ctrl, act = _exec_ctrl()
    ctrl.plan = PlanState(ControllerState.FORCING, T - timedelta(hours=1), ())
    await _run(ctrl)
    assert act.calls == [("engage_hold",)]


async def test_forcing_blocks_hold_and_charges():
    ctrl, act = _exec_ctrl()
    ctrl.hold_state = ExportState(engaged=True, state_since=T - timedelta(hours=1))
    await _run(ctrl, new_state=ControllerState.FORCING)
    assert [c[0] for c in act.calls] == ["engage_and_charge"]
    assert not ctrl.hold_state.engaged


async def test_hold_exit_releases_once():
    ctrl, act = _exec_ctrl()
    ctrl.hold_state = ExportState(engaged=True, state_since=T - timedelta(hours=1))
    await _run(ctrl, water_value=0.21 - MARGIN - 0.001)
    assert act.calls == [("release_to_self",)]
    assert not ctrl.hold_state.engaged


async def test_failed_hold_exit_release_is_retried():
    ctrl, act = _exec_ctrl(StubActuator(fail_on={"release_to_self"}))
    ctrl.hold_state = ExportState(engaged=True, state_since=T - timedelta(hours=1))
    act.engaged = True
    await _run(ctrl, water_value=0.21 - MARGIN - 0.001)
    assert act.calls == [("release_to_self_attempt",)]
    assert ctrl.hold_state.engaged
    act.fail_on = set()
    act.calls.clear()
    await _run(ctrl, water_value=0.21 - MARGIN - 0.001)
    assert act.calls == [("release_to_self",)]
    assert not ctrl.hold_state.engaged and not act.engaged


async def test_hold_engage_failure_releases():
    ctrl, act = _exec_ctrl(StubActuator(fail_on={"engage_hold"}))
    await _run(ctrl)
    assert act.calls == [("engage_hold_attempt",), ("release_to_self",)]
    assert not ctrl.hold_state.engaged


async def test_feature_off_is_untouched_passive():
    ctrl, act = _exec_ctrl()
    ctrl.cfg = replace(ctrl.cfg, low_power_hold=False)
    out = await _run(ctrl)
    assert act.calls == []
    assert out == (0.0, False, None, None, None, None)
    assert not ctrl.hold_state.engaged


@pytest.mark.parametrize("fails", [False, True])
async def test_safe_release_clears_hold_state_only_on_success(fails):
    ctrl, _ = _exec_ctrl(StubActuator(fail_on={"release_to_self"} if fails else None))
    ctrl.hold_state = ExportState(engaged=True, state_since=T - timedelta(hours=1))
    assert await executor.safe_release(ctrl, T, "x") is not fails
    assert ctrl.hold_state.engaged is fails


async def test_hold_state_persists():
    ctrl, _ = _exec_ctrl()
    ctrl.hold_state = ExportState(engaged=True, state_since=T)
    saved = {}

    async def _save(data):
        saved.update(data)

    ctrl._store = SimpleNamespace(async_save=_save)
    await ctrl._persist()
    ctrl2, _ = _exec_ctrl()
    ctrl2.restore(saved)
    assert ctrl2.hold_state == ExportState(engaged=True, state_since=T)


# ── actuator ────────────────────────────────────────────────────────────────


async def test_actuator_engage_hold_writes_zero_under_vpp(hass_stub):
    act = Actuator(hass_stub, {**const.DEFAULT_ENTITIES, **ANKER_TEST_ENTITIES})
    act.last_setpoint_w = -3000.0
    await act.engage_hold()
    calls = [(c[0], c[1]) for c in hass_stub.services.calls]
    assert calls == [("switch", "turn_on"), ("number", "set_value")]
    assert hass_stub.services.calls[1][2]["value"] == 0.0
    assert act.engaged and act.last_setpoint_w == 0.0


# ── simulation: DP ──────────────────────────────────────────────────────────


def _dp(hold):
    cfg = make_config(soc_floor=5.0, soc_target=100.0, idle_drain_w=0.0)
    n = 4
    return optimize.optimize_grid(
        [0.0] * n,
        [0.25] * n,
        [0.30, 0.30, 0.10, 0.30],
        50.0,
        cfg,
        window_start_h=0,
        window_len=n,
        terminal_mode="water_value",
        water_value=0.20,
        hold=hold,
    )


def test_dp_all_false_mask_is_identical_to_none():
    assert _dp([False] * 4) == _dp(None)


def test_dp_held_slots_import_the_deficit():
    base, held = _dp(None), _dp([True, True, False, False])
    # 2 slots × 0.25 kWh at 0.30 now come from the grid.
    assert held["kwh"] == pytest.approx(base["kwh"] + 0.5)
    assert held["eur"] == pytest.approx(base["eur"] + 0.5 * 0.30)


def test_dp_held_slots_pay_standby_drain():
    def held(idle_w):
        cfg = make_config(soc_floor=5.0, soc_target=100.0, idle_drain_w=idle_w)
        # At the firmware floor every held kWh, deficit and standby alike, is a floor import.
        return optimize.optimize_grid(
            [0.0] * 4,
            [0.25] * 4,
            [0.30] * 4,
            5.0,
            cfg,
            window_start_h=0,
            window_len=4,
            terminal_mode="water_value",
            water_value=0.0,
            hold=[True] * 4,
        )

    assert held(100.0)["kwh"] == pytest.approx(held(0.0)["kwh"] + 4 * 0.1)


# ── simulation: plan display ────────────────────────────────────────────────


def _horizon(hold_value, export_request=None, **cfg_over):
    cfg = make_config(soc_floor=5.0, soc_target=100.0, round_trip_eff=0.6, **cfg_over)
    slots = [PriceSlot(T + timedelta(hours=i), p) for i, p in enumerate([0.35, 0.60])]
    ivs = [ForecastInterval(s.start, 0.0, 250.0, 1.0) for s in slots]
    return plan.build_plan_horizon(
        slots, ivs, [], 50.0, T + timedelta(hours=2), cfg, export_request_by_hour=export_request, hold_value=hold_value
    )


def test_plan_holds_cheap_slot_and_discharges_expensive_one():
    held = _horizon(0.25)  # 0.35×0.6=0.21 holds; 0.60×0.6=0.36 discharges
    assert held[0]["self_discharge_w"] == 0.0 and held[0]["soc"] == pytest.approx(50.0)
    assert held[1]["self_discharge_w"] == 250.0
    assert [r["mode"] for r in held] == ["hold", "idle"]
    off = _horizon(None)
    assert off[0]["self_discharge_w"] == 250.0 and off[0]["soc"] < 50.0
    assert [r["mode"] for r in off] == ["idle", "idle"]


def test_plan_held_slot_pays_standby_drain():
    assert _horizon(0.25, idle_drain_w=100.0)[0]["soc"] == pytest.approx(49.0)  # 0.1 kWh of 10


def test_plan_export_slot_never_holds():
    row = _horizon(0.25, export_request={T: 2000.0})[0]
    assert row["self_discharge_w"] == 250.0 and row["grid_export_w"] == 2000.0 and row["mode"] == "export"


# ── soc drift ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("holding, drifts", [(True, False), (False, True)])
def test_held_step_expects_only_standby_drain(monkeypatch, holding, drifts):
    monkeypatch.setattr(ctrl_mod.dt_util, "now", lambda: T)
    ctrl, _ = _exec_ctrl(soc_hedge_fraction=0.5, capacity_kwh=10.0, idle_drain_w=600.0)
    ctrl._soc_drift_day = ctrl_mod.dt_util.as_local(T).date().isoformat()
    ctrl._soc_drift_last_soc_pct = 50.0
    ctrl._soc_drift_last_update = T - timedelta(minutes=1)
    ctrl._soc_drift_last_intervals = [ForecastInterval(T - timedelta(minutes=30), 0.0, 1000.0, 1.0)]
    ctrl.hold_state = ExportState(engaged=holding, state_since=T - timedelta(hours=1))
    # 600 W standby for one minute = 0.01 kWh = 0.1 % of 10 kWh
    ctrl._apply_drift_hedge(T, PlantInputs(soc=49.9, meter_w=1000.0, now=T), _SLOTS, T + timedelta(hours=8))
    assert (abs(ctrl._soc_drift_kwh) > 1e-9) is drifts


# ── full tick ───────────────────────────────────────────────────────────────


def _tick_ctrl(monkeypatch, **cfg_over):
    from tests.test_controller_export_executor import (
        BASE,
        _make_controller,
        _patched_compute_decision,
        _seed_passive_inputs,
    )

    monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
    hass = StubHass()
    ctrl, act, _ = _make_controller(hass, cfg_overrides={"low_power_hold": True, "enable_export": False, **cfg_over})
    ctrl.hold_state = ExportState(engaged=False, state_since=BASE - timedelta(hours=1))
    _seed_passive_inputs(hass)  # pv 2000 W + 300 W import → 300 W deficit at €0.30, static eta_d 1.0
    stub = _patched_compute_decision(export_request={})

    def _cd(*a, _out=None, **k):
        out = stub(*a, _out=_out, **k)
        if _out is not None:
            _out["water_value"] = 0.40
        return out

    monkeypatch.setattr(ctrl_mod, "compute_decision", _cd)
    return ctrl, act


async def test_tick_records_hold_and_publishes_both_values(monkeypatch):
    ctrl, act = _tick_ctrl(monkeypatch)
    await ctrl.tick()
    assert ("engage_hold",) in act.calls
    assert ctrl._recorder.rows[-1]["state"] == "hold"
    assert ctrl.last_status["low_power_hold_active"] is True
    assert ctrl.last_status["hold_discharge_value_eur_per_kwh"] == pytest.approx(0.30)
    assert ctrl.last_status["hold_stored_value_eur_per_kwh"] == 0.40


async def test_tick_feature_off_records_passive(monkeypatch):
    ctrl, act = _tick_ctrl(monkeypatch, low_power_hold=False)
    await ctrl.tick()
    assert act.calls == []
    assert ctrl._recorder.rows[-1]["state"] == "passive"
    assert ctrl.last_status["low_power_hold_active"] is False


async def test_restart_while_holding_and_disabled_releases(monkeypatch):
    ctrl, act = _tick_ctrl(monkeypatch)
    ctrl.hold_state = ExportState(engaged=True, state_since=T)  # restored from the store
    ctrl.enabled = False
    await ctrl.tick()
    assert ("release_to_self",) in act.calls
    assert not ctrl.hold_state.engaged


@pytest.mark.parametrize("enabled", [True, False])
def test_dp_select_slots_builds_the_mask_from_slot_price_and_load(monkeypatch, enabled):
    captured = {}
    real = ctrl_mod.optimize_mod.optimize_grid

    def _spy(*a, **k):
        captured["hold"] = k.get("hold")
        return real(*a, **k)

    monkeypatch.setattr(ctrl_mod.optimize_mod, "optimize_grid", _spy)
    slots = [PriceSlot(T + timedelta(hours=i), p) for i, p in enumerate([0.35, 0.60, 0.35])]
    loads = [250.0, 250.0, 2000.0]
    ivs = [ForecastInterval(s.start, 0.0, w, 1.0) for s, w in zip(slots, loads, strict=True)]
    cfg = make_config(soc_floor=5.0, round_trip_eff=0.6, low_power_hold=enabled)
    ctrl_mod._dp_select_slots(
        inputs=PlantInputs(soc=50.0, meter_w=0.0, now=T),
        slots=slots,
        deadline=T + timedelta(hours=3),
        ceiling=0.20,
        cfg=cfg,
        export_price=None,
        water_value=0.25,
        intervals=ivs,
    )
    # static eta_d is 0.6 at every power: €0.35 slots hold (0.21 < 0.25), €0.60 does not (0.36)
    assert captured["hold"] == ([True, False, True] if enabled else None)
