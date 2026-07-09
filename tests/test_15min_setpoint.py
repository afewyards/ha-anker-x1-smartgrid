"""T7: per-slot kWh -> average W setpoint (·1000/dt_h), not hourly-only ·1000."""
from datetime import datetime, timedelta, timezone

from custom_components.anker_x1_smartgrid import controller as ctrl
from custom_components.anker_x1_smartgrid.models import Config, ForecastInterval, PriceSlot, PlantInputs

UTC = timezone.utc
NOW = datetime(2026, 8, 1, 2, 0, tzinfo=UTC)


def test_half_kwh_quarter_slot_is_2000w():
    cfg = Config(capacity_kwh=10.0, soc_floor=20.0, soc_target=80.0,
                 max_charge_w=2000.0, eta_charge=1.0, charge_window_price_band=1.0)
    n = 4
    slots = [PriceSlot(NOW + timedelta(minutes=15*i), 0.05) for i in range(n)]
    ivs = [ForecastInterval(NOW + timedelta(minutes=15*i), 0.0, 0.0, 0.25) for i in range(n)]
    inputs = PlantInputs(soc=50.0, meter_w=0.0, now=NOW)
    sel, grid, infeasible, exp, rev, ceil = ctrl._dp_select_slots(
        inputs=inputs, slots=slots, deadline=NOW + timedelta(hours=1),
        ceiling=0.20, cfg=cfg, export_price=None, intervals=ivs,
        slot_minutes=15, dt_h=0.25,
    )
    # 2 kW * 0.25 h = 0.5 kWh in slot 0 -> W = 0.5 / 0.25 * 1000 = 2000 W.
    assert abs(grid[NOW] - 2000.0) < 1.0
