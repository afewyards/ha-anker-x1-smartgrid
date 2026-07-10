"""HistGradientBoostingRegressor quantile model for load forecasting (Phase 3).

sklearn is imported LAZILY — never at module top level.  The integration can
import and run this module even when sklearn is not installed.  All public
entry points degrade gracefully:

- ``is_ready(...)``       → ``False``
- ``fit(...)``            → returns ``self`` in an unfitted state
- ``predict_load_w(...)`` → returns ``fallback_w``

Design overview
---------------
One ``HistGradientBoostingRegressor(loss="quantile", quantile=q)`` is fitted
per quantile on the hourly rollup data provided by
``featureset.build_feature_matrix``.

The feature matrix may contain ``float("nan")`` values (missing lags, missing
weather signals, etc.).  ``HistGBR`` handles NaN natively — **no imputation is
done**.

At predict time the caller supplies ``temp`` plus, when available, ``cloud_cover``,
``humidity`` and ``wind_speed``.  HGBR's native NaN support means missing weather
signals do not cause crashes or undefined behaviour; the degraded features
simply reduce per-point accuracy somewhat.

Predict-time lag staleness
--------------------------
Lag features (``load_lag_1h``, ``load_lag_24h``, ``load_lag_168h``,
``rolling_mean_24h``, ``prev_day_total_kwh``) are assembled from a UTC-keyed
lookup built during ``fit()``.  Between retrains these lags reflect the state
at retrain time (up to ``retrain_hours`` stale).  This is acceptable per spec;
the fallback chain (HGBR → BucketedLoadModel → rolling profile → fallback_w)
covers the case where the model is not yet ready.

Hyperparameter rationale (HA runs on a Raspberry Pi / constrained NUC)
-----------------------------------------------------------------------
``max_iter=100``        — sklearn default; already modest; shallow trees are fast.
``max_depth=4``         — prevents overfit on sparse/weekend-heavy data.
``min_samples_leaf=10`` — light regularisation; avoids tiny leaves.
``early_stopping=False``— deterministic & fast; avoids internal train/val split.
``random_state=0``      — fully reproducible results across HA restarts.

No numpy import at module level — numpy is guaranteed by sklearn but we avoid
the top-level dependency to keep the "no sklearn" contract clean.  sklearn's
own ``fit``/``predict`` accept plain Python lists, so we pass them directly.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Sequence
from zoneinfo import ZoneInfo

from . import featureset

# ---------------------------------------------------------------------------
# Module-level constants (no sklearn import here)
# ---------------------------------------------------------------------------

_TZ_AMS = ZoneInfo("Europe/Amsterdam")
_NAN: float = float("nan")


def _coerce_serve(x: float | None) -> float:
    """Coerce a serve-time signal to float, mapping None/NaN/uncoercible → NaN."""
    if x is None:
        return _NAN
    try:
        xf = float(x)
    except (TypeError, ValueError):
        return _NAN
    return _NAN if math.isnan(xf) else xf


# Minimum number of training rows for fit() to proceed.  Below this
# HistGBR would either crash or produce a degenerate model.  The
# is_ready() gate is far stricter (21 days × 24 h = 504+ rows).
_MIN_TRAIN_ROWS: int = 24


# ---------------------------------------------------------------------------
# Lazy sklearn import helper
# ---------------------------------------------------------------------------


def _import_sklearn():
    """Return the HistGradientBoostingRegressor class.

    Raises ``ImportError`` if scikit-learn is not installed.

    This function is defined at module level so tests can monkeypatch it to
    simulate a missing sklearn without touching ``sys.modules``:

        with patch.object(hgbr_module, "_import_sklearn", lambda: (_ for _ in ()).throw(ImportError())):
            ...

    or more cleanly via a ``def _raise(): raise ImportError`` + ``patch.object``.
    """
    from sklearn.ensemble import HistGradientBoostingRegressor  # noqa: PLC0415

    return HistGradientBoostingRegressor


# ---------------------------------------------------------------------------
# HGBRQuantileModel
# ---------------------------------------------------------------------------


class HGBRQuantileModel:
    """Quantile load-forecast model backed by HistGradientBoostingRegressor.

    Typical usage (inside an executor thread)::

        model = HGBRQuantileModel()
        if model.is_ready(hourly_rows):
            model.fit(hourly_rows)
        load_w = model.predict_load_w(when, temp=12.5, fallback_w=400.0, quantile=0.8)

    All methods are safe to call regardless of sklearn availability.
    """

    def __init__(self) -> None:
        self._fitted: bool = False
        # One fitted HGBR instance per quantile, keyed as float (e.g. 0.5, 0.8).
        self._models: dict[float, object] = {}
        # Snapshot of featureset.feature_names() taken at fit time.
        # Stable column order is critical for HistGBR (positional).
        self._feature_names: list[str] = []

        # Predict-time lag lookups — built from hourly rows during fit(),
        # refreshed on every retrain call.
        #
        # _utc_lookup:       UTC datetime → energy-derived hourly load (W,
        #                    house_load_kwh_sum×1000 / house_load_mean
        #                    fallback) or None
        # _local_date_kwh:   Europe/Amsterdam calendar date → daily kWh total
        #                    (summed from house_load_kwh_sum)
        self._utc_lookup: dict[datetime, float | None] = {}
        self._local_date_kwh: dict = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fit(
        self,
        hourly_rows: list[dict],
        quantiles: Sequence[float] = (0.5, 0.8),
    ) -> "HGBRQuantileModel":
        """Fit one HGBR per quantile on the hourly rollup data.

        Parameters
        ----------
        hourly_rows:
            Ordered list of hourly rollup dicts (ASC by ``hour_ts``) from
            ``recorder.read_hourly_rows()``.
        quantiles:
            Quantile values to train.  Default: ``(0.5, 0.8)``.

        Returns
        -------
        ``self`` — for method chaining.  On any failure (sklearn missing, too
        few rows) the model is left in an unfitted state; **no exception is
        raised**.
        """
        # Reset to unfitted; every call is a full retrain
        self._fitted = False
        self._models = {}

        try:
            HGBR = _import_sklearn()
        except ImportError:
            return self

        X, y, _index = featureset.build_feature_matrix(hourly_rows)
        if len(X) < _MIN_TRAIN_ROWS:
            return self

        # Fix 2 — neutralize all-NaN feature columns before fitting.
        # HistGBR raises ValueError on any column that is ENTIRELY NaN (no split
        # candidate can be found).  This can happen when:
        #   • load_lag_168h: train window < 8 days (no 168h-prior data available).
        #   • weather streams: a sensor that had no data over the whole window.
        # Replacing with 0.0 → constant column → the tree never splits on it →
        # contributes nothing to the model, which is exactly what we want.
        # Per-row NaNs in *partially*-available columns are left intact: HistGBR
        # handles them natively via its built-in missing-value support.
        if X:
            n_cols = len(X[0])
            for col_j in range(n_cols):
                if all(math.isnan(row[col_j]) for row in X):
                    for row in X:
                        row[col_j] = 0.0

        # Build predict-time lookup structures from the same rows used for training
        self._build_lookups(hourly_rows)
        self._feature_names = featureset.feature_names()

        # Fix 1 — wrap the per-quantile sklearn fit in try/except so that any
        # unexpected failure (e.g. degenerate data not caught above) leaves the model
        # in a clean unfitted state rather than propagating.  The docstring already
        # promises "no exception is raised" — this enforces that contract.
        try:
            for q in quantiles:
                model = HGBR(
                    loss="quantile",
                    quantile=q,
                    max_iter=100,           # sklearn default; already modest
                    max_depth=4,            # shallow trees — prevents overfit
                    min_samples_leaf=10,    # light regularisation
                    early_stopping=False,   # deterministic; no val-split overhead
                    random_state=0,         # reproducible across restarts
                )
                # sklearn accepts plain Python list-of-lists — no numpy import needed
                model.fit(X, y)
                self._models[float(q)] = model
        except Exception:  # noqa: BLE001 — propagating violates the never-raise contract
            self._fitted = False
            self._models = {}
            return self

        self._fitted = bool(self._models)
        return self

    def refresh_lookups(self, hourly_rows: list[dict]) -> bool:
        """Rebuild the lag lookups from fresh rows at serve time.

        fit() freezes ``_utc_lookup``/``_local_date_kwh`` at train time, which
        makes load_lag_1h/rolling_mean_24h up to ~24h stale by evening.  Calling
        this before predicting re-anchors the lag features on live history
        (intraday adaptation).  Never raises; on any failure the existing
        lookups are kept and the model serves as before.
        """
        try:
            if not hourly_rows:
                return False
            self._build_lookups(hourly_rows)
            return True
        except Exception:  # noqa: BLE001 — serve-path must never raise
            return False

    def predict_load_w(
        self,
        when: datetime,
        temp: float | None,
        fallback_w: float,
        *,
        quantile: float = 0.5,
        cloud_cover: float | None = None,
        humidity: float | None = None,
        wind_speed: float | None = None,
        persons_home: float | None = None,
    ) -> float:
        """Predict house load (W) for the target hour.

        Parameters
        ----------
        when:
            Target hour as a UTC-aware :class:`datetime`.
        temp:
            Forecast temperature (°C) for the target hour; ``None`` → NaN for
            temp/HDD/CDD features.
        fallback_w:
            Returned unchanged on any failure: model not fitted, sklearn not
            installed, or requested ``quantile`` was not trained.
        quantile:
            Which trained quantile to use.  Must be a key in ``_models``.
            Default: ``0.5`` (median).

        Returns
        -------
        Predicted load in watts (float), clamped to ≥ 0.  Returns
        ``fallback_w`` on any failure path.
        """
        if not self._fitted:
            return fallback_w

        model = self._models.get(float(quantile))
        if model is None:
            return fallback_w

        # Guard: confirm sklearn is still available at serve time.
        # In production this is always True if fit() succeeded, but the check
        # allows tests to simulate runtime unavailability via monkeypatching.
        try:
            _import_sklearn()
        except ImportError:
            return fallback_w

        vec = self._assemble_feature_vector(
            when, temp,
            cloud_cover=cloud_cover, humidity=humidity, wind_speed=wind_speed,
            persons_home=persons_home,
        )
        if vec is None:
            return fallback_w

        try:
            # sklearn predict accepts list-of-one-list without numpy
            raw = float(model.predict([vec])[0])  # type: ignore[union-attr]
            # Guard: max(0.0, nan) returns nan which must never reach the control loop.
            if not math.isfinite(raw):
                return fallback_w
            return max(0.0, raw)
        except Exception:  # pragma: no cover — defensive catch for unexpected errors
            return fallback_w

    def is_ready(
        self,
        hourly_rows: list[dict],
        min_days: int = 21,
    ) -> bool:
        """Return ``True`` if there is sufficient lag-complete history to train.

        Lag-complete rule
        -----------------
        A row at UTC time *t* is **lag-complete** when the row at
        *t − 168 h* (the 7-day weekly lag) is **also present** in
        ``hourly_rows``.  This is the most distal lag feature; satisfying it
        implies the shorter lags (1 h, 24 h) are also available.

        We count the distinct **Europe/Amsterdam calendar dates** represented
        by lag-complete rows and require ``≥ min_days``.

        Practical consequence: at least ``(7 + min_days) × 24`` consecutive
        hourly rows are required — 7 days of seed data plus ``min_days`` days
        of rows with all lags satisfied.  With the default of 21, the ML path
        activates after ≈ 28 days of continuous recording.

        Returns ``False`` immediately when sklearn is not installed.
        """
        try:
            _import_sklearn()
        except ImportError:
            return False

        # Build a frozenset of all present UTC timestamps (O(n))
        ts_set: set[datetime] = set()
        for row in hourly_rows:
            ts_str = row.get("hour_ts")
            if ts_str:
                ts_set.add(datetime.fromisoformat(str(ts_str)))

        lag_7d = timedelta(hours=168)
        lag_complete_dates: set = set()

        for row in hourly_rows:
            ts_str = row.get("hour_ts")
            if not ts_str:
                continue
            t = datetime.fromisoformat(str(ts_str))
            if (t - lag_7d) in ts_set:
                local_date = t.astimezone(_TZ_AMS).date()
                lag_complete_dates.add(local_date)

        return len(lag_complete_dates) >= min_days

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_lookups(self, hourly_rows: list[dict]) -> None:
        """Build UTC-keyed load lookup and local-date energy totals.

        Called once during fit(); results are stored for O(1) access during
        predict-time lag assembly.  Storing derived dicts rather than the
        raw row list reduces memory footprint.
        """
        utc_lookup: dict[datetime, float | None] = {}
        local_date_kwh: dict = {}

        for row in hourly_rows:
            ts_str = row.get("hour_ts")
            if not ts_str:
                continue
            ts = datetime.fromisoformat(str(ts_str))
            load = featureset.hourly_load_w(row)
            utc_lookup[ts] = load

            kwh = row.get("house_load_kwh_sum")
            if kwh is not None:
                local_d = ts.astimezone(_TZ_AMS).date()
                local_date_kwh.setdefault(local_d, 0.0)
                local_date_kwh[local_d] += float(kwh)

        self._utc_lookup = utc_lookup
        self._local_date_kwh = local_date_kwh

    def _assemble_feature_vector(
        self,
        when: datetime,
        temp: float | None,
        cloud_cover: float | None = None,
        humidity: float | None = None,
        wind_speed: float | None = None,
        persons_home: float | None = None,
    ) -> list[float] | None:
        """Assemble one 18-float feature vector for the target hour.

        Parameters
        ----------
        when:
            Target hour (UTC-aware datetime).
        temp:
            Forecast temperature (°C); ``None`` or NaN → NaN for
            ``temp_forecast`` / ``hdd`` / ``cdd``.
        cloud_cover, humidity, wind_speed:
            Forecast weather signals for the target hour, when the caller
            has them available at serve time.  ``None`` (the default) or
            NaN → NaN for the corresponding feature.

        Returns
        -------
        18-element list in ``_feature_names`` order, or ``None`` if assembly
        fails for any reason (e.g. naive datetime).  Missing lags default
        to NaN — HGBR handles them natively.

        Weather-NaN rationale (intentional per spec §5)
        ------------------------------------------------
        ``cloud_cover``/``humidity``/``wind_speed`` are coerced from the
        caller-supplied values when provided, else NaN.  HGBR tolerates the
        NaNs natively.
        """
        try:
            # --- Calendar (6 features) --- computed in Europe/Amsterdam local time
            cal = featureset.encode_calendar_features(when)

            # --- Lag features (5) --- shared helper ensures train/predict consistency
            t = when
            lags = featureset.encode_lag_features_from_lookups(
                self._utc_lookup, self._local_date_kwh, t
            )

            # --- Weather features (7) --- only temp is available at serve time
            if temp is None or (isinstance(temp, float) and math.isnan(temp)):
                temp_forecast: float = _NAN
                hdd: float = _NAN
                cdd: float = _NAN
            else:
                temp_forecast = float(temp)
                hdd = max(0.0, 15.5 - temp_forecast)   # spec Decision #4
                cdd = max(0.0, temp_forecast - 22.0)    # spec Decision #4

            feat_dict: dict[str, float] = {
                **cal,   # hour_sin, hour_cos, doy_sin, doy_cos, day_of_week, is_holiday
                **lags,  # load_lag_1h/24h/168h, rolling_mean_24h, prev_day_total_kwh
                "temp_forecast": temp_forecast,
                "hdd": hdd,
                "cdd": cdd,
                "cloud_cover": _coerce_serve(cloud_cover),
                "humidity": _coerce_serve(humidity),
                "wind_speed": _coerce_serve(wind_speed),
                "persons_home": _coerce_serve(persons_home),
            }

            # Project to stable feature_names() order (HistGBR is positional)
            return [feat_dict[name] for name in self._feature_names]

        except Exception:  # noqa: BLE001 — return None on any assembly failure
            return None
