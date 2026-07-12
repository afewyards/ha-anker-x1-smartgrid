"""C2b: peak-only export gate — morning sub-peak hours export 0."""

import pytest

from custom_components.anker_x1_smartgrid.models import Config
from custom_components.anker_x1_smartgrid.optimize import (
    build_charge_mask,
    effective_export_price,
    optimize_grid,
)
from custom_components.anker_x1_smartgrid.regret import windowed_peak_prices, windowed_trough_prices


def _cfg(**kw):
    d = dict(
        capacity_kwh=10.0,
        soc_floor=20.0,
        soc_target=80.0,
        max_charge_w=3000.0,
        eta_charge=1.0,
        round_trip_eff=1.0,
        cycle_cost_eur_per_kwh=0.04,
        export_fee_eur_per_kwh=0.0,
        max_export_w=3000.0,
        grid_export_limit_w=3000.0,
        export_peak_band_frac=0.12,
    )
    d.update(kw)
    return Config(**d)


def test_morning_subpeak_blocked_evening_peak_exports():
    cfg = _cfg()
    n = 24
    pv = [0.0] * n
    load = [0.0] * n
    price = [0.20] * n
    raw_export = [0.0] * n
    raw_export[8] = 0.30  # morning bump: clears the cycle-cost hurdle (0.30*1 - 0.04 > 0)
    raw_export[18] = 0.60  # evening peak
    ep = [effective_export_price(p, cfg) for p in raw_export]
    res = optimize_grid(
        pv,
        load,
        price,
        soc_start=80.0,
        cfg=cfg,
        window_start_h=0,
        window_len=n,
        export_price=ep,
        terminal_mode="water_value",
        water_value=0.0,
    )
    # Band floor at h8 = peak_from[8] (=0.60) * (1-0.12) = 0.528; morning 0.30 < 0.528 -> blocked.
    assert res["export_schedule"][8] == pytest.approx(0.0, abs=1e-9)
    # Evening peak (0.60 >= 0.528) exports.
    assert res["export_schedule"][18] > 0.0


def test_band_one_admits_all_hours():
    """band=1.0 -> band floor = 0 -> only the cycle-cost hurdle gates (back-compat escape hatch)."""
    cfg = _cfg(export_peak_band_frac=1.0)
    n = 24
    raw_export = [0.0] * n
    raw_export[8] = 0.30
    raw_export[18] = 0.60
    ep = [effective_export_price(p, cfg) for p in raw_export]
    res = optimize_grid(
        [0.0] * n,
        [0.0] * n,
        [0.20] * n,
        soc_start=80.0,
        cfg=cfg,
        window_start_h=0,
        window_len=n,
        export_price=ep,
        terminal_mode="water_value",
        water_value=0.0,
    )
    assert res["export_schedule"][8] > 0.0  # hurdle clears, band fully open


def test_windowed_peak_lookback_zero_is_suffix_max():
    p = [0.1, 0.6, 0.4, 0.2]
    # suffix max: [0.6, 0.6, 0.4, 0.2]
    assert windowed_peak_prices(p, 0) == [0.6, 0.6, 0.4, 0.2]


def test_windowed_peak_lookback_remembers_recent_peak():
    p = [0.1, 0.6, 0.4, 0.2]
    # lookback=2: peak[h] = max(p[h-2:]) -> h2 sees p[0..]=0.6, h3 sees p[1..]=0.6
    assert windowed_peak_prices(p, 2) == [0.6, 0.6, 0.6, 0.6]


def test_windowed_peak_empty():
    assert windowed_peak_prices([], 3) == []


def test_postpeak_downslope_blocked():
    """Down-slope hour after a peak must NOT export (judged vs the recent peak)."""
    cfg = _cfg(export_peak_lookback_h=3)
    n = 24
    pv = [0.0] * n
    load = [0.0] * n
    price = [0.20] * n
    raw_export = [0.0] * n
    raw_export[18] = 0.60  # evening peak
    raw_export[20] = 0.40  # down-slope, 2h after peak: 0.40 < 0.60*0.88=0.528
    ep = [effective_export_price(p, cfg) for p in raw_export]
    res = optimize_grid(
        pv,
        load,
        price,
        soc_start=80.0,
        cfg=cfg,
        window_start_h=0,
        window_len=n,
        export_price=ep,
        terminal_mode="water_value",
        water_value=0.0,
    )
    assert res["export_schedule"][18] > 0.0  # peak still exports
    assert res["export_schedule"][20] == pytest.approx(0.0, abs=1e-9)  # down-slope blocked


