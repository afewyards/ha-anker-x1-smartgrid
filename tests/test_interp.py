from datetime import datetime, timedelta, UTC

import pytest

from custom_components.anker_x1_smartgrid.interp import MidpointLinear


def _t(h: int, m: int = 0, s: int = 0) -> datetime:
    return datetime(2026, 8, 2, h, m, s, tzinfo=UTC)


def test_query_at_period_center_is_identity():
    """The property the whole design rests on: querying a point's own period
    center returns that point's value EXACTLY (no float drift), which is what
    keeps slot_minutes=60 and cadence==bucket-width byte-identical."""
    points = [(_t(10), 1200.0), (_t(11), 800.0), (_t(12), 300.0)]
    r = MidpointLinear(points)
    assert r.at(_t(10, 30)) == 1200.0
    assert r.at(_t(11, 30)) == 800.0
    assert r.at(_t(12, 30)) == 300.0


def test_interpolates_between_anchors():
    r = MidpointLinear([(_t(10), 1200.0), (_t(11), 800.0)])
    # 10:45 is 25% of the way from anchor 10:30 to anchor 11:30
    assert r.at(_t(10, 45)) == pytest.approx(1100.0)
    assert r.at(_t(11, 0)) == pytest.approx(1000.0)


def test_flat_hold_before_first_and_after_last_anchor():
    """No extrapolation: a non-negative input can never produce a negative
    output, and no generation is invented before a source's first sample."""
    r = MidpointLinear([(_t(10), 1200.0), (_t(11), 800.0)])
    assert r.at(_t(9, 0)) == 1200.0
    assert r.at(_t(10, 15)) == 1200.0
    assert r.at(_t(12, 0)) == 800.0


def test_30min_cadence_anchors_at_quarter_past():
    """A 30-min series anchors at :15/:45, so 15-min bucket centers
    (:07.5/:22.5/:37.5/:52.5) straddle them."""
    r = MidpointLinear([(_t(11, 0), 386.0), (_t(11, 30), 2145.0)])
    assert r.at(_t(11, 15)) == 386.0  # anchor of the first point
    assert r.at(_t(11, 45)) == 2145.0  # anchor of the last point (mirrored width)
    assert r.at(_t(11, 22, 30)) == pytest.approx(825.75)
    assert r.at(_t(11, 37, 30)) == pytest.approx(1705.25)


def test_runs_split_across_gaps_larger_than_max_gap():
    """A 3h hole must not become a ramp: each side is its own run, flat-clamped."""
    r = MidpointLinear([(_t(11), 1200.0), (_t(14), 500.0)])
    assert r.at(_t(11, 7, 30)) == 1200.0
    assert r.at(_t(14, 7, 30)) == 500.0


def test_exact_1h_gap_does_not_split():
    """Hourly sources (Open-Meteo, France) must still interpolate — the split
    is at gap > max_gap_h, not >=."""
    r = MidpointLinear([(_t(11), 1200.0), (_t(12), 800.0)])
    assert r.at(_t(11, 45)) == pytest.approx(1100.0)


def test_single_point_is_flat_everywhere():
    r = MidpointLinear([(_t(11), 100.0)])
    assert r.at(_t(9)) == 100.0
    assert r.at(_t(11, 7, 30)) == 100.0
    assert r.at(_t(15)) == 100.0


def test_empty_points_returns_none():
    assert MidpointLinear([]).at(_t(11)) is None


def test_unsorted_input_is_sorted():
    r = MidpointLinear([(_t(11), 800.0), (_t(10), 1200.0)])
    assert r.at(_t(10, 30)) == 1200.0
    assert r.at(_t(11, 30)) == 800.0


def test_irregular_cadence_uses_per_interval_widths():
    """15-min then 30-min spacing: each point's anchor uses ITS OWN period."""
    r = MidpointLinear([(_t(11, 0), 100.0), (_t(11, 15), 200.0), (_t(11, 45), 400.0)])
    assert r.at(_t(11, 7, 30)) == 100.0  # anchor of point 0 (width 15 min)
    assert r.at(_t(11, 30)) == 200.0  # anchor of point 1 (width 30 min)
    assert r.at(_t(12, 0)) == 400.0  # anchor of point 2 (width mirrors 30 min)
