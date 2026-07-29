# Device-derived power limits + grid import cap

**Trigger:** 2 modules added → 20 kWh / 12 kW charge / 13.2 kW discharge. `capacity_kwh`
auto-adjusted; power limits did not. Planner still rate-limits at 6 kW.

**Root cause:** `__init__.py:45` comment says "DEVICE-DERIVED LIMITS … hardware, not user
preference" but derives from a **const** (`DEFAULT_MAX_CHARGE_W = 6000.0`), force-written
after the options merge. The real values sit on `number.*_battery_setpoint`'s `min`/`max`
attributes — which `actuator._clamp_to_live_limits` already reads.

Live: `min=-12000.0`, `max=13200.0`, step 100. Grid: 3×25A (~17 kW).

---

## Task 1 — derive limits from the device

TEST: `tests/test_anker_resolver.py`
- setpoint entity min=-12000/max=13200 → resolved `max_charge_w=12000`, `max_export_w=13200`
- attrs missing / non-numeric / bool / wrong-sign → key omitted, const default stands
- entity unavailable/unknown → omitted (mirrors capacity)

IMPL:
- `anker_resolver.resolve_anker_config` — after the capacity block, read `CONF_ENT_SETPOINT`
  (already a hard role, `ANKER_ROLE_SUFFIXES`) state attrs `min`/`max`.
  `max_charge_w = abs(min)` when `min < 0`; `max_export_w = max` when `max > 0`. Soft: a miss
  is omitted, never appended to `missing`.
- `__init__.py:45-50` — const force-write stays as the BASELINE (runs before
  `apply_anker_resolution` at :56, so resolved values win via `data.update`). Fix the comment
  to say const-fallback.
- `const.py:284-285` — `SETPOINT_MIN_W`/`SETPOINT_MAX_W` are absolute backstops, not the real
  limit; widen so they cannot re-clamp a derived 12000/13200. `guard.py:70` uses
  `abs(SETPOINT_MIN_W)` alongside `cfg.max_charge_w`, so both must move together.

VERIFY: reload on lab → `sensor.smartgrid_solar_charge` unchanged (capacity path untouched);
DP charge slots shorten.

## Task 2 — `grid_import_limit_w`

Export already does `min(max_export_w, grid_export_limit_w)` in every path
(`dp_common.py:206`, `pricing_store.py:207`, `energy.py:162`). Charge has **no** grid-side
analogue — `max_charge_w` alone bounds grid pull, so a derived 12 kW goes straight at the
connection.

TEST: parity suite must stay green (`tests/test_optimize_parity.py`) — the cap lands in the
shared helper, so DP and oracle move together by construction.

IMPL:
- `const.py` — `CONF_GRID_IMPORT_LIMIT_W`, `DEFAULT_GRID_IMPORT_LIMIT_W = 17250.0` (3×25A)
- `models.Config` — field
- `config_flow._TUNABLES` — UI knob
- `regret._max_grid_dc:161` — the inverter rate (`max_charge_w`) still governs
  `solar_ac_used` accounting; only the **grid remainder** gets
  `min(remaining_ac, grid_import_limit_w/1000*dt_h)`. Solar is not grid import.
- `plan.py:227` display mirror; `decision.py:1187` + `executor.py:105` FORCING setpoint

NOT touched: `energy.py:32/115` (solar surplus, inverter-bound, not grid).

## Task 3 — startup race (from the same restart)

- Capacity/limits resolve ONCE at setup. If `anker_x1` sensors have no state yet, the stale
  const/stored value silently sticks (this is why capacity stayed 10 kWh). Retry resolution
  on tick until resolved.
- `controller.py:933` polls health once per clock-hour → one missed poll latches
  "⚠ unreachable" for up to 60 min. Retry on the next tick after a failure.

---

## Known risk

`_max_grid_dc:162` looks eta up at `cfg.max_charge_w` ("grid charge is always full-rate").
The measured efficiency curve has **no bins above 6 kW**; `efficiency.py:128` clamps outside
its range to the top anchor, so eta at 12 kW is assumed equal to eta at the highest observed
bin — optimistic, since conversion loss grows with power. Self-corrects once 12 kW charging
populates the bins. Watch `efficiency_curve` on the plan sensor.

## Unresolved

- Is 13200 AC-exportable, or a battery DC discharge limit behind a lower inverter AC rating?
  Old measured net-export ceiling was ~6000 W. Needs an empirical push before trusting it.
