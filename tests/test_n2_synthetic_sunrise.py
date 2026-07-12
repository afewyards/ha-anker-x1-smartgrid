"""N2 discriminating test: overnight ride-out reserve must survive a missing sun entity.

Root cause: when ``sun_times=None`` (no sun entity) or the two-day PV curve is empty,
``compute_decision`` sets ``intervals_reserve`` (single-day, ending at
tonight's price horizon edge, e.g. 00:00).  Then:

1. ``_build_reserve_by_hour`` calls ``scheduler.find_next_solar_pickup(h+1h, suffix)``.
2. All intervals have ``pv_w=0`` (no PV at night, no two-day curve) → returns ``None``.
3. ``energy.reserve_kwh(next_opp=None)`` sums discharge across the *entire suffix* —
   but the suffix only reaches tonight's horizon edge, MISSING the 23:00→08:00 overnight
   load.  The battery may exhaust before next morning's solar pickup.

Fix: when the sun entity is unavailable, extend ``intervals_reserve`` with synthetic
fallback-load intervals from tonight's horizon edge to ``FALLBACK_SOLAR_PICKUP_HOUR_UTC``
(08:00 UTC) the next morning, so the ride-out reserve always covers the full overnight load.

The firmware 5% hard floor backstops this estimate.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, UTC

from custom_components.anker_x1_smartgrid import controller as ctrl
from custom_components.anker_x1_smartgrid.models import (
    Config,
    ControllerState,
    PlantInputs,
    PlanState,
    PriceSlot,
)
from custom_components.anker_x1_smartgrid.forecast import LoadPredictor

# 22:00 UTC — overnight, sun entity unavailable
NOW = datetime(2026, 6, 26, 22, 0, tzinfo=UTC)
SUNSET = datetime(2026, 6, 26, 20, 0, tzinfo=UTC)  # already past

_PREDICTOR = LoadPredictor.from_profile({})  # all hours → fallback_load (400 W)


def _cfg(**kw) -> Config:
    return Config(
        **{
            "capacity_kwh": 10.0,
            "soc_floor": 5.0,
            "eta_charge": 1.0,
            # Trough finder needs at least 1h lookahead (2 slots are fine).
            "trough_percentile": 30.0,
            "trough_lookahead_h": 48,
            "min_horizon_h": 1,
            **kw,
        }
    )


def _overnight_result(cfg: Config):
    """Call compute_decision with sun_times=None and 2 tonight-only price slots."""
    slots = [PriceSlot(NOW + timedelta(hours=i), 0.15) for i in range(2)]
    plan = PlanState(ControllerState.PASSIVE, NOW - timedelta(hours=2), ())
    inputs = PlantInputs(soc=80.0, meter_w=0.0, now=NOW)
    return ctrl.compute_decision(
        plan,
        inputs,
        slots,
        pv_remaining=0.0,
        sunset=SUNSET,
        predictor=_PREDICTOR,
        cur_temp=None,
        cfg=cfg,
        sun_times=None,  # ← no sun entity → triggers N2 code path
    )


def test_n2_missing_sun_entity_reserve_covers_overnight():
    """Discriminating: reserve at 22:00 must cover the full overnight load to 08:00.

    Setup: sun_times=None, 2 price slots (22:00 & 23:00), zero PV, 400 W load.

    Before fix: intervals_reserve = intervals (2 intervals ending at 00:00).
                _build_reserve_by_hour sums only 2 × 400 W × 1 h = 0.8 kWh → FAILS.
    After fix:  intervals_reserve extended with 8 synthetic intervals (00:00–07:00).
                _build_reserve_by_hour sums 10 × 400 W × 1 h = 4.0 kWh → PASSES.
    """
    cfg = _cfg()
    slots = [PriceSlot(NOW + timedelta(hours=i), 0.15) for i in range(2)]

    _plan, _setpoint, _deadline, _horizon, _hm, intervals_reserve = _overnight_result(cfg)

    # Compute the per-hour ride-out reserve from the returned intervals_reserve.
    rsv = ctrl._build_reserve_by_hour(NOW, slots, intervals_reserve, cfg)

    floor_kwh = cfg.soc_floor / 100.0 * cfg.capacity_kwh  # 5% × 10 kWh = 0.5 kWh
    reserve_at_22 = rsv.get(NOW, floor_kwh)

    # BEFORE fix: reserve_at_22 ≈ 0.8 kWh (only 2 h of load to tonight's edge).
    # AFTER fix:  reserve_at_22 ≈ 4.0 kWh (10 h of load to synthetic 08:00 pickup).
    # Threshold = 2.0 kWh — strictly between old value (0.8) and new value (4.0).
    assert reserve_at_22 >= 2.0, (
        f"BUG N2: reserve must cover overnight load to synthetic 08:00 pickup; "
        f"got {reserve_at_22:.3f} kWh (expected ≥ 2.0 kWh). "
        "Hint: intervals_reserve ends at tonight's price horizon — fix must extend "
        "it with synthetic fallback-load intervals to FALLBACK_SOLAR_PICKUP_HOUR_UTC."
    )


def test_n2_reserve_substantially_larger_than_tonight_only():
    """Cross-check: the extended reserve must be meaningfully larger than a 2h reserve.

    This rules out a fix that accidentally only covers 3h instead of the full night.
    A correct fix covers 10h (22:00→08:00): 10 × 0.4 kWh = 4.0 kWh.
    """
    cfg = _cfg()
    slots = [PriceSlot(NOW + timedelta(hours=i), 0.15) for i in range(2)]

    _plan, _setpoint, _deadline, _horizon, _hm, intervals_reserve = _overnight_result(cfg)
    rsv = ctrl._build_reserve_by_hour(NOW, slots, intervals_reserve, cfg)

    floor_kwh = cfg.soc_floor / 100.0 * cfg.capacity_kwh
    reserve_at_22 = rsv.get(NOW, floor_kwh)

    # 2-interval (tonight-only) reserve: 2 × 0.4 kWh = 0.8 kWh.
    # Full-night reserve (08:00 pickup): 10 × 0.4 kWh = 4.0 kWh.
    # 3.0 kWh threshold: must be MORE than 7 hours of synthetic load (8 intervals × 0.4).
    assert reserve_at_22 >= 3.0, f"Reserve must cover most of the night (≥ 3.0 kWh); got {reserve_at_22:.3f} kWh"


def test_n2_intervals_reserve_extended_past_horizon_edge():
    """Structural test: after fix, intervals_reserve must have more coverage than
    the raw price-horizon intervals (tonight-only 2 intervals ending at 00:00).

    Verifies that the fix actually extends the list, not just returns the same 2 intervals.
    """
    cfg = _cfg()

    _plan, _setpoint, _deadline, _horizon, _hm, intervals_reserve = _overnight_result(cfg)

    # The price horizon ends at 00:00 (2 slots: 22:00, 23:00 → horizon_edge = 00:00).
    # intervals covers [22:00, 23:00] only.
    # After fix: intervals_reserve must extend past 00:00 (midnight).
    midnight = NOW.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    last_start = max(iv.start for iv in intervals_reserve)
    assert last_start >= midnight, (
        f"After fix, intervals_reserve must reach past midnight; last interval start = {last_start}"
    )
