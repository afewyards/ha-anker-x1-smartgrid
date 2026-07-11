"""Current-hour kWh accumulator + blend wrapper."""
from datetime import datetime, timedelta, timezone

from custom_components.anker_x1_smartgrid import intra_hour

H = datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc)


def _fill(acc, start, minutes, load_w, step_s=60):
    for i in range(int(minutes * 60 / step_s) + 1):
        acc.add(start + timedelta(seconds=i * step_s), load_w)


def test_accumulator_integrates_rectangle():
    acc = intra_hour.HourAccumulator()
    _fill(acc, H, 30, 1200.0)  # 30 min at 1200 W = 0.6 kWh
    assert abs(acc.kwh - 0.6) < 1e-6
    assert abs(acc.covered_s - 1800.0) < 1e-6
    assert acc.hour == H


def test_accumulator_resets_on_new_hour():
    acc = intra_hour.HourAccumulator()
    _fill(acc, H, 30, 1200.0)
    acc.add(H + timedelta(hours=1), 500.0)
    assert acc.hour == H + timedelta(hours=1)
    assert acc.kwh == 0.0 and acc.covered_s == 0.0


def test_accumulator_skips_gaps_and_none():
    acc = intra_hour.HourAccumulator()
    acc.add(H, 1200.0)
    acc.add(H + timedelta(minutes=10), 1200.0)  # 600 s gap > MAX_STEP_S → dropped
    assert acc.kwh == 0.0 and acc.covered_s == 0.0
    acc.add(H + timedelta(minutes=11), None)     # None breaks the chain
    acc.add(H + timedelta(minutes=12), 1200.0)   # no prior ts → no step
    assert acc.covered_s == 0.0
    acc.add(H + timedelta(minutes=13), 1200.0)   # 60 s step counts
    assert abs(acc.covered_s - 60.0) < 1e-6


class _Base:
    def predict(self, when, temp, fallback_w, *, quantile=0.5):
        return 1000.0


def test_blend_only_applies_to_current_hour_with_coverage():
    acc = intra_hour.HourAccumulator()
    p = intra_hour.CurrentHourBlendPredictor(_Base(), acc, H)
    # no coverage → passthrough
    assert p.predict(H, 20.0, 250.0) == 1000.0
    _fill(acc, H, 30, 2000.0)  # 1.0 kWh observed over 30 min
    # est = 1.0 kWh + 1000 W × 0.5 h → 1500 W slot average
    assert abs(p.predict(H, 20.0, 250.0) - 1500.0) < 1e-6
    # future hours untouched
    assert p.predict(H + timedelta(hours=1), 20.0, 250.0) == 1000.0


def test_blend_below_min_coverage_passthrough():
    acc = intra_hour.HourAccumulator()
    _fill(acc, H, 5, 2000.0)  # 300 s < MIN_COVERAGE_S
    p = intra_hour.CurrentHourBlendPredictor(_Base(), acc, H)
    assert p.predict(H, 20.0, 250.0) == 1000.0


def test_blend_stale_accumulator_hour_passthrough():
    acc = intra_hour.HourAccumulator()
    _fill(acc, H - timedelta(hours=1), 30, 2000.0)  # accumulator on previous hour
    p = intra_hour.CurrentHourBlendPredictor(_Base(), acc, H)
    assert p.predict(H, 20.0, 250.0) == 1000.0
