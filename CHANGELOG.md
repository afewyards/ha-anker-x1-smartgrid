# CHANGELOG


## v0.2.0 (2026-07-05)

### Bug Fixes

- Harden naive-datetime handling in project_persons_home and add person_entities UI label
  ([`3814ee3`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/3814ee3f2e44c536a0ff226c66dec3d0f21dd258))

### Features

- Add person-entities options picker and config key
  ([`d7b90b9`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/d7b90b9892fde8b4b2e8bdac3d007324bcba07a9))

adds CONF_PERSON_ENTITIES config key + a multi-select person picker in the options flow
  (options-only, no install-time default), re-vendored into forecast_core.

- Add persons_home HGBR feature (train + serve vector)
  ([`e329105`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/e329105035993af1d0f1b76705c98ea1bea9030f))

Appends persons_home to the HGBR feature set (17->18 columns), sourced from the hourly rollup's
  persons_home_mean at train time and passed through as a predict_load_w/HGBRQuantileModel
  serve-time kwarg. Missing values coerce to NaN (HGBR-native).

- Add v8 recorder migration for persons_home (samples + hourly)
  ([`b5f0813`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/b5f0813859ca65bf045cbc34506f8ce110c38b9d))

Add the persons_home column to samples and its 5 rollup-stat columns (mean/max/min/std/count) to
  samples_hourly via a guarded v7->v8 migration, re-vendor into forecast_core, and update the addon
  synthetic-data test fixtures to include the new columns.

- Consume persons_home in the addon serve path
  ([`0a2fa79`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/0a2fa79fd2088343b3397113c5451893fe5d6cca))

- Drop irradiance from the HGBR feature set
  ([`c9893dd`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/c9893dd0cc8991759fee6d3b9cd165d02e36f7f1))

- Pass cloud_cover/humidity/wind_speed through HGBR serve path
  ([`075df6e`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/075df6eb3f2089242a3ce0cabf83a23da95e00db))

- Project persons_home per-hour into the forecast payload
  ([`69010e5`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/69010e516b8615b8c1268883c236f8a92b6baeb1))

- Record persons-home count each controller tick
  ([`85fd0c5`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/85fd0c5f74464bfefaa17dc3df0709774192668e))

- Roll up persons_home hourly mean
  ([`6783946`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/67839467fbcfe191a17533d1c427d4eb23f5d407))

### Testing

- Lock full-signal train/serve vector consistency
  ([`93d2340`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/93d23402b3fcbebe9f1553b98ca0359557c870c3))


## v0.1.0 (2026-07-04)

### Chores

- Add project scaffolding and HACS metadata
  ([`0145c23`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/0145c23c41e31f55fb6303c27f0f5625759a34ca))

- Add pyproject tooling config and semantic-release CI
  ([`62dafe6`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/62dafe6bfd3158554cbaa11156d73a1f216d3139))

Port solar-fusion's Python tooling and release setup: - pyproject.toml with ruff, pyright, pytest,
  and python-semantic-release config - pre-commit hooks (ruff-check, ruff-format, pyright) - GitHub
  Actions: release (semantic-release), tests, hassfest, hacs - migrate pytest.ini settings into
  [tool.pytest.ini_options]; remove pytest.ini - add holidays to requirements_test.txt (top-level
  import in featureset.py)

### Documentation

- Add efficiency-curve and load-signals implementation plans
  ([`c353ebb`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/c353ebbdf8f808093b72c23ccfdfa2e36ba8df3e))

- Add measured-efficiency + load-model signals design spec
  ([`f69f508`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/f69f5083fa64ab5ba79635e53723a1907348ac72))

- Revise efficiency+signals spec after design review
  ([`07d43eb`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/07d43eb431fde90923422e69bb4a8058fc0f0fe1))

Full eta callsite inventory, DC-power-keyed curve, energy-balance AC estimation, load-discharge eta
  physics change, drop holiday/irradiance, addon-internal weather fix, v8 migration, regression
  gate.

- **efficiency**: Add Phase-0 empirical validation SQL (user-run)
  ([`0e5e22c`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/0e5e22c993b720af699a57517ecc428c8b84fcf9))

### Features

- Add 15-minute slot resolution
  ([`5aeaefa`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/5aeaefa9280eefbd6ce46df3f0d20e8db6c409bb))

- Add apexcharts plan Lovelace card
  ([`231c675`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/231c675b326b3e9a6caa86cc49302ad8f7f3f8d6))

- Add backtest harness
  ([`fe04ed5`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/fe04ed5831c8200ddb3e98c017b8e5913d00d584))

