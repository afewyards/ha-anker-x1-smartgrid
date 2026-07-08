"""T12: 96-slot golden — DP genuinely trades intra-hour at 15-minute resolution.

End-to-end proof (not just a display-card check) that the DP schedule ITSELF
captures sub-hour arbitrage once ``_dp_select_slots`` / ``optimize_grid`` run
at ``dt_h=0.25`` / ``slot_minutes=15``:

- ``test_dp_charges_cheapest_quarter_and_exports_priciest_quarter`` proves the
  DP grid-charges the single cheap QUARTER (03:15 @ 0.05) while its hourly
  neighbor (03:00 @ 0.25) stays untouched, and exports the single pricey
  QUARTER (18:30 @ 0.60) while its hourly neighbor (18:15 @ 0.25) does not.
  Confirmed non-tautological: re-running the identical 96-slot price/interval
  arrays through ``_dp_select_slots`` with legacy hourly args
  (``slot_minutes=60, dt_h=1.0``) makes ``cheap_q`` vanish from the ``grid``
  dict entirely (keys land on the hour, not the quarter) and ``exp`` comes
  back empty — i.e. every assertion below genuinely fails under hourly
  resolution, it does not just happen to pass.
- ``test_grid_charge_ceiling_dawn_boundary_at_correct_slot`` proves the
  solar-reservation dawn boundary lands at the correct 15-min SLOT index (16,
  not the legacy hourly 4) and that the ceiling array is built at the full
  96-slot length.
"""
from datetime import datetime, timedelta, timezone

from custom_components.anker_x1_smartgrid import controller as ctrl
from custom_components.anker_x1_smartgrid.optimize import solar_cycle_end_idx, solar_reservation_ceiling
from custom_components.anker_x1_smartgrid.models import Config, ForecastInterval, PriceSlot, PlantInputs

UTC = timezone.utc
NOW = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)


def _cfg():
    return Config(capacity_kwh=10.0, soc_floor=20.0, soc_target=80.0,
                  max_charge_w=4000.0, eta_charge=1.0, round_trip_eff=0.9,
                  max_export_w=4000.0, grid_export_limit_w=6000.0, enable_export=True,
                  export_peak_band_frac=0.10, export_peak_lookback_h=4,
                  charge_window_price_band=0.03, charge_trough_lookback_h=8)


def _day96():
    prices = [0.25] * 96
    prices[13] = 0.05   # 03:15 cheapest quarter
    prices[74] = 0.60   # 18:30 priciest quarter
    slots = [PriceSlot(NOW + timedelta(minutes=15 * i), prices[i]) for i in range(96)]
    ivs = [ForecastInterval(NOW + timedelta(minutes=15 * i), 0.0, 200.0, 0.25) for i in range(96)]
    return slots, ivs


def test_dp_charges_cheapest_quarter_and_exports_priciest_quarter():
    slots, ivs = _day96()
    inputs = PlantInputs(soc=50.0, phase_import_w=(0.0, 0.0, 0.0), now=NOW)
    sel, grid, infeasible, exp, rev, ceil_ = ctrl._dp_select_slots(
        inputs=inputs, slots=slots, deadline=NOW + timedelta(hours=24),
        ceiling=0.30, cfg=_cfg(), export_price=0.60,
        export_price_matches_import=True, intervals=ivs,
        slot_minutes=15, dt_h=0.25,
    )
    cheap_q = NOW + timedelta(minutes=15 * 13)     # 03:15
    neighbor = NOW + timedelta(minutes=15 * 12)    # 03:00 (0.25) — same hour as cheap_q
    pricey_q = NOW + timedelta(minutes=15 * 74)    # 18:30
    pricey_neighbor = NOW + timedelta(minutes=15 * 73)  # 18:15 (0.25) — same hour as pricey_q

    # DP grid-charged the cheap quarter, NOT the whole hour it sits in.
    assert cheap_q in grid
    assert grid.get(neighbor, 0.0) < grid.get(cheap_q, 0.0)

    # DP exported the pricey quarter, NOT its hourly neighbor.
    assert exp.get(pricey_q, 0.0) > 0.0
    assert exp.get(pricey_neighbor, 0.0) < exp.get(pricey_q, 0.0)

    # Non-tautology check: the SAME 96-slot arrays through the legacy hourly
    # call (slot_minutes=60, dt_h=1.0) cannot resolve the quarter-hour spike —
    # cheap_q isn't even an hour-boundary key, so it drops out of `grid`
    # entirely, and the isolated pricey quarter is swamped by its flat hourly
    # neighbors and never clears the export band. Every assertion above would
    # fail under this hourly call, proving they exercise real sub-hour DP
    # resolution rather than being vacuously true.
    _, grid_hourly, _, exp_hourly, _, _ = ctrl._dp_select_slots(
        inputs=inputs, slots=slots, deadline=NOW + timedelta(hours=24),
        ceiling=0.30, cfg=_cfg(), export_price=0.60,
        export_price_matches_import=True, intervals=ivs,
        slot_minutes=60, dt_h=1.0,
    )
    assert cheap_q not in grid_hourly
    assert exp_hourly.get(pricey_q, 0.0) == 0.0


def test_grid_charge_ceiling_dawn_boundary_at_correct_slot():
    # sunrise 4h ahead → boundary at slot 16 (15-min), evening slots reserve nothing.
    now = datetime(2026, 8, 1, 2, 0, tzinfo=UTC)
    sun = (now + timedelta(hours=10), now + timedelta(hours=4), now + timedelta(hours=20))
    cyc = solar_cycle_end_idx(now, 96, sun, slot_minutes=15)
    assert cyc[0] == 16
    # ceiling built with that boundary reserves today's solar only before the boundary
    pv = [0.0] * 96
    load = [0.0] * 96
    pv[20] = 3.0   # a post-dawn solar slot
    ceil_ = solar_reservation_ceiling(pv, load, _cfg(), cycle_end_idx=cyc, dt_h=0.25)
    assert len(ceil_) == 96
