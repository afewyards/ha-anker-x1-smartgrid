# Frank Energie Price-Source Support — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Feed the HiDiHo01 `frank_energie` sensors (France box) into the planner — dual-shape price parsing, `prices` attribute fallback, and a per-slot export-credit curve — without changing one byte of Zonneplan behaviour.

**Architecture:** Format auto-detect inside the existing parse path. `parse_price_curve` sniffs each entry (`datetime`+`electricity_price` → Zonneplan scaled; `from`+`price` → Frank plain €/kWh), then dedupes by start keep-first (Frank publishes every slot twice) before deriving durations. `coordinator.read_price_slots` tries ordered attribute candidates `("forecast", "prices")`. A new `coordinator.read_export_price_slots` parses the export entity's own curve and the controller threads it as `export_slots` into `compute_decision` → `_dp_select_slots`, adding an all-or-nothing per-slot export-price case ahead of the ratio-scale fallback. 15-min activation is free: deduped slots make `detect_slot_minutes` return 15.

**Tech Stack:** Python 3.12, Home Assistant custom integration, pytest + `pytest_homeassistant_custom_component`, ruff (line-length 120) + pyright via pre-commit.

## Global Constraints

- Zonneplan regression is **byte-identical**: existing `forecast`/`electricity_price` decode, scaling by `const.PRICE_SCALE` (1e7), NaN/inf guard, sort, consecutive-gap `duration_min` derivation all unchanged in outcome.
- Dedupe is **generic** (keep first) — applies to both shapes, protects `detect_slot_minutes` from 0-minute gaps.
- Export priority order, exactly: (1) static mode → flat `static_price_export`; (2) export entity's own per-slot curve; (3) export entity == import entity → reuse import curve; (4) scalar ratio-scale.
- "Covering" for (2) is **all-or-nothing**: the curve must supply a price for every window slot that has an import price; otherwise fall through to (3)/(4) untouched. **No per-slot mixing.**
- **No code gate for 15-min.** `resolve_slot_minutes` auto-detect stays as-is (`resolution.py:19-45`); the change must not break it.
- Test runner on this box: `./.venv/bin/python -m pytest ...` (bare `python` is not on PATH). CI runs `python -m pytest tests/ -v --tb=short`.
- `zip()` in `custom_components/**` must pass `strict=` (ruff B905 is only per-file-ignored for efficiency.py / optimize.py / parsers.py).
- Full suite must stay green after every task: `./.venv/bin/python -m pytest tests/ -q`.
- France rollout steps (Task 8) are **manual / on-box** — never CI.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `custom_components/anker_x1_smartgrid/parsers.py` | per-entry shape decode + dedupe + duration derivation | 1 |
| `custom_components/anker_x1_smartgrid/coordinator.py` | attribute candidate order; import + export curve readers | 2, 3 |
| `custom_components/anker_x1_smartgrid/decision.py` | `_export_window_curve` helper; DP export-price case; v_hi clamp mirror; `export_slots` plumbing | 4, 5 |
| `custom_components/anker_x1_smartgrid/controller.py` | read the export curve per tick, thread it (live + shadow) | 6 |
| `tests/test_parsers_price.py` | parser shapes / dedupe / Zonneplan regression | 1 |
| `tests/test_coordinator.py` | attribute fallback order | 2 |
| `tests/test_coordinator_export_curve.py` (new) | export curve reader | 3 |
| `tests/test_decision_export_curve.py` (new) | DP case + coverage fallback + v_hi clamp | 4, 5 |
| `tests/test_controller_export_curve.py` (new) | controller seam + kwarg forwarding | 6 |
| `tests/test_frank_price_source_e2e.py` (new) | 15-min detection end-to-end pin | 7 |

---

### Task 1: Parser — dual-shape decode + dedupe by start

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/parsers.py:23-47`
- Test: `tests/test_parsers_price.py`

**Interfaces:**
- Consumes: `custom_components.anker_x1_smartgrid.parsers._parse_dt(value) -> datetime | None` (existing, `parsers.py:13`), `models.PriceSlot(start, price, duration_min=None)`.
- Produces: `parse_price_curve(forecast_attr: list[dict] | None) -> list[PriceSlot]` — unchanged signature, now accepting both entry shapes and collapsing duplicate starts keep-first. New private `_decode_price_entry(entry: dict) -> tuple[datetime, float] | None`.

- [ ] **Step 1: Write the failing tests**

Replace the whole of `tests/test_parsers_price.py` with:

```python
from datetime import UTC, datetime

import pytest

from custom_components.anker_x1_smartgrid import parsers


def test_parse_price_curve_scales_and_sorts():
    attr = [
        {"datetime": "2026-06-20T13:00:00.000000Z", "electricity_price": 1471074},
        {"datetime": "2026-06-20T12:00:00.000000Z", "electricity_price": 1300000},
    ]
    slots = parsers.parse_price_curve(attr)
    assert len(slots) == 2
    assert slots[0].start.hour == 12  # sorted ascending
    assert abs(slots[1].price - 0.1471074) < 1e-9
    assert slots[0].start.tzinfo is not None


def test_parse_price_curve_skips_malformed():
    attr = [
        {"datetime": "bad", "electricity_price": 1},
        {"electricity_price": 1},
        {"datetime": "2026-06-20T12:00:00.000000Z"},
        {"datetime": "2026-06-20T12:00:00.000000Z", "electricity_price": 1300000},
    ]
    slots = parsers.parse_price_curve(attr)
    assert len(slots) == 1


def test_parse_price_curve_empty():
    assert parsers.parse_price_curve([]) == []
    assert parsers.parse_price_curve(None) == []


def test_parse_price_curve_drops_non_finite_prices():
    attr = [
        {"datetime": "2026-07-02T10:00:00Z", "electricity_price": "NaN"},
        {"datetime": "2026-07-02T11:00:00Z", "electricity_price": "Infinity"},
        {"datetime": "2026-07-02T12:00:00Z", "electricity_price": 2500000},
    ]
    slots = parsers.parse_price_curve(attr)
    assert len(slots) == 1 and slots[0].price == 0.25


def test_parse_price_curve_zonneplan_regression_durations_and_scale():
    """Zonneplan decode is byte-identical: ÷PRICE_SCALE, 60-min derived durations."""
    attr = [
        {"datetime": f"2026-06-20T{h:02d}:00:00.000000Z", "electricity_price": 1000000 + h}
        for h in range(6)
    ]
    slots = parsers.parse_price_curve(attr)
    assert [s.duration_min for s in slots] == [60.0] * 6
    assert slots[3].price == pytest.approx(1000003 / 1e7)


def test_parse_price_curve_frank_shape():
    """Frank Energie: {from, till, price} — tz-aware ISO, plain EUR/kWh, no scaling."""
    attr = [
        {"from": "2026-07-31T10:00:00+02:00", "till": "2026-07-31T10:15:00+02:00", "price": 0.2461},
        {"from": "2026-07-31T10:15:00+02:00", "till": "2026-07-31T10:30:00+02:00", "price": 0.2312},
    ]
    slots = parsers.parse_price_curve(attr)
    assert len(slots) == 2
    assert slots[0].price == pytest.approx(0.2461)
    assert slots[1].price == pytest.approx(0.2312)
    assert slots[0].start == datetime(2026, 7, 31, 8, 0, tzinfo=UTC)
    assert [s.duration_min for s in slots] == [15.0, 15.0]