- Add controller, actuator, scheduler and guards
  ([`7a8bff8`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/7a8bff8cec91d1e54eaea14dd2bc397a864c953e))

- Add data recorder, rollup and data-quality storage
  ([`65cabdf`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/65cabdfd22942e170bee4c72acda1a65fd8d0dd7))

- Add data update coordinator
  ([`2eef68f`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/2eef68ff6ac89f61577ce91c345c2b636816d390))

- Add DP optimizer and regret oracle
  ([`affc317`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/affc317d8648e85f7ccdb6e20e78fdb2757c474d))

- Add HGBR load model, featureset and intraday adapter
  ([`7b1fa1a`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/7b1fa1acacc74296e9e71cb3de398fc7262c42e7))

- Add plan builder with SoC drift-hedge and past actuals
  ([`fc013a4`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/fc013a4e1a092a8551a297f56595a25bccb398d0))

- Add price/PV parsers and pricing store
  ([`f36fd02`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/f36fd023ac4a57b5dfd2ec8ea6ba563834b52e64))

- Add sensor and switch entities
  ([`2f1de85`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/2f1de855c4c9d0d5760eb01e52950c826180c7eb))

- Add solar and load forecasting sources
  ([`a7d9373`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/a7d93735da8a7257af0115b9e55522b65535f9a1))

- Scaffold anker_x1_smartgrid integration (setup, config flow, models)
  ([`ec84f71`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/ec84f7133ef8396fcd7cd9ceaef3b8c3984a2046))

- **addon**: Add prediction server, trainer and health service
  ([`91a7c8e`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/91a7c8ef1811d80aaa03405a7d31df5d391d9747))

- **addon**: Add anker_x1_forecast add-on packaging
  ([`5c22dd9`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/5c22dd9d3fa5ba5af3c7d0fdb703da4e079c3822))

- **addon**: Vendor shared forecast_core library
  ([`e1f2fe0`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/e1f2fe0b85f2a380f5c86372904141bc3aab837d))

- **controller**: Build+cache efficiency curve and thread it into the DP planner and reserve
  ([`f2773a3`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/f2773a338c22b6fede84d4f673a56d23d4c36437))

- **controller**: Thread eta_curve into the live export executor, PnL, and soc_drift
  ([`15c2423`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/15c24238229703a02a783a819e539ca71eb4cbf4))

Wires the measured efficiency curve (gated by cfg.use_measured_eta, default OFF) into the C3 live
  export executor's ride_out_reserve_kwh/export_net_target_w calls, the export PnL DC conversion,
  the soc_drift expected-delta calc, the DP net-out revenue adjustment, and the remaining
  build_plan_horizon/ build_display_horizon call sites that T16 left out. Adds a new
  Controller._eta_d_at helper and a full-tick regression test.

- **efficiency**: Add band-stable same-sign episode segmentation
  ([`18b3124`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/18b3124da795cdc158b5f0ee829971f2e39b516d))

- **efficiency**: Add EfficiencyCurve object with static fallback and bin lookup
  ([`dc05be3`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/dc05be353348ac1a02c52e847578afb76fbec396))

- **efficiency**: Add per-bin median aggregation, confidence gate, and EfficiencyCurve.build
  ([`5081493`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/50814939b882aabaccdd231d150fbc167fec952a))

- **efficiency**: Add per-run eta from energy-balance residual with ΔSoC gate and envelope
  ([`8d4a85c`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/8d4a85cd3e27784aaa4632e4f47ccfc0556ebdc7))

- **efficiency**: Add use_measured_eta flag, DC bins, and confidence constants
  ([`ba105c6`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/ba105c6f998919f13a905560e9b9abff5e645361))

- **energy**: Thread eta_curve into simulate_soc, ride_out_reserve_kwh, export_net_target_w
  ([`8f35b6b`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/8f35b6b3170711b78f0f929e400a68d0c838a23f))

- **export_filter**: Thread eta_curve into min-export-block net-revenue recompute
  ([`b6e1259`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/b6e1259b495a5256b53c0e6de9725d9ec49d1c67))

- **optimize**: Thread optional eta_curve through optimize_grid, mirroring the oracle
  ([`85f20f3`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/85f20f37becd3092aef206c78813e8cc5bd6ddd6))

- **plan**: Thread eta_curve into display SoC sim; fix stale plan.py mirror comment (L5)
  ([`36c1e37`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/36c1e3798c51ec69086bf4e51d83ef7ee6dc4098))

- **pricing_store**: Thread eta_curve into the anticipation export-cap eta
  ([`bb7ccb9`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/bb7ccb9c75cbe19d45e526487b5196c62520c836))

