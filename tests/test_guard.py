from custom_components.anker_x1_smartgrid.models import Config
from custom_components.anker_x1_smartgrid import guard
from custom_components.anker_x1_smartgrid import const


# ---------------------------------------------------------------------------
# quantize_setpoint — charge (negative) tests (must remain passing)
# ---------------------------------------------------------------------------


def test_quantize_rounds_and_signs():
    cfg = Config()
    assert guard.quantize_setpoint(2350.0, cfg) == -2300.0  # floor to 100 grid magnitude
    assert guard.quantize_setpoint(50.0, cfg) == 0.0


def test_quantize_clamps_to_min():
    cfg = Config()
    assert guard.quantize_setpoint(99999.0, cfg) == -6000.0


# ---------------------------------------------------------------------------
# quantize_setpoint — discharge (positive) tests
# ---------------------------------------------------------------------------


def test_quantize_positive_rounds():
    """Positive desired → positive quantized (discharge direction)."""
    cfg = Config()
    assert guard.quantize_setpoint(-2350.0, cfg) == 2300.0


def test_quantize_positive_small_rounds_to_zero():
    """Small positive desired → 0 (below one STEP)."""
    cfg = Config()
    assert guard.quantize_setpoint(-50.0, cfg) == 0.0


def test_quantize_positive_clamps_to_max():
    """Desired discharge > SETPOINT_MAX_W is clamped to SETPOINT_MAX_W."""
    cfg = Config()
    assert guard.quantize_setpoint(-99999.0, cfg) == 6000.0


# ---------------------------------------------------------------------------
# command_setpoint — charge (negative) tests (must remain passing)
# ---------------------------------------------------------------------------


def test_command_capped_by_max_charge_w_only():
    """Setpoint is capped only by max_charge_w, not per-phase import."""
    cfg = Config(max_charge_w=6000.0, deadband_w=300.0)
    # Previously, high phase import would reduce headroom and cap setpoint lower.
    # Now, desired=6000 with max_charge_w=6000 → full 6000 allowed.
    out = guard.command_setpoint(6000.0, prev_setpoint_w=0.0, cfg=cfg)
    assert out == -6000.0


def test_command_desired_below_max_charge_w():
    """When desired < max_charge_w, the desired value is used."""
    cfg = Config(max_charge_w=6000.0, deadband_w=300.0)
    out = guard.command_setpoint(4000.0, prev_setpoint_w=0.0, cfg=cfg)
    assert out == -4000.0


def test_command_deadband_keeps_prev():
    cfg = Config(deadband_w=300.0)
    # prev -3000, new desired 3100 (within 300) -> keep prev
    out = guard.command_setpoint(3100.0, prev_setpoint_w=-3000.0, cfg=cfg)
    assert out == -3000.0


def test_command_deadband_allows_large_change():
    cfg = Config(deadband_w=300.0)
    out = guard.command_setpoint(5000.0, prev_setpoint_w=-3000.0, cfg=cfg)
    assert out == -5000.0


def test_command_deadband_clamp_above_max():
    """Deadband must not let prev stay above max_charge_w cap."""
    cfg = Config(max_charge_w=6000.0, deadband_w=300.0)
    # prev=-6200 is below max_charge_w (would be -6000 cap), target=-6000
    # |(-6000)-(-6200)|=200 < deadband 300, deadband would keep -6200 but that exceeds cap
    # clamp must bring it to -6000
    out = guard.command_setpoint(9999.0, prev_setpoint_w=-6200.0, cfg=cfg)
    assert out == -6000.0


# ---------------------------------------------------------------------------
# command_setpoint — discharge (positive) tests
# ---------------------------------------------------------------------------


def test_command_discharge_basic():
    """Positive desired discharge setpoint is quantized and returned positive."""
    cfg = Config(max_export_w=6000.0, deadband_w=300.0)
    out = guard.command_setpoint(-4000.0, prev_setpoint_w=0.0, cfg=cfg)
    assert out == 4000.0


def test_command_discharge_capped_by_max_export_w():
    """Discharge is capped at max_export_w."""
    cfg = Config(max_export_w=3000.0, deadband_w=300.0)
    out = guard.command_setpoint(-9999.0, prev_setpoint_w=0.0, cfg=cfg)
    assert out == 3000.0


def test_command_discharge_capped_by_setpoint_max_w():
    """Discharge is capped at SETPOINT_MAX_W even when max_export_w is higher."""
    cfg = Config(max_export_w=9000.0, deadband_w=300.0)
    out = guard.command_setpoint(-9999.0, prev_setpoint_w=0.0, cfg=cfg)
    assert out == 6000.0


def test_command_discharge_deadband_holds_prev():
    """Deadband holds previous positive setpoint when change is within band."""
    cfg = Config(deadband_w=300.0)
    # prev=3000, new desired=-3100 (magnitude within 300) → keep prev
    out = guard.command_setpoint(-3100.0, prev_setpoint_w=3000.0, cfg=cfg)
    assert out == 3000.0


def test_command_discharge_deadband_allows_large_change():
    """Deadband allows change when outside band."""
    cfg = Config(deadband_w=300.0)
    out = guard.command_setpoint(-5000.0, prev_setpoint_w=3000.0, cfg=cfg)
    assert out == 5000.0


def test_command_mixed_sign_zero_crossing():
    """Switching from charge to discharge crosses zero cleanly — no held wrong-sign value.

    prev=-3000 (charging), new desired=-2000 (discharge). Deadband operates on
    magnitudes within the *same sign direction*; a sign flip is always a large
    change (crosses zero) so it must never be suppressed by the deadband.
    Result: +2000 (discharge), not -3000 (retained wrong sign).
    """
    cfg = Config(deadband_w=300.0)
    out = guard.command_setpoint(-2000.0, prev_setpoint_w=-3000.0, cfg=cfg)
    assert out == 2000.0


def test_command_mixed_sign_discharge_to_charge():
    """Switching from discharge to charge crosses zero cleanly."""
    cfg = Config(deadband_w=300.0)
    # prev=3000 (discharging), new desired=2000 (charge) — sign flip, no deadband suppression
    out = guard.command_setpoint(2000.0, prev_setpoint_w=3000.0, cfg=cfg)
    assert out == -2000.0


def test_command_discharge_cap_override_allows_above_max_export_w():
    """discharge_cap_w overrides the max_export_w clamp on the gross export path."""
    cfg = Config(max_export_w=3000.0, deadband_w=300.0)
    out = guard.command_setpoint(-5000.0, prev_setpoint_w=0.0, cfg=cfg, discharge_cap_w=const.SETPOINT_MAX_W)
    assert out == 5000.0  # NOT clamped to max_export_w=3000


def test_command_discharge_cap_override_still_clamps_setpoint_max():
    cfg = Config(max_export_w=3000.0, deadband_w=300.0)
    out = guard.command_setpoint(-9999.0, prev_setpoint_w=0.0, cfg=cfg, discharge_cap_w=const.SETPOINT_MAX_W)
    assert out == 6000.0  # SETPOINT_MAX_W ceiling


def test_command_discharge_cap_default_uses_max_export_w():
    """Default discharge_cap_w=None preserves the max_export_w clamp (back-compat)."""
    cfg = Config(max_export_w=3000.0, deadband_w=300.0)
    out = guard.command_setpoint(-5000.0, prev_setpoint_w=0.0, cfg=cfg)
    assert out == 3000.0
