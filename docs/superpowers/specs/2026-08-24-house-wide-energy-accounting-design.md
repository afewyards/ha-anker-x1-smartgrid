# House-wide energy accounting + past-attribution parity — design

**Date:** 2026-08-24
**Status:** approved (user: "go")
**Baseline:** main `909a68c`

Two fixes to one reporting seam. Part 1 adds whole-house grid import/export
(kWh + €) alongside the existing battery-basis figures. Part 2 makes the
chart's measured past use the same attribution as the ledger and the DP.

---

## Problem

### Part 1 — the table reports the battery, not the house

`daily_stats` prices only the battery legs — `min(grid_import, batt_charge)`
and `min(grid_export, batt_discharge)` (`optimize.cash_energy_kwh`). Two real
flows are therefore invisible:

- grid energy the **house consumes directly** (never enters the battery);
- PV **spilling straight to the grid** (never leaves the battery).

So the table cannot be reconciled against the meter or the HA Energy
dashboard. Measured on the lab instance:

| Day | table net € (battery) | house net € |
|---|--:|--:|
| 2026-08-19 | −1.95 | −5.31 |
| 2026-08-20 | +0.53 | −0.89 |
| 2026-08-23 | −2.73 | −3.05 |

This is a scope gap, not an arithmetic bug. The battery basis is what the DP
maximises and stays; the house basis is added next to it.

### Part 2 — the past bars use a third attribution

`past_actuals.py` fills the chart's `mode == "actual"` slots with a model that
matches neither the ledger nor the DP:

```python
export_kwh   = _kwh_sum(group, "grid_export_kwh")          # RAW house export
solar_charge = min(charge_kwh, max(0, pv_kwh - load_kwh))  # PV-surplus split
grid_charge  = max(0.0, charge_kwh - solar_charge)
```

Export counts PV spill as battery export. Charge is split by a *forecast-shaped*
residual (`pv − load`, where `load` is itself computed) instead of the measured
`min(grid_import, batt_charge)`. Past and planned bars are therefore not the
same quantity. Measured divergence on 2026-08-13..24: charge −0.00..−0.38
kWh/day, export +0.00..+0.24 kWh/day (1–2%).

Part 1 makes Part 2 safe to fix: once the house column exists, narrowing the
battery bar to battery-only loses no information.

---

## Verification of the measured half

Per-day house import/export replayed from `samples` and compared against the
meter's own daily totals (`sensor.zonneplan_electricity_*`, sampled just
before local midnight):

| Day | imp kWh ours/meter | exp kWh ours/meter | impCost ours/meter |
|---|--:|--:|--:|
| 08-16 | 23.11 / 23.15 | 12.33 / 12.25 | 3.99 / 3.98 |
| 08-20 | 25.94 / 26.20 | 15.58 / 15.66 | 6.25 / 6.31 |
| 08-22 | 24.66 / 24.56 | 8.69 / 8.66 | 3.91 / 3.88 |
| 08-23 | 24.64 / 24.84 | 0.84 / 0.81 | 3.18 / 3.21 |

Within 1–3% on every day available (HA recorder retention limits the window).
Export revenue reads systematically ~5% below the meter because Zonneplan
reports feed-in **gross**; our figure is net of `export_fee_eur_per_kwh`.
**Decision (user): keep the net-of-fee basis**, consistent with the ledger and
the DP. No gross key.

The recorder's per-tick meter integrals are therefore trustworthy as-is. The
measured half needs no new derivation — only new accumulation.

---

## Part 1 — design

### Attribution — one definition, mirrored on both paths

New in `optimize.py`, beside `cash_energy_kwh`:

```python
def house_energy_kwh(meter_w: float, tick_h: float) -> tuple[float, float]:
    """(house_import_kwh, house_export_kwh) for one tick. meter_w: + = import."""
    return max(0.0, meter_w) / 1000.0 * tick_h, max(0.0, -meter_w) / 1000.0 * tick_h
```

This is byte-identical to what `recorder.append` already writes into the
`grid_import_kwh` / `grid_export_kwh` columns (`max(0, p1_w) * dt`,
`max(0, -p1_w) * dt`), and `inputs.meter_w` **is** that `p1_w`. So the live
ledger and the recorded-sample replay cannot drift — the same discipline
`cash_energy_kwh` already enforces for the battery legs.

### Planned half — derived from the plan's own AC model

`plan.py:378-406` fixes the per-slot AC topology: PV serves load first, the
surplus charges (`solar_charge_w`), `self_discharge_w` covers any remaining
deficit, and `grid_export_w` is net-of-house. The house flows follow by
balance:

```
net          = load + solar_charge + grid_charge − pv − self_discharge
house_import = max(0, net)
house_export = max(0, −net) + grid_export
```

Checked against live horizon rows:

| slot | mode | result |
|---|---|---|
| 11:15 | grid | import 2.148 (= `grid_charge`), export 0 |
| 17:30 | export | import 0, export 2.955 |
| 06:45 | idle | import 0, export 0 (battery covers the deficit) |

The `max(0, −net)` term is what surfaces PV spill — the flow nothing reports
today. It becomes non-zero exactly when PV exceeds load and the battery cannot
absorb the surplus (`headroom_w == 0` or the charge rate caps).

`plan.py` currently emits `self_discharge_w` but no `self_discharge_kwh`. Add
it, so the derivation reads energies throughout and the published
`kWh == W * dt_h / 1000` invariant covers every term it uses.

