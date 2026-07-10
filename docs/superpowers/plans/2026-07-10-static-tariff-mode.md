# Static Tariff Mode + NL Default Removal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an explicit static tariff price source (flat + HP/HC) so an install with no dynamic price integration (France/EDF) can plan, and strip NL-install-specific third-party entity defaults from `DEFAULT_ENTITIES`.

**Architecture:** Read-layer synthesis. A new pure module `tariff.py` turns static config into UTC `PriceSlot`s; `coordinator.read_price_slots()` branches on `price_mode`; `controller._resolve_export_price()` returns the static export constant. Controller/DP/display/pricing_store are untouched. NL defaults are removed from `DEFAULT_ENTITIES`; `ent_pv_power` becomes the Anker-native `usable_pv_power` soft role; every reader of a now-blank key is hardened to degrade to `None`/feature-off.

**Tech Stack:** Python 3.13, Home Assistant custom integration, voluptuous config flow, pytest (`.venv/bin/pytest`, `asyncio_mode = auto`), no new dependencies.

## Global Constraints

- **TDD every step:** write failing test → run to see it fail → minimal impl → run to see pass → commit. One action per step.
- **Test runs are delegated:** engineers MUST NOT run pytest directly — hand each `Run:` command to a **test-runner** subagent (Task tool). The exact command + expected result are stated in every run step.
- **Test runner:** `.venv/bin/pytest <path>::<test> -v` from repo root `/Users/kleist/Sites/x1-smartcharge`. `asyncio_mode = auto` (async test funcs need no decorator; existing files still use `@pytest.mark.asyncio` — match the file you edit).
- **≤5 files per task.** If a task would touch more, it has been split; do not merge them back.
- **Commits:** REQUIRED SKILL `committing` before every `git commit` (Angular Conventional Commits). Stage files **by name** (`git add <path> ...`) — never `git add -A`/`.`.
- **Platform:** Python 3.13+, HA 2026.x. **No new dependencies.**
- **Sensor mode stays byte-identical:** existing config entries keep stored values (no migration). Every change to a shared path must be a no-op when `price_mode == "sensor"` and the entity keys are present.
- **`tariff.py` is pure:** no `homeassistant` imports (stdlib + `.models` only) so it is unit-testable in isolation.
- After code changes, keep the knowledge graph current: `graphify update .` (AST-only, no API cost) — run once at the end of a task batch, not per commit.

## File Structure

**New product files**
- `custom_components/anker_x1_smartgrid/tariff.py` — pure static-tariff synthesis: `parse_offpeak_ranges`, `_in_offpeak`, `_resolution_minutes`, `synth_static_price_slots`.

**Modified product files**
- `custom_components/anker_x1_smartgrid/const.py` — new static config keys/defaults/modes; `ANKER_SOFT_ROLE_SUFFIXES` gains `usable_pv_power`; `DEFAULT_ENTITIES` NL removal + PV_POWER/temp/weather retargeting.
- `custom_components/anker_x1_smartgrid/models.py` — 5 new `Config` fields.
- `custom_components/anker_x1_smartgrid/coordinator.py` — `read_price_slots` static branch + blank-tolerant `.get` on `ent_price` and PV-list keys.
- `custom_components/anker_x1_smartgrid/controller.py` — recorder reads (`ent_price`, `ent_irradiance`) → `.get`; `_resolve_export_price` static branch.
- `custom_components/anker_x1_smartgrid/config_flow.py` — `_schema` `ent_price` default → `""`; options price-source selector + static fields + validation.
- `custom_components/anker_x1_smartgrid/strings.json` + `translations/en.json` — price-section labels/descriptions + 3 error strings.

**New test files**
- `tests/test_tariff.py`, `tests/test_config_static_defaults.py`, `tests/test_controller_static.py`.

**Modified test files**
- `tests/conftest.py` (restore removed ids into `ANKER_TEST_ENTITIES`), `tests/test_const.py`, `tests/test_coordinator.py`, `tests/test_config_flow.py`, `tests/test_anker_resolver.py`.

**Key reference facts (verified against current code)**
- `PriceSlot(start: datetime, price: float, duration_min: float | None = None)` — `models.py:97-101`. Starts are tz-aware UTC.
- `Config.from_dict(d)` filters to dataclass field names (`models.py:91-94`); last field today is `use_measured_eta` (`models.py:89`).
- `Controller.__init__` stores `self._data`, `self._recorder`, and `self.cfg = Config.from_dict(data)` (`controller.py:1148-1158`).
- `coordinator.read_price_slots(hass, data)` (`coordinator.py:53-57`) reads `data[CONF_ENT_PRICE]` state, parses `forecast` attr via `parse_price_curve`.
- `controller._resolve_export_price` (`controller.py:3077-3083`) returns `(float|None, matches_import: bool)`.
- Coordinator imports `from homeassistant.util import dt as dt_util`; `dt_util.utcnow()` (UTC-aware) and `dt_util.DEFAULT_TIME_ZONE` (HA local tzinfo, UTC in tests) both exist.

---

## Task 1: Static config keys + Config fields

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/const.py` (after `CONF_SLOT_RESOLUTION` block ~line 58, and after `DEFAULT_USE_MEASURED_ETA` ~line 194)
- Modify: `custom_components/anker_x1_smartgrid/models.py:89` (append 5 `Config` fields)
- Test: `tests/test_config_static_defaults.py` (create)

**Interfaces:**
- Produces: constants `const.CONF_PRICE_MODE="price_mode"`, `CONF_STATIC_PRICE_IMPORT="static_price_import"`, `CONF_STATIC_PRICE_OFFPEAK="static_price_offpeak"`, `CONF_STATIC_OFFPEAK_HOURS="static_offpeak_hours"`, `CONF_STATIC_PRICE_EXPORT="static_price_export"`; `PRICE_MODE_SENSOR="sensor"`, `PRICE_MODE_STATIC="static"`, `DEFAULT_PRICE_MODE=PRICE_MODE_SENSOR`, `DEFAULT_STATIC_PRICE_IMPORT=0.25`, `DEFAULT_STATIC_PRICE_OFFPEAK=0.0`, `DEFAULT_STATIC_OFFPEAK_HOURS=""`, `DEFAULT_STATIC_PRICE_EXPORT=0.0`. `Config` fields `price_mode:str`, `static_price_import:float`, `static_price_offpeak:float`, `static_offpeak_hours:str`, `static_price_export:float`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_config_static_defaults.py`:

```python
"""Static tariff config keys + Config field defaults."""
from custom_components.anker_x1_smartgrid import const
from custom_components.anker_x1_smartgrid.models import Config


def test_price_mode_constants():
    assert const.CONF_PRICE_MODE == "price_mode"
    assert const.PRICE_MODE_SENSOR == "sensor"
    assert const.PRICE_MODE_STATIC == "static"
    assert const.DEFAULT_PRICE_MODE == const.PRICE_MODE_SENSOR


def test_static_config_key_strings():
    assert const.CONF_STATIC_PRICE_IMPORT == "static_price_import"
    assert const.CONF_STATIC_PRICE_OFFPEAK == "static_price_offpeak"
    assert const.CONF_STATIC_OFFPEAK_HOURS == "static_offpeak_hours"
    assert const.CONF_STATIC_PRICE_EXPORT == "static_price_export"


def test_static_config_defaults():
    cfg = Config()
    assert cfg.price_mode == "sensor"
    assert cfg.static_price_import == 0.25
    assert cfg.static_price_offpeak == 0.0
    assert cfg.static_offpeak_hours == ""
    assert cfg.static_price_export == 0.0


def test_static_config_from_dict_override():
    cfg = Config.from_dict({
        "price_mode": "static",
        "static_price_import": 0.30,
        "static_price_offpeak": 0.12,
        "static_offpeak_hours": "01:00-06:00",
        "static_price_export": 0.10,
    })
    assert cfg.price_mode == "static"
    assert cfg.static_price_import == 0.30
    assert cfg.static_price_offpeak == 0.12
    assert cfg.static_offpeak_hours == "01:00-06:00"
    assert cfg.static_price_export == 0.10
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_config_static_defaults.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'CONF_PRICE_MODE'`.

- [ ] **Step 3: Add the constants**

In `const.py`, immediately after the `CONF_SLOT_RESOLUTION = "slot_resolution"` line (~line 58) add:

```python

# Static tariff mode (price source selection) — France/EDF has no dynamic price
# integration.  price_mode="static" synthesizes slots from these flat/HP-HC values.
CONF_PRICE_MODE = "price_mode"
CONF_STATIC_PRICE_IMPORT = "static_price_import"
CONF_STATIC_PRICE_OFFPEAK = "static_price_offpeak"
CONF_STATIC_OFFPEAK_HOURS = "static_offpeak_hours"
CONF_STATIC_PRICE_EXPORT = "static_price_export"
PRICE_MODE_SENSOR = "sensor"
PRICE_MODE_STATIC = "static"
```

In `const.py`, immediately after the `DEFAULT_USE_MEASURED_ETA = False` line (~line 194) add:

