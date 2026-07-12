import itertools
from datetime import datetime, timedelta, timezone, UTC
import pytest
from custom_components.anker_x1_smartgrid import controller as ctrl
from custom_components.anker_x1_smartgrid.models import Config, ForecastInterval, PriceSlot

NOW = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)


def _cfg(**kw):
    d = dict(
        capacity_kwh=10.0,
        soc_floor=10.0,
        eta_charge=1.0,
        round_trip_eff=1.0,
        reserve_cheap_band=0.20,
        reserve_anchor="trough",
    )
    d.update(kw)
    return Config(**d)


def _slots(prices):
    return [PriceSlot(NOW + timedelta(hours=i), p) for i, p in enumerate(prices)]


def test_is_cheap_overnight_false_morning_true():
    # peak 0.43 → 0.37 down-slope → 0.30 overnight → 0.20 → 0.13/0.14 morning trough.
    ic = ctrl._build_is_cheap_by_hour(_slots([0.30, 0.43, 0.37, 0.30, 0.30, 0.20, 0.13, 0.14]), _cfg())
    assert ic[NOW + timedelta(hours=2)] is False  # 0.37: own forward trough 0.13
    assert ic[NOW + timedelta(hours=3)] is False  # 0.30 overnight
    assert ic[NOW + timedelta(hours=5)] is False  # 0.20 not within 20% of 0.13
    assert ic[NOW + timedelta(hours=6)] is True  # 0.13 trough
    assert ic[NOW + timedelta(hours=7)] is True  # 0.14 within band of own 0.14


def test_is_cheap_eps_floors_band_on_near_zero_prices():
    # near-zero prices: band denominator floored at eps=0.02 so 0.05 is NOT "cheap"
    # relative to a 0.00 forward trough (0.05 > 0.00 + 0.20*max(0,0.02)=0.004).
    ic = ctrl._build_is_cheap_by_hour(_slots([0.05, 0.00, 0.00]), _cfg())
    assert ic[NOW] is False
    assert ic[NOW + timedelta(hours=1)] is True


def test_is_cheap_only_real_price_hours_get_entries():
    ic = ctrl._build_is_cheap_by_hour(_slots([0.20, 0.15, 0.13]), _cfg())
    assert set(ic) == {NOW, NOW + timedelta(hours=1), NOW + timedelta(hours=2)}
    assert (NOW + timedelta(hours=3)) not in ic  # no slot → no entry (tail = not cheap)


def test_reserve_monotone_non_increasing_across_overnight():
    # cheap morning at +6/+7; expensive evening/overnight before it.
    prices = [0.30, 0.43, 0.37, 0.30, 0.30, 0.20, 0.13, 0.14]
    slots = _slots(prices)
    # 500W steady deficit all night, then PV pickup at +8h.
    ivs = [ForecastInterval(NOW + timedelta(hours=i), 0.0, 500.0, 1.0) for i in range(8)] + [
        ForecastInterval(NOW + timedelta(hours=8), 3000.0, 200.0, 1.0)
    ]
    ic = ctrl._build_is_cheap_by_hour(slots, _cfg())
    rsv = ctrl._build_reserve_by_hour(NOW, slots, ivs, _cfg(), is_cheap=ic)
    hrs = sorted(rsv)
    vals = [rsv[h] for h in hrs]
    assert all(a + 1e-9 >= b for a, b in itertools.pairwise(vals)), f"not monotone: {vals}"
    assert rsv[NOW] < ctrl._build_reserve_by_hour(NOW, slots, ivs, _cfg())[NOW]  # trough < legacy


def test_horizon_tail_no_price_does_not_collapse():
    # prices only for the first 1h (rest of horizon is the no-price tail); ivs extend to a real PV pickup at +8h.
    slots = _slots([0.30])  # single priced hour; +1h..+7h are the no-price tail
    ivs = [ForecastInterval(NOW + timedelta(hours=i), 0.0, 500.0, 1.0) for i in range(8)] + [
        ForecastInterval(NOW + timedelta(hours=8), 3000.0, 200.0, 1.0)
    ]
    ic = ctrl._build_is_cheap_by_hour(slots, _cfg())
    trough = ctrl._build_reserve_by_hour(NOW, slots, ivs, _cfg(), is_cheap=ic)
    legacy = ctrl._build_reserve_by_hour(NOW, slots, ivs, _cfg())
    assert trough[NOW] == pytest.approx(legacy[NOW], abs=1e-9)  # no cheap hour → no early-break
