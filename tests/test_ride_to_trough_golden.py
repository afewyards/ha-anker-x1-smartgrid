from datetime import datetime, timedelta, timezone
import pytest
from custom_components.anker_x1_smartgrid import controller as ctrl
from custom_components.anker_x1_smartgrid.models import Config, ForecastInterval, PriceSlot

# 2026-07-01: solar ends ~16:00; expensive 0.27-0.43 night; cheap 0.13-0.14 morning ~07:00 next day.
NOW = datetime(2026, 7, 1, 16, 0, tzinfo=timezone.utc)


def _cfg():
    return Config(capacity_kwh=10.0, soc_floor=5.0, eta_charge=0.92,
                  round_trip_eff=0.85, reserve_cheap_band=0.20, reserve_anchor="trough")


def _night_prices():
    # 16:00 -> 08:00 next-day (17 hourly slots): peak 18:00, cheap 06:00-07:00.
    return [0.28, 0.33, 0.43, 0.37, 0.34, 0.31, 0.30, 0.29, 0.28,
            0.27, 0.24, 0.20, 0.17, 0.14, 0.13, 0.13, 0.14]


def _fixture():
    prices = _night_prices()
    slots = [PriceSlot(NOW + timedelta(hours=i), p) for i, p in enumerate(prices)]
    # ~450W steady overnight load, then a real morning solar run: the pickup at
    # +16h CONTINUES for a few hours (mirroring a production 2-day PV curve), so the
    # reserve walk finds a genuine pickup at the horizon boundary instead of hitting
    # the synthetic night-extension backstop (which would pin the last hour to cap).
    ivs = ([ForecastInterval(NOW + timedelta(hours=i), 0.0, 450.0, 1.0) for i in range(16)]
           + [ForecastInterval(NOW + timedelta(hours=16 + j), 3000.0, 200.0, 1.0) for j in range(3)])
    return slots, ivs


def test_overnight_reserve_not_pinned_and_monotone():
    slots, ivs = _fixture()
    cfg = _cfg()
    ic = ctrl._build_is_cheap_by_hour(slots, cfg)
    rsv = ctrl._build_reserve_by_hour(NOW, slots, ivs, cfg, is_cheap=ic)
    hrs = sorted(rsv)
    vals = [rsv[h] for h in hrs]
    cap = cfg.capacity_kwh
    # NOT pinned near capacity (was ~90-100%); evening reserve well below the pack.
    assert rsv[NOW] == pytest.approx(6.831764705882355, abs=1e-6)
    assert rsv[NOW] < 0.7 * cap, f"reserve still balloons: {rsv[NOW]:.2f}/{cap}"
    assert rsv[NOW] > cfg.soc_floor / 100.0 * cap   # above the firmware floor
    # Monotone-non-increasing glide toward the cheap morning.
    assert all(a + 1e-9 >= b for a, b in zip(vals, vals[1:])), vals
    # Collapses toward the floor at the cheap 07:00-08:00 hours.
    assert rsv[NOW + timedelta(hours=15)] < rsv[NOW]


def test_legacy_anchor_still_pins_high():
    slots, ivs = _fixture()
    legacy = ctrl._build_reserve_by_hour(NOW, slots, ivs, _cfg().__class__(
        capacity_kwh=10.0, soc_floor=5.0, eta_charge=0.92, round_trip_eff=0.85,
        reserve_anchor="legacy"))
    assert legacy[NOW] > ctrl._build_reserve_by_hour(
        NOW, slots, ivs, _cfg(), is_cheap=ctrl._build_is_cheap_by_hour(slots, _cfg()))[NOW]