```python

# Static tariff mode defaults.  offpeak price 0.0 = flat-only; export 0.0 = no
# export credit and never mirrors import (mirror = NL salderen assumption).
DEFAULT_PRICE_MODE = PRICE_MODE_SENSOR
DEFAULT_STATIC_PRICE_IMPORT = 0.25
DEFAULT_STATIC_PRICE_OFFPEAK = 0.0
DEFAULT_STATIC_OFFPEAK_HOURS = ""
DEFAULT_STATIC_PRICE_EXPORT = 0.0
```

- [ ] **Step 4: Add the Config fields**

In `models.py`, after the `use_measured_eta: bool = const.DEFAULT_USE_MEASURED_ETA` line (line 89) add:

```python
    # Static tariff mode (price source). price_mode="static" synthesizes slots
    # from these values (see tariff.py); "sensor" keeps the dynamic-sensor path.
    price_mode: str = const.DEFAULT_PRICE_MODE
    static_price_import: float = const.DEFAULT_STATIC_PRICE_IMPORT
    static_price_offpeak: float = const.DEFAULT_STATIC_PRICE_OFFPEAK
    static_offpeak_hours: str = const.DEFAULT_STATIC_OFFPEAK_HOURS
    static_price_export: float = const.DEFAULT_STATIC_PRICE_EXPORT
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_config_static_defaults.py -v`
Expected: PASS (4 tests).

- [ ] **Step 6: Commit**

```bash
git add custom_components/anker_x1_smartgrid/const.py custom_components/anker_x1_smartgrid/models.py tests/test_config_static_defaults.py
git commit -m "feat(config): add static tariff price-mode config keys and Config fields"
```

---

## Task 2: tariff.py — off-peak ranges parser/validator + helpers

**Files:**
- Create: `custom_components/anker_x1_smartgrid/tariff.py`
- Test: `tests/test_tariff.py` (create)

**Interfaces:**
- Produces: `parse_offpeak_ranges(spec: str | None) -> list[tuple[int, int]]` (minutes-of-day pairs; raises `ValueError` on malformed); `_in_offpeak(minute_of_day: int, ranges: list[tuple[int, int]]) -> bool`; `_resolution_minutes(ranges: list[tuple[int, int]]) -> int`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_tariff.py`:

```python
"""Pure static-tariff synthesis (tariff.py)."""
import pytest

from custom_components.anker_x1_smartgrid import tariff


def test_parse_offpeak_ranges_empty():
    assert tariff.parse_offpeak_ranges("") == []
    assert tariff.parse_offpeak_ranges(None) == []
    assert tariff.parse_offpeak_ranges("  ") == []


def test_parse_offpeak_ranges_single():
    assert tariff.parse_offpeak_ranges("01:30-07:30") == [(90, 450)]


def test_parse_offpeak_ranges_multi_and_midnight():
    assert tariff.parse_offpeak_ranges("22:00-06:00, 12:30-14:30") == [(1320, 360), (750, 870)]


@pytest.mark.parametrize("bad", [
    "7-8", "25:00-01:00", "01:60-02:00", "0100-0200",
    "01:00_02:00", "01:00-", "01:00-02:00-03:00", "aa:bb-cc:dd",
])
def test_parse_offpeak_ranges_invalid_raises(bad):
    with pytest.raises(ValueError):
        tariff.parse_offpeak_ranges(bad)


def test_in_offpeak_normal_half_open():
    r = [(90, 450)]  # 01:30-07:30
    assert tariff._in_offpeak(90, r) is True
    assert tariff._in_offpeak(449, r) is True
    assert tariff._in_offpeak(450, r) is False   # end exclusive
    assert tariff._in_offpeak(89, r) is False


def test_in_offpeak_midnight_span():
    r = [(1320, 360)]  # 22:00-06:00
    assert tariff._in_offpeak(1350, r) is True   # 22:30
    assert tariff._in_offpeak(0, r) is True       # 00:00
    assert tariff._in_offpeak(359, r) is True     # 05:59
    assert tariff._in_offpeak(360, r) is False    # 06:00
    assert tariff._in_offpeak(700, r) is False


def test_resolution_minutes_flat_is_60():
    assert tariff._resolution_minutes([]) == 60


def test_resolution_minutes_on_hour_is_60():
    assert tariff._resolution_minutes([(60, 420)]) == 60   # 01:00-07:00


def test_resolution_minutes_half_hour_is_30():
    assert tariff._resolution_minutes([(90, 450)]) == 30   # 01:30-07:30


def test_resolution_minutes_quarter_is_15():
    assert tariff._resolution_minutes([(75, 435)]) == 15   # 01:15-07:15


