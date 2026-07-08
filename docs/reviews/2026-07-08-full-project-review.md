# Full Project Review — 2026-07-08

Six-agent adversarial deep-dive of main @ `2f888e3` (working tree clean). Domains: planner/DP core, live executor, data layer, ML pipeline, addon/vendoring/CI/config, test suite. Known findings from the 2026-07-02 review and the 2026-07-08 audit were excluded up front; everything below is new (or newly concrete).

**Verdict: no Critical anywhere. Three HIGHs — one live today, two latent behind dormant features. The safety core (tick lock, failsafe, clamps, FORCING gating, reserve math, DP parity discipline, SQL/DST hygiene) was independently re-verified sound by every agent that touched it.**

---

## Corrections to prior audit claims (verified this review)

- **The vendor-parity CI gate DOES run.** `.github/workflows/tests.yml` runs both `pytest tests/` (which includes `test_vendored_parity.py`, a pure SHA compare) and `pytest tests_addon/`. The 2026-07-08 audit claim "tests_addon not in CI / parity gate never runs" is wrong for current main. Memory updated.
- **Vendored drift is zero.** All 8 vendored `forecast_core` modules are byte-identical to their integration counterparts; the SOURCE_SHA256 manifest matches current source.
- **The two "missing" repair-issue strings exist.** `soc_floor_above_target` and `anker_roles_missing` are present in `strings.json` == `translations/en.json` (byte-identical). The 2026-07-02 deferred follow-up is already done.

---

## HIGH findings

### H1 — Disabled-path restart never releases an engaged inverter (LIVE)
`controller.py:1560-1571` | executor | confirmed mechanism, narrow trigger

Release on the disabled path is gated only on the in-memory `actuator.engaged`, which is never persisted (`_persist()` at 2713-2733 covers plan/enabled/export_state/pnl/soc_drift). Scenario: master switch OFF while exporting or FORCING → HA crash/power-loss within the ≤60 s before the next tick releases → on restart `enabled=False`, `actuator.engaged=False` (fresh) → release skipped; the `export_state.engaged` branch only resets memory, no `release_to_self()`. The inverter keeps executing its last VPP command (draining to grid or grid-charging) indefinitely while the switch reports hands-off. `test_controller.py:213` bakes the gap in.
**Fix:** derive "was engaged" from persisted `plan.state==FORCING OR export_state.engaged` and fire one `release_to_self()` on the first disabled tick after (re)start, preserving the intentional don't-clobber-manual behavior afterwards.

### H2 — Multi-source PV summed before hourly averaging corrupts mixed-cadence forecasts (LIVE if mixed cadences selected)
`coordinator.py:237` + `parsers.py:191` | ML | confirmed logic, trigger requires mixed cadences

`_read_pv_watts` sums arrays at coincident UTC timestamps, then `build_pv_curve_from_watts` takes the mean per hour bucket. Mixing an hourly source with a 30-min source: hour with A={:00→1000} and B={:00→500, :30→600} → merged {1500, 600} → mean 1050 W vs correct 1550 W — the hourly source is effectively halved. Silent PV under-forecast into the DP (over-reserve, over-charge from grid); no backtest catches it. The multi-select PV picker is deployed, so this is reachable today (e.g. Solcast fusion + Open-Meteo).
**Fix:** resample each source to the common hourly grid independently, then sum.

### H3 — Addon read-only WAL open will fail or flake on first activation (LATENT — gates addon activation)
`addon/anker_x1_forecast/trainer.py:171` + addon `config.yaml:12` | addon | plausible (not executed on HAOS)

The addon mounts `config:ro` and opens the recorder DB with `mode=ro`, but the DB is WAL-journaled and written every 60 s. SQLite readers need read-write access to the `-shm` wal-index; the ro-mount forbids it, and the heap-index fallback needs a quiescent WAL that a live writer denies. Expected result on dormant→started: `train_once` → OperationalError → `ready=False` forever (or intermittent training on stale pre-checkpoint snapshots); same root cause silently no-ops `refresh_model_lookups`. The `/health` endpoint cannot distinguish this from "no data yet" (see L-tier).
**Fix options:** integration runs periodic `PRAGMA wal_checkpoint(TRUNCATE)` + addon opens `mode=ro&immutable=1` (accepts checkpoint lag); or export rows over HTTP / copy into addon-writable `/data`. Validate on-box before promoting the addon out of dormancy.

---

## MEDIUM findings

