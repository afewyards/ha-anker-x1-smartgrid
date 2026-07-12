"""C3: the curtailed-solar credit must not subsidize grid purchases (solar-first accounting)."""
import pytest

from custom_components.anker_x1_smartgrid.models import Config
from custom_components.anker_x1_smartgrid.optimize import optimize_grid, effective_export_price


def _cfg(**kw):
    d = dict(
        capacity_kwh=10.0, soc_floor=20.0, soc_target=80.0, max_charge_w=3000.0,
        eta_charge=1.0, round_trip_eff=1.0, cycle_cost_eur_per_kwh=0.04,
        export_fee_eur_per_kwh=0.02, max_export_w=3000.0, grid_export_limit_w=3000.0,
        export_peak_band_frac=0.12,
    )
    d.update(kw)
    return Config(**d)


def test_spill_credit_does_not_induce_grid_charge():
    """Night charge hour (h0) + huge solar (h1) saturates the pack; export action is OFF so
    the ONLY motive to grid-charge is the curtailed-solar credit.  Because import price
    (0.30) < feed-in (0.38), the UN-capped credit makes the h0 grid kWh look profitable
    (extra spill credit 2*0.38=0.76 > extra cost 2*0.30=0.60) -> the DP charges 2 kWh.
    With the C3 baseline cap there is no extra credit -> schedule is 0."""
    cfg = _cfg()
    n = 24
    pv = [0.0, 5.0] + [0.0] * (n - 2)      # h1 solar far exceeds the 2 kWh headroom
    load = [0.0] * n
    price = [0.30] * n                      # import 0.30 < feed-in 0.38 -> credit can subsidize
    fi = [effective_export_price(0.40, cfg)] * n   # 0.38 feed-in (spill credit price)
    res = optimize_grid(
        pv, load, price, soc_start=60.0, cfg=cfg,  # 6 kWh; target 8 kWh; solar fills it at h1
        window_start_h=0, window_len=n,
        feed_in=fi, export_price=None,             # credit ON, export action OFF (isolates the credit)
    )
    assert sum(res["schedule"]) == pytest.approx(0.0, abs=1e-9)


def test_grid_charge_still_funds_peak_when_solar_absent():
    """Cloudy day, no solar: cheap pre-dawn grid charge funds the evening export peak."""
    cfg = _cfg()
    n = 24
    pv = [0.0] * n
    load = [0.1] * n
    price = [0.10] * 6 + [0.30] * 18        # cheap pre-dawn trough
    raw_export = [0.0] * n
    raw_export[18] = 0.60                    # evening peak
    ep = [effective_export_price(p, cfg) for p in raw_export]
    res = optimize_grid(
        pv, load, price, soc_start=30.0, cfg=cfg, window_start_h=0, window_len=n,
        feed_in=ep, export_price=ep, terminal_mode="water_value", water_value=0.0,
    )
    assert res["export_kwh"] > 0.0
    assert sum(res["schedule"]) > 0.0
