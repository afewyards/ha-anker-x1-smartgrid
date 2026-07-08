from __future__ import annotations

from datetime import datetime, timezone, timedelta
from custom_components.anker_x1_smartgrid import parsers

NOW = datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc)
SUNSET = datetime(2026, 6, 20, 18, 0, tzinfo=timezone.utc)


def test_synth_conserves_energy():
    curve = parsers.synth_pv_curve(6.0, NOW, SUNSET, step_h=1.0)
    total_wh = sum(w * 1.0 for _, w in curve)
    assert abs(total_wh - 6000.0) < 1.0


def test_synth_peaks_in_middle():
    curve = parsers.synth_pv_curve(6.0, NOW, SUNSET, step_h=1.0)
    powers = [w for _, w in curve]
    assert powers[len(powers) // 2] == max(powers)


def test_synth_empty_when_no_energy_or_past_sunset():
    assert parsers.synth_pv_curve(0.0, NOW, SUNSET) == []
    assert parsers.synth_pv_curve(5.0, SUNSET, NOW) == []


def test_synth_interval_starts_increase():
    curve = parsers.synth_pv_curve(6.0, NOW, SUNSET, step_h=1.0)
    starts = [t for t, _ in curve]
    assert starts == sorted(starts)
    assert starts[0] == NOW


TOM_SUNRISE = datetime(2026, 6, 21, 6, 0, tzinfo=timezone.utc)
TOM_SUNSET = datetime(2026, 6, 21, 18, 0, tzinfo=timezone.utc)


# ── A: Migrated build_two_day_pv_curve tests (scalar → single-element list) ──

def test_two_day_curve_concatenates_and_conserves():
    curve = parsers.build_two_day_pv_curve(
        [(2.0, None)], [(6.0, None)], NOW, SUNSET, TOM_SUNRISE, TOM_SUNSET, step_h=1.0
    )
    starts = [t for t, _ in curve]
    assert starts == sorted(starts)
    total_wh = sum(w * 1.0 for _, w in curve)
    assert abs(total_wh - 8000.0) < 1.0  # 2 kWh today + 6 kWh tomorrow


def test_two_day_curve_today_none_only_tomorrow():
    # today_arrays=None: no today PV, but overnight fill [SUNSET, TOM_SUNRISE) is
    # still emitted when a tomorrow segment exists (root-cause fix).
    curve = parsers.build_two_day_pv_curve(
        None, [(6.0, None)], NOW, SUNSET, TOM_SUNRISE, TOM_SUNSET, step_h=1.0
    )
    overnight = [(t, w) for t, w in curve if t < TOM_SUNRISE]
    assert overnight, "overnight fill must be present even when today_arrays=None"
    assert all(w == 0.0 for _, w in overnight)
    assert TOM_SUNRISE in dict(curve)  # tomorrow's PV ramp still present
    # Overnight rows are pv=0 → wh contribution is 0; total energy is tomorrow's 6 kWh.
    assert abs(sum(w for _, w in curve) - 6000.0) < 1.0


def test_two_day_curve_tomorrow_none_only_today():
    curve = parsers.build_two_day_pv_curve(
        [(2.0, None)], None, NOW, SUNSET, TOM_SUNRISE, TOM_SUNSET, step_h=1.0
    )
    assert curve and all(t < SUNSET for t, _ in curve)
    assert abs(sum(w for _, w in curve) - 2000.0) < 1.0


def test_two_day_curve_after_today_sunset_skips_today():
    # now past today's sunset → today segment empty, but overnight fill starts at `now`
    # (root-cause fix: reserve must cover the whole remaining night, not just from sunrise).
    after = SUNSET + timedelta(hours=1)  # 19:00
    curve = parsers.build_two_day_pv_curve(
        [(2.0, None)], [(6.0, None)], after, SUNSET, TOM_SUNRISE, TOM_SUNSET, step_h=1.0
    )
    overnight = [(t, w) for t, w in curve if t < TOM_SUNRISE]
    assert overnight, "overnight fill must start at now even when today segment is empty"
    assert all(w == 0.0 for _, w in overnight)
    assert min(t for t, _ in curve) == after  # fill starts at 19:00, not at SUNSET


# ── B: synth_pv_curve_peaked tests ──────────────────────────────────────────

# Window used for many peaked tests: 10 hours, integer multiple of step_h
PEAK_START = datetime(2026, 6, 21, 6, 0, tzinfo=timezone.utc)
PEAK_END = datetime(2026, 6, 21, 16, 0, tzinfo=timezone.utc)   # 10 h window
PEAK_MID = datetime(2026, 6, 21, 11, 0, tzinfo=timezone.utc)   # midpoint (6+5)


def _total_wh(curve, step_h=1.0):
    return sum(w * step_h for _, w in curve)


# B-1: Energy conservation — symmetric and strongly asymmetric peaks
def test_peaked_energy_conservation_symmetric():
    kwh = 5.0
    curve = parsers.synth_pv_curve_peaked(kwh, PEAK_START, PEAK_END, PEAK_MID)
    assert abs(_total_wh(curve) / 1000.0 - kwh) < 1e-9


def test_peaked_energy_conservation_early_peak():
    kwh = 5.0
    early_peak = PEAK_START + timedelta(hours=1)
    curve = parsers.synth_pv_curve_peaked(kwh, PEAK_START, PEAK_END, early_peak)
    assert abs(_total_wh(curve) / 1000.0 - kwh) < 1e-9


def test_peaked_energy_conservation_late_peak():
    kwh = 5.0
    late_peak = PEAK_END - timedelta(hours=1)
    curve = parsers.synth_pv_curve_peaked(kwh, PEAK_START, PEAK_END, late_peak)
    assert abs(_total_wh(curve) / 1000.0 - kwh) < 1e-9


# B-2: Max-bucket is at or adjacent to the bucket containing peak
def test_peaked_max_bucket_near_peak():
    peak = PEAK_START + timedelta(hours=3)  # 09:00 in a 06:00-16:00 window
    curve = parsers.synth_pv_curve_peaked(5.0, PEAK_START, PEAK_END, peak)
    max_t, _ = max(curve, key=lambda x: x[1])
    # the max bucket's LEFT edge should be within 1 step of peak
    assert abs((max_t - peak).total_seconds()) <= 3600.0 + 1e-6


# B-3: Front-loading (early peak → more energy in first half of window than second)
def test_peaked_early_peak_front_loaded():
    """An early peak concentrates energy in the first half of the window."""
    early_peak = PEAK_START + timedelta(hours=2)
    curve = parsers.synth_pv_curve_peaked(5.0, PEAK_START, PEAK_END, early_peak)
    mid_window = PEAK_START + (PEAK_END - PEAK_START) / 2
    first_half = sum(w for t, w in curve if t < mid_window)
    second_half = sum(w for t, w in curve if t >= mid_window)
    assert first_half > second_half


# B-4: Back-loading (late peak → more energy in second half of window than first)
def test_peaked_late_peak_back_loaded():
    """A late peak concentrates energy in the second half of the window."""
    late_peak = PEAK_END - timedelta(hours=2)
    curve = parsers.synth_pv_curve_peaked(5.0, PEAK_START, PEAK_END, late_peak)
    mid_window = PEAK_START + (PEAK_END - PEAK_START) / 2
    first_half = sum(w for t, w in curve if t < mid_window)
    second_half = sum(w for t, w in curve if t >= mid_window)
    assert second_half > first_half


# B-5: Bucket-0 non-zero for an interior peak (center-sampling guard)
def test_peaked_bucket_zero_nonzero():
    interior_peak = PEAK_START + timedelta(hours=5)
    curve = parsers.synth_pv_curve_peaked(5.0, PEAK_START, PEAK_END, interior_peak)
    assert len(curve) > 0
    assert curve[0][1] > 0.0


# B-6: Legacy parity EXACT for integer-hour window (peak = midpoint)
def test_peaked_legacy_parity_exact_integer_window():
    """synth_pv_curve_peaked(kwh, start, end, midpoint) == synth_pv_curve(kwh, start, end)
    per bucket for an integer-hour window."""
    kwh = 4.0
    start = datetime(2026, 6, 21, 6, 0, tzinfo=timezone.utc)
    end = datetime(2026, 6, 21, 16, 0, tzinfo=timezone.utc)  # exactly 10 h
    midpoint = start + (end - start) / 2

    peaked = parsers.synth_pv_curve_peaked(kwh, start, end, midpoint)
    legacy = parsers.synth_pv_curve(kwh, start, end)

    assert len(peaked) == len(legacy)
    for (t_p, w_p), (t_l, w_l) in zip(peaked, legacy):
        assert t_p == t_l
        assert abs(w_p - w_l) < 1e-9, f"mismatch at {t_p}: peaked={w_p}, legacy={w_l}"


# B-7: Legacy parity within tolerance for fractional-hour window (10.5 h)
def test_peaked_legacy_parity_fractional_window():
    """For a fractional-hour window, synth_pv_curve_peaked(peak=midpoint) and
    synth_pv_curve produce structurally equivalent curves: same timestamps, both
    conserve energy, and the peak bucket is at the same or adjacent position.

    Per-bucket watts differ in the tails because synth_pv_curve stretches the
    half-sine over n=ceil(window) virtual slots, while synth_pv_curve_peaked uses
    the actual window midpoint — so we do NOT assert per-bucket equality here."""
    kwh = 4.0
    start = datetime(2026, 6, 21, 6, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=10.5)
    midpoint = start + (end - start) / 2

    peaked = parsers.synth_pv_curve_peaked(kwh, start, end, midpoint)
    legacy = parsers.synth_pv_curve(kwh, start, end)

    # Same number of buckets and identical timestamps
    assert len(peaked) == len(legacy)
    assert [t for t, _ in peaked] == [t for t, _ in legacy]
    # Both conserve energy exactly
    assert abs(_total_wh(peaked) / 1000.0 - kwh) < 1e-9
    assert abs(_total_wh(legacy) / 1000.0 - kwh) < 1e-9
    # Peak bucket at the same or adjacent index (shapes are similar)
    peaked_max_idx = max(range(len(peaked)), key=lambda i: peaked[i][1])
    legacy_max_idx = max(range(len(legacy)), key=lambda i: legacy[i][1])
    assert abs(peaked_max_idx - legacy_max_idx) <= 1


# B-8: Timezone — peak with non-UTC offset sorts correctly; no crash
def test_peaked_non_utc_peak_timezone():
    tz_plus2 = timezone(timedelta(hours=2))
    # 14:00+02:00 == 12:00 UTC — interior point of [06:00 UTC, 20:00 UTC]
    peak_local = datetime(2026, 6, 21, 14, 0, tzinfo=tz_plus2)
    start = datetime(2026, 6, 21, 6, 0, tzinfo=timezone.utc)
    end = datetime(2026, 6, 21, 20, 0, tzinfo=timezone.utc)

    curve = parsers.synth_pv_curve_peaked(5.0, start, end, peak_local)
    assert len(curve) > 0
    timestamps = [t for t, _ in curve]
    assert timestamps == sorted(timestamps)
    assert timestamps[0] == start


# B-9: Degenerate inputs
def test_peaked_degenerate_zero_kwh():
    assert parsers.synth_pv_curve_peaked(0.0, PEAK_START, PEAK_END, PEAK_MID) == []


def test_peaked_degenerate_negative_kwh():
    assert parsers.synth_pv_curve_peaked(-1.0, PEAK_START, PEAK_END, PEAK_MID) == []


def test_peaked_degenerate_end_le_start():
    assert parsers.synth_pv_curve_peaked(5.0, PEAK_END, PEAK_START, PEAK_MID) == []
    assert parsers.synth_pv_curve_peaked(5.0, PEAK_START, PEAK_START, PEAK_MID) == []


def test_peaked_degenerate_peak_le_start_monotone_falling():
    """peak at or before start → pure falling lobe (monotonically non-increasing watts)."""
    peak_before = PEAK_START - timedelta(hours=1)
    curve = parsers.synth_pv_curve_peaked(5.0, PEAK_START, PEAK_END, peak_before)
    assert len(curve) > 1
    watts = [w for _, w in curve]
    assert all(w1 >= w2 for w1, w2 in zip(watts, watts[1:]))


def test_peaked_degenerate_peak_ge_end_monotone_rising():
    """peak at or beyond end → pure rising lobe (monotonically non-decreasing watts)."""
    peak_after = PEAK_END + timedelta(hours=1)
    curve = parsers.synth_pv_curve_peaked(5.0, PEAK_START, PEAK_END, peak_after)
    assert len(curve) > 1
    watts = [w for _, w in curve]
    assert all(w1 <= w2 for w1, w2 in zip(watts, watts[1:]))


# ── C: build_pv_curve_from_arrays tests ─────────────────────────────────────

# Shared window for array tests: 06:00-20:00 UTC (14 h, 14 buckets at step_h=1)
ARR_START = datetime(2026, 6, 21, 6, 0, tzinfo=timezone.utc)
ARR_END = datetime(2026, 6, 21, 20, 0, tzinfo=timezone.utc)
PEAK_09 = datetime(2026, 6, 21, 9, 0, tzinfo=timezone.utc)
PEAK_17 = datetime(2026, 6, 21, 17, 0, tzinfo=timezone.utc)


def _count_above_half_max(curve):
    max_w = max(w for _, w in curve)
    return sum(1 for _, w in curve if w >= 0.5 * max_w)


# C-1: Two arrays at 09:00 and 17:00 → broad, flatter-topped hump
def test_arrays_two_peaks_broad_hump():
    kwh1, kwh2 = 3.0, 3.0
    arrays: list[tuple[float, datetime | None]] = [(kwh1, PEAK_09), (kwh2, PEAK_17)]
    curve = parsers.build_pv_curve_from_arrays(arrays, ARR_START, ARR_END)

    # (i) total energy ≈ sum of kWh
    total_kwh = sum(w * 1.0 for _, w in curve) / 1000.0
    assert abs(total_kwh - (kwh1 + kwh2)) < 1e-6

    # (ii) broader top: more buckets above 50%-max than a single lobe of same energy
    mid = ARR_START + (ARR_END - ARR_START) / 2
    single = parsers.synth_pv_curve_peaked(kwh1 + kwh2, ARR_START, ARR_END, mid)
    assert _count_above_half_max(curve) > _count_above_half_max(single)

    # (iii) valley filled: watt at inter-peak midpoint ≥ 60% of curve max
    inter_mid = PEAK_09 + (PEAK_17 - PEAK_09) / 2  # 13:00 UTC
    mid_watts = min(curve, key=lambda x: abs((x[0] - inter_mid).total_seconds()))[1]
    assert mid_watts >= 0.6 * max(w for _, w in curve)


# C-2: peak=None array peaks at the window midpoint bucket
def test_arrays_none_peak_uses_midpoint():
    """A single array with peak=None should produce a curve peaking at the window midpoint."""
    kwh = 5.0
    curve = parsers.build_pv_curve_from_arrays([(kwh, None)], ARR_START, ARR_END)
    midpoint = ARR_START + (ARR_END - ARR_START) / 2
    max_t = max(curve, key=lambda x: x[1])[0]
    # max bucket's left edge should be within 1 step of midpoint
    assert abs((max_t - midpoint).total_seconds()) <= 3600.0 + 1e-6


# C-3: Empty arrays → []
def test_arrays_empty_returns_empty():
    assert parsers.build_pv_curve_from_arrays([], ARR_START, ARR_END) == []


# C-4: end <= start → []
def test_arrays_inverted_window_returns_empty():
    assert parsers.build_pv_curve_from_arrays([(3.0, PEAK_09)], ARR_END, ARR_START) == []
    assert parsers.build_pv_curve_from_arrays([(3.0, PEAK_09)], ARR_START, ARR_START) == []


# C-5: Shared grid — two arrays → unique strictly-increasing timestamps (no duplicates)
def test_arrays_shared_grid_no_duplicate_timestamps():
    arrays: list[tuple[float, datetime | None]] = [(3.0, PEAK_09), (3.0, PEAK_17)]
    curve = parsers.build_pv_curve_from_arrays(arrays, ARR_START, ARR_END)
    timestamps = [t for t, _ in curve]
    assert timestamps == sorted(set(timestamps))  # sorted unique = no dups
    assert len(timestamps) == len(set(timestamps))


# ── Regression: negative-power bug in fall branch (fractional windows, frac in (0,0.5)) ──

def test_peaked_no_negative_watts_fractional_window_interior_peak():
    """When window length has a fractional step_h part in (0, 0.5), the last bucket
    center lands past 'end', making (end-t)<0 → negative weight in the fall branch.
    All emitted watts must be >= 0, and energy must still be conserved."""
    start = PEAK_START  # 2026-06-21 06:00 UTC
    end = start + timedelta(hours=10.2)  # frac=0.2 ∈ (0, 0.5) → triggers bug
    peak = start + timedelta(hours=5)    # interior peak
    kwh = 4.0

    curve = parsers.synth_pv_curve_peaked(kwh, start, end, peak)

    assert all(w >= 0.0 for _, w in curve), (
        f"negative watt in curve: {[(str(t), w) for t, w in curve if w < 0]}"
    )
    assert abs(_total_wh(curve) / 1000.0 - kwh) < 1e-9


def test_peaked_no_negative_watts_pure_falling_fractional_window():
    """Pure-falling lobe (peak <= start) over a short fractional window can also
    produce a past-end center in the fall branch → large negative.  All watts >= 0."""
    start = PEAK_START
    end = start + timedelta(hours=3.2)   # frac=0.2 ∈ (0, 0.5)
    peak = start - timedelta(hours=1)    # before start → clamped to start → pure fall
    kwh = 2.0

    curve = parsers.synth_pv_curve_peaked(kwh, start, end, peak)

    assert all(w >= 0.0 for _, w in curve), (
        f"negative watt in curve: {[(str(t), w) for t, w in curve if w < 0]}"
    )
    assert abs(_total_wh(curve) / 1000.0 - kwh) < 1e-9


# ── Plan A: overnight gap-fill (pv=0) between today's and tomorrow's segments ──

def test_two_day_curve_fills_overnight_gap_with_zero_pv():
    curve = parsers.build_two_day_pv_curve(
        [(2.0, None)], [(6.0, None)], NOW, SUNSET, TOM_SUNRISE, TOM_SUNSET, step_h=1.0
    )
    pts = dict(curve)
    # Every overnight hour [SUNSET, TOM_SUNRISE) is present with pv=0.
    h = SUNSET
    while h < TOM_SUNRISE:
        assert h in pts, f"missing overnight fill point at {h}"
        assert pts[h] == 0.0, f"overnight fill at {h} must be pv=0, got {pts[h]}"
        h += timedelta(hours=1)
    # Strictly increasing, unique timestamps (fill abuts both segments cleanly).
    starts = [t for t, _ in curve]
    assert starts == sorted(starts)
    assert len(starts) == len(set(starts))
    # First fill point (SUNSET=18:00) is exactly 1 h after today's last real point (17:00).
    last_real = max(t for t, _ in curve if t < SUNSET)
    assert SUNSET - last_real == timedelta(hours=1)
    # tomorrow's first PV point is exactly 1 h after the last fill point (05:00 → 06:00).
    assert TOM_SUNRISE in pts


def test_two_day_curve_gap_fill_snaps_nonhour_sunset_to_hour():
    sunset = datetime(2026, 6, 20, 21, 43, tzinfo=timezone.utc)
    sunrise = datetime(2026, 6, 21, 5, 0, tzinfo=timezone.utc)
    sunset2 = datetime(2026, 6, 21, 21, 0, tzinfo=timezone.utc)
    curve = parsers.build_two_day_pv_curve(
        [(2.0, None)], [(6.0, None)], NOW, sunset, sunrise, sunset2, step_h=1.0
    )
    pts = dict(curve)
    first_fill = datetime(2026, 6, 20, 22, 0, tzinfo=timezone.utc)
    # 21:43 snaps up to 22:00 for the first fill point (pv=0).
    assert pts.get(first_fill) == 0.0
    # No fill point lands inside the partial hour [21:43, 22:00).
    assert all(not (sunset <= t < first_fill) for t, _ in curve)
    # today's last REAL PV point is 21:00 (window [12:00, 21:43) → left-edge 12:00..21:00);
    # the 22:00 fill abuts it by exactly 1 h — no overlap, no gap.
    last_real = max(t for t, _ in curve if t < sunset)
    assert last_real == datetime(2026, 6, 20, 21, 0, tzinfo=timezone.utc)
    assert first_fill - last_real == timedelta(hours=1)
    # Fill continues hourly up to 04:00 (< sunrise 05:00).
    assert pts.get(datetime(2026, 6, 21, 4, 0, tzinfo=timezone.utc)) == 0.0


def test_two_day_curve_gap_fill_when_today_segment_empty():
    # now past today's sunset → today segment empty, BUT the overnight gap is still
    # filled with pv=0 rows from `now` to tomorrow's sunrise (root-cause fix: the
    # reserve must see the whole night, not just from sunrise).
    after = SUNSET + timedelta(hours=1)  # 19:00, past 18:00 sunset
    curve = parsers.build_two_day_pv_curve(
        [(2.0, None)], [(6.0, None)], after, SUNSET, TOM_SUNRISE, TOM_SUNSET, step_h=1.0
    )
    pts = dict(curve)
    overnight = [(t, w) for t, w in curve if t < TOM_SUNRISE]
    # Overnight rows now present, all pv=0, starting at `now`'s hour, hourly to sunrise.
    assert overnight, "overnight gap must be filled even when today segment is empty"
    assert all(w == 0.0 for _, w in overnight)
    assert min(t for t, _ in curve) == after  # fill starts at `now` (19:00), not sunrise
    assert pts.get(datetime(2026, 6, 21, 5, 0, tzinfo=timezone.utc)) == 0.0  # 05:00 < sunrise
    assert TOM_SUNRISE in pts  # tomorrow's PV ramp still present


def test_two_day_curve_no_gap_fill_when_tomorrow_arrays_empty():
    # tomorrow_arrays=[] (falsy) → NO overnight fill; today segment present but
    # fill gate requires a truthy tomorrow.  Curve contains only today's PV points.
    curve = parsers.build_two_day_pv_curve(
        [(2.0, None)], [], NOW, SUNSET, TOM_SUNRISE, TOM_SUNSET, step_h=1.0
    )
    # With empty tomorrow, the curve is only today's PV (all points before SUNSET).
    assert curve and all(t < SUNSET for t, _ in curve)
    # No fill points in [SUNSET, TOM_SUNRISE) — the gap must be absent.
    assert not any(SUNSET <= t < TOM_SUNRISE for t, _ in curve)
