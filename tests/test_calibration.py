"""Pure calibration-policy tests. No HA, no I/O, no clock."""

import dataclasses
from datetime import datetime, timedelta, UTC

import pytest

from custom_components.anker_x1_smartgrid import calibration, const
from custom_components.anker_x1_smartgrid.models import Config
from tests.test_calibration_cost import midday_spill_slot, peak_export_slot


def _series(start, minutes, soc_values, state="forcing"):
    """(ts, soc, state) rows at fixed `minutes` spacing.

    Defaults to "forcing" because most callers here exercise the run-scan
    mechanics and want their run to qualify; the passive/disabled cases pass
    `state` explicitly.
    """
    return [((start + timedelta(minutes=minutes * i)).isoformat(), v, state) for i, v in enumerate(soc_values)]


BASE = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)


def test_passive_run_does_not_count():
    """A solar plateau at the top with the controller PASSIVE is not a
    calibration dwell: no charge current, so no top-balancing. Observed live
    2026-08-05 15:36->17:16 (101 samples, all state=passive, setpoint 0 W)
    being credited as a completed cycle."""
    rows = _series(BASE, 15, [98.0] * 13, state="passive")
    assert calibration.last_success_end(rows, target_soc=97.0, continue_soc=97.0, dwell_h=2.0) is None


def test_disabled_run_does_not_count():
    rows = _series(BASE, 15, [98.0] * 13, state="disabled")
    assert calibration.last_success_end(rows, target_soc=97.0, continue_soc=97.0, dwell_h=2.0) is None


def test_passive_tick_breaks_the_run():
    """Forcing must be continuous: a passive stretch means the current stopped,
    so the two forced halves may not be summed into one qualifying dwell."""
    # Unbroken 15-min spacing throughout (0..165 min) so the ONLY thing that
    # can split the run is the state -- a timestamp gap would make this pass
    # for the wrong reason. Spanning 2h45m, it would qualify if merged.
    first = _series(BASE, 15, [98.0] * 5)  # 0..60 min forcing
    lull = _series(BASE + timedelta(minutes=75), 15, [98.0] * 2, state="passive")  # 75, 90
    second = _series(BASE + timedelta(minutes=105), 15, [98.0] * 5)  # 105..165 forcing
    assert calibration.last_success_end(first + lull + second, target_soc=97.0, continue_soc=97.0, dwell_h=2.0) is None


def test_qualifying_run_returns_its_last_timestamp():
    # 3 h at 98% on 15-min spacing = 13 samples.
    rows = _series(BASE, 15, [98.0] * 13)
    got = calibration.last_success_end(rows, target_soc=97.0, continue_soc=97.0, dwell_h=2.0)
    assert got == BASE + timedelta(hours=3)


def test_run_just_under_dwell_does_not_count():
    # 1 h 45 min < 2 h dwell.
    rows = _series(BASE, 15, [98.0] * 8)
    assert calibration.last_success_end(rows, target_soc=97.0, continue_soc=97.0, dwell_h=2.0) is None


def test_run_below_top_soc_does_not_count():
    rows = _series(BASE, 15, [96.9] * 13)
    assert calibration.last_success_end(rows, target_soc=97.0, continue_soc=97.0, dwell_h=2.0) is None


def test_gap_breaks_the_run():
    """An HA outage must not fake a long hold."""
    first = _series(BASE, 15, [98.0] * 4)  # 45 min
    later = _series(BASE + timedelta(hours=6), 15, [98.0] * 4)  # 45 min
    assert calibration.last_success_end(first + later, target_soc=97.0, continue_soc=97.0, dwell_h=2.0) is None


def test_most_recent_qualifying_run_wins():
    old = _series(BASE, 15, [98.0] * 13)
    dip = _series(BASE + timedelta(hours=4), 15, [50.0] * 4)
    new = _series(BASE + timedelta(hours=24), 15, [99.0] * 13)
    got = calibration.last_success_end(old + dip + new, target_soc=97.0, continue_soc=97.0, dwell_h=2.0)
    assert got == BASE + timedelta(hours=27)


def test_empty_history_is_none():
    assert calibration.last_success_end([], target_soc=97.0, continue_soc=97.0, dwell_h=2.0) is None


def test_history_span_days():
    rows = _series(BASE, 60, [50.0] * 25)  # 24 h
    assert calibration.history_span_days(rows) == 1.0
    assert calibration.history_span_days([]) == 0.0


def test_run_of_exact_dwell_length_counts():
    """Boundary: a run of exactly `dwell_h` must qualify (`>=`, not `>`)."""
    rows = _series(BASE, 15, [98.0] * 9)  # 0, 15, ..., 120 min = exactly 2 h.
    assert calibration.last_success_end(rows, target_soc=97.0, continue_soc=97.0, dwell_h=2.0) == BASE + timedelta(
        hours=2
    )


