"""A5: unit tests for the synthetic-night interval builder helpers.

Covers ``controller._next_synthetic_pickup`` and ``controller._synthetic_night_rows``
— the pure helpers extracted from the 3 near-identical synthetic-night blocks in
``_build_reserve_by_hour`` and ``compute_decision`` (all anchored on
``const.FALLBACK_SOLAR_PICKUP_HOUR_UTC``).
"""
from datetime import datetime, timedelta, timezone

from custom_components.anker_x1_smartgrid import const
from custom_components.anker_x1_smartgrid import controller as ctrl

UTC = timezone.utc
PICKUP_H = const.FALLBACK_SOLAR_PICKUP_HOUR_UTC


# ---------------------------------------------------------------------------
# _next_synthetic_pickup
# ---------------------------------------------------------------------------


def test_next_synthetic_pickup_rolls_to_next_day_when_hour_already_passed():
    after = datetime(2026, 7, 12, 22, 0, tzinfo=UTC)  # well past 08:00 UTC pickup
    pickup = ctrl._next_synthetic_pickup(after)
    assert pickup == datetime(2026, 7, 13, PICKUP_H, 0, tzinfo=UTC)


def test_next_synthetic_pickup_same_day_when_hour_still_ahead():
    after = datetime(2026, 7, 12, 2, 0, tzinfo=UTC)  # before 08:00 UTC pickup
    pickup = ctrl._next_synthetic_pickup(after)
    assert pickup == datetime(2026, 7, 12, PICKUP_H, 0, tzinfo=UTC)


def test_next_synthetic_pickup_rolls_when_after_equals_pickup_hour():
    # Pickup must be STRICTLY after `after` — equality rolls to the next day.
    after = datetime(2026, 7, 12, PICKUP_H, 0, tzinfo=UTC)
    pickup = ctrl._next_synthetic_pickup(after)
    assert pickup == datetime(2026, 7, 13, PICKUP_H, 0, tzinfo=UTC)


def test_next_synthetic_pickup_zeroes_sub_hour_fields():
    after = datetime(2026, 7, 12, 2, 15, 30, 123, tzinfo=UTC)
    pickup = ctrl._next_synthetic_pickup(after)
    assert (pickup.minute, pickup.second, pickup.microsecond) == (0, 0, 0)


# ---------------------------------------------------------------------------
# _synthetic_night_rows
# ---------------------------------------------------------------------------


def test_synthetic_night_rows_are_zero_pv_and_one_hour_dt():
    start = datetime(2026, 7, 12, 22, 0, tzinfo=UTC)
    end = start + timedelta(hours=3)
    rows = ctrl._synthetic_night_rows(start, end, None, 400.0)
    assert len(rows) == 3
    assert all(r.pv_w == 0.0 for r in rows)
    assert all(r.dt_h == 1.0 for r in rows)


def test_synthetic_night_rows_span_start_to_end_hourly():
    start = datetime(2026, 7, 12, 22, 0, tzinfo=UTC)
    end = start + timedelta(hours=3)
    rows = ctrl._synthetic_night_rows(start, end, None, 400.0)
    assert [r.start for r in rows] == [
        start,
        start + timedelta(hours=1),
        start + timedelta(hours=2),
    ]


def test_synthetic_night_rows_use_load_from_hod_dict():
    start = datetime(2026, 7, 12, 22, 0, tzinfo=UTC)
    end = start + timedelta(hours=2)
    load_by_hod = {22: 111.0, 23: 222.0}
    rows = ctrl._synthetic_night_rows(start, end, load_by_hod, 400.0)
    assert [r.load_w for r in rows] == [111.0, 222.0]


def test_synthetic_night_rows_fall_back_to_default_when_hod_missing_hour():
    start = datetime(2026, 7, 12, 22, 0, tzinfo=UTC)
    end = start + timedelta(hours=1)
    load_by_hod = {5: 999.0}  # does not cover hour 22
    rows = ctrl._synthetic_night_rows(start, end, load_by_hod, 400.0)
    assert rows[0].load_w == 400.0


def test_synthetic_night_rows_use_flat_fallback_when_no_hod_dict():
    start = datetime(2026, 7, 12, 22, 0, tzinfo=UTC)
    end = start + timedelta(hours=2)
    rows = ctrl._synthetic_night_rows(start, end, None, 400.0)
    assert [r.load_w for r in rows] == [400.0, 400.0]


def test_synthetic_night_rows_empty_when_start_not_before_end():
    start = datetime(2026, 7, 12, 22, 0, tzinfo=UTC)
    assert ctrl._synthetic_night_rows(start, start, None, 400.0) == []
    assert ctrl._synthetic_night_rows(start, start - timedelta(hours=1), None, 400.0) == []
