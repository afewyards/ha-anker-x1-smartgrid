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
from collections.abc import Sequence
from zoneinfo import ZoneInfo

from . import const, featureset

# ---------------------------------------------------------------------------
# Module-level constants (no sklearn import here)
# ---------------------------------------------------------------------------

_TZ_AMS = ZoneInfo("Europe/Amsterdam")
_NAN: float = float("nan")

# Longest hole predict_series will carry the last measured load across before
# the first horizon hour (see _bridge_seam).
_MAX_SEAM_CARRY_H = 3


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

# Recency-weighted fits drop rows older than this many half-lives (weight
# < 0.1 %), so fit cost stays bounded as the recorder history grows.
_RECENCY_CAP_HALF_LIVES: int = 10

# History behind the hour-mean prior the served median is blended toward — the
# same window the promotion gate's baseline hour-mean uses.
_PRIOR_DAYS: int = const.DEFAULT_TRAIN_DAYS

# Days of history a row's lag features reach back (load_lag_168h dominates the
# 24 h rolling mean and the previous-day total) plus a day of slack; rows older
# than the training cap and this margin never influence a recency-weighted fit.
_LAG_REACH_DAYS: int = 8


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
    from sklearn.ensemble import HistGradientBoostingRegressor

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

        # Hour-mean prior (featureset.hour_mean_key → W) and the weight
        # predict_series blends the served median toward it with; 0 = off.
        self._prior: dict[tuple[bool, int], float] = {}
        self._prior_weight: float = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fit(
        self,
        hourly_rows: list[dict],
        quantiles: Sequence[float] = (0.5, 0.8),
        *,
        half_life_days: float | None = None,
        prior_weight: float | None = None,
        as_of: datetime | None = None,
    ) -> HGBRQuantileModel:
        """Fit one HGBR per quantile on the hourly rollup data.

        Parameters
        ----------
        hourly_rows:
            Ordered list of hourly rollup dicts (ASC by ``hour_ts``) from
            ``recorder.read_hourly_rows()``.
        quantiles:
            Quantile values to train.  Default: ``(0.5, 0.8)``.
        half_life_days:
            Weight each training row by ``0.5 ** (age_days / half_life_days)``
            and leave out rows older than ``_RECENCY_CAP_HALF_LIVES``
            half-lives (they still feed the kept rows' lag features).
            ``None``/``0`` fits every row unweighted.
        prior_weight:
            Weight of the hour-mean prior — the mean load per
            ``featureset.hour_mean_key`` over the ``_PRIOR_DAYS`` before
            *as_of* — in the median :meth:`predict_series` serves.
            ``None``/``0`` serves the model alone.
        as_of:
            The instant row ages and the prior window are measured from.
            Defaults to the end of the newest row's hour, so a fit is a pure
            function of its rows.

        Returns
        -------
        ``self`` — for method chaining.  On any failure (sklearn missing, too
        few rows) the model is left in an unfitted state; **no exception is
        raised**.
        """
        # Reset to unfitted; every call is a full retrain
        self._fitted = False
        self._models = {}
        self._prior = {}
        self._prior_weight = 0.0

        try:
            HGBR = _import_sklearn()
        except ImportError:
            return self

        if (half_life_days or prior_weight) and as_of is None:
            stamps = [
                datetime.fromisoformat(str(r["hour_ts"]))
                for r in hourly_rows
                if r.get("hour_ts") and featureset.hourly_load_w(r) is not None
            ]
            if stamps:
                as_of = max(stamps) + timedelta(hours=1)
        if half_life_days and as_of is not None:
            reach = timedelta(days=max(_RECENCY_CAP_HALF_LIVES * half_life_days + _LAG_REACH_DAYS, _PRIOR_DAYS))
            try:
                hourly_rows = [
                    r
                    for r in hourly_rows
                    if r.get("hour_ts") and datetime.fromisoformat(str(r["hour_ts"])) >= as_of - reach
                ]
            except TypeError:  # mixed naive/aware hour_ts: keep every row rather than fail the fit
                pass
        X, y, index = featureset.build_feature_matrix(hourly_rows)
        sample_weight: list[float] | None = None
        if half_life_days and as_of is not None:
            ages = [(as_of - datetime.fromisoformat(i)).total_seconds() / 86400.0 for i in index]
            keep = [j for j, age in enumerate(ages) if 0.0 < age <= _RECENCY_CAP_HALF_LIVES * half_life_days]
            X = [X[j] for j in keep]
            y = [y[j] for j in keep]
            sample_weight = [0.5 ** (ages[j] / half_life_days) for j in keep]
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
                    max_iter=100,  # sklearn default; already modest
                    max_depth=4,  # shallow trees — prevents overfit
                    min_samples_leaf=10,  # light regularisation
                    early_stopping=False,  # deterministic; no val-split overhead
                    random_state=0,  # reproducible across restarts
                )
                # sklearn accepts plain Python list-of-lists — no numpy import needed
                model.fit(X, y, sample_weight=sample_weight)
                self._models[float(q)] = model
        except Exception:
            self._fitted = False
            self._models = {}
            return self

        self._fitted = bool(self._models)
        if self._fitted and prior_weight and as_of is not None:
            since = as_of - timedelta(days=_PRIOR_DAYS)
            self._prior = featureset.hour_mean_profile(
                [
                    row
                    for row in hourly_rows
                    if row.get("hour_ts")
                    and featureset.hourly_load_w(row) is not None
                    and since <= datetime.fromisoformat(str(row["hour_ts"])) < as_of
                ]
            )
            self._prior_weight = float(prior_weight)
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
        except Exception:
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
        utc_lookup: dict | None = None,
        local_date_kwh: dict | None = None,
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
        utc_lookup, local_date_kwh:
            Lag history to resolve the lag features against; defaults to the
            model's own (train-time / ``refresh_lookups``) lookups.
            ``predict_series`` passes a working copy carrying its own
            predictions so a multi-hour horizon keeps real-valued lags.

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
            when,
            temp,
            cloud_cover=cloud_cover,
            humidity=humidity,
            wind_speed=wind_speed,
            persons_home=persons_home,
            utc_lookup=utc_lookup,
            local_date_kwh=local_date_kwh,
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

    def predict_series(
        self,
        hours: Sequence[dict],
        fallback_w: float,
        *,
        quantiles: Sequence[float] = (0.5,),
    ) -> list[dict[float, float]]:
        """Predict a whole horizon at once, feeding each hour's lags forward.

        Why this exists
        ---------------
        ``load_lag_1h`` is present on every training row but resolvable for at
        most the FIRST horizon hour at serve time — the rollup for the hour
        before any later one does not exist yet.  Handing the model NaN there
        puts the feature vector where training never went, and its output
        collapses to a near constant: the diurnal shape the DP plans against
        disappears (live 2026-08-24: a flat ~1.7 kW against a 471 W daily
        mean).  Predicting the horizon as a SERIES fixes that at the source —
        each hour's own prediction becomes the next hour's ``load_lag_1h``,
        and rolls into ``rolling_mean_24h`` / ``load_lag_24h`` /
        ``prev_day_total_kwh`` the same way a measured hour would.

        Parameters
        ----------
        hours:
            Hour dicts, any order (predicted chronologically, returned in
            request order).  ``when`` is a UTC-aware datetime; ``temp``,
            ``cloud_cover``, ``humidity``, ``wind_speed`` and ``persons_home``
            are optional and forwarded per hour.
        fallback_w:
            Per-hour fallback, as in :meth:`predict_load_w`.
        quantiles:
            Which trained quantiles to return per hour.  The chain is fed the
            median when it is requested, else the first quantile asked for —
            never a high quantile, whose bias would compound down the horizon.

        Hour-mean prior
        ---------------
        When fitted with a ``prior_weight``, the served median is
        ``(1 − w)·model + w·prior`` for every hour whose
        ``featureset.hour_mean_key`` the prior covers (the model alone
        otherwise), and the other quantiles move by the same delta, upper ones
        never below the served median.  The served median — not the raw model
        output — is what feeds the chain, whichever quantiles were requested.

        Returns
        -------
        One ``{quantile: watts}`` dict per requested hour, in request order.
        Measured hours are never overwritten, and the model's own lookups are
        left untouched, so nothing leaks into the next request.
        """
        qs = [float(q) for q in quantiles]
        results: list[dict[float, float]] = [dict.fromkeys(qs, fallback_w) for _ in hours]
        if not self._fitted or not hours:
            return results

        lookup = dict(self._utc_lookup)
        date_kwh = dict(self._local_date_kwh)
        blend = self._prior_weight > 0 and bool(self._prior) and 0.5 in self._models
        pred_qs = list(dict.fromkeys((0.5, *qs))) if blend else qs
        chain_q = 0.5 if 0.5 in pred_qs else qs[0]

        for idx in sorted(range(len(hours)), key=lambda i: hours[i]["when"]):
            hour = hours[idx]
            when = hour["when"]
            self._bridge_seam(lookup, date_kwh, when)
            preds = {
                q: self.predict_load_w(
                    when,
                    hour.get("temp"),
                    fallback_w,
                    quantile=q,
                    cloud_cover=hour.get("cloud_cover"),
                    humidity=hour.get("humidity"),
                    wind_speed=hour.get("wind_speed"),
                    persons_home=hour.get("persons_home"),
                    utc_lookup=lookup,
                    local_date_kwh=date_kwh,
                )
                for q in pred_qs
            }
            if blend:
                self._blend_prior(preds, when)
            results[idx] = {q: preds[q] for q in qs}
            if lookup.get(when) is None:
                self._record_chain_hour(lookup, date_kwh, when, preds[chain_q])
        return results

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

    def _blend_prior(self, preds: dict[float, float], when: datetime) -> None:
        """Pull the median toward the hour-mean prior and move the other quantiles with it."""
        prior = self._prior.get(featureset.hour_mean_key(when))
        if prior is None:
            return
        model_p50 = preds[0.5]
        served = (1.0 - self._prior_weight) * model_p50 + self._prior_weight * prior
        delta = served - model_p50
        for q in preds:
            if q > 0.5:
                preds[q] = max(preds[q] + delta, served)
            elif q < 0.5:
                preds[q] = min(preds[q] + delta, served)
        preds[0.5] = served

    @staticmethod
    def _record_chain_hour(
        lookup: dict,
        date_kwh: dict,
        when: datetime,
        load_w: float,
    ) -> None:
        """Book a chain value into the working lag history, mirroring a rollup row."""
        lookup[when] = load_w
        local_d = when.astimezone(_TZ_AMS).date()
        date_kwh[local_d] = date_kwh.get(local_d, 0.0) + load_w / 1000.0

    def _bridge_seam(self, lookup: dict, date_kwh: dict, when: datetime) -> None:
        """Carry the last known load forward across a short hole before *when*.

        The rollup for the hour in progress is not written until it closes, so
        a horizon that starts a couple of hours past the newest row would hand
        the FIRST hour a NaN ``load_lag_1h`` — and that hour then seeds every
        later one.  Persisting the last measured value across the hole keeps
        the chain's entry point in-distribution.  Bounded by
        ``_MAX_SEAM_CARRY_H``: past that the history is too stale to bridge
        honestly, and NaN (fallback territory) is the truthful answer.
        """
        if lookup.get(when - timedelta(hours=1)) is not None:
            return
        for back in range(2, _MAX_SEAM_CARRY_H + 2):
            known = lookup.get(when - timedelta(hours=back))
            if known is None:
                continue
            for fill in range(1, back):
                gap_hour = when - timedelta(hours=fill)
                if lookup.get(gap_hour) is None:
                    self._record_chain_hour(lookup, date_kwh, gap_hour, float(known))
            return

    def _assemble_feature_vector(
        self,
        when: datetime,
        temp: float | None,
        cloud_cover: float | None = None,
        humidity: float | None = None,
        wind_speed: float | None = None,
        persons_home: float | None = None,
        utc_lookup: dict | None = None,
        local_date_kwh: dict | None = None,
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
                self._utc_lookup if utc_lookup is None else utc_lookup,
                self._local_date_kwh if local_date_kwh is None else local_date_kwh,
                t,
            )

            # --- Weather features (7) --- only temp is available at serve time
            if temp is None or (isinstance(temp, float) and math.isnan(temp)):
                temp_forecast: float = _NAN
                hdd: float = _NAN
                cdd: float = _NAN
            else:
                temp_forecast = float(temp)
                hdd = max(0.0, 15.5 - temp_forecast)  # spec Decision #4
                cdd = max(0.0, temp_forecast - 22.0)  # spec Decision #4

            feat_dict: dict[str, float] = {
                **cal,  # hour_sin, hour_cos, doy_sin, doy_cos, day_of_week, is_holiday
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

        except Exception:
            return None
