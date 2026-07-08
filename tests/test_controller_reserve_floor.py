"""C1c: controller builds a window-aligned reserve list anchored to the next solar pickup."""
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.anker_x1_smartgrid import controller as ctrl
from custom_components.anker_x1_smartgrid import parsers
from custom_components.anker_x1_smartgrid.forecast import LoadPredictor, build_intervals
from custom_components.anker_x1_smartgrid.models import Config, ForecastInterval, PriceSlot
from custom_components.anker_x1_smartgrid.optimize import optimize_grid

NOW = datetime(2026, 6, 26, 22, 0, tzinfo=timezone.utc)  # 22:00, night


def _cfg(**kw):
    d = dict(capacity_kwh=10.0, soc_floor=20.0, eta_charge=1.0,
             round_trip_eff=1.0)
    d.update(kw)
    return Config(**d)


def test_reserve_by_hour_spans_to_tomorrow_pickup_not_price_horizon():
    """Prices end tonight; PV pickup is +8h. Reserve at 'now' = P80 load over those 8h."""
    cfg = _cfg()
    # Price slots only for tonight: NOW .. NOW+3h (horizon ends tonight).
    slots = [PriceSlot(NOW + timedelta(hours=i), 0.20) for i in range(4)]
    # Two-day reserve intervals: 8h of pure load (0.5 kWh/h) then a PV pickup.
    intervals_reserve = (
        [ForecastInterval(NOW + timedelta(hours=i), 0.0, 500.0, 1.0) for i in range(8)]
        + [ForecastInterval(NOW + timedelta(hours=8), 2000.0, 300.0, 1.0)]
    )
    rsv = ctrl._build_reserve_by_hour(NOW, slots, intervals_reserve, cfg)
    # NOW hour: floor (2.0) + 8h × 0.5 kWh ride-out = 6.0 kWh (was 4.0 pre-floor-stack).
    assert rsv[NOW] == 6.0


def test_reserve_floored_at_firmware_when_load_tiny():
    cfg = _cfg()
    slots = [PriceSlot(NOW + timedelta(hours=i), 0.20) for i in range(4)]
    intervals_reserve = (
        [ForecastInterval(NOW + timedelta(hours=i), 0.0, 50.0, 1.0) for i in range(3)]
        + [ForecastInterval(NOW + timedelta(hours=3), 2000.0, 100.0, 1.0)]
    )
    rsv = ctrl._build_reserve_by_hour(NOW, slots, intervals_reserve, cfg)
    # 3h × 0.05 kWh = 0.15 kWh ride-out stacked on the 2.0 kWh floor → 2.15.
    assert rsv[NOW] == pytest.approx(2.15, abs=1e-6)


# ---------------------------------------------------------------------------
# Discriminating test: the re-anchor from find_next_charge_opportunity to
# find_next_solar_pickup is the CORE survival fix.  This test encodes the
# exact bug: an early cheap import hour (price below the ceiling after the
# post-trough peak) collapsed the ride-out window to ~2h under the old anchor,
# producing only the firmware-floor reserve instead of the full overnight load.
# ---------------------------------------------------------------------------

NOW18 = datetime(2026, 6, 26, 18, 0, tzinfo=timezone.utc)  # 18:00 — evening export peak


def _export_hour_slots():
    """Price curve: peak at +3h (0.65) with a trough at +2h (0.10).
    Ceiling = 0.65 * 0.85 = 0.5525; slot at +1h (0.45) is below it ->
    find_next_charge_opportunity would return +2h (20:00, cheapest after trough)
    as a cheap-import opportunity, collapsing the window."""
    return [
        PriceSlot(NOW18 + timedelta(hours=0), 0.56),  # export peak hour (current)
        PriceSlot(NOW18 + timedelta(hours=1), 0.45),  # below ceiling (0.5525) -> old anchor stops here
        PriceSlot(NOW18 + timedelta(hours=2), 0.10),  # trough
        PriceSlot(NOW18 + timedelta(hours=3), 0.65),  # post-trough peak (sets ceiling)
        PriceSlot(NOW18 + timedelta(hours=4), 0.25),
    ]


