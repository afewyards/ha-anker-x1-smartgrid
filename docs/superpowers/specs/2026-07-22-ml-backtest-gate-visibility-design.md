# ML backtest-gate visibility — design

Date: 2026-07-22 · Status: approved (brainstorm session)

## Problem

Add-on `/health` ships the full walk-forward backtest metrics dict (`improvement_pct`,
`n_horizon_origins_24h`, model/baseline MAE, 24h horizon-energy MAE). The integration's
hourly health poll receives it and drops it — `build_ml_status_attrs()` passes only
`n_rows`/`last_trained` through. During the "backtest gate" phase the card shows the bare
status string; seeing promotion progress requires ssh + curl of the add-on `/health`.

## Decisions

- **Surface:** existing plan-card header — it renders `ml_status` verbatim, so enriching
  the string needs **zero card changes** (no repaste on the box).
- **Detail:** one gate-progress line, shown only while status is "backtest gate".
- **Composition:** integration-side, in `ml_status.py`.

## Behavior

When status resolves to `backtest gate`:

```
backtest gate · 5/8 origins · −24% vs baseline
```

- Format: `backtest gate · {origins}/{required} origins · {imp:+.0f}% vs baseline`.
- `imp` = per-step MAE `improvement_pct` (positive = model beats baseline; gate needs ≥ +2%
  on this AND the 24h horizon-energy metric — the h24 pair is exposed as attrs only).
- A segment is dropped when its value is absent/malformed (e.g.
  `backtest gate · 5/8 origins`). Never raises.
- All other statuses unchanged; on promotion the line collapses back to `ML active`
  by itself (numbers remain as attrs).

## Changes

1. **`custom_components/anker_x1_smartgrid/ml_status.py`**
   - New bounded coercers in the `_coerce_n_rows` style: metric float (round; non-finite →
     None; bool excluded) and metric int.
   - `build_ml_status_attrs()` reads `health.get("metrics")` internally (health dict is
     already passed whole — no signature change) and adds flat attrs:
     `addon_improvement_pct` (round 1), `addon_origins_24h`, `addon_origins_required`
     (= `backtest.MIN_HORIZON_ORIGINS_24H`, no magic 8), `addon_model_mae`,
     `addon_baseline_mae` (round 1, W), `addon_h24_mae`, `addon_baseline_h24_mae`
     (round 2, kWh).
   - Compose the gate string only when the status ladder lands on "backtest gate".
2. **No changes:** plan card, add-on, controller health poll (verify at plan time the full
   health dict reaches `build_ml_status_attrs`; if a call site filters keys, thread
   `metrics` through).
3. **`tests/test_ml_status.py`** — exact-string cases (full line, each segment missing),
   coercer edge cases (NaN/inf/bool/str/missing), update the two test-locked attribute
   orderings to include the new keys.

## Error handling

Same module contract: never raises; each value individually coerced; flat scalars only, so
the 16 KiB recorder attribute cap stays safe.

## Deploy / verify

Integration-only scp to the lab box ([[haos-deploy]] procedure). Verify card header shows
the gate line and dev-tools shows the new attrs. Eyeball line width on the card — if it
wraps, shorten `vs baseline` → `vs base`.

## Out of scope

Card layout changes; a persistent metrics row after promotion; France / 45 instances.
