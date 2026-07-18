# Eta-curve gated-bin neighbor interpolation — design

Date: 2026-07-18. Status: approved (brainstorm w/ user).

## Problem

Non-confident efficiency-curve bins fall back to the static prior (`eta_charge` 0.92 / `eta_discharge_static` 0.9239). The prior can sit far outside the confident measured neighbors, creating artificial per-power cliffs the DP optimizes around:

- **Discharge (hole):** 400–800 W confident 0.9658; 800–1500 W (meas 0.945, n=5) and 1500–2500 W (meas 0.968, n=8) gated → 0.9239; ≥4 kW confident 0.9879. The 4.3% cliff at 800 W beats the 2.5% price spread between the top two evening slots → DP splits tonight's 1.3 kWh export 531 W @0.3099 + 724 W @0.3176 instead of bundling at the peak (watt-exact: 0.75/0.55 kWh DC steps × 0.9658; `_BIN_KWH=0.05`; export-step eta lookup `_eta_discharge_at(e_dc/dt_h·1000)` in `optimize_grid`).
- **Charge (peak, sign flipped):** 1500–2500 W (meas 0.787, n=5) and 2500–4000 W (no data) gated → 0.92, *above* every confident neighbor (0.716 @400–800, 0.8228 @800–1500, 0.8801 @≥4 kW) → DP believes the unmeasured mid band is the cheapest place to charge.
- **Feedback loop:** DP avoids (discharge) or over-uses (charge) gated bands based on fiction; avoided bins never accumulate the `EFFICIENCY_MIN_RUNS=10` / `MIN_DC_KWH=2.0` runs to become confident.

Raising the min-runs gate cannot fix this — the gate already rejected these bins; the defect is the fallback *value*.

## Decision

**Principle: a gated bin may never be more extreme than its confident neighbors.**

At curve **aggregation** time (`efficiency.py` `_aggregate`, after per-bin `confident` finalization ~L195), rewrite `eta` of every non-confident bin (`low_confidence`, `no_data`, `over_unity`) per side:

1. Anchors = confident bins only. Anchor x = bin midpoint `(lo_w+hi_w)/2`; unbounded top bin uses `lo_w`.
2. Gated bin between two anchors → linear interpolation at its midpoint.
3. Gated bin outside the anchor range → flat extension of the nearest anchor.
4. Side has zero confident bins → static prior everywhere (current behavior; fresh install unchanged).
5. Result implicitly ≤ 1.0 (anchors are ≤ 1.0); assert/clamp anyway.

Unchanged: `measured`, `confident`, `fallback_reason` strings (incl. `over_unity` → `any_over_unity` attr), `EfficiencyCurve.static()` constructor (bypasses aggregation), all `eta_curve=None` byte-parity paths, gate constants, lookup-time code.

## Expected live effect (lab curve 2026-07-18)

- Discharge: 0–400 → 0.9658 (flat ext); 800–1500 → ~0.969; 1500–2500 → ~0.975; 2500–4000 → ~0.983. Cliff at 800 W gone → tonight's evening export bundles into the single peak-price slot.
- Charge: 0–400 → 0.716 (flat ext); 1500–2500 → ~0.840; 2500–4000 → ~0.865. Modeled mid-power charge cost +~9% → charge placement/rates may shift; **the larger behavioral change — watch next-day plan + regret after deploy**.

## Testing

- Unit (new): interp between anchors; flat edge extension; zero-anchor side → static prior; `over_unity` bin excluded as anchor but rewritten; charge-side peak case (prior above neighbors pulled down); eta ≤ 1.0 invariant; aggregation idempotent.
- Existing: `eta_curve=None` parity suites stay green; plan-sensor attr shape unchanged.
- Optional: offline replay of tonight's inputs (`replay_dp.py` recipe) confirming export bundles at peak.

## Deploy / rollback

- Integration → lab HAOS first (scp + restart, pristine-checkout gate); addon follows via existing vendor-sync (`sync_core.sh`) + release flow; France/HAOS-45 pick up on release.
- Rollback: `use_measured_eta` option OFF (existing kill switch → static curve), or revert commit.

## Out of scope

- Gate-threshold changes, ΔSoC-quantization hygiene (done in v0.9.0), idle-drain debias, 15-min slots.
