"""Pure calibration-policy tests. No HA, no I/O, no clock."""

import dataclasses
from datetime import datetime, timedelta, UTC

from custom_components.anker_x1_smartgrid import calibration
from custom_components.anker_x1_smartgrid.models import Config, PriceSlot


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


def _slots(start, prices, minutes=60):
    return [
        PriceSlot(start=start + timedelta(minutes=minutes * i), price=p, duration_min=minutes)
        for i, p in enumerate(prices)
    ]


CFG = Config(
    capacity_kwh=20.0,
    max_charge_w=12000.0,
    eta_charge=0.92,
    calibration_top_soc=97.0,
    calibration_dwell_h=2.0,
)


def test_price_percentile_over_all_slot_prices():
    hist = {"2026-08-01": {"0": 0.10, "1": 0.20}, "2026-08-02": {"0": 0.30, "1": 0.40}}
    assert calibration.price_percentile(hist, 50.0) == 0.25
    assert calibration.price_percentile({}, 50.0) is None


def test_selects_cheapest_window_and_requires_the_bar():
    # soc 87 -> 97 = 2 kWh at ~11 kW effective ≈ 0.18 h charge + 2 h dwell.
    slots = _slots(BASE, [0.40, 0.40, 0.05, 0.05, 0.05, 0.40])
    win = calibration.select_window(BASE, 87.0, slots, cfg=CFG, bar=0.10, force=False)
    assert win is not None
    assert win[0] == BASE + timedelta(hours=2)
    # Same slots, an unreachable bar: no window.
    assert calibration.select_window(BASE, 87.0, slots, cfg=CFG, bar=0.01, force=False) is None


def test_force_ignores_the_bar():
    slots = _slots(BASE, [0.40, 0.40, 0.40, 0.40])
    win = calibration.select_window(BASE, 87.0, slots, cfg=CFG, bar=0.01, force=True)
    assert win is not None


def test_no_bar_means_only_force_can_fire():
    slots = _slots(BASE, [0.05, 0.05, 0.05, 0.05])
    assert calibration.select_window(BASE, 87.0, slots, cfg=CFG, bar=None, force=False) is None
    assert calibration.select_window(BASE, 87.0, slots, cfg=CFG, bar=None, force=True) is not None


def test_does_not_skip_today_for_a_cheaper_tomorrow():
    """The 13:00 publication of tomorrow's prices must not pull a cycle off an
    already-qualifying today window."""
    slots = _slots(BASE, [0.20] * 4) + _slots(BASE + timedelta(days=1), [0.01] * 4)
    win = calibration.select_window(BASE, 87.0, slots, cfg=CFG, bar=0.30, force=False)
    assert win is not None
    assert win[0].date() == BASE.date()


def test_cheapest_window_within_the_day_wins():
    """One candidate per start-date, and it is that date's cheapest."""
    slots = _slots(BASE, [0.50, 0.05, 0.05, 0.05, 0.01, 0.01, 0.01, 0.50])
    win = calibration.select_window(BASE, 87.0, slots, cfg=CFG, bar=0.30, force=False)
    assert win is not None
    assert win[0] == BASE + timedelta(hours=4)


def test_mixed_durations_end_reflects_real_elapsed_time_not_extrapolation():
    """A block spanning a switch from 60-min to 15-min slots must end where the
    REAL slots end, not at a naive `n * first_slot_duration` extrapolation.

    Need here is ~130.87 min (soc 87 -> 97 charge + 2h dwell). The real slots
    (60 + 60 + 15 = 135 min) cover that in 2h15m. A width sampled once from
    the first (60-min) slot and extrapolated over a 3-slot count would instead
    land at start + 3h -- 45 minutes late.
    """
    slots = [
        PriceSlot(start=BASE, price=0.30, duration_min=60),
        PriceSlot(start=BASE + timedelta(hours=1), price=0.30, duration_min=60),
        PriceSlot(start=BASE + timedelta(hours=2), price=0.05, duration_min=15),
    ]
    win = calibration.select_window(BASE, 87.0, slots, cfg=CFG, bar=0.30, force=False)
    assert win is not None
    assert win[0] == BASE
    assert win[1] == BASE + timedelta(hours=2, minutes=15)


