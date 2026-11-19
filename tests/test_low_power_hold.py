"""Low-power hold: rule, dwell/hysteresis, executor actuation, simulation."""

from __future__ import annotations

import itertools
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.anker_x1_smartgrid import const, executor, optimize, plan, scheduler
from custom_components.anker_x1_smartgrid import controller as ctrl_mod
from custom_components.anker_x1_smartgrid.actuator import Actuator
from custom_components.anker_x1_smartgrid.efficiency import EfficiencyCurve
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
    cfg = Config(cycle_cost_eur_per_kwh=0.0)
    # 0.35 × 0.6 = 0.21 €/DC-kWh
    assert optimize.discharge_value(0.35, 250.0, cfg, _Curve()) == pytest.approx(0.21)
    assert optimize.holds_at(0.35, 250.0, 0.22, cfg, _Curve())
    assert not optimize.holds_at(0.35, 250.0, 0.21, cfg, _Curve())
    # Same price at a large load discharges efficiently: no hold.
    assert not optimize.holds_at(0.35, 2000.0, 0.22, cfg, _Curve())


def test_rule_never_holds_without_a_deficit():
    assert not optimize.holds_at(0.35, 0.0, 10.0, Config(), _Curve())
    assert not optimize.holds_at(0.35, -300.0, 10.0, Config(), _Curve())


def test_rule_counts_the_standby_on_both_sides():
    """Discharging 250 W at eta_conv 0.85 spends 250/0.85 + 100 = 394.1 W of DC
    and buys nothing; holding spends no DC but buys 250 + 100 = 350 W, because
    the standby keeps running and the meter covers it. So a stored kWh is worth
    350/394.1 of the price -- the standby barely dilutes it."""
    curve = SimpleNamespace(eta_discharge=lambda _w: 0.85, eta_charge=lambda _w: 1.0)
    v = optimize.discharge_value(1.0, 250.0, Config(idle_drain_w=100.0, cycle_cost_eur_per_kwh=0.0), curve)
    assert v == pytest.approx(350.0 / (250.0 / 0.85 + 100.0), abs=1e-6)
    assert v == pytest.approx(0.888, abs=5e-4)
    # No modeled drain -> the bare conversion eta, as before.
    assert optimize.discharge_value(1.0, 250.0, Config(cycle_cost_eur_per_kwh=0.0), curve) == pytest.approx(0.85)


def test_rule_nets_the_wear_of_discharging():
    """Discharging pays wear on every DC kWh it draws; holding pays none."""
    cfg = Config(cycle_cost_eur_per_kwh=0.03)
    assert optimize.discharge_value(0.35, 250.0, cfg, _Curve()) == pytest.approx(0.21 - 0.03)
    assert optimize.holds_at(0.35, 250.0, 0.19, cfg, _Curve())


def test_live_rule_and_dp_read_the_same_value():
    cfg = Config(soc_floor=5.0, idle_drain_w=100.0, cycle_cost_eur_per_kwh=0.0)
    boundary = optimize.discharge_value(0.35, 250.0, cfg, _Curve())
    assert boundary == pytest.approx(0.35 * 350.0 / (250.0 / 0.6 + 100.0))
    assert _values(_gate_ctrl(cfg=cfg), dp_out={"water_value": boundary, "held_slots": frozenset({T})})[
        0
    ] == pytest.approx(boundary)
    assert optimize.holds_at(0.35, 250.0, boundary + 1e-9, cfg, _Curve())
    assert not optimize.holds_at(0.35, 250.0, boundary, cfg, _Curve())


def test_live_rule_cannot_hold_a_slot_the_dp_declined():
    """The DP weighed this slot against the whole horizon; the live rule may
    only refine its choice. Declining is an economic flip, so it waits out the
    dwell instead of slamming the work mode on a DP that changes its mind."""
    held = ExportState(engaged=True, state_since=T - timedelta(hours=1))
    idle = ExportState(engaged=False, state_since=T - timedelta(hours=1))
    # 0.20 < 0.25: the live rule on its own would hold.
    assert _decide(idle, 0.20, 0.25, dp_holds=False) is idle
    assert _decide(held, 0.20, 0.25, dp_holds=False) == ExportState(engaged=False, state_since=T)
    # ...but not before the dwell has run.
    fresh = ExportState(engaged=True, state_since=T - timedelta(minutes=1))
    assert _decide(fresh, 0.20, 0.25, dp_holds=False) is fresh
    # In a slot the DP did choose, the live rule still decides (a load spike releases).
    assert _decide(idle, 0.20, 0.25, dp_holds=True) == ExportState(engaged=True, state_since=T)


def test_no_dp_hold_trajectory_blocks():
    # DP published no hold schedule at all (no water value): the
    # live rule must not invent one.
    assert _values(dp_out={"water_value": 0.25})[0] is None


# ── dwell / hysteresis ──────────────────────────────────────────────────────


def _decide(prev, dv, sv, now=T, dp_holds=True, paused=False, **cfg):
    return scheduler.decide_hold_state(
        prev,
        discharge_value=dv,
        stored_value=sv,
        now=now,
        cfg=Config(export_dwell_min=15, **cfg),
        dp_holds=dp_holds,
        paused=paused,
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
        cfg=Config(soc_floor=5.0, cycle_cost_eur_per_kwh=0.0),
        export_state=ExportState(engaged=False, state_since=T - timedelta(hours=1)),
        _hold_deficit_w=[(T - timedelta(minutes=2), 250.0), (T, 250.0)],
        _planner_curve=lambda: _Curve(),
    )
    for k, v in over.items():
        setattr(c, k, v)
    return c