def test_parse_price_curve_frank_dedupes_doubled_entries():
    """The integration publishes every slot exactly twice; durations must stay 15."""
    raw = [
        {"from": "2026-07-31T10:00:00+02:00", "price": 0.20},
        {"from": "2026-07-31T10:15:00+02:00", "price": 0.21},
        {"from": "2026-07-31T10:30:00+02:00", "price": 0.22},
    ]
    slots = parsers.parse_price_curve(raw + raw)
    assert [s.price for s in slots] == pytest.approx([0.20, 0.21, 0.22])
    assert [s.duration_min for s in slots] == [15.0, 15.0, 15.0]


def test_parse_price_curve_dedupes_zonneplan_duplicates():
    """Dedupe is generic — it protects the Zonneplan path too."""
    attr = [
        {"datetime": "2026-06-20T12:00:00Z", "electricity_price": 1300000},
        {"datetime": "2026-06-20T12:00:00Z", "electricity_price": 1300000},
        {"datetime": "2026-06-20T13:00:00Z", "electricity_price": 1400000},
    ]
    slots = parsers.parse_price_curve(attr)
    assert len(slots) == 2
    assert [s.duration_min for s in slots] == [60.0, 60.0]


def test_parse_price_curve_frank_skips_malformed_and_non_finite():
    attr = [
        {"from": "bad", "price": 0.2},
        {"from": "2026-07-31T10:00:00+02:00", "price": "NaN"},
        {"from": "2026-07-31T11:00:00+02:00", "price": "Infinity"},
        {"from": "2026-07-31T12:00:00+02:00"},
        {"price": 0.3},
        "not-a-dict",
        {"from": "2026-07-31T13:00:00+02:00", "price": 0.25},
    ]
    slots = parsers.parse_price_curve(attr)
    assert len(slots) == 1 and slots[0].price == pytest.approx(0.25)


def test_parse_price_curve_mixed_shapes_both_decoded():
    """A junk-mixed list keeps every entry it can decode, regardless of shape."""
    attr = [
        {"datetime": "2026-07-31T08:00:00Z", "electricity_price": 2000000},
        {"from": "2026-07-31T09:00:00Z", "price": 0.30},
        {"nonsense": 1},
    ]
    slots = parsers.parse_price_curve(attr)
    assert [s.price for s in slots] == pytest.approx([0.20, 0.30])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_parsers_price.py -q`
Expected: FAIL — `test_parse_price_curve_frank_shape` gets `len(slots) == 0` (the `from`/`price` shape is dropped); `test_parse_price_curve_dedupes_zonneplan_duplicates` gets `len(slots) == 3` with `duration_min == [0.0, 60.0, 60.0]`.

- [ ] **Step 3: Write the implementation**

In `custom_components/anker_x1_smartgrid/parsers.py`, replace `parse_price_curve` (lines 23-47) with:

```python
# Disjoint per-entry key sets — sniffing is unambiguous.
_ZONNEPLAN_START = "datetime"
_ZONNEPLAN_PRICE = "electricity_price"
_FRANK_START = "from"
_FRANK_PRICE = "price"


def _decode_price_entry(entry: dict) -> tuple[datetime, float] | None:
    """Decode one forecast entry to (start_utc, price €/kWh), or None.

    Zonneplan — ``{"datetime": ISO, "electricity_price": int}``, price ÷ PRICE_SCALE.
    Frank Energie — ``{"from": ISO, "price": float}``, price already €/kWh.
    Unrecognised / malformed / non-finite entries return None so the caller skips them.
    """
    if _ZONNEPLAN_START in entry and _ZONNEPLAN_PRICE in entry:
        start = _parse_dt(entry.get(_ZONNEPLAN_START))
        raw = entry.get(_ZONNEPLAN_PRICE)
        scale = const.PRICE_SCALE
    elif _FRANK_START in entry and _FRANK_PRICE in entry:
        start = _parse_dt(entry.get(_FRANK_START))
        raw = entry.get(_FRANK_PRICE)
        scale = 1.0
    else:
        return None
    if start is None or raw is None:
        return None
    try:
        price = float(raw) / scale
    except (ValueError, TypeError):
        return None
    if not math.isfinite(price):
        return None  # "NaN"/"Infinity" parse as float; never let them reach the DP
    return start, price


def parse_price_curve(forecast_attr: list[dict] | None) -> list[PriceSlot]:
    """Map a price-sensor forecast attribute to sorted PriceSlots (price in €/kWh).

    Accepts both the Zonneplan (``datetime``/``electricity_price``, integer-scaled)
    and Frank Energie (``from``/``price``, plain €/kWh) entry shapes; the key sets
    are disjoint so per-entry sniffing is unambiguous.

    Duplicate starts are collapsed **keep-first** — the HiDiHo01 ``frank_energie``
    integration publishes every slot exactly twice.  Without the collapse the
    consecutive-gap duration derivation below yields 0-minute slots and
    ``resolution.detect_slot_minutes`` mis-reads the resolution.  The sort is
    stable, so "first" means first in the source list among equal starts.
    """
    if not forecast_attr:
        return []
    slots: list[PriceSlot] = []
    for entry in forecast_attr:
        if not isinstance(entry, dict):
            continue
        decoded = _decode_price_entry(entry)
        if decoded is None:
            continue
        slots.append(PriceSlot(decoded[0], decoded[1]))
    slots.sort(key=lambda s: s.start)
    deduped: list[PriceSlot] = []
    for s in slots:
        if deduped and s.start == deduped[-1].start:
            continue
        deduped.append(s)
    slots = deduped
    if len(slots) < 2:
        return slots
    durations = [(slots[i + 1].start - slots[i].start).total_seconds() / 60.0 for i in range(len(slots) - 1)]
    durations.append(durations[-1])
    return [PriceSlot(s.start, s.price, duration_min=d) for s, d in zip(slots, durations)]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_parsers_price.py -q`
Expected: PASS (11 tests)

- [ ] **Step 5: Run the full suite (Zonneplan regression gate)**

Run: `./.venv/bin/python -m pytest tests/ -q`
Expected: PASS, no new failures.

- [ ] **Step 6: Commit**

```bash
git add custom_components/anker_x1_smartgrid/parsers.py tests/test_parsers_price.py
git commit -m "feat(parsers): decode Frank Energie price shape and dedupe slot starts"
```

---

### Task 2: Coordinator — ordered price-attribute candidates

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/coordinator.py:107-118`
- Test: `tests/test_coordinator.py`

**Interfaces:**
- Consumes: `parsers.parse_price_curve` (Task 1).
- Produces: module constant `_PRICE_ATTR_CANDIDATES = ("forecast", "prices")` and helper `_parse_price_attrs(state) -> list[PriceSlot]` — both reused by Task 3. `read_price_slots(hass, data) -> list[PriceSlot]` keeps its signature.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_coordinator.py`:

```python
async def test_read_price_slots_reads_frank_prices_attribute(hass):
    """Frank Energie exposes `prices`, not `forecast`."""
    d = _data()
    hass.states.async_set(
        d[const.CONF_ENT_PRICE],
        "0.2461",
        {
            "prices": [
                {"from": "2026-07-31T10:00:00+02:00", "till": "2026-07-31T10:15:00+02:00", "price": 0.2461},
                {"from": "2026-07-31T10:15:00+02:00", "till": "2026-07-31T10:30:00+02:00", "price": 0.2312},
            ]
        },
    )
    slots = coordinator.read_price_slots(hass, d)
    assert [s.price for s in slots] == pytest.approx([0.2461, 0.2312])
    assert slots[0].duration_min == 15.0


