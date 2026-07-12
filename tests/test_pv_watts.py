"""Tests for the watts-based PV curve and coordinator readers (TDD: red → green)."""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone, UTC

import pytest

from custom_components.anker_x1_smartgrid import const, coordinator
from custom_components.anker_x1_smartgrid.parsers import build_pv_curve_from_watts
from tests.conftest import ANKER_TEST_ENTITIES

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

UTC = UTC
TZ_PLUS2 = timezone(timedelta(hours=2))


def _dt(h: int, m: int = 0, tz=UTC) -> datetime:
    return datetime(2026, 6, 27, h, m, tzinfo=tz)


def _bell_samples() -> list[tuple[datetime, float]]:
    """Realistic 15-min bell curve on 2026-06-27 (UTC datetimes).

    PV starts ~04:00 UTC (06:00 local +02:00), peaks ~11:00 UTC (13:00 local),
    ends ~20:00 UTC (22:00 local).  Night hours are 0.

    The key regression: the OLD synthetic code produced a MONOTONICALLY RISING
    curve up to ~21:00 UTC.  The real watts data should peak at midday.
    """
    import math

    samples: list[tuple[datetime, float]] = []
    # half-sine over [04:00, 20:00] UTC
    start_h = 4.0
    end_h = 20.0
    peak_w = 2000.0

    h = start_h
    while h <= end_h + 1e-9:
        norm = (h - start_h) / (end_h - start_h)  # 0..1
        w = max(0.0, peak_w * math.sin(math.pi * norm))
        hrs = int(h)
        mins = round((h - hrs) * 60)
        if mins == 60:
            hrs += 1
            mins = 0
        samples.append((_dt(hrs, mins), w))
        h += 0.25  # 15-min steps
    return samples


# ===========================================================================
# build_pv_curve_from_watts — parsers tests
# ===========================================================================


# ── 1: Bell shape (regression for monotonic-rise bug) ──────────────────────


def test_build_pv_curve_from_watts_bell_shape():
    """Curve should peak at midday (before 16:00 UTC), not be monotonically rising,
    and fall to ~0 at the end of the solar window.

    This is the regression test for the bug where the synthetic curve rose monotonically
    to ~21:00 local time because the quarter-sine peaked at/after sunset.
    """
    samples = _bell_samples()
    now = _dt(6, 0)  # 06:00 UTC — well before peak

    curve = build_pv_curve_from_watts([samples], None, now)

    assert curve, "Expected non-empty curve"

    max_t, max_w = max(curve, key=lambda x: x[1])

    # Peak must be in the midday window [10:00, 16:00) UTC — NOT at/near the last point
    assert 10 <= max_t.hour < 16, f"Peak expected between 10:00-16:00 UTC (midday), got {max_t.hour}:00 UTC"

    # The peak must NOT be the last point in the curve (that was the monotonic-rise bug)
    assert curve[-1][0] != max_t, "Peak must not be the last point (monotonic rise bug)"

    # The bell's last sample (at 20:00 UTC = 22:00 local) should be near 0
    assert curve[-1][1] < max_w * 0.05, (
        f"Last bucket {curve[-1][0].hour}:00 UTC has {curve[-1][1]:.1f}W — expected ~0 at end of solar window"
    )

    # The watts sequence is NOT monotonically non-decreasing — it must decrease after peak
    watts = [w for _, w in curve]
    assert not all(a <= b for a, b in itertools.pairwise(watts)), (
        "Curve is monotonically rising — that is the bug we are fixing"
    )


# ── 2: Timezone bucketing ──────────────────────────────────────────────────


def test_build_pv_curve_from_watts_tz_bucketing():
    """A sample at 11:30+02:00 (= 09:30 UTC) must land in the 09:00Z hourly bucket."""
    sample_dt = datetime(2026, 6, 27, 11, 30, tzinfo=TZ_PLUS2)  # == 09:30 UTC
    samples = [(sample_dt, 1130.0)]
    now = _dt(9, 0)  # current hour is 09:00 UTC

    curve = build_pv_curve_from_watts([samples], None, now)

    assert len(curve) == 1, f"Expected 1 bucket, got {len(curve)}: {curve}"
    t, w = curve[0]
    assert t == _dt(9, 0), f"Expected 09:00Z bucket, got {t}"
    assert abs(w - 1130.0) < 1.0, f"Expected ~1130W, got {w}"


# ── 3: Multi-array: today+tomorrow samples merge into the combined output ──