def test_gap_in_slot_series_forecloses_spanning_it():
    """A real discontinuity in the price curve must not be silently spanned as
    if it were elapsed time -- not even under `force`."""
    slots = [
        PriceSlot(start=BASE, price=0.05, duration_min=60),
        PriceSlot(start=BASE + timedelta(hours=1), price=0.05, duration_min=60),
        # 3 h gap: only 2 h of real, contiguous duration precedes it -- below
        # the ~2.18 h need, and the slot after the gap cannot be borrowed.
        PriceSlot(start=BASE + timedelta(hours=5), price=0.05, duration_min=60),
    ]
    assert calibration.select_window(BASE, 87.0, slots, cfg=CFG, bar=0.10, force=True) is None


def test_duration_min_none_falls_back_to_sixty_minutes():
    """A slot with duration_min=None (a single-entry curve) must fall back to
    a 60-minute assumption -- not crash, and not silently become zero-width."""
    slots = [
        PriceSlot(start=BASE, price=0.05, duration_min=None),
        PriceSlot(start=BASE + timedelta(hours=1), price=0.05, duration_min=None),
        PriceSlot(start=BASE + timedelta(hours=2), price=0.05, duration_min=None),
    ]
    win = calibration.select_window(BASE, 87.0, slots, cfg=CFG, bar=0.10, force=False)
    assert win == (BASE, BASE + timedelta(hours=3))


def test_charge_h_clamps_when_soc_at_or_above_top():
    """Without the `max(0.0, ...)` clamp, soc at/above the calibration top
    would compute a negative charge_h (negative gap_kwh) instead of zero."""
    assert calibration._charge_h(97.0, CFG) == 0.0
    assert calibration._charge_h(99.0, CFG) == 0.0


def test_insufficient_total_slots_returns_none():
    """Fewer real minutes available than `need_h` requires, even under
    `force` -- there simply is no candidate to accept."""
    slots = _slots(BASE, [0.05], minutes=60)  # only 1 h available; need ~2.18 h
    assert calibration.select_window(BASE, 87.0, slots, cfg=CFG, bar=0.10, force=True) is None


# --- Window placement against the projected SoC trajectory -------------------
#
# Live lab 2026-08-06: pack at 5%, cheap block 12:00-15:00 at 0.127 EUR/kWh,
# and the DP's own plan riding solar to 99.3% at 16:15 for free. Sizing every
# candidate from the LIVE SoC and ranking by MEAN PRICE picked the cheap block,
# which force-bought 8.56 kWh (~1.10 EUR) at 12:15 and left 5.32 kWh of planned
# solar with nowhere to go. The same cycle at the natural top needs 0.15 kWh.


def _forecast(start, socs, minutes=60):
    return [(start + timedelta(minutes=minutes * i), s) for i, s in enumerate(socs)]


def test_window_is_sized_from_projected_soc_at_its_own_start():
    """A candidate starting hours out must be sized from the SoC it will have
    THEN, not the SoC now. Live soc 50 needs ~2.85 h (0.85 charge + 2 dwell);
    at the projected 96.5 it needs ~2.01 h."""
    slots = _slots(BASE, [0.30] * 8)
    fc = _forecast(BASE, [50.0, 60.0, 75.0, 88.0, 96.5, 96.5, 96.5, 96.5])
    win = calibration.select_window(
        BASE + timedelta(hours=4), 50.0, slots, cfg=CFG, bar=0.40, force=False, soc_forecast=fc
    )
    assert win is not None
    # 2.01 h of need is covered by slots 4 and 5 plus a sliver of 6 -> 3 slots.
    assert win == (BASE + timedelta(hours=4), BASE + timedelta(hours=7))


