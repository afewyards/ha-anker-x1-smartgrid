"""C4: the plan card's arbitrage_pnl reflects the DP's planned export revenue."""
import types

from custom_components.anker_x1_smartgrid.sensor import X1PlanSensor


def test_plan_sensor_arbitrage_pnl_reads_planned_revenue():
    sensor = X1PlanSensor.__new__(X1PlanSensor)  # bypass _Base.__init__
    sensor._controller = types.SimpleNamespace(
        last_status={
            "plan": {"planned_grid_hours": 2, "horizon": [], "deadline": None},
            "planned_export_revenue_eur": 1.234,
            "today_export_pnl_eur": 0.0,
        }
    )
    assert sensor.extra_state_attributes["arbitrage_pnl"] == 1.234


def test_dp_select_slots_returns_export_revenue():
    """_dp_select_slots returns the DP's planned export_revenue_eur as its 5th element."""
    from datetime import datetime, timedelta, timezone

    from custom_components.anker_x1_smartgrid import controller as ctrl
    from custom_components.anker_x1_smartgrid.models import (
        Config,
        ForecastInterval,
        PlantInputs,
        PriceSlot,
    )

    now = datetime(2026, 6, 26, 12, 0, tzinfo=timezone.utc)
    cfg = Config(
        capacity_kwh=10.0,
        soc_floor=20.0,
        soc_target=80.0,
        max_charge_w=3000.0,
        eta_charge=1.0,
        round_trip_eff=1.0,
        cycle_cost_eur_per_kwh=0.04,
        export_fee_eur_per_kwh=0.0,
        enable_export=True,
        export_peak_band_frac=0.5,
    )
    inputs = PlantInputs(soc=80.0, phase_import_w=(0.0, 0.0, 0.0), now=now)
    deadline = now + timedelta(hours=6)
    slots = [PriceSlot(now + timedelta(hours=i), 0.20) for i in range(6)]
    ivs = [ForecastInterval(now + timedelta(hours=i), 0.0, 0.0, 1.0) for i in range(6)]
    out = ctrl._dp_select_slots(
        inputs=inputs,
        slots=slots,
        deadline=deadline,
        ceiling=0.34,
        cfg=cfg,
        export_price=0.60,
        export_price_matches_import=False,
        intervals=ivs,
    )
    assert len(out) == 6
    assert isinstance(out[4], float)
    assert out[4] >= 0.0
    assert isinstance(out[5], dict)  # ceiling_by_hour (SoC% per hour)
