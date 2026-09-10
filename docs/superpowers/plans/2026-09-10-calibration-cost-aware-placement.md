# Calibration Cost-Aware Placement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use `- [ ]`.

**Goal:** calibration windows priced against the DP's own plan (lost export, battery-served load moved to grid, top-up, retained-energy credit); 95% start gate; 30-min hold; € caps.

**Architecture:** pure cost model + policy in `calibration.py` over `CalibSlot`s built from the plan horizon; controller threads `prev` plan + DP water value. DP core/oracle untouched.

**Tech Stack:** Python 3.12, HA custom integration `custom_components/anker_x1_smartgrid`, pytest.

**Spec:** `docs/superpowers/specs/2026-09-10-calibration-cost-aware-placement-design.md` (read it first).

## Global Constraints

- Work in worktree `../x1-smartcharge-calib-cost`, branch `feat/calibration-cost-aware` off `main`. Fast-forward only, no merge commits.
- `calibration.py`, `daily_stats.py` stay pure (no HA imports, no I/O, no clock reads).
- `optimize.py`, `regret.py` NOT touched (parity gate).
- Consts (exact): `CALIBRATION_MIN_START_SOC = 95.0`, `CALIBRATION_COST_ALLOWANCE_EUR = 0.50`, `CALIBRATION_OVERDUE_COST_CAP_EUR = 1.00`, `CALIBRATION_MAX_DWELL_H = 0.5`, `DEFAULT_CALIBRATION_DWELL_H = 0.5`. Removed: `CALIBRATION_MIN_PLAN_SOC`, `CALIBRATION_FREE_TOPUP_KWH`.
- Comments only where code can't explain itself; no ticket IDs / change history in code.
- Tests: `.venv/bin/python -m pytest <path> -q`. Lint = pre-commit (ruff check+format, pyright): `pre-commit run --files <changed files>`.
- Commits: `committing` skill (Angular), one per task, end message with `Claude-Session: https://claude.ai/code/session_01Tqt1jSdLt3ZEzNtthC4s9m`.
- Graphify rule (project CLAUDE.md): orient with `graphify query "<q>"` before reading raw source.

## Dependency order

T1, T2, T3 independent → T4 (needs T1) → T5 (needs T3, T4) → T6 (needs T2, T5) → T7 gate → T8 deploy.

---

### Task 1: Extract `daily_stats.planned_house_flows`

**Files:** Modify `custom_components/anker_x1_smartgrid/daily_stats.py:195-210`; Test `tests/test_daily_stats.py`

**Produces:** `planned_house_flows(row: dict, charge_kwh: float | None = None) -> tuple[float, float]` → `(house_import_kwh, house_export_kwh)`.

- [ ] Failing test:

```python
from custom_components.anker_x1_smartgrid.daily_stats import planned_house_flows

def test_planned_house_flows_balance():
    row = {"load_kwh": 0.5, "solar_charge_kwh": 1.0, "grid_charge_kwh": 0.2,
           "pv_kwh": 2.0, "self_discharge_kwh": 0.0, "grid_export_kwh": 0.3}
    assert planned_house_flows(row) == pytest.approx((0.0, 0.6))
    # charge override (caller's delivered add-back reversal) replaces grid_charge_kwh
    assert planned_house_flows(row, charge_kwh=1.0) == pytest.approx((0.5, 0.3))
```

- [ ] Implement (move the inline balance verbatim, keep its comment on the helper):

```python
def planned_house_flows(row: dict, charge_kwh: float | None = None) -> tuple[float, float]:
    """(house_import_kwh, house_export_kwh) for one forward plan row.

    Whole-house flows follow by balance from the plan's own AC model
    (plan.build_plan_horizon): PV serves load first, the surplus charges,
    self-discharge covers any remaining deficit, and grid_export is
    net-of-house. The max(0, -net) term is PV spill. ``charge_kwh`` overrides
    the row's grid_charge_kwh.
    """
    if charge_kwh is None:
        charge_kwh = float(row.get("grid_charge_kwh") or 0.0)
    net_kwh = (
        float(row.get("load_kwh") or 0.0)
        + float(row.get("solar_charge_kwh") or 0.0)
        + charge_kwh
        - float(row.get("pv_kwh") or 0.0)
        - float(row.get("self_discharge_kwh") or 0.0)
    )
    return max(0.0, net_kwh), max(0.0, -net_kwh) + float(row.get("grid_export_kwh") or 0.0)
```

