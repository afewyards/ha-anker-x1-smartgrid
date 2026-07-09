"""Pure data models for Anker X1 SmartGrid."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from . import const


class ControllerState(str, Enum):
    PASSIVE = "passive"
    FORCING = "forcing"


@dataclass(frozen=True)
class Config:
    capacity_kwh: float = const.DEFAULT_CAPACITY_KWH
    soc_floor: float = const.DEFAULT_SOC_FLOOR
    soc_target: float = const.DEFAULT_SOC_TARGET
    max_charge_w: float = const.DEFAULT_MAX_CHARGE_W
    eta_charge: float = const.DEFAULT_ETA_CHARGE
    deadline_buffer_min: int = const.DEFAULT_DEADLINE_BUFFER_MIN
    peak_k: float = const.DEFAULT_PEAK_K
    peak_after_hour: int = const.DEFAULT_PEAK_AFTER_HOUR
    min_dwell_min: int = const.DEFAULT_MIN_DWELL_MIN
    deadband_w: float = const.DEFAULT_DEADBAND_W
    lookback_days: int = const.DEFAULT_LOOKBACK_DAYS
    retention_days: int = const.DEFAULT_RETENTION_DAYS
    use_learned_model: bool = const.DEFAULT_USE_LEARNED_MODEL
    retrain_hours: int = const.DEFAULT_RETRAIN_HOURS
    min_train_samples: int = const.DEFAULT_MIN_TRAIN_SAMPLES
    train_days: int = const.DEFAULT_TRAIN_DAYS
    backtest_test_days: int = const.DEFAULT_BACKTEST_TEST_DAYS
    round_trip_eff: float = const.DEFAULT_ROUND_TRIP_EFF
    charge_margin_eur_per_kwh: float = const.DEFAULT_CHARGE_MARGIN_EUR_PER_KWH
    ent_weather_forecast: str = const.DEFAULT_ENT_WEATHER_FORECAST
    retention_hourly_days: int = const.DEFAULT_RETENTION_HOURLY_DAYS
    addon_enabled: bool = const.DEFAULT_ADDON_ENABLED
    addon_url: str = const.DEFAULT_ADDON_URL
    addon_timeout: int = const.DEFAULT_ADDON_TIMEOUT
    ent_export_price: str = const.DEFAULT_ENT_EXPORT_PRICE
    trough_percentile: float = const.DEFAULT_TROUGH_PERCENTILE
    trough_lookahead_h: int = const.DEFAULT_TROUGH_LOOKAHEAD_H
    min_horizon_h: int = const.DEFAULT_MIN_HORIZON_H
    water_value_factor: float = const.DEFAULT_WATER_VALUE_FACTOR
    clamp_water_value_nonneg: bool = const.DEFAULT_CLAMP_WATER_VALUE_NONNEG
    end_soc_deadband: float = const.DEFAULT_END_SOC_DEADBAND
    # Cheapest-hour charge gate: hour must be within this spread (€/kWh) above the
    # window's minimum price.  ANDed with the peak-based ceiling.  0.005 means only
    # the trough ± 0.005 €/kWh is chargeable; raise to widen the window.
    charge_window_price_band: float = const.DEFAULT_CHARGE_WINDOW_PRICE_BAND
    # Hours of real-price look-back for the cheap-charge band trough (per-hour
    # windowed trough).  0 = look-back off.  See const.DEFAULT_CHARGE_TROUGH_LOOKBACK_H.
    charge_trough_lookback_h: int = const.DEFAULT_CHARGE_TROUGH_LOOKBACK_H
    # Export / arbitrage options (A2)
    enable_export: bool = const.DEFAULT_ENABLE_EXPORT
    max_export_w: float = const.DEFAULT_MAX_EXPORT_W
    grid_export_limit_w: float = const.DEFAULT_GRID_EXPORT_LIMIT_W
    cycle_cost_eur_per_kwh: float = const.DEFAULT_CYCLE_COST_EUR_PER_KWH
    export_eps_lo_kwh: float = const.DEFAULT_EXPORT_EPS_LO_KWH
    export_eps_hi_kwh: float = const.DEFAULT_EXPORT_EPS_HI_KWH
    export_dwell_min: int = const.DEFAULT_EXPORT_DWELL_MIN
    export_fee_eur_per_kwh: float = const.DEFAULT_EXPORT_FEE_EUR_PER_KWH
    export_peak_band_frac: float = const.DEFAULT_EXPORT_PEAK_BAND_FRAC
    export_peak_lookback_h: int = const.DEFAULT_EXPORT_PEAK_LOOKBACK_H
    export_min_block_kwh: float = const.DEFAULT_EXPORT_MIN_BLOCK_KWH
    export_load_comp_factor: float = const.DEFAULT_EXPORT_LOAD_COMP_FACTOR
    export_drain_window_h: float = const.DEFAULT_EXPORT_DRAIN_WINDOW_H
    # Persistence price prior options (Plan B)
    price_history_days: int = const.DEFAULT_PRICE_HISTORY_DAYS
    price_blend_weight_today: float = const.DEFAULT_PRICE_BLEND_WEIGHT_TODAY
    anticipation_confidence_haircut: float = const.DEFAULT_ANTICIPATION_CONFIDENCE_HAIRCUT
    anticipation_margin_eur_per_kwh: float = const.DEFAULT_ANTICIPATION_MARGIN_EUR_PER_KWH
    # SoC drift-hedge tunables (default off: fraction=0.0 → byte-identical / parity-safe)
    soc_hedge_fraction: float = const.DEFAULT_SOC_HEDGE_FRACTION
    soc_drift_deadband_kwh: float = const.DEFAULT_SOC_DRIFT_DEADBAND_KWH
    soc_drift_decay_halflife_h: float = const.DEFAULT_SOC_DRIFT_DECAY_HALFLIFE_H
    # Intraday residual corrector (Layer A): fraction=0.0 disables (byte-identical).
    load_adapt_fraction: float = const.DEFAULT_LOAD_ADAPT_FRACTION
    load_adapt_window_h: int = const.DEFAULT_LOAD_ADAPT_WINDOW_H
    load_adapt_fade_h: int = const.DEFAULT_LOAD_ADAPT_FADE_H
    # Ride-to-trough reserve (rev-2): anchor selector + cheap-relief band.
    reserve_anchor: str = const.DEFAULT_RESERVE_ANCHOR
    reserve_cheap_band: float = const.DEFAULT_RESERVE_CHEAP_BAND
    # Price-slot resolution override; "auto" -> detect per refresh from slot spacing.
    slot_resolution: str = const.DEFAULT_SLOT_RESOLUTION
    # Measured efficiency curve: False keeps static eta_charge/round_trip_eff (byte-identical).
    use_measured_eta: bool = const.DEFAULT_USE_MEASURED_ETA

    @classmethod
    def from_dict(cls, d: dict) -> "Config":
        fields = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in fields})


@dataclass(frozen=True)
class PriceSlot:
    start: datetime
    price: float  # €/kWh all-in
    duration_min: float | None = None


@dataclass(frozen=True)
class ForecastInterval:
    start: datetime
    pv_w: float
    load_w: float
    dt_h: float


@dataclass(frozen=True)
class PlantInputs:
    soc: float
    # SIGNED per-phase power in watts: positive = importing from grid,
    # negative = exporting to grid.  Not import-only.
    phase_import_w: tuple[float, float, float]
    now: datetime


@dataclass
class PlanState:
    state: ControllerState
    state_since: datetime
    committed_slots: tuple[datetime, ...] = ()
    committed_charge_kwh: float = 0.0
    # Review 1.3: the slot this committed charge belongs to. A commit is only
    # honoured by the deadband-hold hysteresis when it matches the CURRENT
    # slot — a stale carry-over from a previous slot must not re-inject it.
    committed_charge_slot: datetime | None = None

    def to_dict(self) -> dict:
        return {
            "state": self.state.value,
            "state_since": self.state_since.isoformat(),
            "committed_slots": [s.isoformat() for s in self.committed_slots],
            "committed_charge_kwh": self.committed_charge_kwh,
            "committed_charge_slot": (
                self.committed_charge_slot.isoformat()
                if self.committed_charge_slot else None
            ),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PlanState":
        return cls(
            state=ControllerState(d["state"]),
            state_since=datetime.fromisoformat(d["state_since"]),
            committed_slots=tuple(
                datetime.fromisoformat(s) for s in d.get("committed_slots", [])
            ),
            committed_charge_kwh=float(d.get("committed_charge_kwh", 0.0)),
            committed_charge_slot=(
                datetime.fromisoformat(d["committed_charge_slot"])
                if d.get("committed_charge_slot") else None
            ),
        )

    @classmethod
    def initial(cls, now: datetime) -> "PlanState":
        return cls(ControllerState.PASSIVE, now, (), 0.0)


@dataclass
class ExportState:
    """Dwell/hysteresis state for export engagement (C2)."""

    engaged: bool
    state_since: datetime

    def to_dict(self) -> dict:
        return {
            "engaged": self.engaged,
            "state_since": self.state_since.isoformat(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ExportState":
        return cls(
            engaged=bool(d["engaged"]),
            state_since=datetime.fromisoformat(d["state_since"]),
        )

    @classmethod
    def initial(cls, now: datetime) -> "ExportState":
        return cls(engaged=False, state_since=now)
