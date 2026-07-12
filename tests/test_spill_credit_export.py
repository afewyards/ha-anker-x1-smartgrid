import pytest

from custom_components.anker_x1_smartgrid.models import Config
from custom_components.anker_x1_smartgrid.optimize import optimize_grid, effective_export_price


def _cfg(**kw):
    d = dict(
        capacity_kwh=10.0,
        soc_floor=5.0,
        soc_target=100.0,
        max_charge_w=6000.0,
        eta_charge=0.92,
        round_trip_eff=0.85,
        cycle_cost_eur_per_kwh=0.04,
        export_fee_eur_per_kwh=0.02,
        max_export_w=6000.0,
        grid_export_limit_w=6000.0,
        export_peak_band_frac=0.1,
        export_peak_lookback_h=4,
        enable_export=True,
    )
    d.update(kw)
    return Config(**d)


def _two_day_solar_overflow():
    """48h: a lower day-0 evening peak, a higher day-1 evening peak, and a big
    day-1 midday solar surplus that overflows the pack (so holding day-0 energy
    is redundant).

    The day-1 midday feed-in price (the spill-credit rate) is set to a REALISTIC
    salderen value (~0.23 €/kWh net of fee), NOT the unrealistically-cheap 0.08
    used in the first draft of this test.  At 0.08 the state-dependent spill
    credit is too small to make the OLD code hold day-0 (it already sells), so
    the decision-neutrality test could not go red.  The hold only triggers when
    the overflow feed-in price exceeds ~ (ep_peak0·eta_d − cycle)·eta_charge ≈
    0.22; 0.23 clears it, so old code holds and the test is genuinely red.
    """
    n = 48
    pv = [0.0] * n
    for hh in range(33, 40):  # day-1 midday solar, overflows the pack
        pv[hh] = 4.0
    # load=0.1 (NOT 0.3): keep SoC above the floor until day-1 solar so the
    # export-vs-hold SoC differ at overflow time and the credit asymmetry is
    # detectable.  load=0.3 drains the pack to floor before hour 33 -> asymmetry
    # 0 -> old code passes trivially.
    load = [0.1] * n
    # price 0.25 (NOT 0.20): above the day-0 export round-trip threshold
    # (ep_peak0·eta_d − cycle ≈ 0.24 < 0.25/eta_charge ≈ 0.27), so the DP cannot
    # grid-ARBITRAGE energy into the day-0 peak.  At 0.20 cheap grid charging
    # funds both peaks and masks the hold/sell decision entirely.  At 0.25 the
    # day-0 export can only come from STORED energy, isolating the spill-credit
    # bug: old code HOLDS day-0 (~0.09), feed_in=None SELLS (~5.5).
    price = [0.25] * n
    raw_export = [0.10] * n
    for hh in range(33, 40):  # realistic salderen midday tariff (spill rate)
        raw_export[hh] = 0.25
    raw_export[18] = 0.32  # day-0 evening peak (in-band on its own day)
    raw_export[42] = 0.55  # day-1 evening peak (higher)
    return n, pv, load, price, raw_export


def test_spill_credit_is_decision_neutral():
    """feed_in must not change the chosen schedule (constant per-hour credit)."""
    cfg = _cfg()
    n, pv, load, price, raw_export = _two_day_solar_overflow()
    ep = [effective_export_price(p, cfg) for p in raw_export]
    common = dict(
        soc_start=98.0,
        cfg=cfg,
        window_start_h=0,
        window_len=n,
        export_price=ep,
        terminal_mode="water_value",
        water_value=0.0,
    )
    with_fi = optimize_grid(pv, load, price, feed_in=ep, **common)
    no_fi = optimize_grid(pv, load, price, feed_in=None, **common)
    assert with_fi["export_schedule"] == pytest.approx(no_fi["export_schedule"], abs=1e-6)
    assert with_fi["schedule"] == pytest.approx(no_fi["schedule"], abs=1e-6)
    # sanity: the solar-overflow surplus is sold on day 0 (the live bug, fixed)
    assert with_fi["export_schedule"][18] > 0.5


def test_sunny_next_day_sells_first_day_surplus():
    """Next-day solar refills the pack -> sell the first-day surplus now."""
    cfg = _cfg()
    n, pv, load, price, raw_export = _two_day_solar_overflow()
    ep = [effective_export_price(p, cfg) for p in raw_export]
    res = optimize_grid(
        pv,
        load,
        price,
        soc_start=90.0,
        cfg=cfg,
        window_start_h=0,
        window_len=n,
        feed_in=ep,
        export_price=ep,
        terminal_mode="water_value",
        water_value=0.0,
    )
    assert res["export_schedule"][18] > 0.5  # day-0 surplus sold
    assert res["export_schedule"][42] > 0.0  # day-1 peak also sells


def test_low_solar_next_day_holds_for_higher_peak():
    """No next-day solar + low load -> HOLD day-0, sell the higher day-1 peak."""
    cfg = _cfg()
    n = 48
    pv = [0.0] * n  # no solar at all
    load = [0.1] * n  # low load -> pack survives to day-1
    # price 0.25 (same threshold reasoning as the overflow scenario): keeps grid
    # arbitrage out of the day-0 peak so the day-0 hold is a genuine
    # store-for-the-higher-peak decision, not cheap buy-low/sell-high.
    price = [0.25] * n
    raw_export = [0.10] * n
    raw_export[18] = 0.32  # day-0 peak (lower)
    raw_export[42] = 0.55  # day-1 peak (higher, reachable)
    ep = [effective_export_price(p, cfg) for p in raw_export]
    res = optimize_grid(
        pv,
        load,
        price,
        soc_start=90.0,
        cfg=cfg,
        window_start_h=0,
        window_len=n,
        feed_in=ep,
        export_price=ep,
        terminal_mode="water_value",
        water_value=0.0,
    )
    assert res["export_schedule"][18] == pytest.approx(0.0, abs=1e-6)  # day-0 held
    assert res["export_schedule"][42] > 0.5  # sold at higher day-1 peak
