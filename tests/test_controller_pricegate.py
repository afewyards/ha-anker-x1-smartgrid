"""Tests: price gate in compute_decision — no charging at a loss."""
from datetime import datetime, timezone, timedelta

from custom_components.anker_x1_smartgrid.models import (
    Config, PlanState, PlantInputs, PriceSlot, ControllerState,
)
from custom_components.anker_x1_smartgrid import controller, forecast

BASE = datetime(2026, 6, 20, 18, 0, tzinfo=timezone.utc)


def _slots(prices):
    return [PriceSlot(BASE + timedelta(hours=i), p) for i, p in enumerate(prices)]


def test_no_force_when_only_slot_is_peak_priced():
    # reproduces tonight: 94% SoC tiny deficit, deadline in 1h, current price = peak
    cfg = Config(capacity_kwh=10.0, soc_target=100.0, eta_charge=0.92,
                 round_trip_eff=0.85, min_dwell_min=0)
    inputs = PlantInputs(soc=94.0, phase_import_w=(0.0, 0.0, 0.0), now=BASE)
    # current hour 0.343, then the evening peak ~0.356 after deadline
    slots = _slots([0.343, 0.356, 0.335, 0.307, 0.299])
    sunset = BASE + timedelta(hours=1)
    predictor = forecast.LoadPredictor.from_profile({})
    plan = PlanState.initial(BASE - timedelta(hours=2))
    new_plan, setpoint, deadline, _h, _, _ = controller.compute_decision(
        plan, inputs, slots, pv_remaining=0.0, sunset=sunset,
        predictor=predictor, cur_temp=None, cfg=cfg,
    )
    assert new_plan.state is ControllerState.PASSIVE
    assert setpoint == 0.0


def test_force_when_cheap_slot_available_before_deadline():
    # a genuinely cheap slot before the deadline should still charge
    cfg = Config(capacity_kwh=10.0, soc_target=100.0, eta_charge=0.92,
                 round_trip_eff=0.85, min_dwell_min=0)
    inputs = PlantInputs(soc=20.0, phase_import_w=(0.0, 0.0, 0.0), now=BASE)
    # cheap now (0.10), expensive peak later (0.40) -> 0.10 <= 0.40*0.85=0.34 -> worthy
    slots = _slots([0.10, 0.40, 0.40, 0.40, 0.40, 0.40])
    sunset = BASE + timedelta(hours=6)
    predictor = forecast.LoadPredictor.from_profile({})
    plan = PlanState.initial(BASE - timedelta(hours=2))
    new_plan, setpoint, deadline, _h, _, _ = controller.compute_decision(
        plan, inputs, slots, pv_remaining=0.0, sunset=sunset,
        predictor=predictor, cur_temp=None, cfg=cfg,
    )
    assert new_plan.state is ControllerState.FORCING
    assert setpoint < 0.0
