# 2026-07-08 Full-Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the actionable findings of the 2026-07-08 six-agent project review (1 live HIGH, 2 latent HIGHs, mediums, CI/test cleanup) on `anker_x1_smartgrid` + vendored `anker_x1_forecast` addon.

**Architecture:** Home Assistant custom integration (Python 3.12, no external runtime deps beyond `holidays`) + a vendored FastAPI addon that copies 8 HA-free `forecast_core` modules via `sync_core.sh` (SHA-manifest parity gate in CI). Fixes are surgical, each behind its existing config gate; dormant-feature fixes (P1/H3) preserve byte-parity when their flag is off.

**Tech Stack:** Python 3.12, pytest + pytest-asyncio, sqlite3 (WAL), voluptuous config-flow, FastAPI/uvicorn/pydantic (addon), scikit-learn 1.5.2 (addon only), GitHub Actions.

## Global Constraints

- Branch: `fix/review-fixes-2026-07-08` off `main` (@ `2f888e3`). Does NOT overlap `chore/audit-cleanup` (separate pending dead-code plan) — do not touch dead identifiers.
- **Line numbers drift** as `controller.py` / `optimize.py` / `regret.py` tasks land. Engineers MUST anchor every edit on the **QUOTED CODE** shown in each step, not on the absolute line numbers (which are as-of `2f888e3`).
- Engineers MUST NOT edit >5 files per task (source + test counted).
- **Vendored modules** (`const.py`, `dataquality.py`, `rollup.py`, `loadmodel.py`, `featureset.py`, `recorder.py`, `hgbr.py`, `backtest.py`): any task touching one MUST end with `bash addon/anker_x1_forecast/sync_core.sh` then `python -m pytest tests_addon/test_vendor_parity.py -q` (green). `sync_core.sh` regenerates the WHOLE `SOURCE_SHA256` manifest, so **vendored-module tasks run strictly sequentially** — never two in parallel. Vendored chain order (each blockedBy the previous): **Task 4 (recorder) → Task 5 (rollup) → Task 9 (recorder)**.
- **Shared-file serialization:** `controller.py` is edited by Tasks 1, 3, 6, 7, 9, 16, 18; `optimize.py` by 8, 18; `regret.py` by 8, 19. Tasks sharing a source file must be applied sequentially (blockedBy), rebasing on the prior — never parallel on the same file.
- Test runs are delegated to `test-runner` subagents at execution time; VERIFY steps show the exact `python -m pytest tests/... -x -q` command anyway.
- **REQUIRED SKILL:** invoke `committing` before EVERY `git commit`; Angular commit messages.
- Full suite (`tests/` + `tests_addon/`) green before merge; `code-review` agent on the whole branch before merge (major change).
- Deploy to HAOS is a SEPARATE post-merge step, NOT in this plan.

## Non-goals (explicitly deferred)

- **F4** resolution coarse→fine underfill (`resolution.py:98-109`) — dormant until the Aug-2026 15-min tariff cutover; the fix needs a genuinely-failing fixture and a principled span rule (current `span_min=max(slot_minutes,gap)` already fills a leading coarse slot; `max(gap,prev)` bleeds coarse price across middle slots). Do it inside the cutover work with a real repro.
- **E3** unload/reload tick-lock race — low impact, invasive lock refactor.
- **M2** `load_adapt` transient amplification — tuning decision; needs live user data.
- **M1** ML weather/persons serve threading — see Resolved decisions (d); 4-column thread-through OR drop later.
- DP-on-executor at 15-min (latent; revisit at slot_minutes=15).
- Dockerfile hardening, addon watchdog beyond `db_readable`.
- Loose-assertion test tightenings beyond T1/T2/T3.
- Backtest UTC-vs-local baseline + min-origin gate (`backtest.py:99,338,359`) — LOW, ML-metrics only.
- Tier-2 grid/batt sign-split pre-v9 landmine (`rollup.py:188-213`) — dormant, columns unconsumed.

---

## TIER 1 — Live exposure

### Task 1: H1 — disabled-path release on restart-while-engaged

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/controller.py` (`__init__` ~1171; `_tick_impl` top ~1488; disabled branch 1560-1571)
- Test: `tests/test_controller.py:212` (rewrite docstring) + two new tests

**Interfaces:**
- Consumes: `self._actuator.engaged: bool`, `self.export_state.engaged: bool`, `self.plan.state: ControllerState`, `ControllerState.FORCING`, `self._persist()`.
- Produces: `self._first_tick_after_start: bool`.

**Why the persist matters:** the disabled branch NEVER persists (`_persist` at 2713-2733 runs only on the enabled/failsafe paths), so after a first-tick release the store still reads `export_state.engaged=True`. Without persisting, EVERY later restart-while-disabled re-arms `_first_tick` and re-fires `release_to_self()` — clobbering a user-set manual mode and breaking the "hand back ONCE" guarantee (controller.py:1561-1563).

- [ ] **Step 1: Write the failing tests** — add to `tests/test_controller.py` after `test_tick_disabled` (line 223):

```python
@pytest.mark.asyncio
async def test_tick_disabled_after_restart_while_engaged_releases_once():
    """Restart into disabled while physically engaged (persisted export_state,
    fresh actuator.engaged=False) → ONE release on the first disabled tick, then
    hands-off within the same run (no clobber of a later manual mode)."""
    from custom_components.anker_x1_smartgrid.models import ExportState
    hass = _StubHass()
    ctrl, act = _make_controller(hass)
    ctrl.enabled = False
    ctrl.export_state = ExportState(engaged=True, state_since=BASE - timedelta(hours=1))
    act.engaged = False

    r1 = await ctrl.tick()
    assert r1["reason"] == "disabled"
    assert sum(1 for c in act.calls if c[0] == "release_to_self") == 1

    act.calls.clear()
    r2 = await ctrl.tick()
    assert not any(c[0] == "release_to_self" for c in act.calls)


@pytest.mark.asyncio
async def test_tick_disabled_persists_disengaged_so_next_restart_no_release():
    """The first-tick release must PERSIST disengaged/PASSIVE state, so a SECOND
    restart-while-disabled does NOT re-fire release (no repeated manual clobber)."""
    from custom_components.anker_x1_smartgrid.models import ExportState
    hass = _StubHass()
    store = ctrl_store = None
    # Capture what the controller persisted.
    saved = {}
    class _CaptureStore:
        async def async_save(self, data): saved.update(data)
    ctrl, act = _make_controller(hass)
    ctrl._store = _CaptureStore()
    ctrl.enabled = False
    ctrl.export_state = ExportState(engaged=True, state_since=BASE - timedelta(hours=1))
    act.engaged = False
    await ctrl.tick()
    assert saved.get("export_state", {}).get("engaged") is False   # persisted disengaged

    # Simulate a SECOND restart: fresh controller, restore the persisted (disengaged) state.
    ctrl2, act2 = _make_controller(_StubHass())
    ctrl2.restore(saved)
    ctrl2.enabled = False
    act2.engaged = False
    await ctrl2.tick()
    assert not any(c[0] == "release_to_self" for c in act2.calls)
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_controller.py::test_tick_disabled_after_restart_while_engaged_releases_once tests/test_controller.py::test_tick_disabled_persists_disengaged_so_next_restart_no_release -x -q`
Expected: FAIL — first test: 0 releases; second test: `export_state.engaged` still True (disabled path never persists) and the second restart re-fires release.

- [ ] **Step 3: Write minimal implementation**

In `__init__` next to `self._last_rollup_hour = -1` (line 1171), add:

```python
        self._first_tick_after_start = True
```

At the top of `_tick_impl`, immediately after `now = dt_util.utcnow()` (line 1488), capture-and-clear:

```python
        _first_tick = self._first_tick_after_start
        self._first_tick_after_start = False
```

Replace the disabled-branch release gate (lines 1564-1568):

```python
            if self._actuator.engaged:
                try:
                    await self._actuator.release_to_self()
                except Exception:
                    _LOGGER.error("Actuator release_to_self failed (disabled path)", exc_info=True)
```

with:

```python
            # Derive "was engaged" from PERSISTED state on the first tick after a
            # (re)start — actuator.engaged is in-memory only and resets to False on
            # restart, so a crash while exporting/FORCING would otherwise leave the
            # inverter executing its last VPP command forever. Fire ONE release; on
            # every later disabled tick fall back to the live actuator flag so we do
            # not clobber a user-set manual/modbus mode.
            _was_engaged = self._actuator.engaged or (
                _first_tick
                and (self.plan.state is ControllerState.FORCING or self.export_state.engaged)
            )
            if _was_engaged:
                try:
                    await self._actuator.release_to_self()
                except Exception:
                    _LOGGER.error("Actuator release_to_self failed (disabled path)", exc_info=True)
```

Then, right after the disabled branch resets `self.export_state` / `self.plan` (after line 1575 `self.plan = PlanState(ControllerState.PASSIVE, now, ())`), persist the disengaged state so a later restart-while-disabled does NOT re-fire the release:

```python
            # Persist the disengaged/PASSIVE state: the disabled branch otherwise
            # never writes the store, so a mid-disable restart would re-derive
            # "was engaged" from stale persisted export_state and re-release,
            # clobbering a user-set manual mode. Guarded on _first_tick so we do it
            # once per (re)start, not every disabled tick.
            if _first_tick:
                await self._persist()
```

Update `test_tick_disabled` (line 213) docstring to `"""...not engaged AND not restart-into-engaged → does NOT call release_to_self."""` (assertion unchanged).

- [ ] **Step 4: Run tests to verify pass**

Run: `python -m pytest tests/test_controller.py -x -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add custom_components/anker_x1_smartgrid/controller.py tests/test_controller.py
git commit -m "fix(executor): release inverter once on restart-while-disabled and persist disengaged"
```

---

### Task 2: H2 — resample each PV source to hourly grid before summing

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/coordinator.py` (`_read_pv_watts` 182-243, incl. return annotation at 186)
- Modify: `custom_components/anker_x1_smartgrid/parsers.py` (`build_pv_curve_from_watts` 128-193)
- Test: `tests/test_pv_watts.py`, `tests/test_15min_pv_curve.py`

