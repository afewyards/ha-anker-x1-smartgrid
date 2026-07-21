# ML Predictor Status Visibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface which load-predictor tier is active, how far away ML activation is, and whether the configured forecast add-on is reachable — as attributes on `sensor.smartgrid_active_load_model` plus a header readout on the Lovelace plan card.

**Architecture:** Integration-only (spec Approach A). New pure module `ml_status.py` (coverage counter + status-string builder), new never-raise `fetch_health()` in `remote_forecast.py`, controller stores health/coverage and threads one attrs dict through `snapshot.build_status` into the existing declarative `_SensorSpec` attrs mechanism. Card shows a single header-only series reading the `ml_status` attribute.

**Tech Stack:** Python (HA custom integration), pytest, apexcharts-card YAML.

**Spec:** `docs/superpowers/specs/2026-07-21-ml-status-visibility-design.md`

## Global Constraints

- All new paths NEVER raise; any failure degrades to `None` attrs / `addon_reachable: false`. Observability only — zero planning/actuation impact.
- No add-on changes, no vendored-module (`hgbr.py`, `featureset.py`) edits — avoids add-on sync lockstep. EXCEPTION: importing `featureset._TZ_AMS` is read-only use, allowed.
- No new entities; `sensor.smartgrid_active_load_model` state stays the tier name.
- `COVERAGE_REQUIRED_DAYS = 21` (mirrors `hgbr.is_ready` default `min_days=21`).
- Engineers: ≤5 files per task; tests run via test-runner subagent (never directly); `bunx` n/a (Python project).
- Run `graphify update .` after code changes (final task).

---

### Task 1: `ml_status.py` — coverage counter (+ is_ready parity lock)

**Files:**
- Create: `custom_components/anker_x1_smartgrid/ml_status.py`
- Test: `tests/test_ml_status.py`

**Interfaces:**
- Consumes: `featureset._TZ_AMS` (ZoneInfo), hourly-row dicts with `"hour_ts"` ISO-UTC strings (shape of `recorder.read_hourly_rows()` output).
- Produces: `COVERAGE_REQUIRED_DAYS: int = 21`, `count_lag_complete_days(hourly_rows: list[dict]) -> int`. Task 2 adds `build_ml_status_attrs` to this module; Task 4 imports both.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ml_status.py
"""Tests for ml_status — coverage counter + (Task 2) status-string builder."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.anker_x1_smartgrid import ml_status


def _rows(start: datetime, n_hours: int) -> list[dict]:
    return [
        {"hour_ts": (start + timedelta(hours=i)).isoformat()}
        for i in range(n_hours)
    ]


UTC = timezone.utc


def test_count_empty():
    assert ml_status.count_lag_complete_days([]) == 0


def test_count_continuous_30_days():
    # 30 full days: lag-complete rows start at +168h → days 7..29 = 23 dates
    rows = _rows(datetime(2026, 6, 1, 0, 0, tzinfo=UTC), 30 * 24)
    assert ml_status.count_lag_complete_days(rows) == 23


def test_count_below_seed_week_is_zero():
    rows = _rows(datetime(2026, 6, 1, 0, 0, tzinfo=UTC), 6 * 24)
    assert ml_status.count_lag_complete_days(rows) == 0


def test_gap_breaks_lag_completeness():
    # Remove the entire first day: rows at +168h..+191h lose their lag partner
    rows = _rows(datetime(2026, 6, 1, 0, 0, tzinfo=UTC), 30 * 24)
    rows = rows[24:]
    # Days 8..29 remain lag-complete (day 7's partners were deleted) = 22
    assert ml_status.count_lag_complete_days(rows) == 22


def test_malformed_ts_skipped():
    rows = _rows(datetime(2026, 6, 1, 0, 0, tzinfo=UTC), 8 * 24)
    rows.append({"hour_ts": "not-a-date"})
    rows.append({"hour_ts": None})
    # Must not raise; 8 days → lag-complete day 7 only = 1
    assert ml_status.count_lag_complete_days(rows) == 1


def test_amsterdam_date_counting():
    # 2026-06-08T22:00Z = 2026-06-09 00:00 Amsterdam (CEST) — the row's date
    # must be counted in LOCAL time, so a UTC-evening row lands on the next day.
    base = datetime(2026, 6, 1, 22, 0, tzinfo=UTC)
    rows = [{"hour_ts": base.isoformat()},
            {"hour_ts": (base + timedelta(hours=168)).isoformat()}]
    assert ml_status.count_lag_complete_days(rows) == 1  # the +168h row, dated 06-09 local


def test_parity_with_hgbr_is_ready():
    sklearn = pytest.importorskip("sklearn")  # noqa: F841 — dev venv only
    from custom_components.anker_x1_smartgrid.hgbr import HGBRQuantileModel

    model = HGBRQuantileModel()
    for n_days in (27, 28, 29, 35):
        rows = _rows(datetime(2026, 6, 1, 0, 0, tzinfo=UTC), n_days * 24)
        counter_ready = ml_status.count_lag_complete_days(rows) >= ml_status.COVERAGE_REQUIRED_DAYS
        assert counter_ready == model.is_ready(rows), f"diverged at {n_days} days"
```

- [ ] **Step 2: Run tests to verify they fail**

Delegate to test-runner: `pytest tests/test_ml_status.py -v`
Expected: FAIL / ERROR with `ModuleNotFoundError` or `AttributeError` (module doesn't exist).

- [ ] **Step 3: Write the implementation**

```python
# custom_components/anker_x1_smartgrid/ml_status.py
"""Pure helpers for ML predictor status visibility.