`aggregate_planned_days` then uses `house_import_kwh, house_export_kwh = planned_house_flows(row, charge_kwh)`.
- [ ] `.venv/bin/python -m pytest tests/test_daily_stats.py tests/test_daily_stats_controller.py -q` → all pass (existing = regression).
- [ ] Commit `refactor(stats): extract the planned whole-house balance per row`.

### Task 2: Expose DP water value

**Files:** Modify `custom_components/anker_x1_smartgrid/decision.py:1198-1207`; Test: the existing test file that calls `compute_decision(..., _out=...)` on the DP path (find with `rg -n "_out=" tests/test_decision_*.py tests/test_controller_water_value.py`).

**Produces:** `_dp_out["water_value"]: float | None` (€/DC-kWh, the value already computed at `decision.py:935`).

- [ ] Failing test: reuse that file's DP-path setup; assert `out["water_value"] == pytest.approx(optimize.compute_water_value(min(<future slot prices>), cfg))`.
- [ ] Implement: add `_out["water_value"] = water_value` in the `if _out is not None:` block beside `terminal_v_hi`.
- [ ] Run that test file + `tests/test_controller_water_value.py` → pass.
- [ ] Commit `feat(decision): publish the DP water value alongside its terminal artefacts`.

### Task 3: 30-min hold + new consts

**Files:** `const.py:135-163`, `models.py:120-123`, `config_flow.py:416-420`, `strings.json:104`, `translations/en.json:104`; Tests `tests/test_calibration_config.py`, `tests/test_config_flow.py:1418-1434`

- [ ] Failing tests:
  - `test_calibration_config.py::test_defaults_ship_on`: `cfg.calibration_dwell_h == 0.5`.
  - new: `Config.from_dict({"calibration_dwell_h": 2.0}).calibration_dwell_h == 0.5`; `from_dict({"calibration_dwell_h": 0.25})` → `0.25`.
  - new: `const.CALIBRATION_MIN_START_SOC == 95.0`, `CALIBRATION_COST_ALLOWANCE_EUR == 0.50`, `CALIBRATION_OVERDUE_COST_CAP_EUR == 1.00`, `CALIBRATION_MAX_DWELL_H == 0.5`.
  - `test_config_flow.py`: dwell `0.51` → `vol.Invalid`; `0.5` ok; `0.24` still invalid.
- [ ] Implement:
  - `const.py`: `DEFAULT_CALIBRATION_DWELL_H = 0.5`; add the four consts with one-line rationale comments (start gate: top-up ≤ 5% of pack; allowance/cap: € bars on `window_cost`; max dwell: export-blocking bound). Do NOT remove `CALIBRATION_MIN_PLAN_SOC` / `CALIBRATION_FREE_TOPUP_KWH` yet (T5).
  - `models.py` `from_dict`: build cfg, then `if cfg.calibration_dwell_h > const.CALIBRATION_MAX_DWELL_H: cfg = dataclasses.replace(cfg, calibration_dwell_h=const.CALIBRATION_MAX_DWELL_H)` (UI-saved 1.0/2.0 on live boxes; `import dataclasses` if absent).
  - `config_flow.py`: `vol.Range(min=0.25, max=const.CALIBRATION_MAX_DWELL_H)`.
  - strings/en.json dwell description: `"Hours to hold at the calibration top SoC before releasing (0.25–0.5). Default 0.5."`
- [ ] `.venv/bin/python -m pytest tests/test_calibration_config.py tests/test_config_flow.py -q` → pass.
- [ ] Commit `feat(calibration): cap the top-of-pack hold at 30 minutes`.

### Task 4: Cost model (`CalibSlot`, `build_calib_slots`, `window_cost`)

**Files:** Modify `custom_components/anker_x1_smartgrid/calibration.py` (add only; old `select_window`/`_soc_at` stay until T5); Test `tests/test_calibration_cost.py` (new)