**Interfaces:**
- Produces: `_read_pv_watts(...) -> list[list[tuple[datetime, float]]] | None` — per-source arrays (one inner list per source entity that yielded watts; `None` iff config non-empty but no source had watts; `[]` iff config empty).
- Produces: `build_pv_curve_from_watts(today_sources, tomorrow_sources, now, *, step_h=1.0)` where each `*_sources` is `list[list[tuple[datetime,float]]] | None`; resamples EACH source to `step_h` buckets (mean) independently, then SUMS per-bucket across sources. Byte-identical to old output for a single-source hourly input.
- Consumes: intermediate callers (`controller.py:804`, `plan.py:301`, `_read_forecast_bundle`) pass these tokens through unchanged — no edit needed.

- [ ] **Step 1: Write the failing test** — add to `tests/test_pv_watts.py`:

```python
def test_build_pv_curve_mixed_cadence_sums_hourly_means_not_pooled():
    """Two sources of different cadence each resample to hourly FIRST, then sum:
    hourly A={:00→1000} + 30-min B={:00→500,:30→600} → 1000 + 550 = 1550 W,
    NOT the pooled {1500,600}/2 = 1050."""
    from datetime import datetime, timezone
    def dt(h, m=0):
        return datetime(2026, 7, 8, h, m, tzinfo=timezone.utc)
    src_a = [(dt(9), 1000.0)]
    src_b = [(dt(9), 500.0), (dt(9, 30), 600.0)]
    curve = build_pv_curve_from_watts([src_a, src_b], None, dt(9), step_h=1.0)
    assert curve == [(dt(9), pytest.approx(1550.0))]


async def test_read_pv_today_watts_returns_per_source_arrays(hass):
    from custom_components.anker_x1_smartgrid import coordinator, const
    hass.states.async_set("sensor.a", "1.0", {"watts": {"2026-07-08T09:00:00+00:00": 1000}})
    hass.states.async_set("sensor.b", "1.0", {"watts": {
        "2026-07-08T09:00:00+00:00": 500, "2026-07-08T09:30:00+00:00": 600}})
    d = {const.CONF_ENT_PV_TODAY: ["sensor.a", "sensor.b"]}
    result = coordinator.read_pv_today_watts(hass, d)
    assert isinstance(result, list) and len(result) == 2
    assert all(isinstance(src, list) for src in result)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_pv_watts.py::test_build_pv_curve_mixed_cadence_sums_hourly_means_not_pooled tests/test_pv_watts.py::test_read_pv_today_watts_returns_per_source_arrays -x -q`
Expected: FAIL — pooled mean gives 1050.0; `_read_pv_watts` returns a flat list.

- [ ] **Step 3: Write minimal implementation**

In `coordinator.py` `_read_pv_watts`, update the return annotation (line 186) and replace the merged-dict accumulation (202-243) with per-source arrays:

```python
def _read_pv_watts(
    hass: HomeAssistant,
    data: dict,
    kwh_key: str,
) -> list[list[tuple[datetime, float]]] | None:
```

```python
    kwh_list: list[str] = data.get(kwh_key, const.DEFAULT_ENTITIES.get(kwh_key, []))
    if not kwh_list:
        return []

    per_source: list[list[tuple[datetime, float]]] = []
    any_array_yielded = False

    for entity_id in kwh_list:
        state = hass.states.get(entity_id)
        watts_dict = None
        if state is not None:
            candidate = state.attributes.get("watts")
            if candidate:
                watts_dict = candidate
        if watts_dict is None and entity_id.endswith("_remaining"):
            sibling_id = entity_id[: -len("_remaining")]
            sibling_state = hass.states.get(sibling_id)
            if sibling_state is not None:
                candidate = sibling_state.attributes.get("watts")
                if candidate:
                    watts_dict = candidate
        if watts_dict is None:
            continue
        any_array_yielded = True
        samples: list[tuple[datetime, float]] = []
        for k, v in watts_dict.items():
            dt_utc = _parse_dt(str(k))
            if dt_utc is None:
                continue
            try:
                w = float(v)
            except (ValueError, TypeError):
                continue
            samples.append((dt_utc, w))
        per_source.append(sorted(samples))

    if not any_array_yielded:
        return None
    return per_source
```

Update the docstring line 196 to `"Each source's samples are returned as a SEPARATE array (no cross-source pooling); resample+sum happens in build_pv_curve_from_watts."`.

In `parsers.py` `build_pv_curve_from_watts`, rename params `today_samples`/`tomorrow_samples` → `today_sources`/`tomorrow_sources` in the signature (128-134), and replace the flat-pool body (147-193):

```python
    sources: list[list[tuple[datetime, float]]] = []
    for group in (today_sources, tomorrow_sources):
        if group:
            for src in group:
                if src:
                    sources.append(src)
    if not sources:
        return []

    step_min = max(1, round(step_h * 60))

    def _floor(t: datetime) -> datetime:
        minute = (t.minute // step_min) * step_min
        return t.replace(minute=minute, second=0, microsecond=0)

    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now_h = _floor(now.astimezone(timezone.utc).replace(tzinfo=timezone.utc))

    # Resample EACH source to step_h buckets (mean within bucket) INDEPENDENTLY,
    # then sum the per-bucket means across sources. A coarse (hourly) source keeps
    # its full value; a fine (30-min) source averages within its hour — the two
    # then add, instead of pooling raw samples and diluting the coarse source.
    summed: dict[datetime, float] = {}
    for src in sources:
        buckets: dict[datetime, list[float]] = {}
        for dt, w in src:
            dt_utc = (dt.astimezone(timezone.utc).replace(tzinfo=timezone.utc)
                      if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc))
            bucket = _floor(dt_utc)
            if bucket < now_h:
                continue
            buckets.setdefault(bucket, []).append(w)
        for bucket, ws in buckets.items():
            summed[bucket] = summed.get(bucket, 0.0) + sum(ws) / len(ws)

    if not summed:
        return []
    hour_keys = sorted(summed)
    out: list[tuple[datetime, float]] = []
    h = hour_keys[0]
    while h <= hour_keys[-1]:
        out.append((h, summed.get(h, 0.0)))
        h += timedelta(hours=step_h)
    return out
```

In `tests/test_pv_watts.py` and `tests/test_15min_pv_curve.py`, wrap every existing single-source flat-list argument in an extra list (`build_pv_curve_from_watts([samples], None, now)`), and rewrite `test_build_pv_curve_from_watts_multi_array_averages_within_bucket` (line 117) + `test_read_pv_today_watts_multi_string_sums_per_timestamp` (line 291) to the per-source contract (multi-source now SUMS hourly means).

- [ ] **Step 4: Run tests to verify pass**

Run: `python -m pytest tests/test_pv_watts.py tests/test_15min_pv_curve.py -x -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add custom_components/anker_x1_smartgrid/coordinator.py custom_components/anker_x1_smartgrid/parsers.py tests/test_pv_watts.py tests/test_15min_pv_curve.py
git commit -m "fix(coordinator): resample each PV source to hourly grid before summing"
```

---

### Task 3: E1 — non-blocking efficiency read, skip when eta off

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/controller.py` (`_refresh_efficiency_curve` 1234-1254; comment 1489-1491; call site 1492)
- Test: `tests/test_controller.py` (new skip test + convert 3 existing tests to async)
- Depends on: Task 1 (shared `controller.py`).

**Interfaces:**
- Produces: `async def _refresh_efficiency_curve(self, now: datetime) -> None` (was sync). Early-returns when `not self.cfg.use_measured_eta`; wraps `read_efficiency_samples` in `async_add_executor_job`.

**Breaks 3 existing tests (must fix in this task):** `test_controller_builds_efficiency_curve_and_gates_off_by_default` (line 1870, asserts "always builds"), `test_refresh_efficiency_curve_is_cached_within_window` (1890), `test_refresh_efficiency_curve_falls_back_to_static_on_recorder_error` (1910) — all call `_refresh_efficiency_curve` synchronously and the first pins the old always-build contract.

- [ ] **Step 1: Write the failing test + fix the broken 3**

Add a call-counter to `_StubRecorder.read_efficiency_samples` (line 86):

```python
    def read_efficiency_samples(self, since_iso=None):
        self.efficiency_calls = getattr(self, "efficiency_calls", 0) + 1
        return []
```

New skip test:

```python
@pytest.mark.asyncio
async def test_efficiency_read_skipped_when_measured_eta_off():
    """use_measured_eta defaults False → the blocking efficiency read is skipped
    entirely (not just wrapped)."""
    hass = _StubHass()
    ctrl, act = _make_controller(hass)
    assert ctrl.cfg.use_measured_eta is False
    await ctrl.tick()
    assert getattr(ctrl._recorder, "efficiency_calls", 0) == 0
```

Rewrite the 3 existing tests to the new contract (async + `use_measured_eta=True` where the build must run):

```python
@pytest.mark.asyncio
async def test_efficiency_curve_built_only_when_measured_eta_on():
    """Skip-when-off: OFF (default) does NOT build from the recorder; ON builds
    and _planner_curve surfaces it."""
    from dataclasses import replace
    from custom_components.anker_x1_smartgrid.efficiency import EfficiencyCurve
    hass = _StubHass()
    ctrl, _ = _make_controller(hass)
    await ctrl._refresh_efficiency_curve(BASE)
    assert ctrl._eta_curve_built_at is None          # skipped
    assert ctrl._planner_curve() is None
    ctrl.cfg = replace(ctrl.cfg, use_measured_eta=True)
    await ctrl._refresh_efficiency_curve(BASE)
    assert isinstance(ctrl._eta_curve, EfficiencyCurve)
    assert ctrl._eta_curve_built_at == BASE
    assert ctrl._planner_curve() is ctrl._eta_curve


@pytest.mark.asyncio
async def test_refresh_efficiency_curve_is_cached_within_window():
    from dataclasses import replace
    hass = _StubHass()
    ctrl, _ = _make_controller(hass)
    ctrl.cfg = replace(ctrl.cfg, use_measured_eta=True)
    await ctrl._refresh_efficiency_curve(BASE)
    curve_after_first = ctrl._eta_curve
    built_at_first = ctrl._eta_curve_built_at
    soon = BASE + timedelta(seconds=const.EFFICIENCY_CACHE_SECONDS - 1)
    await ctrl._refresh_efficiency_curve(soon)
    assert ctrl._eta_curve is curve_after_first
    assert ctrl._eta_curve_built_at == built_at_first
    later = BASE + timedelta(seconds=const.EFFICIENCY_CACHE_SECONDS + 1)
    await ctrl._refresh_efficiency_curve(later)
    assert ctrl._eta_curve_built_at == later


