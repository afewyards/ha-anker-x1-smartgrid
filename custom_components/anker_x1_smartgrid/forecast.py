"""Load/PV forecasting helpers (Phase 1: rolling profile + synth PV)."""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta

from .loadmodel import _empirical_quantile, _MIN_QUANTILE_SAMPLES
from .models import Config, ForecastInterval


def _is_weekend(dt: datetime) -> bool:
    return dt.weekday() >= 5


def _accumulate_profile_samples(
    samples: list[tuple[str, float]],
    lookback_days: int,
    now: datetime,
) -> dict[tuple[bool, int], list[float]]:
    """Parse raw (iso, watts) samples into (is_weekend, hour) buckets within the lookback window.

    Returns unsorted lists; callers sort if needed. Single source of truth for
    both rolling_load_profile (means) and from_profile_samples (quantiles).
    """
    cutoff = now - timedelta(days=lookback_days)
    acc: dict[tuple[bool, int], list[float]] = {}
    for ts_iso, w in samples:
        try:
            dt = datetime.fromisoformat(ts_iso)
        except (ValueError, TypeError):
            continue
        if dt < cutoff or w is None:
            continue
        key = (_is_weekend(dt), dt.hour)
        acc.setdefault(key, []).append(float(w))
    return acc


def rolling_load_profile(
    samples: list[tuple[str, float]],
    lookback_days: int,
    now: datetime,
) -> dict[tuple[bool, int], float]:
    """Average load (W) by (is_weekend, hour) over the lookback window."""
    acc = _accumulate_profile_samples(samples, lookback_days, now)
    return {k: sum(v) / len(v) for k, v in acc.items() if v}


def predict_load_w(
    profile: dict[tuple[bool, int], float],
    when: datetime,
    fallback_w: float,
) -> float:
    weekend = _is_weekend(when)
    val = profile.get((weekend, when.hour))
    if val is None:
        # Cross-weekend hour-of-day fallback: reuse the opposite weekend class's
        # same-hour mean before collapsing to the flat fallback.
        val = profile.get((not weekend, when.hour))
    return fallback_w if val is None else val


class LoadPredictor:
    """Uniform load-prediction interface over a dict profile or a fitted model."""

    def __init__(self, profile=None, model=None, profile_samples=None) -> None:
        self._profile = profile
        self._model = model
        self._profile_samples: dict[tuple[bool, int], list[float]] | None = profile_samples
        # Detect quantile-kwarg support once at construction to avoid masking real TypeErrors
        self._model_accepts_quantile: bool = (
            model is not None and "quantile" in inspect.signature(model.predict_load_w).parameters
        )

    @classmethod
    def from_profile(cls, profile: dict) -> LoadPredictor:
        return cls(profile=profile)  # no profile_samples — quantile-unaware (legacy scalar dict)

    @classmethod
    def from_model(cls, model) -> LoadPredictor:
        return cls(model=model)

    @classmethod
    def from_profile_samples(
        cls,
        samples: list[tuple[str, float]],
        lookback_days: int,
        now: datetime,
    ) -> LoadPredictor:
        """Build a LoadPredictor from raw (timestamp_iso, watts) samples.

        Keeps the mean-based profile for the P50/central path AND retains sorted
        sample lists for the upper-quantile path. P80>=P50 invariant always holds.
        """
        acc = _accumulate_profile_samples(samples, lookback_days, now)
        profile = {k: sum(v) / len(v) for k, v in acc.items() if v}
        profile_samples = {k: sorted(v) for k, v in acc.items() if v}
        return cls(profile=profile, profile_samples=profile_samples)

    def predict(
        self,
        when: datetime,
        temp: float | None,
        fallback_w: float,
        *,
        quantile: float = 0.5,
    ) -> float:
        if self._model is not None:
            if self._model_accepts_quantile:
                upper = self._model.predict_load_w(when, temp, fallback_w, quantile=quantile)
                if quantile <= 0.5:
                    return upper
                # Independent quantile estimators can cross; clamp the upper
                # quantile to the median so the cushion is never inverted.
                # Mirrors the profile branch below and addon/predictor.py:130.
                p50 = self._model.predict_load_w(when, temp, fallback_w, quantile=0.5)
                return max(p50, upper)
            return self._model.predict_load_w(when, temp, fallback_w)
        if self._profile is not None:
            central = predict_load_w(self._profile, when, fallback_w)
            if quantile <= 0.5 or self._profile_samples is None:
                return central
            # Upper quantile from samples: try same-class first, then
            # opposite-weekend same-hour samples so an unseen class keeps a real
            # per-hour P80 shape (cushion) instead of returning the bare central.
            key = (_is_weekend(when), when.hour)
            key_samples = self._profile_samples.get(key)
            if key_samples is None or len(key_samples) < _MIN_QUANTILE_SAMPLES:
                alt_key = (not _is_weekend(when), when.hour)
                key_samples = self._profile_samples.get(alt_key)
            if key_samples is None or len(key_samples) < _MIN_QUANTILE_SAMPLES:
                return central
            upper = _empirical_quantile(key_samples, quantile)
            return max(central, upper)
        return fallback_w


def build_intervals(
    pv_curve: list[tuple[datetime, float]],
    predictor,
    fallback_load_w: float,
    cfg: Config,
    temp_by_start: dict | None = None,
    *,
    quantile: float = 0.5,
) -> list[ForecastInterval]:
    """Join PV curve with predicted load. `predictor` is a LoadPredictor or legacy dict.

    ``quantile`` is forwarded to ``predictor.predict`` so that callers can request
    an upper-quantile (P80) load forecast vs the default P50 (for display).
    Legacy dict/profile predictors ignore the quantile — their output is unchanged.
    """
    if not pv_curve:
        return []
    if isinstance(predictor, dict):
        predictor = LoadPredictor.from_profile(predictor)
    temp_by_start = temp_by_start or {}
    intervals: list[ForecastInterval] = []
    prev_gap = 1.0
    for i, (start, pv_w) in enumerate(pv_curve):
        if i + 1 < len(pv_curve):
            gap = (pv_curve[i + 1][0] - start).total_seconds() / 3600.0
            prev_gap = gap
        else:
            gap = prev_gap
        load_w = predictor.predict(start, temp_by_start.get(start), fallback_load_w, quantile=quantile)
        intervals.append(ForecastInterval(start, pv_w, load_w, gap))
    return intervals
