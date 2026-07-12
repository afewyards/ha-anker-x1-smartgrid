import math

import pytest

from custom_components.anker_x1_smartgrid.recorder import DataRecorder
from custom_components.anker_x1_smartgrid.rollup import aggregate_hour, _ROLLUP_FEATURES


def test_read_feature_rows_returns_dicts_ordered(tmp_path):
    rec = DataRecorder(str(tmp_path / "t.db"))
    rec.append({"ts": "2026-06-20T09:00:00+00:00", "p1_w": 600.0, "soc": 50.0})
    rec.append({"ts": "2026-06-20T08:00:00+00:00", "p1_w": 500.0, "soc": 49.0})
    rows = rec.read_feature_rows()
    assert [r["p1_w"] for r in rows] == [500.0, 600.0]  # ordered by ts asc
    assert rows[0]["ts"] == "2026-06-20T08:00:00+00:00"
    rec.close()


def test_read_feature_rows_since_filter(tmp_path):
    rec = DataRecorder(str(tmp_path / "t.db"))
    rec.append({"ts": "2026-06-01T08:00:00+00:00", "p1_w": 100.0})
    rec.append({"ts": "2026-06-20T08:00:00+00:00", "p1_w": 200.0})
    rows = rec.read_feature_rows(since_iso="2026-06-10T00:00:00+00:00")
    assert len(rows) == 1 and rows[0]["p1_w"] == 200.0
    rec.close()


# ---------------------------------------------------------------------------
# Pure aggregate_hour tests
# ---------------------------------------------------------------------------


def test_aggregate_hour_correct_stats():
    """Correct mean/max/min/std/count for a hand-computed two-sample set."""
    rows = [
        {"ts": "2026-06-20T14:00:00+00:00", "p1_w": 1000.0, "batt_w": 0.0, "pv_w": 500.0, "soc": 60.0},
        {"ts": "2026-06-20T14:30:00+00:00", "p1_w": 2000.0, "batt_w": 100.0, "pv_w": 300.0, "soc": 55.0},
    ]
    result = aggregate_hour(rows)

    # house_load: row1=1000+0+500=1500, row2=2000+100+300=2400; mean=1950
    # sample std: variance=((1500-1950)²+(2400-1950)²)/(2-1)=405000; std=sqrt(405000)
    assert result["hour_ts"] == "2026-06-20T14:00:00+00:00"
    assert result["house_load_mean"] == pytest.approx(1950.0)
    assert result["house_load_max"] == pytest.approx(2400.0)
    assert result["house_load_min"] == pytest.approx(1500.0)
    assert result["house_load_std"] == pytest.approx(math.sqrt(405000))
    assert result["house_load_count"] == 2

    # pv_w: [500, 300]; mean=400; sample std: variance=((500-400)²+(300-400)²)/1=20000
    assert result["pv_w_mean"] == pytest.approx(400.0)
    assert result["pv_w_max"] == pytest.approx(500.0)
    assert result["pv_w_min"] == pytest.approx(300.0)
    assert result["pv_w_std"] == pytest.approx(math.sqrt(20000))
    assert result["pv_w_count"] == 2

    # soc: [60, 55]; mean=57.5
    assert result["soc_mean"] == pytest.approx(57.5)
    assert result["soc_count"] == 2


def test_aggregate_hour_some_fields_null():
    """Non-NULL-only handling: stats computed only over non-NULL values per field."""
    rows = [
        {
            "ts": "2026-06-20T14:00:00+00:00",
            "p1_w": 1000.0,
            "batt_w": 0.0,
            "pv_w": 500.0,
            "soc": 60.0,
            "irradiance": 400.0,
        },
        {
            "ts": "2026-06-20T14:30:00+00:00",
            "p1_w": 2000.0,
            "batt_w": 0.0,
            "pv_w": None,
            "soc": None,
            "irradiance": None,
        },
    ]
    result = aggregate_hour(rows)

    # pv_w: only row1 (500) is non-NULL; count=1, std=0.0 (count<2 rule)
    assert result["pv_w_count"] == 1
    assert result["pv_w_mean"] == pytest.approx(500.0)
    assert result["pv_w_std"] == pytest.approx(0.0), "count<2 must yield std=0.0"

    # soc: only row1 (60) is non-NULL; count=1
    assert result["soc_count"] == 1
    assert result["soc_mean"] == pytest.approx(60.0)
    assert result["soc_std"] == pytest.approx(0.0)

    # irradiance: only row1 (400) is non-NULL; count=1
    assert result["irradiance_count"] == 1
    assert result["irradiance_mean"] == pytest.approx(400.0)

    # house_load: row1=1000+0+500=1500; row2=2000+0+0=2000 (pv NULL→0, both p1_w non-NULL)
    assert result["house_load_count"] == 2
    assert result["house_load_mean"] == pytest.approx(1750.0)