async def test_read_price_slots_prefers_forecast_over_prices(hass):
    """Candidate order is fixed: `forecast` wins when both attributes exist."""
    d = _data()
    hass.states.async_set(
        d[const.CONF_ENT_PRICE],
        "0.13",
        {
            "forecast": [{"datetime": "2026-06-20T12:00:00Z", "electricity_price": 1300000}],
            "prices": [{"from": "2026-06-20T12:00:00Z", "price": 0.99}],
        },
    )
    slots = coordinator.read_price_slots(hass, d)
    assert len(slots) == 1
    assert slots[0].price == pytest.approx(0.13)


async def test_read_price_slots_empty_forecast_falls_through_to_prices(hass):
    """First NON-EMPTY parse wins — an empty `forecast` must not shadow `prices`."""
    d = _data()
    hass.states.async_set(
        d[const.CONF_ENT_PRICE],
        "0.30",
        {"forecast": [], "prices": [{"from": "2026-06-20T12:00:00Z", "price": 0.30}]},
    )
    slots = coordinator.read_price_slots(hass, d)
    assert len(slots) == 1 and slots[0].price == pytest.approx(0.30)


async def test_read_price_slots_no_recognised_attribute_returns_empty(hass):
    d = _data()
    hass.states.async_set(d[const.CONF_ENT_PRICE], "0.30", {"unit_of_measurement": "EUR/kWh"})
    assert coordinator.read_price_slots(hass, d) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_coordinator.py -q -k "prices_attribute or prefers_forecast or falls_through"`
Expected: FAIL — all three return `[]` because only `attributes.get("forecast")` is read.

- [ ] **Step 3: Write the implementation**

In `custom_components/anker_x1_smartgrid/coordinator.py`, add above `read_price_slots` (after the `_BAD` constant block is fine; keep it adjacent to `read_price_slots`):

```python
# Ordered price-curve attribute candidates.  "forecast" (Zonneplan) is tried
# before "prices" (Frank Energie) so an entity exposing both keeps its
# historical meaning.  First candidate yielding a NON-EMPTY parse wins.
_PRICE_ATTR_CANDIDATES = ("forecast", "prices")


def _parse_price_attrs(state) -> list[PriceSlot]:
    """Parse the first recognised, non-empty price-curve attribute of `state`."""
    if state is None:
        return []
    try:
        attrs = state.attributes
    except AttributeError:
        return []
    for key in _PRICE_ATTR_CANDIDATES:
        slots = parse_price_curve(attrs.get(key))
        if slots:
            return slots
    return []
```

Then replace the sensor-mode tail of `read_price_slots` (currently lines 111-118) with:

```python
    # Sensor mode (default): read the dynamic price sensor's curve attribute.
    ent = data.get(const.CONF_ENT_PRICE)
    if not ent:
        return []
    return _parse_price_attrs(hass.states.get(ent))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_coordinator.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add custom_components/anker_x1_smartgrid/coordinator.py tests/test_coordinator.py
git commit -m "feat(coordinator): try forecast then prices price attributes"
```

---

### Task 3: Coordinator — export price curve reader

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/coordinator.py` (add function after `read_price_slots`)
- Create: `tests/test_coordinator_export_curve.py`

**Interfaces:**
- Consumes: `_parse_price_attrs(state) -> list[PriceSlot]` and `_PRICE_ATTR_CANDIDATES` (Task 2); `const.CONF_ENT_EXPORT_PRICE = "ent_export_price"`, `const.CONF_ENT_PRICE = "ent_price"`, `const.CONF_PRICE_MODE`, `const.PRICE_MODE_STATIC`, `const.DEFAULT_PRICE_MODE`.
- Produces: `read_export_price_slots(hass: HomeAssistant, data: dict) -> list[PriceSlot]` — returns `[]` (meaning "no curve; keep the legacy scalar paths") for static mode, unconfigured/missing export entity, export entity == import entity, or no recognised attribute.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_coordinator_export_curve.py`:

```python
"""coordinator.read_export_price_slots — per-slot export (feed-in) curve reader."""

import pytest

from custom_components.anker_x1_smartgrid import const, coordinator
from tests.conftest import ANKER_TEST_ENTITIES

FRANK_ALL_IN = "sensor.frank_energie_electricity_prices_current_electricity_price_all_in"
FRANK_MARKET = "sensor.frank_energie_electricity_prices_current_electricity_market_price"

_MARKET_ATTR = {
    "prices": [
        {"from": "2026-07-31T10:00:00+02:00", "till": "2026-07-31T10:15:00+02:00", "price": 0.10},
        {"from": "2026-07-31T10:15:00+02:00", "till": "2026-07-31T10:30:00+02:00", "price": 0.12},
    ]
}


def _data(**overrides):
    d = {**const.DEFAULT_ENTITIES, **ANKER_TEST_ENTITIES}
    d.update(overrides)
    return d


async def test_read_export_price_slots_parses_market_curve(hass):
    d = _data(**{const.CONF_ENT_PRICE: FRANK_ALL_IN, const.CONF_ENT_EXPORT_PRICE: FRANK_MARKET})
    hass.states.async_set(FRANK_MARKET, "0.10", _MARKET_ATTR)
    slots = coordinator.read_export_price_slots(hass, d)
    assert [s.price for s in slots] == pytest.approx([0.10, 0.12])
    assert slots[0].duration_min == 15.0


async def test_read_export_price_slots_empty_when_same_entity_as_import(hass):
    """Same entity → the DP already reuses the import curve; keep that path byte-identical."""
    d = _data(**{const.CONF_ENT_PRICE: FRANK_MARKET, const.CONF_ENT_EXPORT_PRICE: FRANK_MARKET})
    hass.states.async_set(FRANK_MARKET, "0.10", _MARKET_ATTR)
    assert coordinator.read_export_price_slots(hass, d) == []


async def test_read_export_price_slots_empty_in_static_mode(hass):
    d = _data(
        **{
            const.CONF_PRICE_MODE: const.PRICE_MODE_STATIC,
            const.CONF_ENT_PRICE: FRANK_ALL_IN,
            const.CONF_ENT_EXPORT_PRICE: FRANK_MARKET,
        }
    )
    hass.states.async_set(FRANK_MARKET, "0.10", _MARKET_ATTR)
    assert coordinator.read_export_price_slots(hass, d) == []


async def test_read_export_price_slots_empty_when_unconfigured_or_missing(hass):
    assert coordinator.read_export_price_slots(hass, _data()) == []
    d = _data(**{const.CONF_ENT_PRICE: FRANK_ALL_IN, const.CONF_ENT_EXPORT_PRICE: "sensor.ghost"})
    assert coordinator.read_export_price_slots(hass, d) == []


async def test_read_export_price_slots_empty_when_no_curve_attribute(hass):
    d = _data(**{const.CONF_ENT_PRICE: FRANK_ALL_IN, const.CONF_ENT_EXPORT_PRICE: FRANK_MARKET})
    hass.states.async_set(FRANK_MARKET, "0.10", {"unit_of_measurement": "EUR/kWh"})
    assert coordinator.read_export_price_slots(hass, d) == []


