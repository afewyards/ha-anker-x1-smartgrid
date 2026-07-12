"""Plan A regression-lock (pure layer): the overnight gap-fill makes build_intervals
emit accurate per-hour overnight intervals, so energy.ride_out_reserve_kwh integrates
the real overnight load instead of one lumped interval / a floor collapse."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


from custom_components.anker_x1_smartgrid import energy, parsers, scheduler
from custom_components.anker_x1_smartgrid.forecast import LoadPredictor, build_intervals
from custom_components.anker_x1_smartgrid.models import Config

NOW = datetime(2026, 6, 26, 19, 0, tzinfo=timezone.utc)       # summer evening
SUNSET = datetime(2026, 6, 26, 21, 0, tzinfo=timezone.utc)
SUNRISE = datetime(2026, 6, 27, 5, 0, tzinfo=timezone.utc)
SUNSET2 = datetime(2026, 6, 27, 21, 0, tzinfo=timezone.utc)


def _cfg(**kw) -> Config:
    d = dict(capacity_kwh=10.0, soc_floor=5.0, eta_charge=1.0)
    d.update(kw)
    return Config(**d)


def _curve_and_intervals(cfg: Config):
    curve = parsers.build_two_day_pv_curve(
        [(0.2, None)], [(8.0, None)], NOW, SUNSET, SUNRISE, SUNSET2, step_h=1.0
    )
    predictor = LoadPredictor.from_profile({})  # empty profile → 400 W fallback everywhere
    intervals = build_intervals(curve, predictor, 400.0, cfg, quantile=0.8)
    return curve, intervals


def test_overnight_intervals_are_hourly_pv_zero():
    cfg = _cfg()
    _curve, intervals = _curve_and_intervals(cfg)
    iv_by_hour = {iv.start: iv for iv in intervals}
    # Every overnight hour [SUNSET, SUNRISE) is its own dt_h=1 interval with pv=0.
    h = SUNSET
    while h < SUNRISE:
        iv = iv_by_hour.get(h)
        assert iv is not None, f"no interval at overnight hour {h}"
        assert iv.pv_w == 0.0, f"overnight pv must be 0 at {h}, got {iv.pv_w}"
        assert iv.dt_h == 1.0, f"overnight interval must be 1 h at {h}, got {iv.dt_h}"
        h += timedelta(hours=1)


def test_ride_out_reserve_integrates_full_overnight_not_floor():
    cfg = _cfg()
    _curve, intervals = _curve_and_intervals(cfg)
    floor_kwh = cfg.soc_floor / 100.0 * cfg.capacity_kwh  # 0.5 kWh
    # Suffix from SUNSET mirrors _build_reserve_by_hour's per-hour suffix filter.
    suffix = [iv for iv in intervals if iv.start >= SUNSET]
    pickup = scheduler.find_next_solar_pickup(SUNSET + timedelta(hours=1), suffix)
    assert pickup is not None and pickup > SUNSET, "tomorrow-morning PV pickup must exist"
    # ride_out walks [SUNSET, trough] debit-only; the dark night dominates the trough.
    rsv = energy.ride_out_reserve_kwh(SUNSET, suffix, cfg)
    dark_h = (SUNRISE - SUNSET).total_seconds() / 3600.0  # 8 h
    assert rsv >= dark_h * 0.4 - 1e-6        # >= raw AC night load (ride_out divides by eta_d -> larger)
    assert rsv > floor_kwh + 1.0             # NOT floor-collapsed