def test_resolution_minutes_floored_at_15():
    assert tariff._resolution_minutes([(65, 125)]) == 15   # :05 → gcd 5 → floor 15
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_tariff.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'custom_components.anker_x1_smartgrid.tariff'`.

- [ ] **Step 3: Create the module with parser + helpers**

Create `custom_components/anker_x1_smartgrid/tariff.py`:

```python
"""Pure synthetic price-slot generator for static tariff mode.

No Home Assistant imports — unit-testable in isolation.  ``synth_static_price_slots``
(added in a later task) turns a static tariff config (flat, or HP/HC with off-peak
wall-clock ranges) into UTC PriceSlots over a rolling top-of-current-hour →
tomorrow-local-midnight horizon.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import gcd

from .models import Config, PriceSlot


def _parse_hhmm(value: str) -> int:
    """Parse 'HH:MM' → minutes-of-day (0..1439). Raises ValueError if malformed."""
    s = value.strip()
    if s.count(":") != 1:
        raise ValueError(f"time {value!r} must be HH:MM")
    hh, mm = s.split(":")
    if not (hh.isdigit() and mm.isdigit()):
        raise ValueError(f"time {value!r} must be numeric HH:MM")
    h, m = int(hh), int(mm)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"time {value!r} out of range 00:00-23:59")
    return h * 60 + m


def parse_offpeak_ranges(spec: str | None) -> list[tuple[int, int]]:
    """Parse 'HH:MM-HH:MM,...' → list of (start_min, end_min) minutes-of-day.

    Empty/blank → []. start > end means the range wraps midnight (interpreted at
    membership time). Raises ValueError on any malformed token.
    """
    text = (spec or "").strip()
    if not text:
        return []
    ranges: list[tuple[int, int]] = []
    for token in text.split(","):
        part = token.strip()
        if part.count("-") != 1:
            raise ValueError(f"range {token!r} must be HH:MM-HH:MM")
        lo, hi = part.split("-")
        ranges.append((_parse_hhmm(lo), _parse_hhmm(hi)))
    return ranges


def _in_offpeak(minute_of_day: int, ranges: list[tuple[int, int]]) -> bool:
    """True when minute_of_day falls in any half-open (start, end) range.

    start < end: [start, end).  start > end: wraps midnight → [start, 1440) ∪
    [0, end).  start == end: empty.
    """
    for start, end in ranges:
        if start == end:
            continue
        if start < end:
            if start <= minute_of_day < end:
                return True
        elif minute_of_day >= start or minute_of_day < end:
            return True
    return False


def _resolution_minutes(ranges: list[tuple[int, int]]) -> int:
    """Slot width: 60 if every boundary is on the hour, else gcd of the boundary
    minute-of-hour offsets, floored at 15."""
    if not ranges:
        return 60
    g = 60
    for start, end in ranges:
        g = gcd(g, start % 60)
        g = gcd(g, end % 60)
    if g <= 0:
        g = 60
    return max(15, g)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_tariff.py -v`
Expected: PASS (all parametrized cases + helpers).

- [ ] **Step 5: Commit**

```bash
git add custom_components/anker_x1_smartgrid/tariff.py tests/test_tariff.py
git commit -m "feat(tariff): add off-peak ranges parser and slot-resolution helpers"
```

---

## Task 3: tariff.py — synth_static_price_slots

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/tariff.py` (append `synth_static_price_slots`)
- Test: `tests/test_tariff.py` (append)

**Interfaces:**
- Consumes: `Config` fields (`static_price_import`, `static_price_offpeak`, `static_offpeak_hours`) from Task 1; `parse_offpeak_ranges`, `_in_offpeak`, `_resolution_minutes` from Task 2; `PriceSlot` from `models`.
- Produces: `synth_static_price_slots(now: datetime, cfg: Config, tz) -> list[PriceSlot]` — contiguous UTC PriceSlots, `now` UTC-aware, `tz` a tzinfo (HA local).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_tariff.py`:

```python
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from custom_components.anker_x1_smartgrid.models import Config

_UTC = timezone.utc


def _cfg(**kw):
    return Config.from_dict({"price_mode": "static", **kw})


def test_synth_flat_all_import_hourly():
    now = datetime(2026, 7, 10, 14, 30, tzinfo=_UTC)
    slots = tariff.synth_static_price_slots(now, _cfg(static_price_import=0.25), _UTC)
    assert slots[0].start == datetime(2026, 7, 10, 14, 0, tzinfo=_UTC)  # top of hour
    assert slots[-1].start == datetime(2026, 7, 11, 23, 0, tzinfo=_UTC)  # 07-12 00:00 exclusive
    assert len(slots) == 34
    assert all(s.price == 0.25 for s in slots)
    assert all(s.duration_min == 60.0 for s in slots)


def test_synth_horizon_extent_late_evening():
    now = datetime(2026, 7, 10, 23, 30, tzinfo=_UTC)
    slots = tariff.synth_static_price_slots(now, _cfg(static_price_import=0.25), _UTC)
    assert slots[0].start == datetime(2026, 7, 10, 23, 0, tzinfo=_UTC)
    assert slots[-1].start == datetime(2026, 7, 11, 23, 0, tzinfo=_UTC)
    assert len(slots) == 25


def test_synth_hp_hc_hourly():
    now = datetime(2026, 7, 10, 14, 0, tzinfo=_UTC)
    cfg = _cfg(static_price_import=0.30, static_price_offpeak=0.10, static_offpeak_hours="01:00-06:00")
    by = {s.start: s.price for s in tariff.synth_static_price_slots(now, cfg, _UTC)}
    assert by[datetime(2026, 7, 11, 2, 0, tzinfo=_UTC)] == 0.10
    assert by[datetime(2026, 7, 11, 6, 0, tzinfo=_UTC)] == 0.30   # end exclusive
    assert by[datetime(2026, 7, 10, 14, 0, tzinfo=_UTC)] == 0.30


def test_synth_half_hour_resolution():
    now = datetime(2026, 7, 10, 14, 0, tzinfo=_UTC)
    cfg = _cfg(static_price_import=0.30, static_price_offpeak=0.10, static_offpeak_hours="01:30-06:30")
    slots = tariff.synth_static_price_slots(now, cfg, _UTC)
    assert all(s.duration_min == 30.0 for s in slots)
    by = {s.start: s.price for s in slots}
    assert by[datetime(2026, 7, 11, 1, 30, tzinfo=_UTC)] == 0.10
    assert by[datetime(2026, 7, 11, 1, 0, tzinfo=_UTC)] == 0.30
    assert by[datetime(2026, 7, 11, 6, 0, tzinfo=_UTC)] == 0.10
    assert by[datetime(2026, 7, 11, 6, 30, tzinfo=_UTC)] == 0.30


def test_synth_midnight_span_offpeak():
    now = datetime(2026, 7, 10, 14, 0, tzinfo=_UTC)
    cfg = _cfg(static_price_import=0.30, static_price_offpeak=0.10, static_offpeak_hours="22:00-06:00")
    by = {s.start: s.price for s in tariff.synth_static_price_slots(now, cfg, _UTC)}
    assert by[datetime(2026, 7, 10, 23, 0, tzinfo=_UTC)] == 0.10
    assert by[datetime(2026, 7, 11, 0, 0, tzinfo=_UTC)] == 0.10
    assert by[datetime(2026, 7, 11, 5, 0, tzinfo=_UTC)] == 0.10
    assert by[datetime(2026, 7, 11, 6, 0, tzinfo=_UTC)] == 0.30


def test_synth_multi_range():
    now = datetime(2026, 7, 10, 10, 0, tzinfo=_UTC)
    cfg = _cfg(static_price_import=0.30, static_price_offpeak=0.10,
               static_offpeak_hours="01:00-06:00,12:00-14:00")
    by = {s.start: s.price for s in tariff.synth_static_price_slots(now, cfg, _UTC)}
    assert by[datetime(2026, 7, 10, 12, 0, tzinfo=_UTC)] == 0.10
    assert by[datetime(2026, 7, 10, 13, 0, tzinfo=_UTC)] == 0.10
    assert by[datetime(2026, 7, 10, 14, 0, tzinfo=_UTC)] == 0.30
    assert by[datetime(2026, 7, 11, 2, 0, tzinfo=_UTC)] == 0.10


def test_synth_flat_only_when_offpeak_price_zero():
    now = datetime(2026, 7, 10, 14, 0, tzinfo=_UTC)
    cfg = _cfg(static_price_import=0.30, static_price_offpeak=0.0, static_offpeak_hours="01:00-06:00")
    slots = tariff.synth_static_price_slots(now, cfg, _UTC)
    assert all(s.price == 0.30 for s in slots)
    assert all(s.duration_min == 60.0 for s in slots)


def test_synth_invalid_ranges_fall_back_to_flat():
    now = datetime(2026, 7, 10, 14, 0, tzinfo=_UTC)
    cfg = _cfg(static_price_import=0.30, static_price_offpeak=0.10, static_offpeak_hours="25:00-99:00")
    slots = tariff.synth_static_price_slots(now, cfg, _UTC)
    assert all(s.price == 0.30 for s in slots)


def test_synth_dst_spring_forward_contiguous_and_skips_local_02():
    tz = ZoneInfo("Europe/Paris")  # spring-forward 2026-03-29 02:00→03:00
    now = datetime(2026, 3, 28, 12, 0, tzinfo=_UTC)
    cfg = _cfg(static_price_import=0.30, static_price_offpeak=0.10, static_offpeak_hours="01:00-06:00")
    slots = tariff.synth_static_price_slots(now, cfg, tz)
    starts = [s.start for s in slots]
    step = starts[1] - starts[0]
    assert all((starts[i + 1] - starts[i]) == step for i in range(len(starts) - 1))
    op_local_hours = {s.start.astimezone(tz).hour for s in slots if s.price == 0.10}
    assert 2 not in op_local_hours          # 02:00 local does not exist on spring-forward day
    assert {1, 3, 4, 5} <= op_local_hours


def test_synth_dst_fall_back_contiguous_local_02_twice():
    tz = ZoneInfo("Europe/Paris")  # fall-back 2026-10-25 03:00→02:00
    now = datetime(2026, 10, 24, 12, 0, tzinfo=_UTC)
    cfg = _cfg(static_price_import=0.30, static_price_offpeak=0.10, static_offpeak_hours="01:00-06:00")
    slots = tariff.synth_static_price_slots(now, cfg, tz)
    starts = [s.start for s in slots]
    step = starts[1] - starts[0]
    assert all((starts[i + 1] - starts[i]) == step for i in range(len(starts) - 1))
    op_local_2 = [s for s in slots if s.price == 0.10 and s.start.astimezone(tz).hour == 2]
    assert len(op_local_2) >= 2             # 02:00 local occurs twice on fall-back day
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_tariff.py -k synth -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'synth_static_price_slots'`.

- [ ] **Step 3: Append the synthesizer**

Append to `custom_components/anker_x1_smartgrid/tariff.py`:

```python


def synth_static_price_slots(now: datetime, cfg: Config, tz) -> list[PriceSlot]:
    """Synthesize import PriceSlots for static tariff mode.

    Horizon: top of the current local hour → local midnight ending tomorrow
    (00:00 of now.date()+2 days), emitted as a contiguous UTC grid — a uniform
    UTC stride with a local-time price lookup is DST-safe by construction.

    Resolution: 60 min for a flat tariff or all-on-hour off-peak boundaries;
    otherwise gcd of the boundary minute offsets, floored at 15 min.

    Price: ``cfg.static_price_import`` (peak), or ``cfg.static_price_offpeak``
    when the slot's local start time is in an off-peak range.  Off-peak is
    active only when ranges are configured AND static_price_offpeak > 0 (0/unset
    ⇒ flat-only).  An invalid ranges string is treated as flat (config flow
    validates on entry; this guards direct/legacy edits).
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    try:
        ranges = parse_offpeak_ranges(cfg.static_offpeak_hours)
    except ValueError:
        ranges = []
    import_price = cfg.static_price_import
    offpeak_price = cfg.static_price_offpeak
    use_offpeak = bool(ranges) and offpeak_price > 0.0
    step_min = _resolution_minutes(ranges) if use_offpeak else 60

    now_local = now.astimezone(tz)
    start_local = now_local.replace(minute=0, second=0, microsecond=0)
    end_date = now_local.date() + timedelta(days=2)
    end_local = datetime(end_date.year, end_date.month, end_date.day, 0, 0, tzinfo=tz)
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)

    step = timedelta(minutes=step_min)
    slots: list[PriceSlot] = []
    t = start_utc
    while t < end_utc:
        local = t.astimezone(tz)
        minute_of_day = local.hour * 60 + local.minute
        price = (
            offpeak_price
            if use_offpeak and _in_offpeak(minute_of_day, ranges)
            else import_price
        )
        slots.append(PriceSlot(t, price, duration_min=float(step_min)))
        t += step
    return slots
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_tariff.py -v`
Expected: PASS (all tariff tests, including DST).

- [ ] **Step 5: Commit**

```bash
git add custom_components/anker_x1_smartgrid/tariff.py tests/test_tariff.py
git commit -m "feat(tariff): synthesize static tariff price slots over a DST-safe UTC horizon"
```

---

## Task 4: coordinator.read_price_slots — static branch + blank-tolerant ent_price

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/coordinator.py:9-11` (imports) and `:53-57` (`read_price_slots`)
- Test: `tests/test_coordinator.py` (append)

