from datetime import datetime, timezone, timedelta, UTC
from custom_components.anker_x1_smartgrid.dataquality import FeatureRow
from custom_components.anker_x1_smartgrid import backtest


def test_mae_rmse_basic():
    pairs = [(10.0, 12.0), (20.0, 18.0)]
    assert backtest.mae(pairs) == 2.0
    assert abs(backtest.rmse(pairs) - 2.0) < 1e-9


def _series():
    # 20 days, hour 8 only; cold days (temp 2) load 1000, warm days (temp 18) load 300
    rows = []
    base = datetime(2026, 6, 1, 8, 0, tzinfo=UTC)
    for d in range(20):
        ts = base + timedelta(days=d)
        cold = d % 2 == 0
        rows.append(FeatureRow(ts, 8, ts.weekday() >= 5, 1000.0 if cold else 300.0, 2.0 if cold else 18.0))
    return rows


def test_walk_forward_model_beats_baseline_on_temp_signal():
    rows = _series()
    res = backtest.walk_forward(rows, train_days=10, test_days=1, fallback_w=500.0)
    assert res["n_test"] > 0
    # temp-aware model should beat the hour-mean baseline when load depends on temp
    assert res["model_mae"] <= res["baseline_mae"]
    assert res["improvement_pct"] >= 0.0


def test_walk_forward_handles_insufficient_data():
    res = backtest.walk_forward([], train_days=10, test_days=1, fallback_w=500.0)
    assert res["n_test"] == 0
    assert res["model_mae"] is None


# ---------------------------------------------------------------------------
# Backward-compatibility: all pre-existing keys still present with defaults
# ---------------------------------------------------------------------------


def test_walk_forward_existing_keys_unchanged():
    """New params must not remove or alter any pre-existing return key."""
    rows = _series()
    res = backtest.walk_forward(rows, train_days=10, test_days=1, fallback_w=500.0)
    for key in (
        "model_mae",
        "baseline_mae",
        "model_rmse",
        "baseline_rmse",
        "n_test",
        "improvement_pct",
    ):
        assert key in res, f"missing pre-existing key: {key}"


def test_walk_forward_new_keys_present():
    """New metric keys appear in the return dict (even with default params)."""
    rows = _series()
    res = backtest.walk_forward(rows, train_days=10, test_days=1, fallback_w=500.0)
    for key in (
        "horizon_energy_mae_24h",
        "horizon_energy_mae_12h",
        "baseline_horizon_energy_mae_24h",
        "pinball_p50",
        "pinball_p80",
    ):
        assert key in res, f"missing new key: {key}"


# ---------------------------------------------------------------------------
# horizon_energy_mae — hand-computable expected values
# ---------------------------------------------------------------------------


def _hourly_series(n_days=60, train_load=800.0, test_load=1200.0):
    """n_days of hourly FeatureRows; first half at train_load, second half at test_load."""
    rows = []
    base = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)  # Monday
    half = n_days // 2
    for d in range(n_days):
        for h in range(24):
            ts = base + timedelta(days=d, hours=h)
            load = train_load if d < half else test_load
            rows.append(
                FeatureRow(
                    ts=ts,
                    hour=h,
                    is_weekend=(ts.weekday() >= 5),
                    load_w=load,
                    temp=10.0,
                )
            )
    return rows