Observability only — nothing here touches planning or actuation.

``count_lag_complete_days`` mirrors ``HGBRQuantileModel.is_ready``'s coverage
rule WITHOUT the sklearn import guard (sklearn cannot install on the on-box
py3.14/musl HA core, so ``is_ready`` always returns False there).  Kept
standalone rather than factored into ``hgbr.py`` to avoid add-on vendoring
lockstep; ``test_parity_with_hgbr_is_ready`` locks the two implementations
together in the dev venv.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .featureset import _TZ_AMS

COVERAGE_REQUIRED_DAYS: int = 21
_LAG_7D = timedelta(hours=168)


def count_lag_complete_days(hourly_rows: list[dict]) -> int:
    """Count distinct Europe/Amsterdam dates carrying lag-complete rows.

    A row at UTC time *t* is lag-complete when a row at *t − 168 h* is also
    present.  Never raises; malformed timestamps are skipped.
    """
    ts_set: set[datetime] = set()
    for row in hourly_rows:
        ts_str = row.get("hour_ts")
        if not ts_str:
            continue
        try:
            ts_set.add(datetime.fromisoformat(str(ts_str)))
        except (ValueError, TypeError):
            continue

    lag_complete_dates = set()
    for ts in ts_set:
        if (ts - _LAG_7D) in ts_set:
            lag_complete_dates.add(ts.astimezone(_TZ_AMS).date())
    return len(lag_complete_dates)
```

- [ ] **Step 4: Run tests to verify they pass**

Delegate to test-runner: `pytest tests/test_ml_status.py -v`
Expected: 7 PASS.

- [ ] **Step 5: Commit**

```bash
git add custom_components/anker_x1_smartgrid/ml_status.py tests/test_ml_status.py
git commit -m "feat(ml-status): add lag-complete coverage counter with is_ready parity lock"
```

---

### Task 2: `ml_status.py` — status-attrs builder

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/ml_status.py`
- Test: `tests/test_ml_status.py`

**Interfaces:**
- Consumes: `COVERAGE_REQUIRED_DAYS` from Task 1.
- Produces: `build_ml_status_attrs(*, addon_enabled: bool, addon_url: str | None, health: dict | None, health_ts: datetime | None, coverage_days: int | None, active_model: str) -> dict` returning EXACTLY the keys: `ml_status, addon_configured, addon_reachable, addon_ready, addon_promoted, addon_n_rows, addon_last_trained, coverage_days, coverage_required, eta_days, last_health_check`. Task 4 calls it; Task 5's sensor attrs use these key names verbatim.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_ml_status.py`)