def _values(c=None, *, plan_state=ControllerState.PASSIVE, dp_out=None, slots=_SLOTS, soc=50.0):
    return executor.hold_values(
        c or _gate_ctrl(),
        T,
        PlanState(plan_state, T, ()),
        slots,
        {"water_value": 0.25, "held_slots": frozenset({T})} if dp_out is None else dp_out,
        60,
        soc,
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


def test_ceiling_blocks_the_hold_without_headroom():
    """At or above this slot's solar-reservation ceiling the kept kWh is one
    tomorrow's sun will spill, so the hold is not allowed however high the
    water value is — same ceiling the DP's held branch and the charge-stop use."""
    dp_out = {"water_value": 10.0, "ceiling_by_hour": {T: 60.0}, "held_slots": frozenset({T})}
    assert _values(dp_out=dp_out, soc=60.1)[0] is None
    # `>` not `>=`, matching the DP's own held branch at the boundary.
    assert _values(dp_out=dp_out, soc=60.0)[0] is not None
    # No ceiling published -> unchanged behaviour.
    assert _values(dp_out={"water_value": 10.0, "held_slots": frozenset({T})}, soc=100.0)[0] is not None


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
        soc_floor=5.0,
        export_dwell_min=0,
        enable_export=False,
        round_trip_eff=0.6,  # static eta_d 0.6 with eta_charge 1.0
        eta_charge=1.0,
        cycle_cost_eur_per_kwh=0.0,
        **cfg_over,
    )
    ctrl._eta_curve = EfficiencyCurve.static(ctrl.cfg)
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
        {"water_value": water_value, "held_slots": frozenset({T})},
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


async def test_hold_weighs_the_kept_value_the_dp_published():
    # Worth less at the end of the horizon than discharging now, but kept for a richer later slot.
    ctrl, act = _exec_ctrl()
    dp_out = {"water_value": 0.15, "kept_by_slot": {T: 0.30}, "held_slots": frozenset({T})}
    await executor.run_forcing_and_export(
        ctrl,
        T,
        PlanState(ControllerState.PASSIVE, T, ()),
        PlantInputs(soc=50.0, meter_w=250.0, now=T),
        _SLOTS,
        dp_out,
        [],
        60,
        None,
        250.0,
    )
    assert act.calls == [("engage_hold",)]
    # The status reports the value the rule weighed.
    assert ctrl.last_hold_values == (pytest.approx(0.21), 0.30)


async def test_a_top_up_the_plan_does_not_force_holds_instead_of_draining():
    # The DP charges this slot, but at 99 % the charge stop keeps the plan PASSIVE.
    ctrl, act = _exec_ctrl()
    dp_out = {"water_value": 0.25, "held_slots": frozenset(), "grid_request": {T: 900.0}}
    await executor.run_forcing_and_export(
        ctrl,
        T,
        PlanState(ControllerState.PASSIVE, T, ()),
        PlantInputs(soc=99.0, meter_w=250.0, now=T),
        _SLOTS,
        dp_out,
        [],
        60,
        None,
        250.0,
    )
    assert act.calls == [("engage_hold",)]


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


@pytest.mark.parametrize("held_for", [timedelta(hours=1), timedelta(minutes=2)])
async def test_a_top_up_burst_hands_straight_back_to_the_hold(held_for):
    # 98 %: a cheap slot forces a burst that hits the 99 % charge stop within two minutes. Releasing
    # then would feed the house from the pack until the dwell ran out, and the next cheap slot repeats it.
    ctrl, act = _exec_ctrl()
    ctrl.cfg = replace(ctrl.cfg, export_dwell_min=15)
    ctrl.hold_state = ExportState(engaged=True, state_since=T - held_for)
    dp_out = {"water_value": 0.25, "held_slots": frozenset({T})}
    for now, soc, state in (
        (T, 98.0, ControllerState.FORCING),
        (T + timedelta(minutes=2), 99.0, ControllerState.PASSIVE),
    ):
        new_plan = PlanState(state, now, ())
        inputs = PlantInputs(soc=soc, meter_w=250.0, now=now)
        await executor.run_forcing_and_export(ctrl, now, new_plan, inputs, _SLOTS, dp_out, [], 60, None, 250.0)
        ctrl.plan = new_plan
    assert [c[0] for c in act.calls] == ["engage_and_charge", "engage_hold"]
    assert ctrl.hold_state.engaged


def test_a_forced_charge_pauses_the_hold_without_a_dwell():
    held = ExportState(engaged=True, state_since=T - timedelta(minutes=2))
    paused = _decide(held, None, 0.25, paused=True)
    assert not paused.engaged
    assert _decide(paused, 0.20, 0.25, now=T + timedelta(minutes=1)).engaged


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


def _dp(hold, *, water_value=0.20, soc=50.0, idle_drain_w=0.0, cycle_cost=0.0, **kw):
    cfg = make_config(soc_floor=5.0, soc_target=100.0, idle_drain_w=idle_drain_w, cycle_cost_eur_per_kwh=cycle_cost)
    n = 4
    return optimize.optimize_grid(
        [0.0] * n,
        [0.25] * n,
        [0.30, 0.30, 0.10, 0.30],
        soc,
        cfg,
        window_start_h=0,
        window_len=n,
        terminal_mode="water_value",
        water_value=water_value,
        hold=hold,
        **kw,
    )


def test_dp_all_false_mask_is_identical_to_none():
    out = _dp([False] * 4)
    assert out.pop("hold_schedule") == [False] * 4
    assert out == _dp(None)


def test_dp_declines_a_hold_that_does_not_pay():
    """The mask only ALLOWS a hold. Buying 0.25 kWh at 0.30 to keep a kWh worth
    0.20 is a loss, so the DP turns it down and the bill is untouched."""
    out = _dp([True, True, False, False])
    assert out["hold_schedule"] == [False] * 4
    base = _dp(None)
    assert out["kwh"] == pytest.approx(base["kwh"])
    assert out["eur"] == pytest.approx(base["eur"])