def test_horizon_energy_mae_24h_hand_computed():
    """Verify the 24h horizon energy error with known train/test load values.

    Dataset: 60 days hourly (train_days=30, test_days=30) so there is
    exactly ONE rolling origin.

    Training data (days 0-29): load=800 W, temp=10 °C → model predicts 800 W
    for all (weekend, hour, bucket) combinations.
    Test data    (days 30-59): load=1200 W → actual = 1200 W.

    For the single origin, the first 24 test rows are hours 0-23 of day 30:
      pred_kWh = 24 * 800 / 1000 = 19.2 kWh
      act_kWh  = 24 * 1200 / 1000 = 28.8 kWh
      error    = |19.2 - 28.8|    = 9.6 kWh  (only 1 origin → mean = 9.6)

    The baseline also trains on 800 W data → same prediction → same error.
    """
    rows = _hourly_series(n_days=60, train_load=800.0, test_load=1200.0)
    res = backtest.walk_forward(
        rows,
        train_days=30,
        test_days=30,
        fallback_w=0.0,
    )
    assert res["horizon_energy_mae_24h"] is not None
    assert res["horizon_energy_mae_12h"] is not None
    assert res["baseline_horizon_energy_mae_24h"] is not None
    assert abs(res["horizon_energy_mae_24h"] - 9.6) < 0.01
    assert abs(res["horizon_energy_mae_12h"] - 4.8) < 0.01
    assert abs(res["baseline_horizon_energy_mae_24h"] - 9.6) < 0.01


def test_horizon_energy_none_when_insufficient_rows():
    """horizon_energy_mae_24h is None when no origin has ≥24 test rows."""
    # _series() has 1 row/day; test_days=1 → only 1 test row → can't form a 24h window
    rows = _series()
    res = backtest.walk_forward(rows, train_days=10, test_days=1, fallback_w=500.0)
    assert res["horizon_energy_mae_24h"] is None
    assert res["horizon_energy_mae_12h"] is None


# ---------------------------------------------------------------------------
# pinball_loss — pure function, hand-checked
# ---------------------------------------------------------------------------


def test_pinball_loss_underprediction_p50():
    """q=0.5, predicted < actual: loss = q * (a - p) = 0.5 * 10 = 5.0."""
    result = backtest.pinball_loss(0.5, [(10.0, 20.0)])
    assert result is not None
    assert abs(result - 5.0) < 1e-9


def test_pinball_loss_overprediction_p50():
    """q=0.5, predicted > actual: loss = (q-1) * (a-p) = -0.5 * (-10) = 5.0."""
    result = backtest.pinball_loss(0.5, [(20.0, 10.0)])
    assert result is not None
    assert abs(result - 5.0) < 1e-9


def test_pinball_loss_underprediction_p80():
    """q=0.8, predicted < actual: loss = 0.8 * 10 = 8.0."""
    result = backtest.pinball_loss(0.8, [(10.0, 20.0)])
    assert result is not None
    assert abs(result - 8.0) < 1e-9


def test_pinball_loss_overprediction_p80():
    """q=0.8, predicted > actual: loss = (0.8-1) * (10-20) = -0.2 * (-10) = 2.0."""
    result = backtest.pinball_loss(0.8, [(20.0, 10.0)])
    assert result is not None
    assert abs(result - 2.0) < 1e-9


def test_pinball_loss_perfect_prediction():
    """Zero loss when predicted == actual."""
    result = backtest.pinball_loss(0.5, [(100.0, 100.0)])
    assert result is not None
    assert result == 0.0


def test_pinball_loss_empty_returns_none():
    assert backtest.pinball_loss(0.5, []) is None
    assert backtest.pinball_loss(0.8, []) is None


def test_pinball_loss_average_over_multiple_pairs():
    """Average across two pairs: (5.0 + 5.0) / 2 = 5.0."""
    pairs = [(10.0, 20.0), (20.0, 10.0)]  # both yield 5.0 for q=0.5
    result = backtest.pinball_loss(0.5, pairs)
    assert result is not None
    assert abs(result - 5.0) < 1e-9


# ---------------------------------------------------------------------------
# walk_forward pinball: BucketedLoadModel now accepts quantile → pinball computed
# ---------------------------------------------------------------------------


def test_walk_forward_pinball_computed_for_bucketed_model():
    """BucketedLoadModel now accepts a quantile kwarg, so walk_forward computes pinball_p50/p80 (no longer None)."""
    rows = _series()
    res = backtest.walk_forward(rows, train_days=10, test_days=1, fallback_w=500.0)
    assert res["pinball_p50"] is not None
    assert res["pinball_p80"] is not None
    assert isinstance(res["pinball_p50"], float) and res["pinball_p50"] >= 0.0
    assert isinstance(res["pinball_p80"], float) and res["pinball_p80"] >= 0.0