def test_duplicate_timestamps_are_benign():
    """Duplicate ts rows (zero delta) must not corrupt the run's duration or end."""
    rows = _series(BASE, 15, [98.0] * 13)
    dup_index = 5
    rows_with_dup = [*rows[: dup_index + 1], rows[dup_index], *rows[dup_index + 1 :]]
    got = calibration.last_success_end(rows_with_dup, target_soc=97.0, continue_soc=97.0, dwell_h=2.0)
    assert got == BASE + timedelta(hours=3)


def _cal(start, socs, *, minutes=60, prices=0.20, pv=0.0, load=0.0, plan_export=0.0, export_price=None):
    """Contiguous ``CalibSlot``s from ``start``.

    Each ``socs`` entry is either a slot's END SoC -- its start chained from
    the previous slot's end, the first slot flat -- or an explicit
    ``(soc_start, soc_end)`` pair. ``prices`` is one price or one per slot;
    ``export_price`` defaults to the slot's import price.
    """
    out = []
    prev_end = None
    for i, soc in enumerate(socs):
        soc_start, soc_end = soc if isinstance(soc, tuple) else (soc if prev_end is None else prev_end, soc)
        price = prices[i] if isinstance(prices, list) else prices
        t0 = start + timedelta(minutes=minutes * i)
        out.append(
            calibration.CalibSlot(
                t0=t0,
                t1=t0 + timedelta(minutes=minutes),
                import_price=price,
                export_price=price if export_price is None else export_price,
                pv_kwh=pv,
                load_kwh=load,
                soc_start=soc_start,
                soc_end=soc_end,
                plan_import_kwh=0.0,
                plan_export_kwh=plan_export,
            )
        )
        prev_end = soc_end
    return out


def _pin_costs(monkeypatch, costs):
    """Replace ``window_cost``: a dict pins it per window start (unlisted
    starts count as uncovered), a number pins every window."""
    if isinstance(costs, dict):
        monkeypatch.setattr(calibration, "window_cost", lambda _slots, start, _end, **_kw: costs.get(start))
    else:
        monkeypatch.setattr(calibration, "window_cost", lambda *_a, **_kw: costs)


CFG = Config(
    capacity_kwh=20.0,
    max_charge_w=12000.0,
    eta_charge=0.92,
    calibration_top_soc=100.0,
    calibration_dwell_h=0.5,
)


def test_price_percentile_over_all_slot_prices():
    hist = {"2026-08-01": {"0": 0.10, "1": 0.20}, "2026-08-02": {"0": 0.30, "1": 0.40}}
    assert calibration.price_percentile(hist, 50.0) == 0.25
    assert calibration.price_percentile({}, 50.0) is None


def test_selects_cheapest_window_and_requires_the_bar(monkeypatch):
    # From 95% every window tops up 1 kWh, so the bar admits 1.0 * bar + 0.50.
    cal = _cal(BASE, [95.0] * 4)
    t = [s.t0 for s in cal]
    _pin_costs(monkeypatch, {t[0]: 0.90, t[1]: 0.90, t[2]: 0.70, t[3]: 0.75})
    pick = calibration.select_window(BASE, cal, cfg=CFG, bar=0.30, force=False, water_value=None)
    assert pick is not None
    assert (pick.start, pick.cost_eur, pick.accepted) == (t[2], 0.70, True)
    # A bar that admits none (0.10 + 0.50 < 0.70): the cheapest is still
    # reported, unaccepted.
    pick = calibration.select_window(BASE, cal, cfg=CFG, bar=0.10, force=False, water_value=None)
    assert pick is not None
    assert (pick.start, pick.cost_eur, pick.accepted) == (t[2], 0.70, False)


def test_force_ignores_the_bar(monkeypatch):
    cal = _cal(BASE, [95.0] * 4)
    _pin_costs(monkeypatch, 0.95)
    assert calibration.select_window(BASE, cal, cfg=CFG, bar=0.01, force=False, water_value=None).accepted is False
    assert calibration.select_window(BASE, cal, cfg=CFG, bar=0.01, force=True, water_value=None).accepted is True


def test_no_bar_leaves_only_the_allowance_or_force(monkeypatch):
    """An empty price history has no bar: a window must then fit inside the
    allowance alone, whatever it tops up, unless the deadline forces it."""
    cal = _cal(BASE, [95.0] * 4)
    _pin_costs(monkeypatch, const.CALIBRATION_COST_ALLOWANCE_EUR)
    assert calibration.select_window(BASE, cal, cfg=CFG, bar=None, force=False, water_value=None).accepted is True
    _pin_costs(monkeypatch, 0.60)  # a 0.10 bar would already admit it
    assert calibration.select_window(BASE, cal, cfg=CFG, bar=None, force=False, water_value=None).accepted is False
    assert calibration.select_window(BASE, cal, cfg=CFG, bar=None, force=True, water_value=None).accepted is True


