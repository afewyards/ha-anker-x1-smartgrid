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


def test_rule_counts_the_standby_on_both_sides():
    """Discharging 250 W at eta_conv 0.85 spends 250/0.85 + 100 = 394.1 W of DC
    and buys nothing; holding spends no DC but buys 250 + 100 = 350 W, because
    the standby keeps running and the meter covers it. So a stored kWh is worth
    350/394.1 of the price -- the standby barely dilutes it."""
    curve = SimpleNamespace(eta_discharge=lambda _w: 0.85, eta_charge=lambda _w: 1.0)
    v = optimize.discharge_value(1.0, 250.0, Config(idle_drain_w=100.0), curve)
    assert v == pytest.approx(350.0 / (250.0 / 0.85 + 100.0), abs=1e-6)
    assert v == pytest.approx(0.888, abs=5e-4)
    # No modeled drain -> the bare conversion eta, as before.
    assert optimize.discharge_value(1.0, 250.0, Config(), curve) == pytest.approx(0.85)


def test_live_rule_and_dp_read_the_same_value():
    cfg = Config(low_power_hold=True, soc_floor=5.0, idle_drain_w=100.0)
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
    # DP published no hold schedule at all (feature off / no water value): the
    # live rule must not invent one.
    assert _values(dp_out={"water_value": 0.25})[0] is None


# ── dwell / hysteresis ──────────────────────────────────────────────────────


def _decide(prev, dv, sv, now=T, dp_holds=True, **cfg):
    return scheduler.decide_hold_state(
        prev,
        discharge_value=dv,
        stored_value=sv,
        now=now,
        cfg=Config(export_dwell_min=15, **cfg),
        dp_holds=dp_holds,
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


def _dp(hold, *, water_value=0.20, soc=50.0, idle_drain_w=0.0, **kw):
    cfg = make_config(soc_floor=5.0, soc_target=100.0, idle_drain_w=idle_drain_w)
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


def test_min_run_is_one_slot_at_hourly_resolution():
    assert optimize.hold_min_run_slots(15, 60) == 1  # no-op, unchanged behaviour
    assert optimize.hold_min_run_slots(15, 15) == 2
    assert optimize.hold_min_run_slots(30, 15) == 3


def test_smoothing_collapses_a_noisy_hold_pattern():
    """Price noise of a few hundredths makes the raw DP choice alternate slot by
    slot; each flip is a work-mode write the dwell cannot absorb."""
    raw = [c == "H" for c in "H.HHHHH.HH.HHHHHH..HHH."]
    smooth = optimize.smooth_hold_runs(raw, 2)
    assert _flips(raw) == 9
    assert _flips(smooth) == 3
    assert optimize.smooth_hold_runs(raw, 1) == raw  # min_run<=1 is a no-op


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
    ctrl, act, _ = _make_controller(hass, cfg_overrides={"low_power_hold": True, "enable_export": False, **cfg_over})
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
    ctrl, act = _tick_ctrl(monkeypatch)
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
