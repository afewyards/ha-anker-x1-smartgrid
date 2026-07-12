"""Q1+Q3: day-2-evening ride-out reserve must be a full night-2 ride-out, not the
firmware floor — so the DP no longer drains the day-2 peak and rebuys cheap.

The horizon spans today+tomorrow PV with NO day+2 PV: for any hour past the last
forecast sunset, scheduler.find_next_solar_pickup returns None, and the pre-fix
reserve collapsed to max(floor, thin-evening-tail) == the firmware floor.
"""

from datetime import datetime, timedelta, timezone, UTC

import pytest

from custom_components.anker_x1_smartgrid import controller as ctrl
from custom_components.anker_x1_smartgrid.models import Config, ForecastInterval, PriceSlot
from custom_components.anker_x1_smartgrid.optimize import optimize_grid

D1 = datetime(2026, 6, 26, 18, 0, tzinfo=UTC)  # day-1 evening 18:00 UTC


def _cfg(**kw) -> Config:
    d = dict(capacity_kwh=10.0, soc_floor=20.0, eta_charge=1.0, round_trip_eff=1.0)
    d.update(kw)
    return Config(**d)


def _two_day_intervals() -> list[ForecastInterval]:
    """today+tomorrow only; every interval load=400 W; daytime pv=2000 (surplus),
    evening/overnight pv=0.  Last forecast interval is 19:00 day-2 (no day+2 PV)."""
    ivs: list[ForecastInterval] = []
    # Day-1 evening + overnight: 18:00 d1 .. 04:00 d2 (11 h, pv=0)
    for i in range(11):
        ivs.append(ForecastInterval(D1 + timedelta(hours=i), 0.0, 400.0, 1.0))
    # Day-2 sunrise pickup + daytime: 05:00 d2 .. 16:00 d2 (12 h, pv=2000 surplus)
    for i in range(11, 23):
        ivs.append(ForecastInterval(D1 + timedelta(hours=i), 2000.0, 400.0, 1.0))
    # Day-2 evening: 17:00, 18:00, 19:00 d2 (pv=0) — NOTHING after (last forecast)
    for i in range(23, 26):
        ivs.append(ForecastInterval(D1 + timedelta(hours=i), 0.0, 400.0, 1.0))
    return ivs


def _slots() -> list[PriceSlot]:
    # Hourly slots 18:00 d1 .. 19:00 d2 so horizon_hours includes the day-2 evening.
    return [PriceSlot(D1 + timedelta(hours=i), 0.20) for i in range(26)]


def test_day2_evening_reserve_is_full_night_not_floor():
    cfg = _cfg()
    rsv = ctrl._build_reserve_by_hour(D1, _slots(), _two_day_intervals(), cfg)
    floor_kwh = cfg.soc_floor / 100.0 * cfg.capacity_kwh  # 2.0 kWh

    h17 = D1 + timedelta(hours=23)  # 17:00 d2
    h18 = D1 + timedelta(hours=24)  # 18:00 d2 — pre-fix collapse hour
    h19 = D1 + timedelta(hours=25)  # 19:00 d2

    # POST-FIX: 18:00 d2 ride-out = floor 2.0 + real tail 18:00,19:00 (0.8) + synthetic
    # 20:00 d2 .. 07:00 d3 (12 h x 0.4 = 4.8) = 7.6 kWh.  Pre-fix this was 2.0 (floor).
    # (eta_d=1.0 so draw/h = load_w/1000 = 0.4 kWh; floor is now ADDED not max'd)
    assert rsv[h18] > floor_kwh
    assert rsv[h18] == pytest.approx(7.6, abs=1e-6)
    # Tapers by exactly one hour of P80 load (0.4 kWh) toward the synthetic sunrise.
    assert rsv[h17] - rsv[h18] == pytest.approx(0.4, abs=1e-6)
    assert rsv[h18] - rsv[h19] == pytest.approx(0.4, abs=1e-6)
    # Night-1 path: real pickup at 05:00 d2 -> floor 2.0 + 11 h x 0.4 = 6.4 kWh.
    assert rsv[D1] == pytest.approx(6.4, abs=1e-6)


