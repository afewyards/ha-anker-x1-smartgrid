"""Tests for predictor.predict_hours (P2-T1).

Covers:
- Fitted model returns one entry per tz-aware hour with ts/p50_w/p80_w.
- p80_w >= p50_w for a fitted model (including clamp for crossing quantiles).
- Naive-ts hours are omitted from the result.
- Result order mirrors input order (minus omitted hours).
- Unparseable ts hours are omitted.
- Empty input → empty output.

Serve-path faithfulness (P2-T2)
-------------------------------
Deep feature-vector train==serve equality is guarded by:
- tests_addon/test_vendor_parity.py (forecast_core is byte-identical to integration)
- tests/test_hgbr.py::test_train_predict_consistency

This module tests that the wrapper predict_hours() faithfully passes (ts, temp_forecast)
to the model without mangling inputs, ensuring its result equals a direct model call.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests_addon._synthetic import make_hourly_rows
from forecast_core.const import DEFAULT_FALLBACK_LOAD_W
from forecast_core.hgbr import HGBRQuantileModel
from predictor import predict_hours

# ---------------------------------------------------------------------------
# Shared fixture: model fitted on 28 days of synthetic data
# ---------------------------------------------------------------------------

_SYNTH_START = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
_SYNTH_END = _SYNTH_START + timedelta(days=28)  # exclusive


@pytest.fixture(scope="module")
def fitted_model() -> HGBRQuantileModel:
    rows = make_hourly_rows(28)
    model = HGBRQuantileModel()
    model.fit(rows, quantiles=(0.5, 0.8))
    assert model._fitted, "Fixture: model did not fit — synthetic data insufficient?"
    return model


# ---------------------------------------------------------------------------
# Helper: build tz-aware future hours just past the synthetic data window
# ---------------------------------------------------------------------------

def _future_hours(n: int, *, temp: float = 10.0) -> list[dict]:
    """Return n hour dicts starting just after the synthetic training window."""
    result = []
    for i in range(n):
        ts = _SYNTH_END + timedelta(hours=i)
        result.append({"ts": ts.isoformat(), "temp_forecast": temp})
    return result


# ---------------------------------------------------------------------------
# Core: fitted model produces correct output shape
# ---------------------------------------------------------------------------


def test_returns_one_entry_per_valid_hour(fitted_model):
    hours = _future_hours(3)
    out = predict_hours(fitted_model, hours)
    assert len(out) == 3


def test_output_keys(fitted_model):
    hours = _future_hours(1)
    out = predict_hours(fitted_model, hours)
    assert set(out[0].keys()) == {"ts", "p50_w", "p80_w"}


def test_ts_matches_input(fitted_model):
    hours = _future_hours(2)
    out = predict_hours(fitted_model, hours)
    assert out[0]["ts"] == hours[0]["ts"]
    assert out[1]["ts"] == hours[1]["ts"]


def test_p50_and_p80_are_floats(fitted_model):
    out = predict_hours(fitted_model, _future_hours(1))
    assert isinstance(out[0]["p50_w"], float)
    assert isinstance(out[0]["p80_w"], float)


def test_p80_ge_p50(fitted_model):
    """P80 quantile must be >= P50 for every predicted hour."""
    out = predict_hours(fitted_model, _future_hours(5, temp=8.0))
    for entry in out:
        assert entry["p80_w"] >= entry["p50_w"], (
            f"p80_w={entry['p80_w']} < p50_w={entry['p50_w']} for ts={entry['ts']}"
        )


def test_monotonicity_clamp_via_mock(fitted_model):
    """When raw quantile predictions cross, the clamp enforces p80_w >= p50_w.

    We force a crossing by monkeypatching predict_load_w to return p50=700 and
    p80=650 (a crossing).  predict_hours must clamp p80 up to 700.
    """
    from unittest.mock import patch

    ts = (_SYNTH_END + timedelta(hours=0)).isoformat()
    hour = {"ts": ts, "temp_forecast": 10.0}

    call_count = 0

    def _mock_predict(when, temp, fallback_w, *, quantile=0.5,
                       cloud_cover=None, humidity=None, wind_speed=None, persons_home=None):
        nonlocal call_count
        call_count += 1
        # First call: quantile=0.5 → 700.0; second: quantile=0.8 → 650.0 (crossing)
        return 700.0 if quantile == 0.5 else 650.0

    with patch.object(fitted_model, "predict_load_w", side_effect=_mock_predict):
        out = predict_hours(fitted_model, [hour])

    assert call_count == 2, "predict_load_w should be called twice (p50 and p80)"
    assert len(out) == 1
    assert out[0]["p50_w"] == 700.0
    assert out[0]["p80_w"] == 700.0, (
        f"Expected clamp to 700.0, got {out[0]['p80_w']}"
    )


def test_p80_ge_p50_across_many_hours(fitted_model):
    """Invariant holds across a broad sweep of hours (catches any real crossing)."""
    hours = _future_hours(24, temp=12.0)
    out = predict_hours(fitted_model, hours)
    for entry in out:
        assert entry["p80_w"] >= entry["p50_w"], (
            f"p80_w={entry['p80_w']} < p50_w={entry['p50_w']} for ts={entry['ts']}"
        )


# ---------------------------------------------------------------------------
# Naive-ts omission
# ---------------------------------------------------------------------------


def test_naive_ts_is_omitted(fitted_model):
    naive_hour = {"ts": "2024-02-01T14:00:00", "temp_forecast": 10.0}  # no +00:00
    aware_hour = {"ts": "2024-02-01T15:00:00+00:00", "temp_forecast": 10.0}

    out = predict_hours(fitted_model, [naive_hour, aware_hour])

    assert len(out) == 1, "Only the tz-aware hour should be returned"
    assert out[0]["ts"] == aware_hour["ts"]


# ---------------------------------------------------------------------------
# Order preservation
# ---------------------------------------------------------------------------


def test_order_preserved(fitted_model):
    hours = _future_hours(4)
    out = predict_hours(fitted_model, hours)
    assert [e["ts"] for e in out] == [h["ts"] for h in hours]


def test_order_preserved_with_naive_omitted(fitted_model):
    h0 = {"ts": (_SYNTH_END + timedelta(hours=0)).isoformat(), "temp_forecast": 9.0}
    h1_naive = {"ts": "2024-03-01T00:00:00", "temp_forecast": 9.0}  # naive → omit
    h2 = {"ts": (_SYNTH_END + timedelta(hours=2)).isoformat(), "temp_forecast": 9.0}

    out = predict_hours(fitted_model, [h0, h1_naive, h2])

    assert len(out) == 2
    assert out[0]["ts"] == h0["ts"]
    assert out[1]["ts"] == h2["ts"]


# ---------------------------------------------------------------------------
# Unparseable ts omission
# ---------------------------------------------------------------------------


def test_unparseable_ts_is_omitted(fitted_model):
    bad = {"ts": "not-a-date", "temp_forecast": 10.0}
    good = {"ts": (_SYNTH_END).isoformat(), "temp_forecast": 10.0}
    out = predict_hours(fitted_model, [bad, good])
    assert len(out) == 1
    assert out[0]["ts"] == good["ts"]


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_input(fitted_model):
    assert predict_hours(fitted_model, []) == []


def test_missing_ts_key_is_omitted(fitted_model):
    no_ts = {"temp_forecast": 10.0}
    good = {"ts": _SYNTH_END.isoformat(), "temp_forecast": 10.0}
    out = predict_hours(fitted_model, [no_ts, good])
    assert len(out) == 1


def test_none_temp_forecast_is_accepted(fitted_model):
    """None temp is valid (model uses NaN path); should not raise or omit."""
    hour = {"ts": _SYNTH_END.isoformat(), "temp_forecast": None}
    out = predict_hours(fitted_model, [hour])
    assert len(out) == 1
    assert isinstance(out[0]["p50_w"], float)


def test_predict_hours_forwards_weather(fitted_model):
    """predict_hours passes cloud_cover/humidity/wind_speed into predict_load_w for both quantiles."""
    from unittest.mock import patch
    captured = []

    def _cap(when, temp, fallback_w, *, quantile=0.5,
             cloud_cover=None, humidity=None, wind_speed=None, persons_home=None):
        captured.append((cloud_cover, humidity, wind_speed))
        return 500.0

    hour = {"ts": _SYNTH_END.isoformat(), "temp_forecast": 10.0,
            "cloud_cover": 55.0, "humidity": 70.0, "wind_speed": 3.3}
    with patch.object(fitted_model, "predict_load_w", side_effect=_cap):
        out = predict_hours(fitted_model, [hour])
    assert len(out) == 1
    assert len(captured) == 2  # p50 + p80
    assert all(c == (55.0, 70.0, 3.3) for c in captured)


def test_predict_hours_forwards_persons_home(fitted_model):
    """predict_hours passes persons_home into predict_load_w for both quantiles."""
    from unittest.mock import patch
    captured = []

    def _cap(when, temp, fallback_w, *, quantile=0.5,
             cloud_cover=None, humidity=None, wind_speed=None, persons_home=None):
        captured.append(persons_home)
        return 500.0

    hour = {"ts": _SYNTH_END.isoformat(), "temp_forecast": 10.0, "persons_home": 3.0}
    with patch.object(fitted_model, "predict_load_w", side_effect=_cap):
        out = predict_hours(fitted_model, [hour])
    assert len(out) == 1
    assert captured == [3.0, 3.0]  # p50 + p80


# ---------------------------------------------------------------------------
# Serve-path faithfulness: wrapper passes (ts, temp) without mangling
# ---------------------------------------------------------------------------


def test_wrapper_faithfulness_to_direct_model_call(fitted_model):
    """Verify predict_hours() result equals a direct model.predict_load_w() call.

    This test ensures the wrapper faithfully passes (ts, temp_forecast) inputs
    to the model without mangling. The test:
    1. Picks a ts inside the training span (lags resolve).
    2. Calls model.predict_load_w() directly for p50 and p80.
    3. Calls predict_hours() wrapper with the same ts/temp.
    4. Asserts wrapper result == direct result (accounting for rounding and clamp).

    Deep feature parity is covered separately by test_vendor_parity and
    test_train_predict_consistency; this test focuses on serve-path correctness.
    """
    # Pick a ts in the middle of the training window so lags resolve
    ts_dt = _SYNTH_START + timedelta(days=14)
    ts_iso = ts_dt.isoformat()
    temp = 12.5

    # Direct model calls (compute before clamp)
    exp_p50 = fitted_model.predict_load_w(
        ts_dt,
        temp,
        DEFAULT_FALLBACK_LOAD_W,
        quantile=0.5,
    )
    exp_p80 = fitted_model.predict_load_w(
        ts_dt,
        temp,
        DEFAULT_FALLBACK_LOAD_W,
        quantile=0.8,
    )

    # Apply the same clamp and rounding that predict_hours applies
    exp_p80_clamped = max(exp_p50, exp_p80)
    exp_p50_rounded = round(exp_p50, 1)
    exp_p80_rounded = round(exp_p80_clamped, 1)

    # Call wrapper
    out = predict_hours(fitted_model, [{"ts": ts_iso, "temp_forecast": temp}])

    # Assert wrapper result matches direct computation
    assert len(out) == 1, "Expected one result for one input hour"
    assert out[0]["ts"] == ts_iso, "ts should be preserved"
    assert out[0]["p50_w"] == exp_p50_rounded, (
        f"Wrapper p50_w={out[0]['p50_w']} != direct p50={exp_p50_rounded}"
    )
    assert out[0]["p80_w"] == exp_p80_rounded, (
        f"Wrapper p80_w={out[0]['p80_w']} != direct p80={exp_p80_rounded}"
    )
