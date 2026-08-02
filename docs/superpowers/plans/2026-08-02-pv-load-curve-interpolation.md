# PV + Load Curve Interpolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the piecewise-constant PV and load curves with midpoint-anchored linear interpolation, and fix the four call sites that build the PV curve at hourly resolution regardless of the live slot width — so the card and the DP both see a continuous curve instead of a staircase.

**Architecture:** One new pure module (`interp.py`) holds a `MidpointLinear` resampler: each input point is the MEAN over its own period, anchored at that period's CENTER, linearly interpolated between anchors, flat-clamped at run edges, with runs split across gaps > 1 h. `parsers.build_pv_curve_from_watts` keeps its emission logic byte-for-byte and only re-derives bucket VALUES through the resampler. `plan.build_display_intervals` does the same for the hourly load model. Because a query at a period center returns that period's own value exactly, every hourly-source / `slot_minutes=60` path is an exact identity.

**Tech Stack:** Python 3.12, pytest (`asyncio_mode=auto`), ruff (line-length 120), Home Assistant custom component. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-02-pv-load-curve-interpolation-design.md`

## Global Constraints

- Helper module must be **pure** — no `homeassistant` imports (same rule as `resolution.py`, `parsers.py`, `plan.py`).
- **Deviation from the spec, already agreed:** the helper lives in a new `interp.py`, not `resolution.py` (whose stated scope is "pure price-slot resolution detection"). Task 5 amends the spec text.
- **Deviation from the spec, already agreed:** the load resampler anchors ONLY on hours that actually have emitted slots — it does NOT probe `predict(hour + 1h)`. Probing past the horizon risks ramping the final hour toward a generic fallback value. Consequence: the last hour's `:30`/`:45` quarters stay flat at that hour's value. Task 5 amends the spec text.
- **Identity rules — any violation is a regression, not an intended change:**
  - `slot_minutes=60` output must be byte-identical everywhere.
  - A PV source whose cadence equals the bucket width must be byte-identical (bucket center == anchor).
- Ruff must stay clean: `.venv/bin/python -m ruff check custom_components tests`.
- No merge commits; commit after every task.
- Max 5 files touched per task.
- Per the user's agent policy, test runs are delegated to a **test-runner** subagent — do not run the suite inline in an engineer agent.
- No new config option. Rollback is `git revert`.

---

### Task 1: `MidpointLinear` resampler

**Files:**
- Create: `custom_components/anker_x1_smartgrid/interp.py`
- Test: `tests/test_interp.py`

**Interfaces:**
- Consumes: nothing (leaf module).
- Produces: `MidpointLinear(points: list[tuple[datetime, float]], *, max_gap_h: float = 1.0, default_width_h: float = 1.0)` with method `at(when: datetime) -> float | None`. Used by Task 2 (`parsers.py`) and Task 4 (`plan.py`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_interp.py`:

```python
from datetime import datetime, timedelta, UTC

import pytest

from custom_components.anker_x1_smartgrid.interp import MidpointLinear


def _t(h: int, m: int = 0, s: int = 0) -> datetime:
    return datetime(2026, 8, 2, h, m, s, tzinfo=UTC)


def test_query_at_period_center_is_identity():
    """The property the whole design rests on: querying a point's own period
    center returns that point's value EXACTLY (no float drift), which is what
    keeps slot_minutes=60 and cadence==bucket-width byte-identical."""
    points = [(_t(10), 1200.0), (_t(11), 800.0), (_t(12), 300.0)]
    r = MidpointLinear(points)
    assert r.at(_t(10, 30)) == 1200.0
    assert r.at(_t(11, 30)) == 800.0
    assert r.at(_t(12, 30)) == 300.0


def test_interpolates_between_anchors():
    r = MidpointLinear([(_t(10), 1200.0), (_t(11), 800.0)])
    # 10:45 is 25% of the way from anchor 10:30 to anchor 11:30
    assert r.at(_t(10, 45)) == pytest.approx(1100.0)
    assert r.at(_t(11, 0)) == pytest.approx(1000.0)


def test_flat_hold_before_first_and_after_last_anchor():
    """No extrapolation: a non-negative input can never produce a negative
    output, and no generation is invented before a source's first sample."""
    r = MidpointLinear([(_t(10), 1200.0), (_t(11), 800.0)])
    assert r.at(_t(9, 0)) == 1200.0
    assert r.at(_t(10, 15)) == 1200.0
    assert r.at(_t(12, 0)) == 800.0


def test_30min_cadence_anchors_at_quarter_past():
    """A 30-min series anchors at :15/:45, so 15-min bucket centers
    (:07.5/:22.5/:37.5/:52.5) straddle them."""
    r = MidpointLinear([(_t(11, 0), 386.0), (_t(11, 30), 2145.0)])
    assert r.at(_t(11, 15)) == 386.0   # anchor of the first point
    assert r.at(_t(11, 45)) == 2145.0  # anchor of the last point (mirrored width)
    assert r.at(_t(11, 22, 30)) == pytest.approx(825.75)
    assert r.at(_t(11, 37, 30)) == pytest.approx(1705.25)


def test_runs_split_across_gaps_larger_than_max_gap():
    """A 3h hole must not become a ramp: each side is its own run, flat-clamped."""
    r = MidpointLinear([(_t(11), 1200.0), (_t(14), 500.0)])
    assert r.at(_t(11, 7, 30)) == 1200.0
    assert r.at(_t(14, 7, 30)) == 500.0


def test_exact_1h_gap_does_not_split():
    """Hourly sources (Open-Meteo, France) must still interpolate — the split
    is at gap > max_gap_h, not >=."""
    r = MidpointLinear([(_t(11), 1200.0), (_t(12), 800.0)])
    assert r.at(_t(11, 45)) == pytest.approx(1100.0)


def test_single_point_is_flat_everywhere():
    r = MidpointLinear([(_t(11), 100.0)])
    assert r.at(_t(9)) == 100.0
    assert r.at(_t(11, 7, 30)) == 100.0
    assert r.at(_t(15)) == 100.0


def test_empty_points_returns_none():
    assert MidpointLinear([]).at(_t(11)) is None


def test_unsorted_input_is_sorted():
    r = MidpointLinear([(_t(11), 800.0), (_t(10), 1200.0)])
    assert r.at(_t(10, 30)) == 1200.0
    assert r.at(_t(11, 30)) == 800.0


def test_irregular_cadence_uses_per_interval_widths():
    """15-min then 30-min spacing: each point's anchor uses ITS OWN period."""
    r = MidpointLinear([(_t(11, 0), 100.0), (_t(11, 15), 200.0), (_t(11, 45), 400.0)])
    assert r.at(_t(11, 7, 30)) == 100.0    # anchor of point 0 (width 15 min)
    assert r.at(_t(11, 30)) == 200.0       # anchor of point 1 (width 30 min)
    assert r.at(_t(12, 0)) == 400.0        # anchor of point 2 (width mirrors 30 min)
```