async def test_read_export_price_slots_reads_zonneplan_forecast_shape(hass):
    """Shape-agnostic: a `forecast`-shaped export entity works too."""
    d = _data(**{const.CONF_ENT_PRICE: FRANK_ALL_IN, const.CONF_ENT_EXPORT_PRICE: "sensor.other_export"})
    hass.states.async_set(
        "sensor.other_export",
        "0.09",
        {"forecast": [{"datetime": "2026-07-31T08:00:00Z", "electricity_price": 900000}]},
    )
    slots = coordinator.read_export_price_slots(hass, d)
    assert [s.price for s in slots] == pytest.approx([0.09])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_coordinator_export_curve.py -q`
Expected: FAIL — `AttributeError: module 'custom_components.anker_x1_smartgrid.coordinator' has no attribute 'read_export_price_slots'`

- [ ] **Step 3: Write the implementation**

In `custom_components/anker_x1_smartgrid/coordinator.py`, add directly after `read_price_slots`:

```python
def read_export_price_slots(hass: HomeAssistant, data: dict) -> list[PriceSlot]:
    """Per-slot export (feed-in) price curve, or ``[]`` when there is none.

    ``[]`` means "no curve — keep the legacy scalar export paths", and is
    returned when:
      * static tariff mode (the configured constant governs the export credit),
      * ``ent_export_price`` is unset,
      * the export entity IS the import entity (the DP already reuses the import
        curve for that case; returning [] keeps Zonneplan byte-identical),
      * the entity is missing or exposes no recognised price attribute.
    """
    if data.get(const.CONF_PRICE_MODE, const.DEFAULT_PRICE_MODE) == const.PRICE_MODE_STATIC:
        return []
    export_ent = data.get(const.CONF_ENT_EXPORT_PRICE, "")
    if not export_ent:
        return []
    if export_ent == data.get(const.CONF_ENT_PRICE, ""):
        return []
    return _parse_price_attrs(hass.states.get(export_ent))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_coordinator_export_curve.py -q`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add custom_components/anker_x1_smartgrid/coordinator.py tests/test_coordinator_export_curve.py
git commit -m "feat(coordinator): read per-slot export price curve from the export entity"
```

---

### Task 4: Decision — per-slot export curve case in the DP

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/decision.py` (add `_export_window_curve` above `_dp_select_slots` at line ~117; add `export_slots` kw-only param to `_dp_select_slots`; insert the new case in the cascade at lines 287-314)
- Create: `tests/test_decision_export_curve.py`

**Interfaces:**
- Consumes: `resolution.resample_price_map(slots, slot_minutes, *, horizon_end=None) -> dict[datetime, float]` (`resolution.py:105`); `optimize_mod.effective_export_price(raw, cfg) -> float` (`optimize.py:194`); the `_price_valid` list already computed at `decision.py:251`.
- Produces:
  - `_export_window_curve(export_slots: list[PriceSlot] | None, starts: list[datetime], required: list[bool], slot_minutes: int) -> list[float] | None`
  - `_dp_select_slots(..., *, export_slots: list[PriceSlot] | None = None)` — new keyword-only param, default `None` ⇒ byte-identical legacy behaviour.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_decision_export_curve.py`:

```python
"""Per-slot export-price curve (Frank Energie market sensor) in the DP.

Priority under test:
  static → EXPORT CURVE (new) → export entity == import entity → ratio-scale.
Coverage is all-or-nothing: a curve that misses any priced window slot is
ignored entirely (no per-slot mixing).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.anker_x1_smartgrid import const
from custom_components.anker_x1_smartgrid import controller as ctrl_mod
from custom_components.anker_x1_smartgrid import optimize as optimize_mod
from custom_components.anker_x1_smartgrid.models import Config, PlantInputs, PriceSlot

BASE = datetime(2026, 7, 31, 10, 0, tzinfo=UTC)
IMPORT_PRICES = [0.20, 0.30, 0.40, 0.25]
EXPORT_PRICES = [0.05, 0.11, 0.19, 0.07]


def _cfg(**overrides) -> Config:
    return Config.from_dict(
        {
            "capacity_kwh": 10.0,
            "soc_target": 97.0,
            "eta_charge": 0.92,
            "round_trip_eff": 0.85,
            "min_dwell_min": 0,
            "max_charge_w": 6000.0,
            "enable_export": True,
            "export_fee_eur_per_kwh": 0.02,
            "export_min_block_kwh": 0.0,
            **overrides,
        }
    )


def _slots(prices: list[float], *, base: datetime = BASE) -> list[PriceSlot]:
    return [PriceSlot(base + timedelta(hours=i), p, duration_min=60.0) for i, p in enumerate(prices)]


def _run(cfg, monkeypatch, *, export_price, export_slots, matches=False) -> dict:
    """Invoke _dp_select_slots with optimize_grid stubbed; return its kwargs."""
    captured: dict = {}

    def _fake_optimize_grid(*args, **kwargs):
        captured.update(kwargs)
        wl = kwargs["window_len"]
        return {"schedule": [0.0] * wl, "export_schedule": [0.0] * wl, "kwh": 0.0, "eur": 0.0}

    monkeypatch.setattr(optimize_mod, "optimize_grid", _fake_optimize_grid)
    ctrl_mod._dp_select_slots(
        inputs=PlantInputs(soc=80.0, meter_w=0.0, now=BASE),
        slots=_slots(IMPORT_PRICES),
        deadline=BASE + timedelta(hours=len(IMPORT_PRICES)),
        ceiling=0.40,
        cfg=cfg,
        export_price=export_price,
        export_price_matches_import=matches,
        intervals=[],
        export_slots=export_slots,
    )
    return captured


def test_export_curve_beats_ratio_scale(monkeypatch):
    """A covering export curve is used verbatim (minus the fee), not ratio-scaled."""
    cfg = _cfg()
    captured = _run(cfg, monkeypatch, export_price=0.05, export_slots=_slots(EXPORT_PRICES))
    fee = cfg.export_fee_eur_per_kwh
    assert captured["export_price"] == pytest.approx([p - fee for p in EXPORT_PRICES])
    assert captured["feed_in"] == pytest.approx([p - fee for p in EXPORT_PRICES])


def test_no_export_curve_keeps_ratio_scale(monkeypatch):
    """export_slots=None → legacy ratio-scale of the import curve (unchanged)."""
    cfg = _cfg()
    captured = _run(cfg, monkeypatch, export_price=0.05, export_slots=None)
    fee = cfg.export_fee_eur_per_kwh
    ratio = 0.05 / IMPORT_PRICES[0]
    assert captured["export_price"] == pytest.approx([p * ratio - fee for p in IMPORT_PRICES])


def test_partial_export_curve_falls_back_all_or_nothing(monkeypatch):
    """A curve missing a priced window slot is ignored entirely — no mixing."""
    cfg = _cfg()
    partial = _slots(EXPORT_PRICES)[:2]  # only the first 2 of 4 window slots
    captured = _run(cfg, monkeypatch, export_price=0.05, export_slots=partial)
    fee = cfg.export_fee_eur_per_kwh
    ratio = 0.05 / IMPORT_PRICES[0]
    assert captured["export_price"] == pytest.approx([p * ratio - fee for p in IMPORT_PRICES])


def test_static_mode_ignores_export_curve(monkeypatch):
    """Static tariff mode still flat-broadcasts the configured constant."""
    cfg = _cfg(price_mode=const.PRICE_MODE_STATIC)
    captured = _run(cfg, monkeypatch, export_price=0.08, export_slots=_slots(EXPORT_PRICES))
    fee = cfg.export_fee_eur_per_kwh
    assert captured["export_price"] == pytest.approx([0.08 - fee] * len(IMPORT_PRICES))


def test_export_price_none_disables_credit_even_with_curve(monkeypatch):
    """No export entity value → export credit stays a strict no-op."""
    cfg = _cfg()
    captured = _run(cfg, monkeypatch, export_price=None, export_slots=_slots(EXPORT_PRICES))
    assert captured["export_price"] is None
    assert captured["feed_in"] is None


def test_export_curve_at_finer_resolution_than_window(monkeypatch):
    """A 15-min export curve forward-fills onto a 60-min window grid."""
    cfg = _cfg()
    fine = [
        PriceSlot(BASE + timedelta(minutes=15 * i), EXPORT_PRICES[i // 4], duration_min=15.0)
        for i in range(16)
    ]
    captured = _run(cfg, monkeypatch, export_price=0.05, export_slots=fine)
    fee = cfg.export_fee_eur_per_kwh
    assert captured["export_price"] == pytest.approx([p - fee for p in EXPORT_PRICES])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_decision_export_curve.py -q`
