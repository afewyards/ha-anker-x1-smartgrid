"""Pure parsers: Zonneplan price curve and PV forecast curve."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from . import const
from .models import PriceSlot


def _parse_dt(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_price_curve(forecast_attr: list[dict] | None) -> list[PriceSlot]:
    """Map Zonneplan forecast entries to sorted PriceSlots (price in €/kWh)."""
    if not forecast_attr:
        return []
    slots: list[PriceSlot] = []
    for entry in forecast_attr:
        if not isinstance(entry, dict):
            continue
        start = _parse_dt(entry.get("datetime"))
        raw = entry.get("electricity_price")
        if start is None or raw is None:
            continue
        try:
            price = float(raw) / const.PRICE_SCALE
        except (ValueError, TypeError):
            continue
        if not math.isfinite(price):
            continue  # "NaN"/"Infinity" parse as float; never let them reach the DP
        slots.append(PriceSlot(start, price))
    slots.sort(key=lambda s: s.start)
    return slots


def synth_pv_curve(
    remaining_kwh: float,
    now: datetime,
    sunset: datetime,
    *,
    step_h: float = 1.0,
) -> list[tuple[datetime, float]]:
    """Distribute remaining PV kWh over [now, sunset] as a half-sine shape."""
    if remaining_kwh <= 0 or sunset <= now:
        return []
    total_h = (sunset - now).total_seconds() / 3600.0
    n = max(1, math.ceil(total_h / step_h))
    # half-sine weights: sin(pi * (i+0.5)/n), normalized so sum*step_h*scale = energy
    weights = [math.sin(math.pi * (i + 0.5) / n) for i in range(n)]
    wsum = sum(weights) or 1.0
    total_wh = remaining_kwh * 1000.0
    curve: list[tuple[datetime, float]] = []
    for i, wt in enumerate(weights):
        energy_wh = total_wh * wt / wsum
        power_w = energy_wh / step_h
        curve.append((now + timedelta(hours=i * step_h), power_w))
    return curve


def synth_pv_curve_peaked(
    kwh: float,
    start: datetime,
    end: datetime,
    peak: datetime,
    *,
    step_h: float = 1.0,
) -> list[tuple[datetime, float]]:
    """One array's asymmetric peaked lobe over [start, end], peaked at peak.

    Uses a quarter-sine rise from start→peak and quarter-sine fall from peak→end,
    sampled at bucket centers and normalized so total energy equals kwh.
    """
    if end <= start or kwh <= 0:
        return []
    p = min(max(peak, start), end)  # clamp into [start, end]
    n = max(1, math.ceil((end - start) / timedelta(hours=step_h)))
    weights = []
    for i in range(n):
        t = start + timedelta(hours=(i + 0.5) * step_h)  # CENTER sample
        if p > start and t <= p:
            w = math.sin((math.pi / 2) * ((t - start) / (p - start)))  # quarter-sine rise
        elif end > p:
            w = math.sin((math.pi / 2) * max(0.0, (end - t) / (end - p)))  # quarter-sine fall; clamp past-end center -> 0
        else:
            w = 0.0  # defensive; unreachable for non-degenerate
        weights.append(w)
    wsum = sum(weights) or 1.0
    total_wh = kwh * 1000.0
    out: list[tuple[datetime, float]] = []
    for i, w in enumerate(weights):
        power_w = total_wh * w / wsum / step_h
        out.append((start + timedelta(hours=i * step_h), power_w))  # LEFT-edge timestamp
    return out


def build_pv_curve_from_arrays(
    arrays: list[tuple[float, datetime | None]],
    start: datetime,
    end: datetime,
    *,
    step_h: float = 1.0,
) -> list[tuple[datetime, float]]:
    """Sum per-array peaked lobes onto a shared grid.

    ``arrays`` is a list of ``(kwh, peak_dt)`` tuples.  When ``peak_dt`` is
    ``None`` the lobe peaks at the window midpoint.  All arrays share the same
    ``(start, end, step_h)`` so bucket timestamps coincide exactly.
    """
    if not arrays or end <= start:
        return []
    midpoint = start + (end - start) / 2
    merged: dict[datetime, float] = {}
    for kwh, peak_dt in arrays:
        peak = peak_dt if peak_dt is not None else midpoint
        for t, w in synth_pv_curve_peaked(kwh, start, end, peak, step_h=step_h):
            merged[t] = merged.get(t, 0.0) + w
    return sorted(merged.items())


def build_pv_curve_from_watts(
    today_sources: list[list[tuple[datetime, float]]] | None,
    tomorrow_sources: list[list[tuple[datetime, float]]] | None,
    now: datetime,
    *,
    step_h: float = 1.0,
) -> list[tuple[datetime, float]]:
    """Build a PV power curve from raw sub-hourly watts samples, per source.

    ``today_sources``/``tomorrow_sources`` are each a list of per-source sample
    arrays (one array per PV entity, already converted to UTC by the coordinator
    reader) or None/[] if unavailable.  Each source is resampled to ``step_h``-wide
    UTC buckets by taking the ARITHMETIC MEAN of ITS OWN samples whose timestamp
    falls in [bucket, bucket+step_h) — INDEPENDENTLY of other sources — and only
    THEN are the per-bucket means SUMMED across sources (H2). This avoids diluting
    a coarse-cadence (e.g. hourly) source when it is pooled with a finer-cadence
    (e.g. 30-min) source before averaging. Drops buckets strictly before ``now``'s
    bucket floor. Returns a sorted list of (datetime_utc, watts_summed) with one
    entry per bucket between the first and last kept bucket (gaps filled with 0.0
    so the timeline stays contiguous). Returns [] when all inputs are None/empty.

    ``step_h`` drives bucket width (1.0h default; 0.25h for 15-min).  At
    ``step_h=1.0`` this reduces byte-identically to the legacy hourly bucketing
    for a single hourly-cadence source.
    """
    sources: list[list[tuple[datetime, float]]] = []
    for group in (today_sources, tomorrow_sources):
        if group:
            for src in group:
                if src:
                    sources.append(src)
    if not sources:
        return []

    step_min = max(1, round(step_h * 60))

    def _floor(t: datetime) -> datetime:
        minute = (t.minute // step_min) * step_min
        return t.replace(minute=minute, second=0, microsecond=0)

    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now_h = _floor(now.astimezone(timezone.utc).replace(tzinfo=timezone.utc))

    # Resample EACH source to step_h buckets (mean within bucket) INDEPENDENTLY,
    # then sum the per-bucket means across sources. A coarse (hourly) source keeps
    # its full value; a fine (30-min) source averages within its hour — the two
    # then add, instead of pooling raw samples and diluting the coarse source.
    summed: dict[datetime, float] = {}
    for src in sources:
        buckets: dict[datetime, list[float]] = {}
        for dt, w in src:
            dt_utc = (dt.astimezone(timezone.utc).replace(tzinfo=timezone.utc)
                      if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc))
            bucket = _floor(dt_utc)
            if bucket < now_h:
                continue
            buckets.setdefault(bucket, []).append(w)
        for bucket, ws in buckets.items():
            summed[bucket] = summed.get(bucket, 0.0) + sum(ws) / len(ws)

    # Fill every missing bucket between first and last kept bucket with 0.0.
    # This ensures the returned curve is CONTIGUOUS so downstream consumers that
    # iterate bucket-by-bucket (build_intervals gap math, ride-out reserve) see a
    # continuous timeline — no multi-bucket holes even for daylight-only source data.
    if not summed:
        return []
    hour_keys = sorted(summed)
    out: list[tuple[datetime, float]] = []
    h = hour_keys[0]
    while h <= hour_keys[-1]:
        out.append((h, summed.get(h, 0.0)))
        h += timedelta(hours=step_h)
    return out


def build_two_day_pv_curve(
    today_arrays: list[tuple[float, datetime | None]] | None,
    tomorrow_arrays: list[tuple[float, datetime | None]] | None,
    now: datetime,
    today_sunset: datetime | None,
    tomorrow_sunrise: datetime | None,
    tomorrow_sunset: datetime | None,
    *,
    step_h: float = 1.0,
) -> list[tuple[datetime, float]]:
    """Hourly peaked PV curve for today's remainder + tomorrow, per array.

    Each segment is skipped when its arrays list is falsy/None or its daylight
    window bounds are missing.
    """
    curve: list[tuple[datetime, float]] = []
    today_curve: list[tuple[datetime, float]] = []
    if today_arrays and today_sunset is not None:
        today_curve = build_pv_curve_from_arrays(today_arrays, now, today_sunset, step_h=step_h)
        curve.extend(today_curve)
    # Fill the overnight gap [max(today_sunset, now), tomorrow_sunrise) with
    # hour-aligned pv=0 grid points so build_intervals produces accurate per-hour
    # overnight intervals (load = predictor P50, pv = 0) covering the WHOLE night —
    # NOT only from sunrise.  Gated on a present tomorrow segment (a no-solar system
    # still gets no fill).  Works even when `now` is already past today_sunset (today
    # segment empty): the fill simply starts at `now`'s hour boundary.
    if (
        today_sunset is not None
        and tomorrow_arrays  # truthy: non-None AND non-empty; [] skips fill
        and tomorrow_sunrise is not None
        and tomorrow_sunset is not None
        and today_sunset < tomorrow_sunrise
    ):
        # Snap to the hour boundary at/after the gap start so the first fill point
        # abuts today's last curve point (if any) with neither gap nor overlap.
        # NOTE: the snap and stride both assume step_h=1.0 (the only value callers pass).
        gap_start = max(today_sunset, now)
        fill = gap_start.replace(minute=0, second=0, microsecond=0)
        if fill < gap_start:
            fill += timedelta(hours=1)
        while fill < tomorrow_sunrise:
            curve.append((fill, 0.0))
            fill += timedelta(hours=step_h)
    if tomorrow_arrays is not None and tomorrow_sunrise is not None and tomorrow_sunset is not None:
        curve.extend(
            build_pv_curve_from_arrays(
                tomorrow_arrays, tomorrow_sunrise, tomorrow_sunset, step_h=step_h
            )
        )
    return curve
