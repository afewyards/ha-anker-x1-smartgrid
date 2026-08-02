# PV + load curve interpolation — design

**Date:** 2026-08-02
**Status:** approved (user: design confirmed, load line explicitly IN scope, no config knob)

## Problem (live evidence, lab @ 42ea157, 15-min slots)

Card draws the solar-forecast area as an hourly staircase (screenshot 2026-08-02 ~09:45).
Two independent causes.

### Cause 1 — display path drops `step_h` (parity bug)

Live PV source `sensor.ha_solcast_fusion_*_energy_production_today` publishes a `watts`
dict at **30-min cadence** (verified: keys `…T12:00`, `…T12:30`, … , n=3889).

- DP: `decision.py:937` → `build_pv_curve_from_watts(..., step_h=dt_h)` = 0.25 → curve at
  15-min buckets, 30-min plateaus after sample-and-hold.
- Display: `plan.py:491` → `build_pv_curve_from_watts(today_watts, tomorrow_watts, now)`
  — **no `step_h`** → falls back to the 1.0 default → each hour collapses to one
  bucket-mean, then `build_display_intervals` D2 holds it across all four 15-min rows.

Live horizon confirms: `08:00/08:15/08:30/08:45` all `pv_w 276.7995`; `09:00…09:45` all
`335.1294`. The card is a **coarser picture than the plan it is drawing**. Same omission
on the fallback builders: `plan.py:501` (`step_h=1.0` literal), `decision.py:939`,
`decision.py:941` (defaults) — so the staircase returns on the DP side too whenever the
watts source drops out.

### Cause 2 — sample-and-hold is piecewise-constant by construction

Even at correct `step_h`, the 2026-08-01 hold fill emits `[v1,v1,v2,v2]` for a 30-min
source at 15-min buckets. Energy-exact, shape-wrong: the true PV ramp is continuous, and
at 15-min resolution the steps are visible and feed the DP's per-slot surplus.

Predicted load (grey line) is hourly-constant across the four quarters for the analogous
reason: the load model is hour-bucketed and `build_display_intervals` calls
`predictor.predict` per slot with an hour-floored profile.

## Fix

### A. Shared helper — `interp.MidpointLinear`

> **Amended during implementation.** Originally specced as `resolution.midpoint_linear`.
> The helper lives in a **new module `interp.py`** instead: `resolution.py`'s stated scope
> is price-slot *resolution detection* (slot-width sniffing, slot flooring, price resampling),
> and a general-purpose numeric resampler does not belong to that concern. Shipped as a
> class (`MidpointLinear(points, *, max_gap_h, default_width_h)` with `.at(when)`) rather
> than the free function sketched below, so a curve is built once and queried per output
> instant instead of rebuilding the anchor table for every output grid.

```python
def midpoint_linear(
    points: list[tuple[datetime, float]],   # (period_start, value)
    out_starts: list[datetime],
    out_step_h: float,
) -> list[float]
```

Semantics: input point `i` is the **mean over `[t_i, t_{i+1})`**, anchored at that period's
**center**. Piecewise-linear between anchors; each output bucket evaluated at **its own
center**. The last input point's period width = the previous gap (mirrors the existing
tail rule). Outside the anchor range: flat hold, never extrapolate.

Properties (all test-pinned):

- **Identity when input cadence == `out_step_h`** — bucket center coincides with the
  anchor, so every value returns unchanged. This is what keeps the hourly-source /
  `slot_minutes=60` world byte-identical.
- **Non-negative** — linear between non-negative endpoints.
- **No temporal shift** — anchoring at period centers (not left edges) is what buys this;
  left-edge anchoring would drag the curve ~½ period late.
