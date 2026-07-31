# Frank Energie price-source support — design

**Date:** 2026-07-31 · **Target:** France instance (192.168.33.45), generic code change
**Approach:** format auto-detect in the existing parse path (approved; config-option and template-adapter alternatives rejected).

## Context

France instance runs `price_mode=static` (no price sensor existed). Frank Energie France launched 2026-02 (hourly/15-min dynamic tariff); the HiDiHo01 `frank_energie` HACS integration is now installed on the France box with `hass.config.country=FR`. Goal: feed its prices into the planner so the France instance gets real DP arbitrage.

## Live findings (verified 2026-07-31 on the France box)

- Import sensor: `sensor.frank_energie_electricity_prices_current_electricity_price_all_in`, attribute `prices` = list of `{from, till, price}`; tz-aware ISO datetimes, price in plain €/kWh (no scaling). Zonneplan shape is `forecast` = `{datetime, electricity_price}` with `electricity_price` scaled by `const.PRICE_SCALE` — the two shapes are disjoint, so sniffing is unambiguous.
- **Quirk: every slot appears exactly twice** (192 entries, 96 distinct, identical values). Parser must dedupe or durations collapse to 0 and the DP double-counts slots.
- Resolution: `select.frank_energie_settings_resolution` = `pt15m` (options `pt15m`/`pt60m`). **User chose 15-min** → France becomes the first live native-dt=15 deployment. No code gate: `resolve_slot_minutes` defaults to auto-detect (min consecutive delta snapped to {15,30,60}); Zonneplan 60-min data was the only reason it lay dormant.
- Export sensor for credit: `sensor.frank_energie_electricity_prices_current_electricity_market_price` (user decision: France has no salderen; Frank credits injection at market price — to be confirmed against contract).
- Tomorrow's slots were NOT present in the attribute at check time (all 96 distinct slots = today).

## Design

### 1. Parser (`parsers.py`)
`parse_price_curve` gains a per-entry decoder:
- `datetime` + `electricity_price` present → Zonneplan decode (÷ `PRICE_SCALE`) — unchanged.
- `from` + `price` present → Frank decode: ISO-parse `from`, `float(price)` as €/kWh directly.
- Then: **dedupe by start (keep first)** → sort → derive `duration_min` from consecutive gaps (existing logic). NaN/Infinity guards apply to both shapes. Dedupe is generic (also protects Zonneplan path).

### 2. Attribute lookup (`coordinator.read_price_slots`)
Replace hardcoded `attributes.get("forecast")` with ordered candidates `("forecast", "prices")`; first attribute yielding a non-empty parse wins.

### 3. Export curve (`decision.py` + plumbing)
Coordinator also reads the export entity's curve via the same mechanics and threads it into `compute_decision` as optional export slots. Export-price priority becomes:
1. static mode → flat `static_price_export` (unchanged)
2. **export entity exposes a curve covering the window → use its per-slot prices (NEW)**. "Covering" = all-or-nothing: the curve must supply a price for every window slot that has an import price; if incomplete, skip to (3)/(4) entirely — no per-slot mixing.
3. export entity == import entity → reuse import curve (unchanged)
4. scalar ratio-scale fallback (unchanged)

Rationale for (2): all-in = market + flat surcharges; ratio-scaling multiplies instead of subtracting, mispricing peaks. The market sensor publishes its own full curve — use it.

### 4. 15-min activation
No code change. Deduped 15-min slots → `detect_slot_minutes` returns 15 → native-dt path live. Watch after deploy: DP tick duration (~96–192 slots), plan/card display density.

### 5. Testing (TDD)
- Parser: Frank shape, duplicated entries, mixed/junk entries, Zonneplan regression (byte-identical).
- Coordinator: attribute fallback order (`forecast` preferred when both exist).
- Decision: export-curve case beats ratio-scale; clean fallback when export entity has no curve attribute; static mode untouched.
- Full suite green; lab behavior unchanged.

### 6. France rollout
1. Deploy integration via scp to `192.168.33.45:/config/custom_components` (backups OUTSIDE custom_components).
2. Options: `price_mode=sensor`, `ent_price` = Frank all-in sensor, export entity = Frank market-price sensor. Static tariff values stay configured as fallback.
3. Verify: slots populate, detected slot_minutes=15, DP tick time sane, per-slot export curve visible in plan.

**Rollback:** set `price_mode=static` (single option flip).

## Open questions

1. Do tomorrow's prices appear in the `prices` attribute after ~13:00 CEST publication? DP overnight value depends on it — verify live before trusting overnight plans.
2. Confirm Frank France injection credit really is spot market price (check contract/app).
3. Doubled-array quirk: file upstream issue on HiDiHo01/home-assistant-frank_energie?
4. France box DP runtime at 15-min resolution — measure, no prior baseline on that hardware.
