"""Plan B (B3+B4): _apply_price_prior raises the per-hour reserve UPSIDE-ONLY,
defers to real prices, and never touches intervals_reserve / slots."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from custom_components.anker_x1_smartgrid import controller as ctrl
from custom_components.anker_x1_smartgrid.models import Config, ForecastInterval, PriceSlot

NOW_H = datetime(2026, 6, 26, 18, 0, tzinfo=timezone.utc)
REAL_END = datetime(2026, 6, 27, 0, 0, tzinfo=timezone.utc)
PICKUP = datetime(2026, 6, 27, 8, 0, tzinfo=timezone.utc)   # winter-late: peak sits before pickup


def _cfg(**kw) -> Config:
    d = dict(
        capacity_kwh=10.0, soc_target=97.0, soc_floor=5.0, export_fee_eur_per_kwh=0.0,
        export_peak_band_frac=0.5, max_export_w=3000.0, grid_export_limit_w=3000.0,
        anticipation_confidence_haircut=0.15, anticipation_margin_eur_per_kwh=0.02,
    )
    d.update(kw)
    return Config(**d)  # type: ignore[arg-type]


def _slots():  # 18:00..23:00 tonight (real export hours)
    prices = [0.30, 0.32, 0.34, 0.36, 0.28, 0.26]
    return [PriceSlot(NOW_H + timedelta(hours=i), p) for i, p in enumerate(prices)]


def _intervals():  # 14 h of overnight load then tomorrow's PV ramp at +14h (08:00 pickup)
    return (
        [ForecastInterval(NOW_H + timedelta(hours=i), 0.0, 400.0, 1.0) for i in range(14)]
        + [ForecastInterval(PICKUP, 2000.0, 300.0, 1.0)]
    )


def _base_reserve():  # one entry per hour incl. one at/after pickup
    rsv = {NOW_H + timedelta(hours=i): 1.0 for i in range(14)}  # 18:00..07:00
    rsv[PICKUP] = 0.5
    return rsv


def _pricey_estimate():  # local-hour-indexed; high 07:00 peak inside [00:00, 08:00)
    est = [0.20] * 24
    est[7] = 0.80
    return est


def test_prior_raises_reserve_upside_only_on_pre_pickup_hours():
    from unittest.mock import patch
    cfg = _cfg()
    rsv = _base_reserve()
    ivs = _intervals()
    ivs_before = list(ivs)
    slots = _slots()
    with patch("homeassistant.util.dt.as_local", side_effect=lambda d: d):
        ctrl._apply_price_prior(rsv, _pricey_estimate(), slots, NOW_H, REAL_END, ivs, cfg)
    # Pre-pickup hours are raised; the >= pickup hour is left exactly alone.
    assert rsv[NOW_H] > 1.0
    assert rsv[PICKUP] == 0.5
    # Upside-only: no pre-pickup reserve was lowered.
    assert all(rsv[NOW_H + timedelta(hours=i)] >= 1.0 for i in range(14))
    # Containment: intervals_reserve and slots are untouched.
    assert ivs == ivs_before
    assert [s.start for s in slots] == [NOW_H + timedelta(hours=i) for i in range(6)]


def test_prior_no_change_when_estimate_below_tonight():
    cfg = _cfg()
    rsv = _base_reserve()
    before = dict(rsv)
    cheap = [0.10] * 24
    ctrl._apply_price_prior(rsv, cheap, _slots(), NOW_H, REAL_END, _intervals(), cfg)
    assert rsv == before     # byte-identical to no-prior


def test_prior_defers_when_real_prices_extend_past_pickup():
    """Post-publication: real horizon already past tomorrow's pickup -> empty estimated
    slots -> reserve byte-identical (defer to real prices)."""
    cfg = _cfg()
    rsv = _base_reserve()
    before = dict(rsv)
    real_end_past = datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc)  # past PICKUP
    ctrl._apply_price_prior(rsv, _pricey_estimate(), _slots(), NOW_H, real_end_past, _intervals(), cfg)
    assert rsv == before


def test_prior_noop_when_estimated_tomorrow_none():
    cfg = _cfg()
    rsv = _base_reserve()
    before = dict(rsv)
    ctrl._apply_price_prior(rsv, None, _slots(), NOW_H, REAL_END, _intervals(), cfg)
    assert rsv == before


def test_prior_noop_when_pack_headroom_zero():
    """Pack-headroom cap: soc_target ≈ base reserve → headroom ≈ 0 → nothing held."""
    cfg = _cfg(soc_target=10.0)   # 10% of 10 kWh = 1.0 kWh == base reserve -> headroom 0
    rsv = _base_reserve()
    before = dict(rsv)
    ctrl._apply_price_prior(rsv, _pricey_estimate(), _slots(), NOW_H, REAL_END, _intervals(), cfg)
    assert rsv == before


def test_prior_noop_when_no_solar_pickup():
    """Real cloudy no-op: pricey estimate but no solar PV in intervals → pickup=None → no hold."""
    from unittest.mock import patch
    cfg = _cfg()   # normal soc_target=97%, plenty of headroom
    rsv = _base_reserve()
    before = dict(rsv)
    # Intervals with zero PV — find_next_solar_pickup returns None.
    no_solar_ivs = [ForecastInterval(NOW_H + timedelta(hours=i), 0.0, 400.0, 1.0) for i in range(14)]
    with patch("homeassistant.util.dt.as_local", side_effect=lambda d: d):
        ctrl._apply_price_prior(rsv, _pricey_estimate(), _slots(), NOW_H, REAL_END, no_solar_ivs, cfg)
    assert rsv == before


def test_prior_does_not_contaminate_dp_price_path():
    """N3 containment: slots (the sole source of window_price/_peak/find_next_trough
    in compute_decision) must be byte-identical before and after _apply_price_prior,
    even when the hook fires and raises the reserve.

    If this assertion ever fails it means estimated prices have leaked into the DP
    optimizer's price inputs — a hard containment violation.
    """
    from unittest.mock import patch
    slots = _slots()
    slot_prices_before = [s.price for s in slots]
    slot_starts_before = [s.start for s in slots]
    rsv = _base_reserve()
    with patch("homeassistant.util.dt.as_local", side_effect=lambda d: d):
        ctrl._apply_price_prior(rsv, _pricey_estimate(), slots, NOW_H, REAL_END, _intervals(), _cfg())
    # Confirm the hook actually fired (held > 0) — if it didn't, the assertion is vacuous.
    assert rsv[NOW_H] > 1.0, "hook must have fired for this containment check to be meaningful"
    # slots is the only source of DP prices — must be unchanged.
    assert [s.price for s in slots] == slot_prices_before
    assert [s.start for s in slots] == slot_starts_before
