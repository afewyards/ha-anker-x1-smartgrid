"""Synthetic data helpers for tests.

DB-write path: direct INSERT OR REPLACE INTO samples_hourly (bypasses the
samples table and rollup_hours path). This is valid because read_hourly_rows()
reads samples_hourly directly, and featureset/hgbr only consume samples_hourly
rows.
"""
from __future__ import annotations

import math
import random
import sqlite3
from datetime import datetime, timedelta, timezone

from forecast_core.recorder import DataRecorder

_HOURLY_COLUMNS = [
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


def make_hourly_rows(
    days: int,
    *,
    start: datetime | None = None,
    seed: int = 0,
    weekly: bool = True,
) -> list[dict]:
    """Return days * 24 row dicts whose keys exactly match DataRecorder.read_hourly_rows()."""
    if start is None:
        start = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)

    rng = random.Random(seed)
    rows: list[dict] = []

    for i in range(days * 24):
        dt = start + timedelta(hours=i)
        h = dt.hour
        weekday = dt.weekday()  # 0=Monday, 5=Saturday, 6=Sunday

        hour_ts = dt.isoformat()

        # house_load
        base = 400.0 + 700.0 * math.sin(math.pi * h / 23)
        if weekly and weekday >= 5:
            base *= 0.85
        base += rng.uniform(-80, 80)
        house_load_mean = max(50.0, base)
        house_load_max = house_load_mean * 1.15
        house_load_min = house_load_mean * 0.85
        house_load_std = 60.0
        house_load_count = 60

        # pv_w
        if 6 <= h <= 18:
            pv_raw = 3500.0 * math.sin(math.pi * (h - 6) / 12)
        else:
            pv_raw = 0.0
        pv_raw += rng.uniform(-50, 50)
        pv_w_mean = max(0.0, pv_raw)
        pv_w_max = pv_w_mean * 1.2
        pv_w_min = pv_w_mean * 0.8
        pv_w_std = 80.0
        pv_w_count = 60

        # soc
        soc_raw = 40.0 + 30.0 * math.sin(math.pi * h / 23) + rng.uniform(-5, 5)
        soc_mean = max(5.0, min(95.0, soc_raw))
        soc_max = min(100.0, soc_mean + 5)
        soc_min = max(0.0, soc_mean - 5)
        soc_std = 3.0
        soc_count = 60

        # irradiance
        if 6 <= h <= 18:
            irr_raw = 800.0 * math.sin(math.pi * (h - 6) / 12)
        else:
            irr_raw = 0.0
        irr_raw += rng.uniform(-20, 20)
        irradiance_mean = max(0.0, irr_raw)
        irradiance_max = irradiance_mean * 1.1
        irradiance_min = irradiance_mean * 0.9
        irradiance_std = 30.0
        irradiance_count = 60

        # temp
        temp_mean = 10.0 + 5.0 * math.sin(math.pi * h / 23) + rng.uniform(-2, 2)
        temp_max = temp_mean + 1.5
        temp_min = temp_mean - 1.5
        temp_std = 0.8
        temp_count = 60

        # temp_forecast
        temp_forecast_mean = temp_mean + rng.uniform(-1, 1)
        temp_forecast_max = temp_forecast_mean + 1.5
        temp_forecast_min = temp_forecast_mean - 1.5
        temp_forecast_std = 0.8
        temp_forecast_count = 60

        # cloud_cover
        cloud_raw = 0.4 + rng.uniform(-0.2, 0.2)
        cloud_cover_mean = max(0.0, min(1.0, cloud_raw))
        cloud_cover_max = min(1.0, cloud_cover_mean + 0.1)
        cloud_cover_min = max(0.0, cloud_cover_mean - 0.1)
        cloud_cover_std = 0.05
        cloud_cover_count = 60

        # humidity
        hum_raw = 60.0 + rng.uniform(-10, 10)
        humidity_mean = max(0.0, min(100.0, hum_raw))
        humidity_max = humidity_mean + 5
        humidity_min = humidity_mean - 5
        humidity_std = 3.0
        humidity_count = 60

        # wind_speed
        wind_raw = 3.0 + rng.uniform(-1, 1)
        wind_speed_mean = max(0.0, wind_raw)
        wind_speed_max = wind_speed_mean + 1
        wind_speed_min = max(0.0, wind_speed_mean - 1)
        wind_speed_std = 0.5
        wind_speed_count = 60

        # persons_home: count of person.* entities in state 'home' (v8)
        ph_raw = 1.5 + rng.uniform(-0.5, 0.5)
        persons_home_mean = max(0.0, ph_raw)
        persons_home_max = min(4.0, persons_home_mean + 1.0)
        persons_home_min = max(0.0, persons_home_mean - 1.0)
        persons_home_std = 0.5
        persons_home_count = 60

        # kWh sums: convert power means to hourly kWh (tier 2 approximation: mean_watts×1h/1000)
        house_load_kwh_sum = house_load_mean / 1000.0
        pv_kwh_sum = pv_w_mean / 1000.0
        # battery: simple charge/discharge split based on soc trend
        # (grid import/export are synthetic; not modeled from actual tariffs)
        batt_charge_kwh_sum = max(0.0, (soc_mean - 40.0) / 100.0) * 0.5
        batt_discharge_kwh_sum = max(0.0, (40.0 - soc_mean) / 100.0) * 0.5
        grid_import_kwh_sum = max(0.0, (house_load_kwh_sum - pv_kwh_sum) * 0.8)
        grid_export_kwh_sum = max(0.0, (pv_kwh_sum - house_load_kwh_sum) * 0.8)

        row = {
            "hour_ts": hour_ts,
            "house_load_mean": house_load_mean,
            "house_load_max": house_load_max,
            "house_load_min": house_load_min,
            "house_load_std": house_load_std,
            "house_load_count": house_load_count,
            "pv_w_mean": pv_w_mean,
            "pv_w_max": pv_w_max,
            "pv_w_min": pv_w_min,
            "pv_w_std": pv_w_std,
            "pv_w_count": pv_w_count,
            "soc_mean": soc_mean,
            "soc_max": soc_max,
            "soc_min": soc_min,
            "soc_std": soc_std,
            "soc_count": soc_count,
            "irradiance_mean": irradiance_mean,
            "irradiance_max": irradiance_max,
            "irradiance_min": irradiance_min,
            "irradiance_std": irradiance_std,
            "irradiance_count": irradiance_count,
            "temp_mean": temp_mean,
            "temp_max": temp_max,
            "temp_min": temp_min,
            "temp_std": temp_std,
            "temp_count": temp_count,
            "temp_forecast_mean": temp_forecast_mean,
            "temp_forecast_max": temp_forecast_max,
            "temp_forecast_min": temp_forecast_min,
            "temp_forecast_std": temp_forecast_std,
            "temp_forecast_count": temp_forecast_count,
            "cloud_cover_mean": cloud_cover_mean,
            "cloud_cover_max": cloud_cover_max,
            "cloud_cover_min": cloud_cover_min,
            "cloud_cover_std": cloud_cover_std,
            "cloud_cover_count": cloud_cover_count,
            "humidity_mean": humidity_mean,
            "humidity_max": humidity_max,
            "humidity_min": humidity_min,
            "humidity_std": humidity_std,
            "humidity_count": humidity_count,
            "wind_speed_mean": wind_speed_mean,
            "wind_speed_max": wind_speed_max,
            "wind_speed_min": wind_speed_min,
            "wind_speed_std": wind_speed_std,
            "wind_speed_count": wind_speed_count,
            "persons_home_mean": persons_home_mean,
            "persons_home_max": persons_home_max,
            "persons_home_min": persons_home_min,
            "persons_home_std": persons_home_std,
            "persons_home_count": persons_home_count,
            "grid_import_kwh_sum": grid_import_kwh_sum,
            "grid_export_kwh_sum": grid_export_kwh_sum,
            "house_load_kwh_sum": house_load_kwh_sum,
            "pv_kwh_sum": pv_kwh_sum,
            "batt_charge_kwh_sum": batt_charge_kwh_sum,
            "batt_discharge_kwh_sum": batt_discharge_kwh_sum,
        }
        rows.append(row)

    return rows


def make_samples_hourly_db(path: str, days: int, **kw) -> None:
    """Create a DB at path with samples_hourly populated from make_hourly_rows.

    Uses DataRecorder to create/migrate the schema, then inserts rows directly
    into samples_hourly via INSERT OR REPLACE (bypassing the samples rollup path).
    """
    # Run migration to ensure schema + user_version are correct.
    rec = DataRecorder(path)
    rec.close()

    rows = make_hourly_rows(days, **kw)

    cols = ", ".join(_HOURLY_COLUMNS)
    placeholders = ", ".join("?" for _ in _HOURLY_COLUMNS)
    sql = f"INSERT OR REPLACE INTO samples_hourly ({cols}) VALUES ({placeholders})"

    conn = sqlite3.connect(path)
    try:
        for row in rows:
            values = [row[col] for col in _HOURLY_COLUMNS]
            conn.execute(sql, values)
        conn.commit()
    finally:
        conn.close()
