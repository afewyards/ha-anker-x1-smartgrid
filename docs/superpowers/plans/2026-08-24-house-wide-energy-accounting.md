# House-wide energy accounting + past-attribution parity — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Report whole-house grid import/export (kWh + €) beside the existing battery-basis figures, and make the chart's measured past use the ledger's attribution.

**Architecture:** Part 1 (T1-T6, T8) is additive — one new attribution helper in `optimize.py` mirrored on both paths (live ledger + recorded-sample replay), house legs through both `daily_stats` aggregators, four `CashLedger` accumulators for today's live row, new card columns. Part 2 (T7) swaps `past_actuals`'s PV-surplus split for per-tick `min()`.

**Tech Stack:** Python 3.12, HA custom integration, pytest + pytest-homeassistant-custom-component, PyYAML (card test).

**Spec:** `docs/superpowers/specs/2026-08-24-house-wide-energy-accounting-design.md`

## Global Constraints

- Baseline: main `909a68c`. Repo root `/Users/kleist/Sites/x1-smartcharge`.
- **Tests MUST run in the project venv:** `source .venv/bin/activate && python3 -m pytest`. Bare `python3` is 3.9, has no `homeassistant`, and `tests/conftest.py` fails to import so pytest never collects. If pytest will not run, STOP and escalate. `asyncio_mode = "auto"` — async tests need no decorator.
- Lint: `ruff check custom_components tests` must pass. `line-length = 120`, `target-version = "py312"`.
- `daily_stats.py` and `past_actuals.py` MUST NOT import Home Assistant. Timezone arrives as an explicit `tzinfo` parameter.
- € basis is **net of `export_fee_eur_per_kwh`** everywhere. No gross key.
- Existing `daily_stats` keys keep their names and meaning. Part 1 is purely additive.
- Sign conventions: `meter_w`/`p1_w` positive = import; `batt_w` positive = discharge.
- Never put ticket refs or change history in code comments.
- Use `bunx`, not `npx` (not needed here, but repo-wide).

---

### Task 1: `house_energy_kwh` attribution helper

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/optimize.py` (beside `cash_energy_kwh`, ~L303)
- Test: `tests/test_daily_stats.py`

**Interfaces:**
- Produces: `optimize.house_energy_kwh(meter_w: float, tick_h: float) -> tuple[float, float]` returning `(house_import_kwh, house_export_kwh)`. Used by T2 and T6.

- [ ] **Step 1: Write the failing test**

```python
class TestHouseEnergyKwh:
    def test_import_leg_only_when_meter_positive(self):
        assert optimize.house_energy_kwh(1000.0, 1.0) == pytest.approx((1.0, 0.0))

    def test_export_leg_only_when_meter_negative(self):
        assert optimize.house_energy_kwh(-1000.0, 0.5) == pytest.approx((0.0, 0.5))

    def test_zero_meter_is_both_legs_zero(self):
        assert optimize.house_energy_kwh(0.0, 1.0) == (0.0, 0.0)
```

Add `from custom_components.anker_x1_smartgrid import optimize` to the imports.

- [ ] **Step 2: Verify it fails**

Run: `python3 -m pytest tests/test_daily_stats.py::TestHouseEnergyKwh -v`
Expected: FAIL, `AttributeError: module ... has no attribute 'house_energy_kwh'`

- [ ] **Step 3: Implement**

```python
def house_energy_kwh(meter_w: float, tick_h: float) -> tuple[float, float]:
    """``(house_import_kwh, house_export_kwh)`` at the meter for one tick.

    THE single definition of the whole-house attribution, deliberately beside
    ``cash_energy_kwh``: that one attributes the BATTERY's share, this one the
    whole house.  ``meter_w`` positive = grid import, negative = export.

    Byte-identical to what ``recorder.append`` writes into the
    ``grid_import_kwh`` / ``grid_export_kwh`` columns, so the live ledger and
    the recorded-sample replay cannot drift.
    """
    return max(0.0, meter_w) / 1000.0 * tick_h, max(0.0, -meter_w) / 1000.0 * tick_h
```

- [ ] **Step 4: Verify it passes**

Run: `python3 -m pytest tests/test_daily_stats.py::TestHouseEnergyKwh -v` → PASS

- [ ] **Step 5: Commit**

```bash
git add custom_components/anker_x1_smartgrid/optimize.py tests/test_daily_stats.py
git commit -m "feat(stats): define whole-house meter attribution beside the battery one"
```

---

### Task 2: House legs in `aggregate_actual_days`

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/daily_stats.py` (`_ZERO`, `_DELTA_COLUMNS`, `aggregate_actual_days`)
- Test: `tests/test_daily_stats.py`

