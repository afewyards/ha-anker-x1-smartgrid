# Refactor Audit Execution Plan (2026-07-12)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Execute the 2026-07-12 refactor audit: dead-code/test hygiene, helper extraction, CI gates, pure-planner split out of controller.py, parity-pair dedup, executor split.

**Architecture:** Five gated phases, risk-ascending. A/B = zero planner risk. C = mechanical code motion of the already-HA-free pure planner. D = optimize↔regret shared helpers, gated on parity-sweep extension. E = executor split, gated on new DP→executor integration test. Source spec = audit report (chat 2026-07-12) + `docs/reviews/2026-07-08-full-project-review.md`.

**Tech Stack:** Python 3.12, pytest (`.venv/bin/python -m pytest`), ruff, pyright, GitHub Actions.

## Global Constraints

- **PARITY GATE after EVERY task in C/D/E:** `.venv/bin/python -m pytest tests/test_optimize_parity.py tests/test_optimize_dt60_golden.py tests/test_parity_flags_off.py tests/test_15min_golden.py tests/test_ride_to_trough_golden.py -q` → all green. DP output byte-identical at default flags.
- Full suite (`tests/` + `tests_addon/`, 1988 tests) green before each phase merges to main.
- Vendored set (`const, dataquality, rollup, loadmodel, featureset, recorder, hgbr, backtest`): any touch → run `./addon/anker_x1_forecast/sync_core.sh`, commit `SOURCE_SHA256` in same commit. NEVER place new shared helpers in vendored modules.
- No merge commits (ff-only). `committing` skill (Angular convention) for every commit. One task = one commit unless stated.
- Engineers: ≤5 files/task; run tests via test-runner subagent.
- Refactor = behavior-identical. Any test needing edits beyond imports/paths → STOP, flag for review.

---

## Phase A — zero-risk cleanups (branch `refactor/phase-a-helpers`)

### Task A1: ruff hygiene in tests
**Files:** Modify: ~20 files in `tests/`, `tests_addon/` (autofix)
- [ ] `uvx ruff check --isolated --no-cache --select F401,F811,F841 --fix tests tests_addon`; fix the 1 F811 (`PriceSlot` redef, `tests/test_controller_export_executor.py`) + remaining ~6 manually
- [ ] Full suite green → commit `test: remove unused imports/locals across test suite`

### Task A2: remove test-only zombie `_bin_index`
**Files:** Modify: `custom_components/anker_x1_smartgrid/efficiency.py:95`, `tests/test_efficiency.py`
- [ ] Repoint 3 test call sites to module-level `bin_index()`; delete `EfficiencyCurve._bin_index`
- [ ] `pytest tests/test_efficiency*.py -q` green → commit `refactor(efficiency): drop test-only _bin_index delegate`

### Task A3: register pytest markers
**Files:** Modify: `pyproject.toml` (`[tool.pytest.ini_options]`)
- [ ] Add `markers = ["benchmark: perf benchmarks", "e2e: end-to-end", "golden: golden-pinned", "parity: DP-oracle parity", "acceptance: acceptance"]`; verify `pytest --collect-only -q 2>&1 | grep -c Warning` → 0
- [ ] Commit `test: register pytest markers (kills benchmark warning)`

### Task A4: `_safe_release` helper (9 duplicate sites)
**Files:** Modify: `custom_components/anker_x1_smartgrid/controller.py`
**Produces:** `async def _safe_release(self, now, context: str, *, reset_export: bool = True) -> None` — try/`release_to_self()`/log-error + `ExportState(engaged=False, state_since=now)` reset
- [ ] Add helper; replace sites at 1577, 1709, 1891, 2155 (reset only → `reset_export`-only path), 2159 (release only), 2310, 2320, 2331, 2343. Keep `self.plan` resets at call sites. Log message → `context` arg
- [ ] `pytest tests/test_controller*.py tests/test_15min_executor.py tests/test_export_pnl_ledger.py -q` green → commit `refactor(controller): extract _safe_release for inverter release + export reset`

