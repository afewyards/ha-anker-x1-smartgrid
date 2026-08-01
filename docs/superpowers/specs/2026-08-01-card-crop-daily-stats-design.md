# Card tariff-crop + per-day grid/€ statistics — design

**Date:** 2026-08-01
**Status:** approved (user: "y")
**Baseline:** main `e261b2a`

Two independent deliverables. Part 1 is pure Lovelace YAML (no integration change).
Part 2 adds one module + one sensor + one card.

---

## Part 1 — Card stops at the last real tariff slot

### Problem

`lovelace/apexcharts-plan-card.yaml` renders the estimated-tomorrow tail
(`horizon[].estimated == true`, spec 2026-07-31-plan-estimated-tail) as 3 dashed
series, and `graph_span` is computed from `H[H.length - 1]` — the last row of the
**whole** horizon, estimated included. Fixture `tests/fixtures/plan-sensor-2026-08-01-postpub.json`:
real prices end `2026-08-02T21:45Z`, chart window runs to `2026-08-03T11:00Z` → 13 h of
speculative tail. User wants the chart to end where real tariff ends.

Second, latent: the 4 line/area series filter `!h.estimated`, but the 3 **column**
series (`Grid charge`, `Solar charge`, `Grid export`) map the whole horizon — bars
already render inside the estimated region today.

### Fix

1. **New config-template-card variable** `HR` — horizon filtered to `!h.estimated`.
   `graph_span` reads `HR[HR.length - 1].start` instead of `H[H.length - 1].start`.
   Keep the `+ 3` (1 h past last slot + 2 h history offset) and the `'38h'` empty
   fallback. Keep it a single double-quoted line — config-template-card strips
   `value.substring(2, len-1)`, so a folded scalar's trailing `\n` breaks it (existing
   comment in the file).
   `H` stays defined; other consumers unchanged.
2. **Bar series filter.** Add `.filter(h => !h.estimated)` to the `data_generator` of
   `Grid charge`, `Solar charge`, `Grid export`.
3. **Delete the 3 `(est)` series** — `Price (est)`, `SoC (est)`, `Solar (est)`.
   Consequential edits in `apex_config`:
   - `fill.type`: 10 → 7 entries `[solid, gradient, solid, solid, solid, solid, solid]`
   - `stroke.dashArray`: 10 → 7 entries `[0, 0, 0, 0, 0, 0, 0]`
   - tooltip `unit()`: drop the `'Price (est)'` and `'SoC (est)'` branches
   - update the "Estimated-tail series (dimmed)" comment block (delete it)

### Non-goals

Plan sensor keeps emitting the estimated tail — the DP needs it for terminal value
(spec 2026-08-01-terminal-piecewise-credit). This is display-only. Reverting restores
the dashed tail.

### Verification

Fixture-driven: load `plan-sensor-2026-08-01-postpub.json`, assert the computed
`graph_span` ends at `2026-08-02T22:00Z` (last real slot `21:45Z` + 1 h ceil), and that
no series' `data_generator` output contains a point at or past the first estimated
row's `start`.

---

## Part 2 — Per-day grid charge / export / € table

### Decisions (user-selected)

| Question | Answer |
|---|---|
| "Discharge" means | **grid export only** (battery→grid), matching the card's green bar and the ledger credit leg |
| "€ per day" means | **battery cash net** = export revenue − grid-charge cost; same basis as `sensor.smartgrid_battery_net_today` / lifetime `total_net_eur` |
| Render | **table card** under the chart |
| History depth | **14 days** back |

### Attribution — one function, no drift

`optimize.cash_flows_eur` (per tick) attributes:

```
grid_charge_w = min(max(0, meter_w), max(0, -batt_w))
batt_export_w = min(max(0, -meter_w), max(0, batt_w))
```

The recorder's v9 per-tick delta columns (`recorder.py` append) are, for one tick,
that same set of powers × **one shared clamped dt**:

```
grid_import_kwh   = max(0,  p1_w) * dt_h / 1000
grid_export_kwh   = max(0, -p1_w) * dt_h / 1000
batt_charge_kwh   = max(0, -batt_w) * dt_h / 1000
batt_discharge_kwh= max(0,  batt_w) * dt_h / 1000
```

Because dt is shared within the tick, `min` of the energies equals `min` of the powers
× dt. So:

```
tick_grid_charge_kwh = min(grid_import_kwh, batt_charge_kwh)
tick_batt_export_kwh = min(grid_export_kwh, batt_discharge_kwh)
```

is **definitionally the same attribution** as the live ledger, computed from recorded
energy. No second attribution model.

Pricing per tick: cost at `samples.import_price`; credit at
`samples.export_price − cfg.export_fee_eur_per_kwh` (mirrors
`optimize.effective_export_price`). A NULL price zeroes its own leg only — same
caller-level skip as `ledger.CashLedger.accumulate`.

### New module `custom_components/anker_x1_smartgrid/daily_stats.py`

Pure functions, no HA imports (pattern: `plan.py`, `past_actuals.py`, `ledger.py`).
`DayTotals` is a plain dict with keys `grid_charge_kwh, grid_export_kwh, cost_eur,
revenue_eur, coverage_ticks, null_ticks` — dicts, not a dataclass, so the sensor can
hand them to HA attributes unconverted.

