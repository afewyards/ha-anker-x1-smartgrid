from custom_components.anker_x1_smartgrid.models import Config
from custom_components.anker_x1_smartgrid.optimize import optimize_grid


def _cfg(lb):
    return Config(capacity_kwh=10.0, soc_floor=20.0, soc_target=80.0,
                  max_charge_w=3000.0, eta_charge=1.0, max_export_w=3000.0,
                  grid_export_limit_w=6000.0, enable_export=True,
                  export_peak_band_frac=0.10, export_peak_lookback_h=lb)


def test_export_lookback_is_wall_clock_invariant_60_vs_15():
    p60 = [0.20, 0.50, 0.30, 0.30, 0.30, 0.30]           # peak at index 1
    out60 = optimize_grid([0.0]*6, [0.0]*6, p60, soc_start=80.0, cfg=_cfg(4),
                          window_start_h=0, window_len=6, dt_h=1.0,
                          export_price=p60, feed_in=p60, slots_per_day=24, day_index=[0]*6)
    p15 = [v for v in p60 for _ in range(4)]              # same wall-clock shape at 15-min
    out15 = optimize_grid([0.0]*24, [0.0]*24, p15, soc_start=80.0, cfg=_cfg(4),
                          window_start_h=0, window_len=24, dt_h=0.25,
                          export_price=p15, feed_in=p15, slots_per_day=96, day_index=[0]*24)
    tail60 = out60["export_schedule"][5] > 0.0
    tail15 = any(v > 0.0 for v in out15["export_schedule"][20:24])
    assert tail60 == tail15
