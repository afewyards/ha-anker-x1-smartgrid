# Battery Calibration Policy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Periodically drive the pack to the top of its range and dwell there, so module BMSs get taper current to top-balance.

**Architecture:** Pure `calibration.py` decides whether a calibration cycle runs this slot. `controller._tick_impl` flips `new_plan.state` to `FORCING` when it does. DP core, `decision.py`, `optimize.py`, `regret.py` untouched — no parity risk.

**Tech Stack:** Python 3.13, Home Assistant custom component, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-08-03-battery-calibration-policy-design.md`

## Global Constraints

- Ships OFF: `calibration_enabled` default `False`. Feature-off must be behaviourally identical to today.
- Every new option MUST be in `config_flow._TUNABLES` — options outside it are wiped by the next UI options save.
- `calibration.py` is pure: no HA imports, no I/O, no clock reads. `now` is always a parameter.
- Nothing forces a charge on absent/short data. Fail-closed everywhere.
- Recorder reads from the controller go through `await self._hass.async_add_executor_job(...)` — sqlite must not block the event loop.
- Run `bunx ruff check` and `bunx ruff format` before each commit; lint is blocking in this repo.
- **Use `.venv/bin/python -m pytest`.** The system `python3` is 3.9.6 with no `homeassistant`; the project venv is 3.12.13. Lint and manual inspection never substitute for running the suite — a broken interpreter is a BLOCKED report, not a reason to commit.
- Full suite must stay green: `.venv/bin/python -m pytest -q` (baseline 2295 passed).
- **Any new option key needs TWO registrations:** `config_flow._TUNABLES` *and* `config_flow.OPTIONS_SECTIONS` (calibration keys belong in `SECTION_BATTERY`). `_TUNABLES` alone fails `test_config_flow.py::test_sections_cover_all_option_fields`.
- **Editing a vendored module requires a re-sync.** Eight modules are vendored byte-identically into the add-on under a SHA-manifest gate (`tests/test_vendored_parity.py`): `backtest, const, dataquality, featureset, hgbr, loadmodel, recorder, rollup`. After touching any of them under `custom_components/anker_x1_smartgrid/`, run `./addon/anker_x1_forecast/sync_core.sh` and commit the regenerated `forecast_core/` copy plus `SOURCE_SHA256`. **This binds Task 1 (`const.py`) and Task 2 (`recorder.py`).**

---

## File Structure

| File | Responsibility |
|---|---|
| `custom_components/anker_x1_smartgrid/calibration.py` | NEW. Pure policy: success detection, window selection, action. |
| `custom_components/anker_x1_smartgrid/const.py` | `CONF_*`/`DEFAULT_*` for 4 options + 2 tuning consts. |
| `custom_components/anker_x1_smartgrid/models.py` | 4 `Config` fields. |
| `custom_components/anker_x1_smartgrid/config_flow.py` | 4 entries in `_TUNABLES`. |
| `custom_components/anker_x1_smartgrid/recorder.py` | `read_soc_samples`. |
| `custom_components/anker_x1_smartgrid/controller.py` | Override hook in `_tick_impl`; expose state for the sensor. |
| `custom_components/anker_x1_smartgrid/sensor.py` | Plan-sensor attributes. |
| `tests/test_calibration.py` | NEW. Pure policy tests. |
| `tests/test_calibration_controller.py` | NEW. Isolation + active-path. |
| `tests/test_calibration_config.py` | NEW. Options round-trip. |

---

### Task 1: Config plumbing

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/const.py`
- Modify: `custom_components/anker_x1_smartgrid/models.py` (`Config` dataclass, ends ~L128)
- Modify: `custom_components/anker_x1_smartgrid/config_flow.py:241` (`_TUNABLES`)
- Test: `tests/test_calibration_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `cfg.calibration_enabled: bool`, `cfg.calibration_interval_days: int`, `cfg.calibration_top_soc: float`, `cfg.calibration_dwell_h: float`; `const.CALIBRATION_PRICE_PERCENTILE: float`, `const.CALIBRATION_GRACE_DAYS: int`.

- [ ] **Step 1: Write the failing test**

`tests/test_calibration_config.py`:

```python
"""Calibration options: defaults, Config wiring, _TUNABLES membership."""

from custom_components.anker_x1_smartgrid import const
from custom_components.anker_x1_smartgrid.config_flow import _TUNABLES
from custom_components.anker_x1_smartgrid.models import Config


def test_defaults_ship_off():
    cfg = Config()
    assert cfg.calibration_enabled is False
    assert cfg.calibration_interval_days == 5
    assert cfg.calibration_top_soc == 97.0
    assert cfg.calibration_dwell_h == 2.0


def test_tuning_consts():
    assert const.CALIBRATION_PRICE_PERCENTILE == 30.0
    assert const.CALIBRATION_GRACE_DAYS == 7


