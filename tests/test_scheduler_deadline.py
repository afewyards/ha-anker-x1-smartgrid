from datetime import datetime, timezone, timedelta, UTC
from custom_components.anker_x1_smartgrid.models import Config, PriceSlot
from custom_components.anker_x1_smartgrid import scheduler


def _slots(prices, start_hour=0):
    base = datetime(2026, 6, 20, 0, 0, tzinfo=UTC)
    return [PriceSlot(base + timedelta(hours=start_hour + i), p) for i, p in enumerate(prices)]


def test_peak_detected_after_threshold():
    # flat 0.10 morning, ramp at 17:00+
    prices = [0.10] * 17 + [0.10, 0.35, 0.40, 0.30, 0.20, 0.15, 0.12]
    slots = _slots(prices)
    cfg = Config(peak_k=1.3, peak_after_hour=15)
    peak = scheduler.detect_evening_peak(slots[0].start, slots, cfg)
    assert peak == slots[18].start  # 18:00, first rising >= median*1.3 after 15:00


def test_no_peak_on_flat_day_returns_none():
    slots = _slots([0.12] * 24)
    cfg = Config()
    assert scheduler.detect_evening_peak(slots[0].start, slots, cfg) is None


def test_deadline_uses_peak_when_earlier_than_sunset():
    prices = [0.10] * 17 + [0.10, 0.35, 0.40, 0.30, 0.20, 0.15, 0.12]
    slots = _slots(prices)
    now = slots[0].start
    sunset = datetime(2026, 6, 20, 21, 30, tzinfo=UTC)
    cfg = Config(deadline_buffer_min=60, min_dwell_min=15)
    dl = scheduler.compute_deadline(now, sunset, slots, cfg)
    assert dl == slots[18].start  # peak 18:00 < sunset-60min (20:30)


def test_deadline_falls_back_to_sunset_minus_buffer_when_no_peak():
    slots = _slots([0.12] * 24)
    now = slots[0].start
    sunset = datetime(2026, 6, 20, 21, 30, tzinfo=UTC)
    cfg = Config(deadline_buffer_min=60)
    dl = scheduler.compute_deadline(now, sunset, slots, cfg)
    assert dl == sunset - timedelta(minutes=60)


def test_deadline_never_later_than_sunset_minus_buffer():
    # peak at 21:00 but sunset-buffer is 20:30 -> clamp to 20:30
    prices = [0.10] * 21 + [0.45, 0.20, 0.10]
    slots = _slots(prices)
    now = slots[0].start
    sunset = datetime(2026, 6, 20, 21, 30, tzinfo=UTC)
    cfg = Config(deadline_buffer_min=60)
    dl = scheduler.compute_deadline(now, sunset, slots, cfg)
    assert dl == sunset - timedelta(minutes=60)
