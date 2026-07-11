"""Pure energy model: forward SoC simulation and solar deficit."""
from __future__ import annotations

from datetime import datetime, timedelta

from .models import Config, ForecastInterval
from . import const, resolution


def simulate_soc(
    soc: float,
    intervals: list[ForecastInterval],
    cfg: Config,
    *,
    eta_curve=None,
) -> float:
    """Simulate SoC% forward over intervals using solar surplus only.

    Surplus (pv - load) is clamped to [0, max_charge_w], multiplied by
    eta_charge to get DC power into the battery, integrated over dt, and the
    SoC is capped at cfg.soc_target.

    ``eta_curve``, when given, supplies a power-dependent ``eta_charge(w)``
    (duck-typed; energy.py does not import efficiency to avoid a cycle).
    ``None`` reproduces the static ``cfg.eta_charge`` scalar byte-identically.
    """
    soc_sim = soc
    cap_wh = cfg.capacity_kwh * 1000.0
    for iv in intervals:
        surplus = max(0.0, iv.pv_w - iv.load_w)
        ac_power = min(surplus, cfg.max_charge_w)
        eta_c = cfg.eta_charge if eta_curve is None else eta_curve.eta_charge(ac_power)
        dc_energy_wh = ac_power * eta_c * iv.dt_h
        soc_sim += dc_energy_wh / cap_wh * 100.0
        if soc_sim >= cfg.soc_target:
            return cfg.soc_target
    return soc_sim


def ride_out_reserve_kwh(
    now: datetime,
    intervals: list[ForecastInterval],
    cfg: Config,
    *,
    max_window_h: int = const.RESERVE_WINDOW_MAX_H,
    is_cheap: dict[datetime, bool] | None = None,
    slot_minutes: int = 60,
    eta_curve=None,
) -> float:
    """DC kWh floor to ride out from *now* to the next sustained solar recovery.

    ``reserve = floor_kwh + (debit-only DC drawdown from *now* to the SoC trough)``

    * **Debit-only**: only ``max(0, load-pv)`` hours add to the reserve.  A solar
      surplus hour is used ONLY to locate the trough (the deepest point of the
      signed forward trajectory), never to bank optimistic PV credit against a
      later deficit — so the survival floor never depends on morning PV that may
      not materialise.
    * **Trough-anchored**: the window ends at the trough, so a brief dawn PV blip
      before a breakfast spike does NOT end it early (the later, deeper deficit is
      still inside ``[now, trough]``).
    * **Physics**: AC deficit → DC drawdown via ``/eta_discharge``
      (= ``min(round_trip_eff/eta_charge, 1)``), matching the displayed
      projected-SoC line (plan.py), NOT ``eta_charge``.
    * Clamped to ``[floor_kwh, capacity_kwh]``.  ``max_window_h`` bounds the walk
      so a recovery-free multi-cloudy stretch cannot bleed the *next* night's
      drawdown into this reserve (drawdown past the pack → reserve pins to
      capacity → export disabled that hour — survival-correct).
    * **Cheap-relief early-break (rev-2)**: when ``is_cheap`` is given (hour-start →
      bool), the walk STOPS (freezing ``reserve_at_trough``) at the first hour STRICTLY
      after ``now`` flagged cheap — so weak-solar reserves bridge to the next genuine
      cheap grid hour instead of a full day's gross load.  Solar recovery is still
      handled by the signed-trough capture (no separate solar stop).  ``is_cheap=None``
      (or an hour absent from the map) ⇒ legacy walk (byte-identical / parity-safe).
    * ``eta_curve``, when given, supplies power-dependent ``eta_charge(w)`` /
      ``eta_discharge(w)`` (duck-typed; no ``efficiency`` import here to avoid a
      cycle). ``eta_curve=None`` reproduces the static scalar walk byte-identically.
    * ``cfg.idle_drain_w`` (default ``0.0``): constant DC standby drain added to
      every deficit hour's drawdown (inverter/BMS idle load the AC deficit
      signal doesn't see). ``0.0`` reproduces the walk byte-identically.
    """
    cap_kwh = cfg.capacity_kwh
    floor_kwh = cfg.soc_floor / 100.0 * cap_kwh
    eta_c = cfg.eta_charge if cfg.eta_charge > 1e-9 else 1.0
    # Discharge half of the round-trip; mirrors optimize.eta_discharge / plan.py.
    # Inlined to avoid an energy -> optimize import cycle.
    eta_d = min(cfg.round_trip_eff / eta_c, 1.0)

    horizon_end = now + timedelta(hours=max_window_h)
    signed_cum = 0.0         # signed DC trajectory vs *now* (credit only locates trough)
    debit_cum = 0.0          # debit-only DC drawdown accumulated so far
    trough_signed = 0.0      # most-negative signed_cum seen (0 = no net drawdown yet)
    reserve_at_trough = 0.0  # debit_cum at that trough

    for iv in sorted(intervals, key=lambda i: i.start):
        if iv.start < now:
            continue
        if iv.start >= horizon_end:
            break
        # rev-2: bridge only to the next genuinely-cheap grid hour AFTER now.
        if is_cheap is not None and iv.start > now:
            _h = resolution.floor_to_slot(iv.start, slot_minutes)
            if is_cheap.get(_h, False):
                break
        deficit_w = max(0.0, iv.load_w - iv.pv_w)
        if deficit_w > 0.0:
            _eta_d = eta_d if eta_curve is None else eta_curve.eta_discharge(deficit_w)
            draw = deficit_w / _eta_d * iv.dt_h / 1000.0 + cfg.idle_drain_w * iv.dt_h / 1000.0
            debit_cum += draw
            signed_cum -= draw
            if signed_cum < trough_signed:
                trough_signed = signed_cum
                reserve_at_trough = debit_cum
        else:
            surplus_w = iv.pv_w - iv.load_w
            charge_w = min(surplus_w, cfg.max_charge_w)
            _eta_c = eta_c if eta_curve is None else eta_curve.eta_charge(charge_w)
            signed_cum += charge_w * _eta_c * iv.dt_h / 1000.0

    return min(floor_kwh + reserve_at_trough, cap_kwh)