```python
NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
HEALTH_DORMANT = {"ready": False, "promoted": False, "n_rows": 622,
                  "last_trained": "2026-07-21T01:00:00+00:00"}
HEALTH_READY = {**HEALTH_DORMANT, "ready": True}
HEALTH_PROMOTED = {**HEALTH_DORMANT, "ready": True, "promoted": True}


def _attrs(**overrides):
    kw = dict(addon_enabled=True, addon_url="http://x:8099", health=HEALTH_DORMANT,
              health_ts=NOW, coverage_days=20, active_model="bucketed")
    kw.update(overrides)
    return ml_status.build_ml_status_attrs(**kw)


def test_status_addon_off():
    assert _attrs(addon_enabled=False)["ml_status"] == "add-on off"
    assert _attrs(addon_url="")["ml_status"] == "add-on off"
    assert _attrs(addon_enabled=False)["addon_configured"] is False


def test_status_unreachable():
    a = _attrs(health=None)
    assert a["ml_status"] == "⚠ unreachable"
    assert a["addon_reachable"] is False
    assert a["addon_n_rows"] is None and a["addon_last_trained"] is None


def test_status_eta_countdown():
    a = _attrs()
    assert a["ml_status"] == "ML in ~1d"
    assert a["eta_days"] == 1
    assert a["coverage_days"] == 20 and a["coverage_required"] == 21
    assert a["addon_reachable"] is True and a["addon_ready"] is False


def test_status_eta_clamps_at_zero():
    assert _attrs(coverage_days=25)["eta_days"] == 0


def test_status_backtest_gate():
    assert _attrs(health=HEALTH_READY)["ml_status"] == "backtest gate"


def test_status_ml_active():
    a = _attrs(health=HEALTH_PROMOTED, active_model="remote")
    assert a["ml_status"] == "ML active"
    assert a["addon_promoted"] is True


def test_status_promoted_not_consumed():
    assert _attrs(health=HEALTH_PROMOTED)["ml_status"] == "⚠ promoted, not consumed"


def test_status_no_health_check_yet_falls_back_to_eta():
    # enabled but first tick hasn't polled yet: NOT flagged unreachable
    a = _attrs(health=None, health_ts=None)
    assert a["ml_status"] == "ML in ~1d"
    assert a["addon_reachable"] is None
    assert a["last_health_check"] is None


def test_status_collecting_data_when_no_coverage():
    assert _attrs(coverage_days=None)["ml_status"] == "collecting data"


def test_last_health_check_iso():
    assert _attrs()["last_health_check"] == NOW.isoformat()
```

- [ ] **Step 2: Run tests to verify they fail**

Delegate to test-runner: `pytest tests/test_ml_status.py -v`
Expected: new tests FAIL with `AttributeError: build_ml_status_attrs`.

- [ ] **Step 3: Write the implementation** (append to `ml_status.py`)

```python
def build_ml_status_attrs(
    *,
    addon_enabled: bool,
    addon_url: str | None,
    health: dict | None,
    health_ts: datetime | None,
    coverage_days: int | None,
    active_model: str,
) -> dict:
    """Build the diagnostic attribute dict for the active-load-model sensor.

    Priority order (spec §4): off → unreachable → active/promoted →
    backtest gate → coverage ETA → collecting data.  Never raises.
    """
    configured = bool(addon_enabled) and bool(addon_url)
    checked = health_ts is not None
    reachable: bool | None = (health is not None) if checked else None
    ready: bool | None = bool(health.get("ready")) if health else None
    promoted: bool | None = bool(health.get("promoted")) if health else None
    eta_days = (
        max(0, COVERAGE_REQUIRED_DAYS - coverage_days)
        if coverage_days is not None
        else None
    )

    if not configured:
        status = "add-on off"
    elif checked and health is None:
        status = "⚠ unreachable"
    elif promoted and active_model == "remote":
        status = "ML active"
    elif promoted:
        status = "⚠ promoted, not consumed"
    elif ready:
        status = "backtest gate"
    elif eta_days is not None:
        status = f"ML in ~{eta_days}d"
    else:
        status = "collecting data"

    return {
        "ml_status": status,
        "addon_configured": configured,
        "addon_reachable": reachable,
        "addon_ready": ready,
        "addon_promoted": promoted,
        "addon_n_rows": health.get("n_rows") if health else None,
        "addon_last_trained": health.get("last_trained") if health else None,
        "coverage_days": coverage_days,
        "coverage_required": COVERAGE_REQUIRED_DAYS,
        "eta_days": eta_days,
        "last_health_check": health_ts.isoformat() if health_ts else None,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Delegate to test-runner: `pytest tests/test_ml_status.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add custom_components/anker_x1_smartgrid/ml_status.py tests/test_ml_status.py
