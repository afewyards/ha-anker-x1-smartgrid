# Anker X1 Forecast Add-on

HGBR load-forecast service for Anker X1 SmartGrid — trainer, `/predict` server, and `/health` endpoint. Runs scikit-learn in a glibc container (`python:3.12-slim`) because the HAOS box's Python 3.14 on Alpine/musl/aarch64 has no prebuilt sklearn wheel.

> **History (P0 spike, 2026-06-22):** P0 validated on-box — `ha addons install local_anker_x1_forecast`
> built in ~28 s (sklearn from a prebuilt cp312/aarch64 wheel, no source build);
> `/health` → `{"ready":true,"sklearn_version":"1.5.2",...}`; uvicorn logs clean.
> The glibc-container premise held → P1 greenlit. **Validation gotchas:** use
> `ha store reload` (not `ha addons reload`) to discover a local add-on; curl the
> **host IP** `172.20.0.47:8099` (the port map), not `localhost` from inside another add-on.
> P0 exit criteria (all passed): (1) on-box build pulls sklearn from prebuilt wheel,
> (2) `/health` returns HTTP 200, (3) add-on stays running with clean logs.

## Contents

- `config.yaml` — Local add-on manifest: glibc base image, port 8099, read-only config volume map.
- `requirements.txt` — `scikit-learn==1.5.2` (confirmed cp312/aarch64 manylinux wheel) plus fastapi and uvicorn.
- `server.py` — HTTP server with `GET /health` and `POST /predict` endpoints.
- `Dockerfile` — `FROM python:3.12-slim` with requirements install.
- `run.sh` — Uvicorn launcher on 0.0.0.0:8099.

## Deploy (Local add-on)

1. Strip `__pycache__` from the add-on directory, then copy:
   ```bash
   COPYFILE_DISABLE=1 scp -rp addon/anker_x1_forecast root@172.20.0.47:/addons/anker_x1_forecast/
   ```

2. Trigger local add-on discovery in HA:
   - UI: Settings → Add-ons → Add-on Store → ⋮ → **Reload**
   - SSH: **`ha store reload`** — note `ha addons reload` did NOT pick up the new local
     folder on this box (Supervisor renamed add-ons → "apps", `/data/apps/...`); `ha store reload`
     reported `1 new` and registered it.

3. Install "Anker X1 Forecast (HGBR)" local add-on (triggers on-box `docker build` where sklearn wheel install is proven), then **Start** it.
   - Slug on the box: `local_anker_x1_forecast` (local add-ons get `local_` prefix).

## Validate

- HTTP endpoint: `curl http://172.20.0.47:8099/health` → expect HTTP 200 with real `sklearn_version` (e.g., `1.5.2`) and `python_version` matching `3.12.x`.
- Logs: `ha addons logs local_anker_x1_forecast` → uvicorn starts cleanly, no import traceback, no meson/source-build error.

## API Reference

### `GET /health`

Returns current training state.

```json
{
  "ready": true,
  "promoted": true,
  "last_trained": "2026-06-22T03:00:00+00:00",
  "n_rows": 1440,
  "metrics": {"mae_p50": 42.1, "mae_p80": 61.3},
  "sklearn_version": "1.5.2",
  "python_version": "3.12.13 ..."
}
```

### `POST /predict`

Request a load forecast for one or more future hours.

**Request body:**

```json
{
  "hours": [
    {
      "ts": "2026-06-22T14:00:00+00:00",
      "temp_forecast": 18.5,
      "cloud_cover": 0.3,
      "humidity": 65.0,
      "wind_speed": 3.2,
      "irradiance": null
    }
  ]
}
```

All weather fields (`temp_forecast`, `cloud_cover`, `humidity`, `wind_speed`,
`irradiance`) are optional and default to `null`.  The model uses `temp_forecast`
internally; the remaining fields are accepted for future compatibility but currently
NaN'd by the model.

`ts` must be an ISO-8601 string **with timezone info** (aware).  Hours with
naive timestamps or unparseable `ts` values are silently omitted from the
response.

**Response body (model ready):**

```json
{
  "ready": true,
  "promoted": true,
  "predictions": [
    {"ts": "2026-06-22T14:00:00+00:00", "p50_w": 820.0, "p80_w": 1040.0}
  ]
}
```

**Response body (model not ready / dormant):**

```json
{"ready": false, "promoted": false, "predictions": []}
```

The handler never triggers training and is always non-blocking.  Safe to poll
on any cadence.  `promoted` reflects whether the model passed the walk-forward
backtest gate — callers may choose to fall back to the on-box bucketed model
when `promoted` is `false`.

## Next (P1+)

- Switch to public git add-on repository (GitHub `afewyards`) with `repository.yaml`.
- Bundle HA-free core modules.
- Read `/config/anker_x1_smartgrid.db` for training data.
- Train HGBR P50/P80 daily.
- Serve `/predict` endpoint.
- Add integration's `RemoteForecastPredictor` tier with bucketed/profile fallback.

## Updating vendored core

`forecast_core/` contains **byte-identical copies** of 8 HA-free modules from
`custom_components/anker_x1_smartgrid/`. The Docker build context is `addon/anker_x1_forecast/`,
so it cannot `COPY` from `custom_components/` — vendoring is required.

**After editing any original in `custom_components/anker_x1_smartgrid/`**, re-sync:

```bash
# From repo root:
bash addon/anker_x1_forecast/sync_core.sh

# Or from the addon directory:
cd addon/anker_x1_forecast
./sync_core.sh
```

The script copies all 8 modules verbatim and regenerates `forecast_core/SOURCE_SHA256`
(one `<sha256>  <module>.py` line per file, sorted).

**Drift detection:** Two CI gates catch un-synced drift:

- `tests/test_vendored_parity.py` — all 8 modules, byte parity + `SOURCE_SHA256`
  freshness, runs in the main test suite.
- `tests_addon/` — add-on's own test suite (includes its own parity checks),
  runs as a separate CI step.

Vendored modules: `const`, `dataquality`, `rollup`, `loadmodel`, `featureset`,
`recorder`, `hgbr`, `backtest`.