def export_surplus_kwh(soc: float, reserve: float, cfg: Config) -> float:
    """DC kWh available above the ride-out reserve (clamp ≥ 0).

    If the battery SoC is at or below ``cfg.soc_floor``, returns 0 — there is
    nothing to export when already at the safety floor.
    """
    floor_kwh = cfg.soc_floor / 100.0 * cfg.capacity_kwh
    soc_kwh = soc / 100.0 * cfg.capacity_kwh

    if soc_kwh <= floor_kwh:
        return 0.0

    return max(0.0, soc_kwh - reserve)


def export_net_target_w(surplus_kwh: float, cfg: Config, *, eta_curve=None) -> float:
    """Net AC watts to sell ``surplus_kwh`` (DC kWh above the ride-out reserve).

    Drains the surplus over ``cfg.export_drain_window_h`` hours, floored at one
    controller tick (``const.TICK_SECONDS``). The default ``0.0`` collapses the
    window to one tick, so the rate is the export cap until the final tick — a
    decisive dump that drives the live SoC down to the ride-out reserve
    (closed-loop on measured SoC; the executor's eps_lo dead-band and one-tick
    granularity bound the stop just above the reserve).
    ``export_drain_window_h = 1.0`` reproduces the legacy ~1-hour
    exponential. Capped by ``max_export_w`` / ``grid_export_limit_w``.

    ``eta_d`` is inlined (mirrors ``optimize.eta_discharge``) to avoid an
    ``energy -> optimize`` import cycle, matching ``ride_out_reserve_kwh``.
    ``eta_curve``, when given, supplies a power-dependent
    ``eta_discharge(w)`` (duck-typed); ``None`` reproduces the static
    ``eta_d`` scalar byte-identically.
    """
    eta_c = cfg.eta_charge if cfg.eta_charge > 1e-9 else 1.0
    eta_d = min(cfg.round_trip_eff / eta_c, 1.0)
    drain_h = max(cfg.export_drain_window_h, const.TICK_SECONDS / 3600.0)
    if eta_curve is None:
        surplus_w = surplus_kwh * eta_d * 1000.0 / drain_h
    else:
        _eta_d = eta_curve.eta_discharge(surplus_kwh * 1000.0 / drain_h)
        surplus_w = surplus_kwh * _eta_d * 1000.0 / drain_h
    return min(surplus_w, cfg.max_export_w, cfg.grid_export_limit_w)