# ---------------------------------------------------------------------------
# should_promote — promotion gate
# ---------------------------------------------------------------------------


def test_should_promote_true_when_beats_both():
    """Strictly better on both horizon-energy-24h AND MAE → promote."""
    metrics = {
        "horizon_energy_mae_24h": 1.0,
        "baseline_horizon_energy_mae_24h": 2.0,
        "model_mae": 50.0,
        "baseline_mae": 100.0,
        "n_horizon_origins_24h": 8,
    }
    assert backtest.should_promote(metrics) is True


def test_should_promote_false_tie_on_horizon():
    """Tie on horizon-energy-24h → do not promote."""
    metrics = {
        "horizon_energy_mae_24h": 2.0,
        "baseline_horizon_energy_mae_24h": 2.0,
        "model_mae": 50.0,
        "baseline_mae": 100.0,
        "n_horizon_origins_24h": 8,
    }
    assert backtest.should_promote(metrics) is False


def test_should_promote_false_worse_on_mae():
    """Better on horizon-energy but worse on MAE → do not promote."""
    metrics = {
        "horizon_energy_mae_24h": 1.0,
        "baseline_horizon_energy_mae_24h": 2.0,
        "model_mae": 200.0,
        "baseline_mae": 100.0,
        "n_horizon_origins_24h": 8,
    }
    assert backtest.should_promote(metrics) is False


def test_should_promote_false_worse_on_horizon():
    """Better on MAE but worse on horizon-energy → do not promote."""
    metrics = {
        "horizon_energy_mae_24h": 3.0,
        "baseline_horizon_energy_mae_24h": 2.0,
        "model_mae": 50.0,
        "baseline_mae": 100.0,
        "n_horizon_origins_24h": 8,
    }
    assert backtest.should_promote(metrics) is False


def test_should_promote_false_when_improvement_below_margin():
    """Beats baseline on both metrics but by < PROMOTE_MIN_IMPROVEMENT → no promote."""
    metrics = {
        "horizon_energy_mae_24h": 1.99,  # 0.5% better than 2.0 (< 2% margin)
        "baseline_horizon_energy_mae_24h": 2.0,
        "model_mae": 99.5,  # 0.5% better than 100 (< 2% margin)
        "baseline_mae": 100.0,
        "n_horizon_origins_24h": 8,
    }
    assert backtest.should_promote(metrics) is False


def test_should_promote_true_when_improvement_clears_margin():
    """Beats baseline on both metrics by > PROMOTE_MIN_IMPROVEMENT → promote."""
    metrics = {
        "horizon_energy_mae_24h": 1.90,  # 5% better
        "baseline_horizon_energy_mae_24h": 2.0,
        "model_mae": 95.0,  # 5% better
        "baseline_mae": 100.0,
        "n_horizon_origins_24h": 8,
    }
    assert backtest.should_promote(metrics) is True


def test_baseline_buckets_amsterdam_local_hour():
    """22:00 UTC in summer is 00:00 Amsterdam → weekday may advance; the baseline
    key must use local time so it aligns with the HGBR local calendar."""
    rows = [{"hour_ts": "2026-07-03T22:00:00+00:00", "house_load_mean": 500.0}]  # Fri 22:00Z = Sat 00:00 local
    base = backtest._baseline_fit_hourly(rows)
    assert (True, 0) in base  # (is_weekend=Sat, hour=0 local)
    assert (False, 22) not in base  # NOT the raw-UTC (Fri, 22) key