- [ ] **Step 2: Run the tests to verify they fail**

Delegate to a test-runner subagent:
`.venv/bin/python -m pytest tests/test_interp.py -v`
Expected: collection error — `ModuleNotFoundError: No module named 'custom_components.anker_x1_smartgrid.interp'`.

- [ ] **Step 3: Write the implementation**

Create `custom_components/anker_x1_smartgrid/interp.py`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

`.venv/bin/python -m pytest tests/test_interp.py -v`
Expected: 10 passed.

- [ ] **Step 5: Lint**

`.venv/bin/python -m ruff check custom_components/anker_x1_smartgrid/interp.py tests/test_interp.py`
Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add custom_components/anker_x1_smartgrid/interp.py tests/test_interp.py
git commit -m "feat(interp): midpoint-anchored linear resampler"
```

---

### Task 2: PV curve values via the resampler

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/parsers.py:192-335` (`build_pv_curve_from_watts`)
- Test: `tests/test_pv_watts.py:495-545` (update pinned hold expectations)
- Test: `tests/test_decision_pv_energy.py:134-219` (docstring + conservation tolerance + new exact-conservation test)

**Interfaces:**
- Consumes: `MidpointLinear` from Task 1.
- Produces: no signature change. `build_pv_curve_from_watts(...)` returns the same bucket timestamps as before; only the watt values change, and only when a source's cadence is coarser than `step_h`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_pv_watts.py`, replace the three hold-pinned tests (lines 495-506 and 524-545) with:

```python
def test_30min_source_step15_interpolates():
    """A 30-min source at 15-min buckets ramps instead of stepping.

    Anchors sit at the period centres 11:15 (386 W) and 11:45 (2145 W); the
    four bucket centres are 11:07:30 / 11:22:30 / 11:37:30 / 11:52:30, so the
    first and last clamp flat and the middle two interpolate.  Hour energy is
    UNCHANGED from the sample-and-hold version (the ramp is symmetric about
    the two anchors) — this is the same 1.2655 kWh the cadence-doubling fix
    pinned.
    """
    src = [(_sh(11, 0), 386.0), (_sh(11, 30), 2145.0)]
    curve = build_pv_curve_from_watts([src], None, _sh(11, 0), step_h=0.25)
    assert [w for _, w in curve] == pytest.approx([386.0, 825.75, 1705.25, 2145.0])
    assert sum(w for _, w in curve) * 0.25 / 1000 == pytest.approx(1.2655)


def test_hourly_source_step15_ramps():
    """An hourly source at 15-min buckets: flat until the first anchor (11:30),
    then a ramp toward the 12:30 anchor.  Gap == 1 h does NOT split the run."""
    src = [(_sh(11, 0), 1200.0), (_sh(12, 0), 800.0)]
    curve = build_pv_curve_from_watts([src], None, _sh(11, 0), step_h=0.25)
    assert [w for _, w in curve] == pytest.approx([1200.0, 1200.0, 1150.0, 1050.0, 950.0])


def test_cross_source_sum_after_interp():
    """Each source interpolates on ITS OWN cadence first, then the per-bucket
    values sum.  The lone-sample source stays flat (single anchor)."""
    a = [(_sh(11, 0), 386.0), (_sh(11, 30), 2145.0)]  # 30-min
    b = [(_sh(11, 0), 100.0)]  # lone sample
    curve = build_pv_curve_from_watts([a, b], None, _sh(11, 0), step_h=0.25)
    assert [w for _, w in curve] == pytest.approx([486.0, 925.75, 1805.25, 2245.0])


def test_tail_gap_uses_unfiltered_predecessor_not_false_lone_sample():
    """Fix-round-1 regression (task-1 review, Important finding), still pinned.

    The `now_h` drop filter must not blind the tail rule's gap computation: a
    genuinely multi-sample source whose second-to-last sample rolled before
    `now` must NOT look like a lone sample (which would wrongly take the flat
    1h fallback and fabricate held energy).  EMISSION is unchanged — exactly
    one bucket, no fabricated tail extension.

    The VALUE now interpolates against the pre-`now` 10:00 sample (anchors
    10:30=1200 and 11:30=800; the 11:00 bucket's centre 11:07:30 is 62.5% of
    the way across).  That is deliberate: anchors come from the source's FULL
    unfiltered history, so a bucket's value never depends on where `now` fell
    and the curve does not shift under the plan as the clock advances.
    """
    src = [(_sh(10, 0), 1200.0), (_sh(11, 0), 800.0)]
    curve = build_pv_curve_from_watts([src], None, _sh(11, 0), step_h=0.25)
    assert [t for t, _ in curve] == [_sh(11, 0)]
    assert curve[0][1] == pytest.approx(950.0)
```

Leave `test_hold_capped_at_1h_across_gap` and `test_step_1h_hourly_byte_identical` exactly as they are — both must still pass unchanged (run-splitting at gaps > 1 h reproduces the hold behaviour there).

Add one new test after them:

```python
def test_hourly_source_step60_byte_identical_to_input():
    """Cadence == bucket width: every bucket centre IS its own anchor, so the
    resampler is an exact identity.  This is the invariant that protects every
    pre-15-min deployment."""
    src = [(_sh(h, 0), 100.0 * h) for h in range(9, 16)]
    curve = build_pv_curve_from_watts([src], None, _sh(9, 0), step_h=1.0)
    assert [w for _, w in curve] == [100.0 * h for h in range(9, 16)]
```

- [ ] **Step 2: Run the tests to verify they fail**

`.venv/bin/python -m pytest tests/test_pv_watts.py -v`
Expected: `test_30min_source_step15_interpolates`, `test_hourly_source_step15_ramps`, `test_cross_source_sum_after_interp` FAIL with the old hold values (e.g. `[386.0, 386.0, 2145.0, 2145.0]`); `test_tail_gap_uses_unfiltered_predecessor...` FAILS on `950.0 != 800.0`. The two untouched tests and `test_hourly_source_step60_byte_identical_to_input` PASS already.

- [ ] **Step 3: Write the implementation**

In `custom_components/anker_x1_smartgrid/parsers.py`, add the import near the other relative imports:

```python
from .interp import MidpointLinear
```

Then, inside `build_pv_curve_from_watts`'s per-source loop, immediately AFTER the existing interior/tail fill loop (`for i, t in enumerate(keys): ...`) and BEFORE `for bucket, value in held.items():`, insert:

```python
        # Values: the emission set above (which buckets exist) is unchanged —
        # only the VALUES are re-derived, by midpoint-anchored linear
        # interpolation.  Anchors come from the source's FULL unfiltered bucket
        # sequence (all_keys), not just the emitted ones, so a bucket's value
        # never depends on where `now` fell — the curve does not shift under
        # the plan as the clock advances.  Runs split at gaps > 1h, which
        # reproduces the old hold behaviour across a data outage (each side is
        # flat-clamped) while still ramping an exactly-hourly source.  When a
        # source's cadence equals `step_h`, every bucket centre IS its own
        # anchor and this is a byte-exact identity.
        resampler = MidpointLinear([(t, all_real[t]) for t in all_keys])
        half = step / 2
        for bucket in held:
            value = resampler.at(bucket + half)
            if value is not None:
                held[bucket] = value
```

Update the function's docstring: replace the "Sample-and-hold gap fill" paragraph's final sentence with a pointer to the new value derivation, keeping the emission rules text intact. Append this paragraph:

```
    Bucket VALUES are then re-derived by midpoint-anchored linear interpolation
    (``interp.MidpointLinear``) over the source's full real-bucket sequence:
    each real bucket is the mean over its own period and is anchored at that
    period's centre, and every emitted bucket reads the interpolant at ITS own
    centre.  Which buckets are emitted is decided entirely by the hold rules
    above and is unchanged.  A source whose cadence equals ``step_h`` is a
    byte-exact identity (bucket centre == anchor); a coarser source ramps
    instead of stepping.  Runs split at gaps > 1h, so a data outage is
    flat-clamped on both sides rather than smeared into a ramp.
```

- [ ] **Step 4: Run the tests to verify they pass**

