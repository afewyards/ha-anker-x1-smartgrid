# Calibration: cost-aware placement — design

Date: 2026-09-10. Amends `2026-08-03-battery-calibration-policy-design.md`.

## Problem

On 45a a calibration cycle ran across the most expensive part of the day and
cancelled ~€10 of planned export profit. Cause, from the code:

- `select_window` costs a candidate as `need_kwh × mean_price`, with
  `need_kwh` sized from the DP's projected SoC at the candidate's start. The DP
  fills the pack right before the evening peak *in order to export it*, so the
  pre-peak candidate needs ~0 kWh, costs ~€0, and wins.
- `need_kwh ≤ CALIBRATION_FREE_TOPUP_KWH` (1.0) also bypasses the price bar.
- Hold-through holds unconditionally whenever a cycle is due and the pack is
  at the top, by any cause, suppressing ALL export for `dwell_h`.
- Nothing prices what a window *suppresses*: planned export, battery-served
  load pushed onto the grid.
- Nothing stops a window opening with the live pack far below the plan's
  projection (plan drift; `_cal_soc_fc` also carries ~24 h of past actuals).

## Decisions (user, 2026-09-10)

| Topic | Decision |
|---|---|
| Cost model | Price each window against the DP's own plan (counterfactual, pure, outside the DP) |
| Start SoC | Window may only start at ≥ **95%** (projected for placement, live for actuation) |
| Hold-through | Hold on a self top-out only if the hold is cheap |
| Overdue | Price bar bypassed, but skip any window over a € cap |
| Hold length | **30 min max** |

## Gates

`const.CALIBRATION_MIN_START_SOC = 95.0` replaces `CALIBRATION_MIN_PLAN_SOC`
(80). The plan-peak gate is dropped as a separate step: no candidate can start
below 95, so it can never bind on its own. `plan_peak_soc` stays for the
controller's overdue log line, compared against the new const.

- **Placement**: a candidate is considered only if the plan's projected SoC at
  its start is ≥ 95.
- **Actuation**: a selected window containing `now` enters `charging` only if
  **live** SoC ≥ 95; otherwise it reports `scheduled` and is re-evaluated next
  tick (it starts the moment the live pack reaches 95 inside the window).
- **Committed**: once running, a cycle is not re-costed or re-gated.
  - previous tick `charging` and `now < window_end` → `charging`
  - previous tick `holding` and `soc ≥ continue_soc` → `holding` (today's
    `already_holding` softening, unchanged)
  - a fresh top-out right after a committed `charging` tick → `holding`
    without a cost check (the window was already priced).
  The `already_holding: bool` kwarg becomes `prev: CalibPlan | None` (phase +
  window), threaded by the controller exactly as `_calibration_was_holding` is
  today.

The projection is built from future rows only (start ≥ current slot start,
not `estimated`, not `mode == "actual"`) — closes the open "yesterday's
actuals open the gate" bug.

## Cost model

Pure function in `calibration.py`:

```
cost(W) = Σ_{slot ∩ W} (cal_eur − plan_eur) − wv × max(0, E_cal_end − E_plan_end)
```

**Inputs** — `CalibSlot(start, dur_min, import_price, export_price | None,
pv_kwh, load_kwh, soc_end, plan_import_kwh, plan_export_kwh)`, built by the
controller from the horizon rows above.

- `plan_import_kwh` / `plan_export_kwh`: the whole-house balance
  `aggregate_planned_days` already derives inline. Extract it to
  `daily_stats.planned_house_flows(row) -> (import_kwh, export_kwh)` and use it
  in both places.
- `export_price`: post-fee, from the resolver nested in
  `controller._publish_daily_stats` (`_export_price_at`). Extract it to a
  controller method returning the callable; both paths use it. `None` zeroes
  the revenue leg, as everywhere else.
- `wv`: the DP's own water value (`optimize.compute_water_value`, already
  computed in `decision.py`), exposed as `_dp_out["water_value"]`. Missing
  (heuristic fallback, DP failure) → 0: cost is overstated, which fails closed.

**Per slot**, over the overlap `m` minutes (fraction `f = m / dur_min`):

- calibration sim: while `E < E_top`, `charge_ac = min(max_charge_w · m/60 /
  1000, (E_top − E) / eta_c)`, `E += charge_ac · eta_c`. The battery never
  discharges (FORCING). `net = load·f + charge_ac − pv·f`;
  `cal_eur = max(0, net)·import_price − max(0, −net)·export_price`.
- plan: `plan_eur = (plan_import·import_price − plan_export·export_price)·f`.
- in-progress slot: its row carries only the modelled REMAINDER (rates are
  scaled to the remaining minutes, `plan.build_plan_horizon` `out_scale`) plus
  the delivered grid-charge add-back. For that row, subtract the add-back
  exactly as `aggregate_planned_days` does (`delivered_at`), and use
  `f = m / remaining_min`.

**Energy anchors** (row `soc` is END-of-slot):

- `E_plan_start`: live SoC if W starts in the current slot, else the previous
  row's `soc_end`.
- `E_plan_end`: linear interpolation inside the last overlapped slot between
  its start SoC and `soc_end`.
- `E_cal_end`: the sim's final `E`.