**Interfaces:**
- Consumes: `synth_static_price_slots` (Task 3), `Config` (Task 1), `const.CONF_PRICE_MODE`/`PRICE_MODE_STATIC`/`DEFAULT_PRICE_MODE`.
- Produces: `read_price_slots(hass, data)` unchanged signature; static-mode returns synth slots, sensor-mode unchanged, absent `ent_price` → `[]`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_coordinator.py`:

```python
def test_read_price_slots_static_mode_synth(hass, monkeypatch):
    import datetime as _dt
    from custom_components.anker_x1_smartgrid import coordinator as _coord
    fixed = _dt.datetime(2026, 7, 10, 14, 0, tzinfo=_dt.timezone.utc)
    monkeypatch.setattr(_coord.dt_util, "utcnow", lambda: fixed)
    d = {
        const.CONF_PRICE_MODE: const.PRICE_MODE_STATIC,
        const.CONF_STATIC_PRICE_IMPORT: 0.30,
        const.CONF_STATIC_PRICE_OFFPEAK: 0.10,
        const.CONF_STATIC_OFFPEAK_HOURS: "01:00-06:00",
    }
    slots = coordinator.read_price_slots(hass, d)
    assert slots
    assert slots[0].start == fixed
    assert {round(s.price, 2) for s in slots} == {0.30, 0.10}


def test_read_price_slots_sensor_mode_absent_price_key_returns_empty(hass):
    d = _data()
    d.pop(const.CONF_ENT_PRICE, None)  # sensor mode, no price sensor configured
    assert coordinator.read_price_slots(hass, d) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_coordinator.py -k "static_mode_synth or absent_price_key" -v`
Expected: FAIL — `test_read_price_slots_static_mode_synth` raises `KeyError: 'ent_price'` (current code subscripts `data[CONF_ENT_PRICE]`); `absent_price_key` raises `KeyError` too.

- [ ] **Step 3: Add the import + rewrite read_price_slots**

In `coordinator.py`, change the imports at lines 10-11 from:

```python
from .models import PlantInputs, PriceSlot
from .parsers import parse_price_curve, _parse_dt
```

to:

```python
from .models import Config, PlantInputs, PriceSlot
from .parsers import parse_price_curve, _parse_dt
from .tariff import synth_static_price_slots
```

Replace `read_price_slots` (lines 53-57):

```python
def read_price_slots(hass: HomeAssistant, data: dict) -> list[PriceSlot]:
    state = hass.states.get(data[const.CONF_ENT_PRICE])
    if state is None:
        return []
    return parse_price_curve(state.attributes.get("forecast"))