git commit -m "feat(ml-status): add status-attrs builder with priority-ordered display string"
```

---

### Task 3: `remote_forecast.fetch_health`

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/remote_forecast.py`
- Test: `tests/test_remote_forecast.py`

**Interfaces:**
- Consumes: nothing new; mirrors `fetch_forecast`'s session/timeout pattern (`asyncio.wait_for` + `resp.json(content_type=None)`, see `remote_forecast.py:180-205`).
- Produces: `async def fetch_health(session, url: str, timeout: int) -> dict | None`. Task 4 calls it from the controller.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_remote_forecast.py`; reuse the file's existing fake-session helpers if present, else use these)

```python
class _FakeGetResponse:
    def __init__(self, status=200, payload=None, raise_on_json=False):
        self.status = status
        self._payload = payload
        self._raise = raise_on_json

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self, content_type=None):
        if self._raise:
            raise ValueError("bad json")
        return self._payload


class _FakeGetSession:
    def __init__(self, response=None, raise_connect=False):
        self._response = response
        self._raise = raise_connect

    def get(self, url):
        if self._raise:
            raise OSError("connection refused")
        return self._response


@pytest.mark.asyncio
async def test_fetch_health_ok():
    payload = {"ready": True, "promoted": False, "n_rows": 622}
    session = _FakeGetSession(_FakeGetResponse(200, payload))
    assert await remote_forecast.fetch_health(session, "http://x:8099/", 5) == payload


@pytest.mark.asyncio
async def test_fetch_health_non_200():
    session = _FakeGetSession(_FakeGetResponse(503, {}))
    assert await remote_forecast.fetch_health(session, "http://x:8099", 5) is None


@pytest.mark.asyncio
async def test_fetch_health_connect_error():
    assert await remote_forecast.fetch_health(_FakeGetSession(raise_connect=True), "http://x:8099", 5) is None


@pytest.mark.asyncio
async def test_fetch_health_malformed_json():
    session = _FakeGetSession(_FakeGetResponse(200, raise_on_json=True))
    assert await remote_forecast.fetch_health(session, "http://x:8099", 5) is None


@pytest.mark.asyncio
async def test_fetch_health_non_dict_payload():
    session = _FakeGetSession(_FakeGetResponse(200, ["not", "a", "dict"]))
    assert await remote_forecast.fetch_health(session, "http://x:8099", 5) is None