def test_does_not_skip_today_for_a_cheaper_tomorrow(monkeypatch):
    """The 13:00 publication of tomorrow's prices must not pull a cycle off an
    already-qualifying today window."""
    today = _cal(BASE, [95.0] * 4)
    tomorrow = _cal(BASE + timedelta(days=1), [95.0] * 4)
    _pin_costs(monkeypatch, {s.t0: 0.40 for s in today} | {s.t0: 0.01 for s in tomorrow})
    pick = calibration.select_window(BASE, today + tomorrow, cfg=CFG, bar=0.30, force=False, water_value=None)
    assert pick is not None and pick.accepted
    assert pick.start.date() == BASE.date()


def test_cheapest_window_within_the_day_wins(monkeypatch):
    """One candidate per start-date, and it is that date's cheapest."""
    cal = _cal(BASE, [95.0] * 8)
    costs = [0.50, 0.30, 0.30, 0.30, 0.10, 0.10, 0.10, 0.50]
    _pin_costs(monkeypatch, {s.t0: c for s, c in zip(cal, costs)})
    pick = calibration.select_window(BASE, cal, cfg=CFG, bar=0.30, force=False, water_value=None)
    assert pick is not None
    assert pick.start == BASE + timedelta(hours=4)


def test_window_running_past_the_plan_is_no_candidate():
    """A window the plan does not cover has no cost, so it is no candidate --
    not even under `force`. Here the hold alone outlasts the plan."""
    cal = _cal(BASE, [100.0], minutes=15)
    assert calibration.select_window(BASE, cal, cfg=CFG, bar=0.30, force=True, water_value=None) is None


def test_window_across_a_gap_in_the_plan_is_no_candidate():
    """A real hole in the plan must not be spanned as if it were elapsed time."""
    holed = _cal(BASE, [100.0], minutes=15) + _cal(BASE + timedelta(minutes=20), [100.0], minutes=15)
    assert calibration.select_window(BASE, holed, cfg=CFG, bar=0.30, force=True, water_value=None) is None
    contiguous = _cal(BASE, [100.0, 100.0], minutes=15)
    pick = calibration.select_window(BASE, contiguous, cfg=CFG, bar=0.30, force=True, water_value=None)
    assert pick is not None
    assert pick.start == BASE


def test_window_spans_slots_of_mixed_cadence():
    """Quarter-hour and hourly plan slots chain into one window, which ends
    where its own need runs out rather than on a slot boundary."""
    cal = _cal(BASE, [95.0], minutes=15) + _cal(BASE + timedelta(minutes=15), [95.0], minutes=60)
    pick = calibration.select_window(BASE, cal, cfg=CFG, bar=0.30, force=False, water_value=None)
    assert pick is not None
    assert pick.start == BASE
    assert pick.end == BASE + timedelta(hours=calibration._charge_h(95.0, CFG) + CFG.calibration_dwell_h)


def test_charge_h_clamps_when_soc_at_or_above_top():
    """Without the `max(0.0, ...)` clamp, soc at/above the calibration top
    would compute a negative charge_h (negative gap_kwh) instead of zero."""
    assert calibration._charge_h(CFG.calibration_top_soc, CFG) == 0.0
    assert calibration._charge_h(CFG.calibration_top_soc + 2.0, CFG) == 0.0


def test_window_is_sized_from_projected_soc_at_its_own_start():
    """A candidate is sized from the SoC the plan projects at ITS start, not
    from where the pack is now: the first slot past the start gate projects
    96, so its window is the top-up from 96 plus the hold."""
    cal = _cal(BASE, [(50.0, 60.0), (60.0, 75.0), (75.0, 88.0), (88.0, 96.0), (96.0, 96.0), (96.0, 96.0)])
    pick = calibration.select_window(BASE, cal, cfg=CFG, bar=0.40, force=False, water_value=None)
    assert pick is not None
    assert pick.start == BASE + timedelta(hours=4)
    assert pick.end - pick.start == timedelta(hours=calibration._charge_h(96.0, CFG) + 0.5)


ON = Config(
    capacity_kwh=20.0,
    max_charge_w=12000.0,
    eta_charge=0.92,
    calibration_enabled=True,
    calibration_interval_days=5,
    calibration_top_soc=100.0,
    calibration_dwell_h=0.5,
)
CHEAP_HISTORY = {"2026-07-30": {str(h): 0.30 for h in range(24)}}


def _stale_history(now, days):
    """SoC series spanning exactly `days`, never reaching top_soc.

    With no qualifying run, days_since == the series span, so this controls
    the policy's notion of "days since last success" directly.
    """
    start = now - timedelta(days=days)
    return _series(start, 60, [50.0] * (int(days * 24) + 1))