Expected: FAIL — `TypeError: _dp_select_slots() got an unexpected keyword argument 'export_slots'`

- [ ] **Step 3: Write the implementation**

3a. In `custom_components/anker_x1_smartgrid/decision.py`, add above `def _dp_select_slots(` (line ~117):

```python
def _export_window_curve(
    export_slots: list[PriceSlot] | None,
    starts: list[datetime],
    required: list[bool],
    slot_minutes: int,
) -> list[float] | None:
    """Raw per-slot export prices aligned to ``starts``, or None.

    All-or-nothing coverage: every position flagged ``required`` (i.e. the window
    slot carries a real import price) must be covered by the export curve;
    otherwise None is returned and the caller keeps the legacy scalar paths.
    Uncovered non-required positions are padded with 0.0, mirroring how
    ``window_price`` pads phantom slots.  The curve is resampled onto the window's
    own grid, so an export sensor at a different resolution than the import
    sensor still lines up (coarse entries forward-fill their declared width).
    """
    if not export_slots:
        return None
    by_start = resolution.resample_price_map(export_slots, slot_minutes)
    out: list[float] = []
    for start, need in zip(starts, required, strict=True):
        price = by_start.get(start)
        if price is None:
            if need:
                return None
            price = 0.0
        out.append(price)
    return out
```

3b. Add the keyword-only parameter to `_dp_select_slots` — append after `overnight_need_kwh: float = 0.0,` in the signature:

```python
    export_slots: list[PriceSlot] | None = None,
```

3c. Replace the export-price cascade (currently `decision.py:287-314`) with:

```python
    # Case 1a — the export entity publishes its OWN per-slot curve (e.g. the
    #   Frank Energie market-price sensor).  Use it verbatim (fee-adjusted):
    #   an all-in import tariff is market + flat surcharges, so ratio-scaling
    #   MULTIPLIES where it should subtract and mis-prices the peaks.
    #   Coverage is all-or-nothing (see _export_window_curve) — a partial curve
    #   is ignored rather than mixed per slot.
    _export_starts = [now_h + h * stride for h in range(window_len)]
    _export_curve = _export_window_curve(export_slots, _export_starts, _price_valid, slot_minutes)
    if export_price is None:
        window_export_price: list[float] = [0.0] * window_len
        feed_in: list[float] | None = None
    elif cfg.price_mode == const.PRICE_MODE_STATIC:
        # Static tariff mode: export_price IS the configured constant
        # (static_price_export), not a live sensor tracking the import curve.
        # Always flat-broadcast it — never ratio-scale by the import curve's
        # shape. Ratio-scaling here would make a fixed export credit swing
        # with an HP/HC import schedule (e.g. 0.30 peak / 0.10 offpeak),
        # mirroring the import curve instead of staying the configured flat
        # constant, which the static-mode spec explicitly forbids.
        eff = optimize_mod.effective_export_price(export_price, cfg)
        window_export_price = [eff] * window_len
        feed_in = [eff] * window_len
    elif _export_curve is not None:
        window_export_price = [optimize_mod.effective_export_price(p, cfg) for p in _export_curve]
        feed_in = list(window_export_price)
    elif export_price_matches_import:
        # Same entity → per-hour export prices == per-hour import prices, less the fee.
        window_export_price = [optimize_mod.effective_export_price(p, cfg) for p in window_price]
        feed_in = list(window_export_price)
    else:
        cur_import = window_price[0] if window_price else 0.0
        if cur_import > 1e-9:
            ratio = export_price / cur_import
            window_export_price = [optimize_mod.effective_export_price(p * ratio, cfg) for p in window_price]
            feed_in = list(window_export_price)
        else:
            eff = optimize_mod.effective_export_price(export_price, cfg)
            window_export_price = [eff] * window_len
            feed_in = [eff] * window_len
```

3d. Update the `Export-credit term` docstring section of `_dp_select_slots` (lines ~164-179) by inserting after the static-mode sentence:

```
    When ``export_slots`` supplies a curve covering every priced window slot,
    those per-slot prices are used verbatim (fee-adjusted) — this outranks both
    the same-entity reuse and the ratio-scale fallback.  Coverage is
    all-or-nothing; a partial curve is discarded, never mixed per slot.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_decision_export_curve.py -q`
Expected: PASS (6 tests)

- [ ] **Step 5: Run the DP/parity regression set**

Run: `./.venv/bin/python -m pytest tests/test_controller_dp.py tests/test_optimize_parity.py tests/test_controller_static.py tests/test_15min_golden.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add custom_components/anker_x1_smartgrid/decision.py tests/test_decision_export_curve.py
git commit -m "feat(decision): use the export entity's own per-slot curve in the DP"
```

---

### Task 5: Decision — mirror the export curve in the overnight v_hi clamp

The `_max_export_dc_value` block (`decision.py:1035-1051`) mirrors the DP's export-price derivation to clamp `water_value_hi`. It must gain the same case, or the clamp is computed from a ratio-scaled curve the DP no longer uses.

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/decision.py` (add `export_slots` param to `compute_decision`; update the v_hi block at 1035-1051; forward `export_slots` in the `_dp_select_slots(...)` call at 1067-1086)
- Test: `tests/test_decision_export_curve.py`

**Interfaces:**
- Consumes: `_export_window_curve(...)` (Task 4); `_dp_select_slots(..., export_slots=...)` (Task 4); `optimize_mod.overnight_terminal_params(*, gap_start, pickup, est_price_by_hour, load_w_by_hod, v_lo, max_export_dc_value, cfg, eta_curve) -> tuple[float, float]`.
- Produces: `compute_decision(..., export_slots: list[PriceSlot] | None = None)` — new keyword param appended after `eta_curve`, default `None` ⇒ legacy behaviour.

- [ ] **Step 1: Write the failing test**

First extend the TOP import block of `tests/test_decision_export_curve.py` (do not import mid-file) so it reads:

```python
from custom_components.anker_x1_smartgrid import const
from custom_components.anker_x1_smartgrid import controller as ctrl_mod
from custom_components.anker_x1_smartgrid import optimize as optimize_mod
from custom_components.anker_x1_smartgrid.controller import compute_decision
from custom_components.anker_x1_smartgrid.forecast import LoadPredictor
from custom_components.anker_x1_smartgrid.models import (
    Config,
    ControllerState,
    PlanState,
    PlantInputs,
    PriceSlot,
)
```

Then append to the end of the file:

```python
# ---------------------------------------------------------------------------
# Overnight v_hi clamp must use the same export curve the DP uses
# ---------------------------------------------------------------------------