def _overnight_intervals():
    """12h of overnight house load (0.5 kWh/h) then tomorrow's PV ramp at +12h."""
    return (
        [ForecastInterval(NOW18 + timedelta(hours=i), 0.0, 500.0, 1.0) for i in range(12)]
        + [ForecastInterval(NOW18 + timedelta(hours=12), 2000.0, 300.0, 1.0)]
    )


def test_cheap_night_import_does_not_collapse_reserve():
    """Solar anchor produces a 6 kWh reserve spanning 12h of overnight load to
    tomorrow's PV ramp, not a collapsed 2 kWh firmware-floor reserve."""
    cfg = _cfg()
    slots = _export_hour_slots()
    intervals = _overnight_intervals()

    rsv = ctrl._build_reserve_by_hour(NOW18, slots, intervals, cfg)
    assert rsv[NOW18] == 8.0, (  # floor 2.0 + 12h × 0.5 = 8.0 (was 6.0 pre-floor-stack)
        f"Solar anchor must produce floor + 12h × 0.5 kWh = 8.0 kWh reserve; got {rsv[NOW18]}"
    )


# ── Plan A: reserve_soc continuity across the night (no 22:00 collapse) ──

SUMMER_NOW = datetime(2026, 6, 26, 19, 0, tzinfo=timezone.utc)   # summer evening
SUMMER_SUNSET = datetime(2026, 6, 26, 21, 0, tzinfo=timezone.utc)
SUMMER_SUNRISE = datetime(2026, 6, 27, 5, 0, tzinfo=timezone.utc)
SUMMER_SUNSET2 = datetime(2026, 6, 27, 21, 0, tzinfo=timezone.utc)


def _summer_reserve_dict(cfg):
    """Mirror controller.py:408-433 + 579: two-day curve → P80 intervals_reserve →
    per-hour reserve dict, the exact chain that feeds reserve_soc and the DP floor."""
    curve = parsers.build_two_day_pv_curve(
        [(0.2, None)], [(8.0, None)], SUMMER_NOW,
        SUMMER_SUNSET, SUMMER_SUNRISE, SUMMER_SUNSET2, step_h=1.0,
    )
    predictor = LoadPredictor.from_profile({})  # empty → 400 W fallback every hour
    intervals_reserve = build_intervals(curve, predictor, 400.0, cfg, quantile=0.8)
    # Hourly price slots 19:00 → 06:00 next day so every overnight hour gets a reserve.
    slots = [PriceSlot(SUMMER_NOW + timedelta(hours=i), 0.20) for i in range(12)]
    return ctrl._build_reserve_by_hour(SUMMER_NOW, slots, intervals_reserve, cfg)


def test_reserve_soc_continuous_no_collapse_at_22():
    cfg = _cfg(soc_floor=5.0)  # firmware floor = 0.5 kWh on a 10 kWh pack
    rsv = _summer_reserve_dict(cfg)
    floor_kwh = cfg.soc_floor / 100.0 * cfg.capacity_kwh

    h21 = SUMMER_NOW + timedelta(hours=2)   # 21:00
    h22 = SUMMER_NOW + timedelta(hours=3)   # 22:00 — the pre-fix collapse hour
    h23 = SUMMER_NOW + timedelta(hours=4)   # 23:00

    # No collapse: 22:00 reserve stays well above the firmware floor.
    assert rsv[h22] > floor_kwh + 1.0
    # Smooth per-hour decrement (one hour of P80 load = 0.4 kWh) across pure-overnight
    # hours — NOT a cliff to the floor at 22:00.
    assert rsv[h21] - rsv[h22] == pytest.approx(0.4, abs=1e-6)
    assert rsv[h22] - rsv[h23] == pytest.approx(0.4, abs=1e-6)
    # Continuity: every overnight hour 19:00..04:00 has a reserve entry (no gap/break).
    h = SUMMER_NOW
    while h < SUMMER_SUNRISE:
        assert h in rsv, f"reserve missing at {h} (collapse/gap)"
        h += timedelta(hours=1)
    # Evening reserve is the accurate per-hour integral to the morning pickup — lower
    # than the pre-fix lumped inflation, and strictly below a whole-pack reserve.
    assert rsv[SUMMER_NOW] < cfg.capacity_kwh