def test_promote_requires_min_horizon_origins():
    """Primary 24h gate must not promote on too few rolling origins."""
    m = {
        "model_mae": 1.0,
        "baseline_mae": 2.0,
        "horizon_energy_mae_24h": 1.0,
        "baseline_horizon_energy_mae_24h": 2.0,
        "n_horizon_origins_24h": 3,
    }
    assert backtest.should_promote(m) is False
    m_ok = {**m, "n_horizon_origins_24h": 8}
    assert backtest.should_promote(m_ok) is True


def test_should_promote_false_for_none_dict():
    assert backtest.should_promote(None) is False


def test_should_promote_false_for_empty_dict():
    assert backtest.should_promote({}) is False


def test_should_promote_false_for_none_value():
    """Any None metric value → do not promote."""
    metrics = {
        "horizon_energy_mae_24h": None,
        "baseline_horizon_energy_mae_24h": 2.0,
        "model_mae": 50.0,
        "baseline_mae": 100.0,
    }
    assert backtest.should_promote(metrics) is False


def test_should_promote_gappy_fallback_promotes_with_enough_samples():
    """No 24h horizon MAE (gappy) + MAE clears margin + n_test >= guard → promote."""
    metrics = {
        "horizon_energy_mae_24h": None,
        "baseline_horizon_energy_mae_24h": None,
        "model_mae": 90.0,  # 10% better than baseline
        "baseline_mae": 100.0,
        "n_test": backtest.MIN_PROMOTE_MAE_SAMPLES,
    }
    assert backtest.should_promote(metrics) is True


def test_should_promote_gappy_fallback_denied_when_thin():
    """Gappy + good MAE but too few samples → do not promote (thin-model guard)."""
    metrics = {
        "horizon_energy_mae_24h": None,
        "baseline_horizon_energy_mae_24h": None,
        "model_mae": 90.0,
        "baseline_mae": 100.0,
        "n_test": backtest.MIN_PROMOTE_MAE_SAMPLES - 1,
    }
    assert backtest.should_promote(metrics) is False


def test_should_promote_gappy_fallback_denied_below_margin():
    """Gappy + enough samples but MAE win below margin → do not promote."""
    metrics = {
        "horizon_energy_mae_24h": None,
        "baseline_horizon_energy_mae_24h": None,
        "model_mae": 99.5,  # 0.5% better (< 2% margin)
        "baseline_mae": 100.0,
        "n_test": 10_000,
    }
    assert backtest.should_promote(metrics) is False


def test_should_promote_gappy_fallback_denied_without_n_test():
    """Gappy + good MAE but n_test missing → do not promote (existing None-value test)."""
    metrics = {
        "horizon_energy_mae_24h": None,
        "baseline_horizon_energy_mae_24h": 2.0,
        "model_mae": 50.0,
        "baseline_mae": 100.0,
    }
    assert backtest.should_promote(metrics) is False


# ---------------------------------------------------------------------------
# walk_forward_hgbr — HGBR rolling-origin backtest
# ---------------------------------------------------------------------------


def _make_hourly_rows(n_days: int, base_load_w: float = 800.0, temp_c: float = 10.0):
    """Generate ``n_days * 24`` hourly rollup row dicts with all feature columns.

    Weather columns (cloud_cover_mean, humidity_mean, wind_speed_mean, irradiance_mean)
    are populated so that no feature column is entirely NaN when a training window of
    at least 8 days is used (giving load_lag_168h some non-NaN values).
    """
    from datetime import datetime, timezone, timedelta
    import math as _math

    rows = []
    base = datetime(2025, 1, 8, 0, 0, tzinfo=UTC)  # Wednesday — matches test_hgbr convention
    for d in range(n_days):
        for h in range(24):
            ts = base + timedelta(days=d, hours=h)
            rows.append(
                {
                    "hour_ts": ts.isoformat(),
                    "house_load_mean": base_load_w + h * 5.0,  # slight hour-to-hour variation
                    "temp_forecast_mean": temp_c,
                    "cloud_cover_mean": 0.5,
                    "humidity_mean": 60.0,
                    "wind_speed_mean": 3.0,
                    "irradiance_mean": max(0.0, 300.0 * _math.sin((h - 6) / 12 * _math.pi)),
                }
            )
    return rows


