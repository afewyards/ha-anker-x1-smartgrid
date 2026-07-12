"""Pure-Python temperature-bucketed load model (Phase 2)."""

from __future__ import annotations

from datetime import datetime

from .dataquality import FeatureRow

TEMP_BUCKETS = (-5, 0, 5, 10, 15, 20)  # 7 buckets: idx 0..len

_MIN_QUANTILE_SAMPLES = 8


def temp_bucket(temp: float | None) -> int:
    """Bucket index for a temperature; -1 for missing."""
    if temp is None:
        return -1
    for i, edge in enumerate(TEMP_BUCKETS):
        if temp < edge:
            return i
    return len(TEMP_BUCKETS)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _empirical_quantile(sorted_vals: list[float], q: float) -> float:
    """Linear-interpolation quantile (numpy 'linear'/type-7 convention).

    Requires sorted_vals to be pre-sorted ascending. q is clamped to [0, 1].
    Raises ValueError on empty input. Returns sorted_vals[0] for n==1.
    """
    if not sorted_vals:
        raise ValueError("_empirical_quantile requires a non-empty list")
    q = min(max(q, 0.0), 1.0)
    n = len(sorted_vals)
    if n == 1:
        return sorted_vals[0]
    pos = q * (n - 1)
    lo = int(pos)
    hi = lo + 1 if lo + 1 < n else lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


class BucketedLoadModel:
    def __init__(
        self, cell, hourly, global_mean, n_samples, cell_samples=None, hourly_samples=None, global_samples=None
    ) -> None:
        self._cell = cell  # (weekend, hour, bucket) -> mean
        self._hourly = hourly  # (weekend, hour) -> mean
        self._global = global_mean  # float | None
        self.n_samples = n_samples
        # Sorted sample lists for upper-quantile computation (None = not stored)
        self._cell_samples: dict[tuple, list[float]] = cell_samples or {}
        self._hourly_samples: dict[tuple, list[float]] = hourly_samples or {}
        self._global_samples: list[float] = global_samples or []

    @property
    def cells(self) -> int:
        return len(self._cell)

    @classmethod
    def fit(cls, rows: list[FeatureRow]) -> BucketedLoadModel:
        cell_acc: dict[tuple, list[float]] = {}
        hour_acc: dict[tuple, list[float]] = {}
        all_loads: list[float] = []
        for r in rows:
            b = temp_bucket(r.temp)
            cell_acc.setdefault((r.is_weekend, r.hour, b), []).append(r.load_w)
            hour_acc.setdefault((r.is_weekend, r.hour), []).append(r.load_w)
            all_loads.append(r.load_w)
        cell = {k: _mean(v) for k, v in cell_acc.items()}
        hourly = {k: _mean(v) for k, v in hour_acc.items()}
        global_mean = _mean(all_loads) if all_loads else None
        # Pre-sort sample lists for O(1) quantile lookup at predict time
        cell_samples = {k: sorted(v) for k, v in cell_acc.items()}
        hourly_samples = {k: sorted(v) for k, v in hour_acc.items()}
        global_samples = sorted(all_loads) if all_loads else []
        return cls(
            cell,
            hourly,
            global_mean,
            len(rows),
            cell_samples=cell_samples,
            hourly_samples=hourly_samples,
            global_samples=global_samples,
        )

    def predict_load_w(
        self,
        when: datetime,
        temp: float | None,
        fallback_w: float,
        *,
        quantile: float = 0.5,
    ) -> float:
        """Predict load using bucketed mean hierarchy; for quantile>0.5, add empirical upper cushion.

        P80>=P50 invariant: result is always max(central_mean, empirical_quantile(samples, q)).
        Upper quantile uses the finest level with >=_MIN_QUANTILE_SAMPLES samples; if none
        qualifies, returns central (no cushion when data is too sparse).
        """
        weekend = when.weekday() >= 5
        b = temp_bucket(temp)
        cell_key = (weekend, when.hour, b)
        hour_key = (weekend, when.hour)
        alt_cell_key = (not weekend, when.hour, b)
        alt_hour_key = (not weekend, when.hour)

        # P50 / central: cell -> hourly -> opposite-weekend cell -> opposite-weekend
        # hourly -> global -> fallback.  The cross-weekend hour-of-day fallback keeps a
        # per-hour shape when one weekend class is unseen (e.g. all-weekday training)
        # instead of collapsing to the flat global mean.
        if cell_key in self._cell:
            central = self._cell[cell_key]
        elif hour_key in self._hourly:
            central = self._hourly[hour_key]
        elif alt_cell_key in self._cell:
            central = self._cell[alt_cell_key]
        elif alt_hour_key in self._hourly:
            central = self._hourly[alt_hour_key]
        elif self._global is not None:
            central = self._global
        else:
            central = fallback_w

        if quantile <= 0.5:
            return central

        # Upper quantile: find finest level with enough samples (opposite-weekend
        # same-hour samples preserve a per-hour P80 shape before the flat global).
        upper: float | None = None
        for samples in (
            self._cell_samples.get(cell_key),
            self._hourly_samples.get(hour_key),
            self._cell_samples.get(alt_cell_key),
            self._hourly_samples.get(alt_hour_key),
            self._global_samples if len(self._global_samples) >= _MIN_QUANTILE_SAMPLES else None,
        ):
            if samples is not None and len(samples) >= _MIN_QUANTILE_SAMPLES:
                upper = _empirical_quantile(samples, quantile)
                break

        if upper is None:
            return central  # too sparse — no cushion
        return max(central, upper)