```

with:

```python
def read_price_slots(hass: HomeAssistant, data: dict) -> list[PriceSlot]:
    # Static tariff mode: synthesize slots from config, ignore any price sensor.
    if data.get(const.CONF_PRICE_MODE, const.DEFAULT_PRICE_MODE) == const.PRICE_MODE_STATIC:
        return synth_static_price_slots(
            dt_util.utcnow(), Config.from_dict(data), dt_util.DEFAULT_TIME_ZONE
        )
    # Sensor mode (default): read the dynamic price sensor's forecast attribute.
    ent = data.get(const.CONF_ENT_PRICE)
    if not ent:
        return []
    state = hass.states.get(ent)
    if state is None:
        return []
    return parse_price_curve(state.attributes.get("forecast"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_coordinator.py -k "static_mode_synth or absent_price_key or test_read_price_slots" -v`
Expected: PASS (static synth, absent-key empty, and the pre-existing `test_read_price_slots` sensor-mode parse).

- [ ] **Step 5: Commit**

```bash
git add custom_components/anker_x1_smartgrid/coordinator.py tests/test_coordinator.py
git commit -m "feat(coordinator): branch read_price_slots on price_mode for static tariff"
```

---

## Task 5: coordinator PV-list readers — blank/missing-key tolerance

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/coordinator.py` (`read_pv_remaining_kwh` :86,:91; `read_pv_tomorrow_kwh` :104,:109; `_read_pv_arrays` :285-286)
- Test: `tests/test_coordinator.py` (append)

**Interfaces:**
- Produces: PV-list readers no longer subscript `data[...]` or `DEFAULT_ENTITIES[...]`; a missing key degrades to `[]` (→ `0.0` sum / `[]` arrays), matching the existing empty-list semantics. Required before Task 8 removes these keys from `DEFAULT_ENTITIES`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_coordinator.py`:

```python
async def test_read_pv_remaining_kwh_key_absent_returns_zero(hass):
    d = _data()
    d.pop(const.CONF_ENT_PV_TODAY, None)
    assert coordinator.read_pv_remaining_kwh(hass, d) == 0.0


async def test_read_pv_tomorrow_kwh_key_absent_returns_zero(hass):
    d = _data()
    d.pop(const.CONF_ENT_PV_TOMORROW, None)
    assert coordinator.read_pv_tomorrow_kwh(hass, d) == 0.0


async def test_read_pv_today_arrays_key_absent_returns_empty(hass):
    d = _data()
    d.pop(const.CONF_ENT_PV_TODAY, None)
    d.pop(const.CONF_ENT_PV_PEAK_TODAY, None)
    assert coordinator.read_pv_today_arrays(hass, d) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_coordinator.py -k "key_absent" -v`
Expected: FAIL — `KeyError: 'ent_pv_today'` (readers subscript `data[CONF_ENT_PV_TODAY]`; `_read_pv_arrays` also subscripts `DEFAULT_ENTITIES[peak_key]`).

- [ ] **Step 3: Harden the readers**

In `read_pv_remaining_kwh`, replace lines 86 and 91:

```python
    for ent in data[const.CONF_ENT_PV_TODAY]:
```
→
```python
    for ent in data.get(const.CONF_ENT_PV_TODAY, []):
```
and
```python
    if not any_available and data[const.CONF_ENT_PV_TODAY]:
```
→
```python
    if not any_available and data.get(const.CONF_ENT_PV_TODAY, []):
```

In `read_pv_tomorrow_kwh`, replace lines 104 and 109:

```python
    for ent in data[const.CONF_ENT_PV_TOMORROW]:
```
→
```python
    for ent in data.get(const.CONF_ENT_PV_TOMORROW, []):
```
and
```python
    if not any_available and data[const.CONF_ENT_PV_TOMORROW]:
```
→
```python
    if not any_available and data.get(const.CONF_ENT_PV_TOMORROW, []):
```

In `_read_pv_arrays`, replace lines 285-286:

```python
    kwh_list = data.get(kwh_key, const.DEFAULT_ENTITIES[kwh_key])
    peak_list = data.get(peak_key, const.DEFAULT_ENTITIES[peak_key])
```
→
```python
    kwh_list = data.get(kwh_key, [])
    peak_list = data.get(peak_key, [])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_coordinator.py -v`
Expected: PASS (new key-absent tests + all pre-existing coordinator tests, incl. `test_read_pv_today_arrays_get_fallback_no_keyerror` whose assertions only check `len == 2` / not-None and stay green with the `[]` fallback).

- [ ] **Step 5: Commit**

```bash
git add custom_components/anker_x1_smartgrid/coordinator.py tests/test_coordinator.py
git commit -m "refactor(coordinator): tolerate missing PV-list config keys (None-degrade)"
```

---

## Task 6: controller — static export price + blank-tolerant recorder reads

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/controller.py:2952-2953` (recorder reads) and `:3077-3083` (`_resolve_export_price`)
- Test: `tests/test_controller_static.py` (create)

**Interfaces:**
- Consumes: `self.cfg.price_mode`, `self.cfg.static_price_export` (Task 1); `const.PRICE_MODE_STATIC`.
- Produces: `_resolve_export_price()` returns `(cfg.static_price_export, False)` in static mode when the constant > 0, else `(None, False)`; sensor mode unchanged. Recorder tolerates absent `ent_price`/`ent_irradiance` (records `None`).

- [ ] **Step 1: Write the failing test**

Create `tests/test_controller_static.py`:

```python
"""Static-tariff wiring in the controller (export price + recorder tolerance)."""
import pytest

from custom_components.anker_x1_smartgrid import controller, const
from tests.test_controller import _StubHass, _make_controller, _seed_valid_inputs, BASE


def test_resolve_export_price_static_constant():
    ctrl, _ = _make_controller(_StubHass(), data_overrides={
        const.CONF_PRICE_MODE: const.PRICE_MODE_STATIC,
        const.CONF_STATIC_PRICE_EXPORT: 0.12,
    })
    assert ctrl._resolve_export_price() == (0.12, False)


def test_resolve_export_price_static_zero_is_none():
    ctrl, _ = _make_controller(_StubHass(), data_overrides={
        const.CONF_PRICE_MODE: const.PRICE_MODE_STATIC,
        const.CONF_STATIC_PRICE_EXPORT: 0.0,
    })
    assert ctrl._resolve_export_price() == (None, False)


def test_resolve_export_price_sensor_mode_unchanged():
    # Default price_mode=sensor, no export entity configured → (None, False).
    ctrl, _ = _make_controller(_StubHass())
    assert ctrl._resolve_export_price() == (None, False)


@pytest.mark.asyncio
async def test_record_sample_tolerates_absent_price_and_irradiance(monkeypatch):
    monkeypatch.setattr(controller.dt_util, "utcnow", lambda: BASE)
    hass = _StubHass()
    ctrl, _ = _make_controller(hass)
    ctrl.enabled = False
    ctrl._data.pop(const.CONF_ENT_PRICE, None)       # new-install shape (post NL removal)
    ctrl._data.pop(const.CONF_ENT_IRRADIANCE, None)
    _seed_valid_inputs(hass, soc="20.0")
    result = await ctrl.tick()
    assert result["reason"] == "disabled"
    assert ctrl._recorder.rows, "a sample row must have been recorded without KeyError"
    row = ctrl._recorder.rows[-1]
    assert row["import_price"] is None
    assert row["irradiance"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_controller_static.py -v`
Expected: FAIL — `test_resolve_export_price_static_constant` asserts `(0.12, False)` but current code reads the export **entity** (returns `(None, False)`); `test_record_sample_tolerates_absent...` raises `KeyError: 'ent_price'`.

- [ ] **Step 3: Harden recorder reads + add static export branch**

In `controller.py`, replace lines 2952-2953:

```python
        import_price = coordinator.read_float(self._hass, self._data[const.CONF_ENT_PRICE])
        irradiance = coordinator.read_float(self._hass, self._data[const.CONF_ENT_IRRADIANCE])
```
with:

```python
        import_price = coordinator.read_float(self._hass, self._data.get(const.CONF_ENT_PRICE, ""))
        irradiance = coordinator.read_float(self._hass, self._data.get(const.CONF_ENT_IRRADIANCE, ""))
```

Replace `_resolve_export_price` (lines 3077-3083):

```python
    def _resolve_export_price(self) -> tuple[float | None, bool]:
        """Live feed-in tariff + whether it points at the same entity as import."""
        export_ent = self._data.get(const.CONF_ENT_EXPORT_PRICE, "")
        import_ent = self._data.get(const.CONF_ENT_PRICE, "")
        price = coordinator.read_float(self._hass, export_ent) if export_ent else None
        matches = bool(export_ent and export_ent == import_ent)
        return price, matches
```

with:

```python
    def _resolve_export_price(self) -> tuple[float | None, bool]:
        """Live feed-in tariff + whether it points at the same entity as import.

        Static tariff mode bypasses the sensor path entirely: it returns the
        configured constant ``static_price_export`` (None when <= 0, i.e. no
        export credit) and never mirrors the import price.
        """
        if self.cfg.price_mode == const.PRICE_MODE_STATIC:
            px = self.cfg.static_price_export
            return (px if px > 0.0 else None), False
        export_ent = self._data.get(const.CONF_ENT_EXPORT_PRICE, "")
        import_ent = self._data.get(const.CONF_ENT_PRICE, "")
        price = coordinator.read_float(self._hass, export_ent) if export_ent else None
        matches = bool(export_ent and export_ent == import_ent)
        return price, matches
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_controller_static.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add custom_components/anker_x1_smartgrid/controller.py tests/test_controller_static.py
git commit -m "feat(controller): static-mode export price and blank-tolerant recorder reads"
```

---

## Task 7: anker_resolver — ent_pv_power as usable_pv_power soft role

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/const.py` (`ANKER_SOFT_ROLE_SUFFIXES` ~line 280-283; `DEFAULT_ENTITIES[CONF_ENT_PV_POWER]` line 238)
- Modify: `tests/test_const.py:13-14`
- Test: `tests/test_anker_resolver.py:12-20` (`_ROLES`) + append

**Interfaces:**
- Produces: `resolve_anker_config` now resolves `CONF_ENT_PV_POWER` from the device (`{entry_id}_usable_pv_power`) as a SOFT role (a miss is omitted, never blocks setup); `DEFAULT_ENTITIES[CONF_ENT_PV_POWER] == "sensor.anker_x1_usable_pv_power"`.

- [ ] **Step 1: Write the failing test**

In `tests/test_anker_resolver.py`, add to the `_ROLES` dict (lines 12-20), after the `"inverter_loss"` entry:

```python
    "usable_pv_power": ("sensor", "anker_x1_usable_pv_power"),
```

Append to `tests/test_anker_resolver.py`:

```python
async def test_resolve_usable_pv_power_soft_role(hass):
    device_id, _ = _register_anker_device(hass)
    resolved, missing = resolve_anker_config(hass, device_id)
    assert missing == []
    assert resolved[const.CONF_ENT_PV_POWER] == "sensor.anker_x1_usable_pv_power"


async def test_resolve_missing_usable_pv_power_is_soft(hass):
    device_id, _ = _register_anker_device(hass, drop=("usable_pv_power",))
    resolved, missing = resolve_anker_config(hass, device_id)
    assert const.CONF_ENT_PV_POWER not in resolved   # miss omitted
    assert const.CONF_ENT_PV_POWER not in missing     # soft: never blocks setup
```

In `tests/test_const.py`, change lines 13-14:

```python
def test_pv_power_entity_default():
    assert const.DEFAULT_ENTITIES[const.CONF_ENT_PV_POWER] == "sensor.solar_power"
```
→
```python
def test_pv_power_entity_default():
    assert const.DEFAULT_ENTITIES[const.CONF_ENT_PV_POWER] == "sensor.anker_x1_usable_pv_power"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_anker_resolver.py -k usable_pv_power tests/test_const.py::test_pv_power_entity_default -v`
Expected: FAIL — resolver returns no `CONF_ENT_PV_POWER` (not a soft role yet); `test_pv_power_entity_default` asserts the new value against the old `"sensor.solar_power"`.

- [ ] **Step 3: Register the soft role + retarget the default**

In `const.py`, change `DEFAULT_ENTITIES` line 238:

```python
    CONF_ENT_PV_POWER: "sensor.solar_power",
```
→
```python
    CONF_ENT_PV_POWER: "sensor.anker_x1_usable_pv_power",
```

In `const.py`, change `ANKER_SOFT_ROLE_SUFFIXES` (lines 280-283):

```python
ANKER_SOFT_ROLE_SUFFIXES: dict[str, str] = {
    CONF_ENT_METER_POWER: "meter_total_power",
    CONF_ENT_INVERTER_LOSS: "inverter_loss",
}
```
→
```python
ANKER_SOFT_ROLE_SUFFIXES: dict[str, str] = {
    CONF_ENT_METER_POWER: "meter_total_power",
    CONF_ENT_INVERTER_LOSS: "inverter_loss",
    # Anker-native usable PV power replaces the NL-specific GoodWe sensor.solar_power
    # default; resolved per-device, soft (a miss falls back to DEFAULT_ENTITIES).
    CONF_ENT_PV_POWER: "usable_pv_power",
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_anker_resolver.py tests/test_const.py -v`
Expected: PASS (new soft-role tests + all pre-existing resolver/const tests).

- [ ] **Step 5: Commit**

```bash
git add custom_components/anker_x1_smartgrid/const.py tests/test_anker_resolver.py tests/test_const.py
git commit -m "feat(const): resolve ent_pv_power from Anker usable_pv_power soft role"
```

---

## Task 8: NL default removal from DEFAULT_ENTITIES

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/const.py` (`DEFAULT_ENT_WEATHER_FORECAST` line 85; `DEFAULT_ENTITIES` lines 237-259)
- Modify: `custom_components/anker_x1_smartgrid/config_flow.py:43` (`_schema` `ent_price` default)
- Modify: `tests/conftest.py:12-18` (restore removed ids into `ANKER_TEST_ENTITIES`)
- Modify: `tests/test_const.py` (weather assertions + removal-lock test)
- Modify: `tests/test_config_flow.py:140` (Config weather default)

**Interfaces:**
- Consumes: hardened readers from Tasks 4/5/6 (so removal cannot KeyError at runtime).
- Produces: `DEFAULT_ENTITIES` no longer carries `ent_price`, `ent_irradiance`, `ent_pv_today/tomorrow/peak_today/peak_tomorrow`; `ent_temp`/`ent_weather_forecast` default to `weather.forecast_home`; `sun.sun`, Anker-device soft-role defaults, and `ent_export_price` are kept. `ANKER_TEST_ENTITIES` restores the removed ids so the many `{**DEFAULT_ENTITIES, **ANKER_TEST_ENTITIES}` test fixtures stay byte-identical.

> **Ordering:** MUST run after Tasks 4, 5, 6 (reader hardening) and Task 7 (PV_POWER default retargeted).

- [ ] **Step 1: Write the failing test**

In `tests/test_const.py`, replace `test_weather_forecast_const` (lines 38-41):

```python
def test_weather_forecast_const():
    assert const.CONF_ENT_WEATHER_FORECAST == "ent_weather_forecast"
    assert const.DEFAULT_ENT_WEATHER_FORECAST == "weather.knmi_home"
    assert const.DEFAULT_ENTITIES[const.CONF_ENT_WEATHER_FORECAST] == "weather.knmi_home"
```
with:

```python
def test_weather_forecast_const():
    assert const.CONF_ENT_WEATHER_FORECAST == "ent_weather_forecast"
    assert const.DEFAULT_ENT_WEATHER_FORECAST == "weather.forecast_home"
    assert const.DEFAULT_ENTITIES[const.CONF_ENT_WEATHER_FORECAST] == "weather.forecast_home"
    assert const.DEFAULT_ENTITIES[const.CONF_ENT_TEMP] == "weather.forecast_home"


def test_default_entities_drops_nl_third_party_ids():
    """NL-install third-party defaults are removed; only Anker-derived + sun kept."""
    for key in (
        const.CONF_ENT_PRICE,
        const.CONF_ENT_IRRADIANCE,
        const.CONF_ENT_PV_TODAY,
        const.CONF_ENT_PV_TOMORROW,
        const.CONF_ENT_PV_PEAK_TODAY,
        const.CONF_ENT_PV_PEAK_TOMORROW,
    ):
        assert key not in const.DEFAULT_ENTITIES
    # Kept:
    assert const.DEFAULT_ENTITIES[const.CONF_ENT_SUN] == "sun.sun"
    assert const.CONF_ENT_EXPORT_PRICE in const.DEFAULT_ENTITIES
    assert const.CONF_ENT_METER_POWER in const.DEFAULT_ENTITIES
    assert const.CONF_ENT_PV_POWER in const.DEFAULT_ENTITIES
```

In `tests/test_config_flow.py`, change `test_weather_forecast_config_default` (lines 138-140):

```python
def test_weather_forecast_config_default():
    cfg = Config()
    assert cfg.ent_weather_forecast == "weather.knmi_home"
```
→
```python
def test_weather_forecast_config_default():
    cfg = Config()
    assert cfg.ent_weather_forecast == "weather.forecast_home"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_const.py::test_weather_forecast_const tests/test_const.py::test_default_entities_drops_nl_third_party_ids tests/test_config_flow.py::test_weather_forecast_config_default -v`
Expected: FAIL — defaults still `weather.knmi_home` and the NL keys still present in `DEFAULT_ENTITIES`.

- [ ] **Step 3: Restore removed ids into the test fixture (keeps the rest of the suite green)**

In `tests/conftest.py`, replace `ANKER_TEST_ENTITIES` (lines 9-18):

```python
# Canonical Anker-role entity ids for tests that build a full config dict.
# These used to live in const.DEFAULT_ENTITIES but are now resolved from the
# picked Anker device at config time; tests stub them here.
ANKER_TEST_ENTITIES = {
    const.CONF_ENT_SETPOINT: "number.anker_x1_battery_setpoint_charge_discharge",
    const.CONF_ENT_ENGAGE: "switch.anker_x1_modbus_control_hand_battery_to_ha_vpp",
    const.CONF_ENT_WORKMODE: "select.anker_x1_work_mode",
    const.CONF_ENT_SOC: "sensor.anker_x1_battery_soc",
    const.CONF_ENT_BATTERY_POWER: "sensor.anker_x1_battery_power",
}
```
with:

```python
# Canonical entity ids for tests that build a full config dict.  The Anker-role
# ids are resolved from the picked device at config time; the price/irradiance/PV
# ids used to live in const.DEFAULT_ENTITIES but were removed as NL-install
# defaults.  Both groups are stubbed here so `{**DEFAULT_ENTITIES,
# **ANKER_TEST_ENTITIES}` fixtures stay byte-identical to pre-removal behaviour.
ANKER_TEST_ENTITIES = {
    const.CONF_ENT_SETPOINT: "number.anker_x1_battery_setpoint_charge_discharge",
    const.CONF_ENT_ENGAGE: "switch.anker_x1_modbus_control_hand_battery_to_ha_vpp",
    const.CONF_ENT_WORKMODE: "select.anker_x1_work_mode",
    const.CONF_ENT_SOC: "sensor.anker_x1_battery_soc",
    const.CONF_ENT_BATTERY_POWER: "sensor.anker_x1_battery_power",
    const.CONF_ENT_PRICE: "sensor.zonneplan_current_electricity_tariff",
    const.CONF_ENT_IRRADIANCE: "sensor.knmi_solar_irradiance",
    const.CONF_ENT_PV_TODAY: ["sensor.home_energy_production_today_remaining"],
    const.CONF_ENT_PV_TOMORROW: ["sensor.home_energy_production_tomorrow"],
    const.CONF_ENT_PV_PEAK_TODAY: ["sensor.home_power_highest_peak_time_today"],
    const.CONF_ENT_PV_PEAK_TOMORROW: ["sensor.home_power_highest_peak_time_tomorrow"],
}
```

- [ ] **Step 4: Apply the const removal + retargeting**

In `const.py`, change `DEFAULT_ENT_WEATHER_FORECAST` (line 85):

```python
DEFAULT_ENT_WEATHER_FORECAST = "weather.knmi_home"
```
→
```python
DEFAULT_ENT_WEATHER_FORECAST = "weather.forecast_home"
```

Replace the whole `DEFAULT_ENTITIES` dict (lines 237-259):

```python
DEFAULT_ENTITIES = {
    CONF_ENT_PV_POWER: "sensor.anker_x1_usable_pv_power",
    CONF_ENT_METER_POWER: "sensor.anker_x1_meter_total_power",
    CONF_ENT_INVERTER_LOSS: "sensor.anker_x1_inverter_loss",
    CONF_ENT_PRICE: "sensor.zonneplan_current_electricity_tariff",
    CONF_ENT_PV_TODAY: [
        "sensor.home_energy_production_today_remaining",
    ],
    CONF_ENT_PV_TOMORROW: [
        "sensor.home_energy_production_tomorrow",
    ],
    CONF_ENT_PV_PEAK_TODAY: [
        "sensor.home_power_highest_peak_time_today",
    ],
    CONF_ENT_PV_PEAK_TOMORROW: [
        "sensor.home_power_highest_peak_time_tomorrow",
    ],
    CONF_ENT_IRRADIANCE: "sensor.knmi_solar_irradiance",
    CONF_ENT_SUN: "sun.sun",
    CONF_ENT_TEMP: "weather.knmi_home",
    CONF_ENT_WEATHER_FORECAST: DEFAULT_ENT_WEATHER_FORECAST,
    CONF_ENT_EXPORT_PRICE: DEFAULT_ENT_EXPORT_PRICE,
}
```
with (NL third-party ids dropped; `ent_temp` retargeted; Anker soft-role defaults + `sun.sun` + `ent_export_price` kept):

```python
DEFAULT_ENTITIES = {
    # Anker-device soft-role fallbacks (also resolved per-device by anker_resolver).
    CONF_ENT_PV_POWER: "sensor.anker_x1_usable_pv_power",
    CONF_ENT_METER_POWER: "sensor.anker_x1_meter_total_power",
    CONF_ENT_INVERTER_LOSS: "sensor.anker_x1_inverter_loss",
    # HA-universal defaults (not NL-specific).
    CONF_ENT_SUN: "sun.sun",
    CONF_ENT_TEMP: DEFAULT_ENT_WEATHER_FORECAST,
    CONF_ENT_WEATHER_FORECAST: DEFAULT_ENT_WEATHER_FORECAST,
    CONF_ENT_EXPORT_PRICE: DEFAULT_ENT_EXPORT_PRICE,
    # NL-install third-party defaults removed (ent_price, ent_irradiance, and the
    # ent_pv_today/tomorrow/peak_* lists): every runtime reader tolerates a
    # blank/missing id (None-degrade); see Tasks 4-6.
}
```

- [ ] **Step 5: Fix the setup-form price default (was DEFAULT_ENTITIES[CONF_ENT_PRICE])**

In `config_flow.py`, change `_schema`'s `ent_price` field (lines 41-44):

```python
            vol.Optional(
                const.CONF_ENT_PRICE,
                default=defaults.get(const.CONF_ENT_PRICE, const.DEFAULT_ENTITIES[const.CONF_ENT_PRICE]),
            ): EntitySelector(EntitySelectorConfig(domain="sensor")),
```
→
```python
            vol.Optional(
                const.CONF_ENT_PRICE,
                default=defaults.get(const.CONF_ENT_PRICE, ""),
            ): EntitySelector(EntitySelectorConfig(domain="sensor")),
```

- [ ] **Step 6: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_const.py tests/test_config_flow.py tests/test_coordinator.py -v`
Expected: PASS. (The restored `ANKER_TEST_ENTITIES` keeps every `{**DEFAULT_ENTITIES, **ANKER_TEST_ENTITIES}` fixture identical; only the pinning tests changed.)

> **Audited breakage set (exhaustive — a codebase sweep confirmed these are the ONLY red assertions after this task, all inside this task's 5 files):** `test_const.py` L14 (fixed in Task 7), L40+L41 (this task, Step 1); `test_config_flow.py` L140 (this task, Step 1); the `config_flow.py:43` eager-`KeyError` (this task, Step 5). The two coordinator PV-peak "fallback" tests (`test_coordinator.py` 358-372, 439-450) stay GREEN — their assertions only check `len == 2` / not-None (docstrings become stale but need no edit). No other test file references `const.DEFAULT_ENTITIES` contents. If, contrary to the audit, a run surfaces a red in a file NOT listed here, STOP and split it into a Task 8b — do not exceed 5 files.

- [ ] **Step 7: Commit**

```bash
git add custom_components/anker_x1_smartgrid/const.py custom_components/anker_x1_smartgrid/config_flow.py tests/conftest.py tests/test_const.py tests/test_config_flow.py
git commit -m "feat(const): drop NL third-party entity defaults; weather.forecast_home as universal default"
```

---

## Task 9: config flow — price-source selector + static fields + validation

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/config_flow.py` (imports; `_options_fields`; `OPTIONS_SECTIONS[SECTION_PRICE]`; `async_step_init` validation)
- Modify: `custom_components/anker_x1_smartgrid/strings.json` (`price_anticipation` section + errors)
- Modify: `custom_components/anker_x1_smartgrid/translations/en.json` (mirror of strings.json)
- Modify: `tests/test_config_flow.py` (section-count bump + validation tests)

**Interfaces:**
- Consumes: `const.CONF_PRICE_MODE`/`CONF_STATIC_*`/`PRICE_MODE_*`/`DEFAULT_*` (Task 1); `tariff.parse_offpeak_ranges` (Task 2).
- Produces: 5 new option fields in the price section; validation errors `static_import_price_required`, `static_offpeak_price_required`, `static_offpeak_hours_invalid`.

- [ ] **Step 1: Write the failing test**

In `tests/test_config_flow.py`, change the section-count assertion in `test_sections_cover_all_option_fields` (line 62):

```python
    assert len(section_keys) == 48
```
→
```python
    assert len(section_keys) == 53
```

Append to `tests/test_config_flow.py`:

```python
def test_options_schema_includes_static_price_fields():
    from custom_components.anker_x1_smartgrid.config_flow import _options_schema
    keys = _flat_keys(_options_schema({}))
    for k in (
        const.CONF_PRICE_MODE,
        const.CONF_STATIC_PRICE_IMPORT,
        const.CONF_STATIC_PRICE_OFFPEAK,
        const.CONF_STATIC_OFFPEAK_HOURS,
        const.CONF_STATIC_PRICE_EXPORT,
    ):
        assert k in keys


async def test_options_static_mode_requires_positive_import(hass):
    entry = await _create_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result2 = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input=_nest({
            const.CONF_PRICE_MODE: const.PRICE_MODE_STATIC,
            const.CONF_STATIC_PRICE_IMPORT: 0.0,
        }),
    )
    assert result2["type"] == "form"
    assert result2["errors"]["base"] == "static_import_price_required"


async def test_options_static_offpeak_hours_must_parse(hass):
    entry = await _create_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result2 = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input=_nest({
            const.CONF_PRICE_MODE: const.PRICE_MODE_STATIC,
            const.CONF_STATIC_PRICE_IMPORT: 0.30,
            const.CONF_STATIC_PRICE_OFFPEAK: 0.10,
            const.CONF_STATIC_OFFPEAK_HOURS: "25:00-07:00",
        }),
    )
    assert result2["type"] == "form"
    assert result2["errors"]["base"] == "static_offpeak_hours_invalid"


async def test_options_static_offpeak_requires_positive_price(hass):
    entry = await _create_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result2 = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input=_nest({
            const.CONF_PRICE_MODE: const.PRICE_MODE_STATIC,
            const.CONF_STATIC_PRICE_IMPORT: 0.30,
            const.CONF_STATIC_PRICE_OFFPEAK: 0.0,
            const.CONF_STATIC_OFFPEAK_HOURS: "01:00-06:00",
        }),
    )
    assert result2["type"] == "form"
    assert result2["errors"]["base"] == "static_offpeak_price_required"


async def test_options_static_mode_valid_saves(hass):
    entry = await _create_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result2 = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input=_nest({
            const.CONF_PRICE_MODE: const.PRICE_MODE_STATIC,
            const.CONF_STATIC_PRICE_IMPORT: 0.30,
            const.CONF_STATIC_PRICE_OFFPEAK: 0.10,
            const.CONF_STATIC_OFFPEAK_HOURS: "01:00-06:00",
        }),
    )
    assert result2["type"] == "create_entry"
    assert entry.options[const.CONF_PRICE_MODE] == const.PRICE_MODE_STATIC
    assert entry.options[const.CONF_STATIC_PRICE_IMPORT] == 0.30
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_options_sensor_mode_ignores_static_fields(hass):
    """Default price_mode=sensor: a zero static_price_import does NOT error."""
    entry = await _create_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result2 = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input=_nest({
            const.CONF_PRICE_MODE: const.PRICE_MODE_SENSOR,
            const.CONF_STATIC_PRICE_IMPORT: 0.0,
        }),
    )
    assert result2["type"] == "create_entry"
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_config_flow.py -k "static or sections_cover" -v`
Expected: FAIL — section count is 48 (not 53); static fields absent; validation errors not produced.

- [ ] **Step 3: Add the tariff import**

In `config_flow.py`, change line 21:

```python
from . import const, forecast_sources
```
→
```python
from . import const, forecast_sources, tariff
```

- [ ] **Step 4: Add the 5 fields to the price section**

In `config_flow.py`, replace `OPTIONS_SECTIONS[SECTION_PRICE]` (lines 161-166):

```python
    SECTION_PRICE: (
        const.CONF_PRICE_HISTORY_DAYS,
        const.CONF_PRICE_BLEND_WEIGHT_TODAY,
        const.CONF_ANTICIPATION_CONFIDENCE_HAIRCUT,
        const.CONF_ANTICIPATION_MARGIN_EUR_PER_KWH,
    ),