def _held(now, cost=None):
    """A previous tick that was already holding."""
    return calibration.CalibPlan("holding", now - timedelta(minutes=10), now + timedelta(minutes=20), cost)


def test_disabled_is_always_none():
    now = BASE
    off = dataclasses.replace(ON, calibration_enabled=False)
    cal = _cal(now, [96.0] * 6, prices=0.01)
    assert calibration.calibration_action(now, 96.0, cal, _stale_history(now, 30), CHEAP_HISTORY, off) is None


def test_not_due_inside_the_interval():
    now = BASE
    recent = _series(now - timedelta(days=1), 15, [100.0] * 13)
    cal = _cal(now, [96.0] * 6, prices=0.01)
    assert calibration.calibration_action(now, 96.0, cal, recent, CHEAP_HISTORY, ON) is None


def test_due_and_cheap_returns_charging():
    now = BASE
    cal = _cal(now, [96.0] * 6, prices=0.01)
    act = calibration.calibration_action(now, 96.0, cal, _stale_history(now, 30), CHEAP_HISTORY, ON)
    assert act is not None
    assert act.phase == "charging"
    assert act.window_start <= now < act.window_end


def test_at_top_soc_reports_holding():
    """Against a neutral plan holding costs nothing, so the hold is accepted."""
    now = BASE
    act = calibration.calibration_action(now, 100.0, _cal(now, [100.0] * 6), _stale_history(now, 30), CHEAP_HISTORY, ON)
    assert act is not None
    assert act.phase == "holding"


def test_continuation_bar_sits_strictly_below_the_charge_target():
    """The tolerance is a CONTINUATION allowance, never an entry discount."""
    assert calibration.continue_soc(ON) < ON.calibration_top_soc


def test_hold_entry_requires_the_charge_target():
    """Entry at anything below the target is the bug this replaced: at 99 the
    pack is still taking ~5 kW (measured 2026-07-30), so an hour spent there
    is bulk charge, not the taper where cells reach balancing voltage."""
    now = BASE
    cal = _cal(now, [99.0] * 6, prices=0.90)
    act = calibration.calibration_action(now, 99.0, cal, _stale_history(now, 30), CHEAP_HISTORY, ON)
    assert act is None or act.phase != "holding", "the dwell clock must not start below the target"


def test_committed_hold_continues_at_the_continuation_bar():
    """Once genuinely at 100 the inverter cuts charge and the pack self-
    discharges (~270 W measured 2026-07-30), so the hold must survive drifting
    a point without restarting the hour."""
    now = BASE
    cal = _cal(now, [99.0] * 6, prices=0.90)
    act = calibration.calibration_action(now, 99.0, cal, _stale_history(now, 30), CHEAP_HISTORY, ON, prev=_held(now))
    assert act is not None
    assert act.phase == "holding"


def test_dwell_only_starts_at_the_charge_target():
    """A run pinned below the target never qualifies, however long it is."""
    rows = _series(BASE, 15, [99.0] * 13)  # 3 h forcing at 99, target is 100
    assert calibration.last_success_end(rows, target_soc=100.0, continue_soc=99.0, dwell_h=2.0) is None


def test_dwell_survives_a_dip_below_the_target():
    """Enters at the target, drifts to the continuation bar, keeps counting."""
    rows = _series(BASE, 15, [100.0, 100.0, 99.0, 99.0, 100.0, 99.0, 99.0, 100.0, 100.0])  # exactly 2 h
    got = calibration.last_success_end(rows, target_soc=100.0, continue_soc=99.0, dwell_h=2.0)
    assert got == BASE + timedelta(hours=2)


def test_dwell_breaks_below_the_continuation_bar():
    """A drop past the continuation bar means the pack left the top."""
    rows = _series(BASE, 15, [100.0] * 4 + [98.0] + [100.0] * 4)
    assert calibration.last_success_end(rows, target_soc=100.0, continue_soc=99.0, dwell_h=2.0) is None


def test_charge_need_is_measured_to_the_target_not_the_hold_bar():
    """Sizing the window to the bar would end the charge exactly where the
    taper -- and therefore the balancing -- begins."""
    assert calibration._charge_h(calibration.continue_soc(ON), ON) > 0.0


def test_holds_through_even_without_a_cheap_window():
    """A committed dwell completes even though holding here forfeits the
    plan's evening-peak export -- a cost no fresh hold would be accepted at."""
    now = BASE
    act = calibration.calibration_action(
        now,
        100.0,
        [peak_export_slot(now)],
        _stale_history(now, 6),
        CHEAP_HISTORY,
        ON,
        prev=_held(now),
        water_value=0.21,
    )
    assert act is not None
    assert act.phase == "holding"