```
aggregate_actual_days(rows, export_fee_eur_per_kwh, tz) -> dict[date, DayTotals]
```
Groups recorder sample rows (column-keyed dicts from `DataRecorder.read_feature_rows`,
`ts` is aware-UTC ISO) by date in `tz`; applies the attribution above. `tz` is an
explicit `tzinfo` parameter — the module stays HA-free, and the controller passes
`dt_util.DEFAULT_TIME_ZONE` so bucketing matches the ledger's `dt_util.as_local` day key. Rows with NULL v9 deltas contribute
nothing and are counted in `coverage_ticks` / `null_ticks` so a gappy day is visible
rather than silently small **within this dict** (see Accepted consequences #6 — the
counters do not currently reach the merged row). No mean-W fallback — pre-v9 rows are
outside the window by construction (v9 landed 2026-07-06; a 14-day window reaches
2026-07-18).

```
aggregate_planned_days(horizon, export_price_at) -> dict[date, DayTotals]
```
Sums future horizon rows' `grid_charge_kwh` × `price` and `grid_export_kwh` ×
`export_price_at(start)`. **Skips rows where `estimated` is true** — consistent with
Part 1: the table shows only what real tariff supports. Skips rows already covered by
actuals (`mode == "actual"`), so today is not double-counted.

`export_price_at(start) -> float | None` is a caller-supplied callable so the pure
function stays testable; the controller backs it with the per-slot export curve when
`export_curve_covered`, else the flat effective export price, else `None` (revenue
leg 0).

```
merge_days(actual, planned, today_totals, today, window_days=14) -> list[dict]
```
`actual` / `planned` are the two maps above; `today_totals` is the live-ledger
`DayTotals` for the current day; `today` is the current local `date` (the seam between
"actual" and "plan"). Rows older than `window_days` before `today` are dropped.
Ordered oldest→newest. Each row:

```
{date, grid_charge_kwh, grid_export_kwh, cost_eur, revenue_eur, net_eur,
 source, actual_net_eur, planned_net_eur}
```

`date` is an ISO `YYYY-MM-DD` string (not a `date` object) so it survives the HA
attribute round-trip into the card template.

`source` ∈ `actual` (past) / `mixed` (today) / `plan` (future). Today's row sums the
actual-so-far and planned-remainder halves; both halves stay visible in
`actual_net_eur` / `planned_net_eur`.

**Precedence rule:** for `date == today`, `today_totals` (live ledger) wins over any
`actual[today]` entry the samples pass may have produced. The two disagree by the
coverage seam in "Accepted consequences" #4; the ledger is authoritative for today
because the card subtitle already publishes it.

### Where today's actuals come from

**Not** from a samples query — from the live `ledger.CashLedger`, so the table's today
row equals the card subtitle's headline number exactly.

`CashLedger` gains two accumulators:

- `today_grid_charge_kwh`
- `today_export_kwh`

incremented in `accumulate()` from the same `min()` pair already computed there, reset
in `rollover()` alongside the € fields (single day key — the existing docstring
requires every daily field reset in that one pass), and added to the controller's
`_PERSIST_GROUPS` entry so a mid-day restart continues accumulating.

### Cost control

14 days of `samples` ≈ 20 k rows. Aggregating that on every 60 s tick is not viable.

- **Past days** are immutable once closed → aggregated **once at startup** and **once
  per local-day rollover**, in an executor job (recorder queries already go through
  `async_add_executor_job`). Result cached in memory on the controller, keyed by date.
- **Today** → live `CashLedger` fields, free.
- **Future days** → recomputed from the in-memory plan horizon each tick, free.

### New sensor `sensor.smartgrid_daily_stats`

- state: number of day rows (int)
- attributes: `days` (the `merge_days` list), `window_days`
- `_unrecorded_attributes = frozenset({"days"})` — same reasoning as `X1PlanSensor.horizon`:
  a per-tick-changing list blob bloats the HA recorder for no gain, the card reads live
  state.

State deliberately is **not** today's € — that would duplicate
`sensor.smartgrid_battery_net_today` and invite the two drifting in a user's eyes.

### Card

Plain `markdown` card below the chart, Jinja loop over
`state_attr('sensor.smartgrid_daily_stats', 'days')`. No new HACS dependency
(`markdown` is core). Columns: date, charge kWh, export kWh, net €. Future rows marked
(e.g. italic date or a `~` prefix) so plan is never mistaken for measurement.

Delivered as a second document in `lovelace/` — `lovelace/daily-stats-card.yaml` —
paste-able alongside the existing plan card.

---

## Accepted consequences

1. **France reads €0 export revenue.** `CONF_ENT_EXPORT_PRICE` is unset there
   (memory: frank-france-15min-wave), so `samples.export_price` is NULL and the credit
   leg is 0 — every day nets negative. This is exactly what the live cash ledger
   already reports; the table only makes it loud. **Deliberate non-goal:** no fallback
   to the import tariff. Per
   memory/frank-energie-fr-export-pricing, FR feed-in is a monthly solar-weighted EPEX
   average, so import-tariff-priced export would be fiction.
