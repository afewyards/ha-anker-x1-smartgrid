"""Q2: load model trained on one weekend-class must keep a per-hour shape for the
unseen class via an opposite-weekend hour-of-day fallback, NOT collapse to the flat
global mean / fallback."""

from datetime import datetime, timezone, UTC

import pytest

from custom_components.anker_x1_smartgrid import forecast as fc
from custom_components.anker_x1_smartgrid.dataquality import FeatureRow
from custom_components.anker_x1_smartgrid.loadmodel import BucketedLoadModel

SAT = datetime(2026, 6, 27, 0, 0, tzinfo=UTC)  # Saturday (is_weekend=True)
_TS = datetime(2026, 6, 23, 0, 0, tzinfo=UTC)  # dummy weekday timestamp


def _weekday_rows(per_hour: int) -> list[FeatureRow]:
    """All-weekday rows; hour h has a distinct load band (100 + 10*h, spread by k)."""
    rows: list[FeatureRow] = []
    for h in range(24):
        for k in range(per_hour):
            rows.append(FeatureRow(_TS.replace(hour=h), h, False, 100.0 + 10.0 * h + k, None))
    return rows


def test_central_uses_opposite_weekend_hour_shape_not_global():
    model = BucketedLoadModel.fit(_weekday_rows(per_hour=1))
    global_mean = model._global  # flat fallback the pre-fix code would return
    p10 = model.predict_load_w(SAT.replace(hour=10), None, fallback_w=999.0)
    p15 = model.predict_load_w(SAT.replace(hour=15), None, fallback_w=999.0)
    # Per-hour weekday shape, NOT the flat global mean and NOT the fallback.
    assert p10 == pytest.approx(100.0 + 10.0 * 10)  # 200
    assert p15 == pytest.approx(100.0 + 10.0 * 15)  # 250
    assert p10 != pytest.approx(global_mean)
    assert p10 != p15  # shape preserved across hours


def test_quantile_keeps_opposite_weekend_per_hour_shape():
    model = BucketedLoadModel.fit(_weekday_rows(per_hour=8))  # >= _MIN_QUANTILE_SAMPLES
    p05 = model.predict_load_w(SAT.replace(hour=5), None, fallback_w=999.0, quantile=0.8)
    p18 = model.predict_load_w(SAT.replace(hour=18), None, fallback_w=999.0, quantile=0.8)
    # P80 from the opposite-weekend same-hour samples — varies by hour (shape kept).
    assert p05 != p18
    assert p05 >= 100.0 + 10.0 * 5  # >= that hour's central, not a flat global P80
    assert p18 >= 100.0 + 10.0 * 18


def test_forecast_profile_opposite_weekend_fallback():
    profile = {(False, h): 100.0 + h for h in range(24)}  # weekday-only profile
    sat_h7 = SAT.replace(hour=7)
    val = fc.predict_load_w(profile, sat_h7, fallback_w=999.0)
    assert val == pytest.approx(107.0)  # weekday hour-7 mean, not the 999 fallback


def test_load_predictor_profile_samples_p80_cross_weekend_shape():
    """LoadPredictor (profile-samples path) must apply a per-hour P80 for an unseen
    weekend class using the opposite-class samples — NOT collapse to the bare central
    (which is P50 without cushion).

    (a) P80 must vary by hour (shape preserved).
    (b) P80 must be >= the central estimate (cushion applied from opp-class samples).
    """
    # Weekday-only samples, 8 per hour (>= _MIN_QUANTILE_SAMPLES); hour h has a
    # distinct rising band (100 + 10*h + k) so per-hour P80 varies across hours.
    NOW = datetime(2026, 6, 26, 12, 0, tzinfo=UTC)
    samples = []
    for h in range(24):
        for k in range(8):
            ts = datetime(2026, 6, 23, h, k * 5, tzinfo=UTC)  # Monday
            samples.append((ts.isoformat(), 100.0 + 10.0 * h + k))

    lp = fc.LoadPredictor.from_profile_samples(samples, lookback_days=14, now=NOW)

    sat_h5 = SAT.replace(hour=5)
    sat_h18 = SAT.replace(hour=18)

    central_h5 = lp.predict(sat_h5, temp=None, fallback_w=999.0, quantile=0.5)
    central_h18 = lp.predict(sat_h18, temp=None, fallback_w=999.0, quantile=0.5)
    p80_h5 = lp.predict(sat_h5, temp=None, fallback_w=999.0, quantile=0.8)
    p80_h18 = lp.predict(sat_h18, temp=None, fallback_w=999.0, quantile=0.8)

    # (a) per-hour shape: P80 must differ between hours (not flat global P80)
    assert p80_h5 != p80_h18
    # (b) P80 >= central (upper cushion applied from opposite-class samples)
    assert p80_h5 > central_h5
    assert p80_h18 > central_h18
    # (c) not the fallback
    assert p80_h5 != pytest.approx(999.0)
    assert p80_h18 != pytest.approx(999.0)
    # (d) same-class path must be unchanged when same-class samples exist
    # (build a weekend-only predictor and confirm P80 comes from weekend samples)
    sat_samples = []
    for k in range(8):
        ts = datetime(2026, 6, 21, 10, k * 5, tzinfo=UTC)  # Sunday
        sat_samples.append((ts.isoformat(), 200.0 + k))
    lp_wknd = fc.LoadPredictor.from_profile_samples(sat_samples, lookback_days=14, now=NOW)
    p80_wknd = lp_wknd.predict(SAT.replace(hour=10), temp=None, fallback_w=999.0, quantile=0.8)
    central_wknd = lp_wknd.predict(SAT.replace(hour=10), temp=None, fallback_w=999.0, quantile=0.5)
    assert p80_wknd >= central_wknd  # same-class samples used, cushion applied