def test_lookback_zero_restores_downslope_export():
    """Escape hatch: lookback=0 == legacy suffix-max -> down-slope exports again."""
    cfg = _cfg(export_peak_lookback_h=0)
    n = 24
    raw_export = [0.0] * n
    raw_export[18] = 0.60
    raw_export[20] = 0.40
    ep = [effective_export_price(p, cfg) for p in raw_export]
    res = optimize_grid(
        [0.0] * n,
        [0.0] * n,
        [0.20] * n,
        soc_start=80.0,
        cfg=cfg,
        window_start_h=0,
        window_len=n,
        export_price=ep,
        terminal_mode="water_value",
        water_value=0.0,
    )
    assert res["export_schedule"][20] > 0.0  # legacy: forgets the peak -> exports


def test_windowed_peak_per_day_does_not_leak_across_days():
    p = [0.10] * 48
    p[18] = 0.30  # day1 peak
    p[42] = 0.50  # day2 (higher) peak
    day = [h // 24 for h in range(48)]
    out = windowed_peak_prices(p, 4, day_index=day)
    assert out[0] == 0.30  # day1 hour judged vs day1's own peak, not day2's 0.50
    assert out[18] == 0.30
    assert out[42] == 0.50


def test_windowed_peak_no_day_index_is_legacy_global():
    p = [0.10] * 48
    p[18] = 0.30
    p[42] = 0.50
    out = windowed_peak_prices(p, 4)  # day_index=None
    assert out[0] == 0.50  # legacy global suffix-max leaks day2's higher peak


def test_two_day_horizon_exports_both_daily_peaks():
    """48h window: a higher day-2 peak must not block export at day-1's own peak."""
    cfg = _cfg(export_peak_lookback_h=4)
    n = 48
    pv = [0.0] * n
    for h in (10, 11, 12, 13):  # day1 midday solar refills the pack
        pv[h] = 3.0
    for h in (34, 35, 36, 37):  # day2 midday solar refills the pack
        pv[h] = 3.0
    load = [0.0] * n
    price = [0.20] * n
    raw_export = [0.10] * n
    raw_export[18] = 0.30  # day1 evening peak
    raw_export[42] = 0.50  # day2 evening peak (higher)
    ep = [effective_export_price(p, cfg) for p in raw_export]
    res = optimize_grid(
        pv,
        load,
        price,
        soc_start=80.0,
        cfg=cfg,
        window_start_h=0,
        window_len=n,
        export_price=ep,
        terminal_mode="water_value",
        water_value=0.0,
    )
    assert res["export_schedule"][18] > 0.0  # day1 peak exports (per-day band)
    assert res["export_schedule"][42] > 0.0  # day2 peak exports


def test_two_day_trough_band_allows_day1_charge_where_global_blocks():
    """48h: a cheaper day-2 trough must NOT block day-1's own cheap charge hour.
    Per-day trough band admits day-1 charging; a GLOBAL (day_index=None) band judges
    day-1 vs day-2's lower trough and blocks it."""
    cfg = _cfg(charge_window_price_band=0.02)
    n = 48
    pv = [0.0] * n
    load = [1.0] * n
    price = [0.30] * n
    price[6] = 0.15
    price[30] = 0.10
    ceiling = max(price)
    day = [h // 24 for h in range(n)]
    trough_perday = windowed_trough_prices(price, 0, day_index=day)
    trough_global = windowed_trough_prices(price, 0, day_index=None)
    mask_perday = build_charge_mask(price, ceiling, price_band=cfg.charge_window_price_band, trough=trough_perday)
    mask_global = build_charge_mask(price, ceiling, price_band=cfg.charge_window_price_band, trough=trough_global)
    assert mask_perday[6] is True
    assert mask_global[6] is False
    res = optimize_grid(
        pv,
        load,
        price,
        soc_start=20.0,
        cfg=cfg,
        window_start_h=0,
        window_len=n,
        slots_per_day=24,
        day_index=day,
        chargeable=mask_perday,
    )
    assert sum(res["schedule"][:24]) > 0.0
