"""Pure parsers: Zonneplan price curve and PV forecast curve."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone, UTC

from . import const
from .models import PriceSlot
from .resolution import hour_floor


def _parse_dt(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


# Disjoint per-entry key sets — sniffing is unambiguous.
_ZONNEPLAN_START = "datetime"
_ZONNEPLAN_PRICE = "electricity_price"
_FRANK_START = "from"
_FRANK_PRICE = "price"
_ZONNEPLAN_Q_START = "start_date"
_ZONNEPLAN_Q_PRICE = "price_tax_included"
_ZONNEPLAN_Q_AMOUNT = "amount"


def _decode_price_entry(entry: dict) -> tuple[datetime, float] | None:
    """Decode one forecast entry to (start_utc, price €/kWh), or None.

    Zonneplan (hourly) — ``{"datetime": ISO, "electricity_price": int}``, price ÷ PRICE_SCALE.
    Frank Energie — ``{"from": ISO, "price": float}``, price already €/kWh.
    Zonneplan (quarter-hourly) — ``{"start_date": ISO, "price_tax_included": {"amount": int}}``,
    price ÷ PRICE_SCALE (tax-included matches the legacy ``electricity_price`` semantics).
    Unrecognised / malformed / non-finite entries return None so the caller skips them.
    """
    if _ZONNEPLAN_START in entry and _ZONNEPLAN_PRICE in entry:
        start = _parse_dt(entry.get(_ZONNEPLAN_START))
        raw = entry.get(_ZONNEPLAN_PRICE)
        scale = const.PRICE_SCALE
    elif _FRANK_START in entry and _FRANK_PRICE in entry:
        start = _parse_dt(entry.get(_FRANK_START))
        raw = entry.get(_FRANK_PRICE)
        scale = 1.0
    elif _ZONNEPLAN_Q_START in entry and _ZONNEPLAN_Q_PRICE in entry:
        start = _parse_dt(entry.get(_ZONNEPLAN_Q_START))
        price_obj = entry.get(_ZONNEPLAN_Q_PRICE)
        raw = price_obj.get(_ZONNEPLAN_Q_AMOUNT) if isinstance(price_obj, dict) else None
        scale = const.PRICE_SCALE
    else:
        return None
    if start is None or raw is None:
        return None
    try:
        price = float(raw) / scale
    except (ValueError, TypeError):
        return None
    if not math.isfinite(price):
        return None  # "NaN"/"Infinity" parse as float; never let them reach the DP
    return start, price


def parse_price_curve(forecast_attr: list[dict] | None) -> list[PriceSlot]:
    """Map a price-sensor forecast attribute to sorted PriceSlots (price in €/kWh).

    Accepts the Zonneplan hourly (``datetime``/``electricity_price``, integer-scaled),
    Frank Energie (``from``/``price``, plain €/kWh), and Zonneplan quarter-hourly
    (``start_date``/``price_tax_included.amount``, integer-scaled) entry shapes; the
    key sets are disjoint so per-entry sniffing is unambiguous.

    Duplicate starts are collapsed **keep-first** — the HiDiHo01 ``frank_energie``
    integration publishes every slot exactly twice.  Without the collapse the
    consecutive-gap duration derivation below yields 0-minute slots and
    ``resolution.detect_slot_minutes`` mis-reads the resolution.  The sort is
    stable, so "first" means first in the source list among equal starts.
    """
    if not isinstance(forecast_attr, list):
        return []
    slots: list[PriceSlot] = []
    for entry in forecast_attr:
        if not isinstance(entry, dict):
            continue
        decoded = _decode_price_entry(entry)
        if decoded is None:
            continue
        slots.append(PriceSlot(decoded[0], decoded[1]))
    slots.sort(key=lambda s: s.start)
    deduped: list[PriceSlot] = []
    for s in slots:
        if deduped and s.start == deduped[-1].start:
            continue
        deduped.append(s)
    slots = deduped
    if len(slots) < 2:
        return slots
    durations = [(slots[i + 1].start - slots[i].start).total_seconds() / 60.0 for i in range(len(slots) - 1)]
    durations.append(durations[-1])
    return [PriceSlot(s.start, s.price, duration_min=d) for s, d in zip(slots, durations)]


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
            w = math.sin(
                (math.pi / 2) * max(0.0, (end - t) / (end - p))
            )  # quarter-sine fall; clamp past-end center -> 0
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
    bucket floor from the EMITTED curve (see the sample-and-hold paragraph below
    for why a dropped bucket can still count towards gap math). Returns a sorted
    list of (datetime_utc, watts_summed) with one entry per bucket between the
    first and last kept bucket (any bucket still empty after the per-source hold
    below is filled with 0.0, so the timeline stays contiguous). Returns [] when
    all inputs are None/empty.

    ``step_h`` drives bucket width (1.0h default; 0.25h for 15-min).  At
    ``step_h=1.0`` this reduces byte-identically to the legacy hourly bucketing
    for a single hourly-cadence source (the sample-and-hold fill below can never
    fire at step_h=1.0: the next candidate bucket is always exactly 1h away,
    which fails the strict "< 1h" gate).

    Sample-and-hold gap fill (per source, BEFORE cross-source summing): a coarser-
    than-``step_h`` cadence (e.g. a 30-min source resampled at step_h=0.25) leaves
    real buckets with in-between holes. An empty bucket strictly between two real
    buckets of the SAME source inherits the previous real bucket's value iff that
    previous real bucket is < 1h older; at/beyond 1h it stays 0.0 (left for the
    contiguity fill below). After a source's LAST real bucket, the value is
    mirrored forward for the same duration as the final inter-sample gap (capped
    at < 1h; a source with only one real bucket, having no gap to mirror, falls
    back to the flat < 1h cap) — this avoids downstream code doubling/halving
    energy when it iterates bucket-by-bucket. That final-gap lookup uses the
    source's FULL (unfiltered) raw-sample history, not just the emitted (>= now_h)
    buckets: otherwise a genuinely multi-sample source whose second-to-last
    sample rolled before ``now`` would look like a lone sample and wrongly take
    the flat-1h fallback instead of correctly seeing its real (possibly >= 1h,
    tail-suppressing) gap. Leading buckets before a source's first sample stay
    0.0 (no look-back).
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
    step = timedelta(hours=step_h)
    hour_cap = timedelta(hours=1)

    def _floor(t: datetime) -> datetime:
        minute = (t.minute // step_min) * step_min
        return t.replace(minute=minute, second=0, microsecond=0)

    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    now_h = _floor(now.astimezone(UTC).replace(tzinfo=UTC))

    # Resample EACH source to step_h buckets (mean within bucket) INDEPENDENTLY,
    # then sample-and-hold fill THIS source's own empty buckets (see docstring),
    # then sum the per-bucket (real-or-held) values across sources. A coarse
    # (hourly) source keeps its full value; a fine (30-min) source averages
    # within its hour — the two then add, instead of pooling raw samples and
    # diluting the coarse source.
    summed: dict[datetime, float] = {}
    for src in sources:
        # Bucket ALL raw samples first (no now_h filtering yet) so a real bucket
        # that ends up dropped from the EMITTED curve can still serve as the
        # true "previous real bucket" for the tail's gap math below — otherwise
        # a multi-sample source whose second-to-last sample rolled before `now`
        # would look like a lone sample (see docstring / task-1 review finding).
        all_buckets: dict[datetime, list[float]] = {}
        for dt, w in src:
            dt_utc = dt.astimezone(UTC).replace(tzinfo=UTC) if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
            bucket = _floor(dt_utc)
            all_buckets.setdefault(bucket, []).append(w)
        if not all_buckets:
            continue
        all_real = {bucket: sum(ws) / len(ws) for bucket, ws in all_buckets.items()}
        all_keys = sorted(all_real)
        all_index = {k: i for i, k in enumerate(all_keys)}

        # EMITTED buckets — same now_h drop as before, applied at the bucket
        # (not per-sample) level; identical result, since a sample's own bucket
        # is what the old per-sample check compared against `now_h` too.
        keys = [k for k in all_keys if k >= now_h]
        if not keys:
            continue
        real = {k: all_real[k] for k in keys}
        held: dict[datetime, float] = dict(real)
        n = len(keys)
        for i, t in enumerate(keys):
            value = real[t]
            if i + 1 < n:
                # Interior gap: hold forward while strictly < 1h old, capped by
                # the next real bucket (whichever comes first).
                next_t = keys[i + 1]
                limit = t + hour_cap
                b = t + step
                while b < next_t and b < limit:
                    held[b] = value
                    b += step
            else:
                # Tail after the source's LAST real bucket: mirror the final
                # observed inter-sample gap (only if it was < 1h); a lone sample
                # (no previous gap to mirror) falls back to the flat 1h cap.
                # The "previous" bucket is looked up in the FULL (unfiltered)
                # sequence so a true predecessor that rolled before now_h still
                # counts — it's just never itself emitted.
                ai = all_index[t]
                gap = t - all_keys[ai - 1] if ai > 0 else None
                if gap is not None and gap >= hour_cap:
                    continue
                limit = t + (gap if gap is not None else hour_cap)
                b = t + step
                while b < limit:
                    held[b] = value
                    b += step
        for bucket, value in held.items():
            summed[bucket] = summed.get(bucket, 0.0) + value

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
        fill = hour_floor(gap_start)
        if fill < gap_start:
            fill += timedelta(hours=1)
        while fill < tomorrow_sunrise:
            curve.append((fill, 0.0))
            fill += timedelta(hours=step_h)
    if tomorrow_arrays is not None and tomorrow_sunrise is not None and tomorrow_sunset is not None:
        curve.extend(build_pv_curve_from_arrays(tomorrow_arrays, tomorrow_sunrise, tomorrow_sunset, step_h=step_h))
    return curve
