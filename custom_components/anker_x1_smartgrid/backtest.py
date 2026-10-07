"""Walk-forward backtest comparing the load model to an hour-mean baseline."""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import featureset
from .dataquality import FeatureRow

_TZ_AMS = ZoneInfo("Europe/Amsterdam")
from .hgbr import HGBRQuantileModel
from .loadmodel import BucketedLoadModel

_LOGGER = logging.getLogger(__name__)

# Minimum fractional improvement the model must beat the baseline by on EACH
# gate metric before promotion (0.02 = 2%).  A bare strict-< win is noise.
PROMOTE_MIN_IMPROVEMENT: float = 0.02

# Minimum per-step test samples required to promote on the MAE-only fallback
# path (when the 24h horizon-energy MAE cannot be formed on gappy data).
# ~7 days of hourly samples — high enough that a thin model is never promoted.
MIN_PROMOTE_MAE_SAMPLES: int = 168

MIN_HORIZON_ORIGINS_24H: int = 8


def mae(pairs: list[tuple[float, float]]) -> float:
    if not pairs:
        return 0.0
    return sum(abs(p - a) for p, a in pairs) / len(pairs)


def rmse(pairs: list[tuple[float, float]]) -> float:
    if not pairs:
        return 0.0
    return math.sqrt(sum((p - a) ** 2 for p, a in pairs) / len(pairs))


def pinball_loss(q: float, pairs: list[tuple[float, float]]) -> float | None:
    """Average pinball (quantile) loss for quantile *q*.

    pairs = [(predicted, actual)]. Returns None when *pairs* is empty.
    Formula per sample: max(q * (a - p), (q - 1) * (a - p))
    """
    if not pairs:
        return None
    total = 0.0
    for p, a in pairs:
        diff = a - p
        total += max(q * diff, (q - 1) * diff)
    return total / len(pairs)


def _baseline_fit(rows: list[FeatureRow]) -> dict:
    acc: dict[tuple, list[float]] = {}
    for r in rows:
        acc.setdefault((r.is_weekend, r.hour), []).append(r.load_w)
    return {k: sum(v) / len(v) for k, v in acc.items()}


def _baseline_fit_hourly(hourly_rows: list[dict]) -> dict:
    """Hour-mean baseline from hourly rollup row dicts.

    Keyed by ``(is_weekend, hour_utc)`` — same convention as ``_baseline_fit``.
    Target is the energy-derived hourly load (``house_load_kwh_sum``×1000,
    ``house_load_mean`` fallback). Rows where it is ``None`` are silently skipped.
    """
    return featureset.hour_mean_profile(hourly_rows)


def should_promote(metrics: dict | None) -> bool:
    """Return True only when the model strictly beats the hour-mean baseline on BOTH:

    - ``horizon_energy_mae_24h`` < ``baseline_horizon_energy_mae_24h``
      (primary — the 24-hour ahead energy error that sets the grid-charge deficit).
    - ``model_mae`` < ``baseline_mae``
      (per-step MAE improvement).

    Any None value or missing key → return False (do not promote).
    """
    if not metrics:
        return False
    h24 = metrics.get("horizon_energy_mae_24h")
    bh24 = metrics.get("baseline_horizon_energy_mae_24h")
    m_mae = metrics.get("model_mae")
    b_mae = metrics.get("baseline_mae")
    # Per-step MAE is mandatory on EVERY promotion path.
    if m_mae is None or b_mae is None:
        return False
    margin = 1.0 - PROMOTE_MIN_IMPROVEMENT
    mae_ok = m_mae < b_mae * margin

    # Primary gate: BOTH 24h horizon-energy AND per-step MAE clear the margin,
    # AND enough rolling origins produced the horizon-energy value.
    if h24 is not None and bh24 is not None:
        n_origins = metrics.get("n_horizon_origins_24h", 0)
        if not isinstance(n_origins, (int, float)) or n_origins < MIN_HORIZON_ORIGINS_24H:
            return False
        return bool(h24 < bh24 * margin and mae_ok)

    # M3 gappy-data fallback: the 24h horizon-energy MAE could not be formed.
    # Gate on per-step MAE ALONE, but keep the margin AND require a minimum
    # sample count so a thin/gappy model is never promoted.
    n_test = metrics.get("n_test")
    if isinstance(n_test, bool) or not isinstance(n_test, (int, float)):
        return False
    if not mae_ok or n_test < MIN_PROMOTE_MAE_SAMPLES:
        return False
    _LOGGER.info(
        "should_promote: 24h horizon-energy MAE absent (gappy data); promoting "
        "on MAE-only fallback (model_mae=%.1f baseline_mae=%.1f n_test=%d).",
        m_mae,
        b_mae,
        int(n_test),
    )
    return True