`.venv/bin/python -m pytest tests/test_pv_watts.py -v`
Expected: all pass.

- [ ] **Step 5: Run the DP-level PV energy tests**

`.venv/bin/python -m pytest tests/test_decision_pv_energy.py -v`
Expected: `test_30min_source_hour_energy_not_doubled` PASSES (1.2655 kWh is preserved by the symmetric ramp). `test_window_pv_energy_conserved_across_slot_minutes` FAILS: `total_15` is 13.80625 vs `total_60` 13.8.

- [ ] **Step 6: Fix the conservation test and pin the exact-conservation case**

The failure is real and understood, not a bug: the fixture is a monotone ramp `0, 50, … 1150 W` that is still rising at the window edge. Interpolation moves energy across bucket boundaries; the imbalance telescopes to the window edges only, and equals `step_h/2 × (Δ_out − Δ_in)` where Δ is the per-step change at each edge — here `0.25/2 × 50 W = 6.25 Wh`. For any curve that is FLAT at both window edges (every real PV day: 0 W at night) the total is exact.

In `tests/test_decision_pv_energy.py`, change the two 15-min assertions in `test_window_pv_energy_conserved_across_slot_minutes` and extend its docstring:

```python
def test_window_pv_energy_conserved_across_slot_minutes():
    """Full synthetic day, single hourly-cadence PV source: total window_pv
    energy is (near-)identical whether the DP window ticks at slot_minutes=60
    or slot_minutes=15 -- resolution must not manufacture or destroy PV energy.

    The fixture is a monotone ramp that is still RISING at the window edge, so
    midpoint interpolation leaves a boundary residue of exactly
    step_h/2 * (delta_out - delta_in) = 0.25/2 * 50 W = 6.25 Wh (0.045% of the
    day).  Interior boundaries telescope out exactly.  A curve that is flat at
    both edges -- i.e. any real PV day, 0 W at night -- conserves exactly; that
    case is pinned by test_window_pv_energy_conserved_zero_ended_day below.
    """
```

with:

```python
    assert total_60 == pytest.approx(expected_kwh, abs=1e-6)
    assert total_15 == pytest.approx(expected_kwh, abs=0.007)
    assert total_60 == pytest.approx(total_15, abs=0.007)
```

Then append the exact-conservation test:

```python
_T3_BASE = datetime(2026, 8, 4, 0, 0, tzinfo=UTC)
# Realistic day: dark until 04:00Z, bell through the afternoon, dark from 20:00Z.
_BELL_WATTS = [0.0] * 4 + [200.0, 600.0, 1100.0, 1600.0, 2000.0, 2200.0, 2300.0, 2200.0,
                           2000.0, 1600.0, 1100.0, 600.0, 200.0] + [0.0] * 7


def test_window_pv_energy_conserved_zero_ended_day():
    """A PV day that is dark at both window edges conserves energy EXACTLY
    across slot_minutes -- the interpolation residue is a pure boundary term
    and both boundaries are flat here."""
    samples = [(_T3_BASE + timedelta(hours=h), _BELL_WATTS[h]) for h in range(24)]
    samples.append((_T3_BASE + timedelta(hours=24), 0.0))
    today_watts = [samples]

    slots_60 = _price_slots(_T3_BASE, 24, 60)
    total_60 = sum(_run_decision(_cfg(), now=_T3_BASE, slots=slots_60, slot_minutes=60,
                                 today_watts=today_watts)["args"][0])

    slots_15 = _price_slots(_T3_BASE, 96, 15)
    total_15 = sum(_run_decision(_cfg(), now=_T3_BASE, slots=slots_15, slot_minutes=15,
                                 today_watts=today_watts)["args"][0])

    assert total_60 == pytest.approx(sum(_BELL_WATTS) / 1000.0, abs=1e-9)
    assert total_15 == pytest.approx(total_60, abs=1e-9)
```

Also update `test_30min_source_hour_energy_not_doubled`'s docstring worked example from `[386, 386, 2145, 2145]` to `[386, 825.75, 1705.25, 2145]`, keeping the `1.2655 kWh` arithmetic line (it is unchanged) and the pre-fix `2.531` contrast.

- [ ] **Step 7: Run both test files to verify they pass**

`.venv/bin/python -m pytest tests/test_pv_watts.py tests/test_decision_pv_energy.py -v`
Expected: all pass.

- [ ] **Step 8: Lint and commit**

```bash
.venv/bin/python -m ruff check custom_components/anker_x1_smartgrid/parsers.py tests/test_pv_watts.py tests/test_decision_pv_energy.py
git add custom_components/anker_x1_smartgrid/parsers.py tests/test_pv_watts.py tests/test_decision_pv_energy.py
git commit -m "feat(parsers): interpolate PV curve buckets instead of holding"
```

---

