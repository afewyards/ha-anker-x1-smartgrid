"""Horizon hours PAST the last forecast interval get a real one-night ride-out
EXPORT floor, not the firmware floor (was: `break` -> floor default)."""
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.anker_x1_smartgrid import controller as ctrl
from custom_components.anker_x1_smartgrid.models import Config, ForecastInterval, PriceSlot

D1 = datetime(2026, 6, 27, 6, 0, tzinfo=timezone.utc)  # 06:00 UTC


def _cfg(**kw) -> Config:
    d = dict(capacity_kwh=10.0, soc_floor=20.0, eta_charge=1.0, round_trip_eff=1.0)
    d.update(kw)
    return Config(**d)


def _intervals() -> list[ForecastInterval]:
    """Morning PV surplus (so _has_solar=True) then zero-PV, ENDING at 18:00."""
    ivs: list[ForecastInterval] = []
    for i in range(5):       # 06:00..10:00 surplus
        ivs.append(ForecastInterval(D1 + timedelta(hours=i), 2000.0, 400.0, 1.0))
    for i in range(5, 12):   # 11:00..17:00 zero-PV (last starts 17:00 -> ends 18:00)
        ivs.append(ForecastInterval(D1 + timedelta(hours=i), 0.0, 400.0, 1.0))
    return ivs


def _slots() -> list[PriceSlot]:
    return [PriceSlot(D1 + timedelta(hours=i), 0.20) for i in range(17)]  # 06:00..22:00


def test_post_curve_hours_get_real_ride_out():
    cfg = _cfg()
    rsv = ctrl._build_reserve_by_hour(D1, _slots(), _intervals(), cfg)
    floor_kwh = cfg.soc_floor / 100.0 * cfg.capacity_kwh  # 2.0
    h20 = D1 + timedelta(hours=14)
    h21 = D1 + timedelta(hours=15)
    h22 = D1 + timedelta(hours=16)
    assert h20 in rsv and rsv[h20] > floor_kwh
    assert h22 in rsv and rsv[h22] > floor_kwh
    assert rsv[h20] == pytest.approx(6.8, abs=1e-6)   # floor 2.0 + 12h x 0.4 (eta_d=1.0)
    assert rsv[h20] - rsv[h21] == pytest.approx(0.4, abs=1e-6)
    assert rsv[h21] - rsv[h22] == pytest.approx(0.4, abs=1e-6)
