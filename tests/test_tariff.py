"""Pure static-tariff synthesis (tariff.py)."""

import pytest

from custom_components.anker_x1_smartgrid import tariff


def test_parse_offpeak_ranges_empty():
    assert tariff.parse_offpeak_ranges("") == []
    assert tariff.parse_offpeak_ranges(None) == []
    assert tariff.parse_offpeak_ranges("  ") == []


def test_parse_offpeak_ranges_single():
    assert tariff.parse_offpeak_ranges("01:30-07:30") == [(90, 450)]


def test_parse_offpeak_ranges_multi_and_midnight():
    assert tariff.parse_offpeak_ranges("22:00-06:00, 12:30-14:30") == [(1320, 360), (750, 870)]


@pytest.mark.parametrize(
    "bad",
    [
        "7-8",
        "25:00-01:00",
        "01:60-02:00",
        "0100-0200",
        "01:00_02:00",
        "01:00-",
        "01:00-02:00-03:00",
        "aa:bb-cc:dd",
    ],
)
def test_parse_offpeak_ranges_invalid_raises(bad):
    with pytest.raises(ValueError):
        tariff.parse_offpeak_ranges(bad)


def test_in_offpeak_normal_half_open():
    r = [(90, 450)]  # 01:30-07:30
    assert tariff._in_offpeak(90, r) is True
    assert tariff._in_offpeak(449, r) is True
    assert tariff._in_offpeak(450, r) is False  # end exclusive
    assert tariff._in_offpeak(89, r) is False


def test_in_offpeak_midnight_span():
    r = [(1320, 360)]  # 22:00-06:00
    assert tariff._in_offpeak(1350, r) is True  # 22:30
    assert tariff._in_offpeak(0, r) is True  # 00:00
    assert tariff._in_offpeak(359, r) is True  # 05:59
    assert tariff._in_offpeak(360, r) is False  # 06:00
    assert tariff._in_offpeak(700, r) is False


def test_resolution_minutes_flat_is_60():
    assert tariff._resolution_minutes([]) == 60


def test_resolution_minutes_on_hour_is_60():
    assert tariff._resolution_minutes([(60, 420)]) == 60  # 01:00-07:00


def test_resolution_minutes_half_hour_is_30():
    assert tariff._resolution_minutes([(90, 450)]) == 30  # 01:30-07:30


def test_resolution_minutes_quarter_is_15():
    assert tariff._resolution_minutes([(75, 435)]) == 15  # 01:15-07:15


def test_resolution_minutes_floored_at_15():
    assert tariff._resolution_minutes([(65, 125)]) == 15  # :05 → gcd 5 → floor 15


def test_resolution_minutes_offhour_offset_not_on_allowed_grid_snaps_to_15():
    # 01:20-06:20 → boundary offset :20 is not a multiple of 30 or 60, and the
    # planner's resolution grid only has {15,30,60} (no 20) — must snap to 15,
    # not the raw gcd(40,20)=20 (an unrepresentable slot width).
    assert tariff._resolution_minutes([(80, 380)]) == 15


from datetime import datetime, timedelta, timezone, UTC
from zoneinfo import ZoneInfo

from custom_components.anker_x1_smartgrid.models import Config

_UTC = UTC


def _cfg(**kw):
    return Config.from_dict({"price_mode": "static", **kw})


def test_synth_flat_all_import_hourly():
    now = datetime(2026, 7, 10, 14, 30, tzinfo=_UTC)
    slots = tariff.synth_static_price_slots(now, _cfg(static_price_import=0.25), _UTC)
    assert slots[0].start == datetime(2026, 7, 10, 14, 0, tzinfo=_UTC)  # top of hour
    assert slots[-1].start == datetime(2026, 7, 11, 23, 0, tzinfo=_UTC)  # 07-12 00:00 exclusive
    assert len(slots) == 34
    assert all(s.price == 0.25 for s in slots)
    assert all(s.duration_min == 60.0 for s in slots)


def test_synth_horizon_extent_late_evening():
    now = datetime(2026, 7, 10, 23, 30, tzinfo=_UTC)
    slots = tariff.synth_static_price_slots(now, _cfg(static_price_import=0.25), _UTC)
    assert slots[0].start == datetime(2026, 7, 10, 23, 0, tzinfo=_UTC)
    assert slots[-1].start == datetime(2026, 7, 11, 23, 0, tzinfo=_UTC)
    assert len(slots) == 25


def test_synth_hp_hc_hourly():
    now = datetime(2026, 7, 10, 14, 0, tzinfo=_UTC)
    cfg = _cfg(static_price_import=0.30, static_price_offpeak=0.10, static_offpeak_hours="01:00-06:00")
    by = {s.start: s.price for s in tariff.synth_static_price_slots(now, cfg, _UTC)}
    assert by[datetime(2026, 7, 11, 2, 0, tzinfo=_UTC)] == 0.10
    assert by[datetime(2026, 7, 11, 6, 0, tzinfo=_UTC)] == 0.30  # end exclusive
    assert by[datetime(2026, 7, 10, 14, 0, tzinfo=_UTC)] == 0.30


def test_synth_half_hour_resolution():
    now = datetime(2026, 7, 10, 14, 0, tzinfo=_UTC)
    cfg = _cfg(static_price_import=0.30, static_price_offpeak=0.10, static_offpeak_hours="01:30-06:30")
    slots = tariff.synth_static_price_slots(now, cfg, _UTC)
    assert all(s.duration_min == 30.0 for s in slots)
    by = {s.start: s.price for s in slots}
    assert by[datetime(2026, 7, 11, 1, 30, tzinfo=_UTC)] == 0.10
    assert by[datetime(2026, 7, 11, 1, 0, tzinfo=_UTC)] == 0.30
    assert by[datetime(2026, 7, 11, 6, 0, tzinfo=_UTC)] == 0.10
    assert by[datetime(2026, 7, 11, 6, 30, tzinfo=_UTC)] == 0.30


