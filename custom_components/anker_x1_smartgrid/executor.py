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
import time
from datetime import datetime, timedelta
from statistics import fmean, median
from typing import TYPE_CHECKING, NamedTuple

from homeassistant.util import dt as dt_util

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
) -> bool:
    """Best-effort inverter release + export dwell-state reset; ``False`` if the release raised.

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
    released = True
    if release:
        try:
            await controller._actuator.release_to_self()
        except Exception:
            _LOGGER.error(context, exc_info=True)
            released = False
        else:
            if controller.hold_state.engaged:
                controller.hold_state = ExportState(engaged=False, state_since=now)
    if reset_export and not reset_before_release and controller.export_state.engaged:
        controller.export_state = ExportState(engaged=False, state_since=now)
    return released


def floor_rescue_needed(controller: Controller, soc: float | None) -> bool:
    """Is the pack BELOW the firmware discharge floor and in need of rescue?

    The planner is economic-only: it models ``const.FIRMWARE_SOC_FLOOR`` as
    physics (the wall the pack sags to) and never force-charges to hold it.
    This is the single exception — below that wall the pack is protected, not
    optimized, so the answer here overrides the DP, the price gate and the
    master switch alike.

    The threshold is deliberately hardcoded to ``const.FIRMWARE_SOC_FLOOR``
    with no config option and no recovery margin: charging stops the moment
    SoC reads back AT the floor. ``cfg.soc_floor`` is a soft decision margin
    and is intentionally NOT consulted.

    Owns the edge-triggered WARNING latch (``controller._floor_rescue_active``)
    so one episode logs once and recovery re-arms it — both tick paths call
    this exactly once per tick, never both.

    ``soc`` is ``None`` when plant inputs are unreadable: fail closed (no
    rescue), matching every other actuation path's treatment of missing data.
    """
    if soc is None or soc >= const.FIRMWARE_SOC_FLOOR:
        controller._floor_rescue_active = False
        return False
    if not controller._floor_rescue_active:
        controller._floor_rescue_active = True
        _LOGGER.warning(
            "Battery below the firmware floor (soc=%.1f%% < %.1f%%): overriding the "
            "economic plan and grid-charging back to the floor",
            soc,
            const.FIRMWARE_SOC_FLOOR,
        )
    return True


def grid_charge_request_w(controller: Controller, inputs: PlantInputs) -> float:
    """Grid-charge request (W) under grid_import_limit_w, the house's draw read off the live meter.

    Net house draw = meter (+ import) + battery (+ discharge, − charge) − inverter loss,
    i.e. ``_compute_house_load_w`` minus PV, so no PV reading is needed. Without a
    battery reading the setpoint stands in, but only while the pack is engaged to
    charge or hold; a free or exporting pack may be feeding the house, so request 0 W.
    """
    batt_w, loss_w = coordinator.read_battery_and_loss_w(controller._hass, controller._data, inputs)
    if batt_w is None:
        if not controller._actuator.engaged or controller._actuator.last_setpoint_w > 0.0:
            return 0.0
        batt_w = controller._actuator.last_setpoint_w
    commanded_w = controller._actuator.last_setpoint_w
    if controller._actuator.engaged and commanded_w < 0.0 and commanded_w > batt_w:
        # Measured power of an overdrawing pack settles above the limit; a tapering pack's smaller reading wins.
        batt_w, loss_w = commanded_w, 0.0
    net_house_draw_w = max(0.0, inputs.meter_w + batt_w - (loss_w or 0.0))
    return energy.grid_charge_request_w(controller.cfg, net_house_draw_w)


class MeterTrim(NamedTuple):
    """What the last tick aimed for, which the meter fast path steers toward until the next tick."""

    slot: datetime
    slot_minutes: int
    # Signed: the tick's charge at zero house draw (< 0), or the most export gross it may command (> 0).
    cap_w: float
    # Export only: the meter export the tick's gross aims for, PV aside, and the PV it saw.
    aim_w: float
    pv_w: float
    # Valid only while no one else has written since (act.last_write_at).
    written_at: float
    # The last write raised (or was the tick's): the first settled window checks the pack followed it.
    verify_raise: bool = True
    # When the fast path last raised (act.last_write_at); -inf if it hasn't since the tick.
    raised_at: float = float("-inf")


def record_meter_trim(
    controller: Controller, now: datetime, slot_minutes: int, cap_w: float, aim_w: float = 0.0, pv_w: float = 0.0
) -> None:
    controller._meter_trim = MeterTrim(
        resolution.floor_to_slot(now, slot_minutes),
        slot_minutes,
        cap_w,
        aim_w,
        pv_w,
        controller._actuator.last_write_at,
    )
    controller._meter_errors.clear()


def battery_stand_in_w(controller: Controller) -> float | None:
    """The setpoint that just landed, while battery_power still shows the one before it."""
    act = controller._actuator
    if act.engaged and act.last_write_ok and time.monotonic() - act.last_write_at < const.BATTERY_POWER_LAG_S:
        return act.last_setpoint_w
    return None


async def trim_to_meter(controller: Controller) -> None:
    """Steer a charge or export we command toward what the last tick wanted, on every meter update.

    A single reading over the grid connection limit cuts at once, on the setpoint
    step and never past 0 W, at most once per ``const.METER_CUT_MIN_GAP_S`` unless
    the last write was a fast raise. A cut after a fast raise holds until the next
    tick. Other moves (a charge raised back toward the tick's cap, an export trimmed
    so the meter holds the tick's aim) act on the median of the latest three
    readings, all ``const.METER_SETTLE_S`` or more after any write and none older
    than ``const.METER_WINDOW_S``, beyond the guard deadband, and only while the
    tick that set the aim is current. Meter only: the battery power register lags
    writes by seconds. Never flips sign, engages or releases. Skipped while a tick
    runs.
    """
    if controller._tick_lock.locked():
        return
    # Read under the lock: a disable or unload queued on it first must not get its setpoint overwritten.
    async with controller._tick_lock:
        act = controller._actuator
        sp = act.last_setpoint_w
        errors = controller._meter_errors
        meter_w = coordinator.read_float(
            controller._hass,
            controller._data.get(const.CONF_ENT_METER_POWER, const.DEFAULT_ENTITIES[const.CONF_ENT_METER_POWER]),
        )
        trim = controller._meter_trim
        if (
            trim is None
            or not controller.enabled
            or trim.written_at != act.last_write_at
            or resolution.floor_to_slot(dt_util.utcnow(), trim.slot_minutes) != trim.slot
            or (sp != 0.0 and (sp < 0.0) != (trim.cap_w < 0.0))
        ):
            trim = None
        if meter_w is None or not act.engaged or (sp == 0.0 and trim is None):
            errors.clear()
            return
        charging = sp < 0.0 if sp != 0.0 else trim is not None and trim.cap_w < 0.0
        limit_w = controller.cfg.grid_import_limit_w
        now_s = time.monotonic()
        since_write_s = now_s - act.last_write_at
        # Meter + = import: our charge pushes it up, our export pushes it down.
        excess_w = (meter_w if charging else -meter_w) - limit_w
        if sp != 0.0 and excess_w >= const.SETPOINT_STEP_W:
            # Right after our own raise a reading can only understate the excess: no double cut to wait out.
            if since_write_s >= const.METER_CUT_MIN_GAP_S or (trim is not None and trim.raised_at == trim.written_at):
                mag = max(0.0, abs(sp) - excess_w) // const.SETPOINT_STEP_W * const.SETPOINT_STEP_W
                if trim is not None and trim.raised_at > float("-inf"):
                    # A toggling load would cycle cut and raise: once a raise is cut, the cut holds until the next tick.
                    trim = trim._replace(cap_w=-mag if charging else mag) if mag else None
                await _write_trim(controller, trim, mag, charging)
            return
        if trim is None:
            errors.clear()
            return
        if since_write_s < const.METER_SETTLE_S:
            return
        if charging:
            errors.append((now_s, -excess_w))
        else:
            pv_w = coordinator.read_pv_power_w(controller._hass, controller._data)
            if pv_w is None:
                return
            # Lost PV spill lowers the aim so the pack never covers it; PV above what the tick saw never raises it.
            errors.append((now_s, min(limit_w, trim.aim_w + min(pv_w, trim.pv_w)) + meter_w))
        while now_s - errors[0][0] > const.METER_WINDOW_S:
            errors.popleft()
        if len(errors) != errors.maxlen:
            return
        err_w, newest_w = median(e for _, e in errors), errors[-1][1]
        # The newest reading may shrink or veto a move, never grow it: a load step it shows isn't outvoted.
        err_w = min(err_w, newest_w, key=abs) if err_w * newest_w > 0.0 else 0.0
        if trim.verify_raise:
            # Room left once a raise settles means the pack isn't taking it (taper, derate): don't wind it up
            # to the cap for the tick to pull back; raising waits for the next tick.
            trim = controller._meter_trim = trim._replace(
                verify_raise=False, cap_w=sp if err_w >= controller.cfg.deadband_w else trim.cap_w
            )
        if err_w >= controller.cfg.deadband_w:
            mag = min(abs(trim.cap_w), abs(sp) + err_w)
        elif err_w <= -controller.cfg.deadband_w and not charging:
            mag = max(0.0, sp + err_w)
        else:
            return
        mag = mag // const.SETPOINT_STEP_W * const.SETPOINT_STEP_W
        mag = abs(act._clamp_to_live_limits(-mag if charging else mag, quiet=True))
        if (mag > abs(sp)) if err_w > 0.0 else (mag < sp):
            await _write_trim(controller, trim, mag, charging)


async def _write_trim(controller: Controller, trim: MeterTrim | None, mag: float, charging: bool) -> None:
    act = controller._actuator
    raised = mag > abs(act.last_setpoint_w)
    try:
        if mag == 0.0:
            # Stay engaged at 0 W, as the tick does at zero headroom; releasing an export would only flap.
            await act.engage_hold()
        elif charging:
            await act.engage_and_charge(-mag)
        else:
            await act.engage_export(mag)
    except Exception:
        _LOGGER.error("Meter fast path: setpoint write failed; leaving it to the next tick", exc_info=True)
        return
    controller._meter_errors.clear()
    if trim is not None:
        controller._meter_trim = trim._replace(
            written_at=act.last_write_at, verify_raise=raised, raised_at=act.last_write_at if raised else trim.raised_at
        )
    controller.last_status["setpoint_w" if charging else "export_setpoint_w"] = act.last_setpoint_w


async def run_floor_rescue(controller: Controller, now: datetime, inputs: PlantInputs) -> float | None:
    """Engage the grid-charge rescue directly; return the setpoint written.

    Used by the DISABLED and failsafe (missing price/PV/sunset) tick paths,
    which never reach :func:`run_forcing_and_export` — the normal enabled path
    expresses the same rescue by overriding its plan to FORCING, reusing that
    function's actuation (and its mutual exclusion with the export executor).

    Returns ``None`` when the engage failed, having released the inverter
    back to firmware control so it is not left half-engaged — same
    best-effort recovery as the FORCING path. Any successful engage returns
    a float, 0.0 included (the house alone fills the connection).
    """
    setpoint = guard.command_setpoint(
        grid_charge_request_w(controller, inputs),
        controller._actuator.last_setpoint_w,
        controller.cfg,
    )
    try:
        await controller._actuator.engage_and_charge(setpoint)
    except Exception:
        _LOGGER.error("Actuator engage_and_charge failed (floor-rescue path)", exc_info=True)
        await safe_release(
            controller,
            now,
            "Actuator release_to_self failed (floor-rescue engage-failure recovery)",
            reset_export=False,
        )
        return None
    return controller._actuator.last_setpoint_w


def recent_deficits_w(controller: Controller, now: datetime) -> list[float]:
    """House-load-minus-PV samples inside the hold's rolling window; empty when the load is stale."""
    window = timedelta(minutes=const.LOW_POWER_HOLD_LOAD_WINDOW_MIN)
    return [w for t, w in controller._hold_deficit_w if now - t < window]


def hold_values(
    controller: Controller,
    now: datetime,
    new_plan: PlanState,
    slots: list[PriceSlot],
    _dp_out: dict,
    _slot_minutes: int,
    soc: float | None = None,
) -> tuple[float | None, float | None]:
    """``(discharge_value, stored_value)`` in €/DC-kWh for the low-power hold.

    ``discharge_value`` is ``None`` whenever a hold is not allowed: feature
    off, a charge (FORCING covers calibration and the floor rescue) or an
    export planned or still running, PV covering the house on the rolling
    mean, SoC above this slot's solar-reservation ceiling, no hold trajectory
    published by the DP at all, or any input missing. Export's previous-tick state blocks too, so an export winding
    down releases first instead of re-engaging in the same tick. Stale house
    load only blocks once the rolling window has no fresh sample left, so a
    sensor blip doesn't release the hold.

    The ceiling is the headroom test: above it the forecast solar of this cycle
    already refills the pack, so a kWh kept there is one tomorrow's sun will
    spill — worth ~0, whatever the water value says. Same signal the DP's own
    held branch and the charge-stop use, so the three cannot disagree.
    """
    cfg = controller.cfg
    stored = _dp_out.get("water_value")
    if stored is not None:
        stored = optimize_mod.hold_stored_value(stored, _dp_out.get("terminal_segments"))
    price = resolution.price_at(slots, now, _slot_minutes)
    deficits = recent_deficits_w(controller, now)
    # The DP's own slot key when it published one: re-flooring `now` here can
    # land on the next slot and miss the current one entirely.
    _slot = _dp_out.get("dp_slot_start") or resolution.floor_to_slot(now, _slot_minutes)
    _ceiling = (_dp_out.get("ceiling_by_hour") or {}).get(_slot)
    if (
        not cfg.low_power_hold
        or new_plan.state is ControllerState.FORCING
        or controller.export_state.engaged
        or _slot in (_dp_out.get("export_request") or {})
        or not deficits
        or fmean(deficits) <= 0.0
        or price is None
        or stored is None
        or _dp_out.get("held_slots") is None
        # `>` not `>=`, to match the DP's own held branch at the boundary.
        or (_ceiling is not None and soc is not None and soc > _ceiling)
    ):
        return None, stored
    # Concave in the deficit, and this is the 5-min mean while the DP used the
    # slot mean, so the two can straddle the boundary inside one slot; the
    # engage/release dwell is what keeps that from flapping.
    return optimize_mod.discharge_value(price, fmean(deficits), cfg, controller._planner_curve()), stored


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
    _prev_hold = controller.hold_state
    _discharge_v, _stored_v = hold_values(controller, now, new_plan, slots, _dp_out, _slot_minutes, inputs.soc)
    # The DP weighed this slot against the whole horizon; the live rule may only
    # refine what it chose, never hold a slot it turned down.
    _dp_held = _dp_out.get("held_slots")
    _dp_slot = _dp_out.get("dp_slot_start") or resolution.floor_to_slot(now, _slot_minutes)
    # The DP's value for this slot: a later slot can be worth more than the horizon end.
    _stored_v = (_dp_out.get("kept_by_slot") or {}).get(_dp_slot, _stored_v)
    controller.hold_state = scheduler.decide_hold_state(
        controller.hold_state,
        discharge_value=_discharge_v,
        stored_value=_stored_v,
        now=now,
        cfg=controller.cfg,
        slot_minutes=_slot_minutes,
        # A charge the plan does not force (charge stop, dwell) keeps the pack rather than drains it.
        dp_holds=_dp_held is None or _dp_slot in _dp_held or _dp_slot in (_dp_out.get("grid_request") or {}),
        paused=new_plan.state is ControllerState.FORCING,
    )
    # Never while charging; the tick a forced charge ends under reserve + 1 hands straight to the hold.
    if (
        controller.cfg.backup_reserve_soc > 0.0
        and inputs.soc is not None
        and new_plan.state is not ControllerState.FORCING
        and not controller.export_state.engaged
        and (controller._reserve_hold_retry_at is None or now >= controller._reserve_hold_retry_at)
    ):
        _recent = recent_deficits_w(controller, now)
        _surplus = bool(_recent) and fmean(_recent) < 0.0
        _after_charge = controller.plan.state is ControllerState.FORCING
        _band = scheduler.backup_reserve_band(controller.cfg)[1]
        _reserve_hold = scheduler.backup_reserve_hold(
            ExportState(engaged=True, state_since=now)
            if _after_charge and not _surplus and inputs.soc < _band
            else _prev_hold,
            soc=inputs.soc,
            pv_surplus=_surplus,
            now=now,
            cfg=controller.cfg,
            slot_minutes=_slot_minutes,
        )
        if _reserve_hold is not None and _reserve_hold.engaged:
            controller.hold_state = _reserve_hold
    _hold = controller.hold_state.engaged
    _hold_state = controller.hold_state
    controller.last_hold_values = (_discharge_v, _stored_v)
    if new_plan.state is ControllerState.FORCING:
        setpoint = guard.command_setpoint(
            grid_charge_request_w(controller, inputs),
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
            if setpoint < 0.0:
                _cap_w = guard.quantize_setpoint(energy.grid_charge_request_w(controller.cfg, 0.0), controller.cfg)
                record_meter_trim(controller, now, _slot_minutes, _cap_w)
        # Mutual exclusion: export executor is skipped entirely while force-charging.
        # Release export state so we transition cleanly after force-charge ends.
        await safe_release(controller, now, release=False)
    else:
        setpoint = 0.0
        # A hold re-engages at 0 W below; releasing first would just flap the workmode.
        _released_for_forcing = False
        if controller.plan.state is ControllerState.FORCING and not _hold:
            _released_for_forcing = True
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
            # and the oracle (regret_job.py) — NOT the earliest
            # qualifying local minimum, which can be far shallower than a
            # deeper refill sitting later in the same horizon (that mismatch
            # would price this ledger's opportunity cost differently than the
            # plan it's supposed to be scoring against).
            #
            # Piecewise terminal credit: when the DP stashed terminal_segments
            # (chronological (dc_kwh, value_eur_per_dc_kwh) pairs) this ledger
            # walks them sorted by value descending, cumulating from the
            # firmware floor — the marginal kWh above the floor is priced at
            # whichever segment it falls into. Beyond the last segment, or
            # when the key is absent/falsy (flag off / stale plan), the
            # legacy horizon-min expression is unchanged.
            _segs = _dp_out.get("terminal_segments")
            _keep_value = None
            if _segs:
                _above_fw = max(0.0, controller.cfg.pct_to_kwh(inputs.soc) - controller.cfg.firmware_floor_kwh)
                for _kwh, _v in sorted(_segs, key=lambda s: -s[1]):
                    if _above_fw <= _kwh:
                        _keep_value = _v
                        break
                    _above_fw -= _kwh
            if _keep_value is None:  # beyond all segments, or keys absent/flag off → legacy, unchanged
                _now_h_keep = resolution.hour_floor(now)
                _remaining_prices_keep = [s.price for s in slots if s.start >= _now_h_keep]
                _keep_value = optimize_mod.compute_water_value(
                    min(_remaining_prices_keep) if _remaining_prices_keep else 0.0,
                    controller.cfg,
                    controller._planner_curve(),
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
                slot_minutes=_slot_minutes,
            )

            if _new_export_state.engaged:
                # NET target: drain the live surplus-above-reserve over
                # cfg.export_drain_window_h (default 0.0 → one tick → at the
                # export cap, stopping at the live reserve on the final tick),
                # then bound that by the rate the DP committed for THIS slot.
                #
                # The committed rate is both the gate and the ceiling. Reading
                # it as a gate alone let the surplus target collapse to the
                # export cap, so a plan that asked to shed a few hundred watts
                # emptied the pack to the ride-out reserve at full rate — and
                # the next DP solve bought it all back, a charge/export limit
                # cycle at the inverter's full rating. The surplus/reserve
                # clamp still wins whenever it is the tighter of the two, so
                # the live SoC (not the forecast) remains the safety bound.
                #
                # An uncommitted slot yields 0 → the zero-rate branch below
                # releases: an export the plan has abandoned must not coast on
                # the dwell window. `export_request` values are net AC watts
                # averaged over the slot (decision.py builds them as
                # kWh/dt_h × 1000), the same basis as export_net_target_w.
                _net_target_w = energy.export_net_target_w(
                    _surplus,
                    controller.cfg,
                    eta_curve=controller._planner_curve(),
                )
                _net_target_w = min(_net_target_w, _committed_export.get(_cur_h, 0.0))
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
                # A zero net target must stay zero: the load-comp term serves the
                # house out of the pack, so adding it to a zeroed target would
                # discharge on a slot the plan never committed.
                _gross_w = _net_target_w + _load_comp_w if _net_target_w > 0.0 else 0.0
                _last_w = controller._actuator.last_setpoint_w
                if _last_w > 0.0:
                    # PV spill shares the connection: stay inside the room the meter shows, or this undoes a meter cut.
                    _gross_w = max(0.0, min(_gross_w, _last_w + controller.cfg.grid_import_limit_w + inputs.meter_w))
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
                        if controller._house_load_fresh and controller.cfg.export_load_comp_factor == 1.0:
                            # The gross covers the house, so the meter shows net + PV − loss; PV is tracked live.
                            # Partial compensation would aim the meter at a share of house load: left to the tick.
                            _loss_w = coordinator.read_float(
                                controller._hass,
                                controller._data.get(
                                    const.CONF_ENT_INVERTER_LOSS, const.DEFAULT_ENTITIES[const.CONF_ENT_INVERTER_LOSS]
                                ),
                            )
                            record_meter_trim(
                                controller,
                                now,
                                _slot_minutes,
                                const.SETPOINT_MAX_W,
                                aim_w=_net_target_w - (_loss_w or 0.0),
                                pv_w=controller._last_pv_w,
                            )
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

        # A hold never overwrites an export that is actually written this tick; a planned export that
        # does not fire leaves the hold standing.
        if _hold and _export_setpoint_w is not None:
            controller.hold_state = ExportState(engaged=False, state_since=now)
            _hold = False
        if _hold:
            try:
                await controller._actuator.engage_hold()
                controller.hold_state = _hold_state
            except Exception:
                _LOGGER.error("Actuator engage_hold failed (low-power hold)", exc_info=True)
                controller._reserve_hold_retry_at = now + timedelta(minutes=controller.cfg.export_dwell_min)
                await safe_release(
                    controller,
                    now,
                    "Actuator release_to_self failed (hold engage-failure recovery)",
                    reset_export=False,
                )
        elif _prev_hold.engaged and _export_setpoint_w is None and not _released_for_forcing:
            if not await safe_release(
                controller, now, "Actuator release_to_self failed (hold exit)", reset_export=False
            ):
                # Still engaged at 0 W: stay in the hold state so the next tick retries the release.
                controller.hold_state = _prev_hold

    return setpoint, _engage_failed, _export_setpoint_w, _export_kwh, _reserve_kwh_val, _surplus_kwh_val