- **Total energy exactly conserved** (*amended — the original "preserved to second order"
  understated this*). Each output bucket is a convex combination of two anchors, and
  summing over a uniform output grid the weights landing on any one anchor add to exactly
  1.0. So the **total** over the resampled window is exact, not approximate — the only
  imbalance is a boundary term `step_h/2 × (Δ_out − Δ_in)`, where `Δ_in`/`Δ_out` are the
  first and last anchor-to-anchor steps. That term vanishes whenever the curve is flat at
  both window edges, which is true of every real PV day (night zeros on both sides).
  Measured on 48 h of live 30-min lab data resampled to 15-min buckets: day total moved
  **0.0000 %**. What interpolation *does* change is the distribution *within* the window:
  energy moves across bucket and hour boundaries in equal-and-opposite amounts (e.g. the
  `17:45` bucket loses exactly what the `18:00` bucket gains). Per-**hour** means are
  therefore **not** individually preserved — the largest measured single-hour move was
  **8.14 Wh** (0.39 % of that day's peak hour), concentrated at the dawn/dusk shoulders
  where the curve is most convex. A conservative PCHIP-on-cumulative variant was considered
  and rejected as not worth the review cost.
- Irregular gaps handled per-interval (no global cadence detection).

### B. PV — `parsers.build_pv_curve_from_watts`

Per source, after the existing bucket+mean step, replace the **interior** sample-and-hold
with `midpoint_linear`. Explicitly unchanged:

- leading buckets before a source's first sample stay 0.0 (no look-back),
- interior gaps ≥ 1 h stay unfilled (0.0, left to the contiguity fill),
- tail after a source's last real bucket keeps today's mirror-the-final-gap hold (nothing
  to interpolate toward),
- cross-source summing — each source interpolates **on its own cadence first**, then the
  per-bucket values sum,
- contiguity zero-fill between first and last kept bucket.

Note for reviewers: real buckets are rewritten too, not just the filled ones. A 30-min
source at 15-min buckets puts bucket `:00` at center `:07.5`, between anchors `:45` (prev
hour) and `:15` — so its value is interpolated, not `v1`. That is the intended ramp.

At `step_h=1.0` with an hourly source there are no empty in-coverage buckets and every
center == anchor → **byte-identical to the current output**.

### C. Parity — pass `step_h` everywhere it was dropped

- `plan.py:491` → `step_h=slot_minutes / 60.0`
- `plan.py:501` (arrays fallback) → slot width instead of the `1.0` literal
- `decision.py:939` (`build_two_day_pv_curve`) → `step_h=dt_h`
- `decision.py:941` (`build_pv_curve_from_arrays`) → `step_h=dt_h`

`build_display_intervals`' D2 cursor lookup needs no change: with the curve on the slot
grid, the "last point at or before slot start, < 1 h old" rule resolves to exact matches.

### D. Load — `plan.build_display_intervals`

> **Amended during implementation.** The original rule — "predict for `hour_floor(slot)`
> **and for `hour_floor(slot) + 1 h`**" — would probe one hour *past the horizon* on the
> final hour's slots. `predictor.predict` has no data there and answers with its fallback,
> so the last hour's quarters would ramp toward a value **no displayed row ever shows**:
> fabricated shape contaminating the visible tail. Replaced by the **emitted-hours-only**
> rule below. Accepted consequence: with no anchor beyond the last emitted hour, the
> resampler's flat-clamp applies and **the final hour's late quarters stay flat** at that
> hour's value. That is the correct trade — a flat tail is honest about the absence of
> data; a ramp toward a fallback is not.

Anchors are exactly the **hours that actually have emitted slots** — never a probe past the
horizon. `predict` is called once per such hour (with that hour's own `temp_by_hour` value),
each hourly value is treated as that hour's **mean** and anchored at the hour centre
(`HH:30`), and every slot reads the interpolant at **its own** centre. Same helper. The
`quantile` argument passes through
untouched — interpolation is applied to whichever quantile the caller asked for, so a P80
caller gets an interpolated P80 curve, never a mixed one.

Identity at `slot_minutes=60` (slot center == `HH:30` == anchor). Covers card **and** DP,
since `decision.py:946` calls this same function.

Scope boundary: `forecast.build_intervals` (ride-out reserve, `decision.py:968`) stays on
the raw hourly predictor. It consumes the load **integral** over many hours, which
interpolation preserves; leaving it alone keeps the blast radius at one function.

Recorded concern (raised, user accepted): the hourly load profile carries no sub-hourly
information, so this ramp is drawn precision, not measured precision — and it shifts
per-slot load inside the DP. PV differs: it has genuine 30-min source data.

### E. Card — no change

The chart reads `horizon[].pv_kwh` / `load_kwh` directly, so the smooth curve arrives from
the data. Keeping the card dumb preserves chart == plan parity. Verify visually after
deploy; only revisit `curve:`/`stroke:` if the rendering still reads wrong.

## Testing

Unit — `midpoint_linear`: identity at matching cadence; 30→15 ramp values; sum
preservation on a linear input; irregular gaps; ≥1 h gap untouched; non-negativity;
single-point input.

Unit — `build_pv_curve_from_watts`: hourly source @ `step_h=1.0` byte-identical (existing
suite); 30-min source @ 0.25 → strictly monotone ramp across a rising hour, hourly mean
within tolerance of the hold version; multi-source with mixed cadence sums after
per-source interpolation; leading/tail/≥1 h-gap rules unchanged.

Unit — `build_display_intervals`: `slot_minutes=60` byte-identical (load and PV);
15-min → the four quarters of an hour are **not** equal; `predict` called once per hour.

E2E — pin a 30-min-source plan at `slot_minutes=15` asserting quarters differ within an
hour on both the PV and load series, and that day-total `pv_kwh` matches the pre-change
total within tolerance.

## Verification before deploy

`scripts/replay_dp.py` against captured live data, hold vs interpolated: compare charge/
export slot placement and daily €. Material movement (beyond slot-boundary jitter) blocks
the deploy and reopens the design. Then lab deploy + morning card check.

## Decisions taken during implementation

### `current_hour_blend` stays OFF and unchanged

`intra_hour.py`'s current-hour blend replaces the current hour's model value with
`observed kWh so far + model × remaining fraction`. Its docstring previously claimed the
blend was "a safe no-op" in 15-min slot mode, on the reasoning that it keys on an exact
`when == now_h` (hour-floored) match which the current *slot* start would miss.

Section D's change invalidates that reasoning: `build_display_intervals` now predicts once
per **HOUR**, so the blend's hour-floored key matches and its trigger re-activates at **any**
slot width. The docstring was corrected accordingly (and a test pins the real behaviour) —
but the flag itself was deliberately **left OFF** (`DEFAULT_CURRENT_HOUR_BLEND = False`, no
code path changed). Two independent reasons:

1. **It would double-correct.** `load_adapt.py` (Layer A, the intraday ratio corrector)
   already performs actual-vs-forecast correction for forward hours. The blend would stack a
   second actuals-based correction on top of it.
2. **Units mismatch at sub-hour resolution.** The blend's output is a **whole-hour** quantity
   that already contains *elapsed* energy. At 15-min slots the emitted rows for the current
   hour cover only the hour's **remaining** quarters, so feeding them a whole-hour blended
   mean pushes already-consumed energy into forward slots. The quantity has no meaning for
   those rows.

Turning it on is therefore a separate design question (it needs a remaining-fraction-aware
formulation first), not a follow-on to this wave.

### Verification harness limitation (found in Task 5)

`scripts/replay_dp.py` cannot verify this wave end-to-end, for a stronger reason than the
plan anticipated. It never calls `build_pv_curve_from_watts` / `build_display_intervals`
(it rebuilds intervals straight from the fixture's own rows), **and** `_build_intervals`
emits **hourly** `ForecastInterval`s (`dt_h=1.0`) even for a `slot_minutes=15` fixture. So
the stock replay is blind to a within-hour shape change by construction, and indeed returns
**byte-identical** output before and after the wave. Decision stability was instead measured
with a throwaway harness that swaps in 15-min intervals in both shapes (flat fan vs
midpoint-interpolated); see `task-5-report.md`. Making the replay harness slot-native would
be a worthwhile standalone improvement.

## Rollback

No config knob (user's call — keeps the options surface from growing, and the change is
identity-preserving at matching cadence). Rollback = `git revert` + scp + restart.

## Unresolved questions

None.