def walk_forward(
    rows: list[FeatureRow],
    *,
    train_days: int,
    test_days: int,
    fallback_w: float,
    horizon_hours: tuple[int, ...] = (12, 24),
    quantiles: tuple[float, ...] = (0.5, 0.8),
) -> dict:
    """Rolling-origin evaluation; returns model vs baseline error metrics.

    New optional parameters (behaviour-preserving defaults):

    ``horizon_hours``
        Horizons (hours) over which to compute summed-energy error.  Keys
        ``horizon_energy_mae_24h`` and ``horizon_energy_mae_12h`` are always
        present in the return dict; they are None when the corresponding
        horizon is not in *horizon_hours* or data is insufficient.
    ``quantiles``
        Quantiles for pinball loss.  ``pinball_p50`` maps to q=0.5 and
        ``pinball_p80`` to q=0.8; set to None when the model does not accept
        a ``quantile`` keyword argument or no pairs were collected.

    All pre-existing return keys are preserved unchanged.
    Added keys: ``horizon_energy_mae_24h``, ``horizon_energy_mae_12h``,
    ``baseline_horizon_energy_mae_24h``, ``pinball_p50``, ``pinball_p80``.
    """
    empty = {
        "model_mae": None,
        "baseline_mae": None,
        "model_rmse": None,
        "baseline_rmse": None,
        "n_test": 0,
        "improvement_pct": 0.0,
        "horizon_energy_mae_24h": None,
        "horizon_energy_mae_12h": None,
        "baseline_horizon_energy_mae_24h": None,
        "pinball_p50": None,
        "pinball_p80": None,
    }
    if not rows:
        return empty
    rows = sorted(rows, key=lambda r: r.ts)
    start = rows[0].ts
    end = rows[-1].ts
    model_pairs: list[tuple[float, float]] = []
    base_pairs: list[tuple[float, float]] = []

    # horizon energy: per-horizon list of |Σpred_kWh - Σactual_kWh| per origin
    horizon_errs: dict[int, list[float]] = {h: [] for h in horizon_hours}
    baseline_horizon_errs: dict[int, list[float]] = {h: [] for h in horizon_hours}

    # pinball pairs: None signals the model doesn't accept the quantile kwarg
    pinball_pairs: dict[float, list[tuple[float, float]] | None] = {q: [] for q in quantiles}

    origin = start + timedelta(days=train_days)
    while origin < end:
        train = [r for r in rows if origin - timedelta(days=train_days) <= r.ts < origin]
        test = [r for r in rows if origin <= r.ts < origin + timedelta(days=test_days)]
        if train and test:
            model = BucketedLoadModel.fit(train)
            base = _baseline_fit(train)

            # per-row pairs + pinball
            for r in test:
                pred = model.predict_load_w(r.ts, r.temp, fallback_w)
                model_pairs.append((pred, r.load_w))
                base_pairs.append((base.get((r.is_weekend, r.hour), fallback_w), r.load_w))
                for q in list(quantiles):
                    if pinball_pairs.get(q) is None:
                        continue  # already determined not supported
                    try:
                        pred_q = model.predict_load_w(r.ts, r.temp, fallback_w, quantile=q)  # type: ignore[call-arg]
                        pinball_pairs[q].append((pred_q, r.load_w))  # type: ignore[union-attr]
                    except TypeError:
                        pinball_pairs[q] = None  # mark unsupported for this and future rows

            # horizon energy: test is already sorted (rows was sorted above)
            for h in horizon_hours:
                window = test[:h]
                if len(window) >= h:
                    pred_kwh = sum(model.predict_load_w(r.ts, r.temp, fallback_w) for r in window) / 1000.0
                    act_kwh = sum(r.load_w for r in window) / 1000.0
                    base_kwh = sum(base.get((r.is_weekend, r.hour), fallback_w) for r in window) / 1000.0
                    horizon_errs[h].append(abs(pred_kwh - act_kwh))
                    baseline_horizon_errs[h].append(abs(base_kwh - act_kwh))

        origin += timedelta(days=test_days)

    if not model_pairs:
        return empty
    m_mae, b_mae = mae(model_pairs), mae(base_pairs)
    improvement = (b_mae - m_mae) / b_mae * 100.0 if b_mae else 0.0

    def _avg(lst: list[float]) -> float | None:
        return sum(lst) / len(lst) if lst else None

    def _pinball(q: float) -> float | None:
        p = pinball_pairs.get(q)
        return pinball_loss(q, p) if isinstance(p, list) else None

    return {
        "model_mae": m_mae,
        "baseline_mae": b_mae,
        "model_rmse": rmse(model_pairs),
        "baseline_rmse": rmse(base_pairs),
        "n_test": len(model_pairs),
        "improvement_pct": improvement,
        "horizon_energy_mae_24h": _avg(horizon_errs.get(24, [])),
        "horizon_energy_mae_12h": _avg(horizon_errs.get(12, [])),
        "baseline_horizon_energy_mae_24h": _avg(baseline_horizon_errs.get(24, [])),
        "pinball_p50": _pinball(0.5),
        "pinball_p80": _pinball(0.8),
    }


