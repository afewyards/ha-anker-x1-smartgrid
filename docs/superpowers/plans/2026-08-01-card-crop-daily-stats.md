# Card tariff-crop + per-day grid/€ statistics — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Crop the Lovelace plan chart at the last real tariff slot, and add a per-day table of grid-charge / grid-export kWh and net € (measured for past days, planned for future days).

**Architecture:** Part 1 is pure Lovelace YAML — filter the estimated tail out of every series and compute `graph_span` from the filtered horizon. Part 2 adds one pure module (`daily_stats.py`) with three functions (actual-day aggregation from recorder samples, planned-day aggregation from the plan horizon, and a merge), two kWh accumulators on the existing `CashLedger`, controller wiring with a once-per-day cached backfill, and one new sensor plus a markdown card.

**Tech Stack:** Python 3.12, Home Assistant custom integration, pytest + pytest-homeassistant-custom-component, sqlite3 (recorder), PyYAML (card structure test), ApexCharts + config-template-card (Lovelace).

**Spec:** `docs/superpowers/specs/2026-08-01-card-crop-daily-stats-design.md`

## Global Constraints

- Baseline: main `77ed780`. Repo root `/Users/kleist/Sites/x1-smartcharge`.
- Run tests with `python3 -m pytest` from the repo root. `asyncio_mode = "auto"` — async tests need no decorator.
- Lint: `ruff check custom_components tests` must pass. `line-length = 120`, `target-version = "py312"`.
- `daily_stats.py` MUST NOT import Home Assistant. Timezone arrives as an explicit `tzinfo` parameter.
- Attribution is defined once, in `optimize.py`. No second copy of the `min()` rule anywhere.
- `DayTotals` is a plain `dict` with exactly these keys: `grid_charge_kwh`, `grid_export_kwh`, `cost_eur`, `revenue_eur`, `coverage_ticks`, `null_ticks`.
- Merged row `date` is an ISO `YYYY-MM-DD` **string**, never a `date` object (HA attribute round-trip).
- History window: `WINDOW_DAYS = 14`.
- Never `git add -A`. Stage the exact files listed in each task.
- Commit style: Angular conventions (`feat:`, `fix:`, `test:`, `docs:`, `refactor:`).

---

### Task 1: Crop the plan card at the last real tariff slot

Independent of every other task. Pure YAML plus a structural regression test.

**Files:**
- Modify: `lovelace/apexcharts-plan-card.yaml`
- Create: `tests/test_lovelace_plan_card.py`

**Interfaces:**
- Consumes: nothing.
- Produces: nothing consumed by later tasks.

**Context:** the card's 4 line/area series already filter `!h.estimated`, but the 3 column series do not, and `graph_span` is computed from `H[H.length - 1]` — the last row of the *whole* horizon, estimated rows included. With fixture `tests/fixtures/plan-sensor-2026-08-01-postpub.json`, real prices end at `2026-08-02T21:45Z` but the chart window runs to `2026-08-03T11:00Z`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_lovelace_plan_card.py`:

```python
"""Structural regression tests for the Lovelace plan card.

The card's logic lives in JS strings inside YAML, so there is no runtime to
assert against here. These tests pin the structural invariants that the
2026-08-01 tariff-crop change establishes, and the fixture contract that the
JS filter predicate relies on.

Spec: docs/superpowers/specs/2026-08-01-card-crop-daily-stats-design.md
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]
CARD_PATH = _ROOT / "lovelace" / "apexcharts-plan-card.yaml"
FIXTURE_PATH = _ROOT / "tests" / "fixtures" / "plan-sensor-2026-08-01-postpub.json"


def _card() -> dict:
    return yaml.safe_load(CARD_PATH.read_text(encoding="utf-8"))


def _horizon() -> list[dict]:
    blob = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return blob.get("attributes", blob)["horizon"]


def test_no_estimated_tail_series_remain():
    names = [s["name"] for s in _card()["card"]["series"]]
    assert [n for n in names if "(est)" in n] == [], names


def test_every_series_filters_estimated_rows():
    # The 3 column series (Grid charge / Solar charge / Grid export) used to
    # map the whole horizon while the line series filtered — bars rendered
    # inside the estimated region.
    for series in _card()["card"]["series"]:
        assert "!h.estimated" in series["data_generator"], series["name"]


def test_graph_span_reads_the_filtered_horizon():
    card = _card()
    assert "HR" in card["variables"], card["variables"].keys()
    assert "!h.estimated" in card["variables"]["HR"]
    assert "HR[HR.length - 1]" in card["card"]["graph_span"]
    assert "H[H.length - 1]" not in card["card"]["graph_span"]


def test_apex_fill_and_stroke_arrays_match_series_count():
    # Both arrays are positional per-series; a stale length silently
    # mis-styles every series past the mismatch.
    card = _card()
    n = len(card["card"]["series"])
    assert len(card["card"]["apex_config"]["fill"]["type"]) == n
    assert len(card["card"]["apex_config"]["stroke"]["dashArray"]) == n


def test_fixture_filter_predicate_crops_thirteen_hours():
    # Pins the data contract the JS `!h.estimated` predicate depends on:
    # real tariff ends 21:45Z, the raw horizon runs 13h further.
    horizon = _horizon()
    real = [h for h in horizon if not h["estimated"]]
    assert real[-1]["start"] == "2026-08-02T21:45:00+00:00"
    assert horizon[-1]["start"] == "2026-08-03T10:00:00+00:00"
    assert len(horizon) - len(real) == 13
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_lovelace_plan_card.py -v`
Expected: FAIL — `test_no_estimated_tail_series_remain` (3 `(est)` names present), `test_every_series_filters_estimated_rows` (bar series lack the filter), `test_graph_span_reads_the_filtered_horizon` (no `HR` variable). `test_apex_fill_and_stroke_arrays_match_series_count` and `test_fixture_filter_predicate_crops_thirteen_hours` PASS already (10 series, 10-entry arrays).

- [ ] **Step 3: Add the `HR` variable and repoint `graph_span`**

In `lovelace/apexcharts-plan-card.yaml`, under `variables:`, add `HR` immediately after `H`:

```yaml
  H: states['sensor.smartgrid_plan'].attributes.horizon || []
  # Real-tariff horizon only. The estimated-tomorrow tail (spec
  # 2026-07-31-plan-estimated-tail) is display-suppressed as of
  # spec 2026-08-01-card-crop-daily-stats: the chart ends where real
  # Zonneplan/Frank tariff ends. The plan sensor still EMITS the tail —
  # the DP needs it for terminal value — this only stops drawing it.
  HR: (states['sensor.smartgrid_plan'].attributes.horizon || []).filter(function (h) { return !h.estimated; })
```

Then replace the `graph_span` line (keep every surrounding comment — the single-line + double-quoted constraint documented there still applies):

```yaml
  graph_span: "${ HR.length ? Math.max(1, Math.ceil((new Date(HR[HR.length - 1].start).getTime() - new Date().setMinutes(0, 0, 0)) / 3600000) + 3) + 'h' : '38h' }"
```

- [ ] **Step 4: Filter the three column series**

In the same file, append `.filter(h => !h.estimated)` to the `data_generator` of `Grid charge`, `Solar charge`, and `Grid export` so all three read:

```yaml
      data_generator: |
        return (entity.attributes.horizon || []).filter(h => !h.estimated).map(h => [new Date(h.start).getTime(), h.grid_charge_kwh > 0.05 ? Math.log10(Math.max(1, h.grid_charge_kwh * 1000)) : null]);
```

```yaml
      data_generator: |
        return (entity.attributes.horizon || []).filter(h => !h.estimated).map(h => [new Date(h.start).getTime(), h.solar_charge_kwh > 0 ? Math.log10(Math.max(1, h.solar_charge_kwh * 1000)) : null]);
```

```yaml
      data_generator: |
        return (entity.attributes.horizon || []).filter(h => !h.estimated).map(h => [new Date(h.start).getTime(), h.grid_export_kwh > 0.05 ? Math.log10(Math.max(1, h.grid_export_kwh * 1000)) : null]);
```

- [ ] **Step 5: Delete the estimated-tail series and resize the positional arrays**

Delete the entire `# --- Estimated-tail series (dimmed) ---` comment block and the three series that follow it (`Price (est)`, `SoC (est)`, `Solar (est)`) — everything from that comment down to (but not including) `apex_config:`.

In `apex_config`, shrink both positional arrays from 10 to 7 entries:

```yaml
    fill:
      type: [solid, gradient, solid, solid, solid, solid, solid]
```