### Task A5: synthetic-night builder (3 sites)
**Files:** Modify: `custom_components/anker_x1_smartgrid/controller.py:641-661,677-700,879-915`
**Produces:** `_next_synthetic_pickup(after: datetime) -> datetime`; `_synthetic_night_rows(start, end, load_w_by_hod: dict[int, float] | None, fallback_w: float) -> list[ForecastInterval]` (module level, pure)
- [ ] Write unit test first (`tests/test_controller_synthetic_night.py`): pickup rollover past midnight; rows zero-PV; load from hod-dict vs flat fallback
- [ ] Extract; replace 3 sites; reserve tests + new test green → commit `refactor(controller): extract synthetic-night interval builder`

### Task A6: canonical `hour_floor`
**Files:** Modify: `custom_components/anker_x1_smartgrid/resolution.py`, `plan.py`, `past_actuals.py`, then `controller.py`
- [ ] A6a: add `def hour_floor(dt): return floor_to_slot(dt, 60)` + unit test; delete `plan._hour`, `plan._slot`, `past_actuals._hour`, repoint. Commit `refactor: canonical resolution.hour_floor`
- [ ] A6b: replace 15 inline `replace(minute=0,...)` copies in controller.py (596, 600, 608, 813, 861, 907, 991, 1342, 1379, 1607, 1617, 2048, 2052, 2056-2058; skip 903 midnight variant). Full controller tests green. Commit `refactor(controller): use hour_floor everywhere`
- [ ] A6c (optional, same pattern): coordinator, intra_hour, parsers, remote_forecast, tariff. NOTE recorder+rollup are VENDORED → leave their inline copies alone (not worth a sync)

### Task A7: sensor.py spec table
**Files:** Modify: `custom_components/anker_x1_smartgrid/sensor.py`
- [ ] `SENSOR_SPECS: list[tuple[key, name, unit | None, state_class | None]]` + one generic `X1StatusSensor(_Base)`; collapse the 15 boilerplate classes (28-209, 241-253). Keep real classes: HouseLoad, Plan, FictivePlan, BatteryNetToday (attrs via optional `attrs_keys` spec)
- [ ] DO NOT touch `unique_id` format (entity identity — see Q3)
- [ ] `pytest tests/test_sensor*.py tests/test_entities*.py tests/test_cash_ledger_sensors.py -q` green → commit `refactor(sensor): declarative spec table for boilerplate sensors`

### Task A8: config_flow options table
**Files:** Modify: `custom_components/anker_x1_smartgrid/config_flow.py:223-482`
- [ ] `_TUNABLES: list[tuple[conf_key, default_const, validator]]` + small dicts for 3 select groups (slot_resolution, reserve_anchor, price_mode) + entity-picker specs; `_options_fields` → loop. While there: add `vol.Range` to `grid_export_limit_w` (review LOW)
- [ ] `pytest tests/test_config*.py -q` green → commit `refactor(config_flow): data-driven options schema`

### Task A9: persist/restore field table
**Files:** Modify: `custom_components/anker_x1_smartgrid/controller.py:3116-3208`
- [ ] `_PERSIST_FIELDS: list[tuple[store_key, attr, to_json, from_json]]`; `_persist` + `restore` iterate it (keep per-field try/except semantics)
- [ ] `pytest tests/test_controller.py -k persist -q` + restore tests green → commit `refactor(controller): table-driven persist/restore`

**Phase A merge gate:** full suite green → ff-merge to main.

---

## Phase B — test infra + CI (branch `refactor/phase-b-testinfra`)