**Consumes:** `daily_stats.planned_house_flows` (T1).
**Produces:**

```python
@dataclass(frozen=True)
class CalibSlot:
    """One future plan slot as the cost model sees it. Energies cover [t0, t1]:
    t0 is ``now`` for the in-progress slot (the plan models only its remaining
    minutes), the slot start otherwise. SoC runs soc_start -> soc_end over it."""
    t0: datetime
    t1: datetime
    import_price: float
    export_price: float | None  # post-fee; None zeroes the revenue leg
    pv_kwh: float
    load_kwh: float
    soc_start: float
    soc_end: float
    plan_import_kwh: float
    plan_export_kwh: float

def build_calib_slots(horizon: list[dict], now: datetime, live_soc: float, slot_minutes: int,
                      export_price_at: Callable[[datetime, float | None], float | None],
                      delivered_at: Callable[[datetime], float] | None = None) -> list[CalibSlot]: ...
def _plan_soc_at(cal_slots: list[CalibSlot], when: datetime) -> float | None: ...
def window_cost(cal_slots: list[CalibSlot], start: datetime, end: datetime, *,
                cfg: Config, water_value: float | None) -> float | None: ...
```

- [ ] Failing tests (`tests/test_calibration_cost.py`). Config: `CFG = Config(capacity_kwh=20.0, max_charge_w=12000.0, eta_charge=0.92, calibration_top_soc=100.0, calibration_dwell_h=0.5)`, `BASE = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)`, helper `_slot(t0, minutes=60, **kw)` building a `CalibSlot` (defaults: prices 0.20/0.20, pv 0, load 0, soc 100→100, plan flows 0).
  1. pre-peak hold: slot price 0.40/0.40, load 0.5, soc 100→67.5, plan_import 0, plan_export 6.0; `window_cost([s], BASE, BASE+30min, wv=0.21)` == approx `0.6175` (cal 0.10 − plan −1.20 = 1.30; credit 0.21×3.25 = 0.6825).
  2. midday spill: price 0.10/0.10, pv 3.5, load 0.5, soc 100→100, plan_export 3.0 → `0.0`.
  3. top-up credit: soc 95→95, price 0.20/0.20; window `BASE → BASE + (1.0/(12×0.92) + 0.5) h` → approx `0.007391` with wv 0.21; approx `0.217391` with `water_value=None`.
  4. two 15-min slots, prices 0.10 / 0.30, load 0.25 each, plan_import 0, soc 100→98.75→97.5; window BASE→BASE+30min, wv 0.21 → approx `-0.005`.
  5. export_price None: the case-1 slot with `export_price=None`, wv None → `0.10` (cal 0.25×0.40; plan export leg 0; no credit).
  6. coverage: window past last slot → `None`; 15-min gap between two slots inside the window → `None`.
  7. `build_calib_slots`: horizon rows (60-min, `now = BASE+15min`): one `mode="actual"` row at BASE−1h, in-progress row at BASE (pv_kwh 2.0, load_kwh 1.0, grid_charge_kwh 0.9, soc 80), future row BASE+1h (soc 90), `estimated=True` row BASE+2h. `delivered_at` returns 0.3 for BASE. Expect 2 slots; slot0 `t0 == now`, `t1 == BASE+1h`, `pv_kwh == 1.5`, `load_kwh == 0.75`, `soc_start == live_soc`, plan flows == `planned_house_flows` of the scaled row with charge 0.6; slot1 `soc_start == 80`, `soc_end == 90`; `export_price == export_price_at(start, price)`.
  8. `_plan_soc_at`: linear interpolation inside a slot; `None` outside all slots.
- [ ] Run → FAIL (names undefined).
- [ ] Implement:

```python
def build_calib_slots(horizon, now, live_soc, slot_minutes, export_price_at, delivered_at=None):
    dur = timedelta(minutes=slot_minutes)
    out: list[CalibSlot] = []
    prev_soc = live_soc
    for row in horizon:
        if row.get("estimated") or row.get("mode") == "actual" or row.get("soc") is None or row.get("price") is None:
            continue
        start = datetime.fromisoformat(row["start"])
        end = start + dur
        if end <= now:
            continue
        t0 = max(start, now)
        # build_plan_horizon scales only the flow columns to the remaining
        # minutes; pv_kwh/load_kwh stay full-slot on the in-progress row.
        frac = (end - t0) / dur
        pv = float(row.get("pv_kwh") or 0.0) * frac
        load = float(row.get("load_kwh") or 0.0) * frac
        charge = float(row.get("grid_charge_kwh") or 0.0)
        if start <= now and delivered_at is not None:
            charge = max(0.0, charge - float(delivered_at(start) or 0.0))
        imp, exp = daily_stats.planned_house_flows({**row, "pv_kwh": pv, "load_kwh": load}, charge)
        price = float(row["price"])
        soc_end = float(row["soc"])
        out.append(CalibSlot(t0, end, price, export_price_at(start, price), pv, load, prev_soc, soc_end, imp, exp))
        prev_soc = soc_end
    return out


def _plan_soc_at(cal_slots, when):
    for s in cal_slots:
        if s.t0 <= when <= s.t1:
            span = (s.t1 - s.t0).total_seconds()
            f = (when - s.t0).total_seconds() / span if span > 0 else 0.0
            return s.soc_start + (s.soc_end - s.soc_start) * f
    return None


def window_cost(cal_slots, start, end, *, cfg, water_value):
    """EUR a calibration over [start, end] costs relative to the plan, or None
    when the plan does not cover the window contiguously.

    Calibration charges at max rate to the top (solar first), then holds: the
    battery never discharges, so load minus PV meets the grid. The energy it
    leaves in the pack above the plan is credited at the DP's water value.
    """
    covering = [s for s in cal_slots if s.t1 > start and s.t0 < end]
    if not covering or covering[0].t0 > start or covering[-1].t1 < end:
        return None
    for a, b in zip(covering, covering[1:], strict=False):
        if abs(b.t0 - a.t1) > _CONTIGUITY_TOLERANCE:
            return None
    soc0, soc1 = _plan_soc_at(cal_slots, start), _plan_soc_at(cal_slots, end)
    if soc0 is None or soc1 is None:
        return None
    e, e_top = cfg.pct_to_kwh(soc0), cfg.pct_to_kwh(cfg.calibration_top_soc)
    eta = cfg.eta_charge_safe()
    delta = 0.0
    for s in covering:
        m_h = (min(s.t1, end) - max(s.t0, start)).total_seconds() / 3600.0
        span_h = (s.t1 - s.t0).total_seconds() / 3600.0
        f = m_h / span_h if span_h > 0 else 0.0
        charge_ac = 0.0
        if e < e_top:
            charge_ac = min(cfg.max_charge_w / 1000.0 * m_h, (e_top - e) / eta)
            e += charge_ac * eta
        net = (s.load_kwh - s.pv_kwh) * f + charge_ac
        exp_p = s.export_price or 0.0
        cal_eur = max(0.0, net) * s.import_price - max(0.0, -net) * exp_p
        plan_eur = (s.plan_import_kwh * s.import_price - s.plan_export_kwh * exp_p) * f
        delta += cal_eur - plan_eur
    return delta - (water_value or 0.0) * max(0.0, e - cfg.pct_to_kwh(soc1))
```

(`from collections.abc import Callable`; `from . import daily_stats`.)
- [ ] Run new file + `tests/test_calibration.py` → pass.
- [ ] Commit `feat(calibration): price a window against the DP's own plan`.

### Task 5: Policy — start gate, committed cycles, cost acceptance (opus)

**Files:** Modify `custom_components/anker_x1_smartgrid/calibration.py:262-556`, `const.py` (remove `CALIBRATION_MIN_PLAN_SOC`, `CALIBRATION_FREE_TOPUP_KWH`); Test `tests/test_calibration.py:109-706` (rewrite), `tests/test_calibration_cost.py` (extend)

**Consumes:** T3 consts, T4 API.
**Produces (T6 relies on these exact signatures):**