def test_build_pv_curve_from_watts_multi_source_sums_hourly_means():
    """Multiple SOURCES in today_sources each resample to their own hourly mean
    FIRST, then SUM across sources (H2) — not pooled into one cross-source mean.
    src_a mean=600 (500,700) + src_b mean=300 (300) = 900, NOT pooled mean 500."""
    src_a = [(_dt(10, 0), 500.0), (_dt(10, 30), 700.0)]
    src_b = [(_dt(10, 15), 300.0)]
    now = _dt(9, 0)

    curve = build_pv_curve_from_watts([src_a, src_b], None, now)

    pts = {t: w for t, w in curve}
    assert _dt(10, 0) in pts, "Expected 10:00Z bucket"
    assert abs(pts[_dt(10, 0)] - 900.0) < 1.0, f"Expected sum 900W at 10:00Z, got {pts[_dt(10, 0)]}"


def test_build_pv_curve_from_watts_disjoint_today_and_tomorrow():
    """Non-overlapping today (10:xx) and tomorrow (22:xx) appear in separate buckets."""
    today = [(_dt(10, 0), 800.0), (_dt(10, 30), 900.0)]
    tomorrow = [(_dt(22, 0), 50.0)]  # late tonight / tomorrow
    now = _dt(9, 0)

    curve = build_pv_curve_from_watts([today], [tomorrow], now)

    pts = {t: w for t, w in curve}
    assert _dt(10, 0) in pts
    assert abs(pts[_dt(10, 0)] - 850.0) < 1.0  # mean(800, 900) = 850
    assert _dt(22, 0) in pts
    assert abs(pts[_dt(22, 0)] - 50.0) < 1.0


# ── 4: now slicing ──────────────────────────────────────────────────────────


def test_build_pv_curve_from_watts_now_slicing():
    """Hours strictly before now's top-of-hour are dropped; current and future kept."""
    samples = [
        (_dt(8, 0), 100.0),  # falls in 08:00 bucket — strictly before now_h=09:00 → drop
        (_dt(8, 30), 150.0),  # same 08:00 bucket → drop
        (_dt(9, 0), 500.0),  # 09:00 bucket — keep (current hour)
        (_dt(9, 30), 600.0),  # 09:00 bucket — keep (merged)
        (_dt(10, 0), 800.0),  # 10:00 bucket — keep
    ]
    now = _dt(9, 45)  # now is 09:45, so now_h = 09:00

    curve = build_pv_curve_from_watts([samples], None, now)

    pts = {t: w for t, w in curve}
    assert _dt(8, 0) not in pts, "08:00 bucket should be dropped (before now)"
    assert _dt(9, 0) in pts, "09:00 bucket should be present (current hour)"
    assert _dt(10, 0) in pts, "10:00 bucket should be present (future)"
    # mean of 09:00 samples (500, 600) = 550
    assert abs(pts[_dt(9, 0)] - 550.0) < 1.0, f"Expected mean 550W at 09:00, got {pts[_dt(9, 0)]}"


# ── 5: Degenerate inputs ────────────────────────────────────────────────────


def test_build_pv_curve_from_watts_none_inputs_return_empty():
    """None today and tomorrow → []."""
    assert build_pv_curve_from_watts(None, None, _dt(9, 0)) == []


def test_build_pv_curve_from_watts_empty_lists_return_empty():
    """Empty sample lists → []."""
    assert build_pv_curve_from_watts([], [], _dt(9, 0)) == []


def test_build_pv_curve_from_watts_only_today_empty():
    """today=[] and tomorrow=None → []."""
    assert build_pv_curve_from_watts([], None, _dt(9, 0)) == []


def test_build_pv_curve_from_watts_sorted_output():
    """Output is sorted by timestamp (ascending)."""
    samples = [
        (_dt(12, 0), 1000.0),
        (_dt(10, 0), 800.0),
        (_dt(11, 0), 900.0),
    ]
    curve = build_pv_curve_from_watts([samples], None, _dt(9, 0))
    timestamps = [t for t, _ in curve]
    assert timestamps == sorted(timestamps), "Output must be sorted by time"


def test_build_pv_curve_mixed_cadence_sums_hourly_means_not_pooled():
    """Two sources of different cadence each resample to hourly FIRST, then sum:
    hourly A={:00→1000} + 30-min B={:00→500,:30→600} → 1000 + 550 = 1550 W,
    NOT the pooled {1500,600}/2 = 1050."""

    def dt(h, m=0):
        return datetime(2026, 7, 8, h, m, tzinfo=UTC)

    src_a = [(dt(9), 1000.0)]
    src_b = [(dt(9), 500.0), (dt(9, 30), 600.0)]
    curve = build_pv_curve_from_watts([src_a, src_b], None, dt(9), step_h=1.0)
    assert curve == [(dt(9), pytest.approx(1550.0))]


