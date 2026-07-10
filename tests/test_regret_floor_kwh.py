"""Regression test for code-review finding M1.

Both ``optimize_grid`` and ``hindsight_optimal_grid`` correctly add the
below-floor direct-import COST (``floor_import_eur``) to the returned ``eur``,
but historically did NOT add the corresponding ENERGY VOLUME to the returned
``total_kwh``.  Meanwhile ``realized_grid_cost`` folds its forced below-floor
imports into ``kwh`` (regret.py:746).

So on a drain-to-floor day where the controller acted OPTIMALLY (realized ==
optimal → ``regret_eur == 0``), ``score_regret`` computed::

    over_buy_kwh = max(0, realized_kwh - oracle_kwh)

which over-reported a PHANTOM over-buy exactly equal to the floor-import volume,
inflating the user-facing ``over_buy_kwh`` / ``over_buy_eur`` sensors and the
``daily_regret`` recorder columns.  ``regret_eur`` itself is unaffected (the
``eur`` term cancels symmetrically).

These tests pin the fix: on an optimal drain-to-floor day the oracle ``kwh`` now
folds the floor-import volume, so ``over_buy_kwh`` and ``over_buy_eur`` are 0.
"""
import pytest

from custom_components.anker_x1_smartgrid.models import Config
from custom_components.anker_x1_smartgrid.regret import (
    DayData,
    hindsight_optimal_grid,
    realized_grid_cost,
    score_regret,
)


def _make_cfg() -> Config:
    """Config whose ``soc_floor`` (20% = 2 kWh) is a soft decision floor;
    the firmware hard floor is 0.5 kWh (const.FIRMWARE_SOC_FLOOR, 5%) and is
    what actually binds physically on the test day.

    ``eta_charge`` < 1.0 makes direct grid->load import STRICTLY cheaper than a
    charge-ahead, so the oracle rides to the floor with an all-zero charge
    schedule (no equal-cost tie-break charge), and the floor really binds.
    """
    return Config(
        capacity_kwh=10.0,
        soc_floor=20.0,   # 2 kWh soft floor (firmware floor is 0.5 kWh)
        soc_target=80.0,  # 8 kWh
        max_charge_w=3000.0,
        eta_charge=0.92,
    )


def _drain_to_floor_day() -> DayData:
    """No PV, flat price, soc_start 30% (3 kWh), 1 kWh/h load.

    Trajectory under a zero charge schedule (floor imports book only below
    the firmware hard floor, 0.5 kWh):
      h0:  3 -> 2                              import 0 (above 0.5 kWh)
      h1:  2 -> 1                              import 0 (above 0.5 kWh)
      h2:  1 -> 0 -> clamp to firmware floor    import 0.5 kWh
      h3..h23: 0.5 -> -0.5 -> clamp             import 1.0 kWh each (21 hours)
    -> 21.5 kWh of below-firmware-floor direct grid->load import
       @ 0.20 €/kWh = 4.30 €.
    """
    pv = [0.0] * 24
    load = [1.0] * 24
    price = [0.20] * 24
    return DayData(
        pv_kwh=tuple(pv), load_kwh=tuple(load),
        price=tuple(price), soc_start=30.0,
    )


def test_oracle_kwh_folds_floor_import_no_phantom_over_buy():
    cfg = _make_cfg()
    day = _drain_to_floor_day()

    # Oracle: water-value terminal mode with value 0 -> no incentive to charge,
    # so the optimal play is to ride to the floor with a zero charge schedule.
    optimal = hindsight_optimal_grid(
        day, cfg, terminal_mode="water_value", water_value=0.0,
    )
    # Sanity: the oracle does NOT force-charge to hold the floor (economic-only).
    assert sum(optimal["schedule"]) == pytest.approx(0.0, abs=1e-9)

    # Realized: the controller acted optimally -> the SAME zero charge schedule.
    realized = realized_grid_cost(day, [0.0] * 24, cfg)

    # The firmware floor binds starting h2 (0.5 kWh) then 1 kWh direct import
    # each subsequent hour (h3-h23, 21 hours): 0.5 + 21*1.0 = 21.5 kWh.
    assert realized["kwh"] == pytest.approx(21.5, abs=1e-9)
    assert realized["eur"] == pytest.approx(4.30, abs=1e-9)

    score = score_regret(realized, optimal)

    # Authoritative metric: realized == optimal -> zero regret (M1 leaves this
    # untouched; the eur term already cancels symmetrically).
    assert score["regret_eur"] == pytest.approx(0.0, abs=1e-9)

    # M1: the oracle kwh now folds the floor-import volume, so there is NO
    # phantom over-buy.  Pre-fix both were inflated by the 21.5 kWh floor import.
    assert score["over_buy_kwh"] == pytest.approx(0.0, abs=1e-9)
    assert score["over_buy_eur"] == pytest.approx(0.0, abs=1e-9)

    # Direct check on the returned oracle volume (the exact locus of the bug):
    # it must match the realized volume, which already folds forced floor imports.
    assert optimal["kwh"] == pytest.approx(realized["kwh"], abs=1e-9)
    assert optimal["kwh"] == pytest.approx(21.5, abs=1e-9)
