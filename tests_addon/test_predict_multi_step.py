"""predict_hours serves the horizon as one multi-step chain, not hour-by-hour.

Only the first horizon hour can resolve ``load_lag_1h`` from a completed
rollup row; predicting each hour independently hands every later one NaN,
which the model never saw in training and answers with a near-constant (live
2026-08-24: a flat ~1.7 kW against a 471 W daily mean).  The endpoint must
therefore route through ``HGBRQuantileModel.predict_series``, where each
hour's own forecast becomes the next hour's lag.
"""

from __future__ import annotations

from datetime import datetime, timedelta, UTC

import pytest

from tests_addon._synthetic import make_hourly_rows
from forecast_core.const import DEFAULT_FALLBACK_LOAD_W
from forecast_core.hgbr import HGBRQuantileModel
from predictor import predict_hours

_TRAIN_DAYS = 28
_SYNTH_START = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def model_and_horizon_start() -> tuple[HGBRQuantileModel, datetime]:
    rows = make_hourly_rows(_TRAIN_DAYS, start=_SYNTH_START)
    model = HGBRQuantileModel().fit(rows, quantiles=(0.5, 0.8))
    assert model._fitted, "Fixture: model did not fit"
    return model, datetime.fromisoformat(rows[-1]["hour_ts"]) + timedelta(hours=1)


def _payload_hours(first: datetime, count: int = 24) -> list[dict]:
    return [
        {
            "ts": (first + timedelta(hours=i)).isoformat(),
            "temp_forecast": 10.0,
            "cloud_cover": 50.0,
            "humidity": 70.0,
            "wind_speed": 4.0,
            "persons_home": 2.0,
        }
        for i in range(count)
    ]


def _as_series_hours(hours: list[dict]) -> list[dict]:
    return [
        {
            "when": datetime.fromisoformat(h["ts"]),
            "temp": h["temp_forecast"],
            "cloud_cover": h["cloud_cover"],
            "humidity": h["humidity"],
            "wind_speed": h["wind_speed"],
            "persons_home": h["persons_home"],
        }
        for h in hours
    ]


def test_horizon_is_predicted_as_a_chain(model_and_horizon_start) -> None:
    model, first = model_and_horizon_start
    hours = _payload_hours(first)
    expected = model.predict_series(
        _as_series_hours(hours),
        DEFAULT_FALLBACK_LOAD_W,
        quantiles=(0.5, 0.8),
    )

    out = predict_hours(model, hours)

    assert [e["p50_w"] for e in out] == [round(p[0.5], 1) for p in expected]
    assert [e["p80_w"] for e in out] == [round(max(p[0.8], p[0.5]), 1) for p in expected]
