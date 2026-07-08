"""Self-tests for the _synthetic helpers."""
import pytest
from datetime import timezone
from tests_addon._synthetic import make_hourly_rows, make_samples_hourly_db
from forecast_core.recorder import DataRecorder
from forecast_core import featureset

REQUIRED_KEYS = [
    "hour_ts",
    "house_load_mean", "house_load_max", "house_load_min", "house_load_std", "house_load_count",
    "pv_w_mean", "pv_w_max", "pv_w_min", "pv_w_std", "pv_w_count",
    "soc_mean", "soc_max", "soc_min", "soc_std", "soc_count",
    "irradiance_mean", "irradiance_max", "irradiance_min", "irradiance_std", "irradiance_count",
    "temp_mean", "temp_max", "temp_min", "temp_std", "temp_count",
    "temp_forecast_mean", "temp_forecast_max", "temp_forecast_min", "temp_forecast_std", "temp_forecast_count",
    "cloud_cover_mean", "cloud_cover_max", "cloud_cover_min", "cloud_cover_std", "cloud_cover_count",
    "humidity_mean", "humidity_max", "humidity_min", "humidity_std", "humidity_count",
    "wind_speed_mean", "wind_speed_max", "wind_speed_min", "wind_speed_std", "wind_speed_count",
    "persons_home_mean", "persons_home_max", "persons_home_min", "persons_home_std", "persons_home_count",
    "grid_import_kwh_sum", "grid_export_kwh_sum", "house_load_kwh_sum",
    "pv_kwh_sum", "batt_charge_kwh_sum", "batt_discharge_kwh_sum",
]


def test_make_hourly_rows_count_and_keys():
    rows = make_hourly_rows(28)
    assert len(rows) == 28 * 24
    assert set(rows[0].keys()) == set(REQUIRED_KEYS)
    assert all(set(r.keys()) == set(REQUIRED_KEYS) for r in rows)


def test_make_hourly_rows_too_few_days():
    rows = make_hourly_rows(5)
    assert len(rows) == 5 * 24  # sanity, not is_ready check — that's for T5


def test_make_samples_hourly_db_roundtrip(tmp_path):
    path = str(tmp_path / "x.db")
    make_samples_hourly_db(path, 28)
    rec = DataRecorder(path)
    db_rows = rec.read_hourly_rows()
    assert len(db_rows) == 28 * 24
    assert set(db_rows[0].keys()) == set(REQUIRED_KEYS)


def test_build_feature_matrix_accepts_rows():
    rows = make_hourly_rows(28)
    X, y, index = featureset.build_feature_matrix(rows)
    assert len(X) > 0
    assert len(y) == len(X)
    assert len(index) == len(X)


def test_deterministic():
    rows_a = make_hourly_rows(7, seed=42)
    rows_b = make_hourly_rows(7, seed=42)
    assert rows_a[0]["house_load_mean"] == rows_b[0]["house_load_mean"]
    assert rows_a[100]["pv_w_mean"] == rows_b[100]["pv_w_mean"]