```yaml
    stroke:
      dashArray: [0, 0, 0, 0, 0, 0, 0]
```

In the tooltip `unit()` function, drop the two `(est)` branches so it reads:

```javascript
          var unit = function (name) {
            return name === 'Price' ? '€/kWh'
              : name === 'Projected SoC' ? '%'
              : 'kWh';
          };
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_lovelace_plan_card.py -v`
Expected: 5 passed.

- [ ] **Step 7: Commit**

```bash
git add lovelace/apexcharts-plan-card.yaml tests/test_lovelace_plan_card.py
git commit -m "feat(card): crop plan chart at the last real tariff slot"
```

---

### Task 2: Extract the tick attribution into `optimize.cash_energy_kwh`

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/optimize.py:303-330`
- Test: `tests/test_cash_ledger.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `optimize.cash_energy_kwh(meter_w: float, batt_w: float, tick_h: float) -> tuple[float, float]` returning `(grid_charge_kwh, batt_export_kwh)`. Task 5 and Task 8 both call it.

**Context:** `cash_flows_eur` currently computes the attributed powers inline and multiplies straight through to €. Task 5 needs the same attribution in kWh. Extracting it keeps one definition of the `min()` rule (a Global Constraint).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cash_ledger.py`:

```python
class TestCashEnergyKwh:
    def test_returns_the_same_attribution_cash_flows_prices(self):
        from custom_components.anker_x1_smartgrid.optimize import cash_energy_kwh

        # Importing 1500 W while the battery charges 2000 W: grid-attributed
        # charge = min(1500, 2000) = 1500 W.
        charge_kwh, export_kwh = cash_energy_kwh(1500.0, -2000.0, TICK_H)
        assert charge_kwh == pytest.approx(1500.0 / 1000.0 * TICK_H)
        assert export_kwh == 0.0

    def test_export_leg_is_battery_sourced_only(self):
        from custom_components.anker_x1_smartgrid.optimize import cash_energy_kwh

        # Exporting 3000 W but the battery only discharges 1000 W: the other
        # 2000 W is PV spill, out of scope.
        charge_kwh, export_kwh = cash_energy_kwh(-3000.0, 1000.0, TICK_H)
        assert charge_kwh == 0.0
        assert export_kwh == pytest.approx(1000.0 / 1000.0 * TICK_H)

    def test_cash_flows_eur_is_energy_times_price(self):
        from custom_components.anker_x1_smartgrid.optimize import cash_energy_kwh

        charge_kwh, export_kwh = cash_energy_kwh(1500.0, -2000.0, TICK_H)
        cost, credit = cash_flows_eur(1500.0, -2000.0, 0.30, 0.25, TICK_H)
        assert cost == pytest.approx(charge_kwh * 0.30)
        assert credit == pytest.approx(export_kwh * 0.25)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_cash_ledger.py::TestCashEnergyKwh -v`
Expected: FAIL with `ImportError: cannot import name 'cash_energy_kwh'`.

- [ ] **Step 3: Extract the helper**

In `custom_components/anker_x1_smartgrid/optimize.py`, insert `cash_energy_kwh` directly above `cash_flows_eur` and rewrite `cash_flows_eur` to delegate:

```python
def cash_energy_kwh(meter_w: float, batt_w: float, tick_h: float) -> tuple[float, float]:
    """``(grid_charge_kwh, batt_export_kwh)`` attributed for one tick interval.

    THE single definition of the cash-ledger attribution rule.  Sign
    conventions: ``meter_w`` positive = grid import, negative = export;
    ``batt_w`` positive = battery discharge, negative = charge.  PV covers the
    house first, so each leg is the ``min()`` of its two signed readings and
    PV-spill export is excluded.

    Both ``cash_flows_eur`` (live ledger) and ``daily_stats`` (recorded-sample
    replay) derive from this, so the two paths cannot drift.
    """
    grid_charge_w = min(max(0.0, meter_w), max(0.0, -batt_w))
    batt_export_w = min(max(0.0, -meter_w), max(0.0, batt_w))
    return grid_charge_w / 1000.0 * tick_h, batt_export_w / 1000.0 * tick_h
```

Then replace the body of `cash_flows_eur` (keep its existing docstring verbatim) with:

```python
    grid_charge_kwh, batt_export_kwh = cash_energy_kwh(meter_w, batt_w, tick_h)
    cost = grid_charge_kwh * import_price if import_price is not None else 0.0
    credit = batt_export_kwh * export_price_eff if export_price_eff is not None else 0.0
    return cost, credit
```

This is arithmetically byte-identical: the original `grid_charge_w / 1000.0 * tick_h * import_price` is left-associative, so the grouping is unchanged.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_cash_ledger.py -v`
Expected: all pass — the new class plus every pre-existing `cash_flows_eur` test (which pin the byte-identical claim).

- [ ] **Step 5: Commit**

```bash
git add custom_components/anker_x1_smartgrid/optimize.py tests/test_cash_ledger.py
git commit -m "refactor(optimize): extract cash_energy_kwh as the single attribution rule"
```

---

### Task 3: `daily_stats.aggregate_actual_days`

**Files:**
- Create: `custom_components/anker_x1_smartgrid/daily_stats.py`
- Create: `tests/test_daily_stats.py`

**Interfaces:**
- Consumes: nothing (pure).
- Produces:
  - `daily_stats.WINDOW_DAYS: int = 14`
  - `daily_stats.new_day_totals() -> dict`
  - `daily_stats.aggregate_actual_days(rows: list[dict], export_fee_eur_per_kwh: float, tz: tzinfo) -> dict[date, dict]`

**Context:** the recorder's v9 per-tick delta columns are `W × one shared clamped dt` within a tick, so `min(grid_import_kwh, batt_charge_kwh)` equals `min(max(0,p1_w), max(0,-batt_w)) × dt` — the same attribution the live ledger applies. `rows` are column-keyed dicts from `DataRecorder.read_feature_rows(since_iso)`; `ts` is an aware-UTC ISO string; `import_price` is the all-in import tariff and `export_price` is the **raw** feed-in tariff (the fee is subtracted here, mirroring `optimize.effective_export_price`).

- [ ] **Step 1: Write the failing test**

Create `tests/test_daily_stats.py`:

```python
"""TDD tests for per-day grid/€ statistics.

Spec: docs/superpowers/specs/2026-08-01-card-crop-daily-stats-design.md
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone, UTC

import pytest

from custom_components.anker_x1_smartgrid import daily_stats

# UTC+2 — the lab/France local zone in August. Chosen so that a 22:00Z tick
# lands on the NEXT local day, which is what makes the tz parameter matter.
CEST = timezone(timedelta(hours=2))


def _row(ts: datetime, **cols) -> dict:
    base = {
        "ts": ts.isoformat(),
        "grid_import_kwh": 0.0,
        "grid_export_kwh": 0.0,
        "batt_charge_kwh": 0.0,
        "batt_discharge_kwh": 0.0,
        "import_price": 0.30,
        "export_price": 0.25,
    }
    base.update(cols)
    return base


class TestAggregateActualDays:
    def test_grid_charge_is_the_min_of_import_and_battery_charge(self):
        # Imported 0.05 kWh while the battery took 0.02 kWh: only 0.02 is
        # grid-attributed (the rest fed the house).
        rows = [_row(datetime(2026, 7, 20, 10, 0, tzinfo=UTC), grid_import_kwh=0.05, batt_charge_kwh=0.02)]
        out = daily_stats.aggregate_actual_days(rows, 0.0, CEST)
        day = out[date(2026, 7, 20)]
        assert day["grid_charge_kwh"] == pytest.approx(0.02)
        assert day["cost_eur"] == pytest.approx(0.02 * 0.30)
        assert day["grid_export_kwh"] == 0.0
        assert day["revenue_eur"] == 0.0

    def test_export_is_battery_sourced_only_and_fee_is_subtracted(self):
        # Exported 0.10 kWh but the battery only discharged 0.04: the rest is
        # PV spill. Fee 0.02 → credited at 0.25 - 0.02.
        rows = [_row(datetime(2026, 7, 20, 12, 0, tzinfo=UTC), grid_export_kwh=0.10, batt_discharge_kwh=0.04)]
        out = daily_stats.aggregate_actual_days(rows, 0.02, CEST)
        day = out[date(2026, 7, 20)]
        assert day["grid_export_kwh"] == pytest.approx(0.04)
        assert day["revenue_eur"] == pytest.approx(0.04 * 0.23)

    def test_ticks_accumulate_within_a_day(self):
        rows = [
            _row(datetime(2026, 7, 20, 1, 0, tzinfo=UTC), grid_import_kwh=0.03, batt_charge_kwh=0.03),
            _row(datetime(2026, 7, 20, 2, 0, tzinfo=UTC), grid_import_kwh=0.03, batt_charge_kwh=0.03),
        ]
        out = daily_stats.aggregate_actual_days(rows, 0.0, CEST)
        assert out[date(2026, 7, 20)]["grid_charge_kwh"] == pytest.approx(0.06)
        assert out[date(2026, 7, 20)]["coverage_ticks"] == 2

    def test_buckets_on_the_local_day_not_utc(self):
        # 22:30Z is 00:30 the NEXT day in CEST.
        rows = [_row(datetime(2026, 7, 20, 22, 30, tzinfo=UTC), grid_import_kwh=0.01, batt_charge_kwh=0.01)]
        out = daily_stats.aggregate_actual_days(rows, 0.0, CEST)
        assert set(out) == {date(2026, 7, 21)}

    def test_null_price_zeroes_only_its_own_leg(self):
        # France: export_price entity unconfigured → NULL. Charge still costs.
        rows = [
            _row(
                datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
                grid_import_kwh=0.02,
                batt_charge_kwh=0.02,
                grid_export_kwh=0.05,
                batt_discharge_kwh=0.05,
                export_price=None,
            )
        ]
        out = daily_stats.aggregate_actual_days(rows, 0.0, CEST)
        day = out[date(2026, 7, 20)]
        assert day["cost_eur"] == pytest.approx(0.02 * 0.30)
        assert day["revenue_eur"] == 0.0
        # kWh is still measured — only the € leg drops out.
        assert day["grid_export_kwh"] == pytest.approx(0.05)

    def test_null_delta_row_counts_as_null_tick_and_contributes_nothing(self):
        rows = [
            _row(datetime(2026, 7, 20, 3, 0, tzinfo=UTC), grid_import_kwh=0.02, batt_charge_kwh=0.02),
            _row(datetime(2026, 7, 20, 4, 0, tzinfo=UTC), batt_charge_kwh=None),
        ]
        out = daily_stats.aggregate_actual_days(rows, 0.0, CEST)
        day = out[date(2026, 7, 20)]
        assert day["grid_charge_kwh"] == pytest.approx(0.02)
        assert day["coverage_ticks"] == 1
        assert day["null_ticks"] == 1

    def test_unparseable_and_missing_ts_rows_are_skipped(self):
        rows = [
            {"ts": None},
            {"ts": "not-a-timestamp"},
            _row(datetime(2026, 7, 20, 5, 0, tzinfo=UTC), grid_import_kwh=0.01, batt_charge_kwh=0.01),
        ]
        out = daily_stats.aggregate_actual_days(rows, 0.0, CEST)
        assert set(out) == {date(2026, 7, 20)}

    def test_empty_input(self):
        assert daily_stats.aggregate_actual_days([], 0.0, CEST) == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_daily_stats.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'custom_components.anker_x1_smartgrid.daily_stats'`.

- [ ] **Step 3: Create the module with `aggregate_actual_days`**

Create `custom_components/anker_x1_smartgrid/daily_stats.py`:

```python
"""Per-day grid-charge / grid-export kWh + cash-€ statistics (display only).

Spec: docs/superpowers/specs/2026-08-01-card-crop-daily-stats-design.md

Pure module — NO Home Assistant imports (the local timezone arrives as an
explicit ``tzinfo`` parameter).  Two independent producers feed one merge:

- :func:`aggregate_actual_days` replays recorded per-tick samples into
  measured per-day totals.  Its attribution is *definitionally* the same as
  the live ``ledger.CashLedger``: within one tick every v9 kWh delta column
  is ``W x the same clamped dt`` (see ``recorder.append``), so ``min`` of two
  energies equals ``min`` of the two powers x dt — exactly
  ``optimize.cash_energy_kwh``.
- :func:`aggregate_planned_days` sums the forward plan horizon.

:func:`merge_days` stitches past / today / future into one ordered list.
Never affects control.
"""

from __future__ import annotations

from datetime import date, datetime, tzinfo

# Rolling history depth of the merged table, in days back from today.
WINDOW_DAYS = 14

_ZERO: dict = {
    "grid_charge_kwh": 0.0,
    "grid_export_kwh": 0.0,
    "cost_eur": 0.0,
    "revenue_eur": 0.0,
    "coverage_ticks": 0,
    "null_ticks": 0,
}

# The four v9 per-tick delta columns the attribution needs. A row missing any
# one of them cannot be attributed at all (see aggregate_actual_days).
_DELTA_COLUMNS = ("grid_import_kwh", "grid_export_kwh", "batt_charge_kwh", "batt_discharge_kwh")


def new_day_totals() -> dict:
    """Fresh zeroed ``DayTotals`` dict (plain dict — HA-attribute friendly)."""
    return dict(_ZERO)


def _parse(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


def aggregate_actual_days(
    rows: list[dict],
    export_fee_eur_per_kwh: float,
    tz: tzinfo,
) -> dict[date, dict]:
    """Group recorder sample rows into measured per-local-day totals.

    ``rows`` are column-keyed dicts from ``DataRecorder.read_feature_rows``;
    ``ts`` is an aware-UTC ISO string.  ``export_price`` in the recorder is
    the RAW feed-in tariff, so the fee is subtracted here (mirrors
    ``optimize.effective_export_price``).

    A row missing any of the four v9 delta columns cannot be attributed; it
    increments ``null_ticks`` and contributes nothing, so a gappy day reads as
    gappy rather than silently small.  A NULL price zeroes only its own €
    leg — the kWh is still counted (France runs with no export-price entity).
    """
    out: dict[date, dict] = {}
    for row in rows:
        ts = _parse(row.get("ts"))
        if ts is None:
            continue
        rec = out.setdefault(ts.astimezone(tz).date(), new_day_totals())
        if any(row.get(col) is None for col in _DELTA_COLUMNS):
            rec["null_ticks"] += 1
            continue
        rec["coverage_ticks"] += 1
        grid_charge_kwh = min(float(row["grid_import_kwh"]), float(row["batt_charge_kwh"]))
        batt_export_kwh = min(float(row["grid_export_kwh"]), float(row["batt_discharge_kwh"]))
        rec["grid_charge_kwh"] += grid_charge_kwh
        rec["grid_export_kwh"] += batt_export_kwh
        import_price = row.get("import_price")
        if import_price is not None:
            rec["cost_eur"] += grid_charge_kwh * float(import_price)
        export_price = row.get("export_price")
        if export_price is not None:
            rec["revenue_eur"] += batt_export_kwh * (float(export_price) - export_fee_eur_per_kwh)
    return out
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_daily_stats.py -v`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add custom_components/anker_x1_smartgrid/daily_stats.py tests/test_daily_stats.py
git commit -m "feat(daily-stats): per-day actuals aggregation from recorder samples"
```

---

### Task 4: Parity test — recorded replay vs live ledger

Depends on Task 2 and Task 3. This is the test that keeps the two attribution paths from drifting; it earns its own task because a reviewer could reject it independently.

**Files:**
- Modify: `tests/test_daily_stats.py`

**Interfaces:**
- Consumes: `optimize.cash_energy_kwh`, `optimize.cash_flows_eur`, `daily_stats.aggregate_actual_days`.
- Produces: nothing.

- [ ] **Step 1: Write the test**

Append to `tests/test_daily_stats.py`:

```python
class TestLedgerParity:
    def test_recorded_replay_equals_live_ledger_euros(self):
        """A synthetic tick stream priced both ways must agree to 1e-9.

        Live path:     optimize.cash_flows_eur(meter_w, batt_w, ...) per tick.
        Recorded path: daily_stats.aggregate_actual_days over the SAME ticks
                       expressed as v9 kWh deltas.

        If these ever diverge, one of the two attribution sites has grown its
        own copy of the min() rule.
        """
        from custom_components.anker_x1_smartgrid.optimize import cash_flows_eur

        tick_h = 60.0 / 3600.0
        fee = 0.015
        # (meter_w, batt_w) pairs: grid charge, PV-covered charge, battery
        # export, PV-spill export, mixed idle, and a negative-price hour.
        ticks = [
            (1500.0, -2000.0),
            (200.0, -2000.0),
            (-2500.0, 2500.0),
            (-3000.0, 1000.0),
            (0.0, 0.0),
            (900.0, -400.0),
        ]
        import_price, raw_export_price = 0.31, 0.24

        live_cost = live_credit = 0.0
        rows = []
        base = datetime(2026, 7, 20, 6, 0, tzinfo=UTC)
        for i, (meter_w, batt_w) in enumerate(ticks):
            cost, credit = cash_flows_eur(meter_w, batt_w, import_price, raw_export_price - fee, tick_h)
            live_cost += cost
            live_credit += credit
            rows.append(
                {
                    "ts": (base + timedelta(minutes=i)).isoformat(),
                    # Recorder columns are the UNattributed per-leg deltas;
                    # the min() happens inside aggregate_actual_days.
                    "grid_import_kwh": max(0.0, meter_w) / 1000.0 * tick_h,
                    "grid_export_kwh": max(0.0, -meter_w) / 1000.0 * tick_h,
                    "batt_charge_kwh": max(0.0, -batt_w) / 1000.0 * tick_h,
                    "batt_discharge_kwh": max(0.0, batt_w) / 1000.0 * tick_h,
                    "import_price": import_price,
                    "export_price": raw_export_price,
                }
            )

        out = daily_stats.aggregate_actual_days(rows, fee, CEST)
        day = out[date(2026, 7, 20)]
        assert day["cost_eur"] == pytest.approx(live_cost, abs=1e-9)
        assert day["revenue_eur"] == pytest.approx(live_credit, abs=1e-9)
