"""Tick phases that publish and persist per-tick state, moved out of ``Controller._tick_impl``."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from . import calibration

if TYPE_CHECKING:
    from .controller import Controller


def publish(
    controller: Controller,
    _export_setpoint_w: float | None,
    _house_load_now_w: float,
    _dp_out: dict,
    horizon: list[dict],
    deadline: datetime | None,
    _slot_minutes: int,
) -> None:
    """Re-add the enabled tick's observability keys to the freshly rebuilt ``last_status``."""
    # E2: surface the live export setpoint for observability only.
    # _status publishes 0.0/"passive" because export runs in the non-FORCING
    # branch with setpoint=0.0 — state is intentionally left untouched.
    # The recorder's smartcharge_state column (_record_sample, called above
    # at line 2201) already always records the plan state ("passive" during
    # export), so leaving last_status["state"] alone matches the recorded
    # history instead of diverging from it. A dedicated sensor reads the key.
    controller.last_status["export_setpoint_w"] = _export_setpoint_w
    # T16: surface the live per-tick house load for observability. Mirrors
    # export_setpoint_w above — this line only runs in the enabled/"ok" tick
    # path. CORRECTION: _status() REBINDS last_status (it does not mutate a
    # persistent dict), so a disabled/failsafe tick DOES drop this key until
    # the next enabled tick re-adds it — same as export_setpoint_w and
    # terminal_v_hi. Only "daily_stats" is deliberately carried across the
    # rebuild (see _status). A dedicated sensor reads the key.
    controller.last_status["house_load_w"] = _house_load_now_w
    controller.last_status["low_power_hold_active"] = controller.hold_state.engaged
    controller.last_status["hold_discharge_value_eur_per_kwh"] = controller.last_hold_values[0]
    controller.last_status["hold_stored_value_eur_per_kwh"] = controller.last_hold_values[1]
    controller._kept_by_slot = _dp_out.get("kept_by_slot")
    controller._kept_src_by_slot = _dp_out.get("kept_src_by_slot")
    # Task 4: surface the DP optimizer's terminal water-value artefacts for
    # dashboard/diagnostic observability only. Absent from _dp_out on DP
    # failure (dict left empty at declaration) → last_status carries None.
    controller.last_status["terminal_v_hi"] = _dp_out.get("terminal_v_hi")
    controller.last_status["terminal_need_kwh"] = _dp_out.get("terminal_need_kwh")
    # R1: surface the DP's export-curve engagement (decision.py writes these
    # into _dp_out) for the dashboard/diagnostic sensor. _status() above just
    # rebuilt self.last_status wholesale, so — like terminal_v_hi/need_kwh —
    # this must be re-added every tick or sensor.py reads permanent None.
    controller.last_status["export_curve_covered"] = _dp_out.get("export_curve_covered")
    controller.last_status["export_curve_slots"] = _dp_out.get("export_curve_slots")
    # Periodic full-charge calibration (spec 2026-08-03): observability only.
    # Published from the PLAN, not the action, so a window accepted for
    # later today is drawable ("scheduled") rather than indistinguishable
    # from idle. The action stays the sole actuation path.
    _cal = controller._calibration_plan
    controller.last_status["calibration_state"] = _cal.phase
    controller.last_status["calibration_window_start"] = (
        _cal.window_start.isoformat() if _cal.window_start is not None else None
    )
    controller.last_status["calibration_window_end"] = (
        _cal.window_end.isoformat() if _cal.window_end is not None else None
    )
    controller.last_status["calibration_last_success"] = (
        controller._calibration_last_success.isoformat() if controller._calibration_last_success is not None else None
    )
    controller.last_status["calibration_days_since"] = controller._calibration_days_since
    # Cost-aware placement (spec 2026-09-10): the window the policy this
    # tick settled on, priced against the plan -- the accepted window while
    # scheduled/charging/holding, or the cheapest rejected candidate while
    # a due cycle sits idle. None when nothing could be costed.
    controller.last_status["calibration_cost_eur"] = round(_cal.cost_eur, 3) if _cal.cost_eur is not None else None
    # The band the card draws: charge target on top, continuation bar
    # below. Published rather than hardcoded in the card so tuning either
    # const cannot leave the chart lying about where the dwell counts.
    controller.last_status["calibration_target_soc"] = controller.cfg.calibration_top_soc
    controller.last_status["calibration_hold_soc"] = calibration.continue_soc(controller.cfg)
    # N2: sum(1 for ...) counts SLOTS, not hours — at 15-min that's a 4x
    # over-report under the "planned_grid_hours" name. Scale by the slot
    # width so the value stays true hours; a no-op at the legacy 60-min
    # default (_slot_minutes/60.0 == 1.0).
    _grid_slots = sum(1 for e in horizon if e["mode"] == "grid")
    controller.last_status["plan"] = {
        "horizon": horizon,
        "deadline": deadline.isoformat() if deadline else None,
        "planned_grid_hours": _grid_slots * (_slot_minutes / 60.0),
    }