_PREDICTOR = LoadPredictor.from_profile({})
_WV_BASE = datetime(2026, 7, 31, 14, 0, tzinfo=UTC)  # 14:00 UTC → horizon crosses midnight
_WV_IMPORT = [0.20, 0.28, 0.30, 0.32, 0.40, 0.45, 0.20, 0.18, 0.15, 0.13]
_WV_EXPORT = [0.06, 0.09, 0.10, 0.11, 0.14, 0.16, 0.07, 0.06, 0.05, 0.04]


def _wv_cfg(**overrides) -> Config:
    return Config.from_dict(
        {
            "capacity_kwh": 10.0,
            "soc_target": 97.0,
            "soc_floor": 5.0,
            "eta_charge": 0.92,
            "round_trip_eff": 0.85,
            "min_dwell_min": 0,
            "max_charge_w": 6000.0,
            "enable_export": True,
            "export_fee_eur_per_kwh": 0.02,
            "export_min_block_kwh": 0.0,
            "cycle_cost_eur_per_kwh": 0.10,
            **overrides,
        }
    )


def _clamp_from_compute_decision(monkeypatch, *, export_slots) -> float:
    """Spy on the terminal builder and return the max_export_dc_value it received."""
    captured: dict = {}

    def _spy_builder(*, max_export_dc_value, v_lo, **_):
        captured["max_export_dc_value"] = max_export_dc_value
        return (v_lo, 0.0)

    monkeypatch.setattr(optimize_mod, "overnight_terminal_params", _spy_builder)
    cfg = _wv_cfg()
    compute_decision(
        PlanState(ControllerState.PASSIVE, _WV_BASE - timedelta(hours=2), ()),
        PlantInputs(soc=80.0, meter_w=0.0, now=_WV_BASE),
        _slots(_WV_IMPORT, base=_WV_BASE),
        0.0,
        _WV_BASE + timedelta(hours=len(_WV_IMPORT)),
        _PREDICTOR,
        None,
        cfg,
        export_price=0.06,
        export_price_matches_import=False,
        export_slots=export_slots,
        _out={},
    )
    return captured["max_export_dc_value"]


def test_v_hi_clamp_uses_export_curve_max(monkeypatch):
    """With a covering export curve, the clamp is max(export curve), not the import max."""
    cfg = _wv_cfg()
    clamp = _clamp_from_compute_decision(monkeypatch, export_slots=_slots(_WV_EXPORT, base=_WV_BASE))
    best_eff = max(optimize_mod.effective_export_price(p, cfg) for p in _WV_EXPORT)
    expected = best_eff * cfg.eta_discharge_static() - cfg.cycle_cost_eur_per_kwh
    assert clamp == pytest.approx(expected)


def test_v_hi_clamp_without_curve_keeps_ratio_scale(monkeypatch):
    """No curve → unchanged legacy ratio-scale clamp."""
    cfg = _wv_cfg()
    clamp = _clamp_from_compute_decision(monkeypatch, export_slots=None)
    ratio = 0.06 / _WV_IMPORT[0]
    best_eff = max(optimize_mod.effective_export_price(p * ratio, cfg) for p in _WV_IMPORT)
    expected = best_eff * cfg.eta_discharge_static() - cfg.cycle_cost_eur_per_kwh
    assert clamp == pytest.approx(expected)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `./.venv/bin/python -m pytest tests/test_decision_export_curve.py -q -k v_hi_clamp`
Expected: FAIL — `TypeError: compute_decision() got an unexpected keyword argument 'export_slots'`

- [ ] **Step 3: Write the implementation**

3a. Add to the `compute_decision` signature (`decision.py:762-788`), after `eta_curve: EfficiencyCurve | None = None,`:

```python
    export_slots: list[PriceSlot] | None = None,
```

3b. Replace the v_hi clamp block (`decision.py:1035-1051`) with:

```python
        if export_price is None:
            _max_export_dc_value = water_value
        else:
            _eta_d = cfg.eta_discharge_static()
            _win_slots = [s for s in slots if now_h <= s.start < horizon_edge]
            _win_prices = [s.price for s in _win_slots]
            # Same span as the DP window ([now, horizon_edge)); every import slot in
            # it is "required", so coverage here matches the DP's own all-or-nothing
            # test.  Partial coverage → None → the legacy branches below.
            _win_export = _export_window_curve(
                export_slots,
                [s.start for s in _win_slots],
                [True] * len(_win_slots),
                slot_minutes,
            )
            if cfg.price_mode == const.PRICE_MODE_STATIC:
                _eff = [optimize_mod.effective_export_price(export_price, cfg)]
            elif _win_export is not None:
                _eff = [optimize_mod.effective_export_price(p, cfg) for p in _win_export]
            elif export_price_matches_import:
                _eff = [optimize_mod.effective_export_price(p, cfg) for p in _win_prices]
            else:
                _cur_import = _win_prices[0] if _win_prices else 0.0
                if _cur_import > 1e-9:
                    _ratio = export_price / _cur_import
                    _eff = [optimize_mod.effective_export_price(p * _ratio, cfg) for p in _win_prices]
                else:
                    _eff = [optimize_mod.effective_export_price(export_price, cfg)]
            _max_export_dc_value = (max(_eff) * _eta_d - cfg.cycle_cost_eur_per_kwh) if _eff else water_value
```

3c. Forward the curve to the DP — in the `_dp_select_slots(` call (`decision.py:1067-1086`), add after `export_price_matches_import=export_price_matches_import,`:

```python
            export_slots=export_slots,
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_decision_export_curve.py tests/test_decision_overnight_terminal.py -q`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `./.venv/bin/python -m pytest tests/ -q`
Expected: PASS, no new failures.

- [ ] **Step 6: Commit**

```bash
git add custom_components/anker_x1_smartgrid/decision.py tests/test_decision_export_curve.py
git commit -m "fix(decision): mirror the export curve in the overnight v_hi clamp"
```

---

### Task 6: Controller — thread the export curve through every tick

**Files:**
- Modify: `custom_components/anker_x1_smartgrid/controller.py` (add `_resolve_export_slots` next to `_resolve_export_price` at line ~1937; add `export_slots` kwarg to `_run_compute_decision` at 880-934; pass it at the shadow call site ~1174-1206 and the live call site ~1343-1396)
- Create: `tests/test_controller_export_curve.py`