```python
@dataclass(frozen=True)
class CalibPlan:
    phase: str
    window_start: datetime | None
    window_end: datetime | None
    cost_eur: float | None = None  # cheapest candidate evaluated this tick (accepted or not); carried while committed

@dataclass(frozen=True)
class WindowPick:
    start: datetime
    end: datetime
    cost_eur: float
    accepted: bool

def select_window(now, cal_slots: list[CalibSlot], *, cfg, bar: float | None, force: bool,
                  water_value: float | None) -> WindowPick | None
def calibration_plan(now, soc_pct, cal_slots: list[CalibSlot], soc_samples, price_history, cfg, *,
                     prev: CalibPlan | None = None, water_value: float | None = None) -> CalibPlan
def calibration_action(...same args...) -> CalibAction | None   # = calibration_plan(...).action
def plan_peak_soc(soc_pct: float, cal_slots: list[CalibSlot]) -> float   # max(soc_pct, max soc_end)
```

Removed: `_soc_at`, `_slot_duration_min`, `already_holding`/`soc_forecast` kwargs, the plan-peak gate step.

- [ ] Implement:

```python
def _acceptable(cost: float, need_kwh: float, *, bar: float | None, force: bool) -> bool:
    if force:
        return cost <= const.CALIBRATION_OVERDUE_COST_CAP_EUR
    return cost <= need_kwh * (bar or 0.0) + const.CALIBRATION_COST_ALLOWANCE_EUR


def select_window(now, cal_slots, *, cfg, bar, force, water_value):
    cands: list[tuple[float, float, datetime, datetime]] = []
    for s in cal_slots:
        if s.t1 <= now or s.soc_start < const.CALIBRATION_MIN_START_SOC:
            continue
        end = s.t0 + timedelta(hours=_charge_h(s.soc_start, cfg) + cfg.calibration_dwell_h)
        cost = window_cost(cal_slots, s.t0, end, cfg=cfg, water_value=water_value)
        if cost is not None:
            cands.append((cost, _charge_kwh(s.soc_start, cfg), s.t0, end))
    if not cands:
        return None
    # Filter BEFORE taking each day's cheapest: the normal bar scales with
    # need_kwh, so a day's cheapest candidate can fail while a dearer one passes.
    per_day: dict[object, tuple[float, float, datetime, datetime]] = {}
    for c in cands:
        if _acceptable(c[0], c[1], bar=bar, force=force):
            key = c[2].date()  # UTC date; see the 2026-08-03 spec
            if key not in per_day or c[0] < per_day[key][0]:
                per_day[key] = c
    if per_day:
        cost, _need, start, end = per_day[min(per_day)]  # earliest acceptable day wins
        return WindowPick(start, end, cost, True)
    cost, _need, start, end = min(cands, key=lambda c: (c[0], c[2]))
    return WindowPick(start, end, cost, False)
```

`calibration_plan` body after the unchanged enabled / `last_success_end` / `days_since` / not-due checks:

```python
    cont_soc = continue_soc(cfg)
    dwell = timedelta(hours=cfg.calibration_dwell_h)

    def _hold(cost):
        run_start = _open_run_start(soc_samples, target_soc=cfg.calibration_top_soc, continue_soc=cont_soc) or now
        return CalibPlan("holding", run_start, run_start + dwell, cost)

    # A running cycle is never re-costed or re-gated: abandoning it mid-way
    # buys the charge without the balancing it was for.
    if prev is not None and prev.phase == "holding" and soc_pct >= cont_soc:
        return _hold(prev.cost_eur)
    if prev is not None and prev.phase == "charging" and prev.window_end is not None and now < prev.window_end:
        if soc_pct >= cfg.calibration_top_soc:
            return _hold(prev.cost_eur)
        return CalibPlan("charging", prev.window_start, prev.window_end, prev.cost_eur)

    force = days_since >= cfg.calibration_interval_days + const.CALIBRATION_GRACE_DAYS
    bar = price_percentile(price_history, const.CALIBRATION_PRICE_PERCENTILE)

    # A self top-out (solar, or the DP's own charge) is held only if the hold
    # itself is cheap -- the DP often fills the pack precisely to export it.
    if soc_pct >= cfg.calibration_top_soc:
        hold_cost = window_cost(cal_slots, now, now + dwell, cfg=cfg, water_value=water_value)
        if hold_cost is not None and _acceptable(hold_cost, 0.0, bar=bar, force=force):
            return _hold(hold_cost)

    pick = select_window(now, cal_slots, cfg=cfg, bar=bar, force=force, water_value=water_value)
    if pick is None:
        return _IDLE
    if not pick.accepted:
        return CalibPlan("idle", None, None, pick.cost_eur)
    if pick.start <= now < pick.end and soc_pct >= const.CALIBRATION_MIN_START_SOC:
        return CalibPlan("charging", pick.start, pick.end, pick.cost_eur)
    return CalibPlan("scheduled", pick.start, pick.end, pick.cost_eur)
```

