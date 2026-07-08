from datetime import datetime, timezone
from custom_components.anker_x1_smartgrid.dataquality import FeatureRow
from custom_components.anker_x1_smartgrid import loadmodel


def _row(day, hour, load, temp, weekend=False):
    ts = datetime(2026, 6, day, hour, 0, tzinfo=timezone.utc)
    return FeatureRow(ts, hour, weekend, load, temp)


def test_temp_bucket_edges():
    assert loadmodel.temp_bucket(-10) == 0
    assert loadmodel.temp_bucket(2) == 2
    assert loadmodel.temp_bucket(100) == len(loadmodel.TEMP_BUCKETS)
    assert loadmodel.temp_bucket(None) == -1


def test_fit_and_predict_specific_cell():
    rows = [
        _row(15, 8, 1000.0, 2.0),   # weekday, hour8, cold
        _row(16, 8, 1200.0, 2.0),
        _row(17, 8, 300.0, 18.0),   # weekday, hour8, warm
    ]
    m = loadmodel.BucketedLoadModel.fit(rows)
    cold = m.predict_load_w(datetime(2026, 6, 22, 8, tzinfo=timezone.utc), temp=2.0, fallback_w=0.0)
    warm = m.predict_load_w(datetime(2026, 6, 22, 8, tzinfo=timezone.utc), temp=18.0, fallback_w=0.0)
    assert cold == 1100.0   # mean of 1000,1200
    assert warm == 300.0


def test_predict_falls_back_to_hour_cell_when_temp_bucket_missing():
    rows = [_row(15, 8, 900.0, 2.0), _row(16, 8, 1100.0, 8.0)]
    m = loadmodel.BucketedLoadModel.fit(rows)
    # query a temp bucket we never saw -> falls back to (weekday, hour) mean = 1000
    out = m.predict_load_w(datetime(2026, 6, 22, 8, tzinfo=timezone.utc), temp=19.0, fallback_w=0.0)
    assert out == 1000.0


def test_predict_uses_fallback_when_no_data():
    m = loadmodel.BucketedLoadModel.fit([])
    out = m.predict_load_w(datetime(2026, 6, 22, 8, tzinfo=timezone.utc), temp=10.0, fallback_w=375.0)
    assert out == 375.0


# ---------------------------------------------------------------------------
# Quantile-aware tests (TDD — written before implementation)
# ---------------------------------------------------------------------------

def test_empirical_quantile_linear_interpolation():
    """_empirical_quantile([10,20,30,40,50], 0.8) == 42.0 (numpy 'linear'/type-7)."""
    from custom_components.anker_x1_smartgrid.loadmodel import _empirical_quantile
    assert _empirical_quantile([10.0, 20.0, 30.0, 40.0, 50.0], 0.8) == 42.0


def test_empirical_quantile_single_value():
    """_empirical_quantile on a single-element list returns that element."""
    from custom_components.anker_x1_smartgrid.loadmodel import _empirical_quantile
    assert _empirical_quantile([77.0], 0.8) == 77.0


def test_bucketed_p80_returns_empirical_quantile():
    """Cell with >=8 right-skewed samples: P80 > mean; P50 returns mean unchanged."""
    # 10 values: [100,200,300,400,500,600,700,800,900,1000]
    # mean = 550.0
    # P80: pos = 0.8*9 = 7.2; vals[7]=800, vals[8]=900 → 800 + 0.2*(900-800) = 820.0
    rows = [_row(1, 8, float(v), 2.0) for v in range(100, 1001, 100)]
    m = loadmodel.BucketedLoadModel.fit(rows)
    when = datetime(2026, 1, 5, 8, tzinfo=timezone.utc)  # Monday

    p50 = m.predict_load_w(when, temp=2.0, fallback_w=0.0, quantile=0.5)
    p80 = m.predict_load_w(when, temp=2.0, fallback_w=0.0, quantile=0.8)

    assert p50 == 550.0, f"P50 must be mean=550, got {p50}"
    assert p80 == 820.0, f"P80 must be 820.0 (interpolated), got {p80}"
    assert p80 >= p50, "Invariant: P80 >= P50"