def test_walk_forward_hgbr_returns_full_metric_set_on_sufficient_data():
    """With enough hourly data, walk_forward_hgbr returns all key/non-None metrics.

    train_days=10 (≥8) ensures load_lag_168h has some non-NaN values so the
    feature matrix is not degenerate and HGBR can fit a real model.
    """
    rows = _make_hourly_rows(n_days=20)
    res = backtest.walk_forward_hgbr(rows, train_days=10, test_days=3, fallback_w=500.0)
    assert res["n_test"] > 0
    assert res["model_mae"] is not None
    assert res["model_mae"] >= 0.0
    assert res["model_rmse"] is not None
    assert res["baseline_mae"] is not None
    assert res["baseline_rmse"] is not None
    assert res["horizon_energy_mae_24h"] is not None
    assert res["horizon_energy_mae_12h"] is not None
    assert res["baseline_horizon_energy_mae_24h"] is not None
    assert res["pinball_p50"] is not None
    assert res["pinball_p80"] is not None


def test_walk_forward_hgbr_returns_same_keys_as_walk_forward():
    """walk_forward_hgbr must expose the identical metric-key set as walk_forward."""
    rows = _make_hourly_rows(n_days=20)
    res = backtest.walk_forward_hgbr(rows, train_days=10, test_days=3, fallback_w=500.0)
    expected_keys = {
        "model_mae",
        "baseline_mae",
        "model_rmse",
        "baseline_rmse",
        "n_test",
        "improvement_pct",
        "horizon_energy_mae_24h",
        "horizon_energy_mae_12h",
        "baseline_horizon_energy_mae_24h",
        "n_horizon_origins_24h",
        "pinball_p50",
        "pinball_p80",
    }
    assert set(res.keys()) == expected_keys


def test_walk_forward_hgbr_rmse_gte_mae():
    """RMSE ≥ MAE always holds (RMSE penalises outliers more)."""
    rows = _make_hourly_rows(n_days=20)
    res = backtest.walk_forward_hgbr(rows, train_days=10, test_days=3, fallback_w=500.0)
    assert res["model_rmse"] is not None
    assert res["model_rmse"] >= res["model_mae"] - 1e-9


def test_walk_forward_hgbr_insufficient_data_returns_all_none():
    """Fewer rows than needed for even one origin → all-None metrics, no raise."""
    # 5 days of hourly data with train_days=7 → no origin falls inside the range.
    rows = _make_hourly_rows(n_days=5)
    res = backtest.walk_forward_hgbr(rows, train_days=7, test_days=1, fallback_w=500.0)
    assert res["model_mae"] is None
    assert res["n_test"] == 0


def test_walk_forward_hgbr_empty_input_returns_all_none():
    """Empty hourly_rows → all-None metrics dict, no raise."""
    res = backtest.walk_forward_hgbr([], train_days=7, test_days=1, fallback_w=500.0)
    assert res["model_mae"] is None
    assert res["n_test"] == 0


def test_walk_forward_hgbr_sklearn_missing_returns_all_none(monkeypatch):
    """When sklearn is absent, walk_forward_hgbr returns all-None dict without raising."""
    from custom_components.anker_x1_smartgrid import hgbr as hgbr_mod

    def _no_sklearn():
        raise ImportError("sklearn not installed (simulated)")

    monkeypatch.setattr(hgbr_mod, "_import_sklearn", _no_sklearn)
    rows = _make_hourly_rows(n_days=20)
    res = backtest.walk_forward_hgbr(rows, train_days=7, test_days=3, fallback_w=500.0)
    assert res["model_mae"] is None
    assert res["n_test"] == 0