### Task 3: pass `step_h` at the four call sites that dropped it

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/plan.py:489-502` (`build_display_horizon` curve build)
- Modify: `custom_components/anker_x1_smartgrid/decision.py:934-943` (`_wv_curve` fallback branches)
- Test: `tests/test_15min_pv_curve.py` (new display-horizon test)

**Interfaces:**
- Consumes: `build_pv_curve_from_watts` / `build_two_day_pv_curve` / `build_pv_curve_from_arrays` with the `step_h` keyword (all four already accept it).
- Produces: no signature change. `build_display_horizon` rows at `slot_minutes=15` now carry a distinct `pv_w` per quarter.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_15min_pv_curve.py`:

```python
def test_display_horizon_builds_watts_curve_on_the_slot_grid():
    """The display path dropped step_h, so it built the PV curve at 1.0h and
    fanned one value across all four quarters -- a coarser picture than the DP
    was optimizing on (live lab evidence 2026-08-02: 08:00/08:15/08:30/08:45
    all read pv_w 276.7995 from a 30-min source)."""
    from custom_components.anker_x1_smartgrid.models import Config
    from custom_components.anker_x1_smartgrid.plan import build_display_horizon

    base = datetime(2026, 8, 2, 11, 0, tzinfo=UTC)
    slots = [PriceSlot(base + timedelta(minutes=15 * i), 0.20) for i in range(4)]
    today_watts = [[(base, 386.0), (base + timedelta(minutes=30), 2145.0)]]
    sun_times = (
        base + timedelta(hours=8),   # today_sunset
        base + timedelta(hours=20),  # tomorrow_sunrise
        base + timedelta(hours=32),  # tomorrow_sunset
    )
    rows = build_display_horizon(
        slots,
        base,
        None,
        None,
        sun_times,
        _P(),
        20.0,
        300.0,
        50.0,
        [],
        base + timedelta(hours=1),
        Config(capacity_kwh=10.0, max_charge_w=3000.0, eta_charge=1.0),
        today_watts=today_watts,
        slot_minutes=15,
    )
    pv = [r["pv_w"] for r in rows]
    assert len(pv) == 4
    assert pv == pytest.approx([386.0, 825.75, 1705.25, 2145.0])
```

Add `import pytest` at the top of the file (it currently has none).

- [ ] **Step 2: Run the test to verify it fails**

`.venv/bin/python -m pytest tests/test_15min_pv_curve.py::test_display_horizon_builds_watts_curve_on_the_slot_grid -v`
Expected: FAIL — all four values equal (the hourly bucket mean, `1265.5`), not the ramp.

- [ ] **Step 3: Write the implementation**

In `plan.py`, replace lines 489-502 with:

```python
    if today_watts is not None or tomorrow_watts is not None:
        # Preferred path: real sub-hourly watts -> correct midday bell.  step_h
        # MUST be the live slot width: without it this built the curve at the
        # 1.0h default and build_display_intervals then fanned one hourly mean
        # across every quarter, so the card drew a coarser staircase than the
        # curve the DP was actually optimizing on (decision.py passes dt_h).
        curve = build_pv_curve_from_watts(today_watts, tomorrow_watts, now, step_h=slot_minutes / 60.0)
    else:
        # Fallback: synthetic quarter-sine from daily kWh totals (tests + degraded data).
        # Same slot-width rule as above, else the staircase returns whenever the
        # watts source drops out.
        curve = build_two_day_pv_curve(
            today_arrays,
            tomorrow_arrays,
            now,
            today_sunset,
            tomorrow_sunrise,
            tomorrow_sunset,
            step_h=slot_minutes / 60.0,
        )
```

In `decision.py`, replace lines 938-941 with:

```python
    elif sun_times is not None:
        _wv_curve = build_two_day_pv_curve(today_arrays, tomorrow_arrays, inputs.now, *sun_times, step_h=dt_h)
    elif today_arrays:
        _wv_curve = build_pv_curve_from_arrays(today_arrays, inputs.now, horizon_edge, step_h=dt_h)
```

- [ ] **Step 4: Run the test to verify it passes**

`.venv/bin/python -m pytest tests/test_15min_pv_curve.py -v`
Expected: all pass (the four pre-existing tests in this file are unaffected — their predictor is constant and their curves are already at bucket cadence).

- [ ] **Step 5: Lint and commit**

```bash
.venv/bin/python -m ruff check custom_components/anker_x1_smartgrid/plan.py custom_components/anker_x1_smartgrid/decision.py tests/test_15min_pv_curve.py
git add custom_components/anker_x1_smartgrid/plan.py custom_components/anker_x1_smartgrid/decision.py tests/test_15min_pv_curve.py
git commit -m "fix(plan): build the PV curve on the live slot grid, not hourly"
```

---

### Task 4: load curve values via the resampler

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/plan.py:13-93` (`build_display_intervals`)
- Test: `tests/test_15min_pv_curve.py:38-68` (update the D1 recording test) + new load tests

**Interfaces:**
- Consumes: `MidpointLinear` from Task 1; `hour_floor` / `floor_to_slot` (already imported in `plan.py`).
- Produces: no signature change. `build_display_intervals` now calls `predictor.predict` ONCE PER HOUR (not once per slot) and returns per-slot interpolated `load_w`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_15min_pv_curve.py`, replace `test_display_intervals_temp_lookup_stays_hour_floored_at_15min` (lines 38-68) with:

```python
def test_display_intervals_predicts_once_per_hour_with_hour_floored_temp():
    """D1 + D3: the temp forecast is intrinsically hourly, and so is the load
    model -- so the predictor is called ONCE per hour with that hour's own
    temp, and the four quarters are then interpolated from those hourly values
    (D3).  Pre-D3 this called predict() four times per hour with identical
    arguments and identical results."""
    base = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
    slots = [PriceSlot(base + timedelta(minutes=15 * i), 0.2) for i in range(4)]
    seen = {}

    class _RecordingPredictor:
        def predict(self, when, temp, fallback_w, *, quantile=0.5):
            seen[when] = temp
            return 300.0

    temp_by_hour = {base: 7.0}  # only the hour key (10:00) is present
    ivs = build_display_intervals(
        slots,
        base,
        [],
        _RecordingPredictor(),
        20.0,
        300.0,
        temp_by_hour=temp_by_hour,
        slot_minutes=15,
    )
    assert len(ivs) == 4
    assert seen == {base: 7.0}  # one call, the hour's own temp -- not cur_temp
    assert [iv.load_w for iv in ivs] == [300.0] * 4  # constant model -> constant output
```

Append two new tests to the same file:

```python
def test_display_intervals_interpolate_load_across_the_hour():
    """An hour-varying load model ramps across the quarters instead of
    stepping.  Anchors are the hour centres (10:30=400, 11:30=800), so the
    10:00 hour's quarters read 400/400/450/550 and the 11:00 hour's read
    650/750/800/800 (flat past the last anchor -- no probe beyond the
    horizon)."""
    base = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
    slots = [PriceSlot(base + timedelta(minutes=15 * i), 0.2) for i in range(8)]

    class _HourlyPredictor:
        def predict(self, when, temp, fallback_w, *, quantile=0.5):
            return 400.0 if when.hour == 10 else 800.0

    ivs = build_display_intervals(slots, base, [], _HourlyPredictor(), 20.0, 300.0, slot_minutes=15)
    assert [iv.load_w for iv in ivs] == pytest.approx(
        [400.0, 400.0, 450.0, 550.0, 650.0, 750.0, 800.0, 800.0]
    )


def test_display_intervals_load_identical_at_60min():
    """slot_minutes=60: the slot centre IS the hour anchor, so interpolation is
    an exact identity and hourly deployments are byte-unchanged."""
    base = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
    slots = [PriceSlot(base + timedelta(hours=i), 0.2) for i in range(3)]

    class _HourlyPredictor:
        def predict(self, when, temp, fallback_w, *, quantile=0.5):
            return 100.0 * when.hour

    ivs = build_display_intervals(slots, base, [], _HourlyPredictor(), 20.0, 300.0, slot_minutes=60)
    assert [iv.load_w for iv in ivs] == [1000.0, 1100.0, 1200.0]
```

- [ ] **Step 2: Run the tests to verify they fail**

`.venv/bin/python -m pytest tests/test_15min_pv_curve.py -v`
Expected: `test_display_intervals_predicts_once_per_hour_with_hour_floored_temp` FAILS (`seen` has four keys), `test_display_intervals_interpolate_load_across_the_hour` FAILS (`[400,400,400,400,800,800,800,800]`). `test_display_intervals_load_identical_at_60min` PASSES already.

- [ ] **Step 3: Write the implementation**

In `plan.py`, add to the imports:

```python
from .interp import MidpointLinear
```

Replace the body of `build_display_intervals` after `pv_sorted = sorted(...)` (i.e. lines 37-93) with:

```python
    pv_sorted = sorted(pv_curve, key=lambda p: p[0])
    pv_n = len(pv_sorted)
    now_h = floor_to_slot(now, slot_minutes)
    dt_h = slot_minutes / 60.0
    # Pass 1: the emitted slot grid (dedup + >= now filter), keeping each row's
    # ORIGINAL slot.start for the PV cursor below — flooring it would change the
    # D2 lookup for non-slot-aligned price starts.
    rows: list[tuple[datetime, datetime]] = []
    seen: set[datetime] = set()
    for slot in sorted(slots, key=lambda s: s.start):
        h = floor_to_slot(slot.start, slot_minutes)
        if h < now_h or h in seen:
            continue
        seen.add(h)
        rows.append((h, slot.start))
    if not rows:
        return []
    # D1: the temp forecast is per-hour — keep the temp lookup HOUR-floored even
    # though the PV/dedup grid is per-slot (else 3-of-4 quarters fall back to
    # cur_temp instead of the actual hourly-forecast temp).
    # D3: the load model is hour-bucketed too, so predict ONCE per hour (with
    # that hour's own temp) and read each slot from a midpoint-anchored linear
    # interpolation of those hourly values — the hourly value is the hour's
    # MEAN, anchored at the hour centre, and each slot reads at ITS own centre.
    # At slot_minutes=60 the slot centre IS the anchor, so this is an exact
    # identity. Anchors are only the hours that actually have emitted slots: no
    # predict() probe past the horizon, whose fallback would ramp the final
    # hour toward a value no row displays. Consequence: the last hour's late
    # quarters stay flat at that hour's value.
    load_points: list[tuple[datetime, float]] = []
    for hour in sorted({hour_floor(start) for _, start in rows}):
        h_temp = temp_by_hour.get(hour, cur_temp) if temp_by_hour else cur_temp
        load_points.append((hour, predictor.predict(hour, h_temp, fallback_w, quantile=quantile)))
    load_curve = MidpointLinear(load_points)
    half = timedelta(minutes=slot_minutes / 2)
    out: list[ForecastInterval] = []
    pv_idx = 0
    for h, slot_start in rows:
        # D2: pv_curve is a step function, not a per-hour sum — a curve point's
        # watts hold from its own timestamp until the NEXT point supersedes it (or
        # it goes stale after 1h with no successor, e.g. overnight). Walk a forward
        # cursor (rows and pv_curve are both processed start-ascending, so the
        # cursor only ever advances) to the last point at or before this slot's
        # start, then use its watts iff that point is < 1h old, else 0.0. This
        # reproduces the legacy hourly-fan behavior byte-identically for
        # HOUR-ALIGNED one-point-per-hour curves while giving dense sub-hourly
        # curves (from_watts output at the live slot width) each slot's own value
        # instead of the hour's SUM fanned across every quarter.
        # Caveat (accepted 2026-08-01 final review): non-hour-aligned hourly
        # curves (synth_pv_curve / arrays anchored at now/sunrise, degraded-data
        # fallback paths only) read one point LATER than the old hour-sum did —
        # a <=1-slot temporal shift, energy-conserved.
        # Precondition: pv_curve is expected to carry at most one point per
        # timestamp (all four parsers.py builders guarantee this). Points sharing
        # a timestamp are NOT summed here; the cursor keeps only the last one.
        while pv_idx + 1 < pv_n and pv_sorted[pv_idx + 1][0] <= slot_start:
            pv_idx += 1
        if pv_idx < pv_n and pv_sorted[pv_idx][0] <= slot_start and (
            slot_start - pv_sorted[pv_idx][0] < timedelta(hours=1)
        ):
            pv_w = pv_sorted[pv_idx][1]
        else:
            pv_w = 0.0
        load_w = load_curve.at(h + half)
        out.append(ForecastInterval(h, pv_w, load_w if load_w is not None else fallback_w, dt_h))
    return out
```

Update the function docstring's `load_w` sentence to:

```
    load_w = a midpoint-anchored linear interpolation (D3) of the per-HOUR
    predictor.predict(hour, h_temp, fallback_w, quantile=quantile) values, where
    h_temp is looked up from temp_by_hour (per-hour forecast, HOUR-floored) falling
    back to cur_temp.  predict is called once per hour, not once per slot.  At
    slot_minutes=60 the slot centre is the hour anchor, so this is byte-identical
    to the legacy per-slot call.
```

- [ ] **Step 4: Run the tests to verify they pass**

`.venv/bin/python -m pytest tests/test_15min_pv_curve.py tests/test_plan.py tests/test_plan_past_actuals.py tests/test_p50_export_reserve.py -v`
Expected: all pass.

- [ ] **Step 5: Lint and commit**

```bash
.venv/bin/python -m ruff check custom_components/anker_x1_smartgrid/plan.py tests/test_15min_pv_curve.py
git add custom_components/anker_x1_smartgrid/plan.py tests/test_15min_pv_curve.py
git commit -m "feat(plan): interpolate the hourly load model across sub-hour slots"
```

---

### Task 5: full-suite triage, replay verification, docs

**Files:**
- Modify: whichever test files the full-suite run flags (triage rules below)
- Modify: `docs/superpowers/specs/2026-08-02-pv-load-curve-interpolation-design.md` (two agreed deviations)
- Create: `/private/tmp/claude-502/-Users-kleist-Sites-x1-smartcharge/4d38018c-6c97-4ab4-b3a6-412e7d470666/scratchpad/compare_curves.py` (throwaway, not committed)

**Interfaces:**
- Consumes: everything from Tasks 1-4.
- Produces: a green suite and a before/after decision comparison recorded in the task report.

- [ ] **Step 1: Run the full suite**

`.venv/bin/python -m pytest tests -q`

- [ ] **Step 2: Triage every failure against the identity rules**

For each failure, classify BEFORE editing anything:

- **Regression (fix the code, never the test):** any failure where `slot_minutes=60`, or where the PV source cadence equals the bucket width. Both are exact identities — a diff there means the implementation is wrong.
- **Intended change (update the test):** a `slot_minutes=15`/`30` case whose predictor varies by hour, or whose PV source is coarser than the bucket. Recompute the expected value by hand from the anchor arithmetic (period centre anchors, bucket-centre queries) and write the number in, with a comment naming the anchors. Do NOT paste whatever the code now returns without deriving it.

