"""Integration/acceptance test — Task A3.

Proves the combined A1 + A2 fix end-to-end through the real ``compute_decision``
path (no production-code mocking):

  **Test 1** — ``test_morning_no_survival_charge_passive``

    At 05:00 with soc=22%, the controller must stay PASSIVE.
    ``_out["grid_request"]`` must have ZERO grid charge across the pre-trough
    morning hours (05:00–10:00), and must still show a planned charge at the
    11:00 trough.

    Regression trigger (pre-A1): the old floor-survival buy in the DP forced
    ~54 W at 07:00 (decisive.py ``B full``), making 07:00 a selected slot and
    the controller enter FORCING inappropriately.

  **Test 2** — ``test_trough_economic_topoff_actuates``

    At 11:00 (trough) with soc=5% (firmware floor, deficit≈0 from here), the
    controller must enter FORCING and issue the standard charge setpoint.

    Regression trigger (pre-A2): FORCING required ``deficit > eps_hi_kwh``
    (0.4 kWh); at 11:00 the bridge deficit is nearly zero, so the old path
    stayed PASSIVE and the cheap trough was wasted.

Window layout
-------------
_TABLE starts at 05:00 UTC and runs to 21:00 UTC (17 slots).
The minimum price is at TABLE index 6 → 05:00 + 6h = **11:00 UTC** (price 0.131).

``_TROUGH_IDX == 6`` (verified at import time by assertion below).
``_TROUGH_HOUR == 2026-06-27T11:00Z``.

Fixture provenance
------------------
All constants are copied verbatim from ``scratchpad/decisive.py`` and are
identical to the canonical fixture in ``test_optimize_floor_economic.py`` (A1).
The critical parameters — ``eta_charge=0.92``, ``soc_start=22%``, P80=P50×1.6 —
are documented in decisive.py and the A1 test docstring.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.anker_x1_smartgrid.controller import compute_decision
from custom_components.anker_x1_smartgrid.guard import command_setpoint
from custom_components.anker_x1_smartgrid.models import (
    Config, ControllerState, PlanState, PlantInputs, PriceSlot,
)

# ---------------------------------------------------------------------------
# Canonical fixture — copied from scratchpad/decisive.py (same as A1 test)
# ---------------------------------------------------------------------------

# (hour_utc, all-in price €/kWh, pv_w, load50_w)
_TABLE = [
    (5, 0.263, 134, 773), (6, 0.259, 204, 383), (7, 0.222, 287, 312),
    (8, 0.174, 400, 257), (9, 0.146, 651, 318), (10, 0.133, 233, 258),
    (11, 0.131, 554, 203), (12, 0.135, 1446, 344), (13, 0.146, 1655, 343),
    (14, 0.191, 1614, 401), (15, 0.250, 1404, 470), (16, 0.282, 1095, 467),
    (17, 0.341, 638, 550), (18, 0.412, 175, 451), (19, 0.419, 48, 418),
    (20, 0.348, 0, 397), (21, 0.310, 0, 278),
]
_N = len(_TABLE)
_PRICE = [r[1] for r in _TABLE]
_LOAD50_W = [float(r[3]) for r in _TABLE]
_SOC_START = 22.0
_NOW0 = datetime(2026, 6, 27, 5, 0, tzinfo=timezone.utc)  # window start = 05:00 UTC

# Trough: global minimum of _PRICE — must be index 6 (11:00, 0.131 €/kWh).
_TROUGH_IDX = _PRICE.index(min(_PRICE))
assert _TROUGH_IDX == 6, (
    f"Fixture invariant broken: expected trough at index 6, got {_TROUGH_IDX}"
)
_TROUGH_HOUR = _NOW0 + timedelta(hours=_TROUGH_IDX)  # 2026-06-27 11:00 UTC

_BASE_CFG = dict(
    capacity_kwh=10.0, soc_floor=5.0, soc_target=100.0, eta_charge=0.92,
    max_charge_w=6000.0, max_export_w=6000.0, grid_export_limit_w=6000.0,
    enable_export=True, export_fee_eur_per_kwh=0.02, export_peak_band_frac=0.12,
    round_trip_eff=0.85, cycle_cost_eur_per_kwh=0.04,
    water_value_factor=1.0, clamp_water_value_nonneg=True,
    # Zero dwell so state transitions happen immediately in unit tests.
    min_dwell_min=0,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg() -> Config:
    return Config.from_dict(_BASE_CFG)


def _slots() -> list[PriceSlot]:
    """Full TABLE price slots, one per clock-hour from 05:00 UTC."""
    return [PriceSlot(_NOW0 + timedelta(hours=i), r[1]) for i, r in enumerate(_TABLE)]


def _today_watts() -> list[tuple[datetime, float]]:
    """PV watts samples from the TABLE (one entry per hour, 05:00–21:00 UTC).

    Passed as ``today_watts`` to ``compute_decision`` so the PV curve uses the
    real TABLE values rather than the synth_pv_curve fallback.
    """
    return [(_NOW0 + timedelta(hours=i), float(r[2])) for i, r in enumerate(_TABLE)]


class _TablePredictor:
    """Predictor keyed to the TABLE's hourly P50 load.

    ``quantile >= 0.8``  → P80 = min(P50 × 1.6, 6000 W)  (decisive.py convention)
    ``quantile <  0.8``  → P50
    """

    def __init__(self) -> None:
        # key: UTC hour-of-day; value: P50 load in W
        self._load50: dict[int, float] = {r[0]: float(r[3]) for r in _TABLE}

    def predict(
        self, when: datetime, temp: float | None, fallback_w: float, *, quantile: float = 0.5,
    ) -> float:
        base = self._load50.get(when.hour, fallback_w)
        return min(base * 1.6, 6000.0) if quantile >= 0.8 else base


def _passive_plan(now: datetime) -> PlanState:
    """Fresh PASSIVE plan with dwell already elapsed (state_since = now − 20 min).

    ``min_dwell_min=0`` makes any positive elapsed time qualify, so this is
    just a clean 'entered PASSIVE 20 minutes ago' prior plan.
    """
    return PlanState(ControllerState.PASSIVE, now - timedelta(minutes=20), ())


# ---------------------------------------------------------------------------
# Test 1 — 05:00 tick: PASSIVE, zero pre-trough grid charge
# ---------------------------------------------------------------------------

def test_morning_no_survival_charge_passive():
    """A1 fix: at 05:00 the controller stays PASSIVE and schedules no morning charge.

    Scenario: soc=22%, P80=P50×1.6, real TABLE PV/prices.  The overnight
    bridge deficit is ≈0.084 kWh — fully coverable at the 11:00 trough.
    Before A1 the DP added a survival floor-charge at ~07:00 (decisive.py
    ``B full`` morning=54 W), making 07:00 a selected slot.  After A1 the DP
    rides to the floor and meets below-floor load via direct grid→load import,
    so ``grid_request`` for hours 05–10 is all-zero.

    Trough window index: ``_TROUGH_IDX == 6`` (11:00, price 0.131 €/kWh).
    """
    cfg = _cfg()
    now = _NOW0  # 05:00 UTC
    inputs = PlantInputs(soc=_SOC_START, phase_import_w=(0.0, 0.0, 0.0), now=now)
    sunset = _NOW0 + timedelta(hours=17)   # 22:00 UTC — safely past the TABLE end (21:00)
    plan = _passive_plan(now)
    _out: dict = {}

    new_plan, setpoint, _deadline, _horizon, _hm, _ = compute_decision(
        plan, inputs, _slots(),
        pv_remaining=0.0,
        sunset=sunset,
        predictor=_TablePredictor(),
        cur_temp=None,
        cfg=cfg,
        today_watts=_today_watts(),
        _out=_out,
    )

    # A2: 05:00 is NOT a DP-selected slot → state stays PASSIVE, setpoint = 0
    assert new_plan.state is ControllerState.PASSIVE, (
        f"Expected PASSIVE at 05:00 (no morning survival charge), got {new_plan.state}. "
        f"dp_selected={_out.get('dp_selected')}"
    )
    assert setpoint == 0.0, f"Expected setpoint=0 at 05:00, got {setpoint}"

    # A1: grid_request must be zero for all pre-trough hours (05:00–10:00)
    grid_req = _out.get("grid_request", {})
    pre_trough_hours = [_NOW0 + timedelta(hours=h) for h in range(_TROUGH_IDX)]
    pre_trough_wh = sum(grid_req.get(h, 0.0) for h in pre_trough_hours)
    noisy_hours = [h for h in pre_trough_hours if grid_req.get(h, 0.0) > 1.0]
    assert pre_trough_wh == pytest.approx(0.0, abs=1.0), (
        f"Pre-trough grid_request must be ≈0 Wh (no survival buy); "
        f"got {pre_trough_wh:.1f} Wh — non-zero hours: "
        f"{[str(h.time()) for h in noisy_hours]}"
    )

    # The trough (11:00) must still appear as a planned charge (arbitrage pays)
    trough_wh = grid_req.get(_TROUGH_HOUR, 0.0)
    assert trough_wh > 0.0, (
        f"Expected grid_request > 0 at trough {_TROUGH_HOUR.time()}; "
        f"got {trough_wh:.1f} Wh  (full grid_req keys: "
        f"{sorted(str(k.time()) for k in grid_req)})"
    )


# ---------------------------------------------------------------------------
# Test 2 — 11:00 tick: FORCING, trough economic top-off actuates
# ---------------------------------------------------------------------------

def test_trough_economic_topoff_actuates():
    """A2 fix: at 11:00 (trough, soc=5%), controller enters FORCING.

    After the morning drain the battery is at the firmware floor (5%, deficit≈0
    from here).  Before A2, ``decide_state`` required ``deficit > eps_hi_kwh``
    (0.4 kWh) to enter FORCING; deficit≈0 kept the controller PASSIVE and the
    cheap trough was wasted.  After A2, FORCING is gated solely on
    ``now_selected`` (the DP selects 11:00 as the global minimum-price slot),
    so the controller enters FORCING and issues the max-charge setpoint.

    Setpoint must equal ``command_setpoint(cfg.max_charge_w, 0.0, cfg)`` —
    the same expression in controller.py line ~795.
    """
    cfg = _cfg()
    now = _TROUGH_HOUR  # 11:00 UTC
    # SoC after riding the morning drain to the firmware floor.
    inputs = PlantInputs(soc=5.0, phase_import_w=(0.0, 0.0, 0.0), now=now)
    sunset = _NOW0 + timedelta(hours=17)   # 22:00 UTC
    plan = _passive_plan(now)
    _out: dict = {}

    new_plan, setpoint, _deadline, _horizon, _hm, _ = compute_decision(
        plan, inputs, _slots(),
        pv_remaining=0.0,
        sunset=sunset,
        predictor=_TablePredictor(),
        cur_temp=None,
        cfg=cfg,
        today_watts=_today_watts(),
        _out=_out,
    )

    # A2: 11:00 is DP-selected (cheapest slot, soc well below ceiling) → FORCING
    assert new_plan.state is ControllerState.FORCING, (
        f"Expected FORCING at trough 11:00 with soc=5% (economic top-off), "
        f"got {new_plan.state}. "
        f"dp_selected={_out.get('dp_selected')} "
        f"grid_request={_out.get('grid_request')}"
    )

    # Setpoint must equal the standard FORCING charge setpoint (negative = charge)
    expected_setpoint = command_setpoint(cfg.max_charge_w, 0.0, cfg)
    assert setpoint == pytest.approx(expected_setpoint, abs=1e-6), (
        f"Expected setpoint={expected_setpoint} (FORCING charge), got {setpoint}"
    )
    assert setpoint < 0.0, "FORCING setpoint must be negative (charge direction)"