def test_aggregate_hour_all_null_field():
    """All-NULL field → mean/max/min/std = None, count = 0."""
    rows = [
        {"ts": "2026-06-20T14:00:00+00:00", "p1_w": 1000.0, "batt_w": 0.0, "irradiance": None},
        {"ts": "2026-06-20T14:30:00+00:00", "p1_w": 2000.0, "batt_w": 0.0, "irradiance": None},
    ]
    result = aggregate_hour(rows)

    assert result["irradiance_mean"] is None
    assert result["irradiance_max"] is None
    assert result["irradiance_min"] is None
    assert result["irradiance_std"] is None
    assert result["irradiance_count"] == 0


def test_aggregate_hour_count_lt2_std_is_zero():
    """count < 2: std must be 0.0 — not None and not a division-by-zero error."""
    rows = [
        {"ts": "2026-06-20T14:00:00+00:00", "p1_w": 1500.0, "batt_w": 0.0, "soc": 42.0},
    ]
    result = aggregate_hour(rows)

    assert result["house_load_count"] == 1
    assert result["house_load_std"] == pytest.approx(0.0)
    assert result["soc_count"] == 1
    assert result["soc_std"] == pytest.approx(0.0)


def test_aggregate_hour_house_load_pv_null_to_zero():
    """house_load derivation: pv_w NULL → treated as 0 (not excluded from load)."""
    rows = [
        # pv_w=None → pv treated as 0 → house_load = p1 + batt + 0
        {"ts": "2026-06-20T14:00:00+00:00", "p1_w": 1000.0, "batt_w": 200.0, "pv_w": None},
    ]
    result = aggregate_hour(rows)

    # house_load = 1000 + 200 + 0 = 1200
    assert result["house_load_mean"] == pytest.approx(1200.0)
    assert result["house_load_count"] == 1


def test_aggregate_hour_house_load_excluded_when_p1_null():
    """house_load: p1_w NULL → derive_house_load_w returns None → excluded from stats."""
    rows = [
        {"ts": "2026-06-20T14:00:00+00:00", "p1_w": None, "batt_w": 200.0, "pv_w": 300.0},
        {"ts": "2026-06-20T14:30:00+00:00", "p1_w": 2000.0, "batt_w": 0.0, "pv_w": 0.0},
    ]
    result = aggregate_hour(rows)

    # Only row2 has valid house_load: 2000+0+0=2000
    assert result["house_load_count"] == 1
    assert result["house_load_mean"] == pytest.approx(2000.0)


def test_aggregate_hour_hour_ts_truncated_to_clock_hour():
    """hour_ts is derived from the first row's ts, truncated to the UTC clock-hour."""
    rows = [
        {"ts": "2026-06-20T14:37:22+00:00", "p1_w": 1000.0, "batt_w": 0.0},
    ]
    result = aggregate_hour(rows)
    assert result["hour_ts"] == "2026-06-20T14:00:00+00:00"


def test_aggregate_hour_contains_all_expected_keys():
    """Result dict must contain hour_ts + 5 stats for every feature in _ROLLUP_FEATURES."""
    rows = [{"ts": "2026-06-20T14:00:00+00:00", "p1_w": 1000.0, "batt_w": 0.0}]
    result = aggregate_hour(rows)

    assert "hour_ts" in result
    for feature in _ROLLUP_FEATURES:
        for stat in ("mean", "max", "min", "std", "count"):
            key = f"{feature}_{stat}"
            assert key in result, f"missing key: {key}"


def test_aggregate_hour_persons_home_mean():
    """persons_home rolls up like any other feature: mean/count over non-NULL values."""
    rows = [
        {"ts": "2026-07-01T14:00:00+00:00", "p1_w": 100.0, "batt_w": 0.0, "persons_home": 2.0},
        {"ts": "2026-07-01T14:00:30+00:00", "p1_w": 100.0, "batt_w": 0.0, "persons_home": 1.0},
    ]
    result = aggregate_hour(rows)
    assert result["persons_home_mean"] == 1.5
    assert result["persons_home_count"] == 2


def test_aggregate_hour_all_weather_forecast_null():
    """Weather forecast columns all NULL → count=0, mean/max/min/std=None per field."""
    rows = [
        {"ts": "2026-06-20T14:00:00+00:00", "p1_w": 1000.0, "batt_w": 0.0},
    ]
    result = aggregate_hour(rows)

    for feature in ("temp_forecast", "cloud_cover", "humidity", "wind_speed"):
        assert result[f"{feature}_count"] == 0, f"{feature}_count must be 0 when all NULL"
        assert result[f"{feature}_mean"] is None
        assert result[f"{feature}_std"] is None


def test_aggregate_hour_raises_on_empty_rows():
    """aggregate_hour must raise ValueError when rows is empty."""
    with pytest.raises(ValueError, match="requires at least one row"):
        aggregate_hour([])


