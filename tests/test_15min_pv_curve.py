import pytest
from datetime import datetime, timedelta, timezone, UTC
from custom_components.anker_x1_smartgrid.parsers import build_pv_curve_from_watts
from custom_components.anker_x1_smartgrid.plan import build_display_intervals
from custom_components.anker_x1_smartgrid.models import PriceSlot

UTC = UTC


def test_watts_curve_buckets_at_15min_step():
    base = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
    samples = [(base + timedelta(minutes=5 * i), 1000.0 + 10 * i) for i in range(12)]
    curve = build_pv_curve_from_watts([samples], None, base, step_h=0.25)
    keys = [t for t, _ in curve]
    assert base + timedelta(minutes=15) in keys  # 4 quarter buckets
    assert base + timedelta(minutes=45) in keys


def test_watts_curve_60min_unchanged():
    base = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
    samples = [(base + timedelta(minutes=15 * i), 1000.0) for i in range(8)]
    curve = build_pv_curve_from_watts([samples], None, base)  # step_h default 1.0
    assert [t for t, _ in curve] == [base, base + timedelta(hours=1)]


class _P:
    def predict(self, *a, **k):
        return 300.0


def test_display_intervals_emit_quarter_dt_h_for_real_slots():
    base = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
    slots = [PriceSlot(base + timedelta(minutes=15 * i), 0.2) for i in range(4)]
    ivs = build_display_intervals(slots, base, [], _P(), 20.0, 300.0, slot_minutes=15)
    assert len(ivs) == 4
    assert all(abs(iv.dt_h - 0.25) < 1e-9 for iv in ivs)  # not 1.0


def test_display_intervals_predicts_once_per_hour_with_hour_floored_temp():
    """D1 + D3: the temp forecast is intrinsically hourly, and so is the load
    model -- so the predictor is called ONCE per hour with that hour's own
    temp, and the four quarters are then interpolated from those hourly values
    (D3).  Pre-D3 this called predict() four times per hour with identical
    arguments and identical results."""
    base = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
    slots = [PriceSlot(base + timedelta(minutes=15 * i), 0.2) for i in range(4)]
    seen = {}

    class _RecordingPredictor:
        def predict(self, when, temp, fallback_w, *, quantile=0.5):
            seen[when] = temp
            return 300.0

    temp_by_hour = {base: 7.0}  # only the hour key (10:00) is present
    ivs = build_display_intervals(
        slots,
        base,
        [],
        _RecordingPredictor(),
        20.0,
        300.0,
        temp_by_hour=temp_by_hour,
        slot_minutes=15,
    )
    assert len(ivs) == 4
    assert seen == {base: 7.0}  # one call, the hour's own temp -- not cur_temp
    assert [iv.load_w for iv in ivs] == [300.0] * 4  # constant model -> constant output


def test_display_intervals_interpolate_load_across_the_hour():
    """An hour-varying load model ramps across the quarters instead of
    stepping.  Anchors are the hour centres (10:30=400, 11:30=800), so the
    10:00 hour's quarters read 400/400/450/550 and the 11:00 hour's read
    650/750/800/800 (flat past the last anchor -- no probe beyond the
    horizon)."""
    base = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
    slots = [PriceSlot(base + timedelta(minutes=15 * i), 0.2) for i in range(8)]

    class _HourlyPredictor:
        def predict(self, when, temp, fallback_w, *, quantile=0.5):
            return 400.0 if when.hour == 10 else 800.0

    ivs = build_display_intervals(slots, base, [], _HourlyPredictor(), 20.0, 300.0, slot_minutes=15)
    assert [iv.load_w for iv in ivs] == pytest.approx(
        [400.0, 400.0, 450.0, 550.0, 650.0, 750.0, 800.0, 800.0]
    )


def test_display_intervals_load_identical_at_60min():
    """slot_minutes=60: the slot centre IS the hour anchor, so interpolation is
    an exact identity and hourly deployments are byte-unchanged."""
    base = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
    slots = [PriceSlot(base + timedelta(hours=i), 0.2) for i in range(3)]

    class _HourlyPredictor:
        def predict(self, when, temp, fallback_w, *, quantile=0.5):
            return 100.0 * when.hour

    ivs = build_display_intervals(slots, base, [], _HourlyPredictor(), 20.0, 300.0, slot_minutes=60)
    assert [iv.load_w for iv in ivs] == [1000.0, 1100.0, 1200.0]


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


def test_display_horizon_builds_watts_curve_on_the_slot_grid():
    """The display path dropped step_h, so it built the PV curve at 1.0h and
    fanned one value across all four quarters -- a coarser picture than the DP
    was optimizing on (live lab evidence 2026-08-02: 08:00/08:15/08:30/08:45
    all read pv_w 276.7995 from a 30-min source)."""
    from custom_components.anker_x1_smartgrid.models import Config
    from custom_components.anker_x1_smartgrid.plan import build_display_horizon

    base = datetime(2026, 8, 2, 11, 0, tzinfo=UTC)
    slots = [PriceSlot(base + timedelta(minutes=15 * i), 0.20) for i in range(4)]
    today_watts = [[(base, 386.0), (base + timedelta(minutes=30), 2145.0)]]
    sun_times = (
        base + timedelta(hours=8),   # today_sunset
        base + timedelta(hours=20),  # tomorrow_sunrise
        base + timedelta(hours=32),  # tomorrow_sunset
    )
    rows = build_display_horizon(
        slots,
        base,
        None,
        None,
        sun_times,
        _P(),
        20.0,
        300.0,
        50.0,
        [],
        base + timedelta(hours=1),
        Config(capacity_kwh=10.0, max_charge_w=3000.0, eta_charge=1.0),
        today_watts=today_watts,
        slot_minutes=15,
    )
    pv = [r["pv_w"] for r in rows]
    assert len(pv) == 4
    assert pv == pytest.approx([386.0, 825.75, 1705.25, 2145.0])