```

- [ ] **Step 2: Run the test**

Run: `python3 -m pytest tests/test_daily_stats.py::TestLedgerParity -v`
Expected: PASS (Tasks 2 and 3 already provide both sides).

- [ ] **Step 3: Commit**

```bash
git add tests/test_daily_stats.py
git commit -m "test(daily-stats): pin recorded-replay vs live-ledger € parity"
```

---

### Task 5: `daily_stats.aggregate_planned_days`

Depends on Task 3 (same module).

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/daily_stats.py`
- Modify: `tests/test_daily_stats.py`

**Interfaces:**
- Consumes: `daily_stats.new_day_totals`.
- Produces: `daily_stats.aggregate_planned_days(horizon: list[dict], export_price_at: Callable[[datetime], float | None], tz: tzinfo) -> dict[date, dict]`.

**Context:** plan horizon rows (`last_status["plan"]["horizon"]`) carry `start` (ISO string), `price`, `grid_charge_kwh`, `grid_export_kwh`, `estimated` (bool) and `mode`. Past rows are back-filled from measurements and carry `mode == "actual"` — they must be skipped here or today double-counts against the ledger. Estimated rows must be skipped for the same reason Task 1 stops drawing them: the table shows only what real tariff supports. `export_price_at` is caller-supplied so the module stays pure; it returns an **effective** (post-fee) price or `None`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_daily_stats.py`:

```python
def _plan_row(start: datetime, **cols) -> dict:
    base = {
        "start": start.isoformat(),
        "price": 0.30,
        "grid_charge_kwh": 0.0,
        "grid_export_kwh": 0.0,
        "estimated": False,
        "mode": "idle",
    }
    base.update(cols)
    return base


def _flat_export(_start):
    return 0.20


class TestAggregatePlannedDays:
    def test_sums_charge_cost_and_export_revenue(self):
        horizon = [
            _plan_row(datetime(2026, 8, 2, 2, 0, tzinfo=UTC), grid_charge_kwh=2.0, mode="grid"),
            _plan_row(datetime(2026, 8, 2, 17, 0, tzinfo=UTC), grid_export_kwh=1.5, mode="export"),
        ]
        out = daily_stats.aggregate_planned_days(horizon, _flat_export, CEST)
        day = out[date(2026, 8, 2)]
        assert day["grid_charge_kwh"] == pytest.approx(2.0)
        assert day["grid_export_kwh"] == pytest.approx(1.5)
        assert day["cost_eur"] == pytest.approx(2.0 * 0.30)
        assert day["revenue_eur"] == pytest.approx(1.5 * 0.20)

    def test_estimated_rows_are_excluded(self):
        horizon = [
            _plan_row(datetime(2026, 8, 3, 2, 0, tzinfo=UTC), grid_charge_kwh=5.0, estimated=True, mode="estimated"),
        ]
        assert daily_stats.aggregate_planned_days(horizon, _flat_export, CEST) == {}

    def test_actual_mode_rows_are_excluded(self):
        # Past plan rows are back-filled measurements; counting them here
        # would double-count against the ledger's today figures.
        horizon = [
            _plan_row(datetime(2026, 8, 1, 8, 0, tzinfo=UTC), grid_charge_kwh=3.0, mode="actual"),
        ]
        assert daily_stats.aggregate_planned_days(horizon, _flat_export, CEST) == {}

    def test_export_price_none_zeroes_only_the_revenue_leg(self):
        horizon = [
            _plan_row(datetime(2026, 8, 2, 17, 0, tzinfo=UTC), grid_export_kwh=1.5, grid_charge_kwh=1.0, mode="export"),
        ]
        out = daily_stats.aggregate_planned_days(horizon, lambda _s: None, CEST)
        day = out[date(2026, 8, 2)]
        assert day["revenue_eur"] == 0.0
        assert day["grid_export_kwh"] == pytest.approx(1.5)
        assert day["cost_eur"] == pytest.approx(1.0 * 0.30)

    def test_per_slot_export_curve_is_honoured(self):
        peak = datetime(2026, 8, 2, 17, 0, tzinfo=UTC)
        off = datetime(2026, 8, 2, 11, 0, tzinfo=UTC)
        curve = {peak: 0.40, off: 0.05}
        horizon = [
            _plan_row(peak, grid_export_kwh=1.0, mode="export"),
            _plan_row(off, grid_export_kwh=1.0, mode="export"),
        ]
        out = daily_stats.aggregate_planned_days(horizon, lambda s: curve.get(s), CEST)
        assert out[date(2026, 8, 2)]["revenue_eur"] == pytest.approx(0.45)

    def test_buckets_on_the_local_day(self):
        # 22:30Z is 00:30 the next day in CEST.
        horizon = [_plan_row(datetime(2026, 8, 2, 22, 30, tzinfo=UTC), grid_charge_kwh=1.0, mode="grid")]
        out = daily_stats.aggregate_planned_days(horizon, _flat_export, CEST)
        assert set(out) == {date(2026, 8, 3)}

    def test_empty_and_none_horizon(self):
        assert daily_stats.aggregate_planned_days([], _flat_export, CEST) == {}
        assert daily_stats.aggregate_planned_days(None, _flat_export, CEST) == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_daily_stats.py::TestAggregatePlannedDays -v`
Expected: FAIL with `AttributeError: module ... has no attribute 'aggregate_planned_days'`.

- [ ] **Step 3: Implement**

Append to `custom_components/anker_x1_smartgrid/daily_stats.py`:

```python
def aggregate_planned_days(
    horizon: list[dict] | None,
    export_price_at,
    tz: tzinfo,
) -> dict[date, dict]:
    """Group forward plan-horizon rows into planned per-local-day totals.

    Skips two row classes:

    - ``estimated`` rows — the estimated-tomorrow tail past the real price
      edge.  Same rule the card applies (spec 2026-08-01-card-crop): the
      table reports only what real tariff supports.
    - ``mode == "actual"`` rows — past slots back-filled from measurements by
      ``past_actuals``.  Counting them here would double-count against the
      ledger figures ``merge_days`` uses for today.

    ``export_price_at(start) -> float | None`` is caller-supplied (keeps this
    module HA-free) and MUST already be post-fee — see
    ``optimize.effective_export_price``.  ``None`` zeroes the revenue leg only.
    """
    out: dict[date, dict] = {}
    for row in horizon or []:
        if row.get("estimated") or row.get("mode") == "actual":
            continue
        start = _parse(row.get("start"))
        if start is None:
            continue
        rec = out.setdefault(start.astimezone(tz).date(), new_day_totals())
        charge_kwh = float(row.get("grid_charge_kwh") or 0.0)
        export_kwh = float(row.get("grid_export_kwh") or 0.0)
        rec["grid_charge_kwh"] += charge_kwh
        rec["grid_export_kwh"] += export_kwh
        price = row.get("price")
        if price is not None:
            rec["cost_eur"] += charge_kwh * float(price)
        export_price = export_price_at(start)
        if export_price is not None:
            rec["revenue_eur"] += export_kwh * float(export_price)
    return out
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_daily_stats.py -v`
Expected: all pass (Task 3's 8 + Task 4's 1 + 7 new).

- [ ] **Step 5: Commit**

```bash
git add custom_components/anker_x1_smartgrid/daily_stats.py tests/test_daily_stats.py
git commit -m "feat(daily-stats): planned per-day aggregation from the plan horizon"
```

---

### Task 6: `daily_stats.merge_days`

Depends on Tasks 3 and 5.

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/daily_stats.py`
- Modify: `tests/test_daily_stats.py`