def test_bucketed_p80_sparse_cell_falls_back_to_hourly():
    """Sparse cell (<8 samples) but well-populated hourly: upper quantile uses hourly samples."""
    # Cell (weekday, hour8, cold_bucket): 3 samples → too sparse for upper quantile
    # Hourly (weekday, hour8): 11 samples total → qualifies for upper quantile
    # We mix cold and warm temps so cell is sparse but hourly is not.
    rows = []
    # 3 samples at cold bucket (temp~2.0)
    for i in range(3):
        rows.append(_row(1 + i, 8, 100.0 + i * 100, 2.0))
    # 8 samples at warm bucket (temp~18.0) for same hour
    for i in range(8):
        rows.append(_row(4 + i, 8, 500.0 + i * 100, 18.0))
    m = loadmodel.BucketedLoadModel.fit(rows)
    when = datetime(2026, 1, 5, 8, tzinfo=timezone.utc)  # Monday (weekday)

    # Query at cold temp — cell has 3 samples (sparse), but hourly has 11
    p50_cold = m.predict_load_w(when, temp=2.0, fallback_w=0.0, quantile=0.5)
    p80_cold = m.predict_load_w(when, temp=2.0, fallback_w=0.0, quantile=0.8)

    # P50 should use the cold cell mean (normal hierarchy): (100+200+300)/3 = 200.0
    assert p50_cold == 200.0, f"P50 central must use cell mean=200, got {p50_cold}"
    # P80 upper quantile must fall back to hourly (11 samples), so P80 > mean
    assert p80_cold > p50_cold, "P80 must exceed P50 when hourly fallback gives upper quantile"


def test_bucketed_p80_all_sparse_falls_back_to_central():
    """All levels too sparse: P80 == P50 (no cushion when data is sparse)."""
    # Only 3 rows total → cell has 3, hourly has 3, global has 3 — all below _MIN_QUANTILE_SAMPLES=8
    rows = [_row(i + 1, 8, float(v), 2.0) for i, v in enumerate([100, 200, 300])]
    m = loadmodel.BucketedLoadModel.fit(rows)
    when = datetime(2026, 1, 5, 8, tzinfo=timezone.utc)  # Monday

    p50 = m.predict_load_w(when, temp=2.0, fallback_w=0.0, quantile=0.5)
    p80 = m.predict_load_w(when, temp=2.0, fallback_w=0.0, quantile=0.8)

    assert p50 == p80, f"With all levels sparse, P80 must equal P50={p50}, got P80={p80}"


def test_bucketed_p50_path_unchanged_regression():
    """Existing P50 behavior: quantile=0.5 returns mean (regression test)."""
    rows = [
        _row(15, 8, 1000.0, 2.0),
        _row(16, 8, 1200.0, 2.0),
    ]
    m = loadmodel.BucketedLoadModel.fit(rows)
    when = datetime(2026, 6, 22, 8, tzinfo=timezone.utc)
    result = m.predict_load_w(when, temp=2.0, fallback_w=0.0, quantile=0.5)
    assert result == 1100.0  # mean of 1000,1200


def test_empirical_quantile_q_clamped_to_one_returns_max():
    """q > 1.0 is clamped to 1.0, returning the maximum element."""
    from custom_components.anker_x1_smartgrid.loadmodel import _empirical_quantile
    assert _empirical_quantile([10.0, 20.0, 30.0], 1.5) == 30.0


def test_empirical_quantile_q_clamped_to_zero_returns_min():
    """q < 0.0 is clamped to 0.0, returning the minimum element."""
    from custom_components.anker_x1_smartgrid.loadmodel import _empirical_quantile
    assert _empirical_quantile([10.0, 20.0, 30.0], -0.5) == 10.0


def test_empirical_quantile_empty_raises():
    """Empty input raises ValueError."""
    import pytest
    from custom_components.anker_x1_smartgrid.loadmodel import _empirical_quantile
    with pytest.raises(ValueError, match="non-empty"):
        _empirical_quantile([], 0.8)
