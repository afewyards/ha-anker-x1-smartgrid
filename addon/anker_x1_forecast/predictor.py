"""FastAPI-free predict helper for the HGBR load forecasting model.

Provides ``predict_hours`` — a pure function that maps a list of future-hour
dicts to p50/p80 load predictions without touching the DB, training pipeline,
or any web framework.

Also provides ``build_predict_payload`` — a pure function that assembles the
``/predict`` response body from a TrainState and a predictions list, mirroring
the ``build_health_payload`` pattern in ``health.py``.

Usage::

    from forecast_core.hgbr import HGBRQuantileModel
    from predictor import predict_hours, build_predict_payload

    model = HGBRQuantileModel().fit(hourly_rows, quantiles=(0.5, 0.8))
    results = predict_hours(model, [
        {"ts": "2024-02-01T14:00:00+00:00", "temp_forecast": 8.5},
        {"ts": "2024-02-01T15:00:00+00:00", "temp_forecast": 7.0},
    ])
    # [{"ts": "2024-02-01T14:00:00+00:00", "p50_w": 820.0, "p80_w": 1040.0}, ...]
"""

from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import TYPE_CHECKING

from forecast_core.const import DEFAULT_FALLBACK_LOAD_W
from forecast_core.hgbr import HGBRQuantileModel

if TYPE_CHECKING:
    from trainer import TrainState

_log = logging.getLogger(__name__)


def predict_hours(
    model: HGBRQuantileModel,
    future_hours: list[dict],
) -> list[dict]:
    """Predict p50/p80 load (W) for each future hour.

    Parameters
    ----------
    model:
        A fitted :class:`~forecast_core.hgbr.HGBRQuantileModel`.  The
        function is safe to call with an unfitted model — the model's
        own fallback path is used and results will equal the fallback
        constant for every hour.
    future_hours:
        Ordered list of hour dicts.  Each dict MUST contain ``ts`` (an
        ISO-8601 string) and MAY contain ``temp_forecast``, ``cloud_cover``,
        ``humidity``, ``wind_speed`` — all four are forwarded to the model.
        ``irradiance`` has no serve-time source and is not read here; the
        model always NaNs it internally.

    Returns
    -------
    List of ``{"ts": <original string>, "p50_w": float, "p80_w": float}``
    dicts, in the same order as ``future_hours``, minus any hours that
    were omitted (naive timestamps, unparseable timestamps, or per-hour
    errors).  Never raises.

    Omit rules
    ----------
    - ``ts`` missing or unparseable → omit + log.
    - ``ts`` parses but has no tzinfo (naive) → omit + log.
    - Either prediction is non-finite (NaN/Inf) → omit + log.
    - Any other per-hour exception → omit + log.

    Monotonicity
    ------------
    The P50 and P80 estimators are trained independently, so the raw
    quantile predictions can cross.  After calling the model for both
    quantiles, ``p80`` is clamped to ``max(p50, p80)``.  Biasing upward
    is safe: P80 is the deficit-cushion quantile, so a larger value is
    conservative rather than aggressive.

    Multi-step
    ----------
    The accepted hours are predicted as ONE chain
    (``HGBRQuantileModel.predict_series``), not independently.  Only the
    first horizon hour can resolve ``load_lag_1h`` from a completed rollup
    row; predicting hour by hour hands every later one NaN — a region the
    training rows never occupy — and the forecast collapses to a near
    constant (live 2026-08-24: a flat ~1.7 kW against a 471 W daily mean).
    """
    accepted: list[tuple[str, dict]] = []

    for hour in future_hours:
        try:
            ts_raw: str | None = hour.get("ts")
            if not ts_raw:
                _log.warning("predict_hours: hour missing 'ts' key, skipping: %r", hour)
                continue

            try:
                ts_dt = datetime.fromisoformat(ts_raw)
            except (ValueError, TypeError) as exc:
                _log.warning("predict_hours: unparseable 'ts' %r — %s, skipping", ts_raw, exc)
                continue

            if ts_dt.tzinfo is None:
                _log.warning("predict_hours: naive ts %r (no tzinfo), skipping", ts_raw)
                continue

            accepted.append(
                (
                    ts_raw,
                    {
                        "when": ts_dt,
                        "temp": hour.get("temp_forecast"),
                        "cloud_cover": hour.get("cloud_cover"),
                        "humidity": hour.get("humidity"),
                        "wind_speed": hour.get("wind_speed"),
                        "persons_home": hour.get("persons_home"),
                    },
                )
            )
        except Exception:
            _log.exception("predict_hours: unexpected error for hour %r, skipping", hour)

    if not accepted:
        return []

    try:
        series = model.predict_series(
            [entry for _, entry in accepted],
            DEFAULT_FALLBACK_LOAD_W,
            quantiles=(0.5, 0.8),
        )
    except Exception:
        # Never raise: an empty body reads as "no forecast" and the caller
        # falls back to its local tier.
        _log.exception("predict_hours: multi-step prediction failed, returning no hours")
        return []

    results: list[dict] = []
    for (ts_raw, _), preds in zip(accepted, series, strict=True):
        p50 = float(preds.get(0.5, DEFAULT_FALLBACK_LOAD_W))
        p80 = float(preds.get(0.8, p50))

        # Drop non-finite values before they reach the control loop.
        if not math.isfinite(p50) or not math.isfinite(p80):
            _log.warning(
                "predict_hours: non-finite prediction for ts=%r (p50=%s p80=%s), skipping",
                ts_raw,
                p50,
                p80,
            )
            continue

        # Clamp monotonicity: independent quantile estimators can cross.
        # Biasing P80 upward is safe — it is the conservative cushion value.
        p80 = max(p50, p80)

        results.append(
            {
                "ts": ts_raw,
                "p50_w": round(p50, 1),
                "p80_w": round(p80, 1),
            }
        )

    return results


def build_predict_payload(
    state: TrainState,
    predictions: list[dict],
) -> dict:
    """Assemble the /predict response body from a TrainState and predictions.

    Pure dict assembly — no I/O.  Accepts any object with the TrainState
    attribute set (duck-typed so tests can pass dataclasses or SimpleNamespace).

    Parameters
    ----------
    state:
        Current training state.  Only ``ready`` and ``promoted`` are read.
    predictions:
        Output of ``predict_hours``, or ``[]`` when the model is not ready.

    Returns
    -------
    dict with keys: ready, promoted, predictions.
    """
    return {
        "ready": state.ready,
        "promoted": state.promoted,
        "predictions": predictions,
    }