def test_dp_takes_a_hold_that_pays():
    """Same window with stored energy worth more than the hour's price: now the
    deficit is bought instead of drawn from the pack."""
    out = _dp([True, True, False, False], water_value=0.60, chargeable=[False] * 4)
    assert out["hold_schedule"] == [True, True, False, False]
    base = _dp(None, water_value=0.60, chargeable=[False] * 4)
    assert out["kwh"] == pytest.approx(base["kwh"] + 0.5)
    assert out["eur"] == pytest.approx(base["eur"] + 0.5 * 0.30)


def test_dp_holds_where_discharging_would_cycle_the_pack_twice():
    """Covering 0.30 from a kWh worth 0.25 at the horizon end saves 0.05, but pays 0.10 of
    wear now that a refilled kWh pays again later: with wear the hold wins."""
    mask = [True, True, False, False]
    assert _dp(mask, water_value=0.25, chargeable=[False] * 4)["hold_schedule"] == [False] * 4
    worn = _dp(mask, water_value=0.25, chargeable=[False] * 4, cycle_cost=0.10)
    assert worn["hold_schedule"] == [True, True, False, False]


def test_dp_never_holds_above_the_solar_reservation():
    """Holding above the solar-reservation ceiling parks the pack in the way of
    this cycle's forecast PV exactly as grid-charging above it would, so the
    held branch is not offered there — whatever the water value claims."""
    kw = dict(chargeable=[False] * 4, grid_charge_ceiling=[5.0] * 4, water_value=10.0, soc=80.0)
    out = _dp([True] * 4, **kw)  # 8 kWh of a 10 kWh pack, ceiling 5 kWh
    assert out["hold_schedule"] == [False] * 4
    assert out["kwh"] == pytest.approx(_dp(None, **kw)["kwh"])
    # Below the ceiling the same window does hold.
    assert _dp([True] * 4, **{**kw, "soc": 40.0})["hold_schedule"] == [True] * 4


def test_dp_declines_to_hold_through_a_sunny_day():
    """End to end on the real solar-reservation ceiling: a full pack in the
    evening with a big PV day coming must discharge, not import at full price
    and then spill tomorrow's free solar. Overcast, the same window holds."""

    def run(pv):
        cfg = make_config(soc_floor=5.0, soc_target=100.0, idle_drain_w=100.0, max_charge_w=5000.0)
        load, price, n = [0.3] * 12, [0.42] * 12, 12
        wv = optimize.compute_water_value(min(price), cfg)
        mask = [optimize.holds_at(price[i], (load[i] - pv[i]) * 1000.0, wv, cfg) for i in range(n)]
        ceiling = optimize.solar_reservation_ceiling(pv, load, cfg, cycle_end_idx=[n] * n, dt_h=1.0)
        out = optimize.optimize_grid(
            pv,
            load,
            price,
            85.0,
            cfg,
            window_start_h=17,
            window_len=n,
            terminal_mode="water_value",
            water_value=wv,
            grid_charge_ceiling=ceiling,
            chargeable=[False] * n,  # isolate the hold choice from refilling
            hold=mask,
        )
        return sum(mask), out

    sunny_allowed, sunny = run([0.0] * 8 + [3.0, 5.0, 4.0, 2.0])
    dark_allowed, dark = run([0.0] * 12)
    assert sunny_allowed >= 7 and dark_allowed == 12  # the rule allows a hold either way
    assert sum(sunny["hold_schedule"]) == 0  # ...the DP only takes it when the pack has nowhere else to go
    assert sum(dark["hold_schedule"]) > 0
    assert sunny["kwh"] < dark["kwh"]


# ── smoothing ───────────────────────────────────────────────────────────


def _flips(mask):
    return sum(1 for a, b in itertools.pairwise(mask) if a != b)


def test_min_run_comes_out_of_the_shipped_config():
    """Fed the CONFIGURED dwell: the clamped one caps at slot_minutes-1, which
    would put the threshold at 1 slot at every resolution and smooth nothing."""
    cfg = Config()  # whatever the integration actually ships
    assert optimize.hold_min_run_slots(cfg.export_dwell_min, 60) == 1  # hourly: no-op
    assert optimize.hold_min_run_slots(cfg.export_dwell_min, 15) == 2
    assert optimize.hold_min_run_slots(cfg.export_dwell_min, 15) > optimize.hold_min_run_slots(
        scheduler._effective_dwell_min(cfg.export_dwell_min, 15, label="x"), 15
    )


def test_dp_select_slots_smooths_a_noisy_night_at_15min(monkeypatch):
    """End to end through the real _dp_select_slots with the shipped Config: the
    published held_slots must be smoothed, not the raw slot-by-slot choice."""
    n = 24
    sched = [c == "H" for c in "HHHH.HHHH..HHH.HHHH.HHHH"]
    monkeypatch.setattr(
        ctrl_mod.optimize_mod,
        "optimize_grid",
        lambda *a, **k: {"schedule": [0.0] * n, "kwh": 0.0, "eur": 0.0, "hold_schedule": list(sched)},
    )
    now = T.replace(minute=0)
    out = ctrl_mod._dp_select_slots(
        inputs=PlantInputs(soc=50.0, meter_w=250.0, now=now),
        slots=[PriceSlot(now + timedelta(minutes=15 * i), 0.26) for i in range(n)],
        deadline=now + timedelta(minutes=15 * n),
        ceiling=0.30,
        cfg=Config(soc_floor=5.0, capacity_kwh=20.0),
        intervals=[ForecastInterval(now + timedelta(minutes=15 * i), 0.0, 320.0, 0.25) for i in range(n)],
        export_price=None,
        slot_minutes=15,
        dt_h=0.25,
        terminal_mode="water_value",
        water_value=0.30,
    )
    held = out[6]
    chosen = [now + timedelta(minutes=15 * i) in held for i in range(n)]
    assert _flips(chosen) < _flips(sched)
    assert chosen == optimize.smooth_hold_runs([False, *sched], 2)[1:]