def test_acceptance_a_day2_export_is_reserve_bounded():
    """(a) Feeding the FIXED reserve to optimize_grid bounds the day-2 evening export;
    the firmware-floor-only plan (reserve=None) drains strictly more."""
    cfg = _cfg(
        soc_target=97.0,
        max_charge_w=0.0,
        round_trip_eff=1.0,
        cycle_cost_eur_per_kwh=0.04,
        export_fee_eur_per_kwh=0.0,
        max_export_w=3000.0,
        grid_export_limit_w=3000.0,
    )
    rsv = ctrl._build_reserve_by_hour(D1, _slots(), _two_day_intervals(), cfg)
    n = 3  # window 17:00 d2 .. 19:00 d2
    base = D1 + timedelta(hours=23)
    reserve_list = [rsv[base + timedelta(hours=i)] for i in range(n)]
    pv = [0.0] * n
    load = [0.0] * n
    price = [0.20] * n
    export_price = [0.60] * n  # rich export every hour
    common = dict(
        soc_start=80.0,
        cfg=cfg,
        window_start_h=0,
        window_len=n,
        export_price=export_price,
        terminal_mode="water_value",
        water_value=0.0,
    )
    res_floor = optimize_grid(pv, load, price, reserve_by_hour=None, **common)
    res_rsv = optimize_grid(pv, load, price, reserve_by_hour=reserve_list, **common)
    # Reserve-bounded: strictly less export than the firmware-floor-only drain.
    assert sum(res_rsv["export_schedule"]) < sum(res_floor["export_schedule"])
    # Never drains below the window-min reserve: exported DC <= start(8 kWh) - min reserve.
    assert sum(res_rsv["export_schedule"]) <= 8.0 - min(reserve_list) + 1e-6


def test_acceptance_b_solar_first_no_grid_charge_for_export():
    """(b) Solar fills the pack to soc_target by the evening export peak; with the
    raised reserve the DP funds the export from the solar-filled pack and grid_charge==0."""
    cfg = _cfg(
        soc_target=97.0,
        max_charge_w=3000.0,
        round_trip_eff=1.0,
        cycle_cost_eur_per_kwh=0.04,
        export_fee_eur_per_kwh=0.0,
        max_export_w=3000.0,
        grid_export_limit_w=3000.0,
    )
    n = 4  # hour0 cheap import, hours1-2 solar block, hour3 export peak
    pv = [0.0, 6000.0, 6000.0, 0.0]
    load = [0.0, 0.0, 0.0, 0.0]
    price = [0.13, 0.30, 0.30, 0.30]
    export_price = [0.0, 0.0, 0.0, 0.60]
    reserve_list = [2.0, 2.0, 2.0, 5.0]  # raised night-2 ride-out at the export hour
    common = dict(
        soc_start=60.0,
        cfg=cfg,
        window_start_h=0,
        window_len=n,
        export_price=export_price,
        terminal_mode="water_value",
        water_value=0.0,
    )
    res = optimize_grid(pv, load, price, reserve_by_hour=reserve_list, **common)
    assert sum(res["schedule"]) == pytest.approx(0.0, abs=1e-9)  # no grid buy to fund export
    assert sum(res["export_schedule"]) > 0.0  # still exports solar surplus


def test_acceptance_c_needed_grid_charge_concentrates_at_cheapest_hour():
    """(c) When a grid charge IS required (no PV, end-reserve target), it concentrates
    at the single cheapest tariff hour — not scattered across expensive hours."""
    cfg = _cfg(soc_target=80.0, max_charge_w=10000.0)
    n = 4
    pv = [0.0] * n
    load = [0.0] * n
    price = [0.40, 0.13, 0.40, 0.40]  # hour 1 clearly cheapest
    res = optimize_grid(
        pv,
        load,
        price,
        soc_start=30.0,
        cfg=cfg,
        window_start_h=0,
        window_len=n,
        terminal_mode="reserve",
    )
    g = res["schedule"]
    assert g[1] == pytest.approx(5.0, abs=0.3)  # ~5 kWh buy at the cheapest hour
    assert g[0] == pytest.approx(0.0, abs=1e-6)
    assert g[2] == pytest.approx(0.0, abs=1e-6)
    assert g[3] == pytest.approx(0.0, abs=1e-6)