def test_dp_export_floor_uses_continuous_overnight_reserve():
    """The continuous overnight reserve, fed to optimize_grid as the export floor,
    withholds energy the pre-fix collapse would have dumped at 22:00.

    max_charge_w=0 prevents the DP from inflating SoC via cheap import (0.20→0.60€
    arbitrage) before the export window — that would create artificial headroom and
    bypass the reserve constraint.  With charge disabled, only the per-hour reserve
    floor limits how far the battery may discharge.
    """
    cfg = _cfg(
        soc_floor=5.0, soc_target=97.0, max_charge_w=0.0, eta_charge=1.0,
        round_trip_eff=1.0, cycle_cost_eur_per_kwh=0.04, export_fee_eur_per_kwh=0.0,
        max_export_w=3000.0, grid_export_limit_w=3000.0,
    )
    rsv = _summer_reserve_dict(cfg)
    n = 6  # window 19:00..00:00
    reserve_list = [rsv[SUMMER_NOW + timedelta(hours=i)] for i in range(n)]
    pv = [0.0] * n
    load = [0.0] * n
    price = [0.20] * n
    export_price = [0.0] * n
    export_price[3] = 0.60   # rich post-sunset export hours (22:00, 23:00)
    export_price[4] = 0.60

    common = dict(
        soc_start=80.0, cfg=cfg, window_start_h=0, window_len=n,
        export_price=export_price, terminal_mode="water_value", water_value=0.0,
    )
    res_floor = optimize_grid(pv, load, price, reserve_by_hour=None, **common)
    res_rsv = optimize_grid(pv, load, price, reserve_by_hour=reserve_list, **common)

    # The continuous reserve exports strictly LESS than the firmware-floor-only plan.
    assert sum(res_floor["export_schedule"]) > sum(res_rsv["export_schedule"])
    # And never drains SoC below the (window-min) per-hour reserve: exported DC ≤
    # start(8 kWh) − min reserve over the window.
    assert sum(res_rsv["export_schedule"]) <= 8.0 - min(reserve_list) + 1e-6


def test_trough_anchor_lowers_reserve_vs_legacy_when_cheap_ahead():
    # cheap morning slot ahead → trough anchor bridges to it; legacy holds the full night.
    cfg_t = _cfg(reserve_cheap_band=0.20)                        # default anchor=trough
    cfg_l = _cfg(reserve_anchor="legacy")
    prices = [0.30, 0.13, 0.30, 0.30]                            # cheap at +1h
    slots = [PriceSlot(NOW + timedelta(hours=i), p) for i, p in enumerate(prices)]
    ivs = (
        [ForecastInterval(NOW + timedelta(hours=i), 0.0, 500.0, 1.0) for i in range(3)]
        + [ForecastInterval(NOW + timedelta(hours=3), 3000.0, 200.0, 1.0)]
    )
    ic = ctrl._build_is_cheap_by_hour(slots, cfg_t)
    r_trough = ctrl._build_reserve_by_hour(NOW, slots, ivs, cfg_t, is_cheap=ic)[NOW]
    r_legacy = ctrl._build_reserve_by_hour(NOW, slots, ivs, cfg_l)[NOW]
    assert r_trough < r_legacy
    assert r_trough == pytest.approx(2.5, abs=1e-6)              # floor(2.0)+1 deficit hr×0.5, break at +1h cheap