def test_smoothing_collapses_a_noisy_hold_pattern():
    """Price noise of a few hundredths makes the raw DP choice alternate slot by
    slot; each flip is a work-mode write the dwell cannot absorb."""
    raw = [c == "H" for c in "H.HHHHH.HH.HHHHHH..HHH."]
    smooth = optimize.smooth_hold_runs(raw, 2)
    assert _flips(raw) == 9
    assert _flips(smooth) == 3
    assert optimize.smooth_hold_runs(raw, 1) == raw  # min_run<=1 is a no-op


def _live_walk(raw, min_run):
    """Replan at every slot, as the box does: the live rule only ever reads slot 0."""
    live, engaged = [], False
    for i in range(len(raw)):
        engaged = optimize.smooth_hold_runs([engaged, *raw[i:]], min_run)[1:][0]
        live.append(engaged)
    return live


def test_live_walk_matches_the_plan_it_was_smoothed_from():
    """The horizon is a left edge. Without the engaged hold as slot -1 an edge gap
    is never filled and an edge run is always dropped, so replanning each slot
    shaves the last slot off every run and the card shows holds the box skips."""
    raw = [c == "H" for c in "HHHH.HHHH..HHH.HH"]
    plan = optimize.smooth_hold_runs([False, *raw], 2)[1:]
    assert _live_walk(raw, 2) == plan
    assert _flips(plan) == 2 and _flips(raw) == 6


def test_live_walk_keeps_each_run_whole_at_a_longer_dwell():
    raw = [c == "H" for c in "HHHH.HHHH..HHH.HH"]
    live = _live_walk(raw, 3)
    # Only the final slot can differ: there the receding horizon has nothing left
    # to anchor a full run against.
    assert live[:-1] == optimize.smooth_hold_runs([False, *raw], 3)[1:][:-1]
    assert _flips(live) <= _flips(raw)


def test_smoothing_keeps_long_runs_and_drops_lone_slots():
    assert optimize.smooth_hold_runs([False, True, False, False, False], 2) == [False] * 5
    assert optimize.smooth_hold_runs([True, True, False, False, True, True], 2) == [
        True,
        True,
        False,
        False,
        True,
        True,
    ]


def test_noisy_prices_do_not_churn_the_work_mode():
    """End to end on the DP: +-0.3 ct of noise on a flat night, 15-min slots."""
    cfg = make_config(soc_floor=5.0, soc_target=100.0, idle_drain_w=100.0, capacity_kwh=20.0)
    n = 24
    price = [0.26 + (0.003 if i % 3 else -0.003) for i in range(n)]
    wv = optimize.compute_water_value(min(price), cfg)
    mask = [optimize.holds_at(price[i], 320.0, wv, cfg) for i in range(n)]
    out = optimize.optimize_grid(
        [0.0] * n,
        [0.08] * n,
        price,
        60.0,
        cfg,
        window_start_h=0,
        window_len=n,
        slots_per_day=96,
        dt_h=0.25,
        terminal_mode="water_value",
        water_value=wv,
        chargeable=[False] * n,
        hold=mask,
    )
    raw = out["hold_schedule"]
    smooth = optimize.smooth_hold_runs(raw, optimize.hold_min_run_slots(cfg.export_dwell_min, 15))
    assert _flips(smooth) <= _flips(raw)
    assert _flips(smooth) <= 2  # at most one hold run per night


def test_dp_holds_at_15min_slots_with_a_curve():
    """dt_h scales the standby the held slot buys, and the eta curve is read at
    the slot's own deficit power rather than its energy."""
    cfg = make_config(soc_floor=5.0, soc_target=100.0, idle_drain_w=100.0, capacity_kwh=20.0)
    n = 8
    out = optimize.optimize_grid(
        [0.0] * n,
        [0.08] * n,  # 320 W over a 15-min slot
        [0.26] * n,
        50.0,
        cfg,
        window_start_h=0,
        window_len=n,
        slots_per_day=96,
        dt_h=0.25,
        terminal_mode="water_value",
        water_value=10.0,
        eta_curve=_Curve(),
        chargeable=[False] * n,
        hold=[True] * n,
    )
    assert out["hold_schedule"] == [True] * n
    # Per slot: 0.08 kWh deficit + 100 W x 0.25 h of standby.
    assert out["kwh"] == pytest.approx(n * (0.08 + 0.025))


def test_a_grid_charging_slot_is_not_labelled_held():
    """A hold only says "don't serve the house from the pack", so charging stays
    available in the same slot — but the slot is reported as a charge."""
    cfg = make_config(soc_floor=5.0, soc_target=100.0, idle_drain_w=100.0)
    out = optimize.optimize_grid(
        [0.0] * 3,
        [0.25] * 3,
        [0.10, 0.50, 0.50],  # cheap now, dear later -> fill the pack while holding
        50.0,
        cfg,
        window_start_h=0,
        window_len=3,
        terminal_mode="water_value",
        water_value=10.0,
        hold=[True] * 3,
    )
    # The slot both idles the pack and charges it, but the actuator can only be
    # in one work mode: a charging slot is never labelled held.
    assert out["schedule"][0] > 0.0
    assert out["hold_schedule"][0] is False
    # The house load it idles through is still bought on top of the charge.
    assert out["kwh"] > sum(out["schedule"])


def test_a_held_charge_leaves_room_for_the_house_on_the_meter():
    """Held and charging, the meter carries the house AND the charge: together they fit the connection."""
    cfg = make_config(
        soc_floor=5.0, soc_target=100.0, capacity_kwh=40.0, max_charge_w=11000.0, grid_import_limit_w=12000.0
    )
    out = optimize.optimize_grid(
        [0.0],
        [5.3],
        [0.05],
        20.0,
        cfg,
        window_start_h=0,
        window_len=1,
        terminal_mode="water_value",
        water_value=10.0,
        hold=[True],
    )
    assert out["schedule"][0] > 0.0
    assert out["kwh"] <= 12.0 + 1e-6