def test_walk_forward_hgbr_baseline_mae_hand_computed():
    """Hand-verified baseline MAE for constant train/test loads.

    Dataset: 20 days hourly (10 train + 10 test) → single rolling origin at day 10.

    Training (days 0–9): all entries at 1000 W → hour-mean = 1000 W for every
    (is_weekend, hour) cell.  train_days=10 (≥8) ensures load_lag_168h has some
    non-NaN values (rows on days 7–9 look back to days 0–2 — present in the window).

    Test (days 10–19): all entries at 1500 W.
    Baseline prediction for every test row = 1000 W (hour-mean from training).
    Expected baseline MAE = |1000 − 1500| = 500 W.
    """
    from datetime import datetime, timezone, timedelta as _td

    base = datetime(2025, 1, 8, 0, 0, tzinfo=UTC)  # same start as test_hgbr
    rows = []
    for d in range(10):  # train at 1000 W
        for h in range(24):
            rows.append(
                {
                    "hour_ts": (base + _td(days=d, hours=h)).isoformat(),
                    "house_load_mean": 1000.0,
                    "temp_forecast_mean": 10.0,
                    "cloud_cover_mean": 0.5,
                    "humidity_mean": 60.0,
                    "wind_speed_mean": 3.0,
                    "irradiance_mean": 0.0,
                }
            )
    for d in range(10, 20):  # test at 1500 W
        for h in range(24):
            rows.append(
                {
                    "hour_ts": (base + _td(days=d, hours=h)).isoformat(),
                    "house_load_mean": 1500.0,
                    "temp_forecast_mean": 10.0,
                    "cloud_cover_mean": 0.5,
                    "humidity_mean": 60.0,
                    "wind_speed_mean": 3.0,
                    "irradiance_mean": 0.0,
                }
            )

    res = backtest.walk_forward_hgbr(rows, train_days=10, test_days=10, fallback_w=500.0)
    assert res["baseline_mae"] is not None
    assert abs(res["baseline_mae"] - 500.0) < 1.0  # allow tiny float rounding


def test_walk_forward_hgbr_scores_the_multi_step_serving_regime():
    """The promotion gate must score what production serves: one chained horizon.

    Predicting each test hour independently leaves ``load_lag_1h`` NaN from the
    second hour on — a regime production no longer runs (predict_series chains
    its own forecasts forward), so a gate measuring it grades the wrong model.
    """
    from datetime import datetime, timedelta

    import pytest

    from custom_components.anker_x1_smartgrid import featureset
    from custom_components.anker_x1_smartgrid.hgbr import HGBRQuantileModel

    rows = _make_hourly_rows(22)  # exactly one rolling origin at 21/1
    res = backtest.walk_forward_hgbr(rows, train_days=21, test_days=1, fallback_w=500.0)

    origin = datetime.fromisoformat(rows[0]["hour_ts"]) + timedelta(days=21)
    train = [r for r in rows if datetime.fromisoformat(r["hour_ts"]) < origin]
    test = [r for r in rows if datetime.fromisoformat(r["hour_ts"]) >= origin]
    assert len(test) == 24, "fixture must yield a single 24 h test window"

    preds = (
        HGBRQuantileModel()
        .fit(train, quantiles=(0.5, 0.8))
        .predict_series(
            [
                {
                    "when": datetime.fromisoformat(r["hour_ts"]),
                    "temp": r.get("temp_forecast_mean"),
                    "cloud_cover": r.get("cloud_cover_mean"),
                    "humidity": r.get("humidity_mean"),
                    "wind_speed": r.get("wind_speed_mean"),
                    "persons_home": r.get("persons_home_mean"),
                }
                for r in test
            ],
            500.0,
            quantiles=(0.5, 0.8),
        )
    )
    actual = [featureset.hourly_load_w(r) for r in test]

    expected_mae = sum(abs(p[0.5] - a) for p, a in zip(preds, actual)) / len(actual)
    expected_h24 = abs(sum(p[0.5] for p in preds) / 1000.0 - sum(actual) / 1000.0)

    assert res["model_mae"] == pytest.approx(expected_mae, rel=1e-9)
    assert res["horizon_energy_mae_24h"] == pytest.approx(expected_h24, rel=1e-9)


