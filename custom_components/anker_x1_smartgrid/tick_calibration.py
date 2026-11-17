"""Periodic full-charge calibration override applied to the controller tick's plan."""

from __future__ import annotations

import dataclasses
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from . import calibration, const
from .models import ControllerState, PlanState, PlantInputs, PriceSlot

if TYPE_CHECKING:
    from .controller import Controller

_LOGGER = logging.getLogger(__name__)


async def tick_override(
    controller: Controller,
    now: datetime,
    *,
    horizon: list[dict],
    inputs: PlantInputs,
    _slot_minutes: int,
    _export_price: float | None,
    _export_slots: list[PriceSlot],
    _export_matches_import: bool,
    delivered_now: dict,
    _calibration_prev: calibration.CalibPlan,
    _dp_out: dict,
    new_plan: PlanState,
) -> PlanState:
    """Re-evaluate the calibration cycle for this tick and return ``new_plan`` with its FORCING override applied."""
    # ── Periodic full-charge calibration override ──────────────────────
    # Deliberately non-economic, and the ONLY force-charge path left after
    # economic-only charging removed the rest.  Quarantined here rather
    # than expressed as a DP constraint so the parity-gated core is
    # untouched.  See the spec for the stranded-capacity evidence.
    #
    # Was self.plan FORCING because of calibration, as of the end of the
    # PREVIOUS tick? Captured before self._calibration_engaged updates
    # below and before self.plan is reassigned further down.
    _calibration_was_engaged = controller._calibration_engaged
    controller._calibration_last_success = None
    controller._calibration_days_since = None
    if controller.cfg.calibration_enabled and controller._recorder is not None:
        # F5: the never-calibrated fallback (calibration.compute_days_since)
        # requires history_span_days >= calibration_interval_days, and span
        # is capped by recorder retention -- so this combo can never become
        # due from a cold start. Log once; config doesn't change without a
        # restart, so no re-arm is needed.
        if (
            not controller._calibration_retention_warned
            and controller.cfg.retention_days < controller.cfg.calibration_interval_days
        ):
            controller._calibration_retention_warned = True
            _LOGGER.warning(
                "retention_days=%d < calibration_interval_days=%d; a pack that has "
                "never completed a calibration dwell can never become due "
                "(history can't span the interval) until one of these is changed",
                controller.cfg.retention_days,
                controller.cfg.calibration_interval_days,
            )
        try:
            _since_iso = (now - timedelta(days=controller.cfg.calibration_interval_days * 3)).isoformat()
            _price_hist = controller._price_store.history if controller._price_store is not None else {}
            # Window PLACEMENT and cost both read the DP's own plan so a
            # window at the solar top is not costed as though it still had
            # to charge from this morning's empty pack. One-directional:
            # the DP never sees calibration back. Estimated rows,
            # past-actual rows, and rows that have already fully elapsed
            # are excluded -- that tail is display-only synthesis (or
            # already-measured history), and sizing or costing a real
            # charge against fabricated or stale numbers no optimiser
            # produced would place or price the window on nothing real.
            _cal_slots = calibration.build_calib_slots(
                horizon,
                now,
                inputs.soc,
                _slot_minutes,
                controller._export_price_fn(
                    now, horizon, _export_price, _export_slots, _slot_minutes, _export_matches_import
                ),
                controller._delivered_fn(delivered_now, _slot_minutes),
            )
            (
                controller._calibration_last_success,
                controller._calibration_plan,
                _span_days,
            ) = await controller._hass.async_add_executor_job(
                controller._calibration_compute_sync,
                _since_iso,
                now,
                inputs.soc,
                _cal_slots,
                _price_hist,
                _calibration_prev,
                _dp_out.get("water_value"),
            )
            # Narrowing: `scheduled` and `idle` yield None, so nothing
            # below can actuate on a window that has not started.
            controller._calibration = controller._calibration_plan.action
            # Shared with calibration_action's own days-since fallback (F3):
            # both sites call calibration.compute_days_since so they cannot
            # drift out of sync.
            controller._calibration_days_since = calibration.compute_days_since(
                controller._calibration_last_success, _span_days, now, controller.cfg
            )
            controller._calibration_fail_logged = False

            # Past interval+grace `force` is set unconditionally, so the
            # cheapest window is bought every day regardless of price. That
            # is correct while converging on a cycle, but a pack that can
            # never reach calibration_top_soc would sit here indefinitely
            # with nothing in the log. Warn once, re-arm on success.
            _overdue_at = controller.cfg.calibration_interval_days + const.CALIBRATION_GRACE_DAYS
            _since = controller._calibration_days_since
            if _since is not None and _since >= _overdue_at:
                if not controller._calibration_overdue_warned:
                    controller._calibration_overdue_warned = True
                    # The start-SoC gate is HARD: past interval+grace it
                    # still suppresses every window, so the forcing message
                    # below would be false. plan_peak_soc approximates --
                    # it is not identical to -- the policy's own
                    # per-candidate soc_start gate (select_window skips a
                    # candidate whose plan SoC at ITS OWN start is below
                    # CALIBRATION_MIN_START_SOC); this reads the plan's
                    # single overall peak, which is enough for this log
                    # line without a second reading of the horizon.
                    _peak = calibration.plan_peak_soc(inputs.soc, _cal_slots)
                    if _peak < const.CALIBRATION_MIN_START_SOC:
                        _LOGGER.warning(
                            "Calibration overdue by %.1f days (past interval %d + grace %d) but the plan "
                            "peaks at %.0f%% (< %.0f%%): no window will be planned until the plan climbs, "
                            "since the grid would otherwise buy the whole charge to "
                            "calibration_top_soc=%.0f%%",
                            _since,
                            controller.cfg.calibration_interval_days,
                            const.CALIBRATION_GRACE_DAYS,
                            _peak,
                            const.CALIBRATION_MIN_START_SOC,
                            controller.cfg.calibration_top_soc,
                        )
                    else:
                        _LOGGER.warning(
                            "Calibration overdue by %.1f days (past interval %d + grace %d): taking the "
                            "cheapest window up to €%.2f with the price bar bypassed. If this persists, check "
                            "calibration_cost_eur — either the pack is not reaching "
                            "calibration_top_soc=%.0f%% (check batt_w during the hold) or every window costs "
                            "more than the cap and the cycle is waiting it out",
                            _since,
                            controller.cfg.calibration_interval_days,
                            const.CALIBRATION_GRACE_DAYS,
                            const.CALIBRATION_OVERDUE_COST_CAP_EUR,
                            controller.cfg.calibration_top_soc,
                        )
            elif _since is not None and _since < controller.cfg.calibration_interval_days:
                controller._calibration_overdue_warned = False
        except Exception:
            # Fail-closed: never force a charge on a failed read. F2: log
            # once per failure streak (not every 60s tick), re-arm on the
            # next success.
            if not controller._calibration_fail_logged:
                controller._calibration_fail_logged = True
                _LOGGER.warning("Calibration policy failed; skipping this tick", exc_info=True)
            controller._calibration = None
            controller._calibration_plan = calibration.CalibPlan(phase="idle", window_start=None, window_end=None)
            controller._calibration_last_success = None
            controller._calibration_days_since = None

    if controller._calibration is not None:
        controller._calibration_engaged = True
        if new_plan.state is not ControllerState.FORCING:
            # Reuse self.plan.state_since whenever self.plan is already
            # FORCING (economic or calibration) so a scheduler bail to
            # PASSIVE doesn't re-stamp `now` every tick and corrupt the
            # dwell timer. Only a genuine PASSIVE->FORCING entry gets `now`.
            _since = controller.plan.state_since if controller.plan.state is ControllerState.FORCING else now
            new_plan = dataclasses.replace(new_plan, state=ControllerState.FORCING, state_since=_since)
        # else: already FORCING (scheduler coasting, or a genuine
        # economic FORCING coincides with calibration) -- leave it.
    else:
        controller._calibration_engaged = False
        if (
            _calibration_was_engaged
            and new_plan is controller.plan
            and new_plan.state is ControllerState.FORCING
            and not _dp_out.get("now_selected", False)
        ):
            # scheduler.decide_state's dwell/now_selected short-circuits
            # return the SAME PlanState object on every coasting tick, not
            # just on entry -- so identity alone can't tell "coasting on
            # calibration's own mandate" from "coasting on a still-wanted
            # economic FORCING". `_dp_out["now_selected"]` (decision.py,
            # from the same selected_slots passed to decide_state this
            # tick) is the extra, real signal: only cancel when the DP
            # does not currently want this hour.
            #
            # state_since=self.plan.state_since, not `now`: decide_state's
            # own PASSIVE->FORCING re-entry is dwell-gated on that same
            # timestamp, so a fresh `now` would lock out a legitimate
            # re-charge for up to min_dwell_min instead of one tick.
            # state_since's only other reader is that same dwell_elapsed
            # check (plus PlanState.to_dict/from_dict round-tripping it
            # through persistence) -- nothing else surfaces it.
            new_plan = dataclasses.replace(
                new_plan, state=ControllerState.PASSIVE, state_since=controller.plan.state_since
            )
    return new_plan
