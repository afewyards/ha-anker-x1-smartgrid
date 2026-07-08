from datetime import datetime, timedelta, timezone
from custom_components.anker_x1_smartgrid.parsers import build_pv_curve_from_watts
from custom_components.anker_x1_smartgrid.plan import build_display_intervals
from custom_components.anker_x1_smartgrid.models import PriceSlot

UTC = timezone.utc


def test_watts_curve_buckets_at_15min_step():
    base = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
    samples = [(base + timedelta(minutes=5*i), 1000.0 + 10*i) for i in range(12)]
    curve = build_pv_curve_from_watts(samples, None, base, step_h=0.25)
    keys = [t for t, _ in curve]
    assert base + timedelta(minutes=15) in keys           # 4 quarter buckets
    assert base + timedelta(minutes=45) in keys


def test_watts_curve_60min_unchanged():
    base = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
    samples = [(base + timedelta(minutes=15*i), 1000.0) for i in range(8)]
    curve = build_pv_curve_from_watts(samples, None, base)  # step_h default 1.0
    assert [t for t, _ in curve] == [base, base + timedelta(hours=1)]


class _P:
    def predict(self, *a, **k): return 300.0


def test_display_intervals_emit_quarter_dt_h_for_real_slots():
    base = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
    slots = [PriceSlot(base + timedelta(minutes=15*i), 0.2) for i in range(4)]
    ivs = build_display_intervals(slots, base, [], _P(), 20.0, 300.0, slot_minutes=15)
    assert len(ivs) == 4
    assert all(abs(iv.dt_h - 0.25) < 1e-9 for iv in ivs)   # not 1.0


def test_display_intervals_temp_lookup_stays_hour_floored_at_15min():
    """D1: temp_by_hour lookup must stay HOUR-floored even at slot_minutes=15 (PV dedup
    runs per-slot but the temp forecast is intrinsically hourly). A per-slot-keyed lookup
    would only match the :00 quarter and mis-fall-back quarters :15/:30/:45 to cur_temp."""
    base = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
    slots = [PriceSlot(base + timedelta(minutes=15 * i), 0.2) for i in range(4)]
    seen = {}

    class _RecordingPredictor:
        def predict(self, when, temp, fallback_w, *, quantile=0.5):
            seen[when] = temp
            return 300.0

    temp_by_hour = {base: 7.0}  # only the hour key (10:00) is present
    ivs = build_display_intervals(
        slots, base, [], _RecordingPredictor(), 20.0, 300.0,
        temp_by_hour=temp_by_hour, slot_minutes=15,
    )
    assert len(ivs) == 4
    assert seen == {
        base: 7.0,
        base + timedelta(minutes=15): 7.0,
        base + timedelta(minutes=30): 7.0,
        base + timedelta(minutes=45): 7.0,
    }


def test_synthetic_overnight_fill_stays_hourly_stride():
    # The overnight ride-out reserve must integrate the FULL overnight load, not ~1/4.
    from custom_components.anker_x1_smartgrid import energy
    from custom_components.anker_x1_smartgrid.models import Config, ForecastInterval
    base = datetime(2026, 8, 1, 22, 0, tzinfo=UTC)
    cfg = Config(capacity_kwh=10.0, soc_floor=5.0, max_charge_w=3000.0, eta_charge=1.0)
    # 8 hourly synthetic rows, dt_h=1.0 (as built by the controller fills)
    ivs = [ForecastInterval(base + timedelta(hours=i), 0.0, 500.0, 1.0) for i in range(8)]
    r = energy.ride_out_reserve_kwh(base, ivs, cfg, slot_minutes=15)
    # 8h * 500W = 4 kWh AC of overnight load — reserve must reflect the full night,
    # not be ~1/4-sized from a mistaken dt_h=0.25 on hourly-stride rows.
    assert r > 3.0