def test_dp_held_slot_buys_the_standby_and_keeps_the_pack():
    # chargeable=False keeps the DP from refilling in the cheap slot instead.
    kw = dict(soc=50.0, water_value=10.0, idle_drain_w=100.0, chargeable=[False] * 4)
    held = _dp([True] * 4, **kw)
    assert held["hold_schedule"] == [True] * 4
    # Deficit AND the standby the pack would have paid, both off the meter.
    assert held["kwh"] == pytest.approx(4 * (0.25 + 0.1))
    # Passively, the pack covers both and nothing is bought.
    assert _dp(None, **kw)["kwh"] == pytest.approx(0.0)


# ── simulation: plan display ────────────────────────────────────────────────


def _horizon(held_slots, export_request=None, **cfg_over):
    cfg = make_config(soc_floor=5.0, soc_target=100.0, round_trip_eff=0.6, **cfg_over)
    slots = [PriceSlot(T + timedelta(hours=i), p) for i, p in enumerate([0.35, 0.60])]
    ivs = [ForecastInterval(s.start, 0.0, 250.0, 1.0) for s in slots]
    return plan.build_plan_horizon(
        slots, ivs, [], 50.0, T + timedelta(hours=2), cfg, export_request_by_hour=export_request, held_slots=held_slots
    )


def test_plan_labels_the_slots_the_dp_chose_to_hold():
    """The plan follows the DP's chosen trajectory; it does not re-run the rule,
    so a slot the DP looked at and declined still reads as a passive discharge."""
    held = _horizon(frozenset({T}))
    assert held[0]["self_discharge_w"] == 0.0 and held[0]["soc"] == pytest.approx(50.0)
    assert held[1]["self_discharge_w"] == 250.0
    assert [r["mode"] for r in held] == ["hold", "idle"]
    off = _horizon(None)
    assert off[0]["self_discharge_w"] == 250.0 and off[0]["soc"] < 50.0
    assert [r["mode"] for r in off] == ["idle", "idle"]
    # An empty set is the DP declining every hold it was offered.
    assert [r["mode"] for r in _horizon(frozenset())] == ["idle", "idle"]


def test_plan_held_slot_loses_no_soc():
    assert _horizon(frozenset({T}), idle_drain_w=100.0)[0]["soc"] == pytest.approx(50.0)


def test_plan_feature_off_still_pays_the_drain():
    # Same slot with the hold off: passive discharge keeps the drain it always had.
    off = _horizon(None, idle_drain_w=100.0)
    assert off[0]["mode"] == "idle"
    # 250 W / eta_d 0.6 + 100 W standby, over one hour of a 10 kWh pack (soc is rounded to 0.1).
    assert off[0]["soc"] == pytest.approx(50.0 - (250.0 / 0.6 + 100.0) / 10000.0 * 100.0, abs=0.05)


def test_plan_export_slot_never_holds():
    row = _horizon(frozenset({T}), export_request={T: 2000.0})[0]
    assert row["self_discharge_w"] == 250.0 and row["grid_export_w"] == 2000.0 and row["mode"] == "export"


# ── soc drift ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "holding, soc_now, drifts",
    [(True, 50.0, False), (True, 49.9, True), (False, 49.9, True)],
)
def test_held_step_expects_a_flat_pack(monkeypatch, holding, soc_now, drifts):
    monkeypatch.setattr(ctrl_mod.dt_util, "now", lambda: T)
    ctrl, _ = _exec_ctrl(soc_hedge_fraction=0.5, capacity_kwh=10.0, idle_drain_w=600.0)
    ctrl._soc_drift_day = ctrl_mod.dt_util.as_local(T).date().isoformat()
    ctrl._soc_drift_last_soc_pct = 50.0
    ctrl._soc_drift_last_update = T - timedelta(minutes=1)
    ctrl._soc_drift_last_grid_charge_kwh = ctrl.today_grid_charge_kwh
    ctrl._soc_drift_last_intervals = [ForecastInterval(T - timedelta(minutes=30), 0.0, 1000.0, 1.0)]
    ctrl.hold_state = ExportState(engaged=holding, state_since=T - timedelta(hours=1))
    # 600 W for one minute = 0.01 kWh = 0.1 % of 10 kWh: a held pack that really
    # sagged that far is drift, a flat one is not.
    ctrl._apply_drift_hedge(T, PlantInputs(soc=soc_now, meter_w=1000.0, now=T), _SLOTS, T + timedelta(hours=8))
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
    ctrl, act, _ = _make_controller(hass, cfg_overrides={"enable_export": False, **cfg_over})
    ctrl.hold_state = ExportState(engaged=False, state_since=BASE - timedelta(hours=1))
    _seed_passive_inputs(hass)  # pv 2000 W + 300 W import → 300 W deficit at €0.30, static eta_d 1.0
    stub = _patched_compute_decision(export_request={})

    def _cd(*a, _out=None, **k):
        out = stub(*a, _out=_out, **k)
        if _out is not None:
            _out["water_value"] = 0.40
            # The DP chose to hold the current slot (full tick path reads this).
            _out["held_slots"] = frozenset({ctrl_mod.resolution.floor_to_slot(BASE, 60)})
        return out

    monkeypatch.setattr(ctrl_mod, "compute_decision", _cd)
    return ctrl, act


async def test_tick_records_hold_and_publishes_both_values(monkeypatch):
    ctrl, act = _tick_ctrl(monkeypatch, cycle_cost_eur_per_kwh=0.0)
    await ctrl.tick()
    assert ("engage_hold",) in act.calls
    assert ctrl._recorder.rows[-1]["state"] == "hold"
    assert ctrl.last_status["low_power_hold_active"] is True
    assert ctrl.last_status["hold_discharge_value_eur_per_kwh"] == pytest.approx(0.30)
    assert ctrl.last_status["hold_stored_value_eur_per_kwh"] == 0.40


