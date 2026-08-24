"""Multi-step serving regime for HGBRQuantileModel.

The model is trained on rows whose lag features are ALWAYS present, but at
serve time only the first horizon hour can resolve ``load_lag_1h`` from a
completed rollup row.  Feeding NaN there puts the feature vector in a region
the model never saw during training, and its output collapses to a near
constant — the diurnal shape the DP plans against disappears.  These tests
pin the recursive (multi-step) contract that keeps serve-time lags in the
same distribution as training: each predicted hour feeds the next hour's lags.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, UTC

from custom_components.anker_x1_smartgrid.hgbr import HGBRQuantileModel

# Training signal: a clean 24 h sinusoid, 500 W ± 200 W (swing 400 W).
_BASE_W = 500.0
_AMPLITUDE_W = 200.0
_FALLBACK_W = 400.0


def _truth_w(ts: datetime) -> float:
    """The load the synthetic history implies for *ts*."""
    return _BASE_W + _AMPLITUDE_W * math.sin((ts.hour / 24) * 2 * math.pi)


def _hourly_rows(n_days: int, start: datetime | None = None) -> list[dict]:
    """``n_days`` × 24 rollup rows carrying the sinusoid and flat weather."""
    if start is None:
        start = datetime(2025, 1, 8, 0, 0, tzinfo=UTC)
    rows: list[dict] = []
    for i in range(n_days * 24):
        ts = start + timedelta(hours=i)
        load = _truth_w(ts)
        rows.append(
            {
                "hour_ts": ts.isoformat(),
                "house_load_mean": load,
                "house_load_kwh_sum": load / 1000.0,
                "house_load_max": load * 1.1,
                "house_load_min": load * 0.9,
                "house_load_std": 50.0,
                "house_load_count": 60,
                "temp_forecast_mean": 15.0,
                "cloud_cover_mean": 0.5,
                "humidity_mean": 60.0,
                "wind_speed_mean": 3.0,
                "irradiance_mean": 0.0,
                "persons_home_mean": 2.0,
            }
        )
    return rows


def _hours(first: datetime, count: int) -> list[dict]:
    """Serve-time hour dicts with the same weather the history was trained on."""
    return [
        {
            "when": first + timedelta(hours=i),
            "temp": 15.0,
            "cloud_cover": 0.5,
            "humidity": 60.0,
            "wind_speed": 3.0,
            "persons_home": 2.0,
        }
        for i in range(count)
    ]


def _fitted_model(n_days: int = 35) -> tuple[HGBRQuantileModel, datetime]:
    rows = _hourly_rows(n_days)
    model = HGBRQuantileModel().fit(rows)
    return model, datetime.fromisoformat(rows[-1]["hour_ts"])


def test_series_tracks_the_diurnal_shape_with_no_future_actuals() -> None:
    """A 24 h horizon past the last rollup row keeps the training swing."""
    model, last_actual = _fitted_model()
    hours = _hours(last_actual + timedelta(hours=1), 24)

    preds = model.predict_series(hours, _FALLBACK_W)

    p50 = [p[0.5] for p in preds]
    errors = [abs(v - _truth_w(h["when"])) for v, h in zip(p50, hours)]
    assert max(errors) < 60.0, f"multi-step forecast drifted off the sinusoid: {p50}"
    assert max(p50) - min(p50) > 300.0, f"diurnal swing collapsed: {p50}"


def test_series_leaves_the_models_own_lookups_untouched() -> None:
    """Predicted hours must not leak into the next request's lag history."""
    model, last_actual = _fitted_model()
    before = dict(model._utc_lookup)

    model.predict_series(_hours(last_actual + timedelta(hours=1), 24), _FALLBACK_W)

    assert model._utc_lookup == before


def test_series_keeps_measured_hours_instead_of_predicting_over_them() -> None:
    """An hour the rollup already covers predicts exactly as the single-hour path."""
    model, last_actual = _fitted_model()
    covered = last_actual - timedelta(hours=5)
    single = model.predict_load_w(
        covered,
        15.0,
        _FALLBACK_W,
        quantile=0.5,
        cloud_cover=0.5,
        humidity=60.0,
        wind_speed=3.0,
        persons_home=2.0,
    )

    preds = model.predict_series(_hours(covered, 3), _FALLBACK_W)

    assert preds[0][0.5] == single


def test_series_bridges_a_short_gap_before_the_first_horizon_hour() -> None:
    """A stale rollup (in-progress hours missing) must not poison the chain."""
    model, last_actual = _fitted_model()
    hours = _hours(last_actual + timedelta(hours=3), 24)

    preds = model.predict_series(hours, _FALLBACK_W)

    p50 = [p[0.5] for p in preds]
    errors = [abs(v - _truth_w(h["when"])) for v, h in zip(p50, hours)]
    assert max(errors) < 120.0, f"gap bridge lost the sinusoid: {p50}"


def test_series_returns_every_requested_quantile_in_request_order() -> None:
    model, last_actual = _fitted_model()
    hours = _hours(last_actual + timedelta(hours=1), 6)

    preds = model.predict_series(hours, _FALLBACK_W, quantiles=(0.5, 0.8))

    assert len(preds) == len(hours)
    assert all(set(p) == {0.5, 0.8} for p in preds)
