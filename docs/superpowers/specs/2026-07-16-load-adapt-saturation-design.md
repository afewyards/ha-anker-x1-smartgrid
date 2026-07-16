# Load-Adapt Saturation + Surge Propagation — Spec

2026-07-16. Trigger: heavy-usage day; intraday corrector pinned at `RATIO_MAX = 1.5` (`load_adapt.py:17`), true surge magnitude unknown (unclamped ratio not recorded); linear fade zeroes the correction ≥ 8 h lead → overnight ride-out hours get baseline forecast. Occupancy corrector deployed but `occ_adapt_fraction = 0` (9 cells ready). Miss cost ≈ €0.15–0.30/night extra dawn imports; `soc_hedge_fraction = 0.5` partially catches realized drift.

**Non-goal:** changing buy economics. Charging stays price-gated (`charge_margin` + `cycle_cost`); better load adaptation buys MORE kWh at qualifying troughs and raises the ride-out reserve — it never adds new charge hours.

## Phase A — Instrument (zero risk, ship first)

- `load_adapt` ratio fn additionally returns the UNCLAMPED ratio (pure, clamped stays primary).
- New plan-sensor attrs: `load_adapt_ratio_raw`, `load_adapt_pinned_h` (consecutive hours ratio == RATIO_MAX; in-memory controller counter, reset on unpin/new day — diagnostic only, not persisted).
- Tests: raw ≥ clamped invariant; pin counter reset semantics. No behavior change → trivial review.
- Decision gate for Phase C: ≥ 7 days of pin frequency + median raw-ratio-while-pinned (HA attr history suffices).

## Phase B — Enable occupancy corrector (ops flip, no code)

- Lab options: `occ_adapt_fraction` 0 → 0.5. Kill switch: back to 0.
- Watch 1 wk: `occ_multiplier` excursions vs `load_forecast_mae` (177 W today) and 12 h horizon MAE (0.85 kWh).
- Scope honesty: today's surge was behavioral with occupancy as-expected (state 1 == expected 1) — occ layer catches presence-pattern deviations (guests, away-days), NOT this class. Independent of A/C; structural, non-fading.

## Phase C — Surge propagation (data-gated; pick ONE after A)

- **C1 cap raise:** `RATIO_MAX` 1.5 → 2.0 (one const). Pick if raw ratio routinely > 1.5 with SHORT pins. Risk: oven+dryer hour doubles short-lead forecast; 3 h window averaging dampens.
- **C2 sustained-surge fade extension:** when `pinned_h ≥ 3`, fade horizon 8 → 16 h for the correction, scoped to slots in daytime/evening local bands only. Pick if pins are LONG (sustained behavioral surge) and recorder shows overnight actual > forecast on surge days. New option `load_adapt_surge_fade_h`, default 8 (= inert).
- Anti-goal either way: blanket 1.5× on 00–06 sleeping hours — last night actual ~225 W ≈ baseline; night forecast is currently good, don't wreck it.

## Rollback

A: additive attrs. B: option → 0. C1: const revert. C2: option → 8 (inert).

## Unresolved questions

1. Phase B now or only after A's data week? (independent of A — rec: now)
2. C2 band scope: daytime+evening only vs all-except-night vs occupancy-gated?
3. Is €0.15–0.30/night worth Phase C at all, or stop after A+B and revisit in winter (bigger loads + spreads)?
4. Persist raw ratio to recorder samples (schema bump) for replay, or are HA attr-history queries enough? (rec: attrs only)