def test_ranks_windows_by_total_grid_cost_not_mean_price():
    """The cheap block is cheaper per kWh but needs 9.4 kWh from the grid; the
    expensive window at the solar top needs 0.1 kWh. 9.4*0.10=0.94 EUR beats
    0.1*0.30=0.03 EUR only if you rank by price instead of by cost."""
    slots = _slots(BASE, [0.10, 0.10, 0.10, 0.10, 0.30, 0.30, 0.30])
    fc = _forecast(BASE, [50.0, 60.0, 75.0, 88.0, 96.5, 96.5, 96.5])
    win = calibration.select_window(BASE, 50.0, slots, cfg=CFG, bar=0.40, force=False, soc_forecast=fc)
    assert win is not None
    assert win[0] == BASE + timedelta(hours=4), "cheap-per-kWh block won on price despite costing 30x more"


def test_cheap_block_still_wins_when_solar_will_not_reach_the_top():
    """The cost ranking must not become 'always calibrate late'. With the pack
    projected to stay at 50%, the late window needs the same 9.4 kWh as the
    early one, so the cheap block is genuinely cheaper and must win."""
    slots = _slots(BASE, [0.10, 0.10, 0.10, 0.10, 0.30, 0.30, 0.30])
    fc = _forecast(BASE, [50.0] * 7)
    win = calibration.select_window(BASE, 50.0, slots, cfg=CFG, bar=0.40, force=False, soc_forecast=fc)
    assert win is not None
    assert win[0] == BASE


def test_absent_forecast_falls_back_to_live_soc():
    """No plan horizon (startup, DP failure) must leave placement exactly as it
    was rather than silently sizing every candidate as a free top-up."""
    slots = _slots(BASE, [0.50, 0.05, 0.05, 0.05, 0.01, 0.01, 0.01, 0.50])
    assert calibration.select_window(
        BASE, 87.0, slots, cfg=CFG, bar=0.30, force=False, soc_forecast=None
    ) == calibration.select_window(BASE, 87.0, slots, cfg=CFG, bar=0.30, force=False)


def test_forecast_before_its_first_row_falls_back_to_live_soc():
    """A candidate starting before the horizon begins has no projection to read;
    it must fall back to live SoC rather than borrow the first row's value."""
    fc = _forecast(BASE + timedelta(hours=3), [96.5, 96.5, 96.5])
    assert calibration._soc_at(BASE, 42.0, fc) == 42.0
    assert calibration._soc_at(BASE + timedelta(hours=3), 42.0, fc) == 96.5
    assert calibration._soc_at(BASE + timedelta(hours=9), 42.0, fc) == 96.5


def test_near_full_window_bypasses_the_price_bar():
    """The bar exists to stop calibrating at expensive times. A window needing
    almost no grid energy has no expensive time to speak of -- rejecting it on
    price alone would leave the cycle waiting for the deadline `force`."""
    slots = _slots(BASE, [0.10, 0.10, 0.10, 0.10, 0.30, 0.30, 0.30])
    fc = _forecast(BASE, [50.0, 60.0, 75.0, 88.0, 96.5, 96.5, 96.5])
    # bar below every price in the late window; the 0.1 kWh need must carry it.
    win = calibration.select_window(BASE, 50.0, slots, cfg=CFG, bar=0.12, force=False, soc_forecast=fc)
    assert win is not None
    assert win[0] == BASE + timedelta(hours=4)


from custom_components.anker_x1_smartgrid import const

ON = Config(
    capacity_kwh=20.0,
    max_charge_w=12000.0,
    eta_charge=0.92,
    calibration_enabled=True,
    calibration_interval_days=5,
    calibration_top_soc=97.0,
    calibration_dwell_h=2.0,
)
CHEAP_HISTORY = {"2026-07-30": {str(h): 0.30 for h in range(24)}}


def _climbing_fc(now):
    """A projected climb that clears the plan-peak gate without touching window
    PLACEMENT: the row sits past every candidate start, so `_soc_at` still falls
    back to the live SoC exactly as it did before the gate existed (pinned by
    test_forecast_before_its_first_row_falls_back_to_live_soc). Tests below that
    are about window selection, not about the gate, thread this in so the gate
    cannot silently turn them into idle-for-the-wrong-reason."""
    return [(now + timedelta(days=7), 99.0)]