```
with:

```python
    SECTION_PRICE: (
        const.CONF_PRICE_MODE,
        const.CONF_STATIC_PRICE_IMPORT,
        const.CONF_STATIC_PRICE_OFFPEAK,
        const.CONF_STATIC_OFFPEAK_HOURS,
        const.CONF_STATIC_PRICE_EXPORT,
        const.CONF_PRICE_HISTORY_DAYS,
        const.CONF_PRICE_BLEND_WEIGHT_TODAY,
        const.CONF_ANTICIPATION_CONFIDENCE_HAIRCUT,
        const.CONF_ANTICIPATION_MARGIN_EUR_PER_KWH,
    ),
```

In `config_flow.py`, inside `_options_fields`'s returned dict, add the following 5 markers immediately before the `vol.Optional(const.CONF_PRICE_HISTORY_DAYS, ...)` entry (line 354):

```python
            vol.Optional(
                const.CONF_PRICE_MODE,
                default=defaults.get(const.CONF_PRICE_MODE, const.DEFAULT_PRICE_MODE),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(value=const.PRICE_MODE_SENSOR, label="Dynamic price sensor"),
                        SelectOptionDict(value=const.PRICE_MODE_STATIC, label="Static tariff (flat / HP-HC)"),
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional(
                const.CONF_STATIC_PRICE_IMPORT,
                default=defaults.get(const.CONF_STATIC_PRICE_IMPORT, const.DEFAULT_STATIC_PRICE_IMPORT),
            ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=2.0)),
            vol.Optional(
                const.CONF_STATIC_PRICE_OFFPEAK,
                default=defaults.get(const.CONF_STATIC_PRICE_OFFPEAK, const.DEFAULT_STATIC_PRICE_OFFPEAK),
            ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=2.0)),
            vol.Optional(
                const.CONF_STATIC_OFFPEAK_HOURS,
                default=defaults.get(const.CONF_STATIC_OFFPEAK_HOURS, const.DEFAULT_STATIC_OFFPEAK_HOURS),
            ): cv.string,
            vol.Optional(
                const.CONF_STATIC_PRICE_EXPORT,
                default=defaults.get(const.CONF_STATIC_PRICE_EXPORT, const.DEFAULT_STATIC_PRICE_EXPORT),
            ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=2.0)),