### Planner
- **P1 — Export-leg efficiency asymmetry between the mirrored DPs** (`optimize.py:802` vs `regret.py:564-568,:686`). Online DP prices every export level at the flat `eta_discharge(max_export_w)` while its own charge leg uses the per-step curve; the oracle routes on per-step eta but reports with the scalar (`eur != best_cost` internally). Reproduced standalone: the parity suite structurally cannot see it (divergence lives in the routing objective; the existing curve-parity test's optimum happens to land in the same eta bin as max rate). Dormant behind `use_measured_eta` (default off) — reconcile plus add a bin-crossing parity test **before** enabling measured-eta.

### Executor
- **E1 — Blocking SQLite read on the event loop hourly** (`controller.py:1492` → `recorder.read_efficiency_samples`), even with `use_measured_eta` off; every sibling read uses `async_add_executor_job`. Wrap it, and skip entirely when the feature is off.
- **E2 — Active export invisible in `setpoint_w`/`state` sensors** (`controller.py:1991-1992,2231` + `sensor.py:39-44`): export path publishes `setpoint=0.0`/`passive`; the real setpoint reaches only the recorder column. Concrete confirmation of the tracked observability gap — surface export setpoint + an "exporting" state.
- **E3 — Unload/reload races an in-flight tick** (`__init__.py:124-132`): `release()` + `recorder.close()` take no tick lock; every options-change reload can interleave release with in-flight `engage_*` calls and close the DB under the tick. Acquire the tick lock in unload.

### Data layer
- **D1 — Future-dated ts freezes the hourly rollup** (`recorder.py:690-707`): watermark lower-bound `MAX(hour_ts)+1h` can exceed the current hour after a backward clock step (HAOS pre-NTP boot); the bounded read becomes empty every hour, `samples_hourly` stops growing silently for months, and recovery is impossible without manual intervention. Clamp watermark to the current hour; optionally reject future ts at append.
- **D2 — NULL-tick kWh undercount** (`rollup.py:176-182`): tier-1 fires on any non-NULL tick but treats NULLs as 0 rather than gaps — an hour with 6/60 ticks non-NULL undercounts ~90%. `house_load_kwh_sum` feeds `prev_day_total_kwh` (`featureset.py:425`/`hgbr.py:378`), an ML input to the DP. Also asymmetric vs the mean/target derive fallback. Add a coverage-ratio gate or scale by expected/non-NULL ticks. (`test_rollup_kwh_sums.py:140` currently blesses the wrong behavior.)

### ML
- **M1 — Train/serve skew: weather + persons features never served locally** (`forecast.py:109-119` + `featureset.py:98-110`): local HGBR trains on cloud_cover/humidity/wind_speed/persons_home but predict forwards only temp → those features are NaN at inference. Backtest is consistent (also temp-only) so promotion stays honest — the cost is dead-weight features and a weaker local tier exactly when weather matters. Thread the signals through predict (kwargs exist) or drop the columns.
- **M2 — load_adapt transient amplification** (`load_adapt.py:16-18,60-71`): fraction=1.0 with a 2-3 h window and MIN_MATCHED=2 lets a single 1 h spike drive the ratio to the 1.5 clamp and bias the next hours' P50 upward after the spike ended. Widen the window, lower the fraction, or raise MIN_MATCHED.

### Addon / CI
- **A1 — CI validates a different stack than prod ships:** unpinned scikit-learn vs prod `1.5.2` (`tests.yml:34`); Python 3.13-only matrix while the addon runs 3.12 and pyproject declares ≥3.12 (`tests.yml:14`); `holidays` unpinned in CI vs `==0.99` prod. Pin versions and add 3.12.
- **A2 — FastAPI request-schema tests silently skipped every CI run** (`tests_addon/test_server.py:18` `importorskip("fastapi")`, never installed). Install fastapi/uvicorn/pydantic in the addon step + add a TestClient smoke test.
- **A3 — Addon version frozen at 0.1.0** (addon `config.yaml:2` not in semantic_release version_variables) → HA store never detects addon updates; matches the historical v8→v10 lockstep pain.
- **A4 — Stock installs silently never get the learned model** (`manifest.json:9` + `hgbr.py`): `DEFAULT_USE_LEARNED_MODEL=True` but sklearn isn't a manifest requirement and the addon defaults off → permanent silent bucketed fallback. Surface it (one-time log + diagnostic/repair issue); do not add sklearn to the manifest.

### Test suite
- **T1 — Tautological test:** `test_charge_trough_lookback_heuristic.py:42-53` asserts PASSIVE for both lookback=8 and lookback=0 (the heuristic path it claims to pin was deleted); passes with the feature removed.
- **T2 — Per-day trough band has no end-to-end DP test** (the symmetric peak band has one, `test_optimize_peak_band.py:107,126`); a wrong slice/dropped day_index would pass every existing test.
- **T3 — DP↔oracle parity never swept with `reserve_anchor=ride_to_trough` (the live default!),** nor `export_peak_lookback_h>0`, `soc_hedge_fraction>0`, or dt_h=0.25. Extend the parametrized parity sweep.
- **T4 — "Byte-identical at dt=60" is not pinned end-to-end** (only per-helper reductions + a 15≠60 golden); mitigated because dt=60 is the live path exercised by the whole hourly suite.

---

## LOW findings (abridged)

- Planner: phantom 0.0-padded hours pass the trough charge-mask (`optimize.py:342-350`, latent — prices contiguous today, fail closed anyway); `score_regret` over-buy decomposition distorts on export-profitable days (`regret.py:954-956`, diagnostic-only); `resample_price_map` under-fills a coarse slot at a coarse→fine boundary (`resolution.py:98-109`, bites only during the Aug 2026 mixed-resolution cutover); peak-band day boundary sits at UTC midnight, internally consistent, benign.
- Executor: `weather.get_forecasts` called every 60 s tick instead of hourly, unbounded await at the top of the tick lock — a hung weather integration wedges ticks with the inverter parked (`controller.py:1496`); swallowed FORCING engage failure still publishes the intended setpoint/state (`controller.py:1979-1990`); DP runs synchronously on the loop — fine at 60-min, revisit at 15-min.
- Data: NULL-ts rows never purged (`recorder.py:466-471`); tier-2 sign-split landmine for pre-v9 hours (dormant, columns unconsumed); lexicographic ISO compare silently assumes +00:00 everywhere; DST local-hour bucketing degradation (~2 days/yr, documented).
- ML: 24 h horizon-energy promotion gate lacks a minimum origin count (`backtest.py:99-100`); baseline comparator buckets UTC vs HGBR local calendar (`backtest.py:338,359`); display SoC sim ZeroDivisionError at `round_trip_eff=0` (`plan.py:128,202,205`).
- Addon/config: `grid_export_limit_w` is the only power field without `vol.Range` — negative input corrupts export planning (no hardware risk, downstream clamps hold) (`config_flow.py:299-302`); concurrent `/predict` mutates shared model lookups in place (`server.py:122-124`); Dockerfile unpinned base/root user/no .dockerignore; no addon watchdog and `/health` can't distinguish "no data" from "DB unreadable" (masks H3); nested addon path may not be store-discoverable (self-documented).
- Tests: failsafe lock-release unasserted after exception; deadband-hold test too loose to catch removal; export-clamp tests never exercise the real 6000 W default; executor reserve callsite pinned off-path only; ride-to-trough "golden" is a loose bound (<0.7·cap); `test_vendored_parity.py` uses a hardcoded 8-module list — a 9th vendored module escapes the gate; `Controller.__new__` init-bypass in 7 test files is refactor-brittle; no single test drives real DP → real executor → clamped setpoint.

---

## Verified sound (independently re-confirmed across agents)

Tick re-entrancy lock airtight (no TOCTOU); whole-tick failsafe correct and tested; guard deadband genuinely one-directional (|result| ≤ |target|), export clamped to ±6000 W; FORCING strictly DP-gated with high-SoC/solar-ceiling preempting dwell; net-out + edge-hysteresis + anti-fight close the same-slot charge/export fight; no sell-below-reserve path; enabled-path restart self-heals from persisted state; spill credit provably decision-neutral; windowed peak/trough suffix logic and lookback conversions correct at all callsites; charge-margin hurdle present in both DPs and excluded from reported cash in both; export_filter min-block/tail-trim/exempt-index off-by-one-free; migrations idempotent with partial-failure recovery; all SQL parameterized; UTC rollup DST-safe; v9 energy deltas sound; no ML label leakage; honest walk-forward backtest; P50 contract consistent end-to-end; promotion can demote; efficiency measurement guards correct; remote client never raises; vendored parity 8/8 byte-identical with a CI gate that actually runs; strings/translations complete and consistent; test hygiene excellent (zero naive now(), zero sleeps/randomness).

---

## Recommended priorities

1. **Now (live exposure):** H1 disabled-path restart release; H2 PV mixed-cadence averaging; E1 blocking read; D1 rollup watermark clamp; D2 kWh coverage gate; E2 export observability.
2. **Before enabling each dormant feature:** H3 before addon activation (+ addon watchdog/health disambiguation); P1 + bin-crossing parity test before `use_measured_eta`; resolution coarse→fine fill + T3/T4 parity/identity sweeps before the 15-min cutover.
3. **CI/tests batch:** A1/A2 version pins + fastapi install; T1 tautology removal; T2 trough end-to-end test; parity sweep incl. `reserve_anchor=ride_to_trough`; A3 semantic-release addon version; A4 fallback surfacing.