async def test_read_pv_today_watts_returns_per_source_arrays(hass):
    from custom_components.anker_x1_smartgrid import coordinator, const

    hass.states.async_set("sensor.a", "1.0", {"watts": {"2026-07-08T09:00:00+00:00": 1000}})
    hass.states.async_set(
        "sensor.b", "1.0", {"watts": {"2026-07-08T09:00:00+00:00": 500, "2026-07-08T09:30:00+00:00": 600}}
    )
    d = {const.CONF_ENT_PV_TODAY: ["sensor.a", "sensor.b"]}
    result = coordinator.read_pv_today_watts(hass, d)
    assert isinstance(result, list) and len(result) == 2
    assert all(isinstance(src, list) for src in result)


# ===========================================================================
# coordinator.read_pv_today_watts / read_pv_tomorrow_watts
# ===========================================================================

_TODAY_REMAINING = "sensor.home_energy_production_today_remaining"
_TODAY_BASE = "sensor.home_energy_production_today"
_TOMORROW = "sensor.home_energy_production_tomorrow"

_WATTS_TODAY = {
    "2026-06-27T11:00:00+02:00": 800,
    "2026-06-27T11:30:00+02:00": 1100,
    "2026-06-27T12:00:00+02:00": 1300,
}

_WATTS_TOMORROW = {
    "2026-06-28T11:00:00+02:00": 600,
    "2026-06-28T11:30:00+02:00": 900,
}


def _data():
    return {**const.DEFAULT_ENTITIES, **ANKER_TEST_ENTITIES}


# ── read_pv_today_watts ─────────────────────────────────────────────────────


async def test_read_pv_today_watts_resolves_remaining_to_sibling(hass):
    """When the configured entity is *_remaining (no watts attr), resolve
    to the sibling entity (strip _remaining) which has the watts dict."""
    d = _data()
    # Default config: CONF_ENT_PV_TODAY = ["sensor.home_energy_production_today_remaining"]
    # _remaining has no watts attribute; sibling (base) has watts.
    hass.states.async_set(_TODAY_REMAINING, "3.5", {})
    hass.states.async_set(_TODAY_BASE, "3.5", {"watts": _WATTS_TODAY})

    result = coordinator.read_pv_today_watts(hass, d)

    assert result is not None, "Expected samples, got None (sibling resolution failed)"
    assert len(result) == 1, f"Expected 1 per-source array (one entity resolved), got {len(result)}"
    samples = result[0]
    assert len(samples) == 3, f"Expected 3 samples (one per watts key), got {len(samples)}"

    # All datetimes must be UTC-aware
    for dt_val, w in samples:
        offset = dt_val.tzinfo.utcoffset(dt_val).total_seconds()
        assert offset == 0, f"Expected UTC datetime, got offset={offset}s at {dt_val}"
        assert w >= 0.0

    # Key "2026-06-27T11:30:00+02:00" → 09:30 UTC must be present
    times = {dt_val for dt_val, _ in samples}
    assert datetime(2026, 6, 27, 9, 30, tzinfo=UTC) in times, "Expected 09:30Z from key 2026-06-27T11:30:00+02:00"


async def test_read_pv_today_watts_returns_none_when_no_entity_has_watts(hass):
    """Returns None when entity list is non-empty but NO entity has a watts attribute."""
    d = _data()
    # Both _remaining AND base have no watts attr
    hass.states.async_set(_TODAY_REMAINING, "3.5", {})
    hass.states.async_set(_TODAY_BASE, "3.5", {})

    result = coordinator.read_pv_today_watts(hass, d)

    assert result is None, f"Expected None when no watts available, got {result}"


async def test_read_pv_today_watts_empty_list_returns_empty(hass):
    """Empty CONF_ENT_PV_TODAY → [] (not None)."""
    d = _data()
    d[const.CONF_ENT_PV_TODAY] = []

    result = coordinator.read_pv_today_watts(hass, d)

    assert result == [], f"Expected [] for empty entity list, got {result}"