@pytest.mark.asyncio
async def test_refresh_efficiency_curve_falls_back_to_static_on_recorder_error():
    from dataclasses import replace
    hass = _StubHass()
    ctrl, _ = _make_controller(hass)
    ctrl.cfg = replace(ctrl.cfg, use_measured_eta=True)
    def _boom(since_iso=None):
        raise RuntimeError("recorder unavailable")
    ctrl._recorder.read_efficiency_samples = _boom
    await ctrl._refresh_efficiency_curve(BASE)
    assert ctrl._eta_curve is not None
    assert ctrl._eta_curve_built_at == BASE
```

(Delete the old sync versions of these 3.)

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_controller.py -k efficiency -x -q`
Expected: FAIL — skip test sees `efficiency_calls == 1`; the rewritten async tests await a still-sync method.

- [ ] **Step 3: Write minimal implementation**

Replace `_refresh_efficiency_curve` (1234-1254):

```python
    async def _refresh_efficiency_curve(self, now: datetime) -> None:
        """Rebuild the measured efficiency curve from recent recorder samples.

        Skipped entirely when ``use_measured_eta`` is off (the default): the
        planner uses the static scalar curve via ``_planner_curve()`` returning
        None, so no read is needed. When on, cached for
        ``EFFICIENCY_CACHE_SECONDS`` and the SQLite read runs off-loop.
        """
        if not self.cfg.use_measured_eta:
            return
        if (
            self._eta_curve_built_at is not None
            and (now - self._eta_curve_built_at).total_seconds() < const.EFFICIENCY_CACHE_SECONDS
        ):
            return
        try:
            since = (now - timedelta(days=const.EFFICIENCY_WINDOW_DAYS)).isoformat()
            rows = await self._hass.async_add_executor_job(
                self._recorder.read_efficiency_samples, since
            )
            self._eta_curve = EfficiencyCurve.build(rows, self.cfg, now)
        except Exception:
            _LOGGER.warning("efficiency curve build failed; using static fallback", exc_info=True)
            self._eta_curve = EfficiencyCurve.static(self.cfg)
        self._eta_curve_built_at = now
```

Update the call site (1492): `await self._refresh_efficiency_curve(now)`.

Update the now-false comment (1489-1491) from:

```python
        # Refresh the measured efficiency curve (cached; cheap no-op most ticks).
        # Built regardless of cfg.use_measured_eta so the curve is warm the
        # moment the flag is flipped on; _planner_curve() is what gates it.
```

to:

```python
        # Refresh the measured efficiency curve off-loop (cached; cheap no-op most
        # ticks). Skipped entirely when use_measured_eta is off (default) — the
        # planner uses the static scalar; the curve rebuilds on the first tick after
        # the flag is flipped on.
```

- [ ] **Step 4: Run tests to verify pass**

Run: `python -m pytest tests/test_controller.py -x -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add custom_components/anker_x1_smartgrid/controller.py tests/test_controller.py
git commit -m "perf(executor): make efficiency-curve read non-blocking and skip when eta off"
```

---

### Task 4: D1 — clamp rollup watermark, drop future hourly rows, purge NULL-ts  *(VENDORED — chain #1)*

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/recorder.py` (`rollup_hours` 690-707 + pre-read cleanup; `purge_older_than` 466-471)
- Test: `tests/test_recorder.py`
- Ends with: `sync_core.sh` regen + vendor-parity green. **BlockedBy: none; blocks Task 5.**

**Interfaces:**
- Consumes: `DataRecorder(db_path)`, `.append(row)`, `.rollup_hours(now_iso) -> int`, `.read_hourly_rows() -> list[dict]`, `.purge_older_than(now_iso, days) -> int`.

**Write-side note:** `DataRecorder.append` has NO clock reference, so a future RAW `ts` cannot be rejected there. The correct write-side companion to the read-side watermark clamp is a `samples_hourly` cleanup in `rollup_hours` (a completed hour can never be ≥ `now`). This removes the bogus future hourly row so `MAX(hour_ts)` falls back to a real past hour and the bounded watermark read resumes — no perpetual full-scan.

- [ ] **Step 1: Write the failing test** — add to `tests/test_recorder.py`:

```python
def test_rollup_recovers_after_future_dated_watermark(tmp_path):
    """A future-dated sample rolled during a pre-NTP boot must NOT freeze the
    hourly rollup once the clock steps back; the bogus future hourly row is dropped."""
    rec = DataRecorder(str(tmp_path / "t.db"))
    rec.append({"ts": "2027-01-01T10:05:00+00:00", "p1_w": 100.0, "batt_w": 0.0,
                "pv_w": 0.0, "load_w": 100.0})
    rec.rollup_hours("2027-01-01T11:00:00+00:00")           # future watermark written
    rec.append({"ts": "2026-07-08T09:10:00+00:00", "p1_w": 200.0, "batt_w": 0.0,
                "pv_w": 0.0, "load_w": 200.0})
    rec.rollup_hours("2026-07-08T10:00:00+00:00")           # clock corrected
    hours = {r["hour_ts"] for r in rec.read_hourly_rows()}
    assert "2026-07-08T09:00:00+00:00" in hours             # recovered
    assert "2027-01-01T10:00:00+00:00" not in hours         # bogus future row dropped
    rec.close()


def test_purge_removes_null_ts_rows(tmp_path):
    rec = DataRecorder(str(tmp_path / "t.db"))
    rec.append({"ts": None, "p1_w": 1.0, "batt_w": 0.0, "pv_w": 0.0, "load_w": 1.0})
    removed = rec.purge_older_than("2026-07-08T00:00:00+00:00", 30)
    assert removed == 1
    rec.close()
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_recorder.py::test_rollup_recovers_after_future_dated_watermark tests/test_recorder.py::test_purge_removes_null_ts_rows -x -q`
Expected: FAIL — 09:00 hour missing (empty bounded read); future row still present; NULL-ts row not purged.

- [ ] **Step 3: Write minimal implementation**

In `rollup_hours`, immediately inside `with self._lock:` (after `current_hour_iso` is computed, before the `existing_hours` read at ~681), drop impossible future hourly rows:

```python
            # D1 (write-side companion to the watermark clamp): a completed hour can
            # never be >= now. Any such row is from a pre-NTP-boot rollup on a future
            # wall clock; drop it so MAX(hour_ts) falls back to a real past hour and
            # the bounded watermark read resumes (no perpetual full-scan). append()
            # has no clock reference, so a future RAW ts cannot be rejected there —
            # this is the correct home.
            self._conn.execute(
                "DELETE FROM samples_hourly WHERE hour_ts >= ?", (current_hour_iso,)
            )
```

Replace the watermark branch (695-707) with the clamp:

```python
            if watermark_hour:
                watermark_hour_end = (
                    datetime.fromisoformat(str(watermark_hour)) + timedelta(hours=1)
                ).isoformat()
            else:
                watermark_hour_end = None
            # Use the bounded read ONLY when the watermark is strictly before the
            # current hour; otherwise full-scan completed hours to recover (belt-and-
            # suspenders with the future-row DELETE above). existing_hours + upsert
            # keep it idempotent.
            if watermark_hour_end is not None and watermark_hour_end < current_hour_iso:
                cur.execute(
                    "SELECT * FROM samples WHERE ts >= ? AND ts < ? ORDER BY ts ASC",
                    (watermark_hour_end, current_hour_iso),
                )
            else:
                cur.execute(
                    "SELECT * FROM samples WHERE ts < ? ORDER BY ts ASC",
                    (current_hour_iso,),
                )
```

In `purge_older_than` (469), include NULL-ts rows:

```python
            cur = self._conn.execute(
                "DELETE FROM samples WHERE ts < ? OR ts IS NULL", (cutoff,)
            )
```

- [ ] **Step 4: Run tests to verify pass**

Run: `python -m pytest tests/test_recorder.py -x -q`
Expected: PASS.

- [ ] **Step 5: Re-vendor + parity gate**

Run: `bash addon/anker_x1_forecast/sync_core.sh && python -m pytest tests_addon/test_vendor_parity.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add custom_components/anker_x1_smartgrid/recorder.py addon/anker_x1_forecast/forecast_core/recorder.py addon/anker_x1_forecast/forecast_core/SOURCE_SHA256 tests/test_recorder.py
git commit -m "fix(recorder): clamp rollup watermark, drop future hourly rows, purge null-ts"
```

---

### Task 5: D2 — scale partial-null kWh sums by tick coverage  *(VENDORED — chain #2)*

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/rollup.py` (`_kwh_sum_pass` 162-178)
- Test: `tests/test_rollup_kwh_sums.py:140`
- Ends with: `sync_core.sh` regen + vendor-parity green. **BlockedBy: Task 4; blocks Task 9.**

**Interfaces:**
- Consumes: `aggregate_hour(rows) -> dict` with `*_kwh_sum` keys; `_ENERGY_KWH_COLUMNS`.
- Behaviour: tier-1 sum of a `*_kwh` column is scaled by `len(rows)/len(non_null)` so NULL ticks WITHIN recorded rows are filled by the observed per-tick average. Genuine HA downtime (fewer rows) is NOT inflated. `values == []` still → None (tier-2 fallback unchanged). `house_load_kwh_sum` feeds `prev_day_total_kwh` (an ML input), so the ML consumer suites must stay green.

- [ ] **Step 1: Write the failing test** — replace `test_aggregate_hour_kwh_sums_partial_null` (140-149) and add the sparse case:

```python
def test_aggregate_hour_kwh_sums_partial_null_scales_by_coverage():
    """NULL ticks within recorded rows are GAPS, not zero energy. 2 non-NULL
    (0.02+0.03) of 3 rows → 0.05 * 3/2 = 0.075."""
    rows = [
        {"ts": "2026-07-06T10:00:00+00:00", "pv_kwh": 0.02},
        {"ts": "2026-07-06T10:01:00+00:00", "pv_kwh": None},
        {"ts": "2026-07-06T10:02:00+00:00", "pv_kwh": 0.03},
    ]
    result = aggregate_hour(rows)
    assert result["pv_kwh_sum"] == pytest.approx(0.075)


def test_aggregate_hour_kwh_sums_sparse_coverage_no_undercount():
    """6 non-NULL ticks of 60 rows → scaled ×10, not ~90% undercount."""
    rows = [{"ts": f"2026-07-06T10:{m:02d}:00+00:00",
             "pv_kwh": (0.01 if m < 6 else None)} for m in range(60)]
    result = aggregate_hour(rows)
    assert result["pv_kwh_sum"] == pytest.approx(0.06 * 60 / 6)   # 0.6
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_rollup_kwh_sums.py::test_aggregate_hour_kwh_sums_partial_null_scales_by_coverage tests/test_rollup_kwh_sums.py::test_aggregate_hour_kwh_sums_sparse_coverage_no_undercount -x -q`
Expected: FAIL — current code returns 0.05 / 0.06 (raw non-NULL sum).

- [ ] **Step 3: Write minimal implementation**

Replace the tier-1 loop (176-178) in `_kwh_sum_pass`:

```python
    n_rows = len(rows)
    for col in _ENERGY_KWH_COLUMNS:
        values = [row[col] for row in rows if row.get(col) is not None]
        if not values:
            result[f"{col}_sum"] = None
        else:
            # Scale by coverage: NULL ticks among recorded rows are gaps, not 0.
            # Full coverage → ×1 (byte-identical). Genuine downtime (fewer rows) is
            # not inflated — only within-row NULL gaps are filled.
            result[f"{col}_sum"] = sum(values) * n_rows / len(values)
```

Update the `_kwh_sum_pass` docstring tier-1 sentence (165-167) to describe coverage scaling.

- [ ] **Step 4: Run tests to verify pass** (include the ML consumers of `*_kwh_sum`)

Run: `python -m pytest tests/test_rollup_kwh_sums.py tests/ -k "rollup or featureset or hgbr or backtest" -x -q`
Expected: PASS.

- [ ] **Step 5: Re-vendor + parity gate**

Run: `bash addon/anker_x1_forecast/sync_core.sh && python -m pytest tests_addon/test_vendor_parity.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add custom_components/anker_x1_smartgrid/rollup.py addon/anker_x1_forecast/forecast_core/rollup.py addon/anker_x1_forecast/forecast_core/SOURCE_SHA256 tests/test_rollup_kwh_sums.py
git commit -m "fix(rollup): scale partial-null kwh sums by tick coverage"
```

---

### Task 6: E2 — surface live export setpoint (sensor-only)

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/controller.py` (after `_status` call at 2231)
- Modify: `custom_components/anker_x1_smartgrid/sensor.py` (new sensor + registration 200-217)
- Test: `tests/test_controller_export_executor.py`, `tests/test_sensor.py`
- Depends on: Tasks 1, 3 (shared `controller.py`). See Resolved decisions (a).

**Interfaces:**
- Produces: `last_status["export_setpoint_w"]: float | None` (positive W = export rate). `last_status["state"]` is UNCHANGED — it stays `"passive"` during export. This is CORRECT: the recorder's `smartcharge_state` column is written by `_record_sample` (controller.py:2176-2187), which runs BEFORE this task's code and always records the plan state, never an export state. Keeping `last_status["state"]` == `"passive"` here matches the recorded history instead of diverging from it; no new state string, no automation-breakage risk.
- Produces: `X1ExportSetpointSensor` keyed on `"export_setpoint_w"` (unit W).

- [ ] **Step 1: Write the failing test** — add to `tests/test_controller_export_executor.py` inside `TestExportEngagePositiveSetpoint`:

```python
    @pytest.mark.asyncio
    async def test_export_setpoint_surfaced_state_stays_passive(self, monkeypatch):
        """Sensor-only observability (E2): export_setpoint_w surfaces in
        last_status during an active export tick; state correctly stays
        "passive" — consistent with the recorded smartcharge_state column
        (_record_sample runs before this point and always records the plan
        state, never an export state)."""
        monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
        hass = _StubHass()
        ctrl, act, store = _make_controller(hass)
        _seed_passive_inputs(hass, soc="90.0", export_price="0.40")
        cur_h = BASE.replace(minute=0, second=0, microsecond=0)
        monkeypatch.setattr(ctrl_mod, "compute_decision",
                            _patched_compute_decision(export_request={cur_h: 3000.0}))
        ctrl.export_state = ExportState(engaged=True, state_since=BASE - timedelta(hours=1))
        result = await ctrl.tick()
        assert any(c[0] == "engage_export" for c in act.calls)
        assert result["state"] == "passive"
        assert result["export_setpoint_w"] is not None and result["export_setpoint_w"] > 0
```

Sensor unit test in `tests/test_sensor.py`:

```python
def test_export_setpoint_sensor_reads_key():
    from custom_components.anker_x1_smartgrid.sensor import X1ExportSetpointSensor
    class _C: last_status = {"export_setpoint_w": 1500.0}
    s = X1ExportSetpointSensor(_C(), "e")
    assert s.native_value == 1500.0
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_controller_export_executor.py::TestExportEngagePositiveSetpoint::test_export_setpoint_surfaced_state_stays_passive -x -q`
Expected: FAIL — `result["export_setpoint_w"]` raises `KeyError` (key absent); the `state == "passive"` assertion already passes (no regression there).

- [ ] **Step 3: Write minimal implementation**

In `controller.py`, immediately after `result = self._status(now, setpoint, deadline, "ok", solar_charge=solar_charge)` (line 2231):

```python
        # E2: surface the live export setpoint for observability only.
        # _status publishes 0.0/"passive" because export runs in the non-FORCING
        # branch with setpoint=0.0 — state is intentionally left untouched.
        # The recorder's smartcharge_state column (_record_sample, called above
        # at line 2176) already always records the plan state ("passive" during
        # export), so leaving last_status["state"] alone matches the recorded
        # history instead of diverging from it. A dedicated sensor reads the key.
        self.last_status["export_setpoint_w"] = _export_setpoint_w
```

In `sensor.py`, add the class after `X1SetpointSensor` (line 45):

```python
class X1ExportSetpointSensor(_Base):
    _attr_native_unit_of_measurement = "W"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, c, e):
        super().__init__(c, e, "export_setpoint_w", "SmartGrid export setpoint")
```

Register it in `async_setup_entry` after `X1SetpointSensor(...)` (line 204):

```python
            X1ExportSetpointSensor(controller, entry.entry_id),
```

- [ ] **Step 4: Run tests to verify pass**

Run: `python -m pytest tests/test_controller_export_executor.py tests/test_sensor.py -x -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add custom_components/anker_x1_smartgrid/controller.py custom_components/anker_x1_smartgrid/sensor.py tests/test_controller_export_executor.py tests/test_sensor.py
git commit -m "feat(sensor): surface live export setpoint (sensor-only observability)"
```

---

### Task 7: E4 — hour-gate the weather-forecast fetch

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/controller.py` (`__init__` ~1171; `_tick_impl` 1496)
- Test: `tests/test_controller.py`
- Depends on: Tasks 1, 3, 6 (shared `controller.py`).

**Interfaces:**
- Produces: `self._last_weather_hour: int` and `self._weather_forecast: list[dict]` cache; weather fetched at most once per clock-hour.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_weather_forecast_fetched_once_per_hour(monkeypatch):
    hass = _StubHass()
    ctrl, act = _make_controller(hass)
    monkeypatch.setattr(controller.dt_util, "utcnow", lambda: BASE)
    calls = {"n": 0}
    async def _fake(hass_, data_):
        calls["n"] += 1
        return []
    monkeypatch.setattr(controller.coordinator, "read_hourly_weather_forecast", _fake)
    await ctrl.tick()
    await ctrl.tick()
    assert calls["n"] == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_controller.py::test_weather_forecast_fetched_once_per_hour -x -q`
Expected: FAIL — `calls["n"] == 2`.

- [ ] **Step 3: Write minimal implementation**

In `__init__` next to `self._last_rollup_hour = -1` (1171):

```python
        self._last_weather_hour = -1
        self._weather_forecast: list[dict] = []
```

Replace the fetch at line 1496:

```python
        _wf_list = await coordinator.read_hourly_weather_forecast(self._hass, self._data)
```

with:

```python
        # Hour-gate: the hourly forecast changes at most hourly, and an unbounded
        # await here (a hung weather integration) would otherwise wedge every 60 s
        # tick with the inverter parked. Fetch once per clock-hour; keep the last
        # good forecast if a refresh returns [] (transient failure).
        if now.hour != self._last_weather_hour:
            self._last_weather_hour = now.hour
            _fetched = await coordinator.read_hourly_weather_forecast(self._hass, self._data)
            if _fetched:
                self._weather_forecast = _fetched
        _wf_list = self._weather_forecast
```

- [ ] **Step 4: Run tests to verify pass**

Run: `python -m pytest tests/test_controller.py -x -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add custom_components/anker_x1_smartgrid/controller.py tests/test_controller.py
git commit -m "perf(executor): hour-gate weather forecast fetch"
```

---

## TIER 2 — Before dormant features activate

### Task 8: P1 — per-step discharge eta in the export leg (both DPs, routing + backtrack)

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/optimize.py` (export routing 802; own backtrack 930)
- Modify: `custom_components/anker_x1_smartgrid/regret.py` (oracle backtrack 686-689)
- Test: `tests/test_optimize_parity.py`

**Interfaces:**
- Consumes: `_eta_discharge_at(power_w, cfg, eta_curve)` (imported in both), `optimize_grid(..., eta_curve=...)`, `hindsight_optimal_grid(..., eta_curve=...)`, `efficiency.EfficiencyCurve`, `BinStat`, `bin_index`, `const.EFFICIENCY_DC_BIN_EDGES_W`.
- Behaviour: export DC→AC uses per-step eta wherever `eta_curve` is provided — in BOTH the routing objective AND the reconstructed schedule of BOTH DPs. `eta_curve=None` stays byte-identical (scalar `eta_d`).