```

NOTE: if `tests/test_remote_forecast.py` already defines fake session/response classes for `fetch_forecast`, extend those (add `.get`) instead of duplicating — DRY, match the file's existing style. Keep the test bodies as written.

- [ ] **Step 2: Run tests to verify they fail**

Delegate to test-runner: `pytest tests/test_remote_forecast.py -k fetch_health -v`
Expected: FAIL with `AttributeError: fetch_health`.

- [ ] **Step 3: Write the implementation** (add to `remote_forecast.py`, after `fetch_forecast`)

```python
async def fetch_health(session, url: str, timeout: int) -> dict | None:
    """GET the add-on ``/health`` endpoint and return its payload dict.

    Mirrors :func:`fetch_forecast`'s never-raise contract: unreachable,
    timeout, non-200, malformed JSON, or a non-dict payload all return
    ``None`` (⇒ "unreachable" in ml_status).  Unlike ``/predict``, this is
    polled even while the model is dormant, so reachability is monitored
    during the exact window where a dead ``addon_url`` would otherwise
    hide (observed live 2026-07-21).
    """
    endpoint = url.rstrip("/") + "/health"

    async def _do_request() -> dict | None:
        async with session.get(endpoint) as resp:
            if resp.status != 200:
                _LOGGER.debug("remote_forecast: /health returned HTTP %s", resp.status)
                return None
            try:
                return await resp.json(content_type=None)
            except Exception as exc:
                _LOGGER.debug("remote_forecast: /health JSON parse error: %s", exc)
                return None

    try:
        data = await asyncio.wait_for(_do_request(), timeout=timeout)
    except TimeoutError:
        _LOGGER.debug("remote_forecast: /health timed out after %ss", timeout)
        return None
    except Exception as exc:
        _LOGGER.debug("remote_forecast: /health connection error: %s", exc)
        return None

    return data if isinstance(data, dict) else None
```

- [ ] **Step 4: Run tests to verify they pass**

Delegate to test-runner: `pytest tests/test_remote_forecast.py -v`
Expected: all PASS (new + pre-existing).

- [ ] **Step 5: Commit**

```bash
git add custom_components/anker_x1_smartgrid/remote_forecast.py tests/test_remote_forecast.py
git commit -m "feat(ml-status): add never-raise fetch_health for add-on reachability"
```

---

### Task 4: Controller + snapshot wiring

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/controller.py`
- Modify: `custom_components/anker_x1_smartgrid/snapshot.py`
- Test: `tests/test_controller_remote.py`

**Interfaces:**
- Consumes: `ml_status.count_lag_complete_days`, `ml_status.build_ml_status_attrs` (Tasks 1-2), `remote_forecast.fetch_health` (Task 3).
- Produces: `controller.last_status` contains the 11 ml-status keys (Task 2's list). `snapshot.build_status` gains keyword `ml_status_attrs: dict | None = None`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_controller_remote.py`, following that file's existing controller-fixture style — it already stubs the remote-fetch path)

```python
# Adapt fixture names to the file's existing conventions; assertions are the contract:

async def test_health_polled_and_status_attrs_present(controller_fixture, monkeypatch):
    """After a tick with addon_enabled, last_status carries ml-status attrs."""
    health = {"ready": False, "promoted": False, "n_rows": 622,
              "last_trained": "2026-07-21T01:00:00+00:00"}

    async def fake_health(session, url, timeout):
        return health

    monkeypatch.setattr(
        "custom_components.anker_x1_smartgrid.controller.fetch_health", fake_health
    )
    # ... drive one tick via the file's existing helper ...
    status = controller_fixture.last_status
    assert status["addon_reachable"] is True
    assert status["addon_n_rows"] == 622
    assert status["ml_status"].startswith(("ML in", "collecting"))
    assert status["coverage_required"] == 21


async def test_health_unreachable_flagged(controller_fixture, monkeypatch):
    async def fake_health(session, url, timeout):
        return None

    monkeypatch.setattr(
        "custom_components.anker_x1_smartgrid.controller.fetch_health", fake_health
    )
    # ... drive one tick ...
    status = controller_fixture.last_status
    assert status["addon_reachable"] is False
    assert status["ml_status"] == "⚠ unreachable"


async def test_health_fetch_failure_never_breaks_tick(controller_fixture, monkeypatch):
    async def exploding_health(session, url, timeout):
        raise RuntimeError("must be swallowed by the tick backstop")

    monkeypatch.setattr(
        "custom_components.anker_x1_smartgrid.controller.fetch_health", exploding_health
    )
    # ... drive one tick — must not raise; planning attrs still present ...
    assert "setpoint_w" in controller_fixture.last_status
```

- [ ] **Step 2: Run tests to verify they fail**

Delegate to test-runner: `pytest tests/test_controller_remote.py -v`
Expected: new tests FAIL (`ImportError: fetch_health` / `KeyError: addon_reachable`).

