"""Midpoint-anchored linear resampling of period-mean series (pure, no HA imports)."""

from __future__ import annotations

import bisect
from datetime import datetime, timedelta


class MidpointLinear:
    """Resample a period-mean series onto arbitrary instants.

    ``points`` is ``[(period_start, value)]`` where ``value`` is the MEAN over
    that point's own period.  Each point is anchored at its period CENTER
    (``start + width/2``); ``width`` is the gap to the next point, the last
    point of a run mirrors the previous gap, and a lone point uses
    ``default_width_h``.  Between anchors the value is linearly interpolated;
    outside a run's anchor span the nearest edge anchor is held FLAT (never
    extrapolated — so a non-negative input can never yield a negative output,
    and no generation is invented before a source's first sample).

    Points more than ``max_gap_h`` apart do not interpolate across each other:
    the series splits into RUNS at those gaps and each run is flat-clamped at
    its own edges.  This keeps a data outage from being smeared into a ramp
    between two unrelated samples.  The split is at ``> max_gap_h``, so an
    exactly-hourly source (gap == 1h) still forms one run.

    Anchoring at period centers rather than left edges is what keeps the
    resampled series free of temporal shift, and makes ``at()`` an EXACT
    identity when queried at a point's own anchor — i.e. whenever the output
    grid width equals the input cadence.  That identity is what preserves the
    hourly / ``slot_minutes=60`` behaviour byte-for-byte.

    All datetimes must share tz-awareness (callers pass UTC-aware values).
    """

    def __init__(
        self,
        points: list[tuple[datetime, float]],
        *,
        max_gap_h: float = 1.0,
        default_width_h: float = 1.0,
    ) -> None:
        ordered = sorted(points, key=lambda p: p[0])
        max_gap = timedelta(hours=max_gap_h)
        default_width = timedelta(hours=default_width_h)
        runs: list[list[tuple[datetime, float]]] = []
        for i, point in enumerate(ordered):
            if runs and point[0] - ordered[i - 1][0] <= max_gap:
                runs[-1].append(point)
            else:
                runs.append([point])
        # Anchor each point at the centre of its own period.
        self._runs: list[tuple[list[datetime], list[float]]] = []
        for run in runs:
            times: list[datetime] = []
            values: list[float] = []
            for i, (start, value) in enumerate(run):
                if i + 1 < len(run):
                    width = run[i + 1][0] - start
                elif len(run) > 1:
                    width = start - run[i - 1][0]  # mirror the run's final gap
                else:
                    width = default_width
                times.append(start + width / 2)
                values.append(value)
            self._runs.append((times, values))

    def at(self, when: datetime) -> float | None:
        """Value at ``when``; ``None`` only when the series is empty.

        The nearest run (by distance to its anchor span, 0 when inside) answers
        the query.  Callers gate which instants they ask about, so a query
        landing between two runs is already excluded by their own coverage
        rules; nearest-run keeps this total rather than returning ``None``.
        """
        if not self._runs:
            return None
        times, values = min(self._runs, key=lambda run: _distance(run[0], when))
        if when <= times[0]:
            return values[0]
        if when >= times[-1]:
            return values[-1]
        i = bisect.bisect_right(times, when)
        t0, t1 = times[i - 1], times[i]
        v0, v1 = values[i - 1], values[i]
        span = (t1 - t0).total_seconds()
        if span <= 0:
            return v0
        return v0 + (when - t0).total_seconds() / span * (v1 - v0)


def _distance(times: list[datetime], when: datetime) -> float:
    """Seconds from ``when`` to a run's anchor span (0.0 when inside it)."""
    if when < times[0]:
        return (times[0] - when).total_seconds()
    if when > times[-1]:
        return (when - times[-1]).total_seconds()
    return 0.0
