import pytest
from datetime import datetime, timezone, timedelta
from custom_components.anker_x1_smartgrid.models import Config, PlanState, PlantInputs, PriceSlot
from custom_components.anker_x1_smartgrid import controller, forecast

BASE = datetime(2026, 6, 20, 11, 0, tzinfo=timezone.utc)


def test_compute_decision_returns_horizon():
    cfg = Config(capacity_kwh=10.0, soc_target=100.0, eta_charge=1.0, min_dwell_min=0)
    inputs = PlantInputs(soc=20.0, meter_w=0.0, now=BASE)
    slots = [PriceSlot(BASE + timedelta(hours=i), 0.30) for i in range(6)]
    sunset = BASE + timedelta(hours=6)
    predictor = forecast.LoadPredictor.from_profile({})
    plan_state = PlanState.initial(BASE - timedelta(hours=1))
    result = controller.compute_decision(
        plan_state, inputs, slots, pv_remaining=2.0, sunset=sunset,
        predictor=predictor, cur_temp=None, cfg=cfg,
    )
    assert len(result) == 6
    horizon = result[3]
    assert isinstance(horizon, list) and len(horizon) > 0
    assert {"start", "price", "mode", "soc"} <= set(horizon[0].keys())


def test_live_horizon_carries_non_flat_reserve_soc_and_export_when_committed():
    """Regression: build_display_horizon (live path, sun_times present) must carry
    non-flat reserve_soc and non-zero grid_export_w when the DP commits export.

    This test locks in the bug-fix: before the fix, reserve_soc was flat at soc_floor
    and grid_export_w was 0 regardless of the DP export plan.

    Price design (keeps only peaks above hurdle):
    - keep_value ≈ compute_water_value(0.05) = 0.05/0.95 ≈ 0.053 €/DC-kWh
    - cycle_cost = 0.04 €/kWh
    - Hurdle: export_price × eta_d - cycle_cost > keep_value
    - At 0.09 (regular hours): 0.09 × 0.947 - 0.04 = 0.045 < 0.053 → doesn't clear ✓
    - At 0.45 (peak hours 14-15): 0.45 × 0.947 - 0.04 = 0.386 > 0.053 → clears ✓
    Only the 2 peak hours export, keeping augmented load feasible for pre-charge.
    """
    # 36 hours of slots with a mix of cheap/expensive prices
    # Trough: hours 0-1 (0.05), regular: all others (0.09 — below hurdle),
    # peak: hours 14-15 (0.45 — above hurdle, the only export-worthy hours)
    slot_prices = [0.09] * 36
    slot_prices[0] = 0.05   # trough — charge here cheaply
    slot_prices[1] = 0.05
    slot_prices[14] = 0.45  # peak — export here
    slot_prices[15] = 0.45
    slots = [PriceSlot(BASE + timedelta(hours=i), slot_prices[i]) for i in range(36)]

    # sun_times: sunset tonight, sunrise/sunset tomorrow
    sun_times = (
        BASE + timedelta(hours=8),    # today_sunset
        BASE + timedelta(hours=18),   # tomorrow_sunrise
        BASE + timedelta(hours=32),   # tomorrow_sunset
    )
    sunset = BASE + timedelta(hours=8)

    # Large ample battery at 90% SoC — lots of surplus above reserve
    # export_price_matches_import=True: the 0.45 peak is also the export price forecast
    # cycle_cost_eur_per_kwh=0.04 (default): ensures 0.09 hours don't clear the hurdle
    cfg = Config(
        capacity_kwh=10.0, soc_floor=5.0, soc_target=97.0,
        max_charge_w=3000.0, eta_charge=0.95, round_trip_eff=0.90,
        enable_export=True,
        max_export_w=3000.0, grid_export_limit_w=6000.0,
        min_dwell_min=0,
    )
    inputs = PlantInputs(soc=90.0, meter_w=0.0, now=BASE)
    predictor = forecast.LoadPredictor.from_profile({})
    plan_state = PlanState.initial(BASE - timedelta(hours=1))

    result = controller.compute_decision(
        plan_state, inputs, slots, pv_remaining=0.5, sunset=sunset,
        predictor=predictor, cur_temp=None, cfg=cfg,
        tomorrow_total=6.0,
        sun_times=sun_times,
        today_arrays=[(0.5, None)],
        tomorrow_arrays=[(6.0, None)],
        export_price=0.45,
        # The test simulates the common case: export entity == import entity (Zonneplan).
        # Per-hour export forecast = per-hour import forecast.  Only hours with price=0.45
        # clear the export hurdle (0.45 × η_d − 0.04 > keep_value ≈ 0.053).
        export_price_matches_import=True,
    )
    horizon = result[3]
    assert isinstance(horizon, list) and len(horizon) > 0

    # Every entry must have reserve_soc and grid_export_w keys
    for entry in horizon:
        assert "reserve_soc" in entry, "horizon entry missing reserve_soc"
        assert "grid_export_w" in entry, "horizon entry missing grid_export_w"

    # reserve_soc must NOT be uniformly flat at soc_floor=5.0 — it should vary
    # as the ride-out reserve changes per hour (approaching the next solar/cheap window)
    reserve_socs = [entry["reserve_soc"] for entry in horizon]
    assert not all(r == pytest.approx(cfg.soc_floor) for r in reserve_socs), (
        f"reserve_soc is flat at soc_floor={cfg.soc_floor} — per-hour reserve not wired in. "
        f"Values: {reserve_socs[:6]}"
    )

    # When DP commits export for peak hours (0.45 price), at least one export entry
    # should have grid_export_w > 0
    export_ws = [entry["grid_export_w"] for entry in horizon]
    assert any(w > 0 for w in export_ws), (
        f"No export in horizon despite 0.45 export price and high SoC. "
        f"Export values: {export_ws[:20]}"
    )