- [ ] **Step 3: Implement**

3a. `controller.py` imports (line ~40, extend the existing `from .remote_forecast import ...`):

```python
from .remote_forecast import RemoteForecastPredictor, build_hours_payload, fetch_forecast, fetch_health
from . import ml_status
```

3b. `controller.py` `__init__` (next to `self._remote_forecast_map`, line ~193):

```python
self._addon_health: dict | None = None
self._addon_health_ts: datetime | None = None
self.coverage_lag_complete_days: int | None = None
```

3c. Tick hourly block (line ~886, `if self.cfg.addon_enabled and now.hour != self._last_remote_forecast_hour:`) — FIRST statements inside the existing `try:` (before the persons/predict code, so a predict-path failure can't skip the health poll):

```python
self._addon_health = await fetch_health(
    async_get_clientsession(self._hass),
    self.cfg.addon_url,
    self.cfg.addon_timeout,
)
self._addon_health_ts = now
```

3d. `_retrain_sync` — at the TOP of the method body (BEFORE the Tier-0 early return at ~line 511, or coverage stops updating once remote activates):

```python
if self._recorder is not None:
    try:
        self.coverage_lag_complete_days = ml_status.count_lag_complete_days(
            self._recorder.read_hourly_rows()
        )
    except Exception:
        self.coverage_lag_complete_days = None
```

3e. `snapshot.py` `build_status` — add keyword param and merge (place `**` right after `"active_model"` in the returned dict):

```python
    ml_status_attrs: dict | None = None,
```
```python
        "active_model": active_model_name,
        **(ml_status_attrs or {}),
```

3f. Controller `build_status` call site (line ~1436) — add:

```python
            ml_status_attrs=ml_status.build_ml_status_attrs(
                addon_enabled=self.cfg.addon_enabled,
                addon_url=self.cfg.addon_url,
                health=self._addon_health,
                health_ts=self._addon_health_ts,
                coverage_days=self.coverage_lag_complete_days,
                active_model=self.active_model_name,
            ),
```

- [ ] **Step 4: Run tests to verify they pass**

Delegate to test-runner: `pytest tests/test_controller_remote.py tests/test_ml_status.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add custom_components/anker_x1_smartgrid/controller.py custom_components/anker_x1_smartgrid/snapshot.py tests/test_controller_remote.py
git commit -m "feat(ml-status): poll add-on health hourly and thread status attrs into last_status"
```

---

### Task 5: Sensor attributes

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/sensor.py` (the `_SensorSpec("active_model", ...)` entry, line ~56)
- Test: `tests/test_sensor.py`

**Interfaces:**
- Consumes: the 11 `last_status` keys from Task 4. The generic spec sensor already renders `attrs_keys` via `extra_state_attributes` (`sensor.py:106`).
- Produces: `sensor.smartgrid_active_load_model` exposes the 11 attributes.

- [ ] **Step 1: Write the failing test** (append to `tests/test_sensor.py`, using that file's existing spec-sensor test pattern)

```python
def test_active_model_sensor_exposes_ml_status_attrs():
    spec = next(s for s in sensor.SENSOR_SPECS if s.key == "active_model")
    attr_names = [a for a, _ in spec.attrs_keys]
    assert attr_names == [
        "ml_status", "addon_configured", "addon_reachable", "addon_ready",
        "addon_promoted", "addon_n_rows", "addon_last_trained",
        "coverage_days", "coverage_required", "eta_days", "last_health_check",
    ]
    # every attr maps to the identical last_status key
    assert all(a == k for a, k in spec.attrs_keys)
```

- [ ] **Step 2: Run test to verify it fails**

Delegate to test-runner: `pytest tests/test_sensor.py -k ml_status_attrs -v`
Expected: FAIL (`attrs_keys` is empty `()`).

- [ ] **Step 3: Implement** — replace line ~56:

```python
    _SensorSpec(
        "active_model",
        "SmartGrid active load model",
        attrs_keys=tuple(
            (k, k)
            for k in (
                "ml_status", "addon_configured", "addon_reachable", "addon_ready",
                "addon_promoted", "addon_n_rows", "addon_last_trained",
                "coverage_days", "coverage_required", "eta_days", "last_health_check",
            )
        ),
    ),
```

- [ ] **Step 4: Run tests to verify they pass**

Delegate to test-runner: `pytest tests/test_sensor.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add custom_components/anker_x1_smartgrid/sensor.py tests/test_sensor.py
git commit -m "feat(ml-status): expose ML status attributes on active load model sensor"
```

---

### Task 6: Lovelace card header

**Files:**
- Modify: `lovelace/apexcharts-plan-card.yaml`

**Interfaces:**
- Consumes: `sensor.smartgrid_active_load_model` attribute `ml_status` (Task 5).
- Produces: card header shows `Predictor: <ml_status>`; no other series values appear in the header.

⚠ CONTEXT: the card's top comment says header/legend readouts are INTENTIONALLY OFF because every plan series would print a bogus last-point "0 h". Enabling `show_states` therefore REQUIRES `in_header: false` on every existing series.

- [ ] **Step 1: Implement**

Inside the wrapped apexcharts config (under `card:`):

1. Add (or extend) the header block:

```yaml
    header:
      show: true
      show_states: true
      colorize_states: false
```

2. For EVERY existing entry under `series:` add `in_header: false` to its `show:` block (create the `show:` block where absent):

```yaml
      show:
        in_header: false
```

3. Append the header-only status series (last in the `series:` list):

```yaml
    - entity: sensor.smartgrid_active_load_model
      attribute: ml_status
      name: Predictor
      show:
        in_chart: false
        in_header: raw
```

4. Update the top NOTE comment: header readouts remain off for plan series; the single header readout is the Predictor ml_status string (raw, non-numeric).

- [ ] **Step 2: Verify**

```bash
python3 -c "import yaml,sys; yaml.safe_load(open('lovelace/apexcharts-plan-card.yaml')); print('YAML OK')"
grep -c "in_header: false" lovelace/apexcharts-plan-card.yaml   # == number of pre-existing series
grep -A2 "attribute: ml_status" lovelace/apexcharts-plan-card.yaml
```
Expected: `YAML OK`; count matches series count; status series present.

- [ ] **Step 3: Commit**

```bash
git add lovelace/apexcharts-plan-card.yaml
git commit -m "feat(card): show ML predictor status in plan card header"
```

---

### Task 7: Full verification + graph update + deploy

**Files:**
- None created; runs checks and deploys.

- [ ] **Step 1: Full test suite + lint** — delegate to test-runner: `pytest tests/ -q` and `ruff check .`
Expected: all green (baseline was 1688+ green), ruff clean.

- [ ] **Step 2: Update knowledge graph**

```bash
graphify update .
```

- [ ] **Step 3: Review gate** — feature is multi-file: dispatch `code-review` agent on the branch diff before deploy. Fix findings, re-run suite.

- [ ] **Step 4: Deploy to lab** (per [[haos-deploy]] memory: pristine checkout; scp; NO .bak files inside custom_components)

```bash
scp custom_components/anker_x1_smartgrid/{ml_status.py,remote_forecast.py,controller.py,snapshot.py,sensor.py} \
  root@172.20.0.47:/config/custom_components/anker_x1_smartgrid/
ssh root@172.20.0.47 "ha core restart"
```

- [ ] **Step 5: Live verify**

```bash
TOKEN=$(cat ~/Sites/.token)
curl -s -H "Authorization: Bearer $TOKEN" \
  https://homeassistant.lab.kle.ist/api/states/sensor.smartgrid_active_load_model | python3 -m json.tool
```
Expected attrs: `ml_status` = `ML in ~0d`/`ML in ~1d` (coverage was 20/21 on 2026-07-21), `addon_reachable: true`, `addon_n_rows` ≥ 622, `coverage_required: 21`.

- [ ] **Step 6: Remind user** — card YAML paste is manual (Lovelace edit → paste updated `apexcharts-plan-card.yaml`).
