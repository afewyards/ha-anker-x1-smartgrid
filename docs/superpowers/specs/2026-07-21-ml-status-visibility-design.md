# ML Predictor Status Visibility — Design

**Date:** 2026-07-21
**Status:** Approved (user, 2026-07-21)

## Problem

The forecast add-on is the only viable ML path on-box (no sklearn wheel for HA core's
py3.14/musl). Its activation is gated (21 lag-complete days coverage, then a 2%
beat-baseline backtest), and the integration's `fetch_forecast` swallows all failures by
design. Consequence observed live on 2026-07-21: `addon_url` pointed at a dead hostname
for weeks and nothing surfaced it — the ML tier could never have activated. There is no
way to see from the UI (a) which predictor tier is active, (b) how far away ML activation
is, (c) whether the configured add-on is even reachable.

## Design (Approach A — integration computes everything)

No add-on change. No new entities. `sensor.smartgrid_active_load_model` keeps its state
(tier name) and gains diagnostic attributes; the Lovelace apexcharts card displays a
single computed status string in its header.

### 1. `remote_forecast.fetch_health(url, timeout) -> dict | None`

`GET {addon_url}/health`, same timeout config as `/predict` (default 5s). Never raises —
timeout / non-200 / malformed JSON → `None`. Mirrors `fetch_forecast`'s never-raise
contract. `None` ⇒ unreachable.

### 2. Controller wiring

- Fetched once per clock-hour under the existing `_last_remote_forecast_hour` guard
  (same place the `/predict` fetch happens), plus on first tick after startup.
- Fetched whenever `addon_enabled` is true — **regardless** of ready/promoted, so
  reachability is monitored during dormancy (the exact window where the URL bug hid).
- Stores: `_addon_health: dict | None`, `_addon_health_ts: datetime | None`.

### 3. Coverage counter — `count_lag_complete_days(hourly_rows) -> int`

Pure-Python standalone helper (new `ml_status.py` module; deliberately NOT factored into
`hgbr.is_ready` to avoid add-on vendoring lockstep). Same rule as `is_ready`: a row at
UTC t is lag-complete when a row at t−168h exists; count distinct Europe/Amsterdam
calendar dates of lag-complete rows. `eta_days = max(0, 21 - count)`.

Runs on the hourly rows already read during retrain; result cached on the controller
(recomputed at most once per clock-hour, alongside the health fetch).

### 4. `ml_status` display-string logic (in `ml_status.py`, pure function)

Priority order:

| Condition | String |
|---|---|
| `not addon_enabled` | `add-on off` |
| enabled, health fetch → None | `⚠ unreachable` |
| reachable, `ready=false` | `ML in ~{eta_days}d` |
| `ready=true, promoted=false` | `backtest gate` |
| `promoted=true`, active tier == `remote` | `ML active` |
| `promoted=true`, active tier != `remote` | `⚠ promoted, not consumed` |

### 5. Sensor attributes (on `sensor.smartgrid_active_load_model`)

`ml_status`, `addon_configured` (bool: enabled && url non-empty), `addon_reachable`
(bool), `addon_ready`, `addon_promoted`, `addon_n_rows`, `addon_last_trained` (pass-through
from `/health`, None when unreachable), `coverage_days`, `coverage_required` (21),
`eta_days`, `last_health_check` (ISO ts). State unchanged (tier name).

### 6. Lovelace card

`lovelace/apexcharts-plan-card.yaml`: enable header `show_states`, add one header-only
series — `entity: sensor.smartgrid_active_load_model`, `attribute: ml_status`,
`show: {in_chart: false, in_header: raw}`, `name: Predictor`. (Card paste is a manual
user step, as usual.)

## Error handling

- All new paths never-raise (health fetch, counter, string builder); any failure degrades
  to `addon_reachable=false` / attrs `None` — never touches planning or actuation.
- Planning behavior is completely unchanged; this is observability only.

## Testing

- `fetch_health`: 200-ok, timeout, non-200, malformed JSON → None (mirror existing
  `fetch_forecast` tests).
- `count_lag_complete_days`: synthetic rows — gap in lags, DST boundary date counting,
  empty. **Parity test** (dev venv, sklearn present): counter ≥ 21 ⟺ `hgbr.is_ready`
  true on the same synthetic sets — locks against divergence.
- `ml_status` string: one test per table row above.
- Sensor: attrs present + populated from controller state.

## Deploy

Integration-only (scp to lab per [[haos-deploy]]). No add-on rebuild, no DB migration.
Verify live: attrs on `sensor.smartgrid_active_load_model`, expect `ML in ~1d` (coverage
was 20/21 on 2026-07-21) and `addon_reachable: true` post-URL-fix.