**Two mirrors to update (both were scalar):** `optimize.py:802` (routing) AND `optimize.py:930` (`optimize`'s OWN backtrack) AND `regret.py:686-689` (oracle backtrack). Miss any and the reported export diverges from the routing objective and the bin-crossing parity test fails.

- [ ] **Step 1: Write the failing test** — add to `tests/test_optimize_parity.py` (import `EfficiencyCurve`, `BinStat`, `bin_index`, `_make_export_cfg`, `DayData`, `optimize_grid`, `hindsight_optimal_grid`):

```python
def _two_bin_discharge_curve(cfg, *, lo_eta, hi_eta, split_w):
    """EfficiencyCurve whose discharge eta is lo_eta below split_w, hi_eta at/above.
    Charge eta stays flat at cfg.eta_charge. Bin edges = const.EFFICIENCY_DC_BIN_EDGES_W
    = [400,800,1500,2500,4000] (6 bins)."""
    from custom_components.anker_x1_smartgrid.efficiency import EfficiencyCurve, BinStat
    from custom_components.anker_x1_smartgrid import const
    edges = const.EFFICIENCY_DC_BIN_EDGES_W
    los, his = [0.0] + edges, edges + [float("inf")]
    disc = [BinStat(lo, hi, "discharge", (lo_eta if lo < split_w else hi_eta),
                    None, 5, 1.0, True, "measured") for lo, hi in zip(los, his)]
    chg = [BinStat(lo, hi, "charge", cfg.eta_charge, None, 5, 1.0, True, "measured")
           for lo, hi in zip(los, his)]
    return EfficiencyCurve(chg, disc, cfg.eta_charge,
                           min(cfg.round_trip_eff / cfg.eta_charge, 1.0))


class TestExportEtaCurveParity:
    """P1: with a measured eta_curve, optimize_grid's export leg must route AND
    report on per-step eta so it stays == the oracle when the optimal export lands
    in a NON-top efficiency bin (different from max_export_w's bin)."""

    def test_export_leg_eta_curve_bin_crossing_parity(self):
        from custom_components.anker_x1_smartgrid.efficiency import bin_index
        cfg = _make_export_cfg()          # cap 10, floor 20% (2 kWh), max_export_w=3000
        curve = _two_bin_discharge_curve(cfg, lo_eta=0.98, hi_eta=0.80, split_w=1500.0)
        pv    = [0.0] * 24
        load  = [0.0] * 24
        price = [0.20] * 24
        export_price = [0.0] * 24
        export_price[18] = 0.55
        # soc_start=30% (3 kWh) with a 2 kWh floor ⇒ ~1 kWh exportable ⇒ ~1000 W
        # per hour ⇒ discharge bin 2; max_export_w=3000 ⇒ bin 4 ⇒ DIFFERENT bins.
        assert bin_index(1000.0) != bin_index(cfg.max_export_w)
        day = DayData(pv_kwh=tuple(pv), load_kwh=tuple(load), price=tuple(price), soc_start=30.0)
        hind = hindsight_optimal_grid(day, cfg, export_price=export_price,
                                      terminal_mode="water_value", water_value=0.0, eta_curve=curve)
        opt = optimize_grid(pv, load, price, soc_start=30.0, cfg=cfg,
                            window_start_h=0, window_len=24, export_price=export_price,
                            terminal_mode="water_value", water_value=0.0, eta_curve=curve)
        assert opt["export_schedule"][18] > 0.0      # export actually fires (non-vacuous)
        assert_export_parity(opt, hind, label="export_eta_curve_bin_crossing")
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_optimize_parity.py::TestExportEtaCurveParity -x -q`
Expected: FAIL on `export_schedule`/`export_kwh` — `optimize` routes+reports at the flat max-rate eta (0.80) while the oracle routes per-step (0.98).

- [ ] **Step 3: Write minimal implementation**

`optimize.py` export routing — replace line 802 `e_ac = e_dc * eta_d`:

```python
                        eta_d_step = (
                            eta_d if eta_curve is None
                            else _eta_discharge_at(e_dc / dt_h * 1000.0, cfg, eta_curve)
                        )
                        e_ac = e_dc * eta_d_step
```

(Keep the scalar `eta_d` for the AC-cap sizing at 791 — mirrors regret.py:550.)

`optimize.py` own backtrack — replace line 930 `export_schedule_ac = [e_dc * eta_d for e_dc in export_dc_sched]`:

```python
        export_schedule_ac = [
            e_dc * (
                eta_d if eta_curve is None
                else _eta_discharge_at(e_dc / dt_h * 1000.0, cfg, eta_curve)
            )
            for e_dc in export_dc_sched
        ]
```

`regret.py` oracle backtrack — replace lines 686-689:

```python
        export_schedule_ac: list[float] = [
            e_dc * (
                eta_d if eta_curve is None
                else _eta_discharge_at(e_dc / dt_h * 1000.0, cfg, eta_curve)
            )
            for e_dc in export_dc_sched
        ]
```

- [ ] **Step 4: Run tests to verify pass**

Run: `python -m pytest tests/test_optimize_parity.py -x -q`
Expected: PASS (bin-crossing case + all existing scalar-eta parity cases byte-identical).

- [ ] **Step 5: Commit**

```bash
git add custom_components/anker_x1_smartgrid/optimize.py custom_components/anker_x1_smartgrid/regret.py tests/test_optimize_parity.py
git commit -m "fix(optimize): per-step discharge eta in export routing and backtrack for eta-curve parity"
```

---

### Task 9: H3a — periodic WAL checkpoint for read-only addon access  *(VENDORED — chain #3)*

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/recorder.py` (add `wal_checkpoint`)
- Modify: `custom_components/anker_x1_smartgrid/controller.py` (`__init__` ~1171; `_tick_impl` hourly block ~1515)
- Test: `tests/test_recorder.py`
- Ends with: `sync_core.sh` regen + vendor-parity green. **BlockedBy: Task 5 (vendored chain); apply after Task 7 (shared `controller.py`).** See Resolved decisions (c).

**Interfaces:**
- Produces: `DataRecorder.wal_checkpoint() -> None` — runs `PRAGMA wal_checkpoint(TRUNCATE)` under `self._lock`, isolation-wrapped. Called once per clock-hour from the controller so an `immutable=1` reader (Task 10) sees checkpointed data.

**Logging note:** `recorder.py` has NO logging import and NO `_LOGGER`/`_log` — it swallows silently (e.g. `_energy_deltas` at :463 uses bare `except: return ...`). Do NOT add a logger to a vendored module; match the existing convention with `except Exception: pass`.

- [ ] **Step 1: Write the failing test** — add to `tests/test_recorder.py`:

```python
def test_wal_checkpoint_makes_rows_visible_to_immutable_reader(tmp_path):
    import sqlite3
    path = str(tmp_path / "t.db")
    rec = DataRecorder(path)
    rec.append({"ts": "2026-07-08T09:00:00+00:00", "p1_w": 100.0, "batt_w": 0.0,
                "pv_w": 0.0, "load_w": 100.0})
    rec.wal_checkpoint()
    ro = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    n = ro.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
    ro.close()
    rec.close()
    assert n == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_recorder.py::test_wal_checkpoint_makes_rows_visible_to_immutable_reader -x -q`
Expected: FAIL — `AttributeError: 'DataRecorder' has no attribute 'wal_checkpoint'`.

- [ ] **Step 3: Write minimal implementation**

In `recorder.py`, add next to `close` (before line 856):

```python
    def wal_checkpoint(self) -> None:
        """Truncate the WAL into the main DB file so a read-only immutable reader
        (the addon, mounted config:ro) can see recent rows. Failure-isolated: a
        busy checkpoint (SQLITE_BUSY) must never abort the tick — isolation is the
        contract here (matches _energy_deltas)."""
        try:
            with self._lock:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:  # noqa: BLE001 — isolation is the contract; never raise into the tick
            pass
```

In `controller.py` `__init__` (1171) add `self._last_wal_checkpoint_hour = -1`. In `_tick_impl`, inside the existing hourly-rollup block (after line 1522, still guarded by `self._recorder is not None`), add:

```python
        if self._recorder is not None and now.hour != self._last_wal_checkpoint_hour:
            self._last_wal_checkpoint_hour = now.hour
            await self._hass.async_add_executor_job(self._recorder.wal_checkpoint)
```

- [ ] **Step 4: Run tests to verify pass**

Run: `python -m pytest tests/test_recorder.py tests/test_controller.py -x -q`
Expected: PASS.

- [ ] **Step 5: Re-vendor + parity gate**

Run: `bash addon/anker_x1_forecast/sync_core.sh && python -m pytest tests_addon/test_vendor_parity.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add custom_components/anker_x1_smartgrid/recorder.py custom_components/anker_x1_smartgrid/controller.py addon/anker_x1_forecast/forecast_core/recorder.py addon/anker_x1_forecast/forecast_core/SOURCE_SHA256 tests/test_recorder.py
git commit -m "feat(recorder): periodic WAL checkpoint for read-only addon access"
```

---

### Task 10: H3b — addon opens DB immutable + `db_readable` in `/health`

**Files:**
- Modify: `addon/anker_x1_forecast/trainer.py` (`load_rows` URI 171)
- Modify: `addon/anker_x1_forecast/health.py` (`build_health_payload` 54-78)
- Modify: `addon/anker_x1_forecast/server.py` (`/health` 100)
- Test: `tests_addon/test_trainer_dataread.py`, `tests_addon/test_health_state.py`
- **BlockedBy: Task 9** — its trainer test imports the VENDORED `forecast_core.recorder.wal_checkpoint`, which only exists after Task 9's `sync_core.sh` run.

**Interfaces:**
- Produces: `load_rows` opens `file:{db_path}?mode=ro&immutable=1`. `build_health_payload(state, sklearn_version, python_version, db_readable=None)` adds a `db_readable: bool | None` key.

- [ ] **Step 1: Write the failing tests**

```python
# tests_addon/test_trainer_dataread.py
def test_load_rows_reads_wal_db_via_immutable(tmp_path):
    from forecast_core.recorder import DataRecorder
    from trainer import load_rows
    path = str(tmp_path / "t.db")
    rec = DataRecorder(path)
    for h in range(30):
        rec.append({"ts": f"2026-06-{h % 28 + 1:02d}T10:00:00+00:00",
                    "p1_w": 100.0, "batt_w": 0.0, "pv_w": 0.0, "load_w": 100.0})
    rec.rollup_hours("2026-07-08T00:00:00+00:00")
    rec.wal_checkpoint()
    rec.close()
    rows = load_rows(path)
    assert rows is not None and len(rows) >= 24
```

```python
# tests_addon/test_health_state.py
def test_health_payload_includes_db_readable():
    from types import SimpleNamespace
    from health import build_health_payload
    state = SimpleNamespace(ready=False, promoted=False, last_trained=None,
                            n_rows=0, metrics=None)
    payload = build_health_payload(state, "1.5.2", "3.12", db_readable=False)
    assert payload["db_readable"] is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests_addon/test_trainer_dataread.py::test_load_rows_reads_wal_db_via_immutable tests_addon/test_health_state.py::test_health_payload_includes_db_readable -x -q`
Expected: FAIL — `build_health_payload` has no `db_readable`; trainer test pins the immutable contract.

- [ ] **Step 3: Write minimal implementation**

`trainer.py` line 171:

```python
        conn = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
```

Update the surrounding comment: `immutable=1` reads a consistent snapshot as of the last integration `wal_checkpoint(TRUNCATE)` (accepts checkpoint lag; no `-shm` write on the config:ro mount).

`health.py` `build_health_payload`:

```python
def build_health_payload(
    state: "TrainState",
    sklearn_version: str,
    python_version: str,
    db_readable: bool | None = None,
) -> dict:
    ...
    return {
        "ready": state.ready,
        "promoted": state.promoted,
        "last_trained": last_trained.isoformat() if last_trained is not None else None,
        "n_rows": state.n_rows,
        "metrics": state.metrics if state.metrics is not None else {},
        "sklearn_version": sklearn_version,
        "python_version": python_version,
        "db_readable": db_readable,
    }
```

`server.py` — add `from pathlib import Path` to imports and pass a readability probe in `/health` (line 100):

```python
    db_ok = bool(_DB_PATH) and Path(_DB_PATH).exists()
    return build_health_payload(STATE, sklearn.__version__, sys.version, db_readable=db_ok)
```

- [ ] **Step 4: Run tests to verify pass**

Run: `python -m pytest tests_addon/ -x -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add addon/anker_x1_forecast/trainer.py addon/anker_x1_forecast/health.py addon/anker_x1_forecast/server.py tests_addon/test_trainer_dataread.py tests_addon/test_health_state.py
git commit -m "fix(addon): open recorder DB immutable and report db_readable in health"
```

---

### Task 11: T3 — extend DP↔oracle parity sweep across live cfg permutations

**Files:**
- Modify: `tests/test_optimize_parity.py`

**Interfaces:**
- Consumes: `_make_export_cfg(**overrides)` (line 586), `assert_export_parity`, `DayData`, `optimize_grid`, `hindsight_optimal_grid`.

**Non-vacuity:** the sweep MUST use `_make_export_cfg` (has `max_export_w`/`cycle_cost`) — a `make_cfg`-based export scenario yields an empty export schedule so parity passes trivially. Assert the oracle actually exports.

- [ ] **Step 1: Write the test**

```python
@pytest.mark.parametrize("overrides,dt_h", [
    ({"reserve_anchor": "ride_to_trough"}, 1.0),   # live default anchor (DP-invariant)
    ({"export_peak_lookback_h": 4.0}, 1.0),         # windowed peak w/ day_index
    ({"soc_hedge_fraction": 0.5}, 1.0),             # controller-level; DP-invariant
    ({}, 0.25),                                      # 15-min slot resolution
])
def test_parity_holds_across_live_cfg_permutations(overrides, dt_h):
    cfg = _make_export_cfg(**overrides)
    pv    = [0.0] * 24
    load  = [0.0] * 24
    price = [0.20] * 24
    export_price = [0.0] * 24
    export_price[18] = 0.55
    day = DayData(pv_kwh=tuple(pv), load_kwh=tuple(load), price=tuple(price), soc_start=80.0)
    hind = hindsight_optimal_grid(day, cfg, export_price=export_price,
                                  terminal_mode="water_value", water_value=0.0, dt_h=dt_h)
    opt = optimize_grid(pv, load, price, soc_start=80.0, cfg=cfg,
                        window_start_h=0, window_len=24, export_price=export_price,
                        terminal_mode="water_value", water_value=0.0, dt_h=dt_h)
    assert sum(hind["export_schedule"]) > 0.0     # sweep is non-vacuous
    assert_export_parity(opt, hind, label=f"parity[{overrides},dt={dt_h}]")
```

(Module note: `reserve_anchor`/`soc_hedge_fraction` are controller-level and do not enter `optimize_grid`; swept to pin invariance. `export_peak_lookback_h` and `dt_h=0.25` are the substantive cases.)

- [ ] **Step 2: Run**

Run: `python -m pytest tests/test_optimize_parity.py::test_parity_holds_across_live_cfg_permutations -x -q`
Expected: PASS confirming coverage; if any permutation diverges, FAIL surfaces a real regression — reconcile the mismatched leg (mirror the peak/export helpers) before continuing.

- [ ] **Step 3: Commit**

```bash
git add tests/test_optimize_parity.py
git commit -m "test(optimize): sweep DP-oracle parity across live cfg permutations"
```

---

### Task 12: T2 — end-to-end per-day trough band charge admission

**Files:**
- Modify: `tests/test_optimize_peak_band.py`

**Interfaces:**
- Consumes: `regret.windowed_trough_prices(prices, lookback, day_index=...)`, `optimize.build_charge_mask(price, ceiling, price_band=..., trough=...)`, `optimize.optimize_grid(..., chargeable=..., day_index=...)`.

- [ ] **Step 1: Write the test** (mirror `test_two_day_horizon_exports_both_daily_peaks`, but for the TROUGH band)

```python
from custom_components.anker_x1_smartgrid.regret import windowed_trough_prices
from custom_components.anker_x1_smartgrid.optimize import build_charge_mask

def test_two_day_trough_band_allows_day1_charge_where_global_blocks():
    """48h: a cheaper day-2 trough must NOT block day-1's own cheap charge hour.
    Per-day trough band admits day-1 charging; a GLOBAL (day_index=None) band judges
    day-1 vs day-2's lower trough and blocks it."""
    cfg = _cfg(charge_window_price_band=0.02)
    n = 48
    pv = [0.0] * n
    load = [1.0] * n
    price = [0.30] * n
    price[6] = 0.15
    price[30] = 0.10
    ceiling = [max(price)] * n
    day = [h // 24 for h in range(n)]
    trough_perday = windowed_trough_prices(price, 0, day_index=day)
    trough_global = windowed_trough_prices(price, 0, day_index=None)
    mask_perday = build_charge_mask(price, ceiling, price_band=cfg.charge_window_price_band, trough=trough_perday)
    mask_global = build_charge_mask(price, ceiling, price_band=cfg.charge_window_price_band, trough=trough_global)
    assert mask_perday[6] is True
    assert mask_global[6] is False
    res = optimize_grid(pv, load, price, soc_start=20.0, cfg=cfg,
                        window_start_h=0, window_len=n, slots_per_day=24,
                        day_index=day, chargeable=mask_perday)
    assert sum(res["schedule"][:24]) > 0.0
```

(Reuse the file's `_cfg` at line 9 with a `charge_window_price_band` override.)

- [ ] **Step 2: Run**

Run: `python -m pytest tests/test_optimize_peak_band.py::test_two_day_trough_band_allows_day1_charge_where_global_blocks -x -q`
Expected: PASS on correct code; a FAIL means a real per-day slicing bug — fix `build_charge_mask`/`windowed_trough_prices` first.

- [ ] **Step 3: Commit**

```bash
git add tests/test_optimize_peak_band.py
git commit -m "test(optimize): end-to-end per-day trough band charge admission"
```

---

## TIER 3 — CI / tests / cleanup

### Task 13: A1 — pin CI deps + add Python 3.12 matrix

**Files:**
- Modify: `.github/workflows/tests.yml` (matrix 14; addon dep install 34)
- Modify: `requirements_test.txt` (holidays pin)
- Test: `tests_addon/test_requirements_pinned.py` (new)

- [ ] **Step 1: Write the failing test**

```python
def test_addon_requirements_pinned_exact():
    from pathlib import Path
    req = (Path(__file__).resolve().parent.parent
           / "addon" / "anker_x1_forecast" / "requirements.txt").read_text()
    assert "scikit-learn==1.5.2" in req
    assert "holidays==0.99" in req
```

- [ ] **Step 2: Run**

Run: `python -m pytest tests_addon/test_requirements_pinned.py -x -q`
Expected: PASS (requirements.txt already pins these; the test pins the invariant CI must match).

- [ ] **Step 3: Edit `tests.yml`**

matrix (line 14):

```yaml
        python-version: ["3.12", "3.13"]
```

addon dep install (line 34):

```yaml
      - name: Install add-on test dependencies
        run: pip install scikit-learn==1.5.2 holidays==0.99 fastapi==0.115.6 uvicorn==0.34.0 pydantic
```

`requirements_test.txt` — pin holidays to prod:

```text
holidays==0.99
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests_addon/ -x -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/tests.yml requirements_test.txt tests_addon/test_requirements_pinned.py
git commit -m "ci: pin scikit-learn/holidays and add python 3.12 to matrix"
```

---

### Task 14: A2 — run FastAPI schema tests + TestClient smoke

**Files:**
- Modify: `tests_addon/test_server.py` (add TestClient smoke; keep importorskip for the pure-schema tests)

**Interfaces:**
- Consumes: `server.app` (FastAPI), `HourIn`, `PredictRequest`, `predictor.build_predict_payload` (returns key `predictions`, verified predictor.py:176).

- [ ] **Step 1: Write the failing test** — append to `tests_addon/test_server.py`:

```python
def test_health_and_predict_smoke():
    """Exercise the real Pydantic request schema via TestClient (not importorskip'd
    away): /health returns ready flag; /predict validates the hours schema."""
    from fastapi.testclient import TestClient
    from server import app
    with TestClient(app) as client:
        h = client.get("/health")
        assert h.status_code == 200 and "ready" in h.json()
        ok = client.post("/predict", json={"hours": [{"ts": "2026-07-08T10:00:00+00:00"}]})
        assert ok.status_code == 200 and "predictions" in ok.json()
        bad = client.post("/predict", json={"hours": "notalist"})
        assert bad.status_code == 422
```

- [ ] **Step 2: Run**

Run: `python -m pytest tests_addon/test_server.py::test_health_and_predict_smoke -x -q`
Expected: PASS where fastapi is installed (Task 13 installs it in CI so the module's `importorskip` no longer silently skips; locally `pip install fastapi uvicorn` first if needed).

- [ ] **Step 3: Run tests**

Run: `python -m pytest tests_addon/test_server.py -x -q`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add tests_addon/test_server.py
git commit -m "test(addon): add TestClient smoke for /health and /predict schema"
```

---

### Task 15: A3 — sync addon config.yaml version via semantic-release

**Files:**
- Modify: `pyproject.toml` (`[tool.semantic_release]` version_variables 99-101)
- Modify: `addon/anker_x1_forecast/config.yaml` (version 2)
- Test: `tests_addon/test_addon_version_sync.py` (new)

- [ ] **Step 1: Write the failing test**

```python
def test_addon_version_matches_integration():
    import json, re
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    proj_ver = re.search(r'^version = "([^"]+)"', (root / "pyproject.toml").read_text(), re.M).group(1)
    manifest = json.loads((root / "custom_components" / "anker_x1_smartgrid" / "manifest.json").read_text())
    addon_ver = re.search(r'^version:\s*"([^"]+)"',
                          (root / "addon" / "anker_x1_forecast" / "config.yaml").read_text(), re.M).group(1)
    assert manifest["version"] == proj_ver == addon_ver
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests_addon/test_addon_version_sync.py -x -q`
Expected: FAIL — addon config.yaml is `0.1.0`, project/manifest `0.2.0`.

- [ ] **Step 3: Edit** — bump config.yaml and add it to `version_variables`.

`addon/anker_x1_forecast/config.yaml` line 2:

```yaml
version: "0.2.0"
```

`pyproject.toml` version_variables (99-101):

```toml
version_variables = [
    "custom_components/anker_x1_smartgrid/manifest.json:version",
    "addon/anker_x1_forecast/config.yaml:version",
]
```

- [ ] **Step 4: Run**

Run: `python -m pytest tests_addon/test_addon_version_sync.py -x -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml addon/anker_x1_forecast/config.yaml tests_addon/test_addon_version_sync.py
git commit -m "chore(release): sync addon config.yaml version via semantic-release"
```

---

### Task 16: A4 — surface silent bucketed-model fallback

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/controller.py` (`__init__` ~1171; one-shot check in `_tick_impl` ~1522)
- Test: `tests/test_controller.py`
- Depends on: Tasks 1,3,6,7,9 (shared `controller.py`). See Resolved decisions (b).

**Interfaces:**
- Produces: `self._learned_model_warned: bool`. When `cfg.use_learned_model` AND NOT `cfg.addon_enabled` AND sklearn unavailable → ONE-TIME WARNING log.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_learned_model_unavailable_warns_once(monkeypatch, caplog):
    from dataclasses import replace
    import importlib
    hass = _StubHass()
    ctrl, act = _make_controller(hass)
    ctrl.cfg = replace(ctrl.cfg, use_learned_model=True, addon_enabled=False)
    monkeypatch.setattr(controller.importlib.util, "find_spec", lambda name: None)
    with caplog.at_level("WARNING"):
        await ctrl.tick()
        await ctrl.tick()
    hits = [r for r in caplog.records if "learned model" in r.getMessage().lower()]
    assert len(hits) == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_controller.py::test_learned_model_unavailable_warns_once -x -q`
Expected: FAIL — no warning emitted.

- [ ] **Step 3: Write minimal implementation**

Add `import importlib.util` at the top of controller.py if absent. In `__init__` (1171) add `self._learned_model_warned = False`. In `_tick_impl`, after the rollup/weather setup (~1522), add:

```python
        # A4: DEFAULT_USE_LEARNED_MODEL is True, but sklearn is NOT an integration
        # requirement (musl is why the addon exists) and the addon defaults off — a
        # stock install then silently falls back to the bucketed model forever.
        if (
            not self._learned_model_warned
            and self.cfg.use_learned_model
            and not self.cfg.addon_enabled
            and importlib.util.find_spec("sklearn") is None
        ):
            self._learned_model_warned = True
            _LOGGER.warning(
                "use_learned_model is on but scikit-learn is unavailable in the "
                "integration and the forecast add-on is disabled — falling back to "
                "the bucketed load model. Enable the Anker X1 Forecast add-on for "
                "the learned HGBR model."
            )
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_controller.py -x -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add custom_components/anker_x1_smartgrid/controller.py tests/test_controller.py
git commit -m "feat: warn when learned model configured but sklearn and add-on unavailable"
```

---

### Task 17: T1 — fix tautological trough-lookback fallback test

**Files:**
- Modify: `tests/test_charge_trough_lookback_heuristic.py`

- [ ] **Step 1: Rewrite the test** — heuristic charge-slot selection was deleted, so a DP exception → `selected=[]` → PASSIVE regardless of `charge_trough_lookback_h`; the docstring claim is false. Replace the module docstring and both tests:

```python
"""On a DP exception the heuristic fallback selects no charge slots → PASSIVE,
independent of charge_trough_lookback_h (heuristic charge selection was removed in
the P80-survival cleanup). Pins the DP-exception → PASSIVE contract, not a look-back
behaviour."""
...
def test_dp_exception_falls_back_to_passive():
    for lookback in (0, 8):
        new_plan = _run(_cfg(charge_trough_lookback_h=lookback), lookback)
        assert new_plan.state is ControllerState.PASSIVE
```

(Delete `test_lookback_blocks_evening_topup_on_fallback` and `test_lookback_zero_fallback_is_passive`.)

- [ ] **Step 2: Run**

Run: `python -m pytest tests/test_charge_trough_lookback_heuristic.py -x -q`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_charge_trough_lookback_heuristic.py
git commit -m "test(optimize): fix tautological trough-lookback fallback test"
```

---

### Task 18: LOW — trough-mask fail-closed on padded price hours (F2)

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/optimize.py` (`build_charge_mask` 263-355)
- Modify: `custom_components/anker_x1_smartgrid/controller.py` (compute_decision mask call ~243)
- Test: `tests/test_optimize_charge_mask.py` (or existing charge-mask test)
- Depends on: Task 8 (shared `optimize.py`), 1/3/6/7/9/16 (shared `controller.py`).

**Interfaces:**
- Produces: `build_charge_mask(price, ceiling, price_band=None, window_min=None, trough=None, price_valid=None)`. `price_valid[h] is False` → that hour fails closed in EVERY path. `None` (default) → all valid (byte parity). **Keep the existing docstring (270-355) and all existing type annotations verbatim** — only add the `price_valid` param, one docstring entry for it, and AND it into the returns.

- [ ] **Step 1: Write the failing test**

```python
def test_trough_mask_fails_closed_on_padded_hour():
    from custom_components.anker_x1_smartgrid.optimize import build_charge_mask
    price   = [0.10, 0.00, 0.12]      # index 1 is a 0.0 pad
    trough  = [0.10, 0.10, 0.12]
    ceiling = 0.30
    m = build_charge_mask(price, ceiling, price_band=0.02, trough=trough, price_valid=[True, False, True])
    assert m[1] is False
    m2 = build_charge_mask(price, ceiling, price_band=0.02, trough=trough)   # default → all valid
    assert m2[1] is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_optimize_charge_mask.py::test_trough_mask_fails_closed_on_padded_hour -x -q`
Expected: FAIL — no `price_valid` param.

- [ ] **Step 3: Write minimal implementation**

Extend the signature (retaining existing annotations) — add `price_valid` as the final param:

```python
def build_charge_mask(
    price: list[float],
    ceiling: float | None,
    price_band: float | None = None,
    window_min: float | None = None,
    trough: Sequence[float | None] | None = None,
    price_valid: Sequence[bool] | None = None,
) -> list[bool]:
```

Add ONE docstring entry after the `trough :` block (keep everything else verbatim):

```python
    price_valid :
        Optional per-hour validity mask (length == ``len(price)``). ``False`` at
        an index fails that hour CLOSED in every path — used to reject 0.0-padded
        phantom-price hours that would otherwise satisfy the trough band. ``None``
        (default) treats every hour as valid (byte parity).
```

AND it into every return (the `if ceiling is None` / empty-price returns stay as-is):

```python
    valid = price_valid if price_valid is not None else [True] * len(price)
    if trough is not None:
        band = price_band if price_band is not None else 0.0
        return [
            (v and t is not None and p <= ceiling and p <= t + band)
            for p, t, v in zip(price, trough, valid)
        ]
    if price_band is not None:
        trough_v = window_min if window_min is not None else min(price)
        trough_threshold = trough_v + price_band
        return [v and p <= ceiling and p <= trough_threshold for p, v in zip(price, valid)]
    return [v and p <= ceiling for p, v in zip(price, valid)]
```

In `controller.py` compute_decision (line 243), pass real-price hours:

```python
    _price_valid = [(now_h + h * stride) in price_by_h for h in range(window_len)]
    chargeable = optimize_mod.build_charge_mask(
        window_price, ceiling,
        price_band=cfg.charge_window_price_band,
        trough=_trough_list,
        price_valid=_price_valid,
    )
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_optimize_charge_mask.py tests/test_optimize_parity.py -x -q`
Expected: PASS (default `price_valid=None` keeps parity).

- [ ] **Step 5: Commit**

```bash
git add custom_components/anker_x1_smartgrid/optimize.py custom_components/anker_x1_smartgrid/controller.py tests/test_optimize_charge_mask.py
git commit -m "fix(optimize): fail closed on padded-price hours in trough charge mask"
```

---

### Task 19: LOW — gross-import regret price + zero-eta display guard

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/regret.py` (`score_regret` 954)
- Modify: `custom_components/anker_x1_smartgrid/plan.py` (SoC sim 201-205)
- Test: `tests/test_regret.py`, `tests/test_plan.py`
- Depends on: Task 8 (shared `regret.py`).

**Interfaces:**
- Produces: `score_regret` over-buy decomposition prices at GROSS import cost (`eur + export_revenue_eur`), not the net eur (which goes negative on export-profitable days). `realized_grid_cost` returns keys `kwh`, `eur`, `export_revenue_eur` (regret.py:898-905).
- Produces: display SoC sim guards `_eta_d`/`_eta_de` denominators with `max(_, 1e-6)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_regret.py
def test_over_buy_eur_uses_gross_import_price_when_export_profitable():
    from custom_components.anker_x1_smartgrid.regret import score_regret
    # eur is NET (gross import − export revenue): gross 10 kWh @0.20 = 2.0, export 3.0 → -1.0
    realized = {"kwh": 10.0, "eur": -1.0, "export_revenue_eur": 3.0}
    optimal  = {"kwh": 6.0, "eur": -2.0, "export_revenue_eur": 3.0}
    out = score_regret(realized, optimal)
    assert out["over_buy_eur"] == pytest.approx(0.80, abs=1e-6)   # 4 kWh × 0.20 gross
```

```python
# tests/test_plan.py
def test_build_plan_horizon_no_zerodiv_at_zero_round_trip_eff():
    from datetime import datetime, timezone
    from custom_components.anker_x1_smartgrid.plan import build_plan_horizon
    from custom_components.anker_x1_smartgrid.models import Config, PriceSlot
    cfg = Config.from_dict({
        "capacity_kwh": 10.0, "soc_floor": 10.0, "soc_target": 90.0,
        "max_charge_w": 3000.0, "eta_charge": 1.0, "round_trip_eff": 0.0,
    })
    t = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
    out = build_plan_horizon([PriceSlot(t, 0.20)], [], [], 80.0, t, cfg,
                             export_request_by_hour={t: 500.0})
    assert out   # completes without ZeroDivisionError
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_regret.py::test_over_buy_eur_uses_gross_import_price_when_export_profitable tests/test_plan.py::test_build_plan_horizon_no_zerodiv_at_zero_round_trip_eff -x -q`
Expected: FAIL — over_buy priced by negative net avg; ZeroDivisionError in plan export drain.

- [ ] **Step 3: Write minimal implementation**

In `regret.py` `score_regret`, replace lines 954-955:

```python
    gross_import_eur = r_eur + realized.get("export_revenue_eur", 0.0)
    avg_realized_price = gross_import_eur / r_kwh if r_kwh > 1e-9 else 0.0
    over_buy_eur = over_buy_kwh * avg_realized_price
```

In `plan.py`, guard the denominators (201-205):

```python
            _eta_d = eta_discharge if eta_curve is None else eta_curve.eta_discharge(self_discharge_w)
            soc_sim -= (self_discharge_w / max(_eta_d, 1e-6)) * dt_h / cap_wh * 100.0
            _eta_de = eta_discharge if eta_curve is None else eta_curve.eta_discharge(grid_export_w)
            soc_sim -= (grid_export_w / max(_eta_de, 1e-6)) * dt_h / cap_wh * 100.0
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_regret.py tests/test_plan.py -x -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add custom_components/anker_x1_smartgrid/regret.py custom_components/anker_x1_smartgrid/plan.py tests/test_regret.py tests/test_plan.py
git commit -m "fix: guard zero-eta display sim and use gross import price for over-buy"
```

---

### Task 20: LOW — config-flow export limit range + vendor-parity glob (BOTH gates)

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/config_flow.py` (grid_export_limit_w 299-302)
- Modify: `tests/test_vendored_parity.py` (MODULES from glob — the CI-run gate)
- Modify: `tests_addon/test_vendor_parity.py` (MODULES from glob — the addon gate)
- Test: `tests/test_config_flow.py`

**Interfaces:**
- Produces: `grid_export_limit_w` validated `vol.Range(min=0)`; BOTH parity gates derive `MODULES` from the vendored dir glob (a 9th vendored module can't escape either gate). `tests/test_vendored_parity.py` runs in `pytest tests/` (CI); `tests_addon/test_vendor_parity.py` runs in `pytest tests_addon/`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_config_flow.py
def test_grid_export_limit_rejects_negative():
    import voluptuous as vol
    from custom_components.anker_x1_smartgrid import config_flow, const
    schema = config_flow.<options-schema-builder>(defaults={})   # anchor on the real builder in-file
    with pytest.raises(vol.Invalid):
        schema({const.CONF_GRID_EXPORT_LIMIT_W: -100.0})
```

```python
# tests/test_vendored_parity.py  AND  tests_addon/test_vendor_parity.py
def test_modules_list_covers_every_vendored_py():
    discovered = sorted(p.stem for p in _VENDOR_DIR.glob("*.py") if p.stem != "__init__")
    assert set(MODULES) == set(discovered)
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_config_flow.py::test_grid_export_limit_rejects_negative tests/test_vendored_parity.py::test_modules_list_covers_every_vendored_py tests_addon/test_vendor_parity.py::test_modules_list_covers_every_vendored_py -x -q`
Expected: FAIL — negative accepted; MODULES is a hardcoded literal in both files.

- [ ] **Step 3: Write minimal implementation**

`config_flow.py` line 302 (and the user-step schema if it also defines this key):

```python
            ): vol.All(vol.Coerce(float), vol.Range(min=0)),
```

`tests/test_vendored_parity.py` (line 17) AND `tests_addon/test_vendor_parity.py` (line 17) — replace the hardcoded `MODULES` list with a glob (each file already defines its own `_VENDOR_DIR`):

```python
MODULES = sorted(p.stem for p in _VENDOR_DIR.glob("*.py") if p.stem != "__init__")
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_config_flow.py tests/test_vendored_parity.py tests_addon/test_vendor_parity.py -x -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add custom_components/anker_x1_smartgrid/config_flow.py tests/test_vendored_parity.py tests_addon/test_vendor_parity.py tests/test_config_flow.py
git commit -m "fix(config-flow): reject negative grid_export_limit_w; glob both vendor-parity gates"
```

---

## Final gate (before merge)

- [ ] Full suite green: `python -m pytest tests/ tests_addon/ -q`
- [ ] Vendor parity green in BOTH gates: `python -m pytest tests/test_vendored_parity.py tests_addon/test_vendor_parity.py -q`
- [ ] `code-review` agent on the whole `fix/review-fixes-2026-07-08` diff (major change gate).
- [ ] `superpowers:finishing-a-development-branch`. Deploy to HAOS is a SEPARATE post-merge step.

---

## Resolved decisions (2026-07-08)

User approved the recommended defaults; locked in for execution.

**(a) E2 export observability — SENSOR-ONLY.** `export_setpoint_w` in `last_status` + new `X1ExportSetpointSensor` (Task 6). NO new `"exporting"` state string. `last_status["state"]` stays `"passive"` during export — correct, matches the recorder's `smartcharge_state` column (`_record_sample` runs before `_status` and always records the plan state). Avoids both automation-string breakage and a record/live divergence.

**(b) A4 learned-model-unavailable surfacing — ONE-TIME LOG only** (Task 16 as planned). No HA repair issue — the integration has no existing `ir.async_create_issue` usage, so that would add new translation-string infra for no clear benefit here.

**(c) H3 approach — `wal_checkpoint(TRUNCATE)` + `immutable=1`** (Tasks 9/10 as planned), not HTTP row-export. MANDATORY: validate on-box (immutable-mode read under a live writer) before promoting the addon out of dormancy — the unit test cannot fully reproduce this.

**(d) M1 weather/persons serve-threading — stays DEFERRED.** Separate change (touches vendored `featureset.py`/`forecast.py`); does not gate this branch.

**(e) Tier-3 scope — KEEP ALL** (Tasks 11, 12, 20 included). Cheap; closes the exact gaps flagged.

**Execution mode:** subagent-driven (`superpowers:subagent-driven-development`).

---

### Self-review (writing-plans checklist)

- **Spec coverage:** H1(T1), H2(T2), E1(T3), D1(T4), D2(T5), E2(T6), E4(T7), P1(T8), H3(T9+T10), T3-parity(T11), T2-trough(T12), A1(T13), A2(T14), A3(T15), A4(T16), T1(T17), F2(T18), F3+plan-eta(T19), config-Range+vendor-glob(T20). recorder NULL-ts purge folded into T4. F4 moved to Non-goals with rationale. ✅
- **Placeholder scan:** No "TBD"/"similar to". T18/T20 reference an in-file builder the engineer anchors on (`build_charge_mask` docstring kept verbatim; config-flow options-schema builder). T8's `_two_bin_discharge_curve` is fully spelled out against efficiency.py (BinStat fields, bin edges). ✅
- **Type consistency:** `_read_pv_watts`/`build_pv_curve_from_watts` per-source `list[list[tuple]]` (T2); `_eta_discharge_at(power_w, cfg, eta_curve)` identical across optimize.py:802/930 + regret.py:686 (T8); `build_charge_mask(..., price_valid=None)` identical in T18; `wal_checkpoint()`/`db_readable` consistent across T9/T10; `MODULES` glob identical in both parity gates (T20). ✅
- **Amendment audit (from adversarial review):** T1 persist + 2nd restart test ✅; T3 async-converts the 3 broken tests + comment fix ✅; T8 adds optimize.py:930 + concrete curve + bin assertion ✅; T9 `except: pass` (no logger in vendored) ✅; T10 blockedBy T9 ✅; F4 removed→non-goals ✅; T11 export cfg + non-vacuity assert ✅; T4 future-hourly-row cleanup + reason why append-side is impossible ✅; T5 VERIFY widened to ML consumers ✅; T19 concrete plan-horizon test ✅; T18 keeps docstring/annotations verbatim ✅; T2 annotation update ✅; T20 both parity gates ✅; global line-drift note ✅. ✅