async def test_executor_keys_on_the_dp_slot_at_15min_resolution(monkeypatch):
    """The executor reads `now` later in the tick than the DP did. Re-flooring it
    there lands on the NEXT slot when a tick falls on a boundary, so the hold
    looks unchosen (spurious release) and the ceiling check fails open. It must
    use the slot the DP keyed its own maps on."""
    ctrl, act = _exec_ctrl()
    boundary = T.replace(minute=15)  # exactly a 15-min slot start
    dp_slot = boundary - timedelta(minutes=15)  # the slot the DP planned for
    ctrl._hold_deficit_w.clear()
    ctrl._hold_deficit_w.append((boundary, 250.0))
    out = await executor.run_forcing_and_export(
        ctrl,
        boundary,
        PlanState(ControllerState.PASSIVE, boundary, ()),
        PlantInputs(soc=50.0, meter_w=250.0, now=boundary),
        [PriceSlot(dp_slot, 0.35), PriceSlot(boundary, 0.35)],
        {
            "water_value": 0.45,
            "held_slots": frozenset({dp_slot}),
            "ceiling_by_hour": {dp_slot: 90.0},
            "dp_slot_start": dp_slot,
        },
        [],
        15,
        None,
        250.0,
    )
    assert out is not None
    assert act.calls == [("engage_hold",)] and ctrl.hold_state.engaged


async def test_restart_while_holding_and_disabled_releases(monkeypatch):
    ctrl, act = _tick_ctrl(monkeypatch)
    ctrl.hold_state = ExportState(engaged=True, state_since=T)  # restored from the store
    ctrl.enabled = False
    await ctrl.tick()
    assert ("release_to_self",) in act.calls
    assert not ctrl.hold_state.engaged


def test_dp_select_slots_builds_the_mask_from_slot_price_and_load(monkeypatch):
    captured = {}
    real = ctrl_mod.optimize_mod.optimize_grid

    def _spy(*a, **k):
        captured["hold"] = k.get("hold")
        return real(*a, **k)

    monkeypatch.setattr(ctrl_mod.optimize_mod, "optimize_grid", _spy)
    slots = [PriceSlot(T + timedelta(hours=i), p) for i, p in enumerate([0.35, 0.60, 0.35])]
    loads = [250.0, 250.0, 2000.0]
    ivs = [ForecastInterval(s.start, 0.0, w, 1.0) for s, w in zip(slots, loads, strict=True)]
    cfg = make_config(soc_floor=5.0, round_trip_eff=0.6, cycle_cost_eur_per_kwh=0.0)
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
    assert captured["hold"] == [True, False, True]


def test_hold_weighs_the_kept_kwh_at_its_terminal_credit(monkeypatch):
    """A kWh the terminal credits at 0.40 is worth keeping over a 0.36 discharge, even
    though the water value says 0.25: the DP is offered that hold, and the live rule
    reads the same stored value."""
    captured = {}
    real = ctrl_mod.optimize_mod.optimize_grid

    def _spy(*a, **k):
        captured["hold"] = k.get("hold")
        return real(*a, **k)

    monkeypatch.setattr(ctrl_mod.optimize_mod, "optimize_grid", _spy)
    slots = [PriceSlot(T + timedelta(hours=i), p) for i, p in enumerate([0.35, 0.60, 0.70])]
    ivs = [ForecastInterval(s.start, 0.0, 250.0, 1.0) for s in slots]
    cfg = make_config(soc_floor=5.0, round_trip_eff=0.6, cycle_cost_eur_per_kwh=0.0)
    segs = [(5.0, 0.40), (1.0, 0.30)]
    ctrl_mod._dp_select_slots(
        inputs=PlantInputs(soc=50.0, meter_w=0.0, now=T),
        slots=slots,
        deadline=T + timedelta(hours=3),
        ceiling=0.20,
        cfg=cfg,
        export_price=None,
        terminal_mode="water_value",
        water_value=0.25,
        intervals=ivs,
        terminal_segments=segs,
    )
    # eta_d 0.6: 0.21 and 0.36 sit below the richest segment, 0.42 does not
    assert captured["hold"] == [True, True, False]
    dp_out = {"water_value": 0.25, "terminal_segments": segs, "held_slots": frozenset({T})}
    assert _values(dp_out=dp_out)[1] == pytest.approx(0.40)


# ── backup reserve hold ─────────────────────────────────────────────────────


def _reserve_hold(prev, soc, *, surplus=False, now=T, cap=10.0, reserve=30.0, dips=False):
    cfg = Config(capacity_kwh=cap, backup_reserve_soc=reserve, export_dwell_min=15)
    return scheduler.backup_reserve_hold(prev, soc=soc, pv_surplus=surplus, now=now, cfg=cfg, dips=dips)


def test_backup_hold_table():
    idle = ExportState(engaged=False, state_since=T - timedelta(hours=1))
    since = T - timedelta(minutes=5)
    on = ExportState(engaged=True, state_since=since)
    assert scheduler.backup_reserve_hold(idle, soc=20.0, pv_surplus=False, now=T, cfg=Config()) is None
    assert _reserve_hold(idle, 30.0) == ExportState(engaged=True, state_since=T)
    assert _reserve_hold(on, 20.0) == on
    assert _reserve_hold(idle, 30.4) is None
    assert _reserve_hold(on, 30.4) == on
    assert _reserve_hold(on, 31.0) == on  # inside the dwell
    assert _reserve_hold(on, 31.0, now=T + timedelta(minutes=11)) == ExportState(
        engaged=False, state_since=T + timedelta(minutes=11)
    )
    assert _reserve_hold(on, 20.0, surplus=True) == on  # PV release waits out the dwell too
    assert _reserve_hold(on, 20.0, surplus=True, now=T + timedelta(minutes=11)) is None
    assert _reserve_hold(idle, 20.0, surplus=True) is None


