"""Pure breaker-guard logic: deadband, quantization.

Sign convention (input to command_setpoint / quantize_setpoint):
  positive value  → charge request; magnitude = watts to charge.
  negative value  → discharge/export request; magnitude = watts to export.

Sign convention (output / inverter setpoint):
  negative value  → charge   (≤ 0, ≥ SETPOINT_MIN_W = -6000 W)
  positive value  → discharge (≥ 0, ≤ SETPOINT_MAX_W = +6000 W)
  zero            → idle
"""
from __future__ import annotations

from . import const
from .models import Config


def quantize_setpoint(desired_w: float, cfg: Config) -> float:
    """Quantize *desired_w* to the 100 W SETPOINT_STEP_W grid; return signed inverter setpoint.

    Positive *desired_w*  → charge request  → negative output (charge direction).
    Negative *desired_w*  → discharge request → positive output (export direction).
    Zero → 0.0.

    The magnitude is floored to the nearest SETPOINT_STEP_W step (never rounds up),
    and the result is clamped to [SETPOINT_MIN_W, SETPOINT_MAX_W].
    """
    mag = abs(desired_w)
    stepped = (mag // const.SETPOINT_STEP_W) * const.SETPOINT_STEP_W
    if desired_w >= 0.0:
        # charge direction: output is negative
        signed = -stepped
        return max(const.SETPOINT_MIN_W, min(0.0, signed))
    else:
        # discharge direction: output is positive
        signed = stepped
        return min(const.SETPOINT_MAX_W, max(0.0, signed))


def command_setpoint(
    desired_w: float,
    prev_setpoint_w: float,
    cfg: Config,
    *,
    discharge_cap_w: float | None = None,
) -> float:
    """Apply hardware cap, deadband against prev, and quantize; return signed inverter setpoint.

    Positive *desired_w*  → charge.  Capped at cfg.max_charge_w.  Returns ≤ 0.
    Negative *desired_w*  → discharge/export.  Capped at min(cap, SETPOINT_MAX_W)
                            where cap = cfg.max_export_w when discharge_cap_w is None
                            (back-compat), or discharge_cap_w when provided.  Use
                            discharge_cap_w=const.SETPOINT_MAX_W when the caller has
                            already applied net feed caps and wants only the hardware
                            ceiling enforced on the gross setpoint.  Returns ≥ 0.
    Zero → 0.0.

    Deadband is applied on **magnitude within the same sign direction**.  A
    sign flip (charge→discharge or vice-versa) always bypasses the deadband
    because the deadband gate checks the sign of *prev_setpoint_w*: a
    same-direction *prev* is required to enter the deadband branch, so an
    opposite-sign *prev* is excluded entirely regardless of deadband magnitude.

    The deadband may keep the previous setpoint, but the magnitude of the
    returned value must never exceed the applicable hardware cap.
    """
    if desired_w >= 0.0:
        # ----- charge path -----
        capped_mag = min(desired_w, cfg.max_charge_w, abs(const.SETPOINT_MIN_W))
        target = quantize_setpoint(capped_mag, cfg)  # ≤ 0
        if prev_setpoint_w <= 0.0 and abs(target - prev_setpoint_w) < cfg.deadband_w:
            result = prev_setpoint_w
        else:
            result = target
        # Clamp: result must not be more negative than target (|result| ≤ |target|).
        return max(result, target)
    else:
        # ----- discharge/export path -----
        # discharge_cap_w lets the caller bound the GROSS setpoint by the hardware
        # ceiling (SETPOINT_MAX_W) instead of the net cap max_export_w — used by
        # the export executor, which has already applied max_export_w to net_target.
        cap = cfg.max_export_w if discharge_cap_w is None else discharge_cap_w
        capped_mag = min(abs(desired_w), cap, const.SETPOINT_MAX_W)
        target = quantize_setpoint(-capped_mag, cfg)  # ≥ 0
        if prev_setpoint_w >= 0.0 and abs(target - prev_setpoint_w) < cfg.deadband_w:
            result = prev_setpoint_w
        else:
            result = target
        # Clamp: result must not exceed target (|result| ≤ |target|).
        return min(result, target)
