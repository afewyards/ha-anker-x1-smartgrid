from datetime import date, datetime, timedelta, timezone

from custom_components.anker_x1_smartgrid.models import PriceSlot
from custom_components.anker_x1_smartgrid import resolution as res

UTC = timezone.utc


def _slots(start, minutes, n, price=0.20):
    return [PriceSlot(start + timedelta(minutes=minutes * i), price) for i in range(n)]


def test_detect_60_from_hourly():
    assert res.detect_slot_minutes(_slots(datetime(2026, 8, 1, tzinfo=UTC), 60, 24)) == 60


def test_detect_15_from_quarter_hourly():
    assert res.detect_slot_minutes(_slots(datetime(2026, 8, 1, tzinfo=UTC), 15, 96)) == 15


def test_detect_30_from_half_hourly():
    assert res.detect_slot_minutes(_slots(datetime(2026, 8, 1, tzinfo=UTC), 30, 48)) == 30


def test_detect_ignores_single_forecast_gap():
    s = _slots(datetime(2026, 8, 1, tzinfo=UTC), 15, 96)
    del s[40]  # one 30-min gap; the MIN positive delta is still 15
    assert res.detect_slot_minutes(s) == 15


def test_detect_mixed_payload_picks_finest_via_min():
    # 6 hourly + 40 quarter-hourly. MIN picks 15 even though hourly deltas exist.
    base = datetime(2026, 8, 1, tzinfo=UTC)
    s = _slots(base, 60, 6) + _slots(base + timedelta(hours=6), 15, 40)
    assert res.detect_slot_minutes(s) == 15


def test_detect_survives_dst_25h_day():
    s = _slots(datetime(2026, 10, 25, tzinfo=UTC), 60, 25)
    assert res.detect_slot_minutes(s) == 60


def test_detect_ignores_sub_min_gap_duplicate():
    # A 5-min duplicate/DST artifact is below the min-gap floor and ignored;
    # the remaining deltas (10-min around it, then 15-min) snap to 15.
    base = datetime(2026, 8, 1, tzinfo=UTC)
    s = _slots(base, 15, 20)
    s.insert(1, PriceSlot(base + timedelta(minutes=5), 0.2))  # 5-min artifact < _MIN_GAP_MIN
    assert res.detect_slot_minutes(s) == 15


def test_detect_fallback_60_on_short_input():
    assert res.detect_slot_minutes([]) == 60
    assert res.detect_slot_minutes(_slots(datetime(2026, 8, 1, tzinfo=UTC), 15, 1)) == 60


def test_detect_snaps_noise_to_60():
    base = datetime(2026, 8, 1, tzinfo=UTC)
    s = [PriceSlot(base + timedelta(minutes=61 * i), 0.2) for i in range(10)]
    assert res.detect_slot_minutes(s) == 60


def test_detect_handles_unsorted():
    base = datetime(2026, 8, 1, tzinfo=UTC)
    s = _slots(base, 15, 8)
    s.reverse()
    assert res.detect_slot_minutes(s) == 15


def test_resolve_override_pins():
    s = _slots(datetime(2026, 8, 1, tzinfo=UTC), 15, 96)
    assert res.resolve_slot_minutes(s, "60") == 60
    assert res.resolve_slot_minutes(s, "auto") == 15


def test_floor_to_slot_60_is_hour_floor_in_non_utc_tz():
    tz = timezone(timedelta(hours=2))
    dt = datetime(2026, 8, 1, 10, 37, 42, tzinfo=tz)
    assert res.floor_to_slot(dt, 60) == dt.replace(minute=0, second=0, microsecond=0)


def test_floor_to_slot_15_buckets():
    dt = datetime(2026, 8, 1, 10, 37, tzinfo=UTC)
    assert res.floor_to_slot(dt, 15) == dt.replace(minute=30, second=0, microsecond=0)


def test_resample_forward_fills_coarse_into_fine():
    base = datetime(2026, 8, 1, tzinfo=UTC)
    s = [PriceSlot(base, 0.10), PriceSlot(base + timedelta(hours=1), 0.20)]
    m = res.resample_price_map(s, 15)
    assert m[base] == 0.10
    assert m[base + timedelta(minutes=45)] == 0.10          # forward-filled
    assert m[base + timedelta(minutes=60)] == 0.20


def test_resample_60_equals_legacy_hourly_dict():
    base = datetime(2026, 8, 1, tzinfo=UTC)
    s = [PriceSlot(base, 0.10), PriceSlot(base + timedelta(hours=1), 0.20)]
    assert res.resample_price_map(s, 60) == {base: 0.10, base + timedelta(hours=1): 0.20}


def test_latch_new_day_resets_to_detected():
    now = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
    eff, state = res.latch_finest(60, now, None)
    assert eff == 60 and state == (60, date(2026, 8, 1))


def test_latch_keeps_finest_within_utc_day():
    d = date(2026, 8, 1)
    now = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
    # first refresh sees 15 (fine head), later refresh sees only hourly tail (60)
    eff, state = res.latch_finest(15, now, (15, d))
    assert eff == 15
    eff2, state2 = res.latch_finest(60, now.replace(hour=20), state)
    assert eff2 == 15                       # no 15→60 flip within the day


def test_latch_resets_at_utc_day_rollover():
    eff, state = res.latch_finest(15, datetime(2026, 8, 1, 23, 0, tzinfo=UTC), (15, date(2026, 8, 1)))
    assert eff == 15
    eff2, _ = res.latch_finest(60, datetime(2026, 8, 2, 0, 30, tzinfo=UTC), state)
    assert eff2 == 60                        # new UTC day → back to detected
