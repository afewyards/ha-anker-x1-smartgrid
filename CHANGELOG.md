# CHANGELOG


## v0.1.0 (2026-07-08)

### Chores

- Add project scaffolding and CI
  ([`603a1bf`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/603a1bfbc6e5a9ed62dfc4aa6e768fc6c9bd5521))

pyproject.toml, pre-commit hooks (ruff, pyright), GitHub Actions (release, tests, hassfest, hacs),
  HACS metadata, and repository config.

### Documentation

- Add README, changelog, and dashboard card
  ([`2f888e3`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/2f888e3d45f92064fe724cdc3ae69ca8e836917c))

Full README with features, prerequisites, installation, setup, configuration reference, entity list,
  architecture overview, and safety model. ApexCharts plan visualization card for the schedule.

### Features

- Add Anker X1 Forecast add-on
  ([`1f615ff`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/1f615ff81f62ff50c234a75bbb06835cd5d34734))

HGBR load-forecast service: trainer, /predict server, /health endpoint, vendored core modules with
  sync script and SHA256 drift detection. Runs scikit-learn in a glibc container (python:3.12-slim).

- Add core integration shell
  ([`bae5154`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/bae51543c80292c3cb6ac0bafb8e33a0733d2b5d))

Entry point, config flow, constants, controller tick loop, coordinator, actuator (Modbus setpoint
  writer), Anker device resolver, guard, sensor/switch entities, and translations.

- Add load forecasting and ML pipeline
  ([`29903d3`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/29903d3c5ca2da7047122aae2a213bd2e515b0e4))

Tiered load model (profile → HGBR → remote add-on), intraday residual corrector, feature
  engineering, measured efficiency curve, walk-forward backtest gate, data quality checks, and
  remote forecast client.

- Add planning and optimization engine
  ([`70dea89`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/70dea8941e7885a8e4e527d2572c46d8c3e8deee))

DP optimizer (charge+export co-optimization), heuristic fallback scheduler, PV forecast adapter,
  price parsers, export post-filters (peak-band, min-block, tail-trim), pricing store, past-actuals
  backfill, and SoC drift hedge.

- Add recorder and energy accounting
  ([`2c0ed69`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/2c0ed69d7d23db1d3895876eb91f3caa8b9c46d3))

Per-minute SQLite sample recorder, hourly rollup aggregation, per-tick kWh energy accounting, and
  oracle hindsight regret scoring.

### Testing

- Add test suite
  ([`83041fe`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/83041fea0269c305ab080fc94fecb7b3c9d5197a))

1558 tests covering the integration (optimizer, controller, scheduler, recorder, config flow,
  entities, 15-min resolution, acceptance) and the forecast add-on (server, trainer, predictor,
  vendored parity).
