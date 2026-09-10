"""The 45a incident end to end: a calibration hold must not run, unpriced,
across the evening export peak.

Plan-horizon rows shaped like ``plan.build_plan_horizon`` output go through
``build_calib_slots`` into ``calibration_plan`` with nothing stubbed, so the
cost model, the gates and the commitment rules are exercised together.
"""

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.anker_x1_smartgrid import calibration
from custom_components.anker_x1_smartgrid.models import Config

DAY = datetime(2026, 9, 10, tzinfo=UTC)
SLOT = timedelta(minutes=15)
PEAK = DAY + timedelta(hours=17)
CAP = 20.0
ETA = 0.92
LOAD = 0.125  # kWh per slot: a flat 500 W house load
CFG = Config(
    capacity_kwh=CAP,
    max_charge_w=12000.0,
    eta_charge=ETA,
    calibration_enabled=True,
    calibration_interval_days=5,
    calibration_top_soc=100.0,
    calibration_dwell_h=0.5,
)
WATER_VALUE = 0.21
PRICE_HISTORY = {"2026-09-01": {str(i): 0.30 for i in range(96)}}  # P30 = 0.30


def _history(days):
    """Passive samples at 50% spanning ``days``: no dwell ever qualified, so
    the cycle is exactly ``days`` old."""
    start = DAY - timedelta(days=days)
    return [((start + timedelta(hours=h)).isoformat(), 50.0, "passive") for h in range(int(days * 24) + 1)]


DUE, OVERDUE = _history(6), _history(30)


def _price(hour):
    if hour < 7:
        return 0.20
    if hour < 11:
        return 0.25
    if hour < 15:
        return 0.10
    if hour == 17:
        return 0.40
    return 0.30


def _flow(pv=0.0, solar=0.0, grid=0.0, discharge=0.0, export=0.0):
    return {"pv": pv, "solar": solar, "grid": grid, "discharge": discharge, "export": export}


def _evening(hour):
    if hour == 17:
        return _flow(discharge=LOAD, export=1.5)  # the DP's 6 kWh peak export
    if hour >= 18:
        return _flow(discharge=LOAD)
    return _flow()


def _sunny():
    """PV fills the pack from 55% by 13:00, then spills until 17:00."""
    need_ac = 0.45 * CAP / ETA
    flows = []
    for i in range(96):
        hour = i // 4
        if 8 <= hour < 13:
            solar = need_ac * (i - 31) / 210.0  # a ramp summing to need_ac over 20 slots
            flows.append(_flow(pv=solar + LOAD, solar=solar))
        elif 13 <= hour < 17:
            flows.append(_flow(pv=need_ac / 10.5 * (1.0 - (i - 52) / 20.0)))
        else:
            flows.append(_evening(hour))
    return flows


def _cloudy():
    """The 45a day: the DP grid-charges to 90% by 15:00, then to 100% right
    at 17:00, to export it across the peak."""
    first, second = 0.40 * CAP / ETA / 16, 0.10 * CAP / ETA / 5
    flows = []
    for i in range(96):
        hour, minute = divmod(i * 15, 60)
        if 11 <= hour < 15:
            flows.append(_flow(pv=0.10, grid=first))
        elif hour == 15 and minute < 45:
            flows.append(_flow(pv=0.10))
        elif hour in (15, 16):
            flows.append(_flow(pv=0.10, grid=second))
        else:
            flows.append(_evening(hour))
    return flows