- **recorder**: Add read_efficiency_samples accessor (v6+ load_w residual)
  ([`c5c6ce8`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/c5c6ce818d7c6e2d66418f021bf5ef5398f2a4e6))

Adds a DataRecorder.read_efficiency_samples() method that returns per-tick residual-power samples
  (load_w - p1_w - pv_w) for rows where load_w, p1_w, soc, and batt_w are all non-NULL, ordered by
  ts ascending with an optional since_iso filter. Includes two new tests in
  tests/test_recorder_query.py.

- **regret**: Add shared eta helpers and optional eta_curve to shared battery physics
  ([`c5a63a3`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/c5a63a3361ad3ccc0e3c25fcbb9e22768b262196))

- **regret**: Thread optional eta_curve through hindsight oracle and realized_grid_cost
  ([`22ca59f`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/22ca59f6e095fdbcece68a81d627114e9337e7f2))

- **scheduler**: Derive charge_price_ceiling round-trip from the eta curve when enabled
  ([`9dc4ce2`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/9dc4ce25b18a252424604d26fb25395533792d48))

- **sensor**: Expose the measured efficiency bin table as status attributes
  ([`21f0c94`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/21f0c942cb86f0305fc2e49e6fe91d6c0fa92b18))

### Testing

- Add 15-minute slot resolution tests
  ([`de727b2`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/de727b26db06953e71435a9656026a16f718c10e))

- Add charge-trough look-back tests
  ([`b16ba38`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/b16ba38d3ad417faeec9a9d9f9daec76467057c7))

- Add config flow tests
  ([`205606b`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/205606b6d4f8141272f5a41b24938974066fb1d9))

- Add controller tests
  ([`1c7175d`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/1c7175d4c7cbf4abb1ebbe1e84ba0c27234cccd0))

- Add entity and sensor tests
  ([`ea957e3`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/ea957e33bb2afb5d446039a3fe9f5932ed66757e))

- Add export tests
  ([`0e8f7d7`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/0e8f7d708f9c4a31d20e9127b621f6539efb43b8))

- Add forecast tests
  ([`1eddb63`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/1eddb63fc5b7620dddd860a7f92f0d7a541bda85))

- Add load-model tests
  ([`f0f5b6c`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/f0f5b6cef8d5cf34d7c2aca731be60dbeb0226b0))

- Add optimizer tests
  ([`29b1d88`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/29b1d88366d878d78c7c2cf791ec67d5f09a6905))

- Add parser and pricing tests
  ([`82abc68`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/82abc6837527d682b1e6d7605adf4e8e4cf05c0e))

- Add planning tests
  ([`feb1327`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/feb1327db084f68754f749b7cf26ae16c7abf88f))

- Add recorder tests
  ([`0da6a53`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/0da6a53d854f4f89533f6f8c530341c723d23533))

- Add regret/oracle tests
  ([`975bdae`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/975bdaebc20d2e47ae3ed2f215dd223cebc756af))

- Add remaining unit and integration tests
  ([`3fc2de3`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/3fc2de363b7a1e5567ec5e9f0f8f655322692438))

- Add reserve and survival tests
  ([`f818655`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/f818655ffb265a9f7e63ab15497cd85444f57887))

- Add scheduler tests
  ([`41a0f7a`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/41a0f7a21af0304c1ec9888899364555e4a6b10a))

- Add SoC drift-hedge tests
  ([`ed3e356`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/ed3e356f6d3c9f6c30c1dabf3261f627c0982a5f))

- Add test fixtures and conftest
  ([`82548c6`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/82548c688d99dc0691f376836d97e215c53af784))

- **addon**: Add add-on test fixtures
  ([`988cdd7`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/988cdd7743cd7d2e3f4c262ae3d2f87afd694941))

- **addon**: Add predictor and server tests
  ([`5368509`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/5368509482bdb5dd4e7806b97bc3c5b58b6aeceb))

- **addon**: Add trainer and vendor-parity tests
  ([`0b0330c`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/0b0330c0670846b4a02a55ae961a56f9fb0f9521))

- **efficiency**: Add reserve/export flag-ON regression gate harness
  ([`bb7130d`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/bb7130d4d062ac0a46e43b2d27cdd2835090e2cb))

- **optimize**: Add flag-ON 0-regret matched-pair property gate for the eta curve
  ([`03378bc`](https://github.com/afewyards/ha-anker-x1-smartgrid/commit/03378bc0b7c2ba989c1b90c6da4861b9243a87fd))