Known-safe: `tests/test_15min_golden.py` builds `ForecastInterval`s directly and must not move. `tests/fixtures/*.json` are plan-sensor snapshots consumed by `scripts/replay_dp.py`, not by the builders — do not regenerate them.

- [ ] **Step 3: Re-run the full suite until green**

`.venv/bin/python -m pytest tests -q`
Expected: 0 failed.

- [ ] **Step 4: Lint the whole tree**

`.venv/bin/python -m ruff check custom_components tests`
Expected: `All checks passed!`

- [ ] **Step 5: Capture live watts and compare curves before/after**

Capture the lab's real PV samples (48 h window is enough):

```bash
cd /Users/kleist/Sites && TOKEN=$(cat .token) && curl -s -H "Authorization: Bearer $TOKEN" \
  https://homeassistant.lab.kle.ist/api/states/sensor.ha_solcast_fusion_01kwmz42eg00x4mf88na1evte7_energy_production_today \
  > "$SCRATCH/pv-watts-live.json"
```

(`$SCRATCH` = the scratchpad path in **Files** above.)

Write `$SCRATCH/compare_curves.py` to load that `watts` dict, build the curve twice — once on `git stash`ed pre-change code, once on the new code — at `step_h=0.25`, and print per-hour kWh for both plus the day total. Simpler equivalent that avoids stashing: reimplement the ~15-line hold fill inline in the script as `baseline`, and import `build_pv_curve_from_watts` for the new path.

Record in the task report: per-hour kWh delta and day-total delta. **Gate: day total must move < 0.5%, and no hour by more than ~5%.** A larger move means the interpolation is redistributing more than the boundary residue predicts — stop and investigate.

- [ ] **Step 6: Replay the DP against the fixtures**

```bash
.venv/bin/python scripts/replay_dp.py \
  --plan tests/fixtures/plan-sensor-2026-08-01-morning.json \
  --options tests/fixtures/options-2026-08-01.json
```

Run it on the pre-change commit (`git stash` or a detached checkout of `HEAD~4`) and on the new code; diff the selected charge/export slots and the reported €.

Caveat to state plainly in the report: these fixtures store already-built horizon ROWS (hourly-flat PV at 15-min), so the replay exercises the DP's response to a changed interval shape only if the script's own interval rebuild is fed interpolated values — it does NOT re-run `build_pv_curve_from_watts`. Treat the result as a sensitivity check on decision stability, not as a full end-to-end reproduction. **Gate: slot placement changes only at boundaries (a charge/export slot moving by one quarter is expected and fine); a change in which HOURS are chosen, or a € move > ~2%, blocks the deploy.**

- [ ] **Step 7: Amend the spec with the two agreed deviations**

In `docs/superpowers/specs/2026-08-02-pv-load-curve-interpolation-design.md`:
- Section A heading: `resolution.py` → `interp.py` (with the reason: `resolution.py`'s scope is price-slot resolution detection).
- Section D: replace the "predict for `hour_floor(slot)` and for `hour_floor(slot) + 1 h`" sentence with the emitted-hours-only rule and its reason (no fallback contamination past the horizon; the last hour's late quarters stay flat).

- [ ] **Step 8: Commit**

```bash
git add docs/superpowers/specs/2026-08-02-pv-load-curve-interpolation-design.md
# plus any test files touched in Step 2
git commit -m "test: align pinned expectations with interpolated curves"
```

- [ ] **Step 9: Deploy to lab and verify visually**

Follow the standard HAOS deploy (scp to `/config/custom_components`, backups OUTSIDE that tree, restart HA), then check:
- `sensor.smartgrid_plan` horizon: consecutive quarters within an hour must differ for both `pv_w` and `load_w`.
- The Lovelace chart's solar area reads as a ramp, not a staircase.
- Day-total PV in the horizon is within ~0.5% of the pre-deploy value.

---

## Self-Review

**Spec coverage:**
- Spec A (helper + 5 properties) → Task 1 (identity, non-negativity via flat-hold, no temporal shift, per-interval widths, run splitting all have named tests).
- Spec B (PV, emission rules unchanged) → Task 2.
- Spec C (four `step_h` sites) → Task 3.
- Spec D (load, scope boundary at `forecast.build_intervals`) → Task 4; `build_intervals` is deliberately untouched in every task.
- Spec E (no card change) → no task, by design.
- Spec "Testing" → Tasks 1-4 steps 1, plus Task 5 steps 1-3.
- Spec "Verification before deploy" → Task 5 steps 5-6.
- Spec "Rollback" → Global Constraints (no knob).

**Type consistency:** `MidpointLinear(points, *, max_gap_h, default_width_h)` / `.at(when) -> float | None` is used identically in Tasks 2 and 4. `build_pv_curve_from_watts` and `build_display_intervals` keep their existing signatures — no caller changes anywhere.

**Known behaviour changes, all with pinned expectations:** 30-min source at 15-min buckets ramps; hourly source at 15-min buckets ramps; the first emitted bucket may interpolate against a pre-`now` sample; `predict` is called once per hour; the display path's PV curve is built at the slot width.
