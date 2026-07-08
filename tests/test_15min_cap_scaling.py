"""T5: DP cap-scaling verification at dt_h=0.25 + solar_cycle_end_idx dawn boundary.

Proves the per-slot caps (charge/export) landed correctly at a REAL sub-hour
dt_h (T1 was a no-op-at-60 seam only), that the optimize/regret DP mirror
holds at dt_h=0.25, and that solar_cycle_end_idx's dawn-boundary index scales
with slot_minutes instead of hardcoding /3600.
"""
from datetime import datetime, timedelta, timezone

from custom_components.anker_x1_smartgrid.models import Config
from custom_components.anker_x1_smartgrid.optimize import optimize_grid, solar_cycle_end_idx
from custom_components.anker_x1_smartgrid.regret import DayData, hindsight_optimal_grid

UTC = timezone.utc


def _cfg():
    return Config(capacity_kwh=10.0, soc_floor=20.0, soc_target=80.0,
                  max_charge_w=4000.0, eta_charge=1.0, max_export_w=4000.0,
                  grid_export_limit_w=6000.0, enable_export=True, charge_window_price_band=1.0)


def test_charge_cap_is_quarter_at_dt_025():
    n = 8; pv = [0.0]*n; load = [0.0]*n
    price = [0.05] + [0.40]*(n-1)
    out = optimize_grid(pv, load, price, soc_start=20.0, cfg=_cfg(),
                        window_start_h=0, window_len=n, dt_h=0.25)
    assert out["schedule"][0] <= 1.0 + 1e-9   # 4kW*0.25h = 1.0 kWh cap
    assert out["schedule"][0] > 0.9


def test_export_cap_is_quarter_at_dt_025():
    n = 8; pv = [0.0]*n; load = [0.0]*n; price = [0.40]*n
    out = optimize_grid(pv, load, price, soc_start=80.0, cfg=_cfg(),
                        window_start_h=0, window_len=n, dt_h=0.25,
                        export_price=price, feed_in=price)
    assert max(out["export_schedule"]) <= 1.0 + 1e-9


def test_optimize_hindsight_mirror_holds_at_dt_025():
    n = 8; pv = [0.0]*n; load = [0.3]*n
    price = [0.10,0.40,0.10,0.40,0.10,0.40,0.10,0.40]
    day = DayData(pv_kwh=tuple(pv), load_kwh=tuple(load), price=tuple(price), soc_start=50.0)
    hind = hindsight_optimal_grid(day, _cfg(), export_price=tuple(price), dt_h=0.25)
    opt = optimize_grid(pv, load, price, soc_start=50.0, cfg=_cfg(),
                        window_start_h=0, window_len=n, dt_h=0.25, export_price=price)
    assert opt["schedule"] == hind["schedule"]
    assert opt["export_schedule"] == hind["export_schedule"]


def test_solar_cycle_boundary_at_correct_dawn_slot_15min():
    # sunrise 4h ahead; at 15-min that is slot index 16, not 4.
    now = datetime(2026, 8, 1, 2, 0, tzinfo=UTC)
    sun = (now + timedelta(hours=10), now + timedelta(hours=4), now + timedelta(hours=20))
    idx60 = solar_cycle_end_idx(now, 24, sun, slot_minutes=60)
    idx15 = solar_cycle_end_idx(now, 96, sun, slot_minutes=15)
    assert idx60[0] == 4          # legacy: 4 hourly slots
    assert idx15[0] == 16         # 4h == 16 quarter-hour slots