Update module/func docstrings to the new rules (drop plan-peak-gate and free-top-up prose). Remove the two dead consts from `const.py`.

- [ ] Rewrite `tests/test_calibration.py` from line 109. Helper `_cal(start, soc_start_end_pairs_or_socs, *, minutes=60, prices=0.20, pv=0.0, load=0.0, plan_export=0.0, export_price=<same as price>)` → contiguous `CalibSlot`s (chain soc_start from previous soc_end). Configs `CFG`/`ON` get `calibration_top_soc=100.0, calibration_dwell_h=0.5`.

| Existing test | Action |
|---|---|
| lines 1-107, `price_percentile`, `continue_soc`, `last_success_end` dwell tests, `_charge_h` clamp, `charge_need_is_measured_to_the_target`, disabled, not-due, fresh-install idle, empty soc history, idle-plan-no-window, scheduled-never-acts | keep (only adapt call signature) |
| `selects_cheapest_window_and_requires_the_bar`, `force_ignores_the_bar`, `no_bar_means_only_force_can_fire`, `does_not_skip_today_for_a_cheaper_tomorrow`, `cheapest_window_within_the_day_wins` | adapt to `WindowPick` + cost acceptance (monkeypatch `calibration.window_cost` with a `{start: cost}` lookup to pin costs) |
| `mixed_durations…`, `gap_in_slot_series…`, `duration_min_none…`, `insufficient_total_slots…` | replace with coverage tests on `select_window` (window not covered ⇒ no candidate) |
| `window_is_sized_from_projected_soc…` | adapt: soc_start 96 ⇒ `end - start == charge_h(96)+0.5h` |
| `ranks_windows_by_total_grid_cost…`, `cheap_block_still_wins…`, `absent_forecast…`, `forecast_before_its_first_row…`, `near_full_window_bypasses_the_price_bar` | delete (behaviour removed; superseded below) |
| `at_top_soc_reports_holding`, `holding_reports_the_open_runs_actual_start`, `holding_falls_back_to_now…` | adapt: neutral plan (cost 0) so the fresh hold is accepted |
| `already_holding_*`, `holds_through_even_without_a_cheap_window`, `gate_cannot_cut_a_dwell…` | adapt to `prev=CalibPlan("holding", …)` |
| `due_and_cheap_returns_charging`, `bar_alone_accepts…`, `due_at_exact_interval_boundary`, `force_at_exact_grace_boundary`, `empty_price_history…`, `future_window_*`, `plan_and_action_agree…` | adapt: live soc & plan soc_start ≥ 95 |
| plan-peak gate block (lines 614-705) | replace with start-gate tests below; `plan_peak_soc` test adapted to `CalibSlot`s |