def _horizon(flows, now, live_soc):
    """Rows as ``plan.build_plan_horizon`` publishes them at ``now``. Elapsed
    slots are measured actuals. The slot in progress is modelled from the live
    SoC over its remaining minutes: its battery flows are that remainder,
    while pv_kwh/load_kwh stay whole-slot forecasts."""
    rows = []
    soc_kwh = live_soc / 100.0 * CAP
    for i, f in enumerate(flows):
        start = DAY + i * SLOT
        row = {"start": start.isoformat(), "price": _price(start.hour), "estimated": False}
        row |= {"pv_kwh": f["pv"], "load_kwh": LOAD}
        if start + SLOT <= now:
            row |= {"mode": "actual", "soc": live_soc, "solar_charge_kwh": f["solar"], "grid_charge_kwh": f["grid"]}
            rows.append(row | {"self_discharge_kwh": f["discharge"], "grid_export_kwh": f["export"]})
            continue
        part = (start + SLOT - max(start, now)) / SLOT
        room = (CAP - soc_kwh) / ETA
        solar = min(f["solar"] * part, room)
        grid = min(f["grid"] * part, room - solar)
        discharge, export = f["discharge"] * part, f["export"] * part
        soc_kwh = max(0.0, soc_kwh + (solar + grid) * ETA - discharge - export)
        mode = "grid" if grid > 0 else "export" if export > 0 else "solar" if f["pv"] > LOAD else "idle"
        row |= {"mode": mode, "soc": round(soc_kwh / CAP * 100.0, 1), "solar_charge_kwh": solar}
        rows.append(row | {"grid_charge_kwh": grid, "self_discharge_kwh": discharge, "grid_export_kwh": export})
    return rows


def _plan(flows, now, soc, history, prev=None):
    horizon = _horizon(flows, now, soc)
    slots = calibration.build_calib_slots(horizon, now, soc, 15, lambda _start, price: price)
    return calibration.calibration_plan(
        now, soc, slots, history, PRICE_HISTORY, CFG, prev=prev, water_value=WATER_VALUE
    )


def test_sunny_day_holds_at_the_midday_top_out_for_free():
    now = DAY + timedelta(hours=13)
    plan = _plan(_sunny(), now, 100.0, DUE)
    assert plan.phase == "holding"
    assert plan.window_start == now
    assert plan.window_end is not None and plan.window_end <= PEAK
    assert plan.cost_eur == pytest.approx(0.0, abs=0.01)


def test_cloudy_day_due_does_not_hold_across_the_peak():
    """The DP filled the pack to 100% at 17:00 precisely to export it."""
    plan = _plan(_cloudy(), PEAK, 100.0, DUE)
    assert plan.phase not in ("holding", "charging")
    assert plan.action is None
    assert plan.cost_eur is not None and plan.cost_eur > 0.50


def test_cloudy_day_overdue_holds_within_the_cap():
    """Overdue, the cycle may give up part of the peak, up to the cap."""
    plan = _plan(_cloudy(), PEAK, 100.0, OVERDUE)
    assert plan.phase == "holding"
    assert plan.cost_eur is not None and plan.cost_eur <= 1.00


def test_a_late_top_out_is_not_held_unpriced_into_the_peak():
    """The 16:30 window is priced to end at ~17:04, but the tapering pack
    only tops out at 17:00:30. A hold from there runs 26 minutes past the
    priced window, across the planned export, so it is priced afresh."""
    flows = _cloudy()
    start = DAY + timedelta(hours=16, minutes=30)
    committed = _plan(flows, start, 96.0, DUE)
    assert committed.phase == "charging"
    assert committed.window_start == start
    assert committed.window_end is not None and committed.window_end < PEAK + timedelta(minutes=5)

    plan = _plan(flows, PEAK + timedelta(seconds=30), 100.0, DUE, prev=committed)
    assert plan.action is None
    assert plan.cost_eur is not None and plan.cost_eur > 0.50


def test_a_stale_hold_does_not_carry_into_the_peak():
    """A 13:00 hold whose samples never qualified is no longer committed at
    the 17:00 peak."""
    stale = calibration.CalibPlan("holding", DAY + timedelta(hours=13), DAY + timedelta(hours=13, minutes=30), 0.0)
    plan = _plan(_sunny(), PEAK, 99.5, DUE, prev=stale)
    assert plan.action is None
    assert plan.cost_eur is not None and plan.cost_eur > 0.50