def test_aggregate_hour_single_row_has_equal_max_min():
    """With a single row, max and min must equal the single value."""
    rows = [
        {"ts": "2026-06-20T14:00:00+00:00", "p1_w": 1234.0, "batt_w": 56.0, "pv_w": 78.0},
    ]
    result = aggregate_hour(rows)

    # house_load = 1234+56+78 = 1368 (fallback path: no load_w)
    assert result["house_load_max"] == pytest.approx(result["house_load_min"])
    assert result["house_load_max"] == pytest.approx(1368.0)


# ---------------------------------------------------------------------------
# T6 — aggregate_hour prefers recorded load_w, falls back to derive
# ---------------------------------------------------------------------------


def test_aggregate_hour_prefers_load_w_over_derive():
    """Row with load_w=200 and p1+batt+pv=900 → house_load_mean=200 (load_w wins)."""
    rows = [
        {
            "ts": "2026-06-24T10:00:00+00:00",
            "p1_w": 300.0,
            "batt_w": 400.0,
            "pv_w": 200.0,  # p1+batt+pv = 900, but load_w=200 overrides
            "load_w": 200.0,
        }
    ]
    result = aggregate_hour(rows)
    assert result["house_load_mean"] == pytest.approx(200.0)
    assert result["house_load_count"] == 1


def test_aggregate_hour_falls_back_to_derive_when_load_w_null():
    """Row with load_w=NULL → derive_house_load_w used as fallback."""
    rows = [
        {
            "ts": "2026-06-24T10:00:00+00:00",
            "p1_w": 300.0,
            "batt_w": 100.0,
            "pv_w": 50.0,
            "load_w": None,  # null → fallback
        }
    ]
    result = aggregate_hour(rows)
    # fallback: 300 + 100 + 50 = 450
    assert result["house_load_mean"] == pytest.approx(450.0)
    assert result["house_load_count"] == 1


def test_aggregate_hour_excludes_row_when_both_load_w_and_p1_null():
    """When load_w=NULL and p1_w=NULL, row contributes no house_load (excluded)."""
    rows = [
        {
            "ts": "2026-06-24T10:00:00+00:00",
            "p1_w": None,
            "batt_w": 100.0,
            "pv_w": 50.0,
            "load_w": None,  # both null → excluded from house_load stats
        }
    ]
    result = aggregate_hour(rows)
    assert result["house_load_count"] == 0
    assert result["house_load_mean"] is None


def test_aggregate_hour_mixed_load_w_prefers_recorded_skips_null():
    """Mixed: row with load_w uses it; row without load_w falls back to derive."""
    rows = [
        {
            "ts": "2026-06-24T10:00:00+00:00",
            "p1_w": 300.0,
            "batt_w": 0.0,
            "pv_w": 0.0,
            "load_w": 200.0,  # recorded → use 200
        },
        {
            "ts": "2026-06-24T10:15:00+00:00",
            "p1_w": 400.0,
            "batt_w": 0.0,
            "pv_w": 0.0,
            "load_w": None,  # null → fallback: 400+0+0=400
        },
    ]
    result = aggregate_hour(rows)
    # values: [200, 400]; mean=300
    assert result["house_load_count"] == 2
    assert result["house_load_mean"] == pytest.approx(300.0)


# ---------------------------------------------------------------------------
# T6 — read_efficiency_samples: v6+ load_w residual accessor
# ---------------------------------------------------------------------------


def test_read_efficiency_samples_filters_and_computes_residual(tmp_path):
    rec = DataRecorder(str(tmp_path / "t.db"))
    rec.append(
        {"ts": "2026-07-01T00:00:00", "soc": 50.0, "batt_w": -3000.0, "p1_w": 200.0, "pv_w": 100.0, "load_w": -2700.0}
    )
    rec.append(
        {"ts": "2026-07-01T00:01:00", "soc": 51.0, "batt_w": -3000.0, "p1_w": 200.0, "pv_w": 100.0, "load_w": None}
    )
    rec.append(
        {"ts": "2026-07-01T00:02:00", "soc": 52.0, "batt_w": -3000.0, "p1_w": 200.0, "pv_w": None, "load_w": -2800.0}
    )
    out = rec.read_efficiency_samples()
    assert [r["ts"] for r in out] == ["2026-07-01T00:00:00+00:00", "2026-07-01T00:02:00+00:00"]
    assert out[0]["residual_w"] == -2700.0 - 200.0 - 100.0
    assert out[1]["residual_w"] == -2800.0 - 200.0 - 0.0
    rec.close()


def test_read_efficiency_samples_since_filter(tmp_path):
    rec = DataRecorder(str(tmp_path / "t.db"))
    for h in range(3):
        rec.append(
            {
                "ts": f"2026-07-0{h + 1}T00:00:00",
                "soc": 50.0,
                "batt_w": -3000.0,
                "p1_w": 0.0,
                "pv_w": 0.0,
                "load_w": -3000.0,
            }
        )
    out = rec.read_efficiency_samples(since_iso="2026-07-02T00:00:00")
    assert [r["ts"] for r in out] == ["2026-07-02T00:00:00+00:00", "2026-07-03T00:00:00+00:00"]
    rec.close()