async def test_read_pv_today_watts_direct_entity_with_watts(hass):
    """When configured entity itself has watts, use it directly (no sibling lookup)."""
    d = _data()
    d[const.CONF_ENT_PV_TODAY] = [_TODAY_BASE]
    hass.states.async_set(_TODAY_BASE, "3.5", {"watts": _WATTS_TODAY})

    result = coordinator.read_pv_today_watts(hass, d)

    assert result is not None
    assert len(result) == 1, f"Expected 1 per-source array, got {len(result)}"
    assert len(result[0]) == 3, f"Expected 3 samples from _WATTS_TODAY, got {len(result[0])}"


async def test_read_pv_today_watts_multi_string_returns_separate_source_arrays(hass):
    """Two PV string entities: each source's samples are kept SEPARATE (per-source
    arrays) — cross-source summing now happens downstream in
    build_pv_curve_from_watts (H2), not in the coordinator reader."""
    d = _data()
    ent1 = "sensor.pv_string_1"
    ent2 = "sensor.pv_string_2"
    d[const.CONF_ENT_PV_TODAY] = [ent1, ent2]

    watts1 = {
        "2026-06-27T11:00:00+00:00": 500,
        "2026-06-27T11:30:00+00:00": 700,
    }
    watts2 = {
        "2026-06-27T11:00:00+00:00": 300,
        "2026-06-27T11:30:00+00:00": 200,
    }

    hass.states.async_set(ent1, "3.0", {"watts": watts1})
    hass.states.async_set(ent2, "2.0", {"watts": watts2})

    result = coordinator.read_pv_today_watts(hass, d)

    assert result is not None
    assert len(result) == 2, f"Expected 2 per-source arrays, got {len(result)}"

    t1100 = datetime(2026, 6, 27, 11, 0, tzinfo=UTC)
    t1130 = datetime(2026, 6, 27, 11, 30, tzinfo=UTC)

    src1 = {dt_val: w for dt_val, w in result[0]}
    src2 = {dt_val: w for dt_val, w in result[1]}
    assert abs(src1[t1100] - 500.0) < 1.0
    assert abs(src2[t1100] - 300.0) < 1.0
    assert abs(src1[t1130] - 700.0) < 1.0
    assert abs(src2[t1130] - 200.0) < 1.0

    # Cross-source sum now happens in build_pv_curve_from_watts, not here.
    # step_h=0.5 keeps the 11:00/11:30 samples in separate buckets (default
    # step_h=1.0 would pool both half-hour samples of EACH source into one
    # hourly mean before summing — a different, also-correct H2 behaviour,
    # just not what this per-timestamp assertion is checking).
    curve = build_pv_curve_from_watts(result, None, datetime(2026, 6, 27, 9, 0, tzinfo=UTC), step_h=0.5)
    pts = {t: w for t, w in curve}
    assert abs(pts[t1100] - 800.0) < 1.0, f"Expected 500+300=800W at 11:00Z (sum), got {pts[t1100]}"
    assert abs(pts[t1130] - 900.0) < 1.0, f"Expected 700+200=900W at 11:30Z (sum), got {pts[t1130]}"


# ── read_pv_tomorrow_watts ──────────────────────────────────────────────────


async def test_read_pv_tomorrow_watts_returns_samples(hass):
    """read_pv_tomorrow_watts returns watts samples from tomorrow entity."""
    d = _data()
    d[const.CONF_ENT_PV_TOMORROW] = [_TOMORROW]
    hass.states.async_set(_TOMORROW, "5.0", {"watts": _WATTS_TOMORROW})

    result = coordinator.read_pv_tomorrow_watts(hass, d)

    assert result is not None
    assert len(result) == 1, f"Expected 1 per-source array, got {len(result)}"
    assert len(result[0]) == 2

    # Timestamps must be UTC
    for dt_val, w in result[0]:
        offset = dt_val.tzinfo.utcoffset(dt_val).total_seconds()
        assert offset == 0, f"Expected UTC, got offset={offset}s"


async def test_read_pv_tomorrow_watts_none_when_no_watts(hass):
    """Returns None when tomorrow entity has no watts attribute."""
    d = _data()
    d[const.CONF_ENT_PV_TOMORROW] = [_TOMORROW]
    hass.states.async_set(_TOMORROW, "5.0", {})

    result = coordinator.read_pv_tomorrow_watts(hass, d)

    assert result is None


async def test_read_pv_tomorrow_watts_empty_list_returns_empty(hass):
    """Empty CONF_ENT_PV_TOMORROW → []."""
    d = _data()
    d[const.CONF_ENT_PV_TOMORROW] = []

    result = coordinator.read_pv_tomorrow_watts(hass, d)

    assert result == []