```

- [ ] **Step 5: Add the validation to the options flow**

In `config_flow.py`, in `X1SmartGridOptionsFlow.async_step_init`, insert the static-mode validation block immediately after the existing `soc_floor >= soc_target` check (after line 503, before the `if not errors:` at line 504):

```python
            if not errors and user_input.get(
                const.CONF_PRICE_MODE, merged.get(const.CONF_PRICE_MODE, const.DEFAULT_PRICE_MODE)
            ) == const.PRICE_MODE_STATIC:
                _imp = user_input.get(
                    const.CONF_STATIC_PRICE_IMPORT,
                    merged.get(const.CONF_STATIC_PRICE_IMPORT, const.DEFAULT_STATIC_PRICE_IMPORT),
                )
                _op_hours = (user_input.get(
                    const.CONF_STATIC_OFFPEAK_HOURS,
                    merged.get(const.CONF_STATIC_OFFPEAK_HOURS, const.DEFAULT_STATIC_OFFPEAK_HOURS),
                ) or "").strip()
                _op_price = user_input.get(
                    const.CONF_STATIC_PRICE_OFFPEAK,
                    merged.get(const.CONF_STATIC_PRICE_OFFPEAK, const.DEFAULT_STATIC_PRICE_OFFPEAK),
                )
                if _imp <= 0:
                    errors["base"] = "static_import_price_required"
                elif _op_hours:
                    if _op_price <= 0:
                        errors["base"] = "static_offpeak_price_required"
                    else:
                        try:
                            tariff.parse_offpeak_ranges(_op_hours)
                        except ValueError:
                            errors["base"] = "static_offpeak_hours_invalid"