def test_walk_forward_hgbr_per_hour_regime_is_opt_in():
    """``chained=False`` grades hours independently — what an in-process
    ``LoadPredictor`` tier serves, since it has no way to chain a horizon."""
    from datetime import datetime, timedelta

    import pytest

    from custom_components.anker_x1_smartgrid import featureset
    from custom_components.anker_x1_smartgrid.hgbr import HGBRQuantileModel

    rows = _make_hourly_rows(22)
    res = backtest.walk_forward_hgbr(
        rows,
        train_days=21,
        test_days=1,
        fallback_w=500.0,
        chained=False,
    )

    origin = datetime.fromisoformat(rows[0]["hour_ts"]) + timedelta(days=21)
    train = [r for r in rows if datetime.fromisoformat(r["hour_ts"]) < origin]
    test = [r for r in rows if datetime.fromisoformat(r["hour_ts"]) >= origin]
    model = HGBRQuantileModel().fit(train, quantiles=(0.5, 0.8))
    expected = [
        (
            model.predict_load_w(
                datetime.fromisoformat(r["hour_ts"]),
                r.get("temp_forecast_mean"),
                500.0,
                quantile=0.5,
            ),
            featureset.hourly_load_w(r),
        )
        for r in test
    ]

    assert res["model_mae"] == pytest.approx(backtest.mae(expected), rel=1e-9)