def test_fresh_install_short_history_is_idle():
    """No qualifying run AND too little history => idle, never 'charge now'."""
    now = BASE
    short = _series(now - timedelta(hours=6), 15, [50.0] * 24)
    cal = _cal(now, [96.0] * 6, prices=0.01)
    assert calibration.calibration_action(now, 96.0, cal, short, CHEAP_HISTORY, ON) is None


def test_empty_soc_history_is_idle():
    now = BASE
    cal = _cal(now, [96.0] * 6, prices=0.01)
    assert calibration.calibration_action(now, 96.0, cal, [], CHEAP_HISTORY, ON) is None


def test_empty_price_history_blocks_percentile_but_not_deadline(monkeypatch):
    now = BASE
    cal = _cal(now, [95.0] * 6)
    _pin_costs(monkeypatch, 0.60)  # over the allowance alone, under the overdue cap
    just_due = _stale_history(now, ON.calibration_interval_days + 1)
    assert calibration.calibration_action(now, 95.0, cal, just_due, {}, ON) is None
    past_grace = _stale_history(now, ON.calibration_interval_days + const.CALIBRATION_GRACE_DAYS + 1)
    assert calibration.calibration_action(now, 95.0, cal, past_grace, {}, ON) is not None


def test_bar_alone_accepts_when_not_yet_forced(monkeypatch):
    """days_since inside [interval, interval+grace) must rely on the price
    bar -- not `force` -- to accept a window: a 1 kWh top-up costing 0.60
    clears 1.0 x P30 + 0.50 but not the allowance alone."""
    now = BASE
    cal = _cal(now, [95.0] * 6)
    _pin_costs(monkeypatch, 0.60)
    mid_grace = _stale_history(now, 6)  # force = 6 >= 5 + 7 = 12 is False
    act = calibration.calibration_action(now, 95.0, cal, mid_grace, CHEAP_HISTORY, ON)
    assert act is not None
    assert act.phase == "charging"
    assert calibration.calibration_action(now, 95.0, cal, mid_grace, {}, ON) is None


def _dear_today_cheap_tomorrow(now):
    """Today's top-ups cost ~0.98 EUR (over the 0.80 bar), tomorrow's ~0.01."""
    return _cal(now, [95.0] * 3, prices=0.90) + _cal(now + timedelta(days=1), [95.0] * 3, prices=0.01)


def test_future_window_is_not_acted_on_yet():
    """`select_window` may accept a future-starting window (today too
    expensive to clear the bar, tomorrow cheap); `calibration_action` must
    not act early just because SOME window was accepted. `force` must be
    False here, and the live pack clears the start gate, so neither the
    deadline nor the live-SoC guard can explain the answer."""
    now = BASE
    due = _stale_history(now, 6)  # force = 6 >= 5 + 7 = 12 is False
    assert calibration.calibration_action(now, 95.0, _dear_today_cheap_tomorrow(now), due, CHEAP_HISTORY, ON) is None


def test_future_window_reports_scheduled_with_its_window():
    """Display needs the accepted-but-not-yet-started window; actuation must
    still refuse it. Same inputs as the test above."""
    now = BASE
    due = _stale_history(now, 6)
    plan = calibration.calibration_plan(now, 95.0, _dear_today_cheap_tomorrow(now), due, CHEAP_HISTORY, ON)
    assert plan.phase == "scheduled"
    assert plan.window_start is not None and plan.window_end is not None
    assert plan.window_start > now, "a scheduled window starts in the future"


def test_scheduled_never_produces_an_action():
    """Safety pin: `scheduled` is display-only. If it ever yielded an action
    the controller would flip to FORCING the moment a window is merely
    accepted -- hours early, at whatever price is live right then."""
    now = BASE
    due = _stale_history(now, 6)
    plan = calibration.calibration_plan(now, 95.0, _dear_today_cheap_tomorrow(now), due, CHEAP_HISTORY, ON)
    assert plan.phase == "scheduled"
    assert plan.action is None


def test_plan_and_action_agree_on_active_phases():
    """calibration_action is derived from calibration_plan, so the two cannot
    drift: whenever the plan is active the action mirrors it exactly."""
    now = BASE
    cal = _cal(now, [96.0] * 6, prices=0.01)
    stale = _stale_history(now, 30)
    for soc in (96.0, ON.calibration_top_soc):
        plan = calibration.calibration_plan(now, soc, cal, stale, CHEAP_HISTORY, ON)
        act = calibration.calibration_action(now, soc, cal, stale, CHEAP_HISTORY, ON)
        assert plan.phase in ("charging", "holding")
        assert act is not None
        assert (act.phase, act.window_start, act.window_end) == (plan.phase, plan.window_start, plan.window_end)


def test_idle_plan_carries_no_window():
    now = BASE
    plan = calibration.calibration_plan(now, 96.0, _cal(now, [96.0] * 6, prices=0.01), [], CHEAP_HISTORY, ON)
    assert plan.phase == "idle"
    assert plan.window_start is None and plan.window_end is None
    assert plan.cost_eur is None
    assert plan.action is None


