"""Reserve/export regression gate for the measured-efficiency-curve flag-ON path.

Not a matched-pair parity test (T10 covers that) — this is a coarse sanity
harness: with a non-trivial (asymmetric, degraded) discharge curve wired in,
the ride-out reserve must not shrink versus the static-scalar baseline, and
the DP export leg must still fire on an obvious evening price spike. Prints
the deltas so a human/CI log can eyeball regression drift across changes to
the efficiency-curve plumbing.
"""
from datetime import datetime

from custom_components.anker_x1_smartgrid.models import Config, ForecastInterval
from custom_components.anker_x1_smartgrid.efficiency import EfficiencyCurve, BinStat
from custom_components.anker_x1_smartgrid.energy import ride_out_reserve_kwh
from custom_components.anker_x1_smartgrid.optimize import optimize_grid


def _curve(cfg, disch_low=0.80):
    """Static curve with the two lowest discharge bins degraded to disch_low."""
    base = EfficiencyCurve.static(cfg)
    d = list(base._discharge)
    d[0] = BinStat(d[0].lo_w, d[0].hi_w, "discharge", disch_low, disch_low, 99, 9.0, True, "")
    d[1] = BinStat(d[1].lo_w, d[1].hi_w, "discharge", disch_low, disch_low, 99, 9.0, True, "")
    return EfficiencyCurve(list(base._charge), d, base._fc, base._fd)


def test_reserve_and_export_regression_report(capsys):
    cfg = Config(eta_charge=0.92, round_trip_eff=0.85, capacity_kwh=10.0,
                 max_export_w=6000.0, grid_export_limit_w=6000.0)
    curve = _curve(cfg)

    now = datetime(2026, 7, 1, 22, 0, 0)
    ivs = [ForecastInterval(datetime(2026, 7, 1, 22, 0, 0), 0.0, 600.0, 6.0)]
    r_off = ride_out_reserve_kwh(now, ivs, cfg)
    r_on = ride_out_reserve_kwh(now, ivs, cfg, eta_curve=curve)
    assert r_on >= r_off - 1e-9

    pv = [0.0] * 24
    load = [0.3] * 24
    price = [0.20] * 24
    ep = [0.0] * 24
    ep[18] = 0.55
    on = optimize_grid(pv, load, price, soc_start=80.0, cfg=cfg, window_start_h=0,
                        window_len=24, export_price=ep, eta_curve=curve)
    assert on["export_kwh"] > 0.0

    with capsys.disabled():
        print(f"[regression-gate] reserve off={r_off:.3f} on={r_on:.3f} "
              f"delta={r_on - r_off:+.3f} kWh | peak export_kwh on={on['export_kwh']:.3f}")