**Interfaces:**
- Consumes: the two `dict[date, dict]` maps above.
- Produces: `daily_stats.merge_days(actual: dict[date, dict], planned: dict[date, dict], today_totals: dict, today: date, window_days: int = WINDOW_DAYS) -> list[dict]`.

Each output row has exactly these keys: `date` (ISO string), `grid_charge_kwh`, `grid_export_kwh`, `cost_eur`, `revenue_eur`, `net_eur`, `source` (`"actual"` / `"mixed"` / `"plan"`), `actual_net_eur` (`None` for future days), `planned_net_eur` (`None` for past days).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_daily_stats.py`:

```python
def _totals(charge=0.0, export=0.0, cost=0.0, revenue=0.0) -> dict:
    out = daily_stats.new_day_totals()
    out.update(
        {"grid_charge_kwh": charge, "grid_export_kwh": export, "cost_eur": cost, "revenue_eur": revenue}
    )
    return out


TODAY = date(2026, 8, 1)


class TestMergeDays:
    def test_past_day_is_actual_only(self):
        actual = {date(2026, 7, 31): _totals(charge=4.0, export=2.0, cost=1.20, revenue=0.60)}
        rows = daily_stats.merge_days(actual, {}, _totals(), TODAY)
        row = next(r for r in rows if r["date"] == "2026-07-31")
        assert row["source"] == "actual"
        assert row["net_eur"] == pytest.approx(-0.60)
        assert row["actual_net_eur"] == pytest.approx(-0.60)
        assert row["planned_net_eur"] is None

    def test_future_day_is_plan_only(self):
        planned = {date(2026, 8, 2): _totals(charge=6.0, export=3.0, cost=1.50, revenue=1.10)}
        rows = daily_stats.merge_days({}, planned, _totals(), TODAY)
        row = next(r for r in rows if r["date"] == "2026-08-02")
        assert row["source"] == "plan"
        assert row["net_eur"] == pytest.approx(-0.40)
        assert row["actual_net_eur"] is None
        assert row["planned_net_eur"] == pytest.approx(-0.40)

    def test_today_sums_actual_so_far_and_planned_remainder(self):
        today_totals = _totals(charge=2.0, export=1.0, cost=0.50, revenue=0.30)
        planned = {TODAY: _totals(charge=1.0, export=4.0, cost=0.25, revenue=1.40)}
        rows = daily_stats.merge_days({}, planned, today_totals, TODAY)
        row = next(r for r in rows if r["date"] == "2026-08-01")
        assert row["source"] == "mixed"
        assert row["grid_charge_kwh"] == pytest.approx(3.0)
        assert row["grid_export_kwh"] == pytest.approx(5.0)
        assert row["actual_net_eur"] == pytest.approx(-0.20)
        assert row["planned_net_eur"] == pytest.approx(1.15)
        assert row["net_eur"] == pytest.approx(0.95)

    def test_live_ledger_wins_over_a_samples_entry_for_today(self):
        # The samples pass also covers today; the ledger is authoritative
        # because the card subtitle already publishes it.
        actual = {TODAY: _totals(charge=99.0, cost=99.0)}
        rows = daily_stats.merge_days(actual, {}, _totals(charge=2.0, cost=0.50), TODAY)
        row = next(r for r in rows if r["date"] == "2026-08-01")
        assert row["grid_charge_kwh"] == pytest.approx(2.0)
        assert row["cost_eur"] == pytest.approx(0.50)

    def test_today_always_present_even_with_no_data(self):
        rows = daily_stats.merge_days({}, {}, _totals(), TODAY)
        assert [r["date"] for r in rows] == ["2026-08-01"]

    def test_rows_are_ordered_oldest_first(self):
        actual = {date(2026, 7, 30): _totals(), date(2026, 7, 31): _totals()}
        planned = {date(2026, 8, 2): _totals()}
        rows = daily_stats.merge_days(actual, planned, _totals(), TODAY)
        assert [r["date"] for r in rows] == ["2026-07-30", "2026-07-31", "2026-08-01", "2026-08-02"]

    def test_days_older_than_the_window_are_dropped(self):
        actual = {
            date(2026, 7, 18): _totals(charge=1.0),  # exactly 14 days back — kept
            date(2026, 7, 17): _totals(charge=1.0),  # 15 days back — dropped
        }
        rows = daily_stats.merge_days(actual, {}, _totals(), TODAY)
        dates = [r["date"] for r in rows]
        assert "2026-07-18" in dates
        assert "2026-07-17" not in dates

    def test_window_days_is_configurable(self):
        actual = {date(2026, 7, 30): _totals(charge=1.0), date(2026, 7, 29): _totals(charge=1.0)}
        rows = daily_stats.merge_days(actual, {}, _totals(), TODAY, window_days=2)
        assert [r["date"] for r in rows] == ["2026-07-30", "2026-08-01"]

    def test_date_is_an_iso_string_not_a_date_object(self):
        rows = daily_stats.merge_days({}, {}, _totals(), TODAY)
        assert isinstance(rows[0]["date"], str)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_daily_stats.py::TestMergeDays -v`
Expected: FAIL with `AttributeError: module ... has no attribute 'merge_days'`.

- [ ] **Step 3: Implement**

Append to `custom_components/anker_x1_smartgrid/daily_stats.py`:

```python
def merge_days(
    actual: dict[date, dict],
    planned: dict[date, dict],
    today_totals: dict,
    today: date,
    window_days: int = WINDOW_DAYS,
) -> list[dict]:
    """Stitch measured past / mixed today / planned future into one table.

    ``today_totals`` is the LIVE ledger's ``DayTotals`` for ``today``; it wins
    over any ``actual[today]`` the samples pass produced, so the table's today
    row equals the number the card subtitle already publishes.  The two differ
    by the dt seam documented in the spec's "Accepted consequences".

    ``today`` always yields a row even with no data, so an empty table still
    shows the current day.  Rows more than ``window_days`` before ``today``
    are dropped; future rows are bounded by the plan horizon itself.
    """
    days = {d for d in actual if d < today} | {d for d in planned if d > today} | {today}
    rows: list[dict] = []
    for day in sorted(days):
        if (today - day).days > window_days:
            continue
        if day < today:
            a, p = actual.get(day), None
        elif day > today:
            a, p = None, planned.get(day)
        else:
            a, p = today_totals, planned.get(day)
        if a is None and p is None:
            continue
        a_cost = a["cost_eur"] if a else 0.0
        a_rev = a["revenue_eur"] if a else 0.0
        p_cost = p["cost_eur"] if p else 0.0
        p_rev = p["revenue_eur"] if p else 0.0
        rows.append(
            {
                "date": day.isoformat(),
                "grid_charge_kwh": round(
                    (a["grid_charge_kwh"] if a else 0.0) + (p["grid_charge_kwh"] if p else 0.0), 3
                ),
                "grid_export_kwh": round(
                    (a["grid_export_kwh"] if a else 0.0) + (p["grid_export_kwh"] if p else 0.0), 3
                ),
                "cost_eur": round(a_cost + p_cost, 3),
                "revenue_eur": round(a_rev + p_rev, 3),
                "net_eur": round((a_rev - a_cost) + (p_rev - p_cost), 3),
                "source": "mixed" if (a is not None and p is not None) else ("actual" if a is not None else "plan"),
                "actual_net_eur": round(a_rev - a_cost, 3) if a is not None else None,
                "planned_net_eur": round(p_rev - p_cost, 3) if p is not None else None,
            }
        )
    return rows
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_daily_stats.py -v`
Expected: all pass (24 total).

- [ ] **Step 5: Lint**

Run: `ruff check custom_components/anker_x1_smartgrid/daily_stats.py tests/test_daily_stats.py`
Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add custom_components/anker_x1_smartgrid/daily_stats.py tests/test_daily_stats.py
git commit -m "feat(daily-stats): merge measured past, mixed today and planned future"
```

---

### Task 7: `CashLedger` kWh accumulators