**Interfaces:**
- Consumes: `coordinator.read_export_price_slots(hass, data) -> list[PriceSlot]` (Task 3); `compute_decision(..., export_slots=...)` (Task 5).
- Produces: `Controller._resolve_export_slots(self) -> list[PriceSlot]`; `Controller._run_compute_decision(..., export_slots: list[PriceSlot] | None = None)` forwarding `export_slots` to `compute_decision`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_controller_export_curve.py`:

```python
"""Controller wiring for the per-slot export price curve."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.anker_x1_smartgrid import const
from custom_components.anker_x1_smartgrid import controller as ctrl_mod
from custom_components.anker_x1_smartgrid.models import PlantInputs, PriceSlot
from tests.helpers import StubHass, make_controller

BASE = datetime(2026, 7, 31, 10, 0, tzinfo=UTC)

_MARKET_ATTR = {
    "prices": [
        {"from": "2026-07-31T10:00:00+00:00", "till": "2026-07-31T10:15:00+00:00", "price": 0.10},
        {"from": "2026-07-31T10:15:00+00:00", "till": "2026-07-31T10:30:00+00:00", "price": 0.12},
    ]
}


def test_resolve_export_slots_reads_market_curve():
    hass = StubHass()
    hass.set_state("sensor.market", "0.10", _MARKET_ATTR)
    ctrl, _ = make_controller(hass=hass, data_overrides={const.CONF_ENT_EXPORT_PRICE: "sensor.market"})
    slots = ctrl._resolve_export_slots()
    assert [s.price for s in slots] == pytest.approx([0.10, 0.12])


def test_resolve_export_slots_empty_without_export_entity():
    hass = StubHass()
    ctrl, _ = make_controller(hass=hass)
    assert ctrl._resolve_export_slots() == []


def test_resolve_export_slots_empty_in_static_mode():
    hass = StubHass()
    hass.set_state("sensor.market", "0.10", _MARKET_ATTR)
    ctrl, _ = make_controller(
        hass=hass,
        data_overrides={
            const.CONF_ENT_EXPORT_PRICE: "sensor.market",
            const.CONF_PRICE_MODE: const.PRICE_MODE_STATIC,
        },
    )
    assert ctrl._resolve_export_slots() == []


async def test_run_compute_decision_forwards_export_slots(monkeypatch):
    """The curve reaches compute_decision as the `export_slots` kwarg."""
    ctrl, _ = make_controller()
    captured: dict = {}

    def _fake_compute_decision(*args, **kwargs):
        captured.update(kwargs)
        return (ctrl.plan, 0.0, BASE, [], "single-day", [])

    monkeypatch.setattr(ctrl_mod, "compute_decision", _fake_compute_decision)
    curve = [PriceSlot(BASE, 0.10, duration_min=60.0)]
    await ctrl._run_compute_decision(
        ctrl.plan,
        None,
        PlantInputs(soc=50.0, meter_w=0.0, now=BASE),
        [PriceSlot(BASE, 0.20, duration_min=60.0)],
        0.0,
        BASE + timedelta(hours=6),
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        export_price=0.10,
        export_price_matches_import=False,
        temp_by_hour={},
        slot_minutes=60,
        dp_out={},
        export_slots=curve,
    )
    assert captured["export_slots"] is curve


async def test_run_compute_decision_defaults_export_slots_to_none(monkeypatch):
    """Omitting the kwarg keeps the legacy None (parity for existing call paths)."""
    ctrl, _ = make_controller()
    captured: dict = {}

    def _fake_compute_decision(*args, **kwargs):
        captured.update(kwargs)
        return (ctrl.plan, 0.0, BASE, [], "single-day", [])

    monkeypatch.setattr(ctrl_mod, "compute_decision", _fake_compute_decision)
    await ctrl._run_compute_decision(
        ctrl.plan,
        None,
        PlantInputs(soc=50.0, meter_w=0.0, now=BASE),
        [PriceSlot(BASE, 0.20, duration_min=60.0)],
        0.0,
        BASE + timedelta(hours=6),
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        export_price=0.10,
        export_price_matches_import=False,
        temp_by_hour={},
        slot_minutes=60,
        dp_out={},
    )
    assert captured["export_slots"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_controller_export_curve.py -q`
Expected: FAIL — `AttributeError: 'Controller' object has no attribute '_resolve_export_slots'` and `TypeError: _run_compute_decision() got an unexpected keyword argument 'export_slots'`

- [ ] **Step 3: Write the implementation**

3a. Add directly after `_resolve_export_price` (`controller.py:1937-1951`):

```python
    def _resolve_export_slots(self) -> list[PriceSlot]:
        """Per-slot export (feed-in) curve for the DP, or [] when there is none.

        Static mode, an unset export entity, an export entity equal to the import
        entity, or an entity with no recognised price attribute all yield [] —
        the DP then keeps its legacy scalar export-price paths untouched.
        """
        return coordinator.read_export_price_slots(self._hass, self._data)
```

3b. In `_run_compute_decision` (`controller.py:880-906`), add to the keyword-only block after `export_price_matches_import: bool,`:

```python
        export_slots: list[PriceSlot] | None = None,
```

and add to the `kwargs = dict(...)` literal (line ~918), after `export_price_matches_import=export_price_matches_import,`:

```python
            export_slots=export_slots,
```

3c. Shadow call site — after line 1174 (`_shadow_export_price, _shadow_export_matches_import = self._resolve_export_price()`) add:

```python
            _shadow_export_slots = self._resolve_export_slots()
```

and in the `self._run_compute_decision(` call below, after `export_price_matches_import=_shadow_export_matches_import,`:

```python
                        export_slots=_shadow_export_slots,
```

3d. Live call site — after line 1343 (`_export_price, _export_matches_import = self._resolve_export_price()`) add:

```python
        _export_slots = self._resolve_export_slots()
```

and in the `self._run_compute_decision(` call at line ~1373, after `export_price_matches_import=_export_matches_import,`:

```python
            export_slots=_export_slots,
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_controller_export_curve.py -q`
Expected: PASS (5 tests)

- [ ] **Step 5: Run the controller regression set + full suite**

Run: `./.venv/bin/python -m pytest tests/ -q`
Expected: PASS, no new failures.

- [ ] **Step 6: Commit**

```bash
git add custom_components/anker_x1_smartgrid/controller.py tests/test_controller_export_curve.py
git commit -m "feat(controller): thread the export price curve into compute_decision"
```

---

### Task 7: End-to-end pin — doubled 15-min Frank attribute activates native dt=15

**Pin task — no new production code.** Spec §4 says 15-min needs no code change; this task proves it and guards the dedupe against regression. The test is expected to PASS immediately on top of Tasks 1-3; if it fails, one of those tasks is wrong.

**Files:**
- Create: `tests/test_frank_price_source_e2e.py`

**Interfaces:**
- Consumes: `coordinator.read_price_slots` (Task 2), `coordinator.read_export_price_slots` (Task 3), `resolution.resolve_slot_minutes(slots, override) -> int` (`resolution.py:41`), `const.SLOT_RESOLUTION_AUTO = "auto"`.
- Produces: nothing — regression pin only.

- [ ] **Step 1: Write the pin test**

Create `tests/test_frank_price_source_e2e.py`:

```python
"""End-to-end pin: a doubled 15-min Frank Energie attribute drives native dt=15.