def test_all_four_options_are_tunable():
    """Outside _TUNABLES an option is wiped by the next UI options save."""
    keys = {name for name, _default, _validator in _TUNABLES}
    assert const.CONF_CALIBRATION_ENABLED in keys
    assert const.CONF_CALIBRATION_INTERVAL_DAYS in keys
    assert const.CONF_CALIBRATION_TOP_SOC in keys
    assert const.CONF_CALIBRATION_DWELL_H in keys
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_calibration_config.py -v`
Expected: FAIL — `AttributeError: module 'const' has no attribute 'CONF_CALIBRATION_ENABLED'`

- [ ] **Step 3: Add consts**

In `const.py`, beside the other `CONF_*` definitions (near `CONF_IDLE_DRAIN_W`, L26):

```python
CONF_CALIBRATION_ENABLED = "calibration_enabled"
CONF_CALIBRATION_INTERVAL_DAYS = "calibration_interval_days"
CONF_CALIBRATION_TOP_SOC = "calibration_top_soc"
CONF_CALIBRATION_DWELL_H = "calibration_dwell_h"
```

Beside the other `DEFAULT_*` definitions (near `DEFAULT_IDLE_DRAIN_W`, L111):

```python
# Periodic full-charge calibration (see
# docs/superpowers/specs/2026-08-03-battery-calibration-policy-design.md).
# Ships OFF. 5 days suits summer, where natural full days mostly satisfy it;
# raise in winter, when it will force a grid charge instead.
DEFAULT_CALIBRATION_ENABLED = False
DEFAULT_CALIBRATION_INTERVAL_DAYS = 5
# 97 sits below the observed 99% stall (2026-08-02: reached 99%, then 0 W for
# 2.5 h), so the dwell is actually reachable.
DEFAULT_CALIBRATION_TOP_SOC = 97.0
DEFAULT_CALIBRATION_DWELL_H = 2.0

# Tuning consts, deliberately not user-facing.
# A window is cheap enough when its mean slot price is at or below this
# percentile of all slot prices in PriceHistoryStore.history.
CALIBRATION_PRICE_PERCENTILE = 30.0
# Past interval + grace days, take the cheapest visible window regardless.
CALIBRATION_GRACE_DAYS = 7
```

- [ ] **Step 4: Add Config fields**

In `models.py`, append to the `Config` dataclass (after `export_drain_window_h`, before the methods):

```python
    # Periodic full-charge calibration (spec 2026-08-03). Ships OFF.
    calibration_enabled: bool = const.DEFAULT_CALIBRATION_ENABLED
    calibration_interval_days: int = const.DEFAULT_CALIBRATION_INTERVAL_DAYS
    calibration_top_soc: float = const.DEFAULT_CALIBRATION_TOP_SOC
    calibration_dwell_h: float = const.DEFAULT_CALIBRATION_DWELL_H
```

- [ ] **Step 5: Add _TUNABLES entries**

In `config_flow.py`, append inside the `_TUNABLES` list:

```python
    (const.CONF_CALIBRATION_ENABLED, const.DEFAULT_CALIBRATION_ENABLED, cv.boolean),
    (
        const.CONF_CALIBRATION_INTERVAL_DAYS,
        const.DEFAULT_CALIBRATION_INTERVAL_DAYS,
        vol.All(vol.Coerce(int), vol.Range(min=1, max=90)),
    ),
    (
        const.CONF_CALIBRATION_TOP_SOC,
        const.DEFAULT_CALIBRATION_TOP_SOC,
        # Floor at 80: below that this stops being a top-of-range dwell.
        vol.All(vol.Coerce(float), vol.Range(min=80.0, max=100.0)),
    ),
    (
        const.CONF_CALIBRATION_DWELL_H,
        const.DEFAULT_CALIBRATION_DWELL_H,
        vol.All(vol.Coerce(float), vol.Range(min=0.25, max=12.0)),
    ),
```

- [ ] **Step 6: Run tests**

Run: `pytest tests/test_calibration_config.py tests/test_config_flow.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
bunx ruff check custom_components tests && bunx ruff format custom_components tests
git add custom_components/anker_x1_smartgrid/const.py custom_components/anker_x1_smartgrid/models.py custom_components/anker_x1_smartgrid/config_flow.py tests/test_calibration_config.py
git commit -m "feat(calibration): add config options and tuning consts"
```

---

### Task 2: `recorder.read_soc_samples`

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/recorder.py` (add after `read_load_samples`, which ends ~L625)
- Test: `tests/test_recorder.py` (append)