### Task B1: `tests/helpers.py`
**Files:** Create: `tests/helpers.py`; Modify: `tests/conftest.py`
**Produces:** `StubHass` (+`_States`/`_StateObj`), `StubActuator` (capturing; injectable `fail_on: set[str]` for raising variants), `StubRecorder`, `StubStore`, `make_config(**overrides)` (promoted verbatim from `tests/test_optimize_parity.py:make_cfg` signature), `make_controller(hass=None, actuator=None, data_overrides=None)` (from `test_controller.py` harness + `_seed_valid_inputs`)
- [ ] Copy canonical impls (StubHass copies are byte-identical — take `test_controller.py`'s); add `recorder_db(tmp_path)` fixture in conftest
- [ ] Sanity: `pytest tests/test_controller.py -q` still green (nothing imports helpers yet) → commit `test: add shared helpers module (stubs + factories)`

### Task B2: migrate stub definitions → helpers (3 sub-tasks, ≤5 files each)
**Files:** Modify: the 14 StubHass files, 12 `_make_controller` files, 39 `_cfg()` files — batched B2a/B2b/B2c…; delete local defs, import from `tests.helpers`
- [ ] Per batch: swap, run batch files, commit `test: migrate <batch> to shared helpers`
- [ ] Also kill cross-test-file imports (10 sites listed in audit: `test_controller_static.py:6-7`, `test_controller_tick_guard.py:8`, `test_controller_dp_executor.py:7`, `test_controller_async_recorder.py:10`, `test_optimize_hedge.py:16`, `test_plan_hedge.py:9`, `test_soc_drift_acceptance.py:25-26`, `test_config_flow.py:9`, `test_anker_reload.py:5`, `test_cash_ledger.py:98`) — repoint to helpers where the symbol moved; `make_cfg`/`call_both` stay importable from `test_optimize_parity.py` as aliases of helpers versions
- [ ] Full suite green → done

### Task B3: CI lint job + release gating
**Files:** Modify: `.github/workflows/tests.yml`, `.github/workflows/release.yml`
- [ ] tests.yml: add `lint` job — `uvx ruff check .`, `uvx ruff format --check .`, `pyright` (pin versions to `.pre-commit-config.yaml` revs)
- [ ] release.yml: trigger on `workflow_run` of Tests (branch main, conclusion success) instead of bare push
- [ ] Commit `ci: lint job + gate release on tests`. Verify on next push: release waits for tests

### Task B4: golden regen path
**Files:** Create: `tests/regen_goldens.py`; Modify: `tests/test_optimize_dt60_golden.py`, `tests/test_ride_to_trough_golden.py`
- [ ] `python -m tests.regen_goldens` recomputes dt60 `GOLDEN` dict and prints paste-ready block; header comment in golden file: `# REGEN: python -m tests.regen_goldens`
- [ ] Loosen `rsv[NOW] == 6.831764705882355` to `pytest.approx(..., abs=0.05)` band (property asserts stay exact)
- [ ] Commit `test: documented golden regen path + band ride-to-trough scalar`

### Task B5: vendor gate + dep pins
**Files:** Modify: `tests/test_vendored_parity.py`, `.github/workflows/tests.yml`, `requirements_test.txt`
- [ ] Glob `forecast_core/*.py` instead of hardcoded 8-module list (mirror `tests_addon/test_vendor_parity.py` approach)
- [ ] CI step: run `./addon/anker_x1_forecast/sync_core.sh && git diff --exit-code addon/` (fails on drift)
- [ ] Pins: `scikit-learn==1.5.2`, `pytest-asyncio==<current>`; tests.yml installs `addon/anker_x1_forecast/requirements.txt` instead of inline `fastapi==... uvicorn==... holidays==...`
- [ ] Commit `ci: harden vendor gate, pin ML/test deps to prod`

**Phase B merge gate:** full suite + CI green on branch push → ff-merge.

---

## Phase C — pure-planner extraction (branch `refactor/phase-c-planner`)

### Task C1: import-boundary contract test (DO FIRST — locks the invariant C2 relies on)
**Files:** Create: `tests/test_import_boundaries.py`
- [ ] Asserts: (1) `optimize, regret, models, resolution, efficiency, export_filter, energy, decision` + all `forecast_core/*.py` contain no `homeassistant` import (AST walk); (2) leaf modules (`const, models, resolution`) import neither `controller`, `coordinator`, nor `__init__`
- [ ] Commit `test: import-boundary contract for pure planner + vendored core`

### Task C2: extract `decision.py` (pure planner, ~1100 lines)
**Files:** Create: `custom_components/anker_x1_smartgrid/decision.py`; Modify: `controller.py`
- [ ] Move verbatim (no logic edits): `_trough_by_hour` (48), `_dp_window` (94), `_dp_select_slots` (120-500), `_build_is_cheap_by_hour` (503), `_build_reserve_by_hour` (544), `_apply_price_prior` (709), `compute_decision` (768-1157) + their imports
- [ ] `controller.py`: `from .decision import compute_decision, _dp_select_slots, ...` (re-export so tests importing from controller keep working — verify which tests import these and repoint in same commit if trivial)
- [ ] PARITY GATE + full suite green → commit `refactor: extract pure planner into decision.py (verbatim move)`

### Task C3: extract `regret_job.py`
**Files:** Create: `custom_components/anker_x1_smartgrid/regret_job.py`; Modify: `controller.py:2480-2920`
**Produces:** `def run_daily_regret(recorder, cfg, day, computed_ts, *, shadow_dp_fn=None) -> RegretResult`; `def backfill_regret(recorder, cfg, days)` — controller thin-wraps in executor thread
- [ ] Verbatim move of `_run_daily_regret_sync` + `_backfill_regret_sync`; controller keeps async wrapper + scheduling
- [ ] PARITY GATE (regret is the oracle mirror) + `pytest tests/test_regret*.py -q` → commit `refactor: extract daily regret job from controller`

### Task C4: hedge/ledger/snapshot extractions (3 commits)
**Files:** Modify: `controller.py`; Create: `custom_components/anker_x1_smartgrid/ledger.py`, `snapshot.py`
- [ ] `_apply_drift_hedge(...) -> dict` from inline 1968-2061 (flag ships OFF-by-default upstream, but hedge=0.5 LIVE on lab — behavior-identical move, test with hedge>0: `tests/test_controller_drift_hedge.py`)
- [ ] `ledger.CashLedger` owning today_*/lifetime_* + `_rollover_daily_ledgers` (3025) + `_accumulate_cash_ledger` (3042) + hour-acc block (2110-2137); `tests/test_cash_ledger*.py`
- [ ] `snapshot.build_snapshot(decision, state, cfg) -> dict` from `_build_decision_snapshot` (2920) + `_status` (2974) + `_occ_status_attrs` (2955); `tests/test_sensor_plan.py` + shadow tests
- [ ] Commits: `refactor(controller): extract drift-hedge helper` / `...cash ledger module` / `...snapshot module`

### Task C5: `_tick_impl` sequencing helpers
**Files:** Modify: `controller.py`
- [ ] Extract shared disabled/enabled sub-flows: `_publish_fictive_plan(slots, dp_out, soc, deadline)` (1856-1878 vs 2437-2459), `_maybe_refresh_models(now)` (1787-1797 vs 1919-1930), `_read_tick_inputs()` (1728-1745 vs 1882-1942), `_run_compute_decision(plan, inputs, bundle, *, shadow)` (1760-1776 vs 2063-2081)
- [ ] `pytest tests/test_shadow_logging.py tests/test_controller*.py -q` green → commit `refactor(controller): dedupe disabled/enabled tick sub-flows`

**Phase C merge gate:** PARITY GATE + full suite + import-boundary test green → ff-merge.

---

## Phase D — parity-pair helpers (branch `refactor/phase-d-parity`; GATED)

### Task D0: GATE — extend parity sweep (review T3) BEFORE any D extraction
**Files:** Modify: `tests/test_optimize_parity.py`
- [ ] Sweep `reserve_anchor="ride_to_trough"` (the live default, never swept) across the 20 seeded random days + structural cases; pin dt=60 end-to-end (review T4)
- [ ] Commit `test(parity): sweep ride_to_trough anchor + pin dt60`

### Tasks D1-D5: one extraction per commit, PARITY GATE after each
Helpers live in `regret.py` or new `dp_common.py` — NEVER `const.py` (vendored).
- [ ] D1 `soc_bins(cap_kwh) -> (n_states, to_bin, from_bin)` — `optimize.py:650-660` ≡ `regret.py:434-441`
- [ ] D2 `select_end_state(dp, *, terminal_mode, water_value, firmware_floor_kwh, floor_kwh, target_kwh, to_bin, from_bin, n_states) -> (idx, value, fallback_used)` — `optimize.py:875-931` ≡ `regret.py:595-647` (~55 identical lines)
- [ ] D3 export-leg precompute — `optimize.py:591-632` vs `regret.py:407-432`; parameterize `day_index=None` (only real diff)
- [ ] D4 `Config.eta_charge_safe()` + `Config.eta_discharge_static()` in `models.py`; replace 7 sites (optimize 105-123+594, regret 87-88+413+859, energy 84-87+156-157, plan 148) — kills the "avoid import cycle" inline comments
- [ ] D5 `Config.pct_to_kwh/kwh_to_pct` + `floor_kwh/target_kwh/firmware_floor_kwh` properties; replace ~16 sites (audit list)
- [ ] Commits: `refactor(dp): shared <helper> for optimize/regret parity pair` each
- [ ] SKIP (explicitly deferred): export action-class inner loop (`optimize.py:814-867` vs `regret.py:536-589`) — parent-tuple shapes differ, bad risk/benefit

**Phase D merge gate:** PARITY GATE + full suite green after EVERY task, then ff-merge.

---

## Phase E — executor split (branch `refactor/phase-e-executor`; GATED)

### Task E1: GATE — DP→executor integration test (review gap, must exist before extraction)
**Files:** Create: `tests/test_dp_to_executor_e2e.py`
- [ ] Real `compute_decision` (real DP, no stubs) → real tick executor path → assert clamped setpoint reaches StubActuator: cases = charge FORCING hour, export hour (clamp ±6000), passive hour, anti-fight (stale FORCING vs export)
- [ ] Commit `test: DP-to-executor integration coverage`

### Task E1b: lint-debt sweep + flip lint job to blocking (added at Phase B gate)
**Files:** repo-wide mechanical; after all code moves are done
- [ ] `ruff check --fix` + `ruff format` over custom_components/ addon/ tests/ tests_addon/ (vendored files: run sync_core.sh after so SOURCE_SHA256 stays true); PARITY GATE + full suite after
- [ ] Remove `continue-on-error: true` from the lint job in tests.yml (it ships advisory because pre-commit lints whole touched files and legacy debt would red every push until this sweep)
- [ ] Commit `style: repo-wide ruff sweep` + `ci: make lint job blocking`

### Task E2: extract `executor.py`
**Files:** Create: `custom_components/anker_x1_smartgrid/executor.py`; Modify: `controller.py`
- [ ] Move FORCING block (2139-2164), C3 export executor (2165-2352), disabled-release (1695-1727), failsafe (1882-1898); actuator surface: `release_to_self, engage_and_charge, engage_export` + `guard.command_setpoint`, `decide_export_state`; `_safe_release` (A4) moves along
- [ ] E1 test + `tests/test_controller_export_executor.py` + hysteresis/anti-fight tests green → commit `refactor: extract live executor from controller`

**Phase E merge gate:** full suite + E1 + PARITY GATE green → ff-merge.

---

## Phase F — backlog (NOT in this plan; plan separately if wanted)
x1planner standalone package · backtest CLI (`python -m ... --db --days N`) · hypothesis DP property tests · export observability sensors (review E2 + recurring memory gap) · tests/ subdirectory reorg (unblocked after B2) · sensor `device_info`/unique_id-collision fix (entity-identity migration) · pytest-cov report-only gate · `PlanningContext`/`HorizonOverlays` param-blob dataclasses (audit M-1; do after C so signatures settle) · energy-tiering convergence past_actuals/regret/rollup (audit H-10; semantics differ deliberately — needs design, ties into review D2 fix) · `_build_is_cheap_by_hour` price-map convergence (audit M-3, verify-first).

---

## Unresolved questions

1. **Scope:** run all phases A→E, or A+B now and reassess before C (planner) / D (parity) / E (executor)?
2. **Sequencing vs deploy backlog:** main is UNPUSHED with several undeployed waves (kWh-native, ledger, idle-drain, occupancy). Push/deploy current backlog first so refactor waves don't entangle rollback?
3. **A7 side finding:** sensor `unique_id` collision + missing `device_info` — fixing changes entity identity (breaks history/dashboards). Defer to Phase F migration task (my default), or fold into A7?
4. **B3 release gating:** `workflow_run` trigger delays releases by one CI round-trip — acceptable?
5. **Phase F:** which items (if any) should get their own plan next?
