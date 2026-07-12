"""BC2: `_apply_price_prior` must genuinely resample the REAL slots onto a
uniform fine grid before handing them to `compute_anticipation_held_extra`,
so a single `dt_h_real` scalar is valid even when `slots` mixes near-term
15-min entries with far-term 60-min entries (the genuine Zonneplan-rollout
shape).  Pre-fix, `detect_slot_minutes` picked the finest grid (15) but the
RAW un-resampled slots were passed through, so still-60-min slots were
counted at a quarter of their true DC (spec §M4 deviation).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, UTC
from unittest.mock import patch

import pytest

from custom_components.anker_x1_smartgrid import controller as ctrl
from custom_components.anker_x1_smartgrid import optimize as opt
from custom_components.anker_x1_smartgrid import pricing_store, scheduler
from custom_components.anker_x1_smartgrid.models import Config, ForecastInterval, PriceSlot

NOW_H = datetime(2026, 6, 26, 18, 0, tzinfo=UTC)
REAL_END = datetime(2026, 6, 27, 0, 0, tzinfo=UTC)
PICKUP = datetime(2026, 6, 27, 8, 0, tzinfo=UTC)  # winter-late: peak sits before pickup


def _cfg(**kw) -> Config:
    d = dict(
        capacity_kwh=10.0,
        soc_target=97.0,
        soc_floor=5.0,
        export_fee_eur_per_kwh=0.0,
        export_peak_band_frac=0.5,  # wide band: only the est_morning walk gates inclusion
        max_export_w=3000.0,
        grid_export_limit_w=3000.0,
        anticipation_confidence_haircut=0.0,
        anticipation_margin_eur_per_kwh=0.02,
    )
    d.update(kw)
    return Config(**d)  # type: ignore[arg-type]


def _intervals():  # 14 h of overnight load then tomorrow's PV ramp at +14h (08:00 pickup)
    return [ForecastInterval(NOW_H + timedelta(hours=i), 0.0, 400.0, 1.0) for i in range(14)] + [
        ForecastInterval(PICKUP, 2000.0, 300.0, 1.0)
    ]


def _base_reserve():
    rsv = {NOW_H + timedelta(hours=i): 1.0 for i in range(14)}  # 18:00..07:00
    rsv[PICKUP] = 0.5
    return rsv


def _estimate():  # local-hour-indexed; peak at 07:00 inside [00:00, 08:00)
    est = [0.10] * 24
    est[7] = 0.305
    return est


def _uniform_hourly_slots():  # 18:00..23:00 tonight, plain 60-min slots
    prices = [0.30, 0.32, 0.34, 0.36, 0.28, 0.26]
    return [PriceSlot(NOW_H + timedelta(hours=i), p) for i, p in enumerate(prices)]


def _mixed_real_slots():
    """Genuine mixed-resolution real payload: 18:00 (near-term) split into four
    real 15-min sub-slots; 19:00..23:00 (far-term) remain 60-min hourly slots —
    the shape `resolution.py`'s own docstring describes for the Zonneplan
    rollout (near-term fine, far-term coarse).  No trailing sentinel: 23:00 is
    the chronologically LAST slot, exactly the production shape (`slots` never
    carries a slot past the real price horizon).  `_apply_price_prior` passes
    `horizon_end=real_horizon_end` (== REAL_END, one hour past 23:00) into
    `resample_price_map` so the boundary hour still infers its true 60-min
    span instead of truncating to one fine sub-slot.
    """
    quarter_prices = [0.30, 0.30, 0.30, 0.30]  # 18:00 hour, split into 4x15min
    hourly_prices = [0.32, 0.34, 0.36, 0.28, 0.26]  # 19:00..23:00, still 60-min
    slots = [PriceSlot(NOW_H + timedelta(minutes=15 * i), p) for i, p in enumerate(quarter_prices)]
    slots += [PriceSlot(NOW_H + timedelta(hours=1 + i), p) for i, p in enumerate(hourly_prices)]
    return slots


def _per_hour_dc(cfg: Config) -> float:
    eta_d = opt.eta_discharge(cfg)
    ac_cap_kwh = min(cfg.max_export_w, cfg.grid_export_limit_w) / 1000.0
    return ac_cap_kwh / eta_d if eta_d > 1e-9 else 0.0


def test_mixed_resolution_real_payload_counts_true_per_hour_dc():
    """Only the two TRUE 60-min hours (22:00 @0.28, 23:00 @0.26) clear the
    price-prior bar (est_morning=0.305, margin=0.02 -> threshold band [0.28,0.30) x
    excludes 0.30).  `held` must equal 2 full hours of DC — NOT the
    15-min-derived quarter of that the un-resampled code produced.
    """
    cfg = _cfg()
    rsv = _base_reserve()
    slots = _mixed_real_slots()
    with patch("homeassistant.util.dt.as_local", side_effect=lambda d: d):
        ctrl._apply_price_prior(rsv, _estimate(), slots, NOW_H, REAL_END, _intervals(), cfg)

    expected_held = 2 * _per_hour_dc(cfg)  # 22:00 + 23:00, each a TRUE 60-min hour
    assert rsv[NOW_H] == pytest.approx(1.0 + expected_held)
    assert all(rsv[NOW_H + timedelta(hours=i)] == pytest.approx(1.0 + expected_held) for i in range(14))
    assert rsv[PICKUP] == 0.5  # >= pickup left exactly alone

    # Containment: the caller's `slots` object must be untouched (resample builds
    # a NEW list; it must never mutate the DP's real price source in place).
    assert [s.price for s in slots] == [s.price for s in _mixed_real_slots()]
    assert [s.start for s in slots] == [s.start for s in _mixed_real_slots()]


def test_uniform_60_resample_is_identity_byte_identical():
    """Hard invariant: at uniform 60-min real slots, `detect_slot_minutes` -> 60
    and `resample_price_map(slots, 60)` is a documented no-op (resolution.py:
    'at 60 the map equals the legacy dict'), so `_apply_price_prior`'s
    post-fix reserve must equal calling `compute_anticipation_held_extra`
    directly on the RAW un-resampled slots with slot_minutes=60 — i.e. exactly
    what the pre-fix code path computed for a uniform payload."""
    cfg = _cfg()
    est = _estimate()
    ivs = _intervals()

    rsv_fixed = _base_reserve()
    rsv_old = _base_reserve()
    with patch("homeassistant.util.dt.as_local", side_effect=lambda d: d):
        ctrl._apply_price_prior(rsv_fixed, est, _uniform_hourly_slots(), NOW_H, REAL_END, ivs, cfg)

        pickup = scheduler.find_next_solar_pickup(REAL_END, ivs)
        est_slots = pricing_store.build_estimated_slots(est, REAL_END, pickup)
        held_old = pricing_store.compute_anticipation_held_extra(
            estimated_slots=est_slots,
            real_slots=_uniform_hourly_slots(),  # raw, un-resampled — pre-fix equivalent
            now_h=NOW_H,
            real_horizon_end=REAL_END,
            tomorrow_solar_pickup=pickup,
            base_reserve_by_hour=rsv_old,
            cfg=cfg,
            slot_minutes=60,
        )
        for h in list(rsv_old):
            if h < pickup:
                rsv_old[h] += held_old

    assert held_old > 0.0, "hook must have fired for this identity check to be meaningful"
    assert rsv_fixed == rsv_old