### Live half — `CashLedger`

Four accumulators alongside the battery ones, reset in the same `rollover`
pass (single `day` key — a second key-comparison block would never fire):

```
today_house_import_kwh, today_house_export_kwh
today_house_cost_eur,   today_house_revenue_eur
```

Fed in `accumulate` from `house_energy_kwh(inputs.meter_w, TICK_SECONDS/3600)`.
Pricing mirrors the battery legs exactly: cost at `resolution.price_at`,
revenue at `effective_export_price`. Energy legs accumulate unconditionally;
each € leg is skipped only when its own price is missing.

**Persistence:** these join the existing **cash-ledger group** in
`_PERSIST_GROUPS` (`controller.py:145`) so a mid-day restart resumes rather
than zeroing today's row. Adding keys to that group is safe for stores written
before this change: `restore()` does `if store_key not in saved: continue`, so
an absent key leaves the dataclass default `0.0` and does **not** abort the
group's remaining fields. A test should pin that, since the group shares one
`try/except` and a genuine parse error *does* abort the rest of the group.

**Independence from `batt_w`:** `accumulate` currently returns early when the
battery reading is missing. The house legs do not depend on `batt_w`, so that
early return must not skip them — reorder so the house legs accumulate first.

### Aggregation — `daily_stats.py`

`DayTotals` gains `house_import_kwh, house_export_kwh, house_cost_eur,
house_revenue_eur` (plain dict keys, as today).

`aggregate_actual_days` — the house legs read `grid_import_kwh` /
`grid_export_kwh` directly, priced at `import_price` and
`export_price − fee`.

> **Coverage split.** The existing all-or-nothing guard skips a row when *any*
> of the four battery delta columns is NULL. The house legs need only the two
> meter columns, so that guard must not suppress them. Split into
> `null_ticks` (battery, unchanged) and a new `house_null_ticks`; a row with
> meter columns present still contributes to the house totals.

`aggregate_planned_days` — applies the balance above. The three skip rules are
unchanged (`estimated`, `mode == "actual"`, `start <= now`), so the today-row
double-count seam is untouched. The `delivered_at` reversal applies to the
battery `grid_charge_kwh` leg only, exactly as now.

`merge_days` — emits `house_import_kwh`, `house_export_kwh`, `house_cost_eur`,
`house_revenue_eur`, `house_net_eur`, plus `actual_house_net_eur` /
`planned_house_net_eur` mirroring the existing battery pair. Existing keys keep
their names and meaning — this is additive.

### Card

`daily-stats-card.yaml` gains Import / Export / House € columns:

```
| Day | Chg | Exp | Batt € | Imp | Exp | House € |
```

---

## Part 2 — design

`past_actuals.py` switches to the ledger's attribution, computed **per tick**
then summed (the `min()` does not commute with a column-wide sum, so
`_kwh_sum` cannot be reused for these two):

```
grid_charge_kwh  = Σ min(grid_import_kwh, batt_charge_kwh)
batt_export_kwh  = Σ min(grid_export_kwh, batt_discharge_kwh)
solar_charge_kwh = batt_charge_kwh − grid_charge_kwh
```

Taking `solar_charge` as the complement preserves the
`solar + grid == batt_charge` invariant the card's stacked bars rely on, and
drops the dependency on the computed `load_w`.

A tick missing either column of a pair contributes nothing to that pair and
falls back to the existing mean-W × slot_h × coverage path for the bucket, as
today. `grid_export_w` becomes the battery-attributed mean for consistency with
its own kWh column.

### Consequence

Past bars shrink slightly (PV spill leaves the export bar; the charge split
shifts by 1–2%). That energy is not lost from the card — it now appears in the
house Export column added by Part 1.

---

## Non-goals

- No change to the DP objective. `optimize_grid` correctly excludes house load
  from its cost (an unoptimisable constant offset); this is reporting only.
- No gross-of-fee key (decided above).
- No change to `sensor.smartgrid_battery_net_today` or `total_net_eur` — the
  battery cash basis keeps its exact current meaning.
- No rollup/`samples_hourly` changes; `aggregate_actual_days` keeps reading
  raw samples.

## Testing

- `house_energy_kwh` sign conventions, including `meter_w == 0`.
- `aggregate_actual_days`: house legs against a hand-built tick set; a row with
  NULL battery deltas but present meter columns still counts toward the house
  totals and increments `null_ticks` but not `house_null_ticks`.
- `aggregate_planned_days`: the three balance cases (grid / export / idle)
  above, plus a PV-spill row (`pv > load`, `headroom == 0`) asserting
  `house_export == pv − load`.
- `merge_days`: house keys present on actual / mixed / plan rows;
  `house_net_eur == house_revenue_eur − house_cost_eur`.
- `CashLedger`: house legs accumulate when `batt_w` is missing; rollover zeroes
  them; restore defaults missing keys to 0.0.
- `past_actuals`: per-tick `min()` attribution pinned against a tick set where
  the PV-surplus split and the `min()` split disagree; `solar + grid ==
  batt_charge`.
- Card structure test for the new columns.
- Regression: existing `daily_stats` / `past_actuals` / ledger tests unchanged
  in meaning.

## Rollback

Part 1 is additive — dropping the new keys and card columns restores the
current table. Part 2 is a behaviour change to measured past slots; reverting
`past_actuals.py` restores the PV-surplus split.