2. **Export fee is retroactive.** History is re-priced at the *current*
   `cfg.export_fee_eur_per_kwh`. Same simplification the ledger makes forward.
3. **History floor ~2026-07-06** (v9 delta columns). A 14-day window clears it today;
   widening the window later hits it.
4. **Today-vs-past seam is coverage, not dt.** Ticks where the controller is DISABLED
   still call `_record_sample` (controller.py:1258) with real `p1_w`/`batt_w`, so the
   recorder's v9 deltas are written — but `_accumulate_cash_ledger` never runs on that
   path (the disabled branch returns at controller.py:1351, well before the ledger call
   at controller.py:1514). Failsafe ticks are worse: the guard at controller.py:1360-1364
   returns before `_record_sample` is ever reached, so those minutes write **nothing** to
   either side. The call site's own comment concedes this: "Disabled-path and failsafe
   ticks return before this point: accepted spec limitation" (controller.py:1513). So
   `aggregate_actual_days` (replaying samples) and the live `CashLedger` (which skips
   those ticks entirely) are not two prices for the same energy — they cover genuinely
   different sets of ticks. A day can therefore shift by **euros, not cents**, when it
   rolls from `mixed` to `actual` — the divergence is bounded by however much real battery
   activity falls inside disabled/failsafe stretches, which can run for hours, not by any
   per-tick rounding. No measured instance has yet been attributed to this seam; the
   magnitude claim is derived from the mechanism, not from an observed day. (An earlier
   draft cited the −€5.58 night of 2026-07-30 here. That was withdrawn: it was a
   release-workmode loss — an App-managed release grid-charging 12 kW in released
   windows — and a release is not the same controller path as disabled or failsafe, so it
   is not established to be an instance of this seam.) Today's
   row is still ledger-sourced (`merge_days`' precedence rule) and still matches the card
   subtitle's headline number exactly — that part is unchanged and remains the reason the
   precedence rule exists.
5. **Open — not a settled decision: static tariff mode reads €0 for every past day.**
   Under `price_mode == static` (`tariff.py`'s synthetic flat/HP-HC price slots),
   `CONF_ENT_PRICE` / `CONF_ENT_EXPORT_PRICE` are unconfigured, so the recorder's direct
   entity reads (controller.py:1963, 1980-1981) leave `samples.import_price` /
   `samples.export_price` NULL on every tick, and `aggregate_actual_days` prices every
   past day at €0. The live ledger does not share this hole: `CashLedger.accumulate`
   prices through `resolution.price_at(slots, now, slot_minutes)` (ledger.py:97), which
   reads the synthesized `PriceSlot` list rather than the entity — its own docstring says
   the direct-entity read is deliberately avoided because it "is empty under static
   tariff mode and would silently zero this leg" (ledger.py:85-86). Net effect: every
   replayed past day shows €0 while today's ledger-sourced row shows real money. The
   parity test (`TestLedgerParity.test_recorded_replay_equals_live_ledger_euros`,
   tests/test_daily_stats.py:113) cannot catch this — it feeds one identical
   `import_price`/`raw_export_price` pair into both paths, so it proves the `min()`
   attribution matches, not that the price *source* matches. The same blind spot already
   exists in `regret_job.py`, which reads `s.get("import_price")` / `s.get("export_price")`
   off the same sample rows (regret_job.py:193,229). Unlike the other items in this
   section, this one has not been ruled on — it is arguably a bug, not an accepted
   tradeoff, and is left open for the user to decide (fix the sample columns, hide the €
   columns under static mode, or accept it).
6. **`coverage_ticks` / `null_ticks` are retained, not surfaced.** The module description
   above states NULL-delta rows are counted so "a gappy day is visible" — true only inside
   `aggregate_actual_days`' own `DayTotals` dict (daily_stats.py:79,81). `merge_days`
   (daily_stats.py:136-188) builds each output row from a fixed 9-key shape (`date,
   grid_charge_kwh, grid_export_kwh, cost_eur, revenue_eur, net_eur, source,
   actual_net_eur, planned_net_eur`) that includes neither counter, so nothing reaches the
   sensor or the card. A gappy day still renders as a plain (small) number, indistinguishable
   from a genuinely quiet day. The counters are retained in `DayTotals` for future use;
   surfacing them in the merged row is not part of this design.

## Testing

- `daily_stats.py` unit tests: attribution min-pair, NULL-price leg skip, NULL-delta
  coverage counting, local-date bucketing across a DST-free month boundary, estimated-row
  exclusion, `mode == "actual"` exclusion, empty inputs.
- Parity test: replay a synthetic tick stream through **both** `cash_flows_eur` (live
  path) and `aggregate_actual_days` (recorded path); assert equal € to 1e-9. This is the
  test that keeps the two attribution paths from drifting.
- `CashLedger`: kWh accumulators reset on rollover; survive persist/restore round-trip.
- Sensor: `days` in `_unrecorded_attributes`; state is the row count.
- Card (Part 1): fixture-driven `graph_span` and series-extent assertions as above.