def test_due_at_exact_interval_boundary():
    """days_since == calibration_interval_days exactly must already count as
    due (`>=`, not `>`)."""
    now = BASE
    cal = _cal(now, [96.0] * 6, prices=0.01)
    exact = _stale_history(now, ON.calibration_interval_days)
    act = calibration.calibration_action(now, 96.0, cal, exact, CHEAP_HISTORY, ON)
    assert act is not None
    assert act.phase == "charging"


def test_committed_hold_softens_reentry_bar_by_one_point():
    """F1: once a hold is in progress, a dip to the continuation bar must
    still hold -- absorbing quantisation/load-spike wobble instead of
    cancelling and re-engaging every tick. Without a committed hold, the same
    soc_pct must NOT hold (entry is at the target, no deadband before a hold
    has begun)."""
    now = BASE
    cal = _cal(now, [99.0] * 6, prices=0.90)
    stale = _stale_history(now, 6)  # due (>=5), not yet forced (<12)
    just_under = calibration.continue_soc(ON)

    # Asserted on PHASE, not on None. One point below the top the top-up costs
    # ~0.20 EUR, inside the allowance, so a window is (correctly) accepted
    # however dear the slots are. What must still hold is that the phase is
    # "charging" -- the pack is below the target and the dwell has not begun,
    # so there is no deadband to soften yet.
    without_latch = calibration.calibration_action(now, just_under, cal, stale, CHEAP_HISTORY, ON)
    assert without_latch is not None
    assert without_latch.phase == "charging", "no deadband before a hold has begun"

    with_latch = calibration.calibration_action(now, just_under, cal, stale, CHEAP_HISTORY, ON, prev=_held(now))
    assert with_latch is not None
    assert with_latch.phase == "holding"


def test_holding_reports_the_open_runs_actual_start():
    """F4: window_start/window_end must reflect the SoC run's real start, not
    a sliding `now` recomputed every tick."""
    now = BASE
    run_start = now - timedelta(minutes=15)
    # Open at the top, but short of the 30-min dwell, so not yet a success.
    history = _stale_history(run_start, 6) + _series(run_start, 15, [100.0] * 2)

    act = calibration.calibration_action(now, 100.0, _cal(now, [100.0] * 6), history, CHEAP_HISTORY, ON)
    assert act is not None
    assert act.phase == "holding"
    assert act.window_start == run_start
    assert act.window_end == run_start + timedelta(hours=ON.calibration_dwell_h)


def test_holding_falls_back_to_now_when_the_run_is_not_yet_recorded():
    """First tick of a new hold: soc_pct is already at top but history hasn't
    recorded a qualifying sample yet -- window_start falls back to `now`,
    which is exactly right (the run genuinely starts now)."""
    now = BASE
    stale = _stale_history(now, 6)  # flat 50%, never reaches top_soc
    act = calibration.calibration_action(now, 100.0, _cal(now, [100.0] * 6), stale, CHEAP_HISTORY, ON)
    assert act is not None
    assert act.phase == "holding"
    assert act.window_start == now
    assert act.window_end == now + timedelta(hours=ON.calibration_dwell_h)


def test_force_at_exact_grace_boundary(monkeypatch):
    """days_since == interval + CALIBRATION_GRACE_DAYS exactly must already
    force (`>=`, not `>`). The window costs more than the bar admits, so only
    the overdue cap can accept it."""
    now = BASE
    _pin_costs(monkeypatch, 0.90)  # over 1.0 x 0.30 + 0.50, under the 1.00 cap
    exact = _stale_history(now, ON.calibration_interval_days + const.CALIBRATION_GRACE_DAYS)
    act = calibration.calibration_action(now, 95.0, _cal(now, [95.0] * 6), exact, CHEAP_HISTORY, ON)
    assert act is not None
    assert act.phase == "charging"


def test_plan_peak_soc_folds_in_the_live_soc():
    """No plan (startup, a failed DP run) degrades to the live SoC rather than
    to a fabricated climb."""
    assert calibration.plan_peak_soc(42.0, []) == 42.0
    assert calibration.plan_peak_soc(42.0, _cal(BASE, [10.0, 20.0])) == 42.0
    assert calibration.plan_peak_soc(42.0, _cal(BASE, [10.0, 91.0, 20.0])) == 91.0


# --- Start gate, committed cycles, cost acceptance ---------------------------
#
# A window starts only where the plan projects >= 95%, is accepted on what it
# costs against the DP's own plan -- a hold across a planned export forfeits
# that export -- and once running is never re-costed.


