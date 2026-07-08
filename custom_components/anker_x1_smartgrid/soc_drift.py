"""Pure SoC drift-hedge accumulator (measured-ΔSoC vs forecast-expected). No I/O; unit-testable.

Integrates `expected_passive_ΔSoC − measured_ΔSoC` since the local-day reset: how far the REAL
pack fell short of what the raw pv/load forecast implied a passive battery would do. Positive =
running BELOW the morning plan; the hedge debits the forward SoC curve by a fraction so the DP
books the cheapest recovery. Measured SoC is ground truth, so a deliberate grid-charge recovery
(SoC ↑) yields a negative per-step and shrinks the accumulator next tick — the loop
self-stabilizes. `expected_soc_delta` is reality-anchored (÷eta_discharge on a deficit),
deliberately ≠ the DP's own 1:1 load discharge. The caller passes deliberate battery→grid export
(duration-scaled) for add-back, GATES the step at the SoC rails / dt anomalies, and supplies the
forecast via `forecast_rate_at` over the SAME intervals the DP consumes.
"""
from __future__ import annotations

from datetime import datetime, timedelta

MAX_DRIFT_STEP_H = 0.25


def expected_soc_delta_kwh(forecast_pv_w: float, forecast_load_w: float, dt_h: float,
                           eta_charge: float, eta_discharge: float) -> float:
    """DC SoC change a PASSIVE battery would make under the raw forecast (η-aware, reality basis)."""
    if not (0.0 < dt_h <= MAX_DRIFT_STEP_H):
        return 0.0
    net_kwh = (forecast_pv_w - forecast_load_w) * dt_h / 1000.0  # AC, signed
    if net_kwh >= 0.0:
        return net_kwh * eta_charge          # surplus charges DC at η_c
    return net_kwh / eta_discharge           # deficit discharges DC at /η_d (stays negative)


def measured_soc_delta_kwh(soc_now_pct: float, soc_prev_pct: float, capacity_kwh: float) -> float:
    return (soc_now_pct - soc_prev_pct) / 100.0 * capacity_kwh


def per_step_drift_kwh(expected_dc_kwh: float, measured_dc_kwh: float,
                       intentional_export_dc_kwh: float = 0.0) -> float:
    # Add back deliberate battery→grid export so our own export isn't read as forecast drift.
    return expected_dc_kwh - (measured_dc_kwh + intentional_export_dc_kwh)


def decay(prev_kwh: float, dt_h: float, halflife_h: float) -> float:
    if halflife_h <= 0.0 or dt_h <= 0.0:
        return prev_kwh
    return prev_kwh * (0.5 ** (dt_h / halflife_h))


def accumulate(prev_kwh: float, per_step_kwh: float, *,
               dt_h: float = 0.0, halflife_h: float = 0.0) -> float:
    return decay(prev_kwh, dt_h, halflife_h) + per_step_kwh


def cap_accumulator(accumulator_kwh: float, max_kwh: float) -> float:
    """Absolute runaway sanity bound (NOT solar-based). Negatives pass through unchanged."""
    return min(accumulator_kwh, max_kwh)


def reset_if_new_day(accumulator_kwh: float, stored_day: str | None,
                     today_key: str) -> tuple[float, str]:
    if stored_day != today_key:
        return 0.0, today_key
    return accumulator_kwh, today_key  # stored_day == today_key here, both are the same str


def drift_kwh(accumulator_kwh: float, engage_deadband_kwh: float,
              release_deadband_kwh: float, engaged_prev: bool) -> tuple[float, bool]:
    """Behind-only (max 0) + two-level hysteresis. Returns (drift_kwh, engaged_now)."""
    d = max(0.0, accumulator_kwh)
    threshold = release_deadband_kwh if engaged_prev else engage_deadband_kwh
    engaged = d >= threshold
    return (d if engaged else 0.0), engaged


def forecast_rate_at(intervals, when: datetime) -> tuple[float, float]:
    """P50 (pv_w, load_w) of the ForecastInterval covering `when`; (0.0, 0.0) if none.

    Same forecast the DP's window is built from — keeps the accumulator and the DP on one source.
    """
    for iv in (intervals or []):
        if iv.start <= when < iv.start + timedelta(hours=iv.dt_h):
            return iv.pv_w, iv.load_w
    return 0.0, 0.0
