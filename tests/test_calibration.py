"""Pure calibration-policy tests. No HA, no I/O, no clock."""

from datetime import datetime, timedelta, UTC

from custom_components.anker_x1_smartgrid import calibration
from custom_components.anker_x1_smartgrid.models import Config, PriceSlot


def _series(start, minutes, soc_values):
    """(ts, soc) rows at fixed `minutes` spacing."""
    return [((start + timedelta(minutes=minutes * i)).isoformat(), v) for i, v in enumerate(soc_values)]


BASE = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)


def test_qualifying_run_returns_its_last_timestamp():
    # 3 h at 98% on 15-min spacing = 13 samples.
    rows = _series(BASE, 15, [98.0] * 13)
    got = calibration.last_success_end(rows, top_soc=97.0, dwell_h=2.0)
    assert got == BASE + timedelta(hours=3)


def test_run_just_under_dwell_does_not_count():
    # 1 h 45 min < 2 h dwell.
    rows = _series(BASE, 15, [98.0] * 8)
    assert calibration.last_success_end(rows, top_soc=97.0, dwell_h=2.0) is None


def test_run_below_top_soc_does_not_count():
    rows = _series(BASE, 15, [96.9] * 13)
    assert calibration.last_success_end(rows, top_soc=97.0, dwell_h=2.0) is None


def test_gap_breaks_the_run():
    """An HA outage must not fake a long hold."""
    first = _series(BASE, 15, [98.0] * 4)  # 45 min
    later = _series(BASE + timedelta(hours=6), 15, [98.0] * 4)  # 45 min
    assert calibration.last_success_end(first + later, top_soc=97.0, dwell_h=2.0) is None


def test_most_recent_qualifying_run_wins():
    old = _series(BASE, 15, [98.0] * 13)
    dip = _series(BASE + timedelta(hours=4), 15, [50.0] * 4)
    new = _series(BASE + timedelta(hours=24), 15, [99.0] * 13)
    got = calibration.last_success_end(old + dip + new, top_soc=97.0, dwell_h=2.0)
    assert got == BASE + timedelta(hours=27)


def test_empty_history_is_none():
    assert calibration.last_success_end([], top_soc=97.0, dwell_h=2.0) is None


def test_history_span_days():
    rows = _series(BASE, 60, [50.0] * 25)  # 24 h
    assert calibration.history_span_days(rows) == 1.0
    assert calibration.history_span_days([]) == 0.0


def test_run_of_exact_dwell_length_counts():
    """Boundary: a run of exactly `dwell_h` must qualify (`>=`, not `>`)."""
    rows = _series(BASE, 15, [98.0] * 9)  # 0, 15, ..., 120 min = exactly 2 h.
    assert calibration.last_success_end(rows, top_soc=97.0, dwell_h=2.0) == BASE + timedelta(hours=2)


def test_duplicate_timestamps_are_benign():
    """Duplicate ts rows (zero delta) must not corrupt the run's duration or end."""
    rows = _series(BASE, 15, [98.0] * 13)
    dup_index = 5
    rows_with_dup = [*rows[: dup_index + 1], rows[dup_index], *rows[dup_index + 1 :]]
    got = calibration.last_success_end(rows_with_dup, top_soc=97.0, dwell_h=2.0)
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


def _stale_history(now, days):
    """SoC series spanning exactly `days`, never reaching top_soc.

    With no qualifying run, days_since == the series span, so this controls
    the policy's notion of "days since last success" directly.
    """
    start = now - timedelta(days=days)
    return _series(start, 60, [50.0] * (int(days * 24) + 1))


def test_disabled_is_always_none():
    now = BASE
    assert (
        calibration.calibration_action(
            now, 50.0, _slots(now, [0.01] * 6), _stale_history(now, 30), CHEAP_HISTORY, Config()
        )
        is None
    )


def test_not_due_inside_the_interval():
    now = BASE
    recent = _series(now - timedelta(days=1), 15, [98.0] * 13)
    assert calibration.calibration_action(now, 50.0, _slots(now, [0.01] * 6), recent, CHEAP_HISTORY, ON) is None


def test_due_and_cheap_returns_charging():
    now = BASE
    slots = _slots(now, [0.01] * 6)
    act = calibration.calibration_action(now, 50.0, slots, _stale_history(now, 30), CHEAP_HISTORY, ON)
    assert act is not None
    assert act.phase == "charging"
    assert act.window_start <= now < act.window_end


def test_at_top_soc_reports_holding():
    now = BASE
    slots = _slots(now, [0.01] * 6)
    act = calibration.calibration_action(now, 98.0, slots, _stale_history(now, 30), CHEAP_HISTORY, ON)
    assert act is not None
    assert act.phase == "holding"


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
    just_due = _stale_history(now, ON.calibration_interval_days + 1)
    assert calibration.calibration_action(now, 50.0, slots, just_due, {}, ON) is None
    past_grace = _stale_history(now, ON.calibration_interval_days + const.CALIBRATION_GRACE_DAYS + 1)
    assert calibration.calibration_action(now, 50.0, slots, past_grace, {}, ON) is not None


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
    act = calibration.calibration_action(now, 50.0, slots, mid_grace, CHEAP_HISTORY, ON)
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
    assert calibration.calibration_action(now, 50.0, slots, due, CHEAP_HISTORY, ON) is None


def test_due_at_exact_interval_boundary():
    """days_since == calibration_interval_days exactly must already count as
    due (`>=`, not `>`)."""
    now = BASE
    slots = _slots(now, [0.01] * 6)
    exact = _stale_history(now, ON.calibration_interval_days)
    act = calibration.calibration_action(now, 50.0, slots, exact, CHEAP_HISTORY, ON)
    assert act is not None
    assert act.phase == "charging"


def test_force_at_exact_grace_boundary():
    """days_since == interval + CALIBRATION_GRACE_DAYS exactly must already
    force (`>=`, not `>`).  Slots are priced above CHEAP_HISTORY's bar so only
    the force path can accept."""
    now = BASE
    slots = _slots(now, [0.90] * 6)
    exact = _stale_history(now, ON.calibration_interval_days + const.CALIBRATION_GRACE_DAYS)
    act = calibration.calibration_action(now, 50.0, slots, exact, CHEAP_HISTORY, ON)
    assert act is not None
    assert act.phase == "charging"