def _stale_history(now, days):
    """SoC series spanning exactly `days`, never reaching top_soc.

    With no qualifying run, days_since == the series span, so this controls
    the policy's notion of "days since last success" directly.
    """
    start = now - timedelta(days=days)
    return _series(start, 60, [50.0] * (int(days * 24) + 1))


def test_disabled_is_always_none():
    now = BASE
    off = dataclasses.replace(ON, calibration_enabled=False)
    assert (
        calibration.calibration_action(now, 50.0, _slots(now, [0.01] * 6), _stale_history(now, 30), CHEAP_HISTORY, off)
        is None
    )


def test_not_due_inside_the_interval():
    now = BASE
    recent = _series(now - timedelta(days=1), 15, [98.0] * 13)
    assert calibration.calibration_action(now, 50.0, _slots(now, [0.01] * 6), recent, CHEAP_HISTORY, ON) is None


def test_due_and_cheap_returns_charging():
    now = BASE
    slots = _slots(now, [0.01] * 6)
    act = calibration.calibration_action(
        now, 50.0, slots, _stale_history(now, 30), CHEAP_HISTORY, ON, soc_forecast=_climbing_fc(now)
    )
    assert act is not None
    assert act.phase == "charging"
    assert act.window_start <= now < act.window_end


def test_at_top_soc_reports_holding():
    now = BASE
    slots = _slots(now, [0.01] * 6)
    act = calibration.calibration_action(now, 98.0, slots, _stale_history(now, 30), CHEAP_HISTORY, ON)
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
    cfg = dataclasses.replace(ON, calibration_top_soc=100.0)
    act = calibration.calibration_action(
        now, 99.0, _slots(now, [0.90] * 6), _stale_history(now, 30), CHEAP_HISTORY, cfg
    )
    assert act is None or act.phase != "holding", "the dwell clock must not start below the target"


def test_already_holding_continues_at_the_continuation_bar():
    """Once genuinely at 100 the inverter cuts charge and the pack self-
    discharges (~270 W measured 2026-07-30), so the hold must survive drifting
    a point without restarting the hour."""
    now = BASE
    cfg = dataclasses.replace(ON, calibration_top_soc=100.0)
    act = calibration.calibration_action(
        now, 99.0, _slots(now, [0.90] * 6), _stale_history(now, 30), CHEAP_HISTORY, cfg, already_holding=True
    )
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
    cfg = dataclasses.replace(ON, calibration_top_soc=100.0)
    assert calibration._charge_h(calibration.continue_soc(cfg), cfg) > 0.0


def test_holds_through_even_without_a_cheap_window():
    """A dwell in progress must complete regardless of the price curve."""
    now = BASE
    act = calibration.calibration_action(now, 98.0, _slots(now, [0.90] * 6), _stale_history(now, 6), CHEAP_HISTORY, ON)
    assert act is not None
    assert act.phase == "holding"


def test_fresh_install_short_history_is_idle():
    """No qualifying run AND too little history => idle, never 'charge now'."""
    now = BASE
    short = _series(now - timedelta(hours=6), 15, [50.0] * 24)
    assert calibration.calibration_action(now, 50.0, _slots(now, [0.01] * 6), short, CHEAP_HISTORY, ON) is None


def test_empty_soc_history_is_idle():
    now = BASE
    assert calibration.calibration_action(now, 50.0, _slots(now, [0.01] * 6), [], CHEAP_HISTORY, ON) is None


def test_empty_price_history_blocks_percentile_but_not_deadline():
    now = BASE
    slots = _slots(now, [0.90] * 6)
    fc = _climbing_fc(now)
    just_due = _stale_history(now, ON.calibration_interval_days + 1)
    assert calibration.calibration_action(now, 50.0, slots, just_due, {}, ON, soc_forecast=fc) is None
    past_grace = _stale_history(now, ON.calibration_interval_days + const.CALIBRATION_GRACE_DAYS + 1)
    assert calibration.calibration_action(now, 50.0, slots, past_grace, {}, ON, soc_forecast=fc) is not None


