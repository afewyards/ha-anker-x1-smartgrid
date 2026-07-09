# Anker X1 SmartGrid

A Home Assistant custom integration that turns an [Anker SOLIX X1](https://www.anker.com/products/anker-solix-x1) home battery into a price-optimized grid asset. A dynamic-programming optimizer builds a rolling charge/export schedule from electricity prices, solar forecasts, and house-load predictions, then an executor actuates the battery via Modbus every minute.

**No software-enforced safety floor** — the X1 firmware's own 5%/100% hard limits are the safety net. The integration only decides *when* to charge and *when* to export; the battery protects itself.

## Features

- **Dynamic-programming optimizer** — co-optimizes grid charging and grid export in a single pass over the price horizon (typically 24-36 hours ahead)
- **Battery arbitrage** — buys cheap, sells at peak; accounts for round-trip efficiency, cycle degradation cost, and feed-in fees
- **Solar-first accounting** — free solar fills the battery before paid grid energy is considered
- **House-load forecasting** — tiered: profile averages → in-process gradient-boosted model → optional [add-on](#add-on) with full ML pipeline
- **Intraday adaptation** — corrects the load forecast in real-time from the last few hours' residuals
- **Measured efficiency curve** — optionally derives charge/discharge efficiency from recorded sessions instead of using static values
- **Self-scaling overnight reserve** — rides out to the next cheap grid hour or solar pickup without draining below firmware floor
- **Price anticipation** — blends recent price history to estimate tomorrow-morning prices before they're published
- **15-minute tariff support** — native sub-hourly planning for providers like Zonneplan
- **Master switch** — `switch.smartgrid_enabled` pauses all actuation without removing the config entry
- **Full observability** — plan horizon, regret tracking, forecast accuracy metrics, and efficiency curves exposed as sensor attributes

## Prerequisites

| Requirement | Details |
|---|---|
| Home Assistant | 2024.10+ |
| Anker X1 integration | Provides SoC, battery power, Modbus setpoint, work mode, meter power, and engage entities |
| Price sensor | Dynamic electricity tariff in EUR/kWh (e.g. [Zonneplan](https://www.zonneplan.nl/)) |
| PV forecast | One or more solar forecast integrations (e.g. Open-Meteo, Forecast.Solar, Solcast) |
| Weather entity | Hourly temperature forecast for the load model (e.g. KNMI) |

## Installation

### HACS (recommended)

1. Add this repository as a custom repository in HACS
2. Search for **Anker X1 SmartGrid** and install
3. Restart Home Assistant

### Manual

Copy `custom_components/anker_x1_smartgrid/` to your Home Assistant `config/custom_components/` directory and restart.

## Setup

Go to **Settings → Devices & Services → Add Integration → Anker X1 SmartGrid**.

The setup flow asks for:

1. **Anker SOLIX X1 device** — battery capacity and all control entities (SoC, setpoint, work mode, Modbus, meter power) are derived automatically
2. **Import price sensor** — your dynamic tariff sensor (EUR/kWh)
3. **Weather forecast entity** — feeds the load model
4. **Solar forecast integrations** — pick one or more; multiple arrays are summed

Everything else defaults to sensible values and can be tuned later via **Configure**.

> The integration starts **disabled** on first install — review the plan in the dashboard before enabling `switch.smartgrid_enabled`.

## Configuration

All tunables are editable via the integration's options flow (**Configure** button), grouped into collapsible sections:

| Section | What it controls |
|---|---|
| **Devices & sensors** | X1 device, price/export-price/load/weather sensors, person entities for home-presence |
| **Solar forecast** | Forecast integrations and PV energy/peak-time sensor lists |
| **Battery & charging** | SoC floor/target, efficiencies, charge margin hurdle, trough look-back |
| **Export & arbitrage** | Enable/disable export, grid limit, fees, cycle cost, reserve sizing, peak band, dwell times |
| **Price anticipation** | History depth, blend weight, confidence haircut, anticipation margin |
| **Load model & ML** | Learned model toggle, training thresholds, intraday adaptation, add-on connection |
| **System** | Slot resolution, data retention, SoC drift hedge |

## Entities

### Switch

| Entity | Description |
|---|---|
| `switch.smartgrid_enabled` | Master on/off — pauses all planning and actuation |

### Sensors

| Entity | Unit | Description |
|---|---|---|
| `sensor.smartgrid_state` | — | Current controller state (IDLE, CHARGING, EXPORTING, ...) |
| `sensor.smartgrid_setpoint` | W | Active Modbus setpoint being sent to the battery |
| `sensor.smartgrid_plan` | h | Planned grid-charge hours; `horizon` attribute contains the full slot-by-slot schedule |
| `sensor.smartgrid_fictive_plan` | h | Shadow plan from the DP optimizer (for comparison/regret) |
| `sensor.smartgrid_solar_charge` | kWh | Solar energy captured into the battery |
| `sensor.smartgrid_daily_regret` | EUR | Daily regret vs. perfect-hindsight oracle |
| `sensor.smartgrid_7d_dp_vs_heuristic_regret_delta` | EUR | 7-day rolling DP vs. heuristic cost delta |
| `sensor.smartgrid_over_buy` | kWh | Energy bought from grid that wasn't needed |
| `sensor.smartgrid_under_buy` | kWh | Energy shortfall vs. plan |
| `sensor.smartgrid_load_forecast_mae` | W | Load forecast mean absolute error |
| `sensor.smartgrid_24h_horizon_energy_mae` | kWh | 24-hour horizon energy forecast error |
| `sensor.smartgrid_12h_horizon_energy_mae` | kWh | 12-hour horizon energy forecast error |
| `sensor.smartgrid_load_forecast_pinball_p50` | W | P50 pinball loss (calibration metric) |
| `sensor.smartgrid_load_forecast_pinball_p80` | W | P80 pinball loss (calibration metric) |
| `sensor.smartgrid_active_load_model` | — | Which forecasting tier is active |

### Plan sensor attributes

The `sensor.smartgrid_plan` entity exposes the full schedule as attributes:

- **`horizon`** — list of per-slot dicts with price, charge/export decisions, SoC trajectory, and PV forecast
- **`deadline`** — when the plan horizon ends
- **`arbitrage_pnl`** — estimated export revenue (EUR)
- **`slot_minutes`** — planning slot length (60 or 15)
- **`efficiency_curve`** — measured efficiency bin table (when enabled)

## Dashboard

A ready-to-use [ApexCharts](https://github.com/RomRider/apexcharts-card) plan visualization card is included in [`lovelace/apexcharts-plan-card.yaml`](lovelace/apexcharts-plan-card.yaml). It shows the price curve, charge/export schedule, and SoC trajectory on a single chart.

**Requires** two HACS frontend cards:
- `apexcharts-card`
- `config-template-card`

## Add-on

`addon/anker_x1_forecast/` is an optional companion add-on that offloads house-load forecasting to a dedicated container running scikit-learn.

**What it does:**
- Trains an HGBR (histogram gradient-boosted regression) model on the integration's recorder history
- Serves P50/P80 load predictions via HTTP (`POST /predict`)
- Runs in a glibc container (`python:3.12-slim`) because HAOS's musl/aarch64 Python has no prebuilt sklearn wheel

**The integration works fine without it** — it falls back to in-process forecasting tiers (profile averages, then a locally trained model once enough data accumulates).

To enable: install the add-on, then toggle **Use forecast add-on** in the integration's options under Load model & ML.

See [`addon/anker_x1_forecast/README.md`](addon/anker_x1_forecast/README.md) for deployment instructions and API reference.

## Architecture

```
controller.py          tick loop — reads sensors, runs planner, sends setpoint
  ├── plan.py          assembles forecasts into a planning frame
  ├── optimize.py      dynamic-programming optimizer (charge + export co-optimization)
  ├── scheduler.py     heuristic fallback scheduler
  ├── forecast.py      PV production forecast (Open-Meteo / Forecast.Solar / Solcast)
  ├── loadmodel.py     house-load forecasting (profile → HGBR → add-on tiers)
  ├── load_adapt.py    intraday residual corrector
  ├── efficiency.py    measured charge/discharge efficiency curve
  ├── export_filter.py post-DP export cleanup (min-block, tail-trim, peak-band)
  ├── regret.py        oracle hindsight regret scoring
  ├── pricing_store.py rolling realized-price history for price anticipation
  ├── recorder.py      per-minute sample DB + hourly rollups (SQLite)
  ├── actuator.py      Modbus setpoint writer (VPP work mode)
  └── guard.py         pre-actuation safety checks
```

## Safety model

The integration does **not** enforce its own safety floor. Instead:

1. **Firmware floor (5%)** — the X1 hardware refuses to discharge below 5% SoC regardless of what the integration requests
2. **Firmware ceiling (100%)** — charging stops at 100% regardless of setpoint
3. **Configurable soft margins** — `soc_floor` (default 5%) and `soc_target` (default 97%) tell the planner where to aim, but the firmware is the hard backstop
4. **Master switch** — disable actuation instantly without uninstalling

## Development

```bash
# Install dependencies
pip install -e ".[dev]"

# Run tests
python -m pytest

# Lint + type check
ruff check .
pyright
```

The project uses [python-semantic-release](https://python-semantic-release.readthedocs.io/) for versioning — commit messages follow the [Angular convention](https://www.conventionalcommits.org/).
