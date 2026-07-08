"""T0.6a — tests for fictive_plan publication (DP-proposed horizon as second plan).

These tests verify that ``compute_decision`` exposes the DP-selected slots and P50
intervals via the ``_out`` side-channel, and that the data is sufficient to build a
fictive-plan horizon with the exact same per-entry schema as the live plan horizon.

Design rationale for the ``_out`` parameter
--------------------------------------------
``compute_decision`` returns a 6-tuple (plan, setpoint, deadline, horizon,
horizon_mode, intervals_reserve); the 6th element was added in C3 to expose the P50
intervals for the export executor without polluting the ``_out`` side-channel.
Callers that need the DP-proposed slots pass an empty dict as ``_out``;
``compute_decision`` populates it when the DP runs successfully.  Tests here
exercise that side-channel.  The Controller's ``tick()`` method uses it to publish
``last_status["fictive_plan"]``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from custom_components.anker_x1_smartgrid import plan as plan_mod
from custom_components.anker_x1_smartgrid.controller import compute_decision
from custom_components.anker_x1_smartgrid.forecast import LoadPredictor
from custom_components.anker_x1_smartgrid.models import (
    Config,
    PlantInputs,
    PlanState,
    PriceSlot,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

BASE = datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc)  # 10:00 UTC, Mon

_PREDICTOR = LoadPredictor.from_profile({})


def _cfg(**overrides) -> Config:
    return Config.from_dict({
        "capacity_kwh": 10.0,
        "soc_target": 97.0,
        "eta_charge": 0.92,
        "eps_hi_kwh": 0.4,
        "eps_lo_kwh": 0.2,
        "min_dwell_min": 0,
        "max_charge_w": 6000.0,
        "round_trip_eff": 0.85,

        **overrides,
    })


def _slots(prices: list[float], base: datetime = BASE) -> list[PriceSlot]:
    return [PriceSlot(base + timedelta(hours=i), p) for i, p in enumerate(prices)]


def _plan(age_h: float = 2.0) -> PlanState:
    return PlanState.initial(BASE - timedelta(hours=age_h))


def _make_dp_mock_first_hour(charge_kwh: float = 5.0):
    """DP mock that puts all charge in hour 0 (current slot)."""
    def _side_effect(*args, **kwargs):
        wl = kwargs.get("window_len", len(args[0]) if args else 1)
        schedule = [charge_kwh] + [0.0] * (wl - 1)
        return {"schedule": schedule, "kwh": charge_kwh, "eur": charge_kwh * 0.05}
    return _side_effect


def _make_dp_mock_zero():
    """DP mock that never charges — produces an empty selected list."""
    def _side_effect(*args, **kwargs):
        wl = kwargs.get("window_len", len(args[0]) if args else 1)
        return {"schedule": [0.0] * wl, "kwh": 0.0, "eur": 0.0}
    return _side_effect


def _call(cfg: Config, *, soc: float = 20.0, slots=None, _out=None):
    """Call compute_decision with minimal valid inputs."""
    if slots is None:
        slots = _slots([0.05, 0.40, 0.40, 0.40, 0.40, 0.40, 0.40, 0.40, 0.40])
    inputs = PlantInputs(soc=soc, phase_import_w=(0.0, 0.0, 0.0), now=BASE)
    sunset = BASE + timedelta(hours=8)
    kwargs = {}
    if _out is not None:
        kwargs["_out"] = _out
    return compute_decision(
        _plan(), inputs, slots, 0.0, sunset, _PREDICTOR, None, cfg, **kwargs
    )


# ===========================================================================
# 1. _out side-channel is populated when DP runs successfully
# ===========================================================================

def test_out_populated_when_dp_runs():
    """Flag-on + DP succeeds → _out["dp_selected"] and _out["intervals"] present."""
    cfg = _cfg()
    _out: dict = {}
    with patch(
        "custom_components.anker_x1_smartgrid.optimize.optimize_grid",
        side_effect=_make_dp_mock_first_hour(5.0),
    ):
        _call(cfg, soc=20.0, _out=_out)

    assert "dp_selected" in _out, "_out must have dp_selected when DP runs"
    assert "intervals" in _out, "_out must have intervals when DP runs"
    assert isinstance(_out["dp_selected"], list)
    assert len(_out["dp_selected"]) > 0, "DP chose hour 0 → at least one selected slot"


def test_out_dp_selected_contains_hour_0():
    """DP puts charge in hour 0 → BASE (truncated to hour) is in dp_selected."""
    cfg = _cfg()
    _out: dict = {}
    with patch(
        "custom_components.anker_x1_smartgrid.optimize.optimize_grid",
        side_effect=_make_dp_mock_first_hour(5.0),
    ):
        _call(cfg, soc=20.0, _out=_out)

    now_h = BASE.replace(minute=0, second=0, microsecond=0)
    assert now_h in _out["dp_selected"], f"Expected {now_h} in dp_selected, got {_out['dp_selected']}"


def test_out_absent_when_dp_raises():
    """DP raises → fallback to heuristic → _out is NOT populated."""
    cfg = _cfg()
    _out: dict = {}
    with patch(
        "custom_components.anker_x1_smartgrid.optimize.optimize_grid",
        side_effect=ValueError("test-induced DP failure"),
    ):
        _call(cfg, soc=20.0, _out=_out)

    assert "dp_selected" not in _out, "_out must NOT be populated when DP raises"


# ===========================================================================
# 2. Fictive-plan schema parity with live plan
# ===========================================================================

_LIVE_PLAN_KEYS = {"start", "price", "pv_w", "load_w", "solar_charge_w", "grid_charge_w", "mode", "soc", "charge_w", "is_past_horizon", "grid_export_w", "self_discharge_w", "reserve_soc"}


def test_fictive_horizon_schema_matches_live_plan():
    """Fictive horizon built from _out data has the same per-entry keys as the live plan.

    build_plan_horizon is used for both → schema is guaranteed to match,
    but this test pins the contract explicitly.
    """
    cfg = _cfg()
    slots = _slots([0.05, 0.40, 0.40, 0.40, 0.40, 0.40, 0.40, 0.40, 0.40])
    _out: dict = {}
    with patch(
        "custom_components.anker_x1_smartgrid.optimize.optimize_grid",
        side_effect=_make_dp_mock_first_hour(5.0),
    ):
        result = _call(cfg, soc=20.0, slots=slots, _out=_out)

    _, _, deadline, live_horizon, _, _ = result
    dp_selected = _out["dp_selected"]
    intervals = _out["intervals"]

    fictive_horizon = plan_mod.build_plan_horizon(
        slots, intervals, dp_selected, 20.0, deadline, cfg
    )

    assert len(fictive_horizon) > 0, "fictive horizon must be non-empty"
    assert len(live_horizon) > 0, "live horizon must be non-empty"

    fictive_keys = set(fictive_horizon[0].keys())
    live_keys = set(live_horizon[0].keys())

    assert fictive_keys == live_keys, (
        f"fictive horizon entry keys {fictive_keys} != live plan keys {live_keys}"
    )
    # Pin the exact expected schema
    assert fictive_keys == _LIVE_PLAN_KEYS


def test_fictive_horizon_mode_grid_for_dp_hours():
    """DP selects hour 0 → fictive horizon entry at BASE-hour has mode=='grid'."""
    cfg = _cfg()
    slots = _slots([0.05, 0.40, 0.40, 0.40, 0.40, 0.40, 0.40, 0.40, 0.40])
    _out: dict = {}
    with patch(
        "custom_components.anker_x1_smartgrid.optimize.optimize_grid",
        side_effect=_make_dp_mock_first_hour(5.0),
    ):
        result = _call(cfg, soc=20.0, slots=slots, _out=_out)

    _, _, deadline, _, _, _ = result
    fictive_horizon = plan_mod.build_plan_horizon(
        slots, _out["intervals"], _out["dp_selected"], 20.0, deadline, cfg
    )

    base_h = BASE.replace(minute=0, second=0, microsecond=0)
    h0_entry = next(
        (e for e in fictive_horizon if e["start"] == base_h.isoformat()), None
    )
    assert h0_entry is not None, f"No entry for {base_h.isoformat()} in fictive horizon"
    assert h0_entry["mode"] == "grid", (
        f"Expected mode='grid' for DP-selected hour 0, got '{h0_entry['mode']}'"
    )


def test_fictive_horizon_grid_entries_exactly_match_dp_selected():
    """Fictive horizon mode='grid' entries correspond exactly to dp_selected hours.

    Note: apply_safety_floor may promote additional hours beyond the raw DP mock
    output when there is a deficit, so we test the bidirectional invariant rather
    than assuming a specific set of selected hours:
        - Every dp_selected hour has mode='grid' in the fictive horizon.
        - Every mode='grid' entry in the fictive horizon is in dp_selected.
    """
    cfg = _cfg()
    slots = _slots([0.05, 0.40, 0.40, 0.40, 0.40, 0.40, 0.40, 0.40, 0.40])
    _out: dict = {}
    with patch(
        "custom_components.anker_x1_smartgrid.optimize.optimize_grid",
        side_effect=_make_dp_mock_first_hour(5.0),
    ):
        result = _call(cfg, soc=20.0, slots=slots, _out=_out)

    _, _, deadline, _, _, _ = result
    fictive_horizon = plan_mod.build_plan_horizon(
        slots, _out["intervals"], _out["dp_selected"], 20.0, deadline, cfg
    )

    dp_selected_isos = {dt.replace(minute=0, second=0, microsecond=0).isoformat()
                        for dt in _out["dp_selected"]}
    grid_entry_isos = {e["start"] for e in fictive_horizon if e["mode"] == "grid"}

    assert grid_entry_isos == dp_selected_isos, (
        f"Fictive horizon grid entries {grid_entry_isos} must exactly match "
        f"dp_selected hours {dp_selected_isos}"
    )


# ===========================================================================
# 3. fictive_plan absent when DP did not run
# ===========================================================================

def test_dp_zero_schedule_yields_empty_dp_selected():
    """DP runs but the safety floor forces no charge (SoC at target → zero deficit).

    When soc == soc_target, deficit=0 and apply_safety_floor adds nothing.
    The mock returns all-zero schedule → dp_selected is [] → fictive_plan has 0 grid hours.
    The dp_selected key MUST be present (even if []) so tick() knows DP ran.
    """
    cfg = _cfg()  # soc_target=97.0
    _out: dict = {}
    with patch(
        "custom_components.anker_x1_smartgrid.optimize.optimize_grid",
        side_effect=_make_dp_mock_zero(),
    ):
        # soc == soc_target → deficit = 0 → safety floor adds nothing → dp_selected = []
        result = _call(cfg, soc=97.0, _out=_out)

    # DP ran (zero schedule is valid), so dp_selected must be present (even if empty)
    assert "dp_selected" in _out, "dp_selected key must be present even when DP selects no hours"
    assert _out["dp_selected"] == [], f"Expected empty list, got {_out['dp_selected']}"

    # A zero-selection fictive_plan with planned_grid_hours=0 is valid
    _, _, deadline, _, _, _ = result
    fictive_horizon = plan_mod.build_plan_horizon(
        _slots([0.05, 0.40, 0.40, 0.40, 0.40, 0.40, 0.40, 0.40, 0.40]),
        _out["intervals"], _out["dp_selected"], 97.0, deadline, cfg
    )
    grid_hours = sum(1 for e in fictive_horizon if e["mode"] == "grid")
    assert grid_hours == 0, f"Zero DP schedule must yield 0 planned_grid_hours, got {grid_hours}"


# ===========================================================================
# 4. grid_request side-channel and fictive bar magnitude
# ===========================================================================

def test_out_grid_request_present_and_keyed_by_hour():
    # At soc=target the survival floor is a no-op, so grid_request reflects the
    # raw DP schedule (no safety-floor inflation). Mock charges 2 kWh in hour 0.
    cfg = _cfg()
    _out: dict = {}
    with patch(
        "custom_components.anker_x1_smartgrid.optimize.optimize_grid",
        side_effect=_make_dp_mock_first_hour(2.0),
    ):
        _call(cfg, soc=97.0, _out=_out)
    assert "grid_request" in _out
    now_h = BASE.replace(minute=0, second=0, microsecond=0)
    assert now_h in _out["grid_request"]
    assert _out["grid_request"][now_h] == 2000.0  # 2.0 kWh * 1000, no floor inflation


def test_fictive_grid_bar_uses_dp_schedule_not_max_charge():
    # End-to-end: controller builds grid_request from the DP schedule (no floor
    # inflation at soc=target); rendering the fictive horizon at a lower display
    # SoC (ample headroom) yields a grid bar == schedule (2000 W), NOT max_charge_w.
    cfg = _cfg()
    slots = _slots([0.05, 0.40, 0.40, 0.40, 0.40, 0.40, 0.40, 0.40, 0.40])
    _out: dict = {}
    with patch(
        "custom_components.anker_x1_smartgrid.optimize.optimize_grid",
        side_effect=_make_dp_mock_first_hour(2.0),
    ):
        result = _call(cfg, soc=97.0, slots=slots, _out=_out)
    _, _, deadline, _, _, _ = result
    fictive = plan_mod.build_plan_horizon(
        slots, _out["intervals"], _out["dp_selected"], 20.0, deadline, cfg,
        grid_request_by_hour=_out["grid_request"],
    )
    base_h = BASE.replace(minute=0, second=0, microsecond=0)
    e = next(e for e in fictive if e["start"] == base_h.isoformat())
    assert e["mode"] == "grid"
    assert e["grid_charge_w"] == 2000.0
