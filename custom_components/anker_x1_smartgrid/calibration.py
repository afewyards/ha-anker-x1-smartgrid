"""Periodic full-charge calibration policy — pure decision logic.

Design: docs/superpowers/specs/2026-08-03-battery-calibration-policy-design.md

The pack strands ~3.6 kWh below ~21% SoC (measured 2026-08-03) and has had no
opportunity to top-balance: on 2026-08-02 it reached 99% and then held 0 W for
2.5 h while PV was exported.  This module decides when to drive the pack to the
top of its range and dwell there so the module BMSs get taper current.

No HA imports, no I/O, no clock reads — ``now`` is always a parameter.  A
completed cycle is READ BACK from SoC history rather than stored, so there is
no new table, no Store, and the policy is restart-safe by construction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .models import Config, PriceSlot

# Two adjacent samples further apart than this do not belong to the same run —
# otherwise an HA outage spanning a high-SoC period fakes a completed dwell.
MAX_SAMPLE_GAP_MIN: float = 15.0


@dataclass(frozen=True)
class CalibAction:
    """An active calibration slot.  ``phase`` is for reporting only —
    both phases actuate identically (FORCING at max rate; the BMS taper
    turns that into a hold once the pack is full)."""

    phase: str  # "charging" | "holding"
    window_start: datetime
    window_end: datetime


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def history_span_days(soc_samples: list[tuple[str, float]]) -> float:
    """Wall-clock days covered by the sample series (0.0 when < 2 rows).

    Precondition: ``soc_samples`` is ascending by timestamp, as guaranteed by
    ``recorder.read_soc_samples``'s ``ORDER BY ts ASC``. This computes
    ``samples[-1] - samples[0]``, so an out-of-order series would silently
    yield a wrong (even negative) span.
    """
    if len(soc_samples) < 2:
        return 0.0
    return (_parse(soc_samples[-1][0]) - _parse(soc_samples[0][0])).total_seconds() / 86400.0


def last_success_end(
    soc_samples: list[tuple[str, float]],
    *,
    top_soc: float,
    dwell_h: float,
) -> datetime | None:
    """End timestamp of the most recent completed calibration dwell.

    A dwell is a maximal block of consecutive samples at/above ``top_soc``
    with no adjacent gap over ``MAX_SAMPLE_GAP_MIN``, spanning at least
    ``dwell_h``.  Returns the block's LAST timestamp, so an in-progress hold
    keeps the clock at ~now and the policy goes idle as soon as it qualifies.

    Returns None when no block qualifies — including an empty series.

    Precondition: ``soc_samples`` is ascending by timestamp, as guaranteed by
    ``recorder.read_soc_samples``'s ``ORDER BY ts ASC``. Duplicate timestamps
    are benign (zero delta, run continues). An out-of-order series is not
    guarded against here: a ``ts`` preceding the open run's ``run_end`` makes
    ``ts - run_end`` negative, which is always ``<= max_gap``, so the run
    would silently keep extending backward and corrupt both the run's
    duration and the most-recent-wins result.
    """
    best: datetime | None = None
    run_start: datetime | None = None
    run_end: datetime | None = None
    max_gap = timedelta(minutes=MAX_SAMPLE_GAP_MIN)
    need = timedelta(hours=dwell_h)

    for ts_s, soc in soc_samples:
        ts = _parse(ts_s)
        if soc >= top_soc and run_end is not None and ts - run_end <= max_gap:
            run_end = ts
            continue
        # Close the open run (if any) before starting a new one.
        if run_start is not None and run_end is not None and run_end - run_start >= need:
            best = run_end
        if soc >= top_soc:
            run_start, run_end = ts, ts
        else:
            run_start, run_end = None, None

    if run_start is not None and run_end is not None and run_end - run_start >= need:
        best = run_end
    return best


def price_percentile(price_history: dict[str, dict[str, float]], pct: float) -> float | None:
    """Linear-interpolated percentile of every slot price in the history ring.

    Returns None for an empty history — the caller must then refuse the
    percentile path and let only the deadline path fire.
    """
    values = sorted(v for day in price_history.values() for v in day.values())
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    pos = (pct / 100.0) * (len(values) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (pos - lo)


def _charge_h(soc_pct: float, cfg: Config) -> float:
    """Hours to lift SoC from ``soc_pct`` to the calibration top, at max rate."""
    gap_kwh = max(0.0, (cfg.calibration_top_soc - soc_pct) / 100.0 * cfg.capacity_kwh)
    rate_kw = cfg.max_charge_w / 1000.0 * cfg.eta_charge_safe()
    if rate_kw <= 0.0:
        return 0.0
    return gap_kwh / rate_kw


# Tolerance for treating two chronologically-adjacent slots as truly
# contiguous. Guards against float/second rounding in stored timestamps
# without letting a REAL price-curve gap be silently spanned as if it were
# elapsed time — mirrors the gap-awareness MAX_SAMPLE_GAP_MIN already gives
# the SoC-history path above, just for the price-slot path.
_CONTIGUITY_TOLERANCE = timedelta(minutes=1.0)


def _slot_duration_min(slot: PriceSlot) -> float:
    """This slot's own duration in minutes.

    Falls back to 60.0 for a missing (``None``), zero, or invalid (negative)
    duration — ``duration_min or 60.0`` alone would NOT catch a negative
    value (a negative number is truthy), so the sign is checked explicitly.
    """
    dur = slot.duration_min
    return dur if dur and dur > 0.0 else 60.0


def select_window(
    now: datetime,
    soc_pct: float,
    slots: list[PriceSlot],
    *,
    cfg: Config,
    bar: float | None,
    force: bool,
) -> tuple[datetime, datetime] | None:
    """Cheapest acceptable contiguous window, or None.

    Deterministic in (now, slots, soc, cfg, bar, force): published prices do
    not change within a day, so re-running each tick yields the same answer
    and no commitment needs storing.
    """
    if not slots:
        return None
    need_min = (_charge_h(soc_pct, cfg) + cfg.calibration_dwell_h) * 60.0
    ordered = sorted(slots, key=lambda s: s.start)

    # Build candidates: variable-length runs of REAL, chronologically-adjacent
    # slots — each contributing its OWN duration_min, never a single width
    # sampled once and extrapolated — whose summed duration covers `need_min`
    # and that have not fully elapsed. A real gap between two slots forecloses
    # spanning it (see _CONTIGUITY_TOLERANCE above): the price curve can mix
    # cadences (e.g. hourly slots followed by 15-min slots, or a genuine
    # missing-data hole), and neither may be papered over with arithmetic.
    candidates: list[tuple[float, datetime, datetime]] = []
    for i in range(len(ordered)):
        acc_min = 0.0
        weighted_price = 0.0
        prev_end: datetime | None = None
        end: datetime | None = None
        for slot in ordered[i:]:
            if prev_end is not None and abs(slot.start - prev_end) > _CONTIGUITY_TOLERANCE:
                break  # real discontinuity in the price curve — do not span it
            dur_min = _slot_duration_min(slot)
            acc_min += dur_min
            weighted_price += slot.price * dur_min
            prev_end = slot.start + timedelta(minutes=dur_min)
            if acc_min >= need_min:
                end = prev_end
                break
        if end is None:
            continue  # ran out of slots, or hit a gap, before covering need_min
        start = ordered[i].start
        if end <= now:
            continue
        mean_price = weighted_price / acc_min
        candidates.append((mean_price, start, end))
    if not candidates:
        return None

    # One candidate per local start-date (the cheapest) => one attempt per day.
    per_day: dict[object, tuple[float, datetime, datetime]] = {}
    for cand in candidates:
        key = cand[1].date()
        if key not in per_day or cand[0] < per_day[key][0]:
            per_day[key] = cand

    # Earliest date with an acceptable window wins.  Taking the EARLIEST rather
    # than the globally cheapest is what stops the 13:00 publication of
    # tomorrow's prices from pulling a cycle off a today window that already
    # qualified — the "never abandon a started window" rule, expressed without
    # storing any commitment.
    for key in sorted(per_day):
        mean_price, start, end = per_day[key]
        if force or (bar is not None and mean_price <= bar):
            return (start, end)
    return None