def _shifted_rows(n_days: int, *, shift_before: datetime, shift_w: float) -> list[dict]:
    """``_make_hourly_rows`` with a day-to-day wobble and ``shift_w`` added to every
    row before ``shift_before`` — separates a 14-day window from all history."""
    rows = _make_hourly_rows(n_days)
    for i, row in enumerate(rows):
        row["house_load_mean"] += ((i // 24) * 37 % 11) * 15.0
        if datetime.fromisoformat(row["hour_ts"]) < shift_before:
            row["house_load_mean"] += shift_w
    return rows


def test_walk_forward_hgbr_score_from_places_origins_without_trimming_history():
    """Rows before ``score_from`` are history only: with the fixed training window
    the gate is identical to scoring the trimmed rows."""
    rows = _make_hourly_rows(40)
    cut = datetime.fromisoformat(rows[0]["hour_ts"]) + timedelta(days=15)

    res = backtest.walk_forward_hgbr(rows, train_days=10, test_days=3, fallback_w=500.0, score_from=cut)
    trimmed = backtest.walk_forward_hgbr(
        [r for r in rows if datetime.fromisoformat(r["hour_ts"]) >= cut],
        train_days=10,
        test_days=3,
        fallback_w=500.0,
    )

    assert res["n_test"] > 0
    assert res == trimmed


def test_walk_forward_hgbr_recency_mode_grades_the_served_model_against_the_window_baseline():
    """With a half-life each origin's model trains on ALL rows before it (recency
    weighted, as of the origin) and serves the prior blend — exactly what the
    add-on serves — while the baseline stays the train_days hour-mean."""
    import pytest

    from custom_components.anker_x1_smartgrid import featureset
    from custom_components.anker_x1_smartgrid.hgbr import HGBRQuantileModel

    start = datetime(2025, 1, 8, 0, 0, tzinfo=UTC)
    origin = start + timedelta(days=29)
    rows = _shifted_rows(30, shift_before=origin - timedelta(days=14), shift_w=600.0)
    res = backtest.walk_forward_hgbr(
        rows,
        train_days=14,
        test_days=1,
        fallback_w=500.0,
        half_life_days=7.0,
        prior_weight=0.25,
        score_from=start + timedelta(days=15),
    )

    train = [r for r in rows if datetime.fromisoformat(r["hour_ts"]) < origin]
    test = [r for r in rows if datetime.fromisoformat(r["hour_ts"]) >= origin]
    assert len(test) == 24, "fixture must yield a single 24 h test window"
    preds = (
        HGBRQuantileModel()
        .fit(train, quantiles=(0.5, 0.8), half_life_days=7.0, prior_weight=0.25, as_of=origin)
        .predict_series(
            [
                {
                    "when": datetime.fromisoformat(r["hour_ts"]),
                    "temp": r.get("temp_forecast_mean"),
                    "cloud_cover": r.get("cloud_cover_mean"),
                    "humidity": r.get("humidity_mean"),
                    "wind_speed": r.get("wind_speed_mean"),
                    "persons_home": r.get("persons_home_mean"),
                }
                for r in test
            ],
            500.0,
            quantiles=(0.5, 0.8),
        )
    )
    actual = [featureset.hourly_load_w(r) for r in test]
    window = featureset.hour_mean_profile(
        [r for r in train if datetime.fromisoformat(r["hour_ts"]) >= origin - timedelta(days=14)]
    )
    base = [window[featureset.hour_mean_key(datetime.fromisoformat(r["hour_ts"]))] for r in test]

    assert res["model_mae"] == pytest.approx(backtest.mae(list(zip([p[0.5] for p in preds], actual))), rel=1e-9)
    assert res["baseline_mae"] == pytest.approx(backtest.mae(list(zip(base, actual))), rel=1e-9)


def test_walk_forward_hgbr_recency_origin_ages_rows_from_the_origin_not_the_last_row():
    """Hours missing just before an origin must not move the ages/prior window:
    the gate fits as of the origin, as the add-on serves as of now."""
    import pytest

    from custom_components.anker_x1_smartgrid import featureset
    from custom_components.anker_x1_smartgrid.hgbr import HGBRQuantileModel

    start = datetime(2025, 1, 8, 0, 0, tzinfo=UTC)
    origin = start + timedelta(days=29)
    gap_from = origin - timedelta(hours=6)
    rows = [
        r
        for r in _shifted_rows(30, shift_before=origin - timedelta(days=14), shift_w=600.0)
        if not gap_from <= datetime.fromisoformat(r["hour_ts"]) < origin
    ]
    res = backtest.walk_forward_hgbr(
        rows,
        train_days=14,
        test_days=1,
        fallback_w=500.0,
        half_life_days=7.0,
        prior_weight=0.25,
        score_from=start + timedelta(days=15),
    )

    train = [r for r in rows if datetime.fromisoformat(r["hour_ts"]) < origin]
    test = [r for r in rows if datetime.fromisoformat(r["hour_ts"]) >= origin]
    series = [
        {
            "when": datetime.fromisoformat(r["hour_ts"]),
            "temp": r.get("temp_forecast_mean"),
            "cloud_cover": r.get("cloud_cover_mean"),
            "humidity": r.get("humidity_mean"),
            "wind_speed": r.get("wind_speed_mean"),
            "persons_home": r.get("persons_home_mean"),
        }
        for r in test
    ]
    actual = [featureset.hourly_load_w(r) for r in test]

    def mae_as_of(as_of):
        model = HGBRQuantileModel().fit(train, quantiles=(0.5, 0.8), half_life_days=7.0, prior_weight=0.25, as_of=as_of)
        preds = model.predict_series(series, 500.0, quantiles=(0.5, 0.8))
        return backtest.mae(list(zip([p[0.5] for p in preds], actual)))

    assert res["model_mae"] == pytest.approx(mae_as_of(origin), rel=1e-9)
    assert mae_as_of(origin) != pytest.approx(mae_as_of(None), rel=1e-9), "fixture must separate the two"


def test_walk_forward_hgbr_unchained_scoring_ignores_the_prior_blend():
    rows = _shifted_rows(30, shift_before=datetime(2025, 1, 1, tzinfo=UTC), shift_w=0.0)
    kw = dict(train_days=14, test_days=1, fallback_w=500.0, half_life_days=7.0, chained=False)

    assert backtest.walk_forward_hgbr(rows, prior_weight=0.25, **kw) == backtest.walk_forward_hgbr(rows, **kw)
