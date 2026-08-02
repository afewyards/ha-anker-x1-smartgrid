"""Task 3 (2026-08-01 PV-cadence-energy-fix wave) — end-to-end PV energy pin
through the DECISION path's DP window bucketing.

``decision._dp_select_slots`` buckets P50 ``ForecastInterval``s into
``window_pv`` (decision.py:275-288, weighted time-overlap into slot_minutes
buckets) and hands that array straight to ``optimize.optimize_grid``. What
lands in those intervals is entirely determined by two upstream builders:

  * Task 1 — ``parsers.build_pv_curve_from_watts`` now emits a DENSE
    sample-and-hold curve from raw sub-hourly watts samples (a 30-min-cadence
    source at step_h=0.25 turns {11:00: 386, 11:30: 2145} into the per-slot
    curve [386, 386, 2145, 2145]).
  * Task 2 — ``plan.build_display_intervals`` reads that curve via a
    step-function lookup (most recent point <= slot start, < 1h old) instead
    of the legacy hour-SUM fan, so each 15-min ForecastInterval gets its own
    curve value instead of the whole hour's summed watts.

Before both fixes (main @ df87248), the hour-summing fan handed every quarter
within an hour the SAME raw-sample SUM for that hour, double-counting energy
whenever more than one sample landed in the hour — exactly the live defect in
memory `pv-curve-cadence-doubling` (DP window_pv 2x truth on a 30-min-cadence
PV source at slot_minutes=15).

Both tests below drive the REAL ``decision.compute_decision`` (not a
reimplementation of the bucketing loop), monkeypatching
``optimize.optimize_grid`` to capture the exact array it receives — same
capture recipe as ``scripts/replay_dp.py``. window_pv/window_load/window_price
are passed POSITIONALLY by decision.py, so the spy captures *args*, not just
kwargs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.anker_x1_smartgrid import optimize as optimize_mod
from custom_components.anker_x1_smartgrid.decision import compute_decision
from custom_components.anker_x1_smartgrid.forecast import LoadPredictor
from custom_components.anker_x1_smartgrid.models import (
    Config,
    ControllerState,
    PlanState,
    PlantInputs,
    PriceSlot,
)

_PREDICTOR = LoadPredictor.from_profile({})


def _cfg(**overrides) -> Config:
    return Config.from_dict(
        {
            "capacity_kwh": 10.0,
            "soc_target": 97.0,
            "soc_floor": 5.0,
            "eta_charge": 0.92,
            "round_trip_eff": 0.85,
            "min_dwell_min": 0,
            "max_charge_w": 6000.0,
            "cycle_cost_eur_per_kwh": 0.10,
            # Keep the pre-DP wiring minimal -- the overnight terminal credit
            # is irrelevant to window_pv bucketing, which is what this file pins.
            "terminal_overnight_credit": False,
            **overrides,
        }
    )


def _price_slots(start: datetime, n: int, slot_minutes: int, price: float = 0.20) -> list[PriceSlot]:
    stride = timedelta(minutes=slot_minutes)
    return [PriceSlot(start=start + i * stride, price=price) for i in range(n)]


def _plan(now: datetime) -> PlanState:
    return PlanState(ControllerState.PASSIVE, now - timedelta(hours=2), ())


def _run_decision(
    cfg: Config,
    *,
    now: datetime,
    slots: list[PriceSlot],
    slot_minutes: int,
    today_watts,
) -> dict:
    """Drive compute_decision, capturing the (args, kwargs) optimize_grid receives.

    Any exception inside the DP block is swallowed by compute_decision itself
    (falls back to PASSIVE) -- the spy records ``captured`` BEFORE delegating
    to the real function, so the capture survives that fallback regardless.
    """
    real_optimize_grid = optimize_mod.optimize_grid
    captured: dict = {}

    def _spy(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return real_optimize_grid(*args, **kwargs)

    optimize_mod.optimize_grid = _spy
    try:
        inputs = PlantInputs(soc=50.0, meter_w=0.0, now=now)
        sunset = slots[-1].start + timedelta(minutes=slot_minutes)
        compute_decision(
            _plan(now),
            inputs,
            slots,
            0.0,  # pv_remaining -- unused: today_watts drives the PV curve
            sunset,
            _PREDICTOR,
            None,
            cfg,
            today_watts=today_watts,
            slot_minutes=slot_minutes,
        )
    finally:
        optimize_mod.optimize_grid = real_optimize_grid

    assert "args" in captured, "the live DP path must call optimize_grid exactly once"
    return captured


# ---------------------------------------------------------------------------
# 1. Live-defect pin: a 30-min-cadence watts source at slot_minutes=15 must
#    contribute the TRUE hourly energy (1.2655 kWh), NOT the doubled legacy
#    value (2.531 kWh).
# ---------------------------------------------------------------------------

_T1_BASE = datetime(2026, 8, 2, 8, 0, tzinfo=UTC)  # slot-aligned "now"


def test_30min_source_hour_energy_not_doubled():
    """386W@11:00Z + 2145W@11:30Z (30-min cadence) fed at slot_minutes=15.

    Task 1's sample-and-hold fill turns this into the dense per-quarter watts
    curve [386, 825.75, 1705.25, 2145] over 11:00/11:15/11:30/11:45Z (the
    exact worked example in ``build_pv_curve_from_watts``'s own docstring). Task
    2's step-function lookup carries those values into the ForecastIntervals
    unfiltered, so the 11:00Z hour's window_pv bucket sum is:

        (386 + 386 + 2145 + 2145) W * 0.25 h / 1000 = 1.2655 kWh

    Pre-fix (main @ df87248), the hour-summing fan handed EVERY quarter in
    the hour the raw 30-min SUM (386+2145=2531 W) instead of each point's own
    value, doubling the hour's energy to 2.531 kWh.
    """
    cfg = _cfg()
    slot_minutes = 15
    slots = _price_slots(_T1_BASE, 16, slot_minutes)  # 08:00 .. 11:45Z -> horizon_edge 12:00Z
    today_watts = [
        [
            (_T1_BASE + timedelta(hours=3), 386.0),  # 11:00Z
            (_T1_BASE + timedelta(hours=3, minutes=30), 2145.0),  # 11:30Z
        ]
    ]

    captured = _run_decision(cfg, now=_T1_BASE, slots=slots, slot_minutes=slot_minutes, today_watts=today_watts)
    window_pv = captured["args"][0]
    assert len(window_pv) == 16

    now_h = _T1_BASE  # already 15-min slot-aligned
    hour_11z = datetime(2026, 8, 2, 11, 0, tzinfo=UTC)
    idx = int((hour_11z - now_h).total_seconds() // (slot_minutes * 60))
    hour_kwh = sum(window_pv[idx : idx + 4])

    assert hour_kwh == pytest.approx(1.2655, abs=1e-3)
    # Explicitly rule out the pre-fix doubled value (well outside noise/rounding).
    assert abs(hour_kwh - 2.531) > 0.5


# ---------------------------------------------------------------------------
# 2. Conservation: total window_pv energy must be resolution-independent.
# ---------------------------------------------------------------------------

_T2_BASE = datetime(2026, 8, 3, 0, 0, tzinfo=UTC)
_HOURLY_WATTS = [50.0 * h for h in range(24)]


def _hourly_today_watts() -> list[list[tuple[datetime, float]]]:
    samples = [(_T2_BASE + timedelta(hours=h), _HOURLY_WATTS[h]) for h in range(24)]
    # Trailing 25th sample, one hour PAST the window: with it, hour 23's curve
    # buckets are interior-gap-filled dense like every other hour's. Without it
    # the CURVE would simply end at 23:00 (hourly tail gap >= 1h -> no tail
    # mirror) -- but conservation would STILL hold, because
    # build_display_intervals' interval-level <1h hold carries the 23:00 point
    # across the 23:15/23:30/23:45Z slots anyway. The sample is kept purely so
    # the fixture treats hour 23 uniformly with hours 0-22 (single code path
    # under test), not because either resolution's total depends on it. It sits
    # outside the decision window (no price slot exists at 24:00Z).
    samples.append((_T2_BASE + timedelta(hours=24), _HOURLY_WATTS[23]))
    return [samples]


def test_window_pv_energy_conserved_across_slot_minutes():
    """Full synthetic day, single hourly-cadence PV source: total window_pv
    energy is (near-)identical whether the DP window ticks at slot_minutes=60
    or slot_minutes=15 -- resolution must not manufacture or destroy PV energy.

    The fixture is a monotone ramp that is still RISING at the window edge, so
    midpoint interpolation leaves a boundary residue of exactly
    step_h/2 * (delta_out - delta_in) = 0.25/2 * 50 W = 6.25 Wh (0.045% of the
    day).  Interior boundaries telescope out exactly.  A curve that is flat at
    both edges -- i.e. any real PV day, 0 W at night -- conserves exactly; that
    case is pinned by test_window_pv_energy_conserved_zero_ended_day below.
    """
    today_watts = _hourly_today_watts()

    slots_60 = _price_slots(_T2_BASE, 24, 60)
    captured_60 = _run_decision(_cfg(), now=_T2_BASE, slots=slots_60, slot_minutes=60, today_watts=today_watts)
    window_pv_60 = captured_60["args"][0]
    assert len(window_pv_60) == 24
    total_60 = sum(window_pv_60)

    slots_15 = _price_slots(_T2_BASE, 96, 15)
    captured_15 = _run_decision(_cfg(), now=_T2_BASE, slots=slots_15, slot_minutes=15, today_watts=today_watts)
    window_pv_15 = captured_15["args"][0]
    assert len(window_pv_15) == 96
    total_15 = sum(window_pv_15)

    expected_kwh = sum(_HOURLY_WATTS) / 1000.0
    assert total_60 == pytest.approx(expected_kwh, abs=1e-6)
    assert total_15 == pytest.approx(expected_kwh, abs=0.007)
    assert total_60 == pytest.approx(total_15, abs=0.007)


_T3_BASE = datetime(2026, 8, 4, 0, 0, tzinfo=UTC)
# Realistic day: dark until 04:00Z, bell through the afternoon, dark from 20:00Z.
_BELL_WATTS = [0.0] * 4 + [200.0, 600.0, 1100.0, 1600.0, 2000.0, 2200.0, 2300.0, 2200.0,
                           2000.0, 1600.0, 1100.0, 600.0, 200.0] + [0.0] * 7


def test_window_pv_energy_conserved_zero_ended_day():
    """A PV day that is dark at both window edges conserves energy EXACTLY
    across slot_minutes -- the interpolation residue is a pure boundary term
    and both boundaries are flat here."""
    samples = [(_T3_BASE + timedelta(hours=h), _BELL_WATTS[h]) for h in range(24)]
    samples.append((_T3_BASE + timedelta(hours=24), 0.0))
    today_watts = [samples]

    slots_60 = _price_slots(_T3_BASE, 24, 60)
    total_60 = sum(_run_decision(_cfg(), now=_T3_BASE, slots=slots_60, slot_minutes=60,
                                 today_watts=today_watts)["args"][0])

    slots_15 = _price_slots(_T3_BASE, 96, 15)
    total_15 = sum(_run_decision(_cfg(), now=_T3_BASE, slots=slots_15, slot_minutes=15,
                                 today_watts=today_watts)["args"][0])

    assert total_60 == pytest.approx(sum(_BELL_WATTS) / 1000.0, abs=1e-9)
    assert total_15 == pytest.approx(total_60, abs=1e-9)