def test_synth_offhour_offset_uniform_15min_stride():
    # Off-peak boundaries at :20 (not representable on the {15,30,60} grid at
    # any coarser width) must synthesize on a uniform 15-min UTC stride, not
    # leave gaps from an unrepresentable 20-min step.
    now = datetime(2026, 7, 10, 14, 0, tzinfo=_UTC)
    cfg = _cfg(static_price_import=0.30, static_price_offpeak=0.10, static_offpeak_hours="01:20-06:20")
    slots = tariff.synth_static_price_slots(now, cfg, _UTC)
    assert all(s.duration_min == 15.0 for s in slots)
    starts = [s.start for s in slots]
    assert all((starts[i + 1] - starts[i]) == timedelta(minutes=15) for i in range(len(starts) - 1))
    by = {s.start: s.price for s in slots}
    assert by[datetime(2026, 7, 11, 1, 15, tzinfo=_UTC)] == 0.30  # before 01:20 -> still peak
    assert by[datetime(2026, 7, 11, 1, 30, tzinfo=_UTC)] == 0.10  # first 15-min slot inside range
    assert by[datetime(2026, 7, 11, 6, 15, tzinfo=_UTC)] == 0.10  # before 06:20 -> still offpeak
    assert by[datetime(2026, 7, 11, 6, 30, tzinfo=_UTC)] == 0.30  # after end -> peak


def test_synth_midnight_span_offpeak():
    now = datetime(2026, 7, 10, 14, 0, tzinfo=_UTC)
    cfg = _cfg(static_price_import=0.30, static_price_offpeak=0.10, static_offpeak_hours="22:00-06:00")
    by = {s.start: s.price for s in tariff.synth_static_price_slots(now, cfg, _UTC)}
    assert by[datetime(2026, 7, 10, 23, 0, tzinfo=_UTC)] == 0.10
    assert by[datetime(2026, 7, 11, 0, 0, tzinfo=_UTC)] == 0.10
    assert by[datetime(2026, 7, 11, 5, 0, tzinfo=_UTC)] == 0.10
    assert by[datetime(2026, 7, 11, 6, 0, tzinfo=_UTC)] == 0.30


def test_synth_multi_range():
    now = datetime(2026, 7, 10, 10, 0, tzinfo=_UTC)
    cfg = _cfg(static_price_import=0.30, static_price_offpeak=0.10, static_offpeak_hours="01:00-06:00,12:00-14:00")
    by = {s.start: s.price for s in tariff.synth_static_price_slots(now, cfg, _UTC)}
    assert by[datetime(2026, 7, 10, 12, 0, tzinfo=_UTC)] == 0.10
    assert by[datetime(2026, 7, 10, 13, 0, tzinfo=_UTC)] == 0.10
    assert by[datetime(2026, 7, 10, 14, 0, tzinfo=_UTC)] == 0.30
    assert by[datetime(2026, 7, 11, 2, 0, tzinfo=_UTC)] == 0.10


def test_synth_flat_only_when_offpeak_price_zero():
    now = datetime(2026, 7, 10, 14, 0, tzinfo=_UTC)
    cfg = _cfg(static_price_import=0.30, static_price_offpeak=0.0, static_offpeak_hours="01:00-06:00")
    slots = tariff.synth_static_price_slots(now, cfg, _UTC)
    assert all(s.price == 0.30 for s in slots)
    assert all(s.duration_min == 60.0 for s in slots)


def test_synth_invalid_ranges_fall_back_to_flat():
    now = datetime(2026, 7, 10, 14, 0, tzinfo=_UTC)
    cfg = _cfg(static_price_import=0.30, static_price_offpeak=0.10, static_offpeak_hours="25:00-99:00")
    slots = tariff.synth_static_price_slots(now, cfg, _UTC)
    assert all(s.price == 0.30 for s in slots)


def test_synth_dst_spring_forward_contiguous_and_skips_local_02():
    tz = ZoneInfo("Europe/Paris")  # spring-forward 2026-03-29 02:00→03:00
    now = datetime(2026, 3, 28, 12, 0, tzinfo=_UTC)
    cfg = _cfg(static_price_import=0.30, static_price_offpeak=0.10, static_offpeak_hours="01:00-06:00")
    slots = tariff.synth_static_price_slots(now, cfg, tz)
    starts = [s.start for s in slots]
    step = starts[1] - starts[0]
    assert all((starts[i + 1] - starts[i]) == step for i in range(len(starts) - 1))
    op_local_hours = {s.start.astimezone(tz).hour for s in slots if s.price == 0.10}
    assert 2 not in op_local_hours  # 02:00 local does not exist on spring-forward day
    assert {1, 3, 4, 5} <= op_local_hours


def test_synth_dst_fall_back_contiguous_local_02_twice():
    tz = ZoneInfo("Europe/Paris")  # fall-back 2026-10-25 03:00→02:00
    now = datetime(2026, 10, 24, 12, 0, tzinfo=_UTC)
    cfg = _cfg(static_price_import=0.30, static_price_offpeak=0.10, static_offpeak_hours="01:00-06:00")
    slots = tariff.synth_static_price_slots(now, cfg, tz)
    starts = [s.start for s in slots]
    step = starts[1] - starts[0]
    assert all((starts[i + 1] - starts[i]) == step for i in range(len(starts) - 1))
    op_local_2 = [s for s in slots if s.price == 0.10 and s.start.astimezone(tz).hour == 2]
    assert len(op_local_2) >= 2  # 02:00 local occurs twice on fall-back day