def test_bar_alone_accepts_when_not_yet_forced():
    """days_since inside [interval, interval+grace) must rely on the price
    bar -- not `force` -- to accept a window.  Every other passing test in
    this suite either has `force=True` already (30-day stale histories) or
    never accepts at all, so a mutant that broke the wiring of the price bar
    into `calibration_action` (wrong percentile constant, or skipping
    `price_percentile` entirely) would survive the whole suite without this
    test."""
    now = BASE
    slots = _slots(now, [0.01] * 6)
    mid_grace = _stale_history(now, 6)  # force = 6 >= 5 + 7 = 12 is False
    act = calibration.calibration_action(now, 50.0, slots, mid_grace, CHEAP_HISTORY, ON, soc_forecast=_climbing_fc(now))
    assert act is not None
    assert act.phase == "charging"


def test_future_window_is_not_acted_on_yet():
    """`select_window` may accept a future-starting window (today too
    expensive to clear the bar, tomorrow cheap); `calibration_action` must
    not act early just because SOME window was accepted.  `force` must be
    False here (a 30-day-stale history would force today's expensive window
    through regardless of price), so use a mid-grace history instead."""
    now = BASE
    slots = _slots(now, [0.90] * 3) + _slots(now + timedelta(days=1), [0.01] * 3)
    due = _stale_history(now, 6)  # force = 6 >= 5 + 7 = 12 is False
    assert (
        calibration.calibration_action(now, 50.0, slots, due, CHEAP_HISTORY, ON, soc_forecast=_climbing_fc(now)) is None
    )


def test_future_window_reports_scheduled_with_its_window():
    """Display needs the accepted-but-not-yet-started window; actuation must
    still refuse it. Same inputs as the test above."""
    now = BASE
    slots = _slots(now, [0.90] * 3) + _slots(now + timedelta(days=1), [0.01] * 3)
    due = _stale_history(now, 6)
    plan = calibration.calibration_plan(now, 50.0, slots, due, CHEAP_HISTORY, ON, soc_forecast=_climbing_fc(now))
    assert plan.phase == "scheduled"
    assert plan.window_start is not None and plan.window_end is not None
    assert plan.window_start > now, "a scheduled window starts in the future"


def test_scheduled_never_produces_an_action():
    """Safety pin: `scheduled` is display-only. If it ever yielded an action
    the controller would flip to FORCING the moment a window is merely
    accepted -- hours early, at whatever price is live right then."""
    now = BASE
    slots = _slots(now, [0.90] * 3) + _slots(now + timedelta(days=1), [0.01] * 3)
    due = _stale_history(now, 6)
    plan = calibration.calibration_plan(now, 50.0, slots, due, CHEAP_HISTORY, ON, soc_forecast=_climbing_fc(now))
    assert plan.phase == "scheduled"
    assert plan.action is None


def test_plan_and_action_agree_on_active_phases():
    """calibration_action is derived from calibration_plan, so the two cannot
    drift: whenever the plan is active the action mirrors it exactly."""
    now = BASE
    slots = _slots(now, [0.01] * 6)
    stale = _stale_history(now, 30)
    fc = _climbing_fc(now)
    for soc in (50.0, ON.calibration_top_soc):
        plan = calibration.calibration_plan(now, soc, slots, stale, CHEAP_HISTORY, ON, soc_forecast=fc)
        act = calibration.calibration_action(now, soc, slots, stale, CHEAP_HISTORY, ON, soc_forecast=fc)
        assert plan.phase in ("charging", "holding")
        assert act is not None
        assert (act.phase, act.window_start, act.window_end) == (plan.phase, plan.window_start, plan.window_end)


def test_idle_plan_carries_no_window():
    now = BASE
    plan = calibration.calibration_plan(now, 50.0, _slots(now, [0.01] * 6), [], CHEAP_HISTORY, ON)
    assert plan.phase == "idle"
    assert plan.window_start is None and plan.window_end is None
    assert plan.action is None