def test_compute_decision_forwards_temp_by_hour_to_display_horizon(monkeypatch):
    """Regression: compute_decision's sun_times-present branch (build_display_horizon,
    controller.py ~1099) must forward temp_by_hour — before the fix it was omitted, so
    the DP/decision intervals (controller.py ~815, already correct) used per-hour
    forecast temps while the PUBLISHED display horizon silently fell back to the flat
    cur_temp scalar for every future hour."""
    captured: dict = {}
    real = controller.plan_mod.build_display_horizon

    def spy(*a, **kw):
        captured["temp_by_hour"] = kw.get("temp_by_hour")
        return real(*a, **kw)

    monkeypatch.setattr(controller.plan_mod, "build_display_horizon", spy)

    cfg = Config(capacity_kwh=10.0, soc_target=97.0, max_charge_w=3000.0,
                 eta_charge=0.95, round_trip_eff=0.90, min_dwell_min=0)
    slots = [PriceSlot(BASE + timedelta(hours=i), 0.20) for i in range(30)]
    sun_times = (
        BASE + timedelta(hours=8),    # today_sunset
        BASE + timedelta(hours=18),   # tomorrow_sunrise
        BASE + timedelta(hours=32),   # tomorrow_sunset
    )
    inputs = PlantInputs(soc=50.0, meter_w=0.0, now=BASE)
    predictor = forecast.LoadPredictor.from_profile({})
    plan_state = PlanState.initial(BASE - timedelta(hours=1))
    temp_by_hour = {
        (BASE + timedelta(hours=i)).replace(minute=0, second=0, microsecond=0): float(i * 2)
        for i in range(30)
    }

    controller.compute_decision(
        plan_state, inputs, slots, pv_remaining=0.5, sunset=BASE + timedelta(hours=8),
        predictor=predictor, cur_temp=99.0, cfg=cfg,
        tomorrow_total=6.0, sun_times=sun_times,
        today_arrays=[(0.5, None)], tomorrow_arrays=[(6.0, None)],
        temp_by_hour=temp_by_hour,
    )

    assert captured.get("temp_by_hour") == temp_by_hour, (
        "build_display_horizon was not passed compute_decision's temp_by_hour "
        f"(got {captured.get('temp_by_hour')!r})"
    )