**Interfaces:**
- Consumes: nothing.
- Produces: `DataRecorder.read_soc_samples(since_iso: str | None = None) -> list[tuple[str, float]]` — `(ts, soc)` ascending, NULL soc rows skipped.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_recorder.py`. Match the file's existing recorder-construction fixture; if it builds a `DataRecorder` on a tmp path, reuse that pattern verbatim.

```python
def test_read_soc_samples_orders_and_skips_nulls(tmp_path):
    from custom_components.anker_x1_smartgrid.recorder import DataRecorder

    rec = DataRecorder(str(tmp_path / "t.db"))
    rec.append({"ts": "2026-08-02T02:00:00+00:00", "soc": 40.0})
    rec.append({"ts": "2026-08-02T01:00:00+00:00", "soc": 30.0})
    rec.append({"ts": "2026-08-02T03:00:00+00:00", "soc": None})
    rows = rec.read_soc_samples()
    assert rows == [
        ("2026-08-02T01:00:00+00:00", 30.0),
        ("2026-08-02T02:00:00+00:00", 40.0),
    ]
    assert rec.read_soc_samples("2026-08-02T02:00:00+00:00") == [
        ("2026-08-02T02:00:00+00:00", 40.0)
    ]
    rec.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_recorder.py::test_read_soc_samples_orders_and_skips_nulls -v`
Expected: FAIL — `AttributeError: 'DataRecorder' object has no attribute 'read_soc_samples'`

- [ ] **Step 3: Implement**

In `recorder.py`, directly after `read_load_samples`:

```python
    def read_soc_samples(self, since_iso: str | None = None) -> list[tuple[str, float]]:
        """Return (ts, soc) rows ordered by ts ascending, NULL soc skipped.

        Mirrors :meth:`read_load_samples`' shape (optional ts>=since window,
        NULL-filtered in SQL).  Consumed by ``calibration.last_success_end``
        to detect a completed full-charge dwell without persisting state.
        """
        with self._lock:
            if since_iso is not None:
                cur = self._conn.execute(
                    "SELECT ts, soc FROM samples WHERE soc IS NOT NULL AND ts >= ? ORDER BY ts ASC",
                    (since_iso,),
                )
            else:
                cur = self._conn.execute(
                    "SELECT ts, soc FROM samples WHERE soc IS NOT NULL ORDER BY ts ASC"
                )
            return [(ts, float(soc)) for ts, soc in cur.fetchall()]
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_recorder.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
bunx ruff check custom_components tests && bunx ruff format custom_components tests
git add custom_components/anker_x1_smartgrid/recorder.py tests/test_recorder.py
git commit -m "feat(calibration): add recorder.read_soc_samples"
```

---

### Task 3: Success detection

**Files:**
- Create: `custom_components/anker_x1_smartgrid/calibration.py`
- Test: `tests/test_calibration.py`

**Interfaces:**
- Consumes: `read_soc_samples` output shape `list[tuple[str, float]]`.
- Produces:
  - `last_success_end(soc_samples, *, top_soc: float, dwell_h: float) -> datetime | None`
  - `history_span_days(soc_samples) -> float`
  - `MAX_SAMPLE_GAP_MIN: float = 15.0`

A run is a maximal block of consecutive samples with `soc >= top_soc` where no adjacent pair is more than `MAX_SAMPLE_GAP_MIN` apart. This stops an HA outage from faking a long hold. A run qualifies when `last_ts - first_ts >= dwell_h`; the returned value is the run's **last** timestamp, so an ongoing hold keeps the clock at ~now.

- [ ] **Step 1: Write the failing test**

`tests/test_calibration.py`:

```python
"""Pure calibration-policy tests. No HA, no I/O, no clock."""

from datetime import datetime, timedelta, UTC

from custom_components.anker_x1_smartgrid import calibration


def _series(start, minutes, soc_values):
    """(ts, soc) rows at fixed `minutes` spacing."""
    return [
        ((start + timedelta(minutes=minutes * i)).isoformat(), v)
        for i, v in enumerate(soc_values)
    ]


BASE = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)


def test_qualifying_run_returns_its_last_timestamp():
    # 3 h at 98% on 15-min spacing = 13 samples.
    rows = _series(BASE, 15, [98.0] * 13)
    got = calibration.last_success_end(rows, top_soc=97.0, dwell_h=2.0)
    assert got == BASE + timedelta(hours=3)


def test_run_just_under_dwell_does_not_count():
    # 1 h 45 min < 2 h dwell.
    rows = _series(BASE, 15, [98.0] * 8)
    assert calibration.last_success_end(rows, top_soc=97.0, dwell_h=2.0) is None


def test_run_below_top_soc_does_not_count():
    rows = _series(BASE, 15, [96.9] * 13)
    assert calibration.last_success_end(rows, top_soc=97.0, dwell_h=2.0) is None


def test_gap_breaks_the_run():
    """An HA outage must not fake a long hold."""
    first = _series(BASE, 15, [98.0] * 4)  # 45 min
    later = _series(BASE + timedelta(hours=6), 15, [98.0] * 4)  # 45 min
    assert calibration.last_success_end(first + later, top_soc=97.0, dwell_h=2.0) is None


def test_most_recent_qualifying_run_wins():
    old = _series(BASE, 15, [98.0] * 13)
    dip = _series(BASE + timedelta(hours=4), 15, [50.0] * 4)
    new = _series(BASE + timedelta(hours=24), 15, [99.0] * 13)
    got = calibration.last_success_end(old + dip + new, top_soc=97.0, dwell_h=2.0)
    assert got == BASE + timedelta(hours=27)


def test_empty_history_is_none():
    assert calibration.last_success_end([], top_soc=97.0, dwell_h=2.0) is None


