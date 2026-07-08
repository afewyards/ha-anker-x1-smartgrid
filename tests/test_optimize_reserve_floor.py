"""C1b: per-hour ride-out reserve as the export discharge floor."""
from __future__ import annotations

from typing import Any

import pytest

from custom_components.anker_x1_smartgrid.models import Config
from custom_components.anker_x1_smartgrid.optimize import optimize_grid


def _cfg(**kw: Any) -> Config:
    d: dict[str, Any] = dict(
        capacity_kwh=10.0, soc_floor=20.0, soc_target=80.0, max_charge_w=3000.0,
        eta_charge=1.0, round_trip_eff=1.0, cycle_cost_eur_per_kwh=0.04,
        export_fee_eur_per_kwh=0.0, max_export_w=3000.0, grid_export_limit_w=3000.0,
    )
    d.update(kw)
    return Config(**d)


def test_export_stops_at_reserve_floor_not_firmware_floor():
    """Two rich export hours (rate cap = 3 kWh/h); reserve floor (5 kWh) caps the drain
    above the firmware floor (2 kWh)."""
    cfg = _cfg()
    n = 6
    pv = [0.0] * n
    load = [0.0] * n
    price = [0.20] * n
    export_price = [0.0] * n
    export_price[2] = 0.60  # two peak hours clear the hurdle (need 2h to drain >3 kWh)
    export_price[3] = 0.60
    # Reserve = 5 kWh (50%) every hour; firmware floor = 2 kWh (20%).
    reserve = [5.0] * n
    res = optimize_grid(
        pv, load, price, soc_start=80.0, cfg=cfg,  # start 8 kWh
        window_start_h=0, window_len=n,
        export_price=export_price, terminal_mode="water_value", water_value=0.0,
        reserve_by_hour=reserve,
    )
    # Exported DC = start(8) - reserve(5) = 3 kWh; SoC never drops below 5 kWh
    # (h2 drains 8->5, h3 is already at the reserve floor -> exports 0).
    assert sum(res["export_schedule"]) == pytest.approx(3.0, abs=1e-6)


def test_reserve_none_matches_firmware_floor():
    """reserve_by_hour=None drains to the firmware floor (8 -> 2 kWh = 6 kWh over two hours)."""
    cfg = _cfg()
    n = 6
    pv = [0.0] * n
    load = [0.0] * n
    price = [0.20] * n
    export_price = [0.0] * n
    export_price[2] = 0.60
    export_price[3] = 0.60  # 3 kWh/h rate cap => two hours needed to export 6 kWh
    res = optimize_grid(
        pv, load, price, soc_start=80.0, cfg=cfg,
        window_start_h=0, window_len=n,
        export_price=export_price, terminal_mode="water_value", water_value=0.0,
        reserve_by_hour=None,
    )
    assert sum(res["export_schedule"]) == pytest.approx(6.0, abs=1e-6)


def test_reserve_length_mismatch_raises():
    cfg = _cfg()
    with pytest.raises(ValueError):
        optimize_grid(
            [0.0] * 3, [0.0] * 3, [0.2] * 3, soc_start=80.0, cfg=cfg,
            window_start_h=0, window_len=3, reserve_by_hour=[5.0, 5.0],  # len 2 != 3
        )