def test_backup_hold_has_no_opinion_where_the_plan_dips():
    idle = ExportState(engaged=False, state_since=T - timedelta(hours=1))
    on = ExportState(engaged=True, state_since=T - timedelta(minutes=5))
    assert _reserve_hold(idle, 20.0, dips=True) is None
    assert _reserve_hold(on, 20.0, dips=True) == on  # the release still waits out the dwell
    assert _reserve_hold(on, 20.0, dips=True, now=T + timedelta(minutes=11)) is None


def test_backup_hold_uses_the_snapped_reserve():
    # 25 % of 20.48 kWh snaps up to ~25.15 %: the planner parks there, so the hold must too.
    idle = ExportState(engaged=False, state_since=T - timedelta(hours=1))
    assert _reserve_hold(idle, 25.1, cap=20.48, reserve=25.0) is not None
    assert _reserve_hold(idle, 25.3, cap=20.48, reserve=25.0) is None


def _reserve_ctrl(*, load=250.0, held=frozenset(), **cfg_over):
    ctrl, act = _exec_ctrl(**cfg_over)
    ctrl.cfg = replace(ctrl.cfg, backup_reserve_soc=30.0, capacity_kwh=10.0, export_dwell_min=15)
    ctrl._hold_deficit_w.clear()
    if load is not None:
        ctrl._hold_deficit_w.append((T, load))
    ctrl._held = held
    return ctrl, act


async def _run_at(ctrl, soc, *, new_state=ControllerState.PASSIVE, export=None, now=T, export_price=None, dips=None):
    return await executor.run_forcing_and_export(
        ctrl,
        now,
        PlanState(new_state, now, ()),
        PlantInputs(soc=soc, meter_w=250.0, now=now),
        _SLOTS,
        {"water_value": 0.25, "held_slots": ctrl._held, "export_request": export or {}, "dip_slots": dips},
        [],
        60,
        export_price,
        250.0,
    )


async def test_executor_holds_at_reserve():
    ctrl, act = _reserve_ctrl()  # the DP declined: held_slots is empty
    await _run_at(ctrl, 29.0)
    assert act.calls == [("engage_hold",)]
    assert ctrl.hold_state.engaged


@pytest.mark.parametrize(("dips", "holds"), [(None, True), (frozenset(), True), (frozenset({T}), False)])
async def test_executor_reserve_hold_follows_the_plan(dips, holds):
    """No plan, or a slot the plan does not run under the reserve (a forecast surplus too), holds;
    a slot the plan runs under the reserve does not."""
    ctrl, act = _reserve_ctrl()
    ctrl.cfg = replace(ctrl.cfg, backup_reserve_soc=40.0)
    await _run_at(ctrl, 35.0, dips=dips)
    assert act.calls == ([("engage_hold",)] if holds else [])
    assert ctrl.hold_state.engaged is holds


async def test_executor_planned_dip_releases_the_reserve_hold_once_after_its_dwell():
    ctrl, act = _reserve_ctrl()
    ctrl.hold_state = ExportState(engaged=True, state_since=T - timedelta(minutes=5))
    await _run_at(ctrl, 29.0, dips=frozenset({T}))
    assert act.calls == [("engage_hold",)]
    assert ctrl._in_planned_dip
    later = T + timedelta(minutes=11)
    ctrl._hold_deficit_w.append((later, 250.0))
    await _run_at(ctrl, 29.0, dips=frozenset({T}), now=later)
    assert act.calls[1:] == [("release_to_self",)]
    assert not ctrl.hold_state.engaged


async def test_executor_forced_refill_ending_in_a_planned_dip_releases():
    ctrl, act = _reserve_ctrl()
    ctrl.plan = PlanState(ControllerState.FORCING, T, ())
    await _run_at(ctrl, 29.5, dips=frozenset({T}))
    assert [c[0] for c in act.calls] == ["release_to_self"]
    assert not ctrl.hold_state.engaged
    assert ctrl._in_planned_dip


async def test_executor_forcing_is_not_a_planned_dip():
    ctrl, act = _reserve_ctrl()
    ctrl._in_planned_dip = True
    await _run_at(ctrl, 29.0, new_state=ControllerState.FORCING, dips=frozenset({T}))
    assert not ctrl._in_planned_dip


async def test_executor_no_hold_with_pv_surplus():
    ctrl, act = _reserve_ctrl(load=-400.0)
    await _run_at(ctrl, 20.0)
    assert act.calls == []
    assert not ctrl.hold_state.engaged


async def test_executor_forcing_wins_under_reserve():
    ctrl, act = _reserve_ctrl()
    await _run_at(ctrl, 20.0, new_state=ControllerState.FORCING)
    assert [c[0] for c in act.calls] == ["engage_and_charge"]
    assert not ctrl.hold_state.engaged


async def test_executor_planned_export_not_overwritten():
    ctrl, act = _reserve_ctrl()
    ctrl.cfg = replace(ctrl.cfg, enable_export=True)
    ctrl.hold_state = ExportState(engaged=True, state_since=T - timedelta(hours=1))
    await _run_at(ctrl, 80.0, export={T: 3000.0}, export_price=0.5)
    assert act.calls[-1][0] == "engage_export"
    assert not any(c[0] == "engage_hold" for c in act.calls)
    assert not ctrl.hold_state.engaged


@pytest.mark.parametrize("soc", [29.0, 30.5])
async def test_executor_holds_in_an_export_slot_that_does_not_fire(soc):
    ctrl, act = _reserve_ctrl()
    ctrl.cfg = replace(ctrl.cfg, enable_export=True)
    ctrl.hold_state = ExportState(engaged=True, state_since=T - timedelta(hours=1))
    await _run_at(ctrl, soc, export={T: 3000.0}, export_price=0.10)
    assert act.calls == [("engage_hold",)]
    assert ctrl.hold_state.engaged