Depends on Task 2. Independent of Tasks 3-6.

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/ledger.py:34-40,42-57,94-103`
- Modify: `custom_components/anker_x1_smartgrid/controller.py:143-147` (`_PERSIST_GROUPS`) and the ledger property block near `:339`
- Modify: `tests/test_cash_ledger.py`

**Interfaces:**
- Consumes: `optimize.cash_energy_kwh`.
- Produces: `CashLedger.today_grid_charge_kwh: float`, `CashLedger.today_export_kwh: float`, mirrored as `Controller.today_grid_charge_kwh` / `Controller.today_export_kwh` properties (Task 8 reads them).

**Context:** `_PERSIST_GROUPS` is a table of `(payload_key, controller_attr, to_payload, from_payload, allow_none)` tuples; each inner list is one try/except group. `CashLedger` fields are exposed on `Controller` via same-named properties so this table keeps working. The new fields join the existing cash-ledger group so a mid-day restart resumes accumulating.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cash_ledger.py`:

```python
class TestLedgerEnergyAccumulators:
    def _ledger(self):
        from custom_components.anker_x1_smartgrid.ledger import CashLedger

        return CashLedger()

    def test_new_ledger_starts_at_zero(self):
        led = self._ledger()
        assert led.today_grid_charge_kwh == 0.0
        assert led.today_export_kwh == 0.0

    def test_rollover_resets_both_energy_accumulators(self):
        led = self._ledger()
        led.day = "2026-07-31"
        led.today_grid_charge_kwh = 4.0
        led.today_export_kwh = 2.0
        led.today_charge_cost_eur = 1.0
        led.rollover(datetime(2026, 8, 1, 3, 0, tzinfo=UTC))
        assert led.today_grid_charge_kwh == 0.0
        assert led.today_export_kwh == 0.0
        assert led.today_charge_cost_eur == 0.0

    def test_same_day_rollover_preserves_energy_accumulators(self):
        led = self._ledger()
        led.rollover(datetime(2026, 8, 1, 3, 0, tzinfo=UTC))
        led.today_grid_charge_kwh = 4.0
        led.rollover(datetime(2026, 8, 1, 4, 0, tzinfo=UTC))
        assert led.today_grid_charge_kwh == 4.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_cash_ledger.py::TestLedgerEnergyAccumulators -v`
Expected: FAIL with `AttributeError: 'CashLedger' object has no attribute 'today_grid_charge_kwh'`.

- [ ] **Step 3: Add the fields, reset, and accumulation**

In `custom_components/anker_x1_smartgrid/ledger.py`, add two fields to the dataclass immediately after `total_net_eur`:

```python
    total_net_eur: float = 0.0
    # Measured kWh companions to the € legs above, same attribution
    # (optimize.cash_energy_kwh) and same daily reset — daily_stats reports
    # today's row from these rather than re-querying samples.
    today_grid_charge_kwh: float = 0.0
    today_export_kwh: float = 0.0
```

In `rollover`, add both to the reset block (the docstring already requires EVERY daily field reset in that one pass):

```python
        if _today != self.day:
            self.today_export_pnl_eur = 0.0
            self.today_charge_cost_eur = 0.0
            self.today_export_revenue_eur = 0.0
            self.today_grid_charge_kwh = 0.0
            self.today_export_kwh = 0.0
            self.day = _today
```

In `accumulate`, replace the final assignment block so the kWh come from the shared helper:

```python
        cost, credit = optimize_mod.cash_flows_eur(
            inputs.meter_w,
            batt_w,
            import_price,
            export_price_eff,
            const.TICK_SECONDS / 3600.0,
        )
        charge_kwh, export_kwh = optimize_mod.cash_energy_kwh(
            inputs.meter_w,
            batt_w,
            const.TICK_SECONDS / 3600.0,
        )
        self.today_charge_cost_eur += cost
        self.today_export_revenue_eur += credit
        self.total_net_eur += credit - cost
        # Energy legs accumulate unconditionally — unlike the € legs they do
        # not depend on a price being available (France runs with no
        # export-price entity but still exports real kWh).
        self.today_grid_charge_kwh += charge_kwh
        self.today_export_kwh += export_kwh
```

- [ ] **Step 4: Expose the fields on Controller and persist them**

In `custom_components/anker_x1_smartgrid/controller.py`, add both to the cash-ledger group in `_PERSIST_GROUPS`:

```python
    [
        ("today_charge_cost_eur", "today_charge_cost_eur", lambda v: v, float, False),
        ("today_export_revenue_eur", "today_export_revenue_eur", lambda v: v, float, False),
        ("total_net_eur", "total_net_eur", lambda v: v, float, False),
        ("today_grid_charge_kwh", "today_grid_charge_kwh", lambda v: v, float, False),
        ("today_export_kwh", "today_export_kwh", lambda v: v, float, False),
    ],
```

Then add two property/setter pairs alongside the existing ledger properties (near `:339`), matching their style exactly:

```python
    @property
    def today_grid_charge_kwh(self) -> float:
        return self._ledger.today_grid_charge_kwh

    @today_grid_charge_kwh.setter
    def today_grid_charge_kwh(self, value: float) -> None:
        self._ledger.today_grid_charge_kwh = value

    @property
    def today_export_kwh(self) -> float:
        return self._ledger.today_export_kwh

    @today_export_kwh.setter
    def today_export_kwh(self, value: float) -> None:
        self._ledger.today_export_kwh = value
```

- [ ] **Step 5: Add the persistence round-trip test**

Append to `tests/test_cash_ledger.py`:

```python
class TestLedgerEnergyPersistence:
    async def test_energy_accumulators_survive_persist_restore(self):
        from tests.helpers import CapturingStore, make_controller

        # make_controller returns (controller, actuator) and always builds its
        # own StubStore — swap in a CapturingStore to read the payload back.
        ctrl, _act = make_controller()
        ctrl._store = CapturingStore()
        ctrl.today_grid_charge_kwh = 3.5
        ctrl.today_export_kwh = 1.25
        await ctrl._persist()

        # CapturingStore.saved holds the LAST payload (a dict, not a list).
        payload = ctrl._store.saved
        assert payload["today_grid_charge_kwh"] == pytest.approx(3.5)
        assert payload["today_export_kwh"] == pytest.approx(1.25)

        fresh, _ = make_controller()
        fresh.restore(payload)
        assert fresh.today_grid_charge_kwh == pytest.approx(3.5)
        assert fresh.today_export_kwh == pytest.approx(1.25)
```

`Controller._persist` is async; `Controller.restore` is sync. Do not modify `tests/helpers.py`.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_cash_ledger.py -v`
Expected: all pass, including the pre-existing ledger tests.

- [ ] **Step 7: Commit**

```bash
git add custom_components/anker_x1_smartgrid/ledger.py custom_components/anker_x1_smartgrid/controller.py tests/test_cash_ledger.py
git commit -m "feat(ledger): daily grid-charge / export kWh accumulators"
```

---

### Task 8: Controller wiring — cached backfill + per-tick publish

Depends on Tasks 3, 5, 6, 7.

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/controller.py` — grouped import block at `:19-41`, `__init__` (near `:181`), `_tick_impl` at `:1478` and `:1600-1604`, plus two new methods
- Create: `tests/test_daily_stats_controller.py`

**Interfaces:**
- Consumes: `daily_stats.aggregate_actual_days`, `daily_stats.aggregate_planned_days`, `daily_stats.merge_days`, `daily_stats.WINDOW_DAYS`, `Controller.today_grid_charge_kwh`, `Controller.today_export_kwh`, `optimize_mod.effective_export_price`, `resolution.resample_price_map`.
- Produces: `Controller.last_status["daily_stats"]: list[dict]` — read by Task 9's sensor.

**Context:** `_tick_impl` (`async def`, starts `:1005`) has `now`, `inputs`, `slots`, `_slot_minutes`, `_export_price`, `_export_slots` and `horizon` all in scope. 14 days of `samples` is ~20k rows — far too heavy for a 60s tick, so closed-day aggregation runs only when the local-day key moves (and once on the first tick after a restart).

- [ ] **Step 1: Write the failing test**

Create `tests/test_daily_stats_controller.py`:

```python
"""Controller wiring for the per-day statistics table.

Spec: docs/superpowers/specs/2026-08-01-card-crop-daily-stats-design.md
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, UTC

import pytest

from custom_components.anker_x1_smartgrid import daily_stats

# make_controller returns (controller, actuator); the controller holds its
# StubRecorder at ._recorder and its Config at .cfg (Config is frozen, so
# overrides go through dataclasses.replace).


class TestActualsCache:
    async def test_backfill_runs_once_per_local_day(self):
        from tests.helpers import make_controller

        ctrl, _act = make_controller()
        calls = []

        def _fake_read(since_iso):
            calls.append(since_iso)
            return []

        ctrl._recorder.read_feature_rows = _fake_read

        now = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
        await ctrl._refresh_daily_actuals(now)
        await ctrl._refresh_daily_actuals(now + timedelta(hours=3))
        assert len(calls) == 1, "same local day must not re-query"

        await ctrl._refresh_daily_actuals(now + timedelta(days=1))
        assert len(calls) == 2, "new local day must re-query"

    async def test_backfill_window_reaches_one_day_past_the_table_window(self):
        from tests.helpers import make_controller

        ctrl, _act = make_controller()
        seen = []
        ctrl._recorder.read_feature_rows = lambda since_iso: seen.append(since_iso) or []

        now = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
        await ctrl._refresh_daily_actuals(now)
        since = datetime.fromisoformat(seen[0])
        assert (now - since).days == daily_stats.WINDOW_DAYS + 1

    async def test_recorder_failure_leaves_the_cache_intact(self):
        from tests.helpers import make_controller

        ctrl, _act = make_controller()
        ctrl._daily_actuals = {date(2026, 7, 31): daily_stats.new_day_totals()}
        ctrl._daily_actuals_day = "2026-07-31"

        def _boom(_since):
            raise RuntimeError("sqlite is unhappy")

        ctrl._recorder.read_feature_rows = _boom
        await ctrl._refresh_daily_actuals(datetime(2026, 8, 1, 10, 0, tzinfo=UTC))
        assert set(ctrl._daily_actuals) == {date(2026, 7, 31)}
        assert ctrl._daily_actuals_day == "2026-07-31", "a failed refresh must not advance the key"


class TestPublishDailyStats:
    async def test_publishes_a_merged_table_with_today_from_the_ledger(self):
        from tests.helpers import make_controller

        ctrl, _act = make_controller()
        ctrl._daily_actuals = {}
        ctrl._daily_actuals_day = None
        ctrl.today_grid_charge_kwh = 2.0
        ctrl.today_export_kwh = 1.0
        ctrl.today_charge_cost_eur = 0.60
        ctrl.today_export_revenue_eur = 0.25

        now = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
        ctrl._publish_daily_stats(now, horizon=[], export_price=None, export_slots=None, slot_minutes=60)

        table = ctrl.last_status["daily_stats"]
        assert isinstance(table, list) and table, table
        today_row = table[-1]
        assert today_row["grid_charge_kwh"] == pytest.approx(2.0)
        assert today_row["grid_export_kwh"] == pytest.approx(1.0)
        assert today_row["net_eur"] == pytest.approx(-0.35)

    async def test_flat_export_price_has_the_fee_subtracted(self):
        from tests.helpers import make_controller

        ctrl, _act = make_controller()
        ctrl._daily_actuals = {}
        ctrl._daily_actuals_day = None
        # Config is @dataclass(frozen=True) — assignment to a field raises
        # FrozenInstanceError; swap the whole object instead.
        ctrl.cfg = replace(ctrl.cfg, export_fee_eur_per_kwh=0.02)

        now = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
        horizon = [
            {
                "start": datetime(2026, 8, 2, 17, 0, tzinfo=UTC).isoformat(),
                "price": 0.30,
                "grid_charge_kwh": 0.0,
                "grid_export_kwh": 2.0,
                "estimated": False,
                "mode": "export",
            }
        ]
        ctrl._publish_daily_stats(now, horizon=horizon, export_price=0.22, export_slots=None, slot_minutes=60)

        future = next(r for r in ctrl.last_status["daily_stats"] if r["source"] == "plan")
        assert future["revenue_eur"] == pytest.approx(2.0 * 0.20)
```

Note: `tests.helpers.StubRecorder` already implements `read_feature_rows(since_iso=None)` (returns `[]` when no rows were appended), so adding the `_refresh_daily_actuals` call to `_tick_impl` does not break existing controller tests. Do not modify `tests/helpers.py`.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_daily_stats_controller.py -v`
Expected: FAIL with `AttributeError: 'Controller' object has no attribute '_refresh_daily_actuals'`.

- [ ] **Step 3: Import the module and initialise the cache**

In `custom_components/anker_x1_smartgrid/controller.py`, add `daily_stats,` to the grouped `from . import (...)` block, alphabetically between `coordinator,` and `energy,`.

In `Controller.__init__`, alongside the other per-instance caches, add:

```python
        # Per-day statistics: closed days never change, so the measured half
        # is aggregated once per local day (see _refresh_daily_actuals) rather
        # than re-querying ~20k sample rows every 60s tick.
        self._daily_actuals: dict[date, dict] = {}
        self._daily_actuals_day: str | None = None
```

(`date` is already imported at `:11`.)

- [ ] **Step 4: Add the two methods**

Add near the other ledger helpers (after `_accumulate_cash_ledger`, around `:1753`):

```python
    async def _refresh_daily_actuals(self, now: datetime) -> None:
        """Re-aggregate the closed-day actuals cache (startup + once per local day).

        WINDOW_DAYS+1 days of samples is ~20k rows — far too heavy for a 60s
        tick — but closed days never change, so this only runs when the local
        day key moves (``None`` on the first tick after a restart).  A failed
        read leaves both the cache AND the key untouched so the next tick
        retries rather than pinning an empty table for the rest of the day.
        """
        _today = dt_util.as_local(now).date().isoformat()
        if self._daily_actuals_day == _today:
            return
        _since = (now - timedelta(days=daily_stats.WINDOW_DAYS + 1)).isoformat()
        try:
            rows = await self._hass.async_add_executor_job(self._recorder.read_feature_rows, _since)
        except Exception:
            _LOGGER.warning("daily stats: actuals backfill failed; keeping previous cache", exc_info=True)
            return
        self._daily_actuals = daily_stats.aggregate_actual_days(
            rows, self.cfg.export_fee_eur_per_kwh, dt_util.DEFAULT_TIME_ZONE
        )
        self._daily_actuals_day = _today

    def _publish_daily_stats(
        self,
        now: datetime,
        horizon: list[dict],
        export_price: float | None,
        export_slots: list[PriceSlot] | None,
        slot_minutes: int,
    ) -> None:
        """Merge cached actuals + live ledger + plan horizon into last_status.

        Cheap: the measured half is already cached and the horizon is in
        memory, so this runs every tick.  Export is valued at the per-slot
        curve where one is supplied, else the flat entity price; both are put
        through effective_export_price so the fee is applied exactly once.
        """
        _curve = resolution.resample_price_map(export_slots, slot_minutes) if export_slots else {}
        _flat = optimize_mod.effective_export_price(export_price, self.cfg) if export_price is not None else None

        def _export_price_at(start: datetime) -> float | None:
            _raw = _curve.get(start)
            return _flat if _raw is None else optimize_mod.effective_export_price(_raw, self.cfg)

        _tz = dt_util.DEFAULT_TIME_ZONE
        _today_totals = daily_stats.new_day_totals()
        _today_totals.update(
            {
                "grid_charge_kwh": self._ledger.today_grid_charge_kwh,
                "grid_export_kwh": self._ledger.today_export_kwh,
                "cost_eur": self._ledger.today_charge_cost_eur,
                "revenue_eur": self._ledger.today_export_revenue_eur,
            }
        )
        self.last_status["daily_stats"] = daily_stats.merge_days(
            self._daily_actuals,
            daily_stats.aggregate_planned_days(horizon, _export_price_at, _tz),
            _today_totals,
            dt_util.as_local(now).date(),
        )
```

- [ ] **Step 5: Call both from `_tick_impl`**

Immediately after `self._rollover_daily_ledgers(now)` (`:1478`), add:

```python
        # Per-day statistics: refresh the closed-day cache when the local day
        # key moves. Placed right after the ledger rollover so the cache and
        # the ledger agree on which day "today" is.
        await self._refresh_daily_actuals(now)