def test_start_gate_places_nothing_below_the_bar():
    """A window may only start where the plan projects >= 95%, however cheap
    and however overdue."""
    below = _cal(BASE, [94.9] * 2, prices=0.01)
    assert calibration.select_window(BASE, below, cfg=CFG, bar=0.30, force=True, water_value=None) is None
    at_bar = _cal(BASE, [95.0] * 2, prices=0.01)
    pick = calibration.select_window(BASE, at_bar, cfg=CFG, bar=0.30, force=True, water_value=None)
    assert pick is not None
    assert pick.start == BASE


def test_prefers_a_cheap_hold_to_the_pre_peak_one():
    """Holding through the evening export peak forfeits the export; holding
    through a midday PV spill forfeits nothing. The peak comes first, so
    "earliest wins" cannot be what picks the midday hold."""
    peak = peak_export_slot(BASE)
    # Not chained (the start gate would drop a slot starting at the peak's
    # 67.5), and not adjacent: at a shared boundary the plan SoC resolves to
    # the earlier slot's end, which would bill the midday hold for a recharge
    # from 67.5 the plan never needs.
    midday = midday_spill_slot(BASE + timedelta(hours=2))
    for force in (False, True):
        pick = calibration.select_window(BASE, [peak, midday], cfg=CFG, bar=0.30, force=force, water_value=0.21)
        assert pick is not None
        assert pick.start == midday.t0
        assert pick.accepted is True
        assert pick.cost_eur == pytest.approx(0.0)


def test_acceptance_filters_before_taking_the_days_cheapest(monkeypatch):
    """The bar scales with each window's top-up, so a day's cheapest window
    can fail it while a dearer one that tops up more passes."""
    cal = _cal(BASE, [(100.0, 95.0), (95.0, 95.0)])
    a, b = cal[0].t0, cal[1].t0
    _pin_costs(monkeypatch, {a: 0.55, b: 0.60})  # A: 0.55 > 0.50; B: 0.60 <= 1.0 x 0.30 + 0.50
    pick = calibration.select_window(BASE, cal, cfg=CFG, bar=0.30, force=False, water_value=None)
    assert pick is not None
    assert (pick.start, pick.cost_eur, pick.accepted) == (b, 0.60, True)


def test_overdue_accepts_up_to_the_cost_cap(monkeypatch):
    cal = _cal(BASE, [96.0] * 2)
    _pin_costs(monkeypatch, 0.99)
    assert calibration.select_window(BASE, cal, cfg=CFG, bar=None, force=True, water_value=None).accepted is True
    _pin_costs(monkeypatch, 1.01)
    assert calibration.select_window(BASE, cal, cfg=CFG, bar=None, force=True, water_value=None).accepted is False
    plan = calibration.calibration_plan(BASE, 96.0, cal, _stale_history(BASE, 30), CHEAP_HISTORY, ON)
    assert plan.phase == "idle"
    assert plan.cost_eur == 1.01


def test_window_waits_for_the_live_pack_to_reach_the_start_gate(monkeypatch):
    """The plan projects 96 now but the pack is at 94 (plan drift): the window
    stays scheduled, and starts once the live pack reaches 95."""
    now = BASE
    _pin_costs(monkeypatch, 0.0)
    cal = _cal(now, [96.0] * 2)
    due = _stale_history(now, 6)
    plan = calibration.calibration_plan(now, 94.0, cal, due, CHEAP_HISTORY, ON)
    assert plan.phase == "scheduled"
    assert plan.window_start == now
    assert plan.action is None
    assert calibration.calibration_plan(now, 95.0, cal, due, CHEAP_HISTORY, ON).phase == "charging"


def test_committed_charging_survives_a_cost_spike(monkeypatch):
    now = BASE
    _pin_costs(monkeypatch, 99.0)
    prev = calibration.CalibPlan("charging", now - timedelta(minutes=5), now + timedelta(minutes=30), 0.1)
    plan = calibration.calibration_plan(
        now, 97.0, _cal(now, [97.0] * 2), _stale_history(now, 6), CHEAP_HISTORY, ON, prev=prev
    )
    assert (plan.phase, plan.window_start, plan.window_end, plan.cost_eur) == (
        "charging",
        prev.window_start,
        prev.window_end,
        0.1,
    )


def test_committed_charging_reaching_the_top_holds(monkeypatch):
    now = BASE
    _pin_costs(monkeypatch, 99.0)
    prev = calibration.CalibPlan("charging", now - timedelta(minutes=5), now + timedelta(minutes=30), 0.1)
    plan = calibration.calibration_plan(
        now, 100.0, _cal(now, [97.0] * 2), _stale_history(now, 6), CHEAP_HISTORY, ON, prev=prev
    )
    assert plan.phase == "holding"
    assert plan.cost_eur == 0.1


def test_committed_hold_survives_a_planned_export(monkeypatch):
    now = BASE
    _pin_costs(monkeypatch, 99.0)
    plan = calibration.calibration_plan(
        now, 99.5, _cal(now, [100.0] * 2), _stale_history(now, 6), CHEAP_HISTORY, ON, prev=_held(now, 0.2)
    )
    assert plan.phase == "holding"
    assert plan.cost_eur == 0.2