```

- [ ] **Step 6: Add strings + translations**

In `strings.json`, replace the whole `price_anticipation` section (lines 121-135) with the expanded section (renamed; all 9 keys in `data` and `data_description`):

```json
          "price_anticipation": {
            "name": "Price source & anticipation",
            "description": "Where prices come from (dynamic sensor or static tariff) and how tomorrow-morning prices are estimated.",
            "data": {
              "price_mode": "Price source",
              "static_price_import": "Static import price (€/kWh)",
              "static_price_offpeak": "Static off-peak price (€/kWh)",
              "static_offpeak_hours": "Off-peak hours",
              "static_price_export": "Static export price (€/kWh)",
              "price_history_days": "Price history depth (days)",
              "price_blend_weight_today": "Today blend weight",
              "anticipation_confidence_haircut": "Anticipation haircut",
              "anticipation_margin_eur_per_kwh": "Anticipation margin (€/kWh)"
            },
            "data_description": {
              "price_mode": "Dynamic price sensor (default) reads the import price sensor above; Static tariff synthesizes prices from the values below (for installs with no dynamic price integration).",
              "static_price_import": "Flat price, or the peak (HP) price when off-peak hours are set. Required in static mode. Default 0.25.",
              "static_price_offpeak": "Off-peak (HC) price. 0 = flat-only (off-peak hours ignored). Default 0.",
              "static_offpeak_hours": "Comma-separated local-time ranges HH:MM-HH:MM (e.g. 01:30-07:30,12:30-14:30); a range may span midnight. Empty = flat.",
              "static_price_export": "Constant feed-in price in static mode. 0 = no export credit. Does not mirror the import price. Default 0.",
              "price_history_days": "Days of realized prices kept for the price prior. Default 8.",
              "price_blend_weight_today": "Weight of today's prices versus same-weekday-last-week in the morning estimate (0–1). Default 0.5.",
              "anticipation_confidence_haircut": "Discount applied to the estimated morning price before comparing with tonight (0–1). Default 0.15.",
              "anticipation_margin_eur_per_kwh": "The morning estimate must beat tonight's price by this much before charging is deferred. Default 0.02."
            }
          },
```

In `strings.json`, add the 3 error strings to `options.error` (lines 184-187) — after `"soc_floor_above_target"`:

```json
    "error": {
      "anker_roles_missing": "The selected device is missing required entities (SoC, battery power, setpoint, work mode or Modbus control). Pick a device from the Anker X1 integration.",
      "soc_floor_above_target": "SoC floor must be below SoC target.",
      "static_import_price_required": "Static mode needs a positive import price.",
      "static_offpeak_price_required": "Set a positive off-peak price, or clear the off-peak hours.",
      "static_offpeak_hours_invalid": "Off-peak hours must be comma-separated HH:MM-HH:MM ranges."
    }
```

Apply the SAME two edits to `translations/en.json` (it must byte-match `strings.json` — enforced by `test_en_json_matches_strings_json`): replace its `price_anticipation` section and its `options.error` block with the identical JSON shown above.

- [ ] **Step 7: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_config_flow.py -v`
Expected: PASS — including `test_sections_cover_all_option_fields` (53), `test_strings_cover_all_option_sections_and_fields`, `test_en_json_matches_strings_json`, and the new static-mode validation tests.

- [ ] **Step 8: Commit**

```bash
git add custom_components/anker_x1_smartgrid/config_flow.py custom_components/anker_x1_smartgrid/strings.json custom_components/anker_x1_smartgrid/translations/en.json tests/test_config_flow.py
git commit -m "feat(config): add price-source selector and static tariff fields to options flow"
```

---

## Task 10: Integration test — static mode end-to-end tick

**Files:**
- Modify: `tests/test_controller_static.py` (append)

**Interfaces:**
- Consumes: all prior tasks. Verifies the spec's integration requirement: static mode with zero price entities → controller ticks, DP runs, plan populates.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_controller_static.py`:

```python
from datetime import timedelta


@pytest.mark.asyncio
async def test_tick_static_mode_zero_price_entities_runs_dp(monkeypatch):
    """Static mode with NO price sensor still ticks (reason ok) and populates a plan."""
    monkeypatch.setattr(controller.dt_util, "utcnow", lambda: BASE)
    hass = _StubHass()
    ctrl, act = _make_controller(hass, data_overrides={
        const.CONF_PRICE_MODE: const.PRICE_MODE_STATIC,
        const.CONF_STATIC_PRICE_IMPORT: 0.30,
        const.CONF_STATIC_PRICE_OFFPEAK: 0.10,
        const.CONF_STATIC_OFFPEAK_HOURS: "01:00-06:00",
        const.CONF_ENT_PRICE: "",          # no dynamic price sensor
        const.CONF_ENT_PV_TODAY: [],
        const.CONF_ENT_PV_TOMORROW: [],
    })
    # Seed plant inputs + sun ONLY — no price forecast entity exists.
    hass.set_state("sensor.soc", "20.0")
    hass.set_state("sensor.meter_power", "0.0")
    hass.set_state("sun.sun", "above_horizon",
                   {"next_setting": (BASE + timedelta(hours=8)).isoformat()})
    hass.set_state("sensor.pv_power", "0.0")
    hass.set_state("sensor.battery_power", "0.0")

    result = await ctrl.tick()

    # NOT failsafe → synth produced slots, all inputs present, DP ran.
    assert result["reason"] == "ok"
    assert ctrl.last_decision, "last_decision must be populated in static mode"
    assert isinstance(ctrl.last_decision["committed_hours"], list)
    # The synthesized horizon carried both tariff levels.
    slots = controller.coordinator.read_price_slots(hass, ctrl._data)
    assert {round(s.price, 2) for s in slots} == {0.30, 0.10}
```

- [ ] **Step 2: Run test to verify it fails then passes**

Run: `.venv/bin/pytest tests/test_controller_static.py::test_tick_static_mode_zero_price_entities_runs_dp -v`
Expected: PASS (all product code from Tasks 1-9 is already in place — this task adds only the end-to-end assertion). If it FAILS with `reason == "failsafe"`, that signals a real regression in an earlier task (synth returned no slots, or a reader raised) — debug the earlier task, do not weaken this assertion.

- [ ] **Step 3: Commit**

```bash
git add tests/test_controller_static.py
git commit -m "test(controller): static tariff mode end-to-end tick with zero price entities"
```

---

## Task 11: Full-suite regression + graph refresh

**Files:** none (verification only).

- [ ] **Step 1: Run the full suite (delegate to test-runner)**

Run: `.venv/bin/pytest -q`
Expected: PASS — the full suite green (baseline ~1688 tests + the new tariff/config/controller tests; no regressions). Sensor-mode paths are byte-identical.

- [ ] **Step 2: If any test fails**

Use `superpowers:systematic-debugging`. Do NOT edit product behaviour to make a test pass unless the failure is a genuine regression; a failing sensor-mode test means the change was not behaviour-preserving.

- [ ] **Step 3: Refresh the knowledge graph**

Run: `graphify update .`
Expected: completes (AST-only, no API cost).

- [ ] **Step 4: Commit (only if the graph output changed)**

```bash
git add graphify-out
git commit -m "chore(graph): refresh knowledge graph after static tariff mode"
```

---

## Notes for the executor

- **Task order is a dependency chain for green-at-every-commit:** 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 → 11. Tasks 4/5/6 (reader hardening) MUST precede Task 8 (removal). Tasks 2+3 (tariff) precede 4 and 9.
- **Export-constant coverage:** the spec lists "export constant" under tariff unit tests, but `synth_static_price_slots` emits import slots only — the export constant lives in `_resolve_export_price`, so it is verified in Task 6 (`test_resolve_export_price_static_constant` / `_static_zero_is_none`).
- **Section placement:** the 5 new keys live in `SECTION_PRICE` (`price_anticipation`, relabelled "Price source & anticipation") alongside the price sensor pickers already in `SECTION_DEVICES`. `price_mode` is options-only; setup keeps the minimal form (its `ent_price` default is now `""`), matching the France rollout ("configure static in options after deploy").