# ===========================================================================
# H1 — contiguity: overnight gaps must be filled with 0.0
# ===========================================================================


def _dt2(h: int, m: int = 0, tz=UTC) -> datetime:
    """Datetime for 2026-06-28 (tomorrow) at the given UTC hour/minute."""
    return datetime(2026, 6, 28, h, m, tzinfo=tz)


def test_build_pv_curve_from_watts_contiguous_hourly_no_overnight_gap():
    """Curve must contain a point for EVERY hour between first and last bucket.

    Daylight-only source data produces an overnight gap (20:00 today → 06:00 tomorrow).
    The function must fill that gap with 0.0 watts so downstream consumers that iterate
    hour-by-hour (build_intervals gap math) do not see a multi-hour hole.
    """
    # Today: daylight samples only (06:00–20:00 UTC on 06-27)
    today = [
        (_dt(6, 0), 50.0),
        (_dt(10, 0), 1200.0),
        (_dt(19, 0), 300.0),
        (_dt(20, 0), 0.0),  # last daylight point
    ]
    # Tomorrow: daylight samples only (06:00–20:00 UTC on 06-28)
    tomorrow = [
        (_dt2(6, 0), 80.0),
        (_dt2(10, 0), 1100.0),
        (_dt2(19, 0), 250.0),
        (_dt2(20, 0), 0.0),
    ]
    now = _dt(5, 0)  # 05:00 UTC — before today's first sample

    curve = build_pv_curve_from_watts([today], [tomorrow], now)

    pts = {t: w for t, w in curve}
    timestamps = sorted(pts.keys())

    # There must be a point for EVERY hour between first and last
    first = timestamps[0]
    last = timestamps[-1]
    h = first
    while h <= last:
        assert h in pts, f"Missing hourly point at {h} — overnight gap was not filled with 0.0"
        h += timedelta(hours=1)

    # Overnight hours (21:00 06-27 → 05:00 06-28) must be exactly 0.0
    overnight_hours = [datetime(2026, 6, 27, h_, 0, tzinfo=UTC) for h_ in range(21, 24)] + [
        datetime(2026, 6, 28, h_, 0, tzinfo=UTC) for h_ in range(0, 6)
    ]
    for t in overnight_hours:
        if first <= t <= last:
            assert pts[t] == 0.0, f"Overnight hour {t} should be 0.0W, got {pts[t]}"


def test_build_pv_curve_from_watts_single_day_no_gap_fill_needed():
    """Single-day samples with no overnight → contiguous per spec, no fill needed."""
    today = [(_dt(6, 0), 100.0), (_dt(7, 0), 200.0), (_dt(8, 0), 100.0)]
    now = _dt(5, 0)

    curve = build_pv_curve_from_watts([today], None, now)

    pts = {t: w for t, w in curve}
    # 06, 07, 08 must all be present — no gaps in a 3-hour contiguous daylight block
    assert _dt(6, 0) in pts
    assert _dt(7, 0) in pts
    assert _dt(8, 0) in pts
    assert len(pts) == 3


# ===========================================================================
# M1 — naive-key safety: a naive datetime key must be treated as UTC
# ===========================================================================


async def test_read_pv_today_watts_naive_key_treated_as_utc(hass):
    """A watts dict key with NO timezone suffix must be interpreted as UTC 11:00Z.

    The coordinator must use parsers._parse_dt (which treats naive as UTC) rather
    than datetime.astimezone() which would use the system-local timezone and produce
    wrong UTC times on non-UTC hosts (e.g. Amsterdam +02:00 → 09:00Z instead of 11:00Z).
    """
    d = _data()
    ent = "sensor.pv_naive_key"
    d[const.CONF_ENT_PV_TODAY] = [ent]
    # Naive key — no +HH:MM or Z suffix
    hass.states.async_set(ent, "3.0", {"watts": {"2026-06-27T11:00:00": 750}})

    result = coordinator.read_pv_today_watts(hass, d)

    assert result is not None, "Expected samples, got None"
    assert len(result) == 1, f"Expected 1 per-source array, got {len(result)}"
    pts = {dt_val: w for dt_val, w in result[0]}
    expected_utc = datetime(2026, 6, 27, 11, 0, tzinfo=UTC)
    assert expected_utc in pts, (
        f"Naive key '2026-06-27T11:00:00' must map to 11:00Z (UTC), "
        f"not to local-timezone-converted UTC. Got keys: {sorted(pts.keys())}"
    )
    assert abs(pts[expected_utc] - 750.0) < 1.0