New tests (all in `tests/test_calibration.py`; due = `_stale_history(now, 6)`, overdue = `_stale_history(now, 30)`):
  1. start gate: slot soc_start 94.9 ⇒ `select_window` → `None`; 95.0 ⇒ pick.
  2. prefers cheap hold over pre-peak: slot0 = T4 case-1 slot (peak export, soc 100→67.5), slot1 = T4 case-2 slot with EXPLICIT soc 100→100 (not chained, else the start gate drops it), wv 0.21 ⇒ pick.start == slot1.t0 for `force=False` AND `force=True` (peak first so "earliest wins" cannot explain the result).
  3. filter-before-cheapest: costs {A: 0.55 with soc_start 100, B: 0.60 with soc_start 95}, bar 0.30 ⇒ B accepted (0.60 ≤ 1.0×0.30+0.50).
  4. overdue cap: cost 0.99 ⇒ accepted; 1.01 ⇒ `accepted is False`, plan phase `idle`, `cost_eur == 1.01`.
  5. live-SoC guard: plan soc_start 96 at `now`, `soc_pct=94`, cost 0 ⇒ `scheduled`, `action is None`.
  6. committed charging survives cost spike: `prev=CalibPlan("charging", now-5min, now+30min, 0.1)`, soc 97, `window_cost`→99 ⇒ `charging`, same window, `cost_eur == 0.1`.
  7. committed charging reaching top ⇒ `holding` despite `window_cost`→99.
  8. committed hold survives planned export: `prev` holding, soc 99.5, `window_cost`→99 ⇒ `holding`.
  9. committed charging past `window_end` ⇒ re-evaluated (not `charging` when cost 99).
  10. fresh top-out: soc 100, T4 case-1 plan, due ⇒ phase != `holding`, `cost_eur ≈ 0.6175`; T4 case-2 plan ⇒ `holding`, `cost_eur == 0.0`; case-1 plan + overdue ⇒ `holding` (0.6175 ≤ 1.00).
  11. no plan: soc 100, due, `cal_slots=[]` ⇒ `idle`; same with `prev` holding ⇒ `holding`.
- [ ] `.venv/bin/python -m pytest tests/test_calibration.py tests/test_calibration_cost.py tests/test_calibration_config.py -q` → pass. (`test_calibration_controller.py` breaks until T6 — expected.)
- [ ] Commit `feat(calibration): gate, cost and commit calibration cycles against the plan`.

### Task 6: Controller + sensor wiring

**Files:** Modify `controller.py:1768-1893` (block), `:2066-2086` (status), `:2117-2151` (`_calibration_compute_sync`), `:2385-2474` (`_publish_daily_stats`); `sensor.py:332-338`; Test `tests/test_calibration_controller.py`

**Consumes:** T2 `_dp_out["water_value"]`, T5 API.

- [ ] Implement:
  - Extract from `_publish_daily_stats` (move its export-valuation docstring with it):
    - `def _export_price_fn(self, now, horizon, export_price, export_slots, slot_minutes, export_matches_import) -> Callable[[datetime, float | None], float | None]` (body = current `_curve/_flat/_static/_cur_import` + nested `_export_price_at`, returned).
    - `@staticmethod def _delivered_fn(delivered_by_hour, slot_minutes) -> Callable[[datetime], float]` (current `_delivered_at`).
    - `_publish_daily_stats` calls both; behaviour identical.
  - Calibration block: replace `_calibration_was_holding` with `_calibration_prev = self._calibration_plan` (captured before the reset); replace `_cal_soc_fc` with
    ```python
    _cal_slots = calibration.build_calib_slots(
        horizon, now, inputs.soc, _slot_minutes,
        self._export_price_fn(now, horizon, _export_price, _export_slots, _slot_minutes, _export_matches_import),
        self._delivered_fn(delivered_now, _slot_minutes),
    )
    ```
    and pass `_cal_slots`, `_calibration_prev`, `_dp_out.get("water_value")` to `_calibration_compute_sync(since_iso, now, soc_pct, cal_slots, price_history, prev, water_value)`, which forwards `prev=`/`water_value=` to `calibration.calibration_plan`.
  - Overdue log: `_peak = calibration.plan_peak_soc(inputs.soc, _cal_slots)` vs `const.CALIBRATION_MIN_START_SOC`; keep substrings `"peaks at"` (gate branch) and `"grace"` + `"price bar bypassed"` (forcing branch); forcing text: `"...taking the cheapest window up to €%.2f with the price bar bypassed..."` with `const.CALIBRATION_OVERDUE_COST_CAP_EUR`.
  - Status: `self.last_status["calibration_cost_eur"] = round(_cal.cost_eur, 3) if _cal.cost_eur is not None else None`; `sensor.py`: add `"calibration_cost_eur": self._controller.last_status.get("calibration_cost_eur")`.
