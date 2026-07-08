"""Tests for hgbr.py — HGBRQuantileModel (P3-T1).

Tests are deterministic: synthetic rows are generated inline; no DB, no HA fixtures.
sklearn IS installed in the dev venv, so normal fit/predict paths exercise the real
HistGradientBoostingRegressor.  The lazy-import path is covered by monkeypatching
the module-level ``_import_sklearn`` helper.

Imports
-------
All imports from ``custom_components.anker_x1_smartgrid`` are placed at MODULE LEVEL
(not inside test methods).  This mirrors the pattern used by ``test_featureset.py``
and avoids a breakage caused by ``pytest_homeassistant_custom_component``, which
modifies ``sys.modules`` during test execution for HA isolation — late imports
inside test methods see a corrupted module namespace.  Top-level imports are
resolved at collection time, before any HA fixture runs.

Synthetic dataset helper
------------------------
``_make_hourly_rows(n_days)`` generates consecutive hourly rows starting on
2025-01-08 00:00 UTC.  Each row carries a deterministic sinusoidal load value
and minimal weather columns (no NaN targets).

is_ready lag-complete verification (key timezone note)
------------------------------------------------------
Start = 2025-01-08 00:00 UTC = 2025-01-08 01:00 CET.
A row at t is "lag-complete" if (t − 168 h) is present.  With 7 days (168 rows,
h=0..167) no row can be lag-complete → always False.

With UTC+1 the boundary between local calendar dates falls at 23:00 UTC.
The following counts have been verified analytically:

    n_days=7  → 0  lag-complete local dates
    n_days=8  → 2  lag-complete local dates (Jan 15 and Jan 16 via the 00:00 CET boundary)
    n_days=9  → 3  lag-complete local dates (Jan 15, Jan 16, Jan 17)
    n_days=26 → 20 lag-complete local dates
    n_days=27 → 21 lag-complete local dates
    n_days=35 → 29 lag-complete local dates  (safe margin above 21)
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# TOP-LEVEL imports from the package (resolved at collection time, before HA
# fixtures run).  See module docstring for why this is required.
# ---------------------------------------------------------------------------
from custom_components.anker_x1_smartgrid import hgbr as hgbr_module
from custom_components.anker_x1_smartgrid.featureset import build_feature_matrix, feature_names
from custom_components.anker_x1_smartgrid.hgbr import HGBRQuantileModel

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Synthetic data factory
# ---------------------------------------------------------------------------


def _make_hourly_rows(
    n_days: int,
    *,
    start: datetime | None = None,
    base_load: float = 500.0,
    temp: float = 15.0,
) -> list[dict]:
    """Return n_days * 24 hourly rollup rows with a sinusoidal load pattern.

    Each row has the mandatory ``hour_ts`` and ``house_load_mean`` keys plus the
    minimal weather columns used by ``encode_weather_features``.
    """
    if start is None:
        # Wednesday 2025-01-08 00:00 UTC = 01:00 CET — fixed for reproducibility
        start = datetime(2025, 1, 8, 0, 0, 0, tzinfo=UTC)
    rows: list[dict] = []
    for h in range(n_days * 24):
        ts = start + timedelta(hours=h)
        phase = (h % 24) / 24 * 2 * math.pi
        load = base_load + 200.0 * math.sin(phase)
        rows.append(
            {
                "hour_ts": ts.isoformat(),
                "house_load_mean": load,
                "house_load_kwh_sum": load / 1000.0,
                "house_load_max": load * 1.1,
                "house_load_min": load * 0.9,
                "house_load_std": 50.0,
                "house_load_count": 60,
                "temp_forecast_mean": temp + 2.0 * math.sin(phase),
                "cloud_cover_mean": 0.5,
                "humidity_mean": 60.0,
                "wind_speed_mean": 3.0,
                "irradiance_mean": max(0.0, 300.0 * math.sin((h % 24 - 6) / 12 * math.pi)),
                "persons_home_mean": 2.0,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# 1. Import sanity (no top-level sklearn in hgbr module)
# ---------------------------------------------------------------------------


class TestModuleImport:
    def test_hgbr_module_importable(self) -> None:
        """hgbr module is importable — verified by the top-level import above."""
        assert hgbr_module is not None
        assert hasattr(hgbr_module, "HGBRQuantileModel")
        assert hasattr(hgbr_module, "_import_sklearn")

    def test_instantiate_without_sklearn(self) -> None:
        """HGBRQuantileModel() can be constructed when sklearn is patched away."""

        def _raise() -> None:
            raise ImportError("sklearn not installed")

        with patch.object(hgbr_module, "_import_sklearn", _raise):
            model = HGBRQuantileModel()
            assert not model._fitted


# ---------------------------------------------------------------------------
# 2. Fit / predict round-trip (sklearn IS available)
# ---------------------------------------------------------------------------


class TestFitPredict:
    def test_round_trip_returns_finite_nonnegative(self) -> None:
        """After fitting on 30 days, predict_load_w returns a finite non-negative float."""
        rows = _make_hourly_rows(30)
        model = HGBRQuantileModel()
        model.fit(rows)

        assert model._fitted, "model should be fitted after 30 days of data"
        assert 0.5 in model._models and 0.8 in model._models

        # Predict an hour that is AFTER the training window
        when = datetime(2025, 2, 9, 12, 0, 0, tzinfo=UTC)
        result = model.predict_load_w(when, temp=12.0, fallback_w=12345.0)

        assert math.isfinite(result), f"prediction should be finite, got {result}"
        assert result >= 0.0, "load cannot be negative"
        assert result != 12345.0, "model should have predicted (not returned fallback)"

    def test_p50_and_p80_are_distinct_model_objects(self) -> None:
        """Two distinct fitted model objects are stored under their quantile keys."""
        rows = _make_hourly_rows(30)
        model = HGBRQuantileModel().fit(rows, quantiles=(0.5, 0.8))

        assert 0.5 in model._models
        assert 0.8 in model._models
        assert model._models[0.5] is not model._models[0.8]

    def test_both_quantiles_return_finite_nonnegative(self) -> None:
        """predict_load_w with q=0.5 and q=0.8 both return finite non-negative values."""
        rows = _make_hourly_rows(30)
        model = HGBRQuantileModel().fit(rows, quantiles=(0.5, 0.8))
        when = datetime(2025, 2, 9, 18, 0, 0, tzinfo=UTC)

        p50 = model.predict_load_w(when, temp=10.0, fallback_w=12345.0, quantile=0.5)
        p80 = model.predict_load_w(when, temp=10.0, fallback_w=12345.0, quantile=0.8)

        assert math.isfinite(p50) and p50 >= 0.0
        assert math.isfinite(p80) and p80 >= 0.0

    def test_single_quantile_fit(self) -> None:
        """Fitting with a single quantile works; that quantile predicts correctly."""
        rows = _make_hourly_rows(30)
        model = HGBRQuantileModel().fit(rows, quantiles=(0.5,))
        assert model._fitted
        assert 0.8 not in model._models

        when = datetime(2025, 2, 9, 9, 0, 0, tzinfo=UTC)
        result = model.predict_load_w(when, temp=13.0, fallback_w=12345.0)
        assert math.isfinite(result) and result >= 0.0

    def test_feature_names_snapshot_stored(self) -> None:
        """fit() stores a snapshot of featureset.feature_names() for predict-time use."""
        rows = _make_hourly_rows(30)
        model = HGBRQuantileModel().fit(rows)
        assert model._feature_names == feature_names()
        assert len(model._feature_names) == 18

    def test_feature_vector_length(self) -> None:
        """_assemble_feature_vector returns exactly 18 elements."""
        rows = _make_hourly_rows(30)
        model = HGBRQuantileModel().fit(rows)
        when = datetime(2025, 2, 9, 14, 0, 0, tzinfo=UTC)
        vec = model._assemble_feature_vector(when, temp=10.0)
        assert vec is not None
        assert len(vec) == len(feature_names()) == 18

    def test_predict_clamps_to_zero(self) -> None:
        """HGBR predictions are clamped to ≥ 0.0 even if the raw output is negative."""
        rows = _make_hourly_rows(30)
        model = HGBRQuantileModel().fit(rows)
        # Force the internal model to return a negative value
        for _q, m in model._models.items():
            m.predict = lambda X: [-100.0]  # type: ignore[method-assign]

        when = datetime(2025, 2, 9, 12, 0, 0, tzinfo=UTC)
        result = model.predict_load_w(when, temp=12.0, fallback_w=400.0)
        assert result == 0.0


# ---------------------------------------------------------------------------
# 3. Fallback paths
# ---------------------------------------------------------------------------


class TestFallback:
    def test_unfitted_model_returns_fallback(self) -> None:
        """Unfitted model returns exactly fallback_w."""
        model = HGBRQuantileModel()
        assert not model._fitted

        result = model.predict_load_w(
            datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC),
            temp=15.0,
            fallback_w=777.0,
        )
        assert result == 777.0

    def test_untrained_quantile_returns_fallback(self) -> None:
        """Requesting a quantile not present in _models returns fallback_w."""
        rows = _make_hourly_rows(30)
        model = HGBRQuantileModel().fit(rows, quantiles=(0.5, 0.8))
        when = datetime(2025, 2, 9, 12, 0, 0, tzinfo=UTC)

        # q=0.9 was not trained
        result = model.predict_load_w(when, temp=12.0, fallback_w=333.0, quantile=0.9)
        assert result == 333.0

    def test_fit_empty_rows_stays_unfitted(self) -> None:
        """Fitting on zero rows leaves the model unfitted (no exception)."""
        model = HGBRQuantileModel().fit([])
        assert not model._fitted
        assert model.predict_load_w(
            datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC), 15.0, 400.0
        ) == 400.0

    def test_fit_too_few_rows_stays_unfitted(self) -> None:
        """Fitting on fewer than _MIN_TRAIN_ROWS (24) rows leaves the model unfitted.

        Uses 10 non-empty rows — distinct from test_fit_empty_rows_stays_unfitted —
        to specifically exercise the _MIN_TRAIN_ROWS guard in fit().
        """
        rows = _make_hourly_rows(1)[:10]  # 10 rows < _MIN_TRAIN_ROWS = 24
        assert len(rows) == 10
        model = HGBRQuantileModel().fit(rows)
        assert not model._fitted

    def test_predict_nan_temp_no_crash(self) -> None:
        """predict_load_w with temp=None or NaN must not raise; returns finite or fallback."""
        rows = _make_hourly_rows(30)
        model = HGBRQuantileModel().fit(rows)
        when = datetime(2025, 2, 9, 12, 0, 0, tzinfo=UTC)

        for bad_temp in (None, float("nan")):
            result = model.predict_load_w(when, temp=bad_temp, fallback_w=500.0)
            # HGBR handles NaN in features; either a prediction or the fallback
            assert math.isfinite(result) and result >= 0.0


# ---------------------------------------------------------------------------
# 4. is_ready gate
# ---------------------------------------------------------------------------


class TestIsReady:
    """Verify the lag-complete gate logic.

    Timezone note: start = 2025-01-08 00:00 UTC = 01:00 CET.  With UTC+1:
        n_days=7  → 0  lag-complete local dates  (7×24=168 rows, none have 168h lag)
        n_days=8  → 2  lag-complete local dates
        n_days=9  → 3  lag-complete local dates
        n_days=26 → 20 lag-complete local dates
        n_days=27 → 21 lag-complete local dates
        n_days=35 → 29 lag-complete local dates
    """

    def test_false_on_7_days_any_min(self) -> None:
        """7 days of data → 0 lag-complete rows → always False."""
        rows = _make_hourly_rows(7)
        model = HGBRQuantileModel()
        # Even min_days=1 must be False because no rows have their 168h lag
        assert not model.is_ready(rows, min_days=1)

    def test_true_with_3_lag_complete_days(self) -> None:
        """9 days → 3 lag-complete local dates → True with min_days=3."""
        rows = _make_hourly_rows(9)
        model = HGBRQuantileModel()
        assert model.is_ready(rows, min_days=3)

    def test_false_just_below_min_days(self) -> None:
        """8 days → 2 lag-complete local dates → False with min_days=3."""
        rows = _make_hourly_rows(8)
        model = HGBRQuantileModel()
        assert not model.is_ready(rows, min_days=3)

    def test_default_min_days_true(self) -> None:
        """27 days → 21 lag-complete local dates → True with default min_days=21."""
        rows = _make_hourly_rows(27)
        model = HGBRQuantileModel()
        assert model.is_ready(rows)  # uses default min_days=21

    def test_default_min_days_false(self) -> None:
        """26 days → 20 lag-complete local dates → False with default min_days=21."""
        rows = _make_hourly_rows(26)
        model = HGBRQuantileModel()
        assert not model.is_ready(rows)  # 20 < 21

    def test_empty_rows_false(self) -> None:
        """Empty row list → False."""
        model = HGBRQuantileModel()
        assert not model.is_ready([])

    def test_large_dataset_true(self) -> None:
        """35 days → 29 lag-complete local dates — comfortably above 21."""
        rows = _make_hourly_rows(35)
        model = HGBRQuantileModel()
        assert model.is_ready(rows)


# ---------------------------------------------------------------------------
# 5. Lazy-import resilience
# ---------------------------------------------------------------------------


class TestLazyImport:
    """Verify that all entry points degrade gracefully when sklearn is absent.

    Technique: ``patch.object(hgbr_module, "_import_sklearn", _raise)`` replaces
    the module-level helper with one that always raises ``ImportError``.  This
    avoids touching ``sys.modules`` and cleanly tests every code path that calls
    ``_import_sklearn()`` without actually uninstalling the library.
    """

    @staticmethod
    def _raise_import_error():
        raise ImportError("sklearn not installed (simulated)")

    def test_is_ready_false_when_sklearn_missing(self) -> None:
        """is_ready returns False when sklearn cannot be imported."""
        rows = _make_hourly_rows(35)
        model = HGBRQuantileModel()

        with patch.object(hgbr_module, "_import_sklearn", self._raise_import_error):
            assert not model.is_ready(rows, min_days=1)

    def test_fit_returns_self_unfitted_when_sklearn_missing(self) -> None:
        """fit() returns self in an unfitted state when sklearn is missing."""
        rows = _make_hourly_rows(35)
        model = HGBRQuantileModel()

        with patch.object(hgbr_module, "_import_sklearn", self._raise_import_error):
            result = model.fit(rows)

        assert result is model, "fit must return self"
        assert not model._fitted
        assert model._models == {}

    def test_predict_returns_fallback_when_sklearn_missing(self) -> None:
        """predict_load_w returns fallback_w when sklearn cannot be imported.

        The model is fitted FIRST (sklearn is available), then sklearn is
        hidden for the predict call — simulating a runtime ImportError.
        """
        rows = _make_hourly_rows(30)
        model = HGBRQuantileModel()
        model.fit(rows)
        assert model._fitted, "precondition: model should be fitted"

        when = datetime(2025, 2, 9, 12, 0, 0, tzinfo=UTC)
        fallback = 555.0

        with patch.object(hgbr_module, "_import_sklearn", self._raise_import_error):
            result = model.predict_load_w(when, temp=12.0, fallback_w=fallback)

        assert result == fallback

    def test_import_hgbr_never_fails(self) -> None:
        """The hgbr module import itself must succeed without sklearn.

        Verified implicitly: the top-level import at the head of this file
        succeeded — if sklearn were imported at module level in hgbr.py and
        were absent, that import would have raised.
        """
        assert hgbr_module is not None
        assert hasattr(hgbr_module, "HGBRQuantileModel")
        assert hasattr(hgbr_module, "_import_sklearn")


# ---------------------------------------------------------------------------
# 6. Predict-time vector assembly edge cases
# ---------------------------------------------------------------------------


class TestVectorAssembly:
    def test_empty_history_predict_does_not_crash(self) -> None:
        """Clearing stored history after fit must not cause predict_load_w to raise."""
        rows = _make_hourly_rows(30)
        model = HGBRQuantileModel().fit(rows)
        assert model._fitted

        # Wipe lookup — simulates stale / empty history between retrains
        model._utc_lookup = {}
        model._local_date_kwh = {}

        when = datetime(2025, 2, 9, 12, 0, 0, tzinfo=UTC)
        # All lags → NaN; HGBR should still produce a valid prediction
        result = model.predict_load_w(when, temp=12.0, fallback_w=9999.0)
        # Must be finite and non-negative (HGBR tolerates NaN lags)
        assert math.isfinite(result) and result >= 0.0

    def test_predict_uses_feature_names_order(self) -> None:
        """_assemble_feature_vector keys match featureset.feature_names() exactly."""
        rows = _make_hourly_rows(30)
        model = HGBRQuantileModel().fit(rows)
        when = datetime(2025, 2, 9, 10, 0, 0, tzinfo=UTC)

        vec = model._assemble_feature_vector(when, temp=14.0)
        assert vec is not None, "_assemble_feature_vector must not return None for valid input"
        assert len(vec) == 18
        # Each element must be a numeric scalar (float or int — day_of_week is int;
        # HGBR accepts both)
        names = feature_names()
        for i, v in enumerate(vec):
            assert isinstance(v, (int, float)), f"feature[{i}] ({names[i]}) should be numeric"

    def test_lag_features_nan_when_history_missing(self) -> None:
        """With empty lookup, all lag values in the feature vector should be NaN."""
        rows = _make_hourly_rows(30)
        model = HGBRQuantileModel().fit(rows)
        model._utc_lookup = {}
        model._local_date_kwh = {}

        when = datetime(2025, 2, 9, 10, 0, 0, tzinfo=UTC)
        vec = model._assemble_feature_vector(when, temp=14.0)
        assert vec is not None

        names = feature_names()
        lag_keys = {"load_lag_1h", "load_lag_24h", "load_lag_168h", "rolling_mean_24h", "prev_day_total_kwh"}
        for key in lag_keys:
            idx = names.index(key)
            assert math.isnan(vec[idx]), f"{key} should be NaN when lookup is empty"

    def test_weather_defaults_to_nan_when_not_provided(self) -> None:
        """With only temp supplied, weather features default to NaN at serve."""
        rows = _make_hourly_rows(30)
        model = HGBRQuantileModel().fit(rows)
        when = datetime(2025, 2, 9, 10, 0, 0, tzinfo=UTC)
        vec = model._assemble_feature_vector(when, temp=14.0)
        assert vec is not None

        names = feature_names()
        for key in ("cloud_cover", "humidity", "wind_speed"):
            idx = names.index(key)
            assert math.isnan(vec[idx]), f"{key} must be NaN at predict time"

    def test_weather_passthrough_when_provided(self) -> None:
        """cloud_cover/humidity/wind_speed flow into the vector when supplied at serve."""
        rows = _make_hourly_rows(30)
        model = HGBRQuantileModel().fit(rows)
        when = datetime(2025, 2, 9, 10, 0, 0, tzinfo=UTC)
        vec = model._assemble_feature_vector(
            when, temp=14.0, cloud_cover=60.0, humidity=82.0, wind_speed=4.5
        )
        assert vec is not None
        names = feature_names()
        assert vec[names.index("cloud_cover")] == 60.0
        assert vec[names.index("humidity")] == 82.0
        assert vec[names.index("wind_speed")] == 4.5

    def test_persons_home_passthrough_when_provided(self) -> None:
        rows = _make_hourly_rows(30)
        model = HGBRQuantileModel().fit(rows)
        when = datetime(2025, 2, 9, 10, 0, 0, tzinfo=UTC)
        vec = model._assemble_feature_vector(when, temp=14.0, persons_home=3.0)
        names = feature_names()
        assert vec[names.index("persons_home")] == 3.0

    def test_hdd_cdd_correct_at_predict_time(self) -> None:
        """HDD and CDD are correctly derived from temp at predict time."""
        rows = _make_hourly_rows(30)
        model = HGBRQuantileModel().fit(rows)
        names = feature_names()
        when = datetime(2025, 2, 9, 10, 0, 0, tzinfo=UTC)

        # temp=10 → HDD=5.5, CDD=0
        vec = model._assemble_feature_vector(when, temp=10.0)
        assert vec is not None
        assert vec[names.index("temp_forecast")] == pytest.approx(10.0)
        assert vec[names.index("hdd")] == pytest.approx(5.5)
        assert vec[names.index("cdd")] == pytest.approx(0.0)

        # temp=25 → HDD=0, CDD=3
        vec2 = model._assemble_feature_vector(when, temp=25.0)
        assert vec2 is not None
        assert vec2[names.index("hdd")] == pytest.approx(0.0)
        assert vec2[names.index("cdd")] == pytest.approx(3.0)

    def test_train_predict_consistency(self) -> None:
        """Crown-jewel regression guard: training vector must equal predict vector.

        Picks a training row whose lags are fully present (index 200, well
        past the 168h seed window) and verifies that:

        - Indices 0-13 (calendar 6 + lag 5 + temp_forecast/hdd/cdd 3): identical
          between the build_feature_matrix vector and _assemble_feature_vector.
        - Indices 14-16 (cloud_cover/humidity/wind_speed): NaN in the
          predict vector (intentional per spec §5 — only temp is available at
          serve time; HGBR tolerates the NaNs).

        If the lag math in featureset and hgbr._assemble_feature_vector ever
        diverge (e.g. different window bounds, different rolling mean formula),
        this test will catch it immediately.
        """
        rows = _make_hourly_rows(30)

        # Row at index 200 (h=200, UTC=2025-01-16T08:00Z):
        # lag_1h=h199, lag_24h=h176, lag_168h=h32 — all present → no NaN lags.
        chosen_idx = 200
        chosen_row = rows[chosen_idx]
        when = datetime.fromisoformat(chosen_row["hour_ts"])
        # Pass the SAME temp used during training so temp_forecast/hdd/cdd match.
        temp = chosen_row["temp_forecast_mean"]

        # --- Training vector from build_feature_matrix ---
        X, _y, index = build_feature_matrix(rows)
        train_pos = index.index(chosen_row["hour_ts"])
        train_vec = X[train_pos]

        # --- Predict-time vector from fitted model ---
        model = HGBRQuantileModel().fit(rows)
        pred_vec = model._assemble_feature_vector(when, temp=temp)
        assert pred_vec is not None, "_assemble_feature_vector returned None unexpectedly"

        names = feature_names()

        # --- Indices 0-13: calendar + lag + temp/hdd/cdd must be identical ---
        # (6 calendar + 5 lag + 3 weather-from-temp = 14 features)
        for i in range(14):
            tv, pv = train_vec[i], pred_vec[i]
            # Both should be finite for this well-covered row
            assert math.isfinite(float(tv)), f"train feature[{i}] ({names[i]}) is non-finite: {tv!r}"
            assert tv == pytest.approx(pv, rel=1e-9), (
                f"Train/predict MISMATCH at feature[{i}] ({names[i]}): "
                f"train={tv!r}, predict={pv!r}"
            )

        # --- Indices 14-16: NaN in predict (cloud_cover/humidity/wind_speed) ---
        for key in ("cloud_cover", "humidity", "wind_speed"):
            idx = names.index(key)
            assert math.isnan(pred_vec[idx]), (
                f"predict feature[{idx}] ({key}) should be NaN at serve time, "
                f"got {pred_vec[idx]!r}"
            )
    def test_full_signal_serve_matches_training(self):
        """With weather + persons supplied at serve, the serve vector == training vector."""
        rows = _make_hourly_rows(30)
        chosen = rows[200]  # lags fully present past the 168h seed window
        when = datetime.fromisoformat(chosen["hour_ts"])

        X, _y, index = build_feature_matrix(rows)
        train_vec = X[index.index(chosen["hour_ts"])]

        model = HGBRQuantileModel().fit(rows)
        pred_vec = model._assemble_feature_vector(
            when,
            temp=chosen["temp_forecast_mean"],
            cloud_cover=chosen["cloud_cover_mean"],
            humidity=chosen["humidity_mean"],
            wind_speed=chosen["wind_speed_mean"],
            persons_home=chosen["persons_home_mean"],
        )
        assert pred_vec is not None
        names = feature_names()
        assert len(pred_vec) == len(names) == 18
        for i in range(len(names)):
            assert train_vec[i] == pytest.approx(pred_vec[i], rel=1e-9), (
                f"train/serve mismatch at feature[{i}] ({names[i]}): "
                f"{train_vec[i]!r} vs {pred_vec[i]!r}"
            )


# ---------------------------------------------------------------------------
# 6b. _build_lookups() — prev-day kWh uses house_load_kwh_sum, not mean/1000
# ---------------------------------------------------------------------------


class TestBuildLookupsUsesKwhSum:
    """``_build_lookups()`` must sum ``house_load_kwh_sum`` directly, matching
    the ``featureset.encode_lag_features_from_lookups``/``build_feature_matrix`` change."""

    def test_prev_day_kwh_uses_kwh_sum(self) -> None:
        """A row with house_load_mean=4000W but house_load_kwh_sum=0.6 must
        contribute 0.6 kWh, not the naive mean/1000 = 4.0 kWh."""
        model = HGBRQuantileModel()
        ts_str = "2026-06-20T22:00:00+00:00"
        rows = [
            {"hour_ts": ts_str, "house_load_mean": 4000.0, "house_load_kwh_sum": 0.6},
        ]
        model._build_lookups(rows)

        local_d = datetime.fromisoformat(ts_str).astimezone(hgbr_module._TZ_AMS).date()
        assert abs(model._local_date_kwh[local_d] - 0.6) < 1e-9

    def test_prev_day_kwh_skips_none(self) -> None:
        """A row with house_load_kwh_sum missing/None contributes nothing,
        even though house_load_mean is present (pre-v10 transition window)."""
        model = HGBRQuantileModel()
        rows = [
            {"hour_ts": "2026-06-20T22:00:00+00:00", "house_load_mean": 1000.0},
        ]
        model._build_lookups(rows)

        assert model._local_date_kwh == {}


# ---------------------------------------------------------------------------
# 7. Resilience: all-NaN column + sklearn fit exception (Fix 1 + Fix 2)
# ---------------------------------------------------------------------------


class TestResilienceFixes:
    """Verify that Fix 1 (try/except) and Fix 2 (all-NaN column neutralization)
    give hgbr.py its "never raises" contract under adversarial conditions.
    """

    def test_fit_all_nan_column_does_not_raise_and_yields_fitted(self) -> None:
        """7 days (168 rows) → load_lag_168h is all-NaN; fit must succeed.

        With only 7 days of history the weekly lag feature (``load_lag_168h``)
        has no look-back data for any training row — every cell in that column
        is ``float('nan')``.  Fix 2 detects this and substitutes 0.0 before
        calling sklearn, so HistGBR never sees the all-NaN column.

        The model must be fitted (``_fitted=True``) and must not raise.
        """
        rows = _make_hourly_rows(7)  # 168 rows — above _MIN_TRAIN_ROWS (24)
        model = HGBRQuantileModel()
        # Must not raise despite the all-NaN load_lag_168h column
        model.fit(rows, quantiles=(0.5,))
        assert model._fitted, (
            "fit() should succeed on 7 days — Fix 2 should have neutralized the "
            "all-NaN load_lag_168h column"
        )
        assert 0.5 in model._models

    def test_fit_sklearn_fit_raises_returns_unfitted(self) -> None:
        """If sklearn's own fit() raises, hgbr.fit() returns an unfitted model.

        Monkeypatches ``_import_sklearn`` to return a fake HGBR class whose
        ``fit()`` always raises ``ValueError``.  Fix 1 (try/except) must catch
        this and leave the model clean — no exception propagated, _fitted=False.
        """

        class _FakeHGBR:
            """Minimal stub that raises on fit() to simulate a sklearn crash."""

            def __init__(self, **_kwargs: object) -> None:
                pass

            def fit(self, _X: object, _y: object) -> None:
                raise ValueError("simulated sklearn internal error")

        rows = _make_hourly_rows(30)
        model = HGBRQuantileModel()

        with patch.object(hgbr_module, "_import_sklearn", lambda: _FakeHGBR):
            model.fit(rows, quantiles=(0.5,))

        # Fix 1 must have caught the ValueError and left the model unfitted
        assert not model._fitted, "model must be unfitted when sklearn.fit() raises"
        assert model._models == {}, "_models must be cleared after a fit exception"