Reproduces the live France-box shape verified 2026-07-31: 192 entries / 96
distinct 15-min slots, every slot published exactly twice, tz-aware ISO
datetimes, plain €/kWh floats.
"""

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.anker_x1_smartgrid import const, coordinator, resolution
from tests.conftest import ANKER_TEST_ENTITIES

FRANK_ALL_IN = "sensor.frank_energie_electricity_prices_current_electricity_price_all_in"
FRANK_MARKET = "sensor.frank_energie_electricity_prices_current_electricity_market_price"

_DAY_START = datetime(2026, 7, 31, 0, 0, tzinfo=UTC)


def _frank_prices(offset: float) -> list[dict]:
    """96 distinct 15-min slots, each entry emitted twice (the upstream quirk)."""
    entries = []
    for i in range(96):
        start = _DAY_START + timedelta(minutes=15 * i)
        entries.append(
            {
                "from": start.isoformat(),
                "till": (start + timedelta(minutes=15)).isoformat(),
                "price": round(offset + 0.001 * i, 6),
            }
        )
    return entries + entries


def _data(**overrides):
    d = {**const.DEFAULT_ENTITIES, **ANKER_TEST_ENTITIES}
    d[const.CONF_ENT_PRICE] = FRANK_ALL_IN
    d[const.CONF_ENT_EXPORT_PRICE] = FRANK_MARKET
    d.update(overrides)
    return d


async def test_doubled_15min_attribute_yields_96_slots_at_15_minutes(hass):
    d = _data()
    raw = _frank_prices(0.20)
    assert len(raw) == 192
    hass.states.async_set(FRANK_ALL_IN, "0.20", {"prices": raw})

    slots = coordinator.read_price_slots(hass, d)
    assert len(slots) == 96
    assert [s.duration_min for s in slots] == [15.0] * 96
    assert slots[0].price == pytest.approx(0.20)
    assert slots[-1].price == pytest.approx(0.20 + 0.001 * 95)
    assert resolution.resolve_slot_minutes(slots, const.SLOT_RESOLUTION_AUTO) == 15


async def test_export_market_curve_parses_at_same_resolution(hass):
    d = _data()
    hass.states.async_set(FRANK_ALL_IN, "0.20", {"prices": _frank_prices(0.20)})
    hass.states.async_set(FRANK_MARKET, "0.08", {"prices": _frank_prices(0.08)})

    export_slots = coordinator.read_export_price_slots(hass, d)
    assert len(export_slots) == 96
    assert resolution.resolve_slot_minutes(export_slots, const.SLOT_RESOLUTION_AUTO) == 15
    # Import and export curves share the same slot grid → all-or-nothing coverage holds.
    assert [s.start for s in export_slots] == [s.start for s in coordinator.read_price_slots(hass, d)]
```

- [ ] **Step 2: Run the pin**

Run: `./.venv/bin/python -m pytest tests/test_frank_price_source_e2e.py -q`
Expected: PASS (2 tests). A FAIL here means Task 1's dedupe or Task 2's candidate order is wrong — fix there, not here.

- [ ] **Step 3: Commit**

```bash
git add tests/test_frank_price_source_e2e.py
git commit -m "test(price): pin Frank 15-min detection end to end"
```

---

### Task 8: France rollout (MANUAL / ON-BOX — not CI)

Every step below runs by hand against the France instance (`192.168.33.45`, token `~/Sites/.token-france`). Nothing here belongs in CI.

**Files:** none (operations only).

**Interfaces:**
- Consumes: everything merged in Tasks 1-7.
- Produces: France instance running `price_mode=sensor` on the Frank sensors.

- [ ] **Step 1: Pre-flight — clean tree, full suite green**

```bash
cd /Users/kleist/Sites/x1-smartcharge
git status --porcelain          # must be empty
./.venv/bin/python -m pytest tests/ -q
```
Expected: no output from `git status`, suite green.

- [ ] **Step 2: Back up the running integration OUTSIDE custom_components**

Backups inside `/config/custom_components` get imported by HA and break the load.

```bash
ssh root@192.168.33.45 'mkdir -p /config/x1_backups && cp -a /config/custom_components/anker_x1_smartgrid /config/x1_backups/anker_x1_smartgrid.$(date +%Y%m%d-%H%M%S)'
```
Expected: no output; verify with `ssh root@192.168.33.45 'ls /config/x1_backups'`.

- [ ] **Step 3: Deploy the integration**

```bash
cd /Users/kleist/Sites/x1-smartcharge
scp -r custom_components/anker_x1_smartgrid root@192.168.33.45:/config/custom_components/
ssh root@192.168.33.45 'find /config/custom_components/anker_x1_smartgrid -name "._*" -delete'
```
Expected: files transferred; the `find` removes macOS AppleDouble artefacts.

- [ ] **Step 4: Restart HA and confirm a clean load**

```bash
ssh root@192.168.33.45 'ha core restart'
ssh root@192.168.33.45 'ha core logs | grep -i anker_x1 | tail -30'
```
Expected: no traceback, integration set up.

- [ ] **Step 5: Flip the options in the HA UI**

Settings → Devices & Services → Anker X1 Smartgrid → Configure:
- `price_mode` = `sensor`
- `ent_price` = `sensor.frank_energie_electricity_prices_current_electricity_price_all_in`
- `ent_export_price` = `sensor.frank_energie_electricity_prices_current_electricity_market_price`
- Leave the static tariff values configured as the rollback fallback.

Then reload the config entry (Devices & Services → ⋮ → Reload) — a bare restart can leave cached capacity/predictor state.

- [ ] **Step 6: Verify on-box**

```bash
ssh root@192.168.33.45 'ha core logs | grep -iE "anker_x1|slot_minutes" | tail -40'
```
Check the plan sensor attributes in Developer Tools → States:
- plan rows are populated and 15 minutes apart (96+ rows/day),
- diagnostic `slot_minutes` (or the plan attribute exposing it) reads `15`,
- per-slot export prices vary across the evening (not a flat broadcast, not a ratio-scaled copy of the import shape),
- DP tick duration in the logs is sane (record the number — no prior baseline for this hardware; Open question 4).

- [ ] **Step 7: Rollback if anything is wrong**

Single option flip: set `price_mode` = `static` in the options flow and reload the config entry. If the integration itself fails to load, restore from `/config/x1_backups/anker_x1_smartgrid.<timestamp>`.

- [ ] **Step 8: Follow-up check after 13:00 CEST**

Re-inspect `sensor.frank_energie_electricity_prices_current_electricity_price_all_in` attributes: confirm tomorrow's slots appear (Open question 1). Until confirmed, treat overnight plans as unvalidated.

---

## Unresolved questions

From the spec:

1. Do tomorrow's prices appear in the `prices` attribute after ~13:00 CEST publication? DP overnight value depends on it — verify live before trusting overnight plans.
2. Confirm Frank France injection credit really is spot market price (check contract/app).
3. Doubled-array quirk: file an upstream issue on HiDiHo01/home-assistant-frank_energie?
4. France box DP runtime at 15-min resolution — measure, no prior baseline on that hardware.

Discovered while planning:

5. **Export credit is still gated on the scalar `export_price` being readable.** `decision.py` short-circuits to "no export credit" when `export_price is None`, and `Controller._resolve_export_price` derives that from `read_float(export_entity)`. If the Frank market sensor's *state* goes `unavailable` while its `prices` attribute is still populated, the whole export term is disabled and the new curve is never consulted. Should the curve's current-slot price be allowed to stand in for the scalar? Out of scope as specified; flag before deploy if the market sensor proves flaky.
6. **Oracle/regret parity.** `regret_job.py` builds its per-slot `eff_export` from *recorded* `export_price` samples (mean per slot), so with the Frank market sensor the oracle already sees a real per-slot curve and needs no change. Worth confirming on the first regret run that DP and oracle no longer disagree the way they would under ratio-scaling.
7. **v_hi clamp coverage window.** The DP's coverage test runs over resampled window buckets; the v_hi clamp's runs over the raw import slots in the same `[now, horizon_edge)` span. They agree in every realistic case (both curves at the same resolution), but a partially-covering export curve could in principle satisfy one and not the other, leaving the clamp on the ratio-scale branch while the DP uses the curve. Conservative, but confirm it never fires in the France logs.
8. **`ent_export_price` is a single-entity selector** (`config_flow.py:421`), unlike the multi-select PV pickers. Fine for Frank; note it if a future tariff needs summed export sensors.
