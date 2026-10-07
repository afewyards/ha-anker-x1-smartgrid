"""Recency-weighted fit and hour-mean prior blend for HGBRQuantileModel.

Package and sklearn imports stay at module level: pytest_homeassistant_custom_component
rewrites sys.modules during tests, and late imports see a corrupted namespace.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, UTC

import pytest
from sklearn.ensemble import HistGradientBoostingRegressor

from custom_components.anker_x1_smartgrid import hgbr as hgbr_module
from custom_components.anker_x1_smartgrid.featureset import (
    build_feature_matrix,
    feature_names,
    hour_mean_key,
    hour_mean_profile,
)
from custom_components.anker_x1_smartgrid.hgbr import HGBRQuantileModel

_START = datetime(2025, 1, 8, 0, 0, tzinfo=UTC)
_FALLBACK_W = 400.0


def _rows(n_days: int, *, level_w=lambda ts: 0.0) -> list[dict]:
    """Hourly rows: diurnal sinusoid plus a deterministic day-to-day wobble, so the
    model's median and the plain hour-mean genuinely differ."""
    rows: list[dict] = []
    for i in range(n_days * 24):
        ts = _START + timedelta(hours=i)
        load = 500.0 + 200.0 * math.sin(ts.hour / 24 * 2 * math.pi) + ((i // 24) * 37 % 11) * 15.0 + level_w(ts)
        rows.append(
            {
                "hour_ts": ts.isoformat(),
                "house_load_mean": load,
                "house_load_kwh_sum": load / 1000.0,
                "temp_forecast_mean": 10.0 + (i // 24) % 5,
                "cloud_cover_mean": 0.5,
                "humidity_mean": 60.0,
                "wind_speed_mean": 3.0,
                "persons_home_mean": 2.0,
            }
        )
    return rows


def _ts(row: dict) -> datetime:
    return datetime.fromisoformat(row["hour_ts"])


def _hours(first: datetime, count: int) -> list[dict]:
    return [
        {
            "when": first + timedelta(hours=i),
            "temp": 12.0,
            "cloud_cover": 0.5,
            "humidity": 60.0,
            "wind_speed": 3.0,
            "persons_home": 2.0,
        }
        for i in range(count)
    ]


@pytest.fixture
def recorded_fits(monkeypatch):
    """Record (X, sample_weight) of every HGBR fit while still fitting for real."""
    fits: list[tuple[list, list | None]] = []

    class _Recording(HistGradientBoostingRegressor):
        def fit(self, X, y, sample_weight=None):
            fits.append(([list(r) for r in X], None if sample_weight is None else list(sample_weight)))
            return super().fit(X, y, sample_weight=sample_weight)

    monkeypatch.setattr(hgbr_module, "_import_sklearn", lambda: _Recording)
    return fits


# ---------------------------------------------------------------------------
# Recency weighting
# ---------------------------------------------------------------------------


def test_default_fit_is_unweighted_on_every_row(recorded_fits):
    rows = _rows(30)
    HGBRQuantileModel().fit(rows, quantiles=(0.5,))
    X, weights = recorded_fits[0]
    assert weights is None
    assert len(X) == len(rows)


def test_recency_weights_halve_per_half_life_from_the_end_of_the_newest_hour(recorded_fits):
    rows = _rows(30)
    HGBRQuantileModel().fit(rows, quantiles=(0.5,), half_life_days=7.0)

    as_of = _ts(rows[-1]) + timedelta(hours=1)
    _, _, index = build_feature_matrix(rows)
    expected = [0.5 ** (((as_of - datetime.fromisoformat(i)).total_seconds() / 86400.0) / 7.0) for i in index]
    _, weights = recorded_fits[0]
    assert weights == pytest.approx(expected, rel=1e-12)


def test_recency_weights_measure_age_from_an_explicit_as_of(recorded_fits):
    rows = _rows(30)
    as_of = _ts(rows[-1]) + timedelta(days=3)
    HGBRQuantileModel().fit(rows, quantiles=(0.5,), half_life_days=7.0, as_of=as_of)

    _, weights = recorded_fits[0]
    newest_age_days = (as_of - _ts(rows[-1])).total_seconds() / 86400.0
    assert weights[-1] == pytest.approx(0.5 ** (newest_age_days / 7.0), rel=1e-12)


def test_recency_fit_leaves_out_rows_older_than_ten_half_lives(recorded_fits):
    rows = _rows(30)
    HGBRQuantileModel().fit(rows, quantiles=(0.5,), half_life_days=1.0)

    X, weights = recorded_fits[0]
    assert len(X) == 10 * 24
    assert min(weights) >= 0.5**10


def test_rows_beyond_the_recency_cap_still_feed_the_kept_rows_lags(recorded_fits):
    rows = _rows(30)
    HGBRQuantileModel().fit(rows, quantiles=(0.5,), half_life_days=1.0)

    X, _ = recorded_fits[0]
    assert not math.isnan(X[0][feature_names().index("load_lag_168h")])


def test_recency_weighted_model_serves_finite_predictions():
    rows = _rows(30)
    model = HGBRQuantileModel().fit(rows, quantiles=(0.5, 0.8), half_life_days=7.0)
    preds = model.predict_series(_hours(_ts(rows[-1]) + timedelta(hours=1), 24), _FALLBACK_W, quantiles=(0.5, 0.8))
    assert model._fitted
    assert all(math.isfinite(p[0.5]) and math.isfinite(p[0.8]) for p in preds)


# ---------------------------------------------------------------------------
# Hour-mean prior blend
# ---------------------------------------------------------------------------


def _prior_of(rows: list[dict], as_of: datetime) -> dict:
    return hour_mean_profile([r for r in rows if as_of - timedelta(days=14) <= _ts(r) < as_of])


def test_prior_blend_serves_the_weighted_mix_of_model_and_hour_mean():
    rows = _rows(30)
    first = _ts(rows[-1]) + timedelta(hours=1)
    plain = HGBRQuantileModel().fit(rows, quantiles=(0.5, 0.8))
    blended = HGBRQuantileModel().fit(rows, quantiles=(0.5, 0.8), prior_weight=0.25)

    ml = plain.predict_series(_hours(first, 1), _FALLBACK_W, quantiles=(0.5, 0.8))[0][0.5]
    served = blended.predict_series(_hours(first, 1), _FALLBACK_W, quantiles=(0.5, 0.8))[0][0.5]

    hm = _prior_of(rows, first)[hour_mean_key(first)]
    assert abs(hm - ml) > 1.0, "fixture must separate the model from the hour-mean"
    assert served == pytest.approx(0.75 * ml + 0.25 * hm, rel=1e-12)


def test_prior_blend_shifts_p80_by_the_median_delta_and_keeps_it_above_the_median():
    rows = _rows(30)
    hours = _hours(_ts(rows[-1]) + timedelta(hours=1), 1)
    plain = HGBRQuantileModel().fit(rows, quantiles=(0.5, 0.8)).predict_series(hours, _FALLBACK_W, quantiles=(0.5, 0.8))
    served = (
        HGBRQuantileModel()
        .fit(rows, quantiles=(0.5, 0.8), prior_weight=0.25)
        .predict_series(hours, _FALLBACK_W, quantiles=(0.5, 0.8))
    )

    delta = served[0][0.5] - plain[0][0.5]
    assert served[0][0.8] == pytest.approx(max(plain[0][0.8] + delta, served[0][0.5]), rel=1e-12)
    assert served[0][0.8] >= served[0][0.5]


def test_prior_blend_feeds_the_served_median_into_the_next_hours_lag():
    rows = _rows(30)
    first = _ts(rows[-1]) + timedelta(hours=1)
    model = HGBRQuantileModel().fit(rows, quantiles=(0.5,), prior_weight=0.25)
    served = model.predict_series(_hours(first, 2), _FALLBACK_W, quantiles=(0.5,))

    lookup, date_kwh = dict(model._utc_lookup), dict(model._local_date_kwh)
    HGBRQuantileModel._record_chain_hour(lookup, date_kwh, first, served[0][0.5])
    second = _hours(first, 2)[1]
    ml_second = model.predict_load_w(
        second["when"],
        second["temp"],
        _FALLBACK_W,
        cloud_cover=second["cloud_cover"],
        humidity=second["humidity"],
        wind_speed=second["wind_speed"],
        persons_home=second["persons_home"],
        utc_lookup=lookup,
        local_date_kwh=date_kwh,
    )
    hm = _prior_of(rows, first)[hour_mean_key(second["when"])]
    assert served[1][0.5] == pytest.approx(0.75 * ml_second + 0.25 * hm, rel=1e-12)


def test_prior_covers_only_the_fourteen_days_before_as_of():
    first = _START + timedelta(days=30)
    rows = _rows(30, level_w=lambda ts: 5000.0 if ts < first - timedelta(days=14) else 0.0)
    model = HGBRQuantileModel().fit(rows, quantiles=(0.5,), prior_weight=1.0)

    served = model.predict_series(_hours(first, 1), _FALLBACK_W, quantiles=(0.5,))[0][0.5]
    assert served == pytest.approx(_prior_of(rows, first)[hour_mean_key(first)], rel=1e-12)


def test_prior_blend_serves_the_model_alone_for_an_hour_the_prior_never_saw():
    rows = _rows(30)
    first = _ts(rows[-1]) + timedelta(hours=1)
    missing = hour_mean_key(first)
    rows = [r for r in rows if hour_mean_key(_ts(r)) != missing or _ts(r) < first - timedelta(days=14)]
    hours = _hours(first, 1)

    plain = HGBRQuantileModel().fit(rows, quantiles=(0.5, 0.8)).predict_series(hours, _FALLBACK_W, quantiles=(0.5, 0.8))
    served = (
        HGBRQuantileModel()
        .fit(rows, quantiles=(0.5, 0.8), prior_weight=0.25)
        .predict_series(hours, _FALLBACK_W, quantiles=(0.5, 0.8))
    )
    assert served == plain


@pytest.mark.parametrize("weight", [None, 0.0])
def test_no_prior_weight_serves_the_model_alone(weight):
    rows = _rows(30)
    hours = _hours(_ts(rows[-1]) + timedelta(hours=1), 24)
    plain = HGBRQuantileModel().fit(rows, quantiles=(0.5, 0.8)).predict_series(hours, _FALLBACK_W, quantiles=(0.5, 0.8))
    served = (
        HGBRQuantileModel()
        .fit(rows, quantiles=(0.5, 0.8), prior_weight=weight)
        .predict_series(hours, _FALLBACK_W, quantiles=(0.5, 0.8))
    )
    assert served == plain


def test_prior_blend_applies_to_an_upper_quantile_requested_alone():
    rows = _rows(30)
    hours = _hours(_ts(rows[-1]) + timedelta(hours=1), 6)
    model = HGBRQuantileModel().fit(rows, quantiles=(0.5, 0.8), prior_weight=0.25)

    alone = model.predict_series(hours, _FALLBACK_W, quantiles=(0.8,))
    both = model.predict_series(hours, _FALLBACK_W, quantiles=(0.5, 0.8))
    assert [set(p) for p in alone] == [{0.8}] * len(hours)
    assert [p[0.8] for p in alone] == [p[0.8] for p in both]


def test_prior_blend_keeps_p80_above_the_served_median_when_the_model_crosses(monkeypatch):
    rows = _rows(30)
    first = _ts(rows[-1]) + timedelta(hours=1)
    model = HGBRQuantileModel().fit(rows, quantiles=(0.5, 0.8), prior_weight=0.5)
    monkeypatch.setattr(
        HGBRQuantileModel,
        "predict_load_w",
        lambda self, when, temp, fallback_w, *, quantile=0.5, **kw: {0.5: 100.0, 0.8: 90.0}[quantile],
    )

    served = model.predict_series(_hours(first, 1), _FALLBACK_W, quantiles=(0.5, 0.8))[0]

    hm = _prior_of(rows, first)[hour_mean_key(first)]
    assert served[0.5] == pytest.approx(0.5 * 100.0 + 0.5 * hm)
    assert 90.0 + (served[0.5] - 100.0) < served[0.5], "fixture must push the shifted P80 below the median"
    assert served[0.8] == served[0.5]


def test_prior_ignores_rows_at_or_after_as_of():
    as_of = _START + timedelta(days=20)
    rows = _rows(30, level_w=lambda ts: 5000.0 if ts >= as_of else 0.0)
    model = HGBRQuantileModel().fit(rows, quantiles=(0.5,), prior_weight=1.0, as_of=as_of)

    served = model.predict_series(_hours(as_of, 1), _FALLBACK_W, quantiles=(0.5,))[0][0.5]
    assert served == pytest.approx(_prior_of([r for r in rows if _ts(r) < as_of], as_of)[hour_mean_key(as_of)])
    assert served < 2000.0


def test_recency_fit_trains_only_on_rows_before_as_of(recorded_fits):
    rows = _rows(30)
    as_of = _START + timedelta(days=20)
    HGBRQuantileModel().fit(rows, quantiles=(0.5,), half_life_days=7.0, as_of=as_of)

    X, weights = recorded_fits[0]
    assert len(X) == 20 * 24
    assert max(weights) < 1.0


def test_prior_needs_a_median_model_to_blend_into():
    rows = _rows(30)
    hours = _hours(_ts(rows[-1]) + timedelta(hours=1), 6)
    plain = HGBRQuantileModel().fit(rows, quantiles=(0.8,)).predict_series(hours, _FALLBACK_W, quantiles=(0.8,))
    served = (
        HGBRQuantileModel()
        .fit(rows, quantiles=(0.8,), prior_weight=0.25)
        .predict_series(hours, _FALLBACK_W, quantiles=(0.8,))
    )
    assert served == plain


def test_prior_window_is_the_gate_training_window():
    from custom_components.anker_x1_smartgrid import const

    assert hgbr_module._PRIOR_DAYS == const.DEFAULT_TRAIN_DAYS


def test_recency_fit_builds_features_only_over_the_rows_its_lags_can_reach(monkeypatch):
    rows = _rows(60)
    built: list[int] = []
    real = build_feature_matrix

    def _spy(r):
        built.append(len(r))
        return real(r)

    monkeypatch.setattr(hgbr_module.featureset, "build_feature_matrix", _spy)
    HGBRQuantileModel().fit(rows, quantiles=(0.5,), half_life_days=1.0)

    assert built == [(10 + 8) * 24]


def test_trimming_history_leaves_the_trained_matrix_unchanged(recorded_fits):
    rows = _rows(60)
    HGBRQuantileModel().fit(rows, quantiles=(0.5,), half_life_days=1.0)

    X, weights = recorded_fits[0]
    full_X, _, index = build_feature_matrix(rows)
    as_of = _ts(rows[-1]) + timedelta(hours=1)
    keep = [j for j, i in enumerate(index) if (as_of - datetime.fromisoformat(i)).total_seconds() <= 10 * 86400]
    assert len(X) == len(keep)
    for got, j in zip(X, keep):
        assert got == pytest.approx(full_X[j], nan_ok=True)