async def test_executor_export_winding_down_hands_over_to_the_hold_next_tick():
    ctrl, act = _reserve_ctrl()
    ctrl.cfg = replace(ctrl.cfg, enable_export=True)
    ctrl.export_state = ExportState(engaged=True, state_since=T - timedelta(minutes=5))
    await _run_at(ctrl, 29.0, export_price=0.10)
    assert [c[0] for c in act.calls] == ["release_to_self"]
    assert not ctrl.hold_state.engaged and not ctrl.export_state.engaged
    await _run_at(ctrl, 29.0, export_price=0.10, now=T + timedelta(minutes=1))
    assert [c[0] for c in act.calls] == ["release_to_self", "engage_hold"]
    assert ctrl.hold_state.engaged


async def test_executor_pv_crossover_does_not_flap_the_work_mode():
    ctrl, act = _reserve_ctrl()
    ctrl.hold_state = ExportState(engaged=True, state_since=T - timedelta(hours=1))
    release_ticks = []
    for i in range(40):
        t = T + timedelta(minutes=i)
        ctrl._hold_deficit_w.clear()
        ctrl._hold_deficit_w.append((t, 60.0 if i % 2 else -60.0))
        n0 = len(act.calls)
        await _run_at(ctrl, 25.0, now=t)
        if any(c[0] == "release_to_self" for c in act.calls[n0:]):
            release_ticks.append(i)
    assert len(release_ticks) >= 2
    assert all(b - a >= 15 for a, b in itertools.pairwise(release_ticks))


@pytest.mark.parametrize("held_before", [True, False])
async def test_executor_forced_refill_ending_inside_the_band_hands_straight_back(held_before):
    ctrl, act = _reserve_ctrl()
    ctrl.hold_state = ExportState(engaged=held_before, state_since=T - timedelta(minutes=5))
    ctrl.plan = PlanState(ControllerState.PASSIVE, T, ())
    await _run_at(ctrl, 29.0, new_state=ControllerState.FORCING)
    assert not ctrl.hold_state.engaged  # charging is not a hold
    ctrl.plan = PlanState(ControllerState.FORCING, T, ())
    later = T + timedelta(minutes=1)
    ctrl._hold_deficit_w.append((later, 250.0))
    await _run_at(ctrl, 30.5, now=later)
    assert [c[0] for c in act.calls] == ["engage_and_charge", "engage_hold"]
    assert ctrl.hold_state.engaged


async def test_executor_forced_charge_ending_high_releases_once_without_a_hold():
    ctrl, act = _reserve_ctrl()
    ctrl.plan = PlanState(ControllerState.FORCING, T, ())
    await _run_at(ctrl, 60.0)
    assert [c[0] for c in act.calls] == ["release_to_self"]
    assert not ctrl.hold_state.engaged


async def test_executor_forced_charge_ending_in_pv_surplus_releases():
    ctrl, act = _reserve_ctrl(load=-400.0)
    ctrl.plan = PlanState(ControllerState.FORCING, T, ())
    await _run_at(ctrl, 30.5)
    assert [c[0] for c in act.calls] == ["release_to_self"]


async def test_executor_forced_charge_ending_in_an_export_slot_writes_one_mode():
    ctrl, act = _reserve_ctrl()
    ctrl.cfg = replace(ctrl.cfg, enable_export=True)
    ctrl.plan = PlanState(ControllerState.FORCING, T, ())
    await _run_at(ctrl, 30.5, export={T: 3000.0}, export_price=0.10)
    assert [c[0] for c in act.calls] == ["engage_hold"]


async def test_executor_hold_inside_its_dwell_yields_to_a_firing_export():
    ctrl, act = _reserve_ctrl()
    ctrl.cfg = replace(ctrl.cfg, enable_export=True)
    ctrl.hold_state = ExportState(engaged=True, state_since=T - timedelta(minutes=5))
    await _run_at(ctrl, 80.0, export={T: 3000.0}, export_price=0.5)
    assert [c[0] for c in act.calls] == ["engage_export"]
    assert not ctrl.hold_state.engaged


async def test_executor_failing_engage_hold_retries_once_per_dwell():
    ctrl, act = _reserve_ctrl()
    act.fail_on = {"engage_hold"}
    for i in range(30):
        t = T + timedelta(minutes=i)
        ctrl._hold_deficit_w.append((t, 250.0))
        await _run_at(ctrl, 25.0, now=t)
    # one attempt (and one recovery release) per 15-min dwell, not per tick
    assert [c[0] for c in act.calls].count("release_to_self") == 2
    assert len([c for c in act.calls if c[0].startswith("engage_hold")]) == 2


async def test_executor_stale_load_holds():
    ctrl, act = _reserve_ctrl(load=None)
    ctrl._hold_deficit_w.append((T - timedelta(minutes=30), -900.0))
    await _run_at(ctrl, 29.0)
    assert act.calls == [("engage_hold",)]


async def test_executor_reserve_hold_releases_once_after_dwell():
    ctrl, act = _reserve_ctrl()
    await _run_at(ctrl, 29.0)
    later = T + timedelta(minutes=5)
    ctrl._hold_deficit_w.append((later, 250.0))
    await _run_at(ctrl, 32.0, now=later)  # above the band, still inside the dwell
    assert act.calls == [("engage_hold",), ("engage_hold",)]
    done = T + timedelta(minutes=20)
    ctrl._hold_deficit_w.append((done, 250.0))
    await _run_at(ctrl, 32.0, now=done)
    assert act.calls[2:] == [("release_to_self",)]
    assert not ctrl.hold_state.engaged