The credit gives back the extra energy calibration leaves in the pack at the
refill value the DP itself uses, so a cheap window is not charged for storing
energy the plan would have bought anyway.

**Ignored, all small at a ≤ 1 kWh top-up:** top-of-pack drift (+270 W), cycle
wear, the X1 total-import ceiling.

**Worked cases:**

- Pre-peak hold (plan exports 6 kWh at €0.40; `wv` ≈ €0.21): lost export
  €2.40, credit ≈ €1.30, cost ≈ €1.10.
- Midday solar top-out with no export planned: plan and sim both spill the PV
  surplus, so cost ≈ €0.

**No plan rows** (DP failure, startup) → no placement and no fresh
hold-through. Committed phases still finish.

## Acceptance and ranking

- Filter candidates to the acceptable ones (rules below) FIRST, then take the
  cheapest acceptable candidate per UTC day. The earliest day with one still
  wins (unchanged stability rule). Filtering first matters: the normal bar
  varies with each candidate's `need_kwh`, so the cheapest candidate of a day
  can fail while a dearer one passes.
- **Normal**: accept iff `cost ≤ need_kwh × P30 + CALIBRATION_COST_ALLOWANCE_EUR`
  (€0.50). `P30` is the existing `price_percentile(history, 30)`; `None` → the
  allowance alone. `CALIBRATION_FREE_TOPUP_KWH` is removed: at a 95% start
  every window needs ≤ 1 kWh, so the bypass would always fire.
- **Overdue** (`days_since ≥ interval + grace`): accept iff
  `cost ≤ CALIBRATION_OVERDUE_COST_CAP_EUR` (€1.00). Otherwise wait; the
  existing overdue warning keeps it visible.
- **Hold-through** on a fresh, uncommitted top-out: the candidate
  `[now, now + dwell_h]` (`need_kwh = 0`), costed and accepted by the same
  rules. Rejected → fall through to normal selection; the export runs.

## Hold length

- `DEFAULT_CALIBRATION_DWELL_H` 1.0 → 0.5.
- UI range max 12 → 0.5 (min 0.25 kept).
- Hard clamp `const.CALIBRATION_MAX_DWELL_H = 0.5` applied where `Config` is
  built, so a UI-saved 1.0/2.0 on either box cannot restore the long hold.
- Translation text ("Default 2.") corrected.

The top-up from ≥ 95% (a few minutes) comes before the hold. Success detection
uses the clamped dwell, and older ≥ 1 h successes still count.

## Observability

New plan attribute `calibration_cost_eur`: the cost of the cheapest candidate
evaluated this tick, accepted or not. `None` when the cycle is not due or no
candidate exists. Read it together with `calibration_state` to see why a due
cycle is idle.

## Files

| File | Change |
|---|---|
| `calibration.py` | `CalibSlot`, `window_cost`, start gate, committed `prev`, new acceptance, hold-through costing; plan-peak gate step removed |
| `controller.py` | build `CalibSlot`s (future rows only), pass `prev` + `wv`, shared export-price resolver, overdue log vs new const, `calibration_cost_eur` attr |
| `daily_stats.py` | extract `planned_house_flows` |
| `decision.py` | `_out["water_value"]` |
| `const.py` | `CALIBRATION_MIN_START_SOC`, `CALIBRATION_COST_ALLOWANCE_EUR`, `CALIBRATION_OVERDUE_COST_CAP_EUR`, `CALIBRATION_MAX_DWELL_H`, dwell default; drop `CALIBRATION_MIN_PLAN_SOC`, `CALIBRATION_FREE_TOPUP_KWH` |
| `config_flow.py`, `models.py`, `strings.json`, `translations/en.json` | dwell range, clamp, text |
| `sensor.py` | new attribute |

DP core and oracle are untouched, so there is no parity-gate impact.

## Testing

`tests/test_calibration.py`:

- cost: hold across planned export is expensive; midday top-out ≈ 0; credit
  arithmetic; partial-slot overlap; missing `wv` → credit 0; missing export
  price → revenue leg 0
- selection prefers a midday top-out over the pre-peak candidate
- start gate: projected 94.9 rejected, 95 accepted; live 94 → `scheduled`, not
  `charging`
- committed charging survives a mid-window cost spike; committed hold survives
  a planned export appearing
- hold-through: rejected at the pre-peak, accepted at midday
- overdue: under the cap accepted, over the cap `idle`
- no plan rows → no placement, no fresh hold; committed phase continues

`tests/test_calibration_controller.py`:

- past-actual and estimated rows are excluded from `CalibSlot`s
- `prev` is threaded
- the attribute is published

`tests/test_calibration_config.py` / `tests/test_config_flow.py`: dwell
default 0.5, UI max 0.5, clamp.

`tests/test_daily_stats*`: `planned_house_flows` gives the same numbers as the
current inline balance.

Existing tests pinning `CALIBRATION_MIN_PLAN_SOC`, the free-top-up bypass, or
the `need × price` ranking are rewritten to the new rules, not deleted.

## Resolved (user, 2026-09-10)

- "30 min max" is the HOLD; the few-minute top-up from ≥ 95% precedes it.
- Allowance €0.50 and overdue cap €1.00 accepted as starting values; tune from
  `calibration_cost_eur` once live.
- Deploy to both lab and 45a.