def test_history_span_days():
    rows = _series(BASE, 60, [50.0] * 25)  # 24 h
    assert calibration.history_span_days(rows) == 1.0
    assert calibration.history_span_days([]) == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_calibration.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named '...calibration'`

- [ ] **Step 3: Implement**

Create `custom_components/anker_x1_smartgrid/calibration.py`:

```python
"""Periodic full-charge calibration policy — pure decision logic.

Design: docs/superpowers/specs/2026-08-03-battery-calibration-policy-design.md

The pack strands ~3.6 kWh below ~21% SoC (measured 2026-08-03) and has had no
opportunity to top-balance: on 2026-08-02 it reached 99% and then held 0 W for
2.5 h while PV was exported.  This module decides when to drive the pack to the
top of its range and dwell there so the module BMSs get taper current.

No HA imports, no I/O, no clock reads — ``now`` is always a parameter.  A
completed cycle is READ BACK from SoC history rather than stored, so there is
no new table, no Store, and the policy is restart-safe by construction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

# Two adjacent samples further apart than this do not belong to the same run —
# otherwise an HA outage spanning a high-SoC period fakes a completed dwell.
MAX_SAMPLE_GAP_MIN: float = 15.0


@dataclass(frozen=True)
class CalibAction:
    """An active calibration slot.  ``phase`` is for reporting only —
    both phases actuate identically (FORCING at max rate; the BMS taper
    turns that into a hold once the pack is full)."""

    phase: str  # "charging" | "holding"
    window_start: datetime
    window_end: datetime


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def history_span_days(soc_samples: list[tuple[str, float]]) -> float:
    """Wall-clock days covered by the sample series (0.0 when < 2 rows)."""
    if len(soc_samples) < 2:
        return 0.0
    return (_parse(soc_samples[-1][0]) - _parse(soc_samples[0][0])).total_seconds() / 86400.0


def last_success_end(
    soc_samples: list[tuple[str, float]],
    *,
    top_soc: float,
    dwell_h: float,
) -> datetime | None:
    """End timestamp of the most recent completed calibration dwell.

    A dwell is a maximal block of consecutive samples at/above ``top_soc``
    with no adjacent gap over ``MAX_SAMPLE_GAP_MIN``, spanning at least
    ``dwell_h``.  Returns the block's LAST timestamp, so an in-progress hold
    keeps the clock at ~now and the policy goes idle as soon as it qualifies.

    Returns None when no block qualifies — including an empty series.
    """
    best: datetime | None = None
    run_start: datetime | None = None
    run_end: datetime | None = None
    max_gap = timedelta(minutes=MAX_SAMPLE_GAP_MIN)
    need = timedelta(hours=dwell_h)

    for ts_s, soc in soc_samples:
        ts = _parse(ts_s)
        if soc >= top_soc and run_end is not None and ts - run_end <= max_gap:
            run_end = ts
            continue
        # Close the open run (if any) before starting a new one.
        if run_start is not None and run_end is not None and run_end - run_start >= need:
            best = run_end
        if soc >= top_soc:
            run_start, run_end = ts, ts
        else:
            run_start, run_end = None, None

    if run_start is not None and run_end is not None and run_end - run_start >= need:
        best = run_end
    return best
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_calibration.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
bunx ruff check custom_components tests && bunx ruff format custom_components tests
git add custom_components/anker_x1_smartgrid/calibration.py tests/test_calibration.py
git commit -m "feat(calibration): detect completed dwell from SoC history"
```

---

### Task 4: Price bar and window selection

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/calibration.py`
- Test: `tests/test_calibration.py` (append)

**Interfaces:**
- Consumes: `CalibAction`, `MAX_SAMPLE_GAP_MIN` from Task 3; `models.Config`, `models.PriceSlot`.
- Produces:
  - `price_percentile(price_history: dict[str, dict[str, float]], pct: float) -> float | None`
  - `select_window(now, soc_pct, slots, *, cfg, bar, force) -> tuple[datetime, datetime] | None`

Rules, all deterministic (no stored commitment):
- Candidate = contiguous run of slots covering `charge_h + dwell_h`, ending after `now`.
- At most one candidate per local start-date, the cheapest — this is what makes it one attempt per day.
- A candidate containing `now` wins over any future candidate — this is "a started window is never abandoned".
- Accept when mean slot price `<= bar`, or when `force`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_calibration.py`:

```python
from custom_components.anker_x1_smartgrid.models import Config, PriceSlot


def _slots(start, prices, minutes=60):
    return [
        PriceSlot(start=start + timedelta(minutes=minutes * i), price=p, duration_min=minutes)
        for i, p in enumerate(prices)
    ]


CFG = Config(
    capacity_kwh=20.0,
    max_charge_w=12000.0,
    eta_charge=0.92,
    calibration_top_soc=97.0,
    calibration_dwell_h=2.0,
)


def test_price_percentile_over_all_slot_prices():
    hist = {"2026-08-01": {"0": 0.10, "1": 0.20}, "2026-08-02": {"0": 0.30, "1": 0.40}}
    assert calibration.price_percentile(hist, 50.0) == 0.25
    assert calibration.price_percentile({}, 50.0) is None


def test_selects_cheapest_window_and_requires_the_bar():
    # soc 87 -> 97 = 2 kWh at ~11 kW effective ≈ 0.18 h charge + 2 h dwell.
    slots = _slots(BASE, [0.40, 0.40, 0.05, 0.05, 0.05, 0.40])
    win = calibration.select_window(BASE, 87.0, slots, cfg=CFG, bar=0.10, force=False)
    assert win is not None
    assert win[0] == BASE + timedelta(hours=2)
    # Same slots, an unreachable bar: no window.
    assert calibration.select_window(BASE, 87.0, slots, cfg=CFG, bar=0.01, force=False) is None


def test_force_ignores_the_bar():
    slots = _slots(BASE, [0.40, 0.40, 0.40, 0.40])
    win = calibration.select_window(BASE, 87.0, slots, cfg=CFG, bar=0.01, force=True)
    assert win is not None


def test_no_bar_means_only_force_can_fire():
    slots = _slots(BASE, [0.05, 0.05, 0.05, 0.05])
    assert calibration.select_window(BASE, 87.0, slots, cfg=CFG, bar=None, force=False) is None
    assert calibration.select_window(BASE, 87.0, slots, cfg=CFG, bar=None, force=True) is not None


def test_does_not_skip_today_for_a_cheaper_tomorrow():
    """The 13:00 publication of tomorrow's prices must not pull a cycle off an
    already-qualifying today window."""
    slots = _slots(BASE, [0.20] * 4) + _slots(BASE + timedelta(days=1), [0.01] * 4)
    win = calibration.select_window(BASE, 87.0, slots, cfg=CFG, bar=0.30, force=False)
    assert win is not None
    assert win[0].date() == BASE.date()


def test_cheapest_window_within_the_day_wins():
    """One candidate per start-date, and it is that date's cheapest."""
    slots = _slots(BASE, [0.50, 0.05, 0.05, 0.05, 0.01, 0.01, 0.01, 0.50])
    win = calibration.select_window(BASE, 87.0, slots, cfg=CFG, bar=0.30, force=False)
    assert win is not None
    assert win[0] == BASE + timedelta(hours=4)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_calibration.py -v`
Expected: FAIL — `AttributeError: module 'calibration' has no attribute 'price_percentile'`

- [ ] **Step 3: Implement**

Append to `calibration.py`:

```python
def price_percentile(price_history: dict[str, dict[str, float]], pct: float) -> float | None:
    """Linear-interpolated percentile of every slot price in the history ring.

    Returns None for an empty history — the caller must then refuse the
    percentile path and let only the deadline path fire.
    """
    values = sorted(v for day in price_history.values() for v in day.values())
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    pos = (pct / 100.0) * (len(values) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (pos - lo)


def _charge_h(soc_pct: float, cfg) -> float:
    """Hours to lift SoC from ``soc_pct`` to the calibration top, at max rate."""
    gap_kwh = max(0.0, (cfg.calibration_top_soc - soc_pct) / 100.0 * cfg.capacity_kwh)
    rate_kw = cfg.max_charge_w / 1000.0 * cfg.eta_charge_safe()
    if rate_kw <= 0.0:
        return 0.0
    return gap_kwh / rate_kw


def select_window(
    now: datetime,
    soc_pct: float,
    slots: list,
    *,
    cfg,
    bar: float | None,
    force: bool,
) -> tuple[datetime, datetime] | None:
    """Cheapest acceptable contiguous window, or None.

    Deterministic in (now, slots, soc, cfg, bar, force): published prices do
    not change within a day, so re-running each tick yields the same answer
    and no commitment needs storing.
    """
    if not slots:
        return None
    need_h = _charge_h(soc_pct, cfg) + cfg.calibration_dwell_h
    ordered = sorted(slots, key=lambda s: s.start)
    slot_h = (ordered[0].duration_min or 60.0) / 60.0
    if slot_h <= 0.0:
        return None
    n = max(1, int(need_h / slot_h + 0.999999))
    if n > len(ordered):
        return None

    # Build candidates: contiguous runs of n slots that have not fully elapsed.
    candidates: list[tuple[float, datetime, datetime]] = []
    for i in range(len(ordered) - n + 1):
        block = ordered[i : i + n]
        start = block[0].start
        end = start + timedelta(hours=slot_h * n)
        if end <= now:
            continue
        mean_price = sum(s.price for s in block) / n
        candidates.append((mean_price, start, end))
    if not candidates:
        return None

    # One candidate per local start-date (the cheapest) => one attempt per day.
    per_day: dict[object, tuple[float, datetime, datetime]] = {}
    for cand in candidates:
        key = cand[1].date()
        if key not in per_day or cand[0] < per_day[key][0]:
            per_day[key] = cand

    # Earliest date with an acceptable window wins.  Taking the EARLIEST rather
    # than the globally cheapest is what stops the 13:00 publication of
    # tomorrow's prices from pulling a cycle off a today window that already
    # qualified — the "never abandon a started window" rule, expressed without
    # storing any commitment.
    for key in sorted(per_day):
        mean_price, start, end = per_day[key]
        if force or (bar is not None and mean_price <= bar):
            return (start, end)
    return None
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_calibration.py -v`
Expected: PASS (13 tests)

- [ ] **Step 5: Commit**

```bash
bunx ruff check custom_components tests && bunx ruff format custom_components tests
git add custom_components/anker_x1_smartgrid/calibration.py tests/test_calibration.py
git commit -m "feat(calibration): price bar and window selection"
```

---

### Task 5: `calibration_action` orchestration

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/calibration.py`
- Test: `tests/test_calibration.py` (append)

**Interfaces:**
- Consumes: everything from Tasks 3–4.
- Produces: `calibration_action(now, soc_pct, slots, soc_samples, price_history, cfg) -> CalibAction | None`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_calibration.py`:

```python
from custom_components.anker_x1_smartgrid import const

ON = Config(
    capacity_kwh=20.0,
    max_charge_w=12000.0,
    eta_charge=0.92,
    calibration_enabled=True,
    calibration_interval_days=5,
    calibration_top_soc=97.0,
    calibration_dwell_h=2.0,
)
CHEAP_HISTORY = {"2026-07-30": {str(h): 0.30 for h in range(24)}}


def _stale_history(now, days):
    """SoC series spanning exactly `days`, never reaching top_soc.

    With no qualifying run, days_since == the series span, so this controls
    the policy's notion of "days since last success" directly.
    """
    start = now - timedelta(days=days)
    return _series(start, 60, [50.0] * (int(days * 24) + 1))


def test_disabled_is_always_none():
    now = BASE
    assert (
        calibration.calibration_action(
            now, 50.0, _slots(now, [0.01] * 6), _stale_history(now, 30), CHEAP_HISTORY, Config()
        )
        is None
    )


def test_not_due_inside_the_interval():
    now = BASE
    recent = _series(now - timedelta(days=1), 15, [98.0] * 13)
    assert (
        calibration.calibration_action(
            now, 50.0, _slots(now, [0.01] * 6), recent, CHEAP_HISTORY, ON
        )
        is None
    )


def test_due_and_cheap_returns_charging():
    now = BASE
    slots = _slots(now, [0.01] * 6)
    act = calibration.calibration_action(
        now, 50.0, slots, _stale_history(now, 30), CHEAP_HISTORY, ON
    )
    assert act is not None
    assert act.phase == "charging"
    assert act.window_start <= now < act.window_end


def test_at_top_soc_reports_holding():
    now = BASE
    slots = _slots(now, [0.01] * 6)
    act = calibration.calibration_action(
        now, 98.0, slots, _stale_history(now, 30), CHEAP_HISTORY, ON
    )
    assert act is not None
    assert act.phase == "holding"


def test_holds_through_even_without_a_cheap_window():
    """A dwell in progress must complete regardless of the price curve."""
    now = BASE
    act = calibration.calibration_action(
        now, 98.0, _slots(now, [0.90] * 6), _stale_history(now, 6), CHEAP_HISTORY, ON
    )
    assert act is not None
    assert act.phase == "holding"


def test_fresh_install_short_history_is_idle():
    """No qualifying run AND too little history => idle, never 'charge now'."""
    now = BASE
    short = _series(now - timedelta(hours=6), 15, [50.0] * 24)
    assert (
        calibration.calibration_action(
            now, 50.0, _slots(now, [0.01] * 6), short, CHEAP_HISTORY, ON
        )
        is None
    )


def test_empty_soc_history_is_idle():
    now = BASE
    assert (
        calibration.calibration_action(now, 50.0, _slots(now, [0.01] * 6), [], CHEAP_HISTORY, ON)
        is None
    )


def test_empty_price_history_blocks_percentile_but_not_deadline():
    now = BASE
    slots = _slots(now, [0.90] * 6)
    just_due = _stale_history(now, ON.calibration_interval_days + 1)
    assert calibration.calibration_action(now, 50.0, slots, just_due, {}, ON) is None
    past_grace = _stale_history(
        now, ON.calibration_interval_days + const.CALIBRATION_GRACE_DAYS + 1
    )
    assert calibration.calibration_action(now, 50.0, slots, past_grace, {}, ON) is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_calibration.py -v`
Expected: FAIL — `AttributeError: module 'calibration' has no attribute 'calibration_action'`

- [ ] **Step 3: Implement**

Append to `calibration.py` (add `from . import const` to the imports at the top of the file):

```python
def calibration_action(
    now: datetime,
    soc_pct: float,
    slots: list,
    soc_samples: list[tuple[str, float]],
    price_history: dict[str, dict[str, float]],
    cfg,
) -> CalibAction | None:
    """Whether a calibration cycle is running in the slot containing ``now``.

    Fail-closed: absent or too-short history yields None rather than
    "never calibrated, charge now".
    """
    if not cfg.calibration_enabled:
        return None

    last = last_success_end(
        soc_samples, top_soc=cfg.calibration_top_soc, dwell_h=cfg.calibration_dwell_h
    )
    if last is None:
        # No qualifying dwell.  Only treat that as "overdue" once the series is
        # long enough to have shown one — otherwise a fresh install charges.
        if history_span_days(soc_samples) < cfg.calibration_interval_days:
            return None
        days_since = history_span_days(soc_samples)
    else:
        days_since = (now - last).total_seconds() / 86400.0

    if days_since < cfg.calibration_interval_days:
        return None

    # Hold-through: once the pack is AT the top and a cycle is due, keep
    # holding until the dwell completes, independent of the window.  The price
    # curve's back-horizon is not guaranteed deep enough to keep re-selecting a
    # window that started hours ago (coordinator.read_price_slots passes the
    # sensor's curve through verbatim), and a stranded half-dwell buys the
    # charge without the balancing it was for.  Ends by itself: the moment the
    # run reaches dwell_h, last_success_end returns and days_since drops to ~0.
    if soc_pct >= cfg.calibration_top_soc:
        return CalibAction(
            phase="holding",
            window_start=now,
            window_end=now + timedelta(hours=cfg.calibration_dwell_h),
        )

    force = days_since >= cfg.calibration_interval_days + const.CALIBRATION_GRACE_DAYS
    bar = price_percentile(price_history, const.CALIBRATION_PRICE_PERCENTILE)
    win = select_window(now, soc_pct, slots, cfg=cfg, bar=bar, force=force)
    if win is None:
        return None

    start, end = win
    if not (start <= now < end):
        return None  # accepted a future window; nothing to do yet

    phase = "holding" if soc_pct >= cfg.calibration_top_soc else "charging"
    return CalibAction(phase=phase, window_start=start, window_end=end)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_calibration.py -v`
Expected: PASS (21 tests)

- [ ] **Step 5: Commit**

```bash
bunx ruff check custom_components tests && bunx ruff format custom_components tests
git add custom_components/anker_x1_smartgrid/calibration.py tests/test_calibration.py
git commit -m "feat(calibration): orchestrate the calibration decision"
```

---

### Task 6: Controller wiring

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/controller.py` — import; `__init__` (near L344); `_tick_impl` between the `_run_compute_decision` call (L1532) and `executor.run_forcing_and_export` (L1630)
- Test: `tests/test_calibration_controller.py`

**Interfaces:**
- Consumes: `calibration.calibration_action`, `recorder.read_soc_samples`.
- Produces: `controller._calibration: CalibAction | None`, read by Task 7.

**Why this is the whole execution mechanism:** `executor.py:107-112` already charges at `min(cfg.max_charge_w, cfg.grid_import_limit_w)` whenever `new_plan.state is FORCING`, and the actuator applies its live-BMS-limit clamp inside `engage_and_charge`. Flipping the state is sufficient; no setpoint plumbing. Both calibration phases actuate identically — once the pack is full the BMS accepts ~0 and house load falls to the grid, which *is* the hold.

- [ ] **Step 1: Write the failing test**

`tests/test_calibration_controller.py`:

```python
"""Calibration override at the controller boundary: isolation + active path."""

import dataclasses
from datetime import timedelta

import pytest

from custom_components.anker_x1_smartgrid import calibration
from tests.helpers import BASE, StubHass, make_controller, seed_valid_inputs


@pytest.mark.asyncio
async def test_disabled_never_consults_the_policy(monkeypatch):
    """calibration_enabled=False => behaviour identical to today."""
    hass = StubHass()
    ctrl, _act = make_controller(hass)
    seed_valid_inputs(hass, soc="50.0")
    called = False

    def _spy(*a, **k):
        nonlocal called
        called = True
        return None

    monkeypatch.setattr(calibration, "calibration_action", _spy)
    result = await ctrl.tick()
    assert called is False, "policy must not even be consulted when disabled"
    assert result["state"] == "passive"


@pytest.mark.asyncio
async def test_active_calibration_forces_charge(monkeypatch):
    """Active calibration flips the plan to FORCING regardless of the DP.

    soc=98 would otherwise decide PASSIVE (see
    test_controller.py::test_tick_forcing_to_passive_calls_release), so a
    FORCING outcome here can only come from the override.
    """
    hass = StubHass()
    ctrl, act = make_controller(hass)
    seed_valid_inputs(hass, soc="98.0")
    ctrl.cfg = dataclasses.replace(ctrl.cfg, calibration_enabled=True)
    action = calibration.CalibAction(
        phase="holding",
        window_start=BASE - timedelta(hours=1),
        window_end=BASE + timedelta(hours=2),
    )
    monkeypatch.setattr(calibration, "calibration_action", lambda *a, **k: action)

    result = await ctrl.tick()

    assert result["state"] == "forcing"
    assert ctrl.last_status["calibration_state"] == "holding"
    assert any(c[0] == "engage_and_charge" for c in act.calls)
```

`Config` is frozen — `dataclasses.replace` is the way to flip the flag. If `make_controller` returns only the controller rather than `(ctrl, act)`, adjust the unpacking; check `tests/helpers.py:262` before writing.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_calibration_controller.py -v`
Expected: FAIL — `AttributeError: 'Controller' object has no attribute '_calibration'`

- [ ] **Step 3: Implement**

Add to `controller.py` imports:

```python
from . import calibration
```

In `Controller.__init__`, beside the other per-tick caches (near L344):

```python
        # Active calibration cycle for this tick (spec 2026-08-03), or None.
        # Published as plan-sensor attrs; not persisted — recomputed each tick
        # from SoC + price history, which is what makes it restart-safe.
        self._calibration: calibration.CalibAction | None = None
        self._calibration_last_success: datetime | None = None
        self._calibration_days_since: float | None = None
```

In `_tick_impl`, after `new_plan, _, deadline, horizon, ... = await self._run_compute_decision(...)` (L1532) and before `executor.run_forcing_and_export` (L1630):

```python
        # ── Periodic full-charge calibration override ──────────────────────
        # Deliberately non-economic, and the ONLY force-charge path left after
        # economic-only charging removed the rest.  Quarantined here rather
        # than expressed as a DP constraint so the parity-gated core is
        # untouched.  See the spec for the stranded-capacity evidence.
        self._calibration = None
        if self.cfg.calibration_enabled:
            try:
                _soc_rows = await self._hass.async_add_executor_job(
                    self._recorder.read_soc_samples,
                    (now - timedelta(days=self.cfg.calibration_interval_days * 3)).isoformat(),
                )
                self._calibration = calibration.calibration_action(
                    now,
                    inputs.soc,
                    slots,
                    _soc_rows,
                    self._price_store.history if self._price_store is not None else {},
                    self.cfg,
                )
            except Exception:
                # Fail-closed: never force a charge on a failed read.
                _LOGGER.warning("Calibration policy failed; skipping this tick", exc_info=True)
                self._calibration = None
        if self._calibration is not None and new_plan.state is not ControllerState.FORCING:
            # state_since moves only on the transition, so the executor's
            # dwell hysteresis still sees a stable timestamp.
            new_plan = dataclasses.replace(new_plan, state=ControllerState.FORCING, state_since=now)
```

Check `dataclasses` and `timedelta` are imported in `controller.py`; add if missing.

- [ ] **Step 3b: Publish calibration into `last_status`**

The plan sensor reads `controller.last_status`, not controller attributes (see
`tests/test_sensor.py::test_plan_sensor_surfaces_export_curve_diagnostics`).
Where `export_curve_covered` / `export_curve_slots` are written into the status
dict, add:

```python
            "calibration_state": (self._calibration.phase if self._calibration is not None else "idle"),
            "calibration_window_start": (
                self._calibration.window_start.isoformat() if self._calibration is not None else None
            ),
            "calibration_window_end": (
                self._calibration.window_end.isoformat() if self._calibration is not None else None
            ),
            "calibration_last_success": (
                self._calibration_last_success.isoformat()
                if self._calibration_last_success is not None
                else None
            ),
            "calibration_days_since": self._calibration_days_since,
```

`_calibration_last_success` and `_calibration_days_since` are set in the same
override block as `_calibration`. Extend that block: after reading `_soc_rows`,
before calling `calibration_action`, add

```python
                self._calibration_last_success = calibration.last_success_end(
                    _soc_rows,
                    top_soc=self.cfg.calibration_top_soc,
                    dwell_h=self.cfg.calibration_dwell_h,
                )
                self._calibration_days_since = (
                    (now - self._calibration_last_success).total_seconds() / 86400.0
                    if self._calibration_last_success is not None
                    else None
                )
```

and initialise both to `None` alongside `self._calibration` in `__init__` and
at the top of the override block.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_calibration_controller.py tests/test_controller.py -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `pytest -q`
Expected: PASS, no regressions. The isolation test is the guard that feature-off changed nothing.

- [ ] **Step 6: Commit**

```bash
bunx ruff check custom_components tests && bunx ruff format custom_components tests
git add custom_components/anker_x1_smartgrid/controller.py tests/test_calibration_controller.py
git commit -m "feat(calibration): wire the override into the controller tick"
```

---

### Task 7: Plan-sensor attributes

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/sensor.py:~329` (`X1PlanSensor.extra_state_attributes`, beside `export_curve_covered`)
- Test: `tests/test_sensor.py` (append)

**Interfaces:**
- Consumes: the `last_status` keys published in Task 6 Step 3b.
- Produces: plan-sensor attrs `calibration_state`, `calibration_window_start`, `calibration_window_end`, `calibration_last_success`, `calibration_days_since`.

Follows the `export_curve_covered` convention exactly: the sensor is a thin
`last_status.get(...)` pass-through, and every key is always present (value
`None` until set).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_sensor.py`, mirroring the two `export_curve` tests above it:

```python
def test_plan_sensor_surfaces_calibration_state():
    from custom_components.anker_x1_smartgrid.sensor import X1PlanSensor

    class _C:
        last_status = {
            "plan": {"horizon": [], "deadline": None, "planned_grid_hours": 0},
            "calibration_state": "holding",
            "calibration_window_start": "2026-08-03T02:00:00+00:00",
            "calibration_window_end": "2026-08-03T04:00:00+00:00",
            "calibration_last_success": "2026-07-29T05:00:00+00:00",
            "calibration_days_since": 5.0,
        }

    attrs = X1PlanSensor(_C(), "e").extra_state_attributes
    assert attrs["calibration_state"] == "holding"
    assert attrs["calibration_window_end"] == "2026-08-03T04:00:00+00:00"
    assert attrs["calibration_days_since"] == 5.0


def test_plan_sensor_calibration_defaults_to_idle():
    """Absent from last_status (feature off / tick never ran) -> idle + Nones."""
    from custom_components.anker_x1_smartgrid.sensor import X1PlanSensor

    class _C:
        last_status = {"plan": {"horizon": [], "deadline": None, "planned_grid_hours": 0}}

    attrs = X1PlanSensor(_C(), "e").extra_state_attributes
    assert attrs["calibration_state"] == "idle"
    assert attrs["calibration_window_start"] is None
    assert attrs["calibration_last_success"] is None
    assert attrs["calibration_days_since"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_sensor.py -k calibration -v`
Expected: FAIL — `KeyError: 'calibration_state'`

- [ ] **Step 3: Implement**

In `sensor.py`, in the plan sensor's attribute dict beside `"export_curve_covered"` (~L329):

```python
            # Calibration policy (spec 2026-08-03): "idle" | "charging" | "holding".
            "calibration_state": self._controller.last_status.get("calibration_state", "idle"),
            "calibration_window_start": self._controller.last_status.get("calibration_window_start"),
            "calibration_window_end": self._controller.last_status.get("calibration_window_end"),
            "calibration_last_success": self._controller.last_status.get("calibration_last_success"),
            "calibration_days_since": self._controller.last_status.get("calibration_days_since"),
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_sensor.py tests/test_calibration_controller.py -v`
Expected: PASS

- [ ] **Step 5: Full suite + commit**

```bash
pytest -q
bunx ruff check custom_components tests && bunx ruff format custom_components tests
git add custom_components/anker_x1_smartgrid/sensor.py tests/test_sensor.py
git commit -m "feat(calibration): expose calibration state on the plan sensor"
```

---

## Verification (manual, after deploy)

1. Deploy to lab. Set `calibration_enabled: true` via the UI options flow (**not** jq — non-`_TUNABLES` keys are wiped on the next UI save).
2. Confirm `sensor.smartgrid_plan` shows `calibration_state`. It should read `idle` until the pack has gone `calibration_interval_days` without a qualifying dwell.
3. When a cycle runs, confirm SoC climbs to ≥97% and stays there for ~2 h with charging engaged, and that `calibration_state` reads `charging` then `holding`, then returns to `idle`.
4. After the next deep discharge, re-run the Wh-per-1%-by-band analysis over HA history. Compare the 10–19% band against the current baseline of **42–76 Wh/1%** (nominal ~190). Recovery toward nominal confirms balancing reclaims the stranded capacity; no change refutes it and makes the planning-floor wave the answer instead.

## Unresolved questions

1. `_charge_h` uses `capacity_kwh = 20`, which we know overstates real energy. The window is therefore wider than needed — harmless, but it is the phantom capacity leaking into the policy. Leave, or de-rate?
2. Task 6 places the override after `_run_compute_decision`, so the published plan/horizon still shows the DP's schedule for calibration slots — display and actuation disagree for the window's duration. Acceptable, or should the horizon be overwritten too?
3. A calibration window that overlaps the evening peak suppresses export for its slots. Windows land overnight/midday in practice so this should be rare — worth an explicit guard, or leave it?
4. `MAX_SAMPLE_GAP_MIN = 15.0` assumes the tick interval stays ~60 s. If sampling ever slows past 15 min, every run breaks and calibration never records a success. Should this derive from the configured tick interval instead?
5. ~~`select_window` stability depends on `slots` retaining past slots.~~ RESOLVED before execution: `coordinator.read_price_slots` passes the sensor curve through verbatim and it does include past slots (`coordinator._slot_covering_now` only makes sense if it does), but the back-horizon depth is not guaranteed. Handled by the hold-through rule in Task 5 rather than by making window selection stickier — the charge phase can restart harmlessly, the dwell cannot.
