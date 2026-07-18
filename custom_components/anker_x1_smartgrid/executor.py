"""Live battery-actuation executor: FORCING charge + C3 export dispatch (Task E2).

Extracted verbatim from ``controller.py``'s ``_tick_impl`` — the highest-risk
code path in the project (tick lock, failsafe, anti-fight guard, hysteresis,
export executor). State ownership (``export_state``, ``plan``, forcing
latches, cash-ledger/drift accumulators) stays on ``Controller`` — every
function here takes the live ``controller`` instance and reads/writes its
attributes directly (mirrors the original ``self.`` access exactly) instead
of owning a private copy, because persistence (``_PERSIST_GROUPS``) and the
test suite reach these fields on ``Controller``.

Every log message, exception scope, awaiting order (including the
reset-before-release ordering on the export-disabled path), and hysteresis/
tick-lock interaction is unchanged from the pre-extraction code — this is a
mechanical ``self`` → ``controller`` move, not a rewrite.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from . import const, coordinator, energy, guard, optimize as optimize_mod, resolution, scheduler
from .decision import _build_is_cheap_by_hour
from .models import ControllerState, ExportState, PlanState, PlantInputs, PriceSlot

if TYPE_CHECKING:
    from .controller import Controller

_LOGGER = logging.getLogger(__name__)


async def safe_release(
    controller: Controller,
    now: datetime,
    context: str = "",
    *,
    release: bool = True,
    reset_export: bool = True,
    reset_before_release: bool = False,
) -> None:
    """Best-effort inverter release + export dwell-state reset.

    Consolidates the try/``release_to_self()``/log-error +
    ``ExportState(engaged=False, state_since=now)`` reset pattern repeated
    across the tick/failsafe/export-executor paths. ``context`` becomes the
    error-log message on a release failure, so each call site keeps its
    original diagnostic text.

    ``release``/``reset_export`` let a call site express a release-only or
    reset-only variant (some sites reset export state without ever having
    engaged the actuator; others release without touching export state
    directly — e.g. a local ``_new_export_state`` var is unified into
    ``controller.export_state`` later by the caller). The export-state reset
    is itself gated on ``controller.export_state.engaged`` (mirrors every
    original call site) so a no-op reset never bumps ``state_since``.

    ``reset_before_release`` mirrors the one call site (export-disabled
    path) whose original code reset ``controller.export_state`` BEFORE
    attempting the release rather than after — order matters there since
    ``release_to_self()`` awaits, and a concurrent reader (e.g. a sensor)
    could observe ``export_state`` mid-release.
    """
    if reset_export and reset_before_release and controller.export_state.engaged:
        controller.export_state = ExportState(engaged=False, state_since=now)
    if release:
        try:
            await controller._actuator.release_to_self()
        except Exception:
            _LOGGER.error(context, exc_info=True)
    if reset_export and not reset_before_release and controller.export_state.engaged:
        controller.export_state = ExportState(engaged=False, state_since=now)


async def run_forcing_and_export(
    controller: Controller,
    now: datetime,
    new_plan: PlanState,
    inputs: PlantInputs,
    slots: list[PriceSlot],
    _dp_out: dict,
    _ivs_reserve: list,
    _slot_minutes: int,
    _export_price: float | None,
    _house_load_now_w: float,
) -> tuple[float, bool, float | None, float | None, float | None, float | None]:
    """FORCING charge actuation + C3 live export executor (mutually exclusive).

    Returns ``(setpoint, engage_failed, export_setpoint_w, export_kwh,
    reserve_kwh_val, surplus_kwh_val)`` for the caller's ``_record_sample`` /
    decision-snapshot / status publication. Mutates ``controller.export_state``
    in place; does NOT touch ``controller.plan`` (the caller assigns
    ``new_plan`` to it afterwards — this function reads the OLD
    ``controller.plan`` for the FORCING→PASSIVE transition check).
    """
    _export_setpoint_w: float | None = None
    _export_kwh: float | None = None
    _reserve_kwh_val: float | None = None
    _surplus_kwh_val: float | None = None

    _engage_failed = False
    if new_plan.state is ControllerState.FORCING:
        setpoint = guard.command_setpoint(
            controller.cfg.max_charge_w,
            controller._actuator.last_setpoint_w,
            controller.cfg,
        )
        try:
            await controller._actuator.engage_and_charge(setpoint)
        except Exception:
            # Engage failed → the inverter may be half-engaged (modbus switch
            # already turned on, setpoint never written or the write itself
            # raised, e.g. a live-limit ServiceValidationError). Release the
            # hardware back to firmware control in THIS tick — same
            # release_to_self mechanism as the FORCING→PASSIVE transition
            # below — so the battery isn't left VPP-idle/frozen retrying
            # every tick. Publish truth for THIS tick regardless of whether
            # the release itself succeeds (best-effort): setpoint 0 + PASSIVE
            # is honest; controller.plan stays FORCING so the next tick
            # retries the engage.
            _LOGGER.error("Actuator engage_and_charge failed (FORCING path); publishing passive/0", exc_info=True)
            _engage_failed = True
            setpoint = 0.0
            await safe_release(
                controller,
                now,
                "Actuator release_to_self failed (FORCING engage-failure recovery)",
                reset_export=False,
            )
        else:
            # Actuator._engage may have further tightened `setpoint` via its
            # live-limit clamp (the setpoint entity's min/max are LIVE
            # inverter BMS limits that float and can be tighter than the
            # static guard.command_setpoint clamp above). Report what was
            # ACTUALLY WRITTEN to hardware, not the pre-clamp request, so
            # _record_sample / the published status stay truthful.
            setpoint = controller._actuator.last_setpoint_w
        # Mutual exclusion: export executor is skipped entirely while force-charging.
        # Release export state so we transition cleanly after force-charge ends.
        await safe_release(controller, now, release=False)
    else:
        setpoint = 0.0
        if controller.plan.state is ControllerState.FORCING:
            await safe_release(
                controller,
                now,
                "Actuator release_to_self failed (FORCING→PASSIVE transition)",
                reset_export=False,
            )

        # ── C3: live export executor ──────────────────────────────────────
        # Only fires when export is enabled and an export price is available.
        # A1 = NET-EXPORT: setpoint is export_rate directly (inverter serves
        # house load first, exports the remainder); no house_load_now term.
        if controller.cfg.enable_export and _export_price is not None and _export_price > 0.0:
            # Compute ride-out reserve and battery surplus above it.
            # _ivs_reserve is the TWO-DAY reserve interval list from compute_decision
            # (6th return element).  Trough-anchored: ride_out_reserve_kwh walks
            # forward to the deepest signed-trajectory point, matching the DP floor.
            # rev-2: under the trough anchor, hour-align now + thread the SAME
            # cheap-relief map as the plan so the live export floor matches the
            # planned floor at this hour. Legacy anchor keeps the raw `now` and
            # no map → byte-identical rollback behavior (unchanged from pre-rev-2).
            if controller.cfg.reserve_anchor == const.RESERVE_ANCHOR_TROUGH:
                _cur_h_reserve = resolution.floor_to_slot(now, _slot_minutes)
                _reserve_is_cheap = _build_is_cheap_by_hour(slots, controller.cfg, _slot_minutes)
            else:
                _cur_h_reserve = now
                _reserve_is_cheap = None
            _reserve = energy.ride_out_reserve_kwh(
                _cur_h_reserve,
                _ivs_reserve,
                controller.cfg,
                is_cheap=_reserve_is_cheap,
                slot_minutes=_slot_minutes,
                eta_curve=controller._planner_curve(),
            )
            _surplus = energy.export_surplus_kwh(inputs.soc, _reserve, controller.cfg)

            # Economic hurdle: does exporting now beat holding for later use?
            # Anchored to the MINIMUM price over the remaining horizon — the
            # same convention as the DP's terminal water value (decision.py)
            # and the oracle (regret_job.py) — NOT find_next_trough's earliest
            # qualifying local minimum, which can be far shallower than a
            # deeper refill sitting later in the same horizon (that mismatch
            # would price this ledger's opportunity cost differently than the
            # plan it's supposed to be scoring against).
            #
            # Two-segment terminal credit: when the DP stashed the overnight
            # terminal params (terminal_overnight_credit ON) and the live SoC
            # sits within the overnight-need band above the firmware floor,
            # this ledger uses the same richer terminal_v_hi the DP is
            # crediting that energy at. Above the band, or when the keys are
            # absent (flag off / stale plan), the legacy horizon-min
            # expression is unchanged.
            _terminal_v_hi = _dp_out.get("terminal_v_hi")
            _terminal_need = _dp_out.get("terminal_need_kwh")
            if (
                _terminal_v_hi is not None
                and _terminal_need is not None
                and controller.cfg.pct_to_kwh(inputs.soc) <= controller.cfg.firmware_floor_kwh + _terminal_need
            ):
                _keep_value = _terminal_v_hi
            else:
                _now_h_keep = resolution.hour_floor(now)
                _remaining_prices_keep = [s.price for s in slots if s.start >= _now_h_keep]
                _keep_value = optimize_mod.compute_water_value(
                    min(_remaining_prices_keep) if _remaining_prices_keep else 0.0,
                    controller.cfg,
                )
            # Economic decision (which hours, how much) = the DP's committed plan.
            # Read the committed export RATE (W) for the current clock-hour; plan
            # membership is the hurdle gate.  No committed rate ⇒ no export (strictly
            # safer than the old ungated surplus-dump).  Real-time adaptation = the
            # live surplus clamp below + inverter net-export (house served first).
            # export_request is keyed on the slot grid (see _dp_select_slots);
            # slot-floor `now` so the lookup names the actual current slot.
            _cur_h = resolution.floor_to_slot(now, _slot_minutes)
            _committed_export = _dp_out.get("export_request") or {}
            _hurdle = _cur_h in _committed_export

            # Decide next export dwell/hysteresis state.
            _new_export_state = scheduler.decide_export_state(
                controller.export_state,
                surplus_kwh=_surplus,
                hurdle_clears=_hurdle,
                now=now,
                cfg=controller.cfg,
            )

            if _new_export_state.engaged:
                # NET target: drain the live surplus-above-reserve decisively
                # over cfg.export_drain_window_h (default 0.0 → one tick → at the
                # export cap, stopping at the live reserve on the final tick).
                # _hurdle gates WHETHER to export (DP plan membership); committed
                # rate no longer throttles HOW FAST.
                _net_target_w = energy.export_net_target_w(
                    _surplus,
                    controller.cfg,
                    eta_curve=controller._planner_curve(),
                )
                # GROSS setpoint must cover house load (firmware serves house
                # first, exports the remainder).  Bounded only by SETPOINT_MAX_W
                # via discharge_cap_w (max_export_w already capped net_target).
                # Only compensate with a FRESH read (A: fix for a safety
                # regression) — a stale cached value (pv/batt sensor blip this
                # tick, soc+meter still live so no failsafe) must not inflate
                # the gross setpoint beyond the reserve-aware target;
                # under-compensating (0.0) is the safe direction here.
                _load_comp_w = (
                    controller.cfg.export_load_comp_factor * _house_load_now_w if controller._house_load_fresh else 0.0
                )
                _gross_w = _net_target_w + _load_comp_w
                _export_sp = guard.command_setpoint(
                    -_gross_w,
                    controller._actuator.last_setpoint_w,
                    controller.cfg,
                    discharge_cap_w=const.SETPOINT_MAX_W,
                )
                # command_setpoint returns positive value for discharge; engage_export
                # validates > 0, so a sign error here fails loudly (safety-net).
                if _export_sp > 0:
                    try:
                        await controller._actuator.engage_export(_export_sp)
                        _export_setpoint_w = _export_sp
                        # R1: MEASURED export, not the commanded setpoint.
                        # _metered_export_w is the battery-sourced portion of
                        # the live grid export — min(meter export, battery
                        # discharge) — read directly from the meter + battery
                        # power sensors this tick.  Mirrors the daily-regret
                        # battery-sourced export rule (F3/actual_export_w
                        # above): PV-spill export is out of scope, only the
                        # energy actually drawn from the battery counts.
                        # Drives PnL + record; independent of the gross
                        # setpoint (which may be inflated by load_comp/
                        # quantization and does not reflect what was
                        # actually metered).
                        _batt_w_now = coordinator.read_float(
                            controller._hass, controller._data[const.CONF_ENT_BATTERY_POWER]
                        )
                        _metered_export_w = min(
                            max(0.0, -inputs.meter_w),
                            max(0.0, _batt_w_now if _batt_w_now is not None else 0.0),
                        )
                        _export_kwh = _metered_export_w / 1000.0 * (const.TICK_SECONDS / 3600.0)
                        _reserve_kwh_val = _reserve
                        _surplus_kwh_val = _surplus
                        # E3: accumulate realized PnL for this export interval.
                        # Price at the effective (post-fee) rate so PnL matches
                        # the DP's objective (gross − export_fee).
                        # PnL uses the DC-stored basis export_pnl_eur expects:
                        # convert AC metered export to DC drawn (AC / eta_discharge)
                        # so revenue = AC * price (the helper's eta_discharge cancels
                        # — no spurious second factor); cost/opportunity scale on DC
                        # energy actually dispatched. _export_kwh (recorded AC) is
                        # now the measured value above, not a setpoint estimate.
                        _eta_d = controller._eta_d_at(_metered_export_w)
                        _export_kwh_dc = _export_kwh / _eta_d if _eta_d > 1e-9 else _export_kwh
                        # Retain for the NEXT tick's drift add-back (duration-scaled).
                        # The drift step re-zeros this field at its start; C3 re-sets
                        # it here only when an export actually fired this tick.
                        # Restart-gap caveat: if the process restarts between C3 and the
                        # next tick's end-of-tick _persist(), this value is lost and the
                        # add-back for that export window is skipped — self-correcting
                        # (one missed add-back → slight over-count in accumulator for
                        # one step). Do NOT add an extra _persist() here; that risks
                        # double-counting if C3 fires multiple times per tick.
                        controller._soc_drift_last_export_kwh_dc = _export_kwh_dc
                        _eff_export_price = optimize_mod.effective_export_price(_export_price, controller.cfg)
                        _tick_pnl = optimize_mod.export_pnl_eur(
                            _export_kwh_dc, _eff_export_price, _keep_value, controller.cfg
                        )
                        controller.today_export_pnl_eur += _tick_pnl
                    except Exception:
                        _LOGGER.error("Actuator engage_export failed (C3 path)", exc_info=True)
                        # Engage failed → do NOT report engaged. Force a clean
                        # disengaged state and best-effort release so the next
                        # tick starts from self-consumption (mirror FORCING L1409).
                        _new_export_state = ExportState(engaged=False, state_since=now)
                        await safe_release(
                            controller,
                            now,
                            "Actuator release_to_self failed (engage_export except)",
                            reset_export=False,
                        )
                else:
                    # Surplus too small to quantize to a valid step — release.
                    _new_export_state = ExportState(engaged=False, state_since=now)
                    if controller.export_state.engaged:
                        await safe_release(
                            controller,
                            now,
                            "Actuator release_to_self failed (C3 zero-rate path)",
                            reset_export=False,
                        )
            else:
                # Gate fail or surplus below lo-eps: release if currently engaged.
                if controller.export_state.engaged:
                    await safe_release(
                        controller,
                        now,
                        "Actuator release_to_self failed (C3 disengage path)",
                        reset_export=False,
                    )

            controller.export_state = _new_export_state
        else:
            # Export disabled or no export price: release if engaged.
            if controller.export_state.engaged:
                await safe_release(
                    controller,
                    now,
                    "Actuator release_to_self failed (export disabled path)",
                    reset_before_release=True,
                )

    return setpoint, _engage_failed, _export_setpoint_w, _export_kwh, _reserve_kwh_val, _surplus_kwh_val