def test_due_at_exact_interval_boundary():
    """days_since == calibration_interval_days exactly must already count as
    due (`>=`, not `>`)."""
    now = BASE
    slots = _slots(now, [0.01] * 6)
    exact = _stale_history(now, ON.calibration_interval_days)
    act = calibration.calibration_action(now, 50.0, slots, exact, CHEAP_HISTORY, ON, soc_forecast=_climbing_fc(now))
    assert act is not None
    assert act.phase == "charging"


def test_already_holding_softens_reentry_bar_by_one_point():
    """F1: once a hold is in progress, a dip to the continuation bar must
    still hold -- absorbing quantisation/load-spike wobble instead of
    cancelling and re-engaging every tick. Without already_holding, the same
    soc_pct must NOT hold (entry is at the target, no deadband before a hold
    has begun)."""
    now = BASE
    slots = _slots(now, [0.90] * 6)
    stale = _stale_history(now, 6)  # due (>=5), not yet forced (<12)
    just_under = calibration.continue_soc(ON)

    # Asserted on PHASE, not on None. One point below the top the window needs
    # 0.2 kWh, so select_window's near-full rule bypasses the price bar and a
    # window is (correctly) accepted however expensive the slots are. What must
    # still hold is that the phase is "charging" -- the pack is below the target
    # and the dwell has not begun, so there is no deadband to soften yet.
    without_latch = calibration.calibration_action(now, just_under, slots, stale, CHEAP_HISTORY, ON)
    assert without_latch is not None
    assert without_latch.phase == "charging", "no deadband before a hold has begun"

    with_latch = calibration.calibration_action(now, just_under, slots, stale, CHEAP_HISTORY, ON, already_holding=True)
    assert with_latch is not None
    assert with_latch.phase == "holding"


def test_holding_reports_the_open_runs_actual_start():
    """F4: window_start/window_end must reflect the SoC run's real start, not
    a sliding `now` recomputed every tick."""
    now = BASE
    run_start = now - timedelta(hours=1)
    history = _stale_history(run_start, 6) + _series(run_start, 15, [98.0] * 5)  # 1h run, ends at `now`

    act = calibration.calibration_action(now, 98.0, _slots(now, [0.90] * 6), history, CHEAP_HISTORY, ON)
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
    act = calibration.calibration_action(now, 98.0, _slots(now, [0.90] * 6), stale, CHEAP_HISTORY, ON)
    assert act is not None
    assert act.phase == "holding"
    assert act.window_start == now
    assert act.window_end == now + timedelta(hours=ON.calibration_dwell_h)


def test_force_at_exact_grace_boundary():
    """days_since == interval + CALIBRATION_GRACE_DAYS exactly must already
    force (`>=`, not `>`).  Slots are priced above CHEAP_HISTORY's bar so only
    the force path can accept."""
    now = BASE
    slots = _slots(now, [0.90] * 6)
    exact = _stale_history(now, ON.calibration_interval_days + const.CALIBRATION_GRACE_DAYS)
    act = calibration.calibration_action(now, 50.0, slots, exact, CHEAP_HISTORY, ON, soc_forecast=_climbing_fc(now))
    assert act is not None
    assert act.phase == "charging"


# --- The plan-peak gate ------------------------------------------------------
#
# A calibration is only worth planning off a climb the plan already makes.
# Below CALIBRATION_MIN_PLAN_SOC the grid has to buy the WHOLE climb to the
# calibration top, which is the cost this gate exists to refuse.


def test_plan_peak_soc_folds_in_the_live_soc():
    """No horizon (startup, a failed DP run) degrades to 'is the pack already
    near the top right now' rather than to a fabricated climb."""
    assert calibration.plan_peak_soc(42.0, None) == 42.0
    assert calibration.plan_peak_soc(42.0, []) == 42.0
    assert calibration.plan_peak_soc(42.0, _forecast(BASE, [10.0, 20.0])) == 42.0
    assert calibration.plan_peak_soc(42.0, _forecast(BASE, [10.0, 91.0, 20.0])) == 91.0