def test_committed_charging_is_re_evaluated_past_its_window(monkeypatch):
    now = BASE
    _pin_costs(monkeypatch, 99.0)
    prev = calibration.CalibPlan("charging", now - timedelta(minutes=35), now - timedelta(minutes=5), 0.1)
    plan = calibration.calibration_plan(
        now, 97.0, _cal(now, [97.0] * 2), _stale_history(now, 6), CHEAP_HISTORY, ON, prev=prev
    )
    assert plan.phase == "idle"
    assert plan.cost_eur == 99.0


def test_committed_charging_topping_out_at_its_window_end_still_holds(monkeypatch):
    """The pack can reach the top on the first tick at or past window_end. The
    window was already priced, so the hold is not re-costed: rejecting it near
    a peak would buy the charge without the balancing it was for."""
    now = BASE
    _pin_costs(monkeypatch, 99.0)
    cal, due = _cal(now, [97.0] * 2), _stale_history(now, 6)
    for end in (now, now - timedelta(minutes=1)):
        prev = calibration.CalibPlan("charging", end - timedelta(minutes=30), end, 0.1)
        plan = calibration.calibration_plan(now, 100.0, cal, due, CHEAP_HISTORY, ON, prev=prev)
        assert (plan.phase, plan.cost_eur) == ("holding", 0.1)
    assert calibration.calibration_plan(now, 100.0, cal, due, CHEAP_HISTORY, ON).phase == "idle"


def test_fresh_top_out_holds_only_when_the_hold_is_cheap():
    """A top-out the DP made in order to export is not held; a midday PV top-out
    is. Overdue, the cap admits the dearer hold too."""
    now = BASE
    due, overdue = _stale_history(now, 6), _stale_history(now, 30)
    pre_peak = calibration.calibration_plan(
        now, 100.0, [peak_export_slot(now)], due, CHEAP_HISTORY, ON, water_value=0.21
    )
    assert pre_peak.phase == "idle"
    assert pre_peak.action is None
    assert pre_peak.cost_eur == pytest.approx(0.6175)
    midday = calibration.calibration_plan(
        now, 100.0, [midday_spill_slot(now)], due, CHEAP_HISTORY, ON, water_value=0.21
    )
    assert midday.phase == "holding"
    assert midday.cost_eur == pytest.approx(0.0)
    forced = calibration.calibration_plan(
        now, 100.0, [peak_export_slot(now)], overdue, CHEAP_HISTORY, ON, water_value=0.21
    )
    assert forced.phase == "holding"  # 0.6175 <= the 1.00 overdue cap


def test_rejected_fresh_hold_reports_its_cost(monkeypatch):
    """A costed, rejected fresh hold is a candidate like any other: idle
    reports the cheaper of it and the best rejected window."""
    now = BASE
    due = _stale_history(now, 6)
    # Plan drift: the pack is at the top but the plan sits at 94, so no window
    # clears the start gate and only the hold is costed.
    drifted = _cal(now, [(94.0, 94.0), (94.0, 94.0)])
    _pin_costs(monkeypatch, 0.70)
    plan = calibration.calibration_plan(now, 100.0, drifted, due, CHEAP_HISTORY, ON)
    assert (plan.phase, plan.cost_eur) == ("idle", 0.70)
    climbs = _cal(now, [(94.0, 100.0), (100.0, 100.0)])  # the second slot is a candidate
    for hold, window in ((0.70, 0.90), (0.90, 0.70)):
        _pin_costs(monkeypatch, {now: hold, now + timedelta(hours=1): window})
        plan = calibration.calibration_plan(now, 100.0, climbs, due, CHEAP_HISTORY, ON)
        assert (plan.phase, plan.cost_eur) == ("idle", 0.70)


def test_no_plan_places_nothing_but_a_committed_hold_finishes():
    now = BASE
    due = _stale_history(now, 6)
    assert calibration.calibration_plan(now, 100.0, [], due, CHEAP_HISTORY, ON).phase == "idle"
    assert calibration.calibration_plan(now, 100.0, [], due, CHEAP_HISTORY, ON, prev=_held(now)).phase == "holding"


def test_start_gate_cannot_cut_a_committed_hold():
    """With the target configured below the start gate a committed hold sits
    under it. The gate only places windows, so the dwell still finishes."""
    now = BASE
    cfg = dataclasses.replace(ON, calibration_top_soc=75.0)
    plan = calibration.calibration_plan(
        now, 76.0, _cal(now, [70.0] * 6, prices=0.90), _stale_history(now, 6), CHEAP_HISTORY, cfg, prev=_held(now)
    )
    assert plan.phase == "holding"