- [ ] Tests (`tests/test_calibration_controller.py`):
  - `test_calibration_soc_wobble…`: add `monkeypatch.setattr(calibration, "window_cost", lambda *a, **k: 0.0)` (fresh top-out now costed); assertions unchanged.
  - new `test_prev_plan_is_threaded`: spy `calibration.calibration_plan` returning `CalibPlan("charging", BASE, BASE+timedelta(minutes=40), 0.1)`; two ticks; 2nd call's `kwargs["prev"].phase == "charging"`.
  - new `test_water_value_reaches_the_policy`: `_wrap_compute_decision`-style wrapper sets `_out["water_value"] = 0.123`; spy asserts `kwargs["water_value"] == 0.123`.
  - new `test_cal_slots_exclude_past_and_estimated_rows`: spy captures `cal_slots` (3rd positional); assert every `s.t1 > now` and none from `estimated`/`actual` rows (compare against `ctrl.last_status["plan"]["horizon"]`).
  - new `test_cost_is_published`: plan stub `CalibPlan("idle", None, None, 0.61749)` ⇒ `last_status["calibration_cost_eur"] == 0.617`.
  - `test_overdue_warning_reports_the_gate…`: docstring const name → `CALIBRATION_MIN_START_SOC`; logic unchanged.
- [ ] `.venv/bin/python -m pytest tests/test_calibration_controller.py tests/test_daily_stats_controller.py tests/test_daily_stats.py tests/test_lovelace_plan_card.py -q` → pass.
- [ ] Commit `feat(controller): feed calibration the plan's costs and its previous state`.

### Task 7: Gate + review

- [ ] Full suite from the worktree: `.venv/bin/python -m pytest tests/ -q` and `.venv/bin/python -m pytest tests_addon/ -q` → all pass (report counts).
- [ ] `pre-commit run --from-ref main --to-ref HEAD` → clean.
- [ ] `code-review` agent on `main..HEAD`; fix CONFIRMED findings (new commit per fix).
- [ ] Offline sanity: build a 45a-shaped horizon (full-by-17:00, 6 kWh export 17:00-18:00 at €0.40, midday PV spill) through `build_calib_slots` + `calibration_plan` (due, not overdue) ⇒ hold NOT at 17:00; midday hold accepted. Scratchpad script, not committed.
- [ ] `graphify update .` (main checkout after ff).
- [ ] Fast-forward `main` to the branch (`git merge --ff-only feat/calibration-cost-aware` in main checkout); do NOT push unless user asks.

### Task 8: Deploy lab + 45a

Per box — lab: SSH `root@172.20.0.47`, API `https://homeassistant.lab.kle.ist`, token `~/Sites/.token`; 45a: SSH `root@192.168.31.74`, API `https://45a.nl.kle.ist`, token `~/Sites/.token-45`. Deploy from the pristine worktree at the gated commit, never the shared main checkout.

- [ ] Backup OUTSIDE custom_components: `ssh <box> 'mkdir -p /config/x1_backups && cp -a /config/custom_components/anker_x1_smartgrid /config/x1_backups/anker_x1_smartgrid.$(date +%Y%m%d-%H%M%S)'`
- [ ] Ship: `git archive HEAD custom_components/anker_x1_smartgrid | ssh <box> 'tar -x -C /config && rm -rf /config/custom_components/anker_x1_smartgrid/__pycache__'`
- [ ] `ssh <box> 'ha core check && ha core restart'` (no `ha` CLI ⇒ REST `POST /api/services/homeassistant/restart`, 504 through proxy is normal). Reload-config-entry is NOT enough (modules not re-imported).
- [ ] Verify after ~2 min: plan sensor (`curl …/api/states | jq '.[] | select(.attributes.calibration_state != null) | .entity_id'`) shows `calibration_cost_eur` key; `ha core logs | grep -iE 'anker_x1_smartgrid|calibration'` has no tracebacks; report `calibration_state`, `calibration_cost_eur`, `calibration_days_since`, `calibration_window_*` per box.
- [ ] Update memory `calibration-cost-aware-placement.md` (commit SHA, deployed boxes, first observed costs).

## Unresolved questions

- None blocking. Watch: first live `calibration_cost_eur` values to tune €0.50 / €1.00.
