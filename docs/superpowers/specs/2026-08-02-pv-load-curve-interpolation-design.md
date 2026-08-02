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

### A. Shared helper — `resolution.midpoint_linear`

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
- **Per-period mean preserved to second order** — exact when the interpolant's slope is
  symmetric about the anchor, curvature-limited otherwise. Not exactly conservative;
  accepted (error is orders below PV forecast error). A conservative PCHIP-on-cumulative
  variant was considered and rejected as not worth the review cost.
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

Per slot: predict for `hour_floor(slot)` and for `hour_floor(slot) + 1 h` (memoized per
hour so `predict` is called once per hour, not once per slot), anchor both at `HH:30`,
evaluate at the slot center. Same helper. The `quantile` argument passes through
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

## Rollback

No config knob (user's call — keeps the options surface from growing, and the change is
identity-preserving at matching cadence). Rollback = `git revert` + scp + restart.

## Unresolved questions

None.