def walk_forward_hgbr(
    hourly_rows: list[dict],
    *,
    train_days: int,
    test_days: int,
    fallback_w: float,
    quantiles: tuple[float, ...] = (0.5, 0.8),
    chained: bool = True,
    half_life_days: float | None = None,
    prior_weight: float | None = None,
    score_from: datetime | None = None,
) -> dict:
    """Rolling-origin evaluation of :class:`HGBRQuantileModel` on hourly rollup data.

    Mirrors :func:`walk_forward`'s return contract: identical metric-key dict,
    same all-None empty dict on insufficient data or sklearn absence, and the
    same pure helpers (``mae``, ``rmse``, ``pinball_loss``,
    ``_baseline_fit_hourly``) are reused throughout.

    Parameters
    ----------
    hourly_rows:
        List of ``samples_hourly`` row dicts ordered (or orderable) by
        ``hour_ts`` ASC.  Each row should contain at least ``hour_ts``
        (UTC ISO string) and the energy-derived hourly load
        (``house_load_kwh_sum``×1000, ``house_load_mean`` fallback), W.
    train_days, test_days:
        Rolling-origin window sizes in calendar days.
    fallback_w:
        Watt value returned by ``predict_load_w`` when the model is not
        fitted or the quantile was not trained.
    quantiles:
        Quantiles for pinball-loss computation.  ``pinball_p50`` maps to
        ``q=0.5`` and ``pinball_p80`` to ``q=0.8``.
    chained:
        Which serving regime to score — a gate is only meaningful when it
        grades the model the way its consumer will run it.  ``True`` (the
        add-on) predicts each test window as one multi-step chain, the way
        ``predictor.predict_hours`` serves a horizon.  ``False`` predicts every
        hour independently, with temperature as the only weather signal — what
        an in-process ``LoadPredictor`` tier does, since it is called one hour
        at a time and cannot chain.
    half_life_days, prior_weight:
        Forwarded to :meth:`HGBRQuantileModel.fit` (as of each origin), so the
        gate grades the model the way it is served.  With a half-life each
        origin trains on every row before it instead of the trailing
        ``train_days``; the baseline stays the ``train_days`` hour-mean.
        ``prior_weight`` blends only the chained series: ``predict_load_w``
        never sees the prior, so it is ignored when ``chained`` is False.
    score_from:
        Place the first origin ``train_days`` after the first row at or after
        this instant; earlier rows are training and lag history only.

    Returns
    -------
    dict with keys identical to :func:`walk_forward`.  Returns the all-None
    empty dict when there is insufficient data or when sklearn is not
    installed.  **Never raises.**
    """
    if prior_weight and not chained:
        _LOGGER.debug("walk_forward_hgbr: prior_weight ignored; per-hour prediction serves no prior blend")
        prior_weight = None
    empty = {
        "model_mae": None,
        "baseline_mae": None,
        "model_rmse": None,
        "baseline_rmse": None,
        "n_test": 0,
        "improvement_pct": 0.0,
        "horizon_energy_mae_24h": None,
        "horizon_energy_mae_12h": None,
        "baseline_horizon_energy_mae_24h": None,
        "pinball_p50": None,
        "pinball_p80": None,
    }
    try:
        if not hourly_rows:
            return empty

        # Parse and sort by UTC timestamp.
        parsed: list[tuple[datetime, dict]] = []
        for row in hourly_rows:
            ts_str = row.get("hour_ts")
            if ts_str:
                parsed.append((datetime.fromisoformat(str(ts_str)), row))
        if not parsed:
            return empty
        parsed.sort(key=lambda x: x[0])

        start_ts = next(ts for ts, _ in parsed if score_from is None or ts >= score_from)
        end_ts = parsed[-1][0]

        model_pairs: list[tuple[float, float]] = []
        base_pairs: list[tuple[float, float]] = []
        _horizon_hours = (12, 24)
        horizon_errs: dict[int, list[float]] = {h: [] for h in _horizon_hours}
        baseline_horizon_errs: dict[int, list[float]] = {h: [] for h in _horizon_hours}

        # HGBR always accepts a quantile kwarg — initialise all as empty lists.
        pinball_pairs: dict[float, list[tuple[float, float]]] = {q: [] for q in quantiles}

        origin = start_ts + timedelta(days=train_days)
        while origin < end_ts:
            window_rows = [row for ts, row in parsed if origin - timedelta(days=train_days) <= ts < origin]
            train_rows = [row for ts, row in parsed if ts < origin] if half_life_days else window_rows
            # Only include test entries that have a real target (energy-derived
            # hourly load: house_load_kwh_sum×1000, house_load_mean fallback).
            test_entries = [
                (ts, row)
                for ts, row in parsed
                if origin <= ts < origin + timedelta(days=test_days) and featureset.hourly_load_w(row) is not None
            ]

            if train_rows and test_entries:
                # One HGBR fit per rolling origin — intentionally pessimistic: lag
                # lookups are built only from train_rows, so test-time lag values
                # reflect what the model would actually see at the origin timestamp
                # (no look-ahead from later rows).
                model = HGBRQuantileModel()
                model.fit(
                    train_rows,
                    quantiles=quantiles,
                    half_life_days=half_life_days,
                    prior_weight=prior_weight,
                    as_of=origin,
                )
                if model._fitted:  # False when sklearn absent or too few rows
                    base = _baseline_fit_hourly(window_rows)

                    # The test window is predicted as ONE chain, exactly as the
                    # add-on serves a horizon: hour-by-hour calls would leave
                    # load_lag_1h NaN from the second hour on, grading a regime
                    # production does not run.  Weather comes from the row for
                    # the same reason — serve time has it.
                    _qs = tuple(dict.fromkeys((0.5, *quantiles)))
                    if chained:
                        series = model.predict_series(
                            [
                                {
                                    "when": ts,
                                    "temp": row.get("temp_forecast_mean"),
                                    "cloud_cover": row.get("cloud_cover_mean"),
                                    "humidity": row.get("humidity_mean"),
                                    "wind_speed": row.get("wind_speed_mean"),
                                    "persons_home": row.get("persons_home_mean"),
                                }
                                for ts, row in test_entries
                            ],
                            fallback_w,
                            quantiles=_qs,
                        )
                    else:
                        series = [
                            {
                                q: model.predict_load_w(
                                    ts,
                                    row.get("temp_forecast_mean"),
                                    fallback_w,
                                    quantile=q,
                                )
                                for q in _qs
                            }
                            for ts, row in test_entries
                        ]

                    for (ts, row), preds in zip(test_entries, series, strict=True):
                        actual = featureset.hourly_load_w(row)

                        # Median prediction drives the primary error metrics.
                        pred_p50 = preds[0.5]
                        model_pairs.append((pred_p50, actual))

                        base_key = (ts.astimezone(_TZ_AMS).weekday() >= 5, ts.astimezone(_TZ_AMS).hour)
                        base_pairs.append((base.get(base_key, fallback_w), actual))

                        for q in quantiles:
                            pinball_pairs[q].append((preds[float(q)], actual))

                    # Horizon energy (reuses the same accumulation pattern as walk_forward).
                    for h in _horizon_hours:
                        window = test_entries[:h]
                        if len(window) >= h:
                            pred_kwh = sum(preds[0.5] for preds in series[:h]) / 1000.0
                            act_kwh = sum(featureset.hourly_load_w(row) for _, row in window) / 1000.0
                            base_kwh = (
                                sum(
                                    base.get(
                                        (ts.astimezone(_TZ_AMS).weekday() >= 5, ts.astimezone(_TZ_AMS).hour), fallback_w
                                    )
                                    for ts, _ in window
                                )
                                / 1000.0
                            )
                            horizon_errs[h].append(abs(pred_kwh - act_kwh))
                            baseline_horizon_errs[h].append(abs(base_kwh - act_kwh))

            origin += timedelta(days=test_days)

        if not model_pairs:
            return empty

        m_mae, b_mae = mae(model_pairs), mae(base_pairs)
        improvement = (b_mae - m_mae) / b_mae * 100.0 if b_mae else 0.0

        def _avg(lst: list[float]) -> float | None:
            return sum(lst) / len(lst) if lst else None

        def _pinball(q: float) -> float | None:
            return pinball_loss(q, pinball_pairs.get(q, []))

        return {
            "model_mae": m_mae,
            "baseline_mae": b_mae,
            "model_rmse": rmse(model_pairs),
            "baseline_rmse": rmse(base_pairs),
            "n_test": len(model_pairs),
            "n_horizon_origins_24h": len(horizon_errs.get(24, [])),
            "improvement_pct": improvement,
            "horizon_energy_mae_24h": _avg(horizon_errs.get(24, [])),
            "horizon_energy_mae_12h": _avg(horizon_errs.get(12, [])),
            "baseline_horizon_energy_mae_24h": _avg(baseline_horizon_errs.get(24, [])),
            "pinball_p50": _pinball(0.5),
            "pinball_p80": _pinball(0.8),
        }
    except Exception:
        return empty