**Interfaces:**
- Consumes: nothing from T1 (reads recorder columns directly — the columns T1's helper mirrors).
- Produces: `DayTotals` keys `house_import_kwh`, `house_export_kwh`, `house_cost_eur`, `house_revenue_eur`, `house_null_ticks`. Used by T4, T5, T6.

**Why not reuse `house_energy_kwh` here:** the recorder already integrated each tick into `grid_import_kwh`/`grid_export_kwh`; re-deriving from a power would double-apply dt.

- [ ] **Step 1: Write the failing tests**

```python
class TestHouseLegsActual:
    def test_house_legs_count_all_grid_flow_not_just_the_battery_share(self):
        # Imported 0.05 while the battery took 0.02: battery leg 0.02, house 0.05.
        rows = [_row(datetime(2026, 7, 20, 10, 0, tzinfo=UTC), grid_import_kwh=0.05, batt_charge_kwh=0.02)]
        day = daily_stats.aggregate_actual_days(rows, 0.0, CEST)[date(2026, 7, 20)]
        assert day["grid_charge_kwh"] == pytest.approx(0.02)
        assert day["house_import_kwh"] == pytest.approx(0.05)
        assert day["house_cost_eur"] == pytest.approx(0.05 * 0.30)

    def test_pv_spill_export_counts_for_the_house_but_not_the_battery(self):
        # Exported 0.04 with the battery idle: pure PV spill.
        rows = [_row(datetime(2026, 7, 20, 12, 0, tzinfo=UTC), grid_export_kwh=0.04, batt_discharge_kwh=0.0)]
        day = daily_stats.aggregate_actual_days(rows, 0.05, CEST)[date(2026, 7, 20)]
        assert day["grid_export_kwh"] == pytest.approx(0.0)
        assert day["house_export_kwh"] == pytest.approx(0.04)
        assert day["house_revenue_eur"] == pytest.approx(0.04 * (0.25 - 0.05))

    def test_null_battery_columns_still_yield_house_totals(self):
        # The house legs need only the two meter columns.
        rows = [_row(datetime(2026, 7, 20, 3, 0, tzinfo=UTC), grid_import_kwh=0.07, batt_charge_kwh=None)]
        day = daily_stats.aggregate_actual_days(rows, 0.0, CEST)[date(2026, 7, 20)]
        assert day["null_ticks"] == 1
        assert day["coverage_ticks"] == 0
        assert day["house_null_ticks"] == 0
        assert day["house_import_kwh"] == pytest.approx(0.07)

    def test_null_meter_columns_increment_house_null_ticks_only(self):
        rows = [_row(datetime(2026, 7, 20, 4, 0, tzinfo=UTC), grid_import_kwh=None, grid_export_kwh=None)]
        day = daily_stats.aggregate_actual_days(rows, 0.0, CEST)[date(2026, 7, 20)]
        assert day["house_null_ticks"] == 1
        assert day["house_import_kwh"] == 0.0
```

- [ ] **Step 2: Verify they fail**

Run: `python3 -m pytest tests/test_daily_stats.py::TestHouseLegsActual -v`
Expected: FAIL with `KeyError: 'house_import_kwh'`

- [ ] **Step 3: Implement**

Add to `_ZERO`:

```python
    "house_import_kwh": 0.0,
    "house_export_kwh": 0.0,
    "house_cost_eur": 0.0,
    "house_revenue_eur": 0.0,
    "house_null_ticks": 0,
```

Add beside `_DELTA_COLUMNS`:

```python
# The two meter columns the WHOLE-HOUSE legs need. Deliberately a smaller set
# than _DELTA_COLUMNS: a row that lost its battery readings can still be
# attributed to the house, and suppressing it would under-report the meter.
_METER_COLUMNS = ("grid_import_kwh", "grid_export_kwh")
```

In `aggregate_actual_days`, replace the body of the row loop after `rec = out.setdefault(...)` with:

```python
        import_price = row.get("import_price")
        export_price = row.get("export_price")
        if any(row.get(col) is None for col in _METER_COLUMNS):
            rec["house_null_ticks"] += 1
        else:
            house_import_kwh = float(row["grid_import_kwh"])
            house_export_kwh = float(row["grid_export_kwh"])
            rec["house_import_kwh"] += house_import_kwh
            rec["house_export_kwh"] += house_export_kwh
            if import_price is not None:
                rec["house_cost_eur"] += house_import_kwh * float(import_price)
            if export_price is not None:
                rec["house_revenue_eur"] += house_export_kwh * (float(export_price) - export_fee_eur_per_kwh)
        if any(row.get(col) is None for col in _DELTA_COLUMNS):
            rec["null_ticks"] += 1
            continue
        rec["coverage_ticks"] += 1
        grid_charge_kwh = min(float(row["grid_import_kwh"]), float(row["batt_charge_kwh"]))
        batt_export_kwh = min(float(row["grid_export_kwh"]), float(row["batt_discharge_kwh"]))
        rec["grid_charge_kwh"] += grid_charge_kwh
        rec["grid_export_kwh"] += batt_export_kwh
        if import_price is not None:
            rec["cost_eur"] += grid_charge_kwh * float(import_price)
        if export_price is not None:
            rec["revenue_eur"] += batt_export_kwh * (float(export_price) - export_fee_eur_per_kwh)
```

Extend the docstring: house legs read the meter columns only, so `null_ticks` (battery) and `house_null_ticks` (meter) move independently.

- [ ] **Step 4: Verify the whole file passes**

Run: `python3 -m pytest tests/test_daily_stats.py -v` → PASS (existing tests must be unchanged)

- [ ] **Step 5: Commit**

```bash
git add custom_components/anker_x1_smartgrid/daily_stats.py tests/test_daily_stats.py
git commit -m "feat(stats): measure whole-house grid import and export per day"
```

---

### Task 3: Emit `self_discharge_kwh` from the plan

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/plan.py:496-503` (the emitted row dict)
- Test: `tests/test_plan.py`

**Interfaces:**
- Produces: horizon row key `self_discharge_kwh: float`. Required by T4's balance.

- [ ] **Step 1: Write the failing test**

```python
def test_row_emits_self_discharge_kwh_matching_its_power():
    # Hour 1 has no sun and 400 W of load, so the battery covers the deficit:
    # self_discharge_w == 400. The published kWh must equal W * dt_h / 1000,
    # the invariant every other energy column on the row already holds.
    cfg = Config(capacity_kwh=10.0, soc_target=100.0, max_charge_w=3000.0, eta_charge=1.0)
    intervals = [
        ForecastInterval(BASE, pv_w=0.0, load_w=400.0, dt_h=1.0),
        ForecastInterval(BASE + timedelta(hours=1), pv_w=0.0, load_w=400.0, dt_h=1.0),
    ]
    out = plan.build_plan_horizon(_slots(2), intervals, [], 50.0, BASE + timedelta(hours=2), cfg)
    assert out[0]["self_discharge_w"] == 400.0
    assert out[0]["self_discharge_kwh"] == pytest.approx(0.4)
    for r in out:
        assert r["self_discharge_kwh"] == pytest.approx(round(r["self_discharge_w"] * 1.0 / 1000.0, 3))
```

`_slots`, `BASE`, `Config`, `ForecastInterval` are already imported at the top of `tests/test_plan.py` — do NOT add new helpers or fixtures.

- [ ] **Step 2: Verify it fails**

Run: `python3 -m pytest tests/test_plan.py -k self_discharge_kwh -v`
Expected: FAIL with `KeyError: 'self_discharge_kwh'`

- [ ] **Step 3: Implement**

In the `out.append({...})` dict, directly after the `"grid_export_kwh"` entry:

```python
                "self_discharge_kwh": round(self_discharge_w * dt_h / 1000.0, 3),
```

- [ ] **Step 4: Verify**

Run: `python3 -m pytest tests/test_plan.py tests/test_entities_plan.py tests/test_sensor_plan.py -v` → PASS

- [ ] **Step 5: Commit**

```bash
git add custom_components/anker_x1_smartgrid/plan.py tests/test_plan.py
git commit -m "feat(plan): publish self-discharge energy alongside its power"
```

---

### Task 4: House legs in `aggregate_planned_days`

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/daily_stats.py` (`aggregate_planned_days`)
- Test: `tests/test_daily_stats.py`

**Interfaces:**
- Consumes: T2's `DayTotals` house keys; T3's `self_discharge_kwh` row key.
- Produces: the same house keys on planned days.

**Balance (spec §Part 1):**
```
net          = load + solar_charge + grid_charge − pv − self_discharge
house_import = max(0, net)
house_export = max(0, −net) + grid_export
```

- [ ] **Step 1: Write the failing tests**

```python
class TestHouseLegsPlanned:
    def _horizon(self, **cols):
        base = {
            "start": datetime(2026, 7, 20, 10, 0, tzinfo=UTC).isoformat(),
            "price": 0.20, "pv_kwh": 0.0, "load_kwh": 0.0,
            "solar_charge_kwh": 0.0, "grid_charge_kwh": 0.0,
            "grid_export_kwh": 0.0, "self_discharge_kwh": 0.0,
        }
        base.update(cols)
        return [base]

    def test_grid_charge_slot_imports_exactly_the_charge(self):
        # pv 0.202 covers load 0.151; surplus 0.051 charges; grid adds 2.148.
        h = self._horizon(pv_kwh=0.202, load_kwh=0.151, solar_charge_kwh=0.051, grid_charge_kwh=2.148)
        day = daily_stats.aggregate_planned_days(h, lambda s, p: 0.10, CEST)[date(2026, 7, 20)]
        assert day["house_import_kwh"] == pytest.approx(2.148)
        assert day["house_export_kwh"] == pytest.approx(0.0)
        assert day["house_cost_eur"] == pytest.approx(2.148 * 0.20)

    def test_export_slot_exports_and_battery_covers_the_house(self):
        h = self._horizon(pv_kwh=0.048, load_kwh=0.182, self_discharge_kwh=0.134, grid_export_kwh=2.955)
        day = daily_stats.aggregate_planned_days(h, lambda s, p: 0.10, CEST)[date(2026, 7, 20)]
        assert day["house_import_kwh"] == pytest.approx(0.0)
        assert day["house_export_kwh"] == pytest.approx(2.955)
        assert day["house_revenue_eur"] == pytest.approx(2.955 * 0.10)

    def test_idle_slot_draws_nothing_from_the_grid(self):
        h = self._horizon(pv_kwh=0.03, load_kwh=0.103, self_discharge_kwh=0.073)
        day = daily_stats.aggregate_planned_days(h, lambda s, p: 0.10, CEST)[date(2026, 7, 20)]
        assert day["house_import_kwh"] == pytest.approx(0.0)
        assert day["house_export_kwh"] == pytest.approx(0.0)

    def test_pv_spill_exports_when_the_battery_cannot_absorb_it(self):
        # Full pack: pv 3.0 over load 0.5, nothing charges -> 2.5 spills to grid.
        h = self._horizon(pv_kwh=3.0, load_kwh=0.5)
        day = daily_stats.aggregate_planned_days(h, lambda s, p: 0.10, CEST)[date(2026, 7, 20)]
        assert day["house_export_kwh"] == pytest.approx(2.5)
        assert day["house_revenue_eur"] == pytest.approx(2.5 * 0.10)
```

- [ ] **Step 2: Verify they fail**

Run: `python3 -m pytest tests/test_daily_stats.py::TestHouseLegsPlanned -v`
Expected: FAIL with `KeyError: 'house_import_kwh'` / zeros

- [ ] **Step 3: Implement**

In `aggregate_planned_days`, after the existing `rec["grid_export_kwh"] += export_kwh`:

```python
        # Whole-house flows follow by balance from the plan's own AC model
        # (plan.build_plan_horizon): PV serves load first, the surplus charges,
        # self-discharge covers any remaining deficit, and grid_export is
        # net-of-house. The max(0, -net) term is PV spill — the leg the
        # battery-basis columns above cannot see.
        net_kwh = (
            float(row.get("load_kwh") or 0.0)
            + float(row.get("solar_charge_kwh") or 0.0)
            + charge_kwh
            - float(row.get("pv_kwh") or 0.0)
            - float(row.get("self_discharge_kwh") or 0.0)
        )
        house_import_kwh = max(0.0, net_kwh)
        house_export_kwh = max(0.0, -net_kwh) + export_kwh
        rec["house_import_kwh"] += house_import_kwh
        rec["house_export_kwh"] += house_export_kwh
        if import_price is not None:
            rec["house_cost_eur"] += house_import_kwh * import_price
```

and inside the existing `if export_price is not None:` block add:

```python
            rec["house_revenue_eur"] += house_export_kwh * float(export_price)
```

Note `charge_kwh` is the delivered-add-back-adjusted value already computed above — reuse it, do not re-read the row.

- [ ] **Step 4: Verify**

Run: `python3 -m pytest tests/test_daily_stats.py -v` → PASS

- [ ] **Step 5: Commit**

```bash
git add custom_components/anker_x1_smartgrid/daily_stats.py tests/test_daily_stats.py
git commit -m "feat(stats): derive planned whole-house grid flows from the plan's AC model"
```

---

### Task 5: House keys through `merge_days`

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/daily_stats.py` (`merge_days`)
- Test: `tests/test_daily_stats.py`

**Interfaces:**
- Produces: merged row keys `house_import_kwh`, `house_export_kwh`, `house_cost_eur`, `house_revenue_eur`, `house_net_eur`, `actual_house_net_eur`, `planned_house_net_eur`. Consumed by T6 and T8.

- [ ] **Step 1: Write the failing test**

```python
def test_merge_carries_house_keys_and_nets_them():
    actual = daily_stats.new_day_totals()
    actual.update({"house_import_kwh": 10.0, "house_export_kwh": 4.0,
                   "house_cost_eur": 3.0, "house_revenue_eur": 1.0})
    planned = daily_stats.new_day_totals()
    planned.update({"house_import_kwh": 2.0, "house_export_kwh": 1.0,
                    "house_cost_eur": 0.5, "house_revenue_eur": 0.25})
    rows = daily_stats.merge_days({}, {}, actual, date(2026, 7, 20))
    row = rows[0]
    assert row["house_import_kwh"] == pytest.approx(10.0)
    assert row["house_net_eur"] == pytest.approx(-2.0)
    assert row["actual_house_net_eur"] == pytest.approx(-2.0)
    assert row["planned_house_net_eur"] is None
```

- [ ] **Step 2: Verify it fails**

Run: `python3 -m pytest tests/test_daily_stats.py -k merge_carries_house -v`
Expected: FAIL with `KeyError: 'house_import_kwh'`

- [ ] **Step 3: Implement**

In `merge_days`, beside the existing `a_cost` / `a_rev` / `p_cost` / `p_rev` locals:

```python
        a_hc = a["house_cost_eur"] if a else 0.0
        a_hr = a["house_revenue_eur"] if a else 0.0
        p_hc = p["house_cost_eur"] if p else 0.0
        p_hr = p["house_revenue_eur"] if p else 0.0
```

and in the appended dict, after `"planned_net_eur"`:

```python
                "house_import_kwh": round(
                    (a["house_import_kwh"] if a else 0.0) + (p["house_import_kwh"] if p else 0.0), 3
                ),
                "house_export_kwh": round(
                    (a["house_export_kwh"] if a else 0.0) + (p["house_export_kwh"] if p else 0.0), 3
                ),
                "house_cost_eur": round(a_hc + p_hc, 3),
                "house_revenue_eur": round(a_hr + p_hr, 3),
                "house_net_eur": round((a_hr - a_hc) + (p_hr - p_hc), 3),
                "actual_house_net_eur": round(a_hr - a_hc, 3) if a is not None else None,
                "planned_house_net_eur": round(p_hr - p_hc, 3) if p is not None else None,
```

- [ ] **Step 4: Verify**

Run: `python3 -m pytest tests/test_daily_stats.py tests/test_daily_stats_sensor.py -v` → PASS

- [ ] **Step 5: Commit**

```bash
git add custom_components/anker_x1_smartgrid/daily_stats.py tests/test_daily_stats.py
git commit -m "feat(stats): carry whole-house totals through the merged day table"
```

---

### Task 6: Live house ledger + controller wiring

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/ledger.py` (`CashLedger` fields, `rollover`, `accumulate`)
- Modify: `custom_components/anker_x1_smartgrid/controller.py:145` (cash-ledger `_PERSIST_GROUPS` entry), controller properties, `_publish_daily_stats` `_today_totals`
- Test: `tests/test_cash_ledger.py`, `tests/test_daily_stats_controller.py`

**Interfaces:**
- Consumes: `optimize.house_energy_kwh` (T1); `DayTotals` house keys (T2).
- Produces: `CashLedger.today_house_import_kwh`, `.today_house_export_kwh`, `.today_house_cost_eur`, `.today_house_revenue_eur`, exposed on `Controller` under the same names.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cash_ledger.py`, using its existing `_ledger_ctrl()` helper (L137) and the `ctrl._accumulate_cash_ledger(now, inputs, slots, slot_minutes, raw_export_price)` entry point the surrounding tests use. `_ledger_ctrl()` zeroes `export_fee_eur_per_kwh`, so `eff == raw`.

```python
class TestLedgerHouseAccumulators:
    def test_house_import_accumulates_at_the_import_price(self):
        ctrl, hass = _ledger_ctrl()
        hass.set_state("sensor.battery_power", "-2000.0")  # charging
        inputs = PlantInputs(soc=50.0, meter_w=1500.0, now=BASE)  # importing 1500 W
        ctrl._accumulate_cash_ledger(BASE, inputs, [PriceSlot(start=BASE, price=0.30)], 60, None)
        # One 60 s tick at 1500 W = 0.025 kWh.
        assert ctrl.today_house_import_kwh == pytest.approx(0.025)
        assert ctrl.today_house_cost_eur == pytest.approx(0.025 * 0.30)

    def test_pv_spill_export_counts_for_the_house_but_not_the_battery(self):
        ctrl, hass = _ledger_ctrl()
        hass.set_state("sensor.battery_power", "0.0")  # battery idle
        inputs = PlantInputs(soc=90.0, meter_w=-1800.0, now=BASE)  # exporting PV
        ctrl._accumulate_cash_ledger(BASE, inputs, [PriceSlot(start=BASE, price=0.30)], 60, 0.20)
        assert ctrl.today_house_export_kwh == pytest.approx(0.03)
        assert ctrl.today_house_revenue_eur == pytest.approx(0.03 * 0.20)
        assert ctrl.today_export_kwh == 0.0  # battery contributed nothing

    def test_house_legs_accumulate_when_the_battery_reading_is_missing(self):
        # batt_w unavailable: the battery legs cannot be attributed, but the
        # meter still measured real grid flow and must not be dropped.
        ctrl, hass = _ledger_ctrl()
        hass.set_state("sensor.battery_power", "unavailable")
        inputs = PlantInputs(soc=50.0, meter_w=1500.0, now=BASE)
        ctrl._accumulate_cash_ledger(BASE, inputs, [PriceSlot(start=BASE, price=0.30)], 60, None)
        assert ctrl.today_house_import_kwh == pytest.approx(0.025)
        assert ctrl.today_grid_charge_kwh == 0.0

    def test_rollover_resets_the_house_accumulators(self):
        from custom_components.anker_x1_smartgrid.ledger import CashLedger

        led = CashLedger()
        led.day = "2026-07-19"
        led.today_house_import_kwh = 5.0
        led.today_house_export_kwh = 2.0
        led.today_house_cost_eur = 1.0
        led.today_house_revenue_eur = 0.5
        led.rollover(datetime(2026, 7, 20, 12, 0, tzinfo=UTC))
        assert led.today_house_import_kwh == 0.0
        assert led.today_house_export_kwh == 0.0
        assert led.today_house_cost_eur == 0.0
        assert led.today_house_revenue_eur == 0.0
```

Add to `tests/test_daily_stats_controller.py` a test that today's merged row carries the ledger's house figures:

```python
def test_today_row_reports_the_live_house_ledger(...):
    controller._ledger.today_house_import_kwh = 7.0
    controller._ledger.today_house_cost_eur = 2.0
    # ... run _publish_daily_stats as the surrounding tests do
    today_row = [r for r in controller.last_status["daily_stats"] if r["source"] != "plan"][-1]
    assert today_row["house_import_kwh"] >= 7.0
```

Also pin the persist migration:

```python
def test_restore_without_house_keys_keeps_the_other_cash_ledger_fields():
    # Stores written before this change have no house keys; a missing key must
    # be skipped, not abort the rest of the cash-ledger group.
    controller.restore({"today_charge_cost_eur": 1.5, "total_net_eur": 9.0})
    assert controller.today_charge_cost_eur == 1.5
    assert controller.total_net_eur == 9.0
    assert controller.today_house_import_kwh == 0.0
```

- [ ] **Step 2: Verify they fail**

Run: `python3 -m pytest tests/test_cash_ledger.py tests/test_daily_stats_controller.py -v`
Expected: FAIL with `AttributeError`/`TypeError` on `today_house_import_kwh`

- [ ] **Step 3: Implement**

`ledger.py` — add after `today_export_kwh`:

```python
    # Whole-house meter legs. Independent of batt_w: these are what the house
    # as a whole drew from / pushed to the grid, so daily_stats can report
    # today's row on the same basis it reports closed days.
    today_house_import_kwh: float = 0.0
    today_house_export_kwh: float = 0.0
    today_house_cost_eur: float = 0.0
    today_house_revenue_eur: float = 0.0
```

Reset all four in `rollover` alongside the existing daily fields.

In `accumulate`, move the house legs **above** the `batt_w is None` early return:

```python
        import_price = resolution.price_at(slots, now, slot_minutes)
        export_price_eff = (
            optimize_mod.effective_export_price(raw_export_price, cfg) if raw_export_price is not None else None
        )
        house_import_kwh, house_export_kwh = optimize_mod.house_energy_kwh(
            inputs.meter_w, const.TICK_SECONDS / 3600.0
        )
        self.today_house_import_kwh += house_import_kwh
        self.today_house_export_kwh += house_export_kwh
        if import_price is not None:
            self.today_house_cost_eur += house_import_kwh * import_price
        if export_price_eff is not None:
            self.today_house_revenue_eur += house_export_kwh * export_price_eff
        batt_w = coordinator.read_float(hass, data.get(const.CONF_ENT_BATTERY_POWER, ""))
        if batt_w is None:
            return
```

Delete the now-duplicated `import_price` / `export_price_eff` lines further down. Update the docstring: the house legs precede the battery guard deliberately.

`controller.py` — add four entries to the cash-ledger group in `_PERSIST_GROUPS`:

```python
        ("today_house_import_kwh", "today_house_import_kwh", lambda v: v, float, False),
        ("today_house_export_kwh", "today_house_export_kwh", lambda v: v, float, False),
        ("today_house_cost_eur", "today_house_cost_eur", lambda v: v, float, False),
        ("today_house_revenue_eur", "today_house_revenue_eur", lambda v: v, float, False),
```

Add the four matching `@property` / setter pairs delegating to `self._ledger`, following the existing `today_grid_charge_kwh` pair exactly.

In `_publish_daily_stats`, extend `_today_totals.update({...})`:

```python
                "house_import_kwh": self._ledger.today_house_import_kwh,
                "house_export_kwh": self._ledger.today_house_export_kwh,
                "house_cost_eur": self._ledger.today_house_cost_eur,
                "house_revenue_eur": self._ledger.today_house_revenue_eur,
```

- [ ] **Step 4: Verify**

Run: `python3 -m pytest tests/test_cash_ledger.py tests/test_cash_ledger_sensors.py tests/test_daily_stats_controller.py tests/test_daily_stats_sensor.py -v` → PASS

- [ ] **Step 5: Commit**

```bash
git add custom_components/anker_x1_smartgrid/ledger.py custom_components/anker_x1_smartgrid/controller.py tests/
git commit -m "feat(ledger): accumulate today's whole-house grid cost and revenue"
```

---

### Task 7: `past_actuals` uses the ledger's attribution (Part 2)

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/past_actuals.py:100-155`
- Test: `tests/test_past_actuals.py`

**Interfaces:**
- Produces: unchanged key set; `grid_charge_*` / `grid_export_*` / `solar_charge_*` change meaning to the ledger's `min()` attribution.

**Rule:** the `min()` does not commute with a column-wide sum, so `_kwh_sum` cannot be reused for these two — compute per tick, then sum. Same in the power domain for the mean-W fallback path.

- [ ] **Step 1: Write the failing tests**

```python
def test_grid_charge_is_the_measured_min_not_the_pv_surplus_split():
    # PV 2000, load 200 -> old split said 0 grid. The meter only ever showed
    # 50 W of import, so at most 50 W of the 1000 W charge came from the grid.
    rows = [{"ts": _ts(10), "pv_w": 2000.0, "load_w": 200.0, "batt_w": -1000.0, "p1_w": 50.0, "soc": 40.0}]
    rec = aggregate_past_actuals(rows)[datetime(2026, 6, 29, 10, tzinfo=UTC)]
    assert rec["grid_charge_w"] == 50.0
    assert rec["solar_charge_w"] == 950.0


def test_pv_spill_export_is_not_battery_export():
    # Exporting 800 W with the battery idle: PV spill, not battery discharge.
    rows = [{"ts": _ts(12), "pv_w": 1500.0, "load_w": 700.0, "batt_w": 0.0, "p1_w": -800.0, "soc": 90.0}]
    rec = aggregate_past_actuals(rows)[datetime(2026, 6, 29, 12, tzinfo=UTC)]
    assert rec["grid_export_w"] == 0.0


def test_split_still_sums_to_total_battery_charge():
    rows = [{"ts": _ts(9), "pv_w": 500.0, "load_w": 200.0, "batt_w": -1000.0, "p1_w": 700.0, "soc": 20.0}]
    rec = aggregate_past_actuals(rows)[datetime(2026, 6, 29, 9, tzinfo=UTC)]
    assert rec["solar_charge_w"] + rec["grid_charge_w"] == pytest.approx(1000.0)
```

Update the two existing tests that pin the old split:
`test_solar_first_split_when_pv_surplus_covers_charge` now expects `grid_charge_w == 50.0` / `solar_charge_w == 950.0`.
`test_charge_exceeding_surplus_spills_to_grid` is unchanged (`min(700, 1000) == 700`).

- [ ] **Step 2: Verify**

Run: `python3 -m pytest tests/test_past_actuals.py -v`
Expected: the three new tests FAIL

- [ ] **Step 3: Implement**

Replace the power-domain split (currently `solar_surplus` / `solar_charge_w` / `grid_charge_w`) with a per-tick `min()` mean:

```python
    # Attribution matches optimize.cash_energy_kwh (the live ledger) and the
    # DP's own grid_charge/grid_export, so past and planned bars are the same
    # quantity. The min() is per TICK — it does not commute with a bucket-wide
    # mean, and a PV-surplus split would additionally lean on the COMPUTED
    # load_w rather than on what the meter measured. PV spill therefore stays
    # out of the battery export bar; it is reported by the house columns.
    _gc = [
        min(max(0.0, r["p1_w"]), max(0.0, -r["batt_w"]))
        for r in group
        if r.get("p1_w") is not None and r.get("batt_w") is not None
    ]
    _ge = [
        min(max(0.0, -r["p1_w"]), max(0.0, r["batt_w"]))
        for r in group
        if r.get("p1_w") is not None and r.get("batt_w") is not None
    ]
    grid_charge_w = (sum(_gc) / len(_gc)) if _gc else 0.0
    grid_export_w = (sum(_ge) / len(_ge)) if _ge else 0.0
    solar_charge_w = max(0.0, charge_w - grid_charge_w)
```

Delete `solar_surplus` and the old `grid_export_w = _mean(export_vals) or 0.0`.

Replace the energy-domain split:

```python
    _gc_kwh = [
        min(float(r["grid_import_kwh"]), float(r["batt_charge_kwh"]))
        for r in group
        if r.get("grid_import_kwh") is not None and r.get("batt_charge_kwh") is not None
    ]
    _ge_kwh = [
        min(float(r["grid_export_kwh"]), float(r["batt_discharge_kwh"]))
        for r in group
        if r.get("grid_export_kwh") is not None and r.get("batt_discharge_kwh") is not None
    ]
    grid_charge_kwh = sum(_gc_kwh) if _gc_kwh else grid_charge_w / 1000.0 * slot_h * coverage
    export_kwh = sum(_ge_kwh) if _ge_kwh else grid_export_w / 1000.0 * slot_h * coverage
    solar_charge_kwh = max(0.0, charge_kwh - grid_charge_kwh)
```

Delete the old `surplus_kwh` / `solar_charge_kwh` / `grid_charge_kwh` lines. Update the module docstring (it currently describes the PV-surplus split).

- [ ] **Step 4: Verify**

Run: `python3 -m pytest tests/test_past_actuals.py tests/test_plan_past_actuals.py tests/test_controller_past_actuals.py -v` → PASS

- [ ] **Step 5: Commit**

```bash
git add custom_components/anker_x1_smartgrid/past_actuals.py tests/test_past_actuals.py
git commit -m "fix(stats): attribute measured past slots the way the ledger does"
```

---

### Task 8: Card columns + README

**Files:**
- Modify: `lovelace/daily-stats-card.yaml`
- Modify: `README.md:111-120` (daily stats sensor attributes)
- Create: `tests/test_lovelace_daily_stats_card.py` — **no existing test loads `daily-stats-card.yaml`**; this module is new. Follow the loader pattern of `tests/test_lovelace_plan_card.py:19-25`.

**Interfaces:**
- Consumes: T5's merged row keys.

- [ ] **Step 1: Write the failing test**

```python
"""Structural regression tests for the Lovelace daily-stats card.

The card's logic lives in a Jinja template inside YAML, so there is no runtime
to assert against here — these tests pin the column contract against the keys
daily_stats.merge_days emits.

Spec: docs/superpowers/specs/2026-08-24-house-wide-energy-accounting-design.md
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import yaml

from custom_components.anker_x1_smartgrid import daily_stats

_ROOT = Path(__file__).resolve().parents[1]
CARD_PATH = _ROOT / "lovelace" / "daily-stats-card.yaml"


def _content() -> str:
    return yaml.safe_load(CARD_PATH.read_text(encoding="utf-8"))["content"]


def test_card_renders_the_battery_basis_columns():
    content = _content()
    for key in ("grid_charge_kwh", "grid_export_kwh", "net_eur"):
        assert key in content


def test_card_renders_the_house_columns():
    content = _content()
    for key in ("house_import_kwh", "house_export_kwh", "house_net_eur"):
        assert key in content


def test_every_referenced_key_is_one_merge_days_emits():
    # Guards against a typo'd d.<key> silently rendering blank in Lovelace.
    emitted = set(
        daily_stats.merge_days({}, {}, daily_stats.new_day_totals(), date(2026, 7, 20))[0]
    )
    for key in set(re.findall(r"\bd\.([a-z_]+)", _content())):
        assert key in emitted, f"card references d.{key}, which merge_days does not emit"
```

- [ ] **Step 2: Verify it fails**

Run: `python3 -m pytest tests/test_lovelace_daily_stats_card.py -v`
Expected: `test_card_renders_the_house_columns` FAILS

- [ ] **Step 3: Implement**

Replace the table header/row in `daily-stats-card.yaml`:

```
  | Day | Chg | Exp | Batt € | Imp | Exp | House € |

  |---|--:|--:|--:|--:|--:|--:|

  {% for d in days -%}
  | {{ d.date[5:] }}{% if d.source != 'actual' %} *{% endif %} |
  {{ '%.1f' | format(d.grid_charge_kwh) }} |
  {{ '%.1f' | format(d.grid_export_kwh) }} |
  {{ '%+.2f' | format(d.net_eur) }} |
  {{ '%.1f' | format(d.house_import_kwh) }} |
  {{ '%.1f' | format(d.house_export_kwh) }} |
  {{ '%+.2f' | format(d.house_net_eur) }} |

  {% endfor %}
```

Update the card's header comment: Chg/Exp/Batt € are the battery cash basis; Imp/Exp/House € are the whole house at the meter, both net of the feed-in fee.

Update the README bullet to list the new keys.

- [ ] **Step 4: Verify the full suite**

```bash
source .venv/bin/activate && python3 -m pytest -q && ruff check custom_components tests
```
Expected: all PASS, ruff clean.

- [ ] **Step 5: Commit**

```bash
git add lovelace/daily-stats-card.yaml README.md tests/
git commit -m "feat(card): show whole-house import, export and net € per day"
```

---

## Post-implementation

- [ ] `graphify update .` (project CLAUDE.md).
- [ ] Deploy to lab, then reconcile: the table's House € for a closed day should land within a few % of `sensor.zonneplan_electricity_delivery_costs_today` / `..._production_costs_today` sampled just before local midnight, the residual being `export_fee_eur_per_kwh × exported kWh`.
- [ ] Card must be re-pasted into Lovelace by hand (the markdown card is not currently on the lab dashboard at all — only the apexcharts card is).

## Unresolved questions

1. **Card not deployed.** `daily-stats-card.yaml` is in the repo but is NOT on the lab dashboard — the only smartgrid card there is the apexcharts one. Do you want me to add the table card to the Power view, or are you reading these numbers somewhere else?
2. **`house_null_ticks` surfacing.** It is aggregated but nothing displays it. Leave it as a diagnostic attribute, or mark a gappy day on the card?
3. **Battery columns on the card.** With House € added, the row gets wide for mobile. Keep all six columns, or drop `Chg`/`Exp` kWh and keep the two € figures plus the house kWh?
