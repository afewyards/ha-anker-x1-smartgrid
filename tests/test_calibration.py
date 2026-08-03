"""Pure calibration-policy tests. No HA, no I/O, no clock."""

from datetime import datetime, timedelta, UTC

from custom_components.anker_x1_smartgrid import calibration


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