```

Immediately after the `self.last_status["plan"] = {...}` assignment (ends `:1604`), add:

```python
        self._publish_daily_stats(now, horizon, _export_price, _export_slots, _slot_minutes)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_daily_stats_controller.py -v`
Expected: 5 passed.

- [ ] **Step 7: Run the controller regression suite**

Run: `python3 -m pytest tests/ -q -k "controller or ledger or daily_stats"`
Expected: all pass. `_tick_impl` gained two calls; any stub recorder lacking `read_feature_rows` will surface here.

- [ ] **Step 8: Commit**

```bash
git add custom_components/anker_x1_smartgrid/controller.py tests/test_daily_stats_controller.py
git commit -m "feat(controller): publish the per-day grid/€ statistics table"
```

---

### Task 9: `sensor.smartgrid_daily_stats`

Depends on Task 8.

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/sensor.py` — add the class near `X1FictivePlanSensor` (`:347`) and register it in `async_setup_entry` (`:376+`)
- Create: `tests/test_daily_stats_sensor.py`

**Interfaces:**
- Consumes: `Controller.last_status["daily_stats"]`.
- Produces: entity `sensor.smartgrid_daily_stats` — state = row count, attributes `days` + `window_days`.

**Context:** state is deliberately **not** today's € — that would duplicate `sensor.smartgrid_battery_net_today` and invite the two visibly drifting. `days` goes in `_unrecorded_attributes` for the same reason `X1PlanSensor.horizon` does: a per-tick-changing list blob bloats the HA recorder and the card reads live state.

- [ ] **Step 1: Write the failing test**

Create `tests/test_daily_stats_sensor.py`:

```python
"""Sensor exposure of the per-day statistics table.

Spec: docs/superpowers/specs/2026-08-01-card-crop-daily-stats-design.md
"""

from __future__ import annotations

import pytest

from custom_components.anker_x1_smartgrid import daily_stats
from custom_components.anker_x1_smartgrid.sensor import X1DailyStatsSensor


class _StubController:
    def __init__(self, status):
        self.last_status = status


def _rows():
    return [
        {"date": "2026-07-31", "grid_charge_kwh": 4.0, "grid_export_kwh": 2.0, "net_eur": -0.6, "source": "actual"},
        {"date": "2026-08-01", "grid_charge_kwh": 3.0, "grid_export_kwh": 5.0, "net_eur": 0.95, "source": "mixed"},
    ]


def test_state_is_the_row_count():
    sensor = X1DailyStatsSensor(_StubController({"daily_stats": _rows()}), "entry")
    assert sensor.native_value == 2


def test_attributes_carry_the_table_and_the_window():
    sensor = X1DailyStatsSensor(_StubController({"daily_stats": _rows()}), "entry")
    attrs = sensor.extra_state_attributes
    assert attrs["days"] == _rows()
    assert attrs["window_days"] == daily_stats.WINDOW_DAYS


def test_missing_table_is_zero_rows_not_a_crash():
    # First tick after a restart, before _publish_daily_stats has run.
    sensor = X1DailyStatsSensor(_StubController({}), "entry")
    assert sensor.native_value == 0
    assert sensor.extra_state_attributes["days"] == []


def test_days_blob_is_not_recorded():
    assert "days" in X1DailyStatsSensor._unrecorded_attributes
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_daily_stats_sensor.py -v`
Expected: FAIL with `ImportError: cannot import name 'X1DailyStatsSensor'`.

- [ ] **Step 3: Add the sensor class**

In `custom_components/anker_x1_smartgrid/sensor.py`, add `daily_stats` to the `from . import const, coordinator` line so it reads `from . import const, coordinator, daily_stats`, then add the class immediately after `X1FictivePlanSensor`:

```python
class X1DailyStatsSensor(_Base):
    """Per-day grid-charge / grid-export kWh + net € table (spec 2026-08-01).

    Reads ``last_status["daily_stats"]`` published by the controller's
    ``_publish_daily_stats``.  Entity-id: sensor.smartgrid_daily_stats.

    State is the ROW COUNT, deliberately not today's € — that number already
    has an entity (sensor.smartgrid_battery_net_today) and two entities
    publishing the same figure would eventually be seen to disagree.
    """

    # A ~15-row list that changes every tick bloats the recorder DB for no
    # gain; the card reads live state. Same reasoning as X1PlanSensor.horizon.
    _unrecorded_attributes = frozenset({"days"})

    def __init__(self, c, e):
        super().__init__(c, e, "daily_stats", "SmartGrid daily stats")

    @property
    def native_value(self):
        return len(self._controller.last_status.get("daily_stats") or [])

    @property
    def extra_state_attributes(self):
        return {
            "days": self._controller.last_status.get("daily_stats") or [],
            "window_days": daily_stats.WINDOW_DAYS,
        }
```

- [ ] **Step 4: Register the entity**

In `async_setup_entry`, add it to the `async_add_entities([...])` list immediately after `X1FictivePlanSensor(controller, entry.entry_id),`:

```python
            X1DailyStatsSensor(controller, entry.entry_id),
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_daily_stats_sensor.py -v`
Expected: 4 passed.

- [ ] **Step 6: Run the sensor regression suite**

Run: `python3 -m pytest tests/ -q -k "sensor"`
Expected: all pass. Any test asserting an exact entity count will fail here and must be updated to include the new entity.

- [ ] **Step 7: Commit**

```bash
git add custom_components/anker_x1_smartgrid/sensor.py tests/test_daily_stats_sensor.py
git commit -m "feat(sensor): expose the per-day grid/€ statistics table"
```

---

### Task 10: Daily-stats Lovelace card + full-suite gate

Depends on Task 9.

**Files:**
- Create: `lovelace/daily-stats-card.yaml`
- Modify: `README.md`

**Interfaces:**
- Consumes: `sensor.smartgrid_daily_stats`.
- Produces: nothing.

- [ ] **Step 1: Write the card**

Create `lovelace/daily-stats-card.yaml`:

```yaml
# Anker X1 SmartGrid — per-day grid charge / export / € table
#
# Spec: docs/superpowers/specs/2026-08-01-card-crop-daily-stats-design.md
#
# No HACS dependency — `markdown` is a core Lovelace card. Paste this below
# the apexcharts plan card.
#
# Rows: past days are MEASURED (recorder samples, same attribution as the
# cash ledger), today is measured-so-far PLUS planned-remainder, future days
# are PLAN only. Future/mixed rows are marked so plan is never mistaken for
# measurement.
#
# € is the battery cash basis: export revenue − grid-charge cost. No
# cycle-cost or opportunity deductions (that is the separate economic PnL).
# Where no export-price entity is configured (e.g. the France instance) the
# revenue leg reads 0 by design — kWh is still measured.
type: markdown
content: >-
  {% set days = state_attr('sensor.smartgrid_daily_stats', 'days') or [] %}

  | Day | Charge | Export | Net |

  |---|--:|--:|--:|

  {% for d in days -%}
  | {{ d.date[5:] }}{% if d.source != 'actual' %} *{% endif %} |
  {{ '%.1f' | format(d.grid_charge_kwh) }} kWh |
  {{ '%.1f' | format(d.grid_export_kwh) }} kWh |
  {{ '%+.2f' | format(d.net_eur) }} € |

  {% endfor %}

  {% if days %}<sub>* includes planned (not yet measured)</sub>{% else %}<sub>No data yet.</sub>{% endif %}
```

- [ ] **Step 2: Verify the YAML parses**

Run: `python3 -c "import yaml;print(yaml.safe_load(open('lovelace/daily-stats-card.yaml'))['type'])"`
Expected: `markdown`

- [ ] **Step 3: Document both cards**

In `README.md`, find the section that tells the user to paste `lovelace/apexcharts-plan-card.yaml`. Add a sentence there noting that the plan chart now ends at the last real tariff slot (the estimated tail is no longer drawn), and add `lovelace/daily-stats-card.yaml` as a second paste-able card showing per-day grid charge / export / net €. Match the surrounding heading level and prose style.

- [ ] **Step 4: Run the full suite**

Run: `python3 -m pytest tests/ -q`
Expected: all pass. Compare the count against the pre-change baseline (`git stash && python3 -m pytest tests/ -q` if unsure) — the delta must be exactly the tests added by this plan.

- [ ] **Step 5: Lint**

Run: `ruff check custom_components tests`
Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add lovelace/daily-stats-card.yaml README.md
git commit -m "feat(card): per-day grid charge / export / € markdown table"
```

---

## Deployment notes (not part of the task list)

- Integration-only change. Per memory `haos-deploy`, `scp` to `/config/custom_components` and keep backups **outside** that directory (`haos-deploy-backup-gotcha`).
- Both card YAMLs need a manual paste into Lovelace; the chart card changes shape (7 series instead of 10), so a hard refresh (Ctrl-F5) is needed after pasting.
- The addon is untouched — no lockstep sync required.
- First tick after deploy runs the 15-day backfill in an executor thread; expect one slightly slower tick, then nothing until midnight.