def test_gate_blocks_when_the_plan_never_climbs():
    """Cheap window, due, but the plan peaks at 40% -- every kWh of the climb
    to the top would be bought."""
    now = BASE
    slots = _slots(now, [0.01] * 6)
    fc = _forecast(now, [40.0] * 6)
    plan = calibration.calibration_plan(now, 40.0, slots, _stale_history(now, 6), CHEAP_HISTORY, ON, soc_forecast=fc)
    assert plan.phase == "idle"


def test_gate_is_hard_and_survives_the_deadline_force():
    """Past interval+grace the price bar is bypassed, but the gate is not: an
    overdue pack waits for a day the plan actually climbs, and days_since just
    keeps growing."""
    now = BASE
    slots = _slots(now, [0.01] * 6)
    fc = _forecast(now, [40.0] * 6)
    plan = calibration.calibration_plan(now, 40.0, slots, _stale_history(now, 30), CHEAP_HISTORY, ON, soc_forecast=fc)
    assert plan.phase == "idle"


def test_gate_passes_on_a_projected_climb_from_a_low_live_soc():
    """The whole point: the pack is at 50 now, but the plan rides solar to the
    top later today, so the cycle is worth placing -- and placement is exactly
    what it was before the gate existed."""
    now = BASE
    slots = _slots(now, [0.01] * 7)
    fc = _forecast(now, [50.0, 60.0, 75.0, 88.0, 96.5, 96.5, 96.5])
    plan = calibration.calibration_plan(now, 50.0, slots, _stale_history(now, 6), CHEAP_HISTORY, ON, soc_forecast=fc)
    assert plan.phase != "idle"
    win = calibration.select_window(now, 50.0, slots, cfg=ON, bar=0.30, force=False, soc_forecast=fc)
    assert win is not None
    assert (plan.window_start, plan.window_end) == win


def test_gate_passes_on_live_soc_alone_without_a_forecast():
    """An absent horizon must not strand a pack that genuinely is near the
    top -- plan_peak_soc folds the live SoC in."""
    now = BASE
    slots = _slots(now, [0.01] * 6)
    plan = calibration.calibration_plan(now, 85.0, slots, _stale_history(now, 6), CHEAP_HISTORY, ON, soc_forecast=None)
    assert plan.phase == "charging"


def test_gate_blocks_on_live_soc_alone_without_a_forecast():
    """The fail-closed direction of the same fold-in: no horizon and a low
    pack cannot buy a whole climb off missing data."""
    now = BASE
    slots = _slots(now, [0.01] * 6)
    plan = calibration.calibration_plan(now, 50.0, slots, _stale_history(now, 6), CHEAP_HISTORY, ON, soc_forecast=None)
    assert plan.phase == "idle"


def test_gate_cannot_cut_a_dwell_already_in_progress():
    """Ordering pin: the gate sits AFTER hold-through. With the target
    configured below the gate the hold bar sits under it, so a gate placed
    earlier would abandon a live dwell halfway -- buying the charge without
    the balancing it was for."""
    now = BASE
    cfg = dataclasses.replace(ON, calibration_top_soc=75.0)
    fc = _forecast(now, [70.0] * 6)
    plan = calibration.calibration_plan(
        now, 76.0, _slots(now, [0.90] * 6), _stale_history(now, 6), CHEAP_HISTORY, cfg, soc_forecast=fc
    )
    assert plan.phase == "holding"


def test_gate_boundary_is_inclusive():
    """Exactly CALIBRATION_MIN_PLAN_SOC passes (`>=`, not `>`)."""
    now = BASE
    slots = _slots(now, [0.01] * 6)
    at_bar = _forecast(now, [50.0] + [const.CALIBRATION_MIN_PLAN_SOC] * 5)
    below = _forecast(now, [50.0] + [const.CALIBRATION_MIN_PLAN_SOC - 0.1] * 5)
    stale = _stale_history(now, 6)
    assert calibration.calibration_plan(now, 50.0, slots, stale, CHEAP_HISTORY, ON, soc_forecast=at_bar).phase != "idle"
    assert calibration.calibration_plan(now, 50.0, slots, stale, CHEAP_HISTORY, ON, soc_forecast=below).phase == "idle"
