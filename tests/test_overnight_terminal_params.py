"""Unit tests for optimize.overnight_terminal_params (overnight two-segment terminal builder).

Covers the two outputs of the helper:

* **need_kwh** — a debit-only, trough-anchored walk over the post-horizon gap
  (mirrors ``energy.ride_out_reserve_kwh`` physics; pv=0 so every gap hour is a
  deficit hour), with the ``_build_is_cheap_by_hour`` early-break and the
  ``[0, capacity − firmware_floor]`` clamp.
* **v_hi** — ``clamp(load_weighted_mean(gap prices)·η_d_static − cycle_cost,
  v_lo, max(v_lo, max_export_dc_value))``.
"""

from datetime import datetime, timedelta

import pytest

from custom_components.anker_x1_smartgrid.models import Config
from custom_components.anker_x1_smartgrid.optimize import overnight_terminal_params

ETA_D = min(0.90 / 0.95, 1.0)  # eta_discharge_static for the test cfg


def _cfg(**kw) -> Config:
    base = dict(
        eta_charge=0.95,
        round_trip_eff=0.90,
        capacity_kwh=10.0,
        cycle_cost_eur_per_kwh=0.10,
        idle_drain_w=0.0,
        reserve_cheap_band=0.20,
    )
    base.update(kw)
    return Config(**base)


def _hours(start: datetime, prices: list[float]) -> dict[datetime, float]:
    """Build an hour-keyed price map starting at *start*."""
    return {start + timedelta(hours=i): p for i, p in enumerate(prices)}


T0 = datetime(2026, 7, 18, 22, 0, 0)  # 22:00 local, a plausible horizon edge


# ---------------------------------------------------------------------------
# Empty / degenerate gaps
# ---------------------------------------------------------------------------


def test_empty_gap_returns_v_lo_and_zero_need():
    v_hi, need = overnight_terminal_params(T0, T0, {}, {}, v_lo=0.13, max_export_dc_value=0.5, cfg=_cfg())
    assert (v_hi, need) == (0.13, 0.0)


def test_gap_start_after_pickup_is_empty():
    v_hi, need = overnight_terminal_params(
        T0, T0 - timedelta(hours=2), {}, {}, v_lo=0.13, max_export_dc_value=0.5, cfg=_cfg()
    )
    assert (v_hi, need) == (0.13, 0.0)


def test_no_priced_gap_returns_v_lo_but_walks_need():
    # 4-hour gap, no prices at all → v_hi floors to v_lo, need is the full walk.
    # load_w_by_hod empty → const.DEFAULT_FALLBACK_LOAD_W (400 W) every hour.
    pickup = T0 + timedelta(hours=4)
    v_hi, need = overnight_terminal_params(T0, pickup, {}, {}, v_lo=0.13, max_export_dc_value=0.5, cfg=_cfg())
    assert v_hi == 0.13
    assert need == pytest.approx(4 * 400.0 / ETA_D / 1000.0)


# ---------------------------------------------------------------------------
# need: cap, cheap-break, eta_curve
# ---------------------------------------------------------------------------


def test_need_capped_at_capacity_minus_firmware_floor():
    # 30-hour gap at 2000 W → raw walk ~63 kWh, clamped to capacity − fw_floor.
    cfg = _cfg()
    pickup = T0 + timedelta(hours=30)
    load = {h: 2000.0 for h in range(24)}
    _, need = overnight_terminal_params(T0, pickup, {}, load, v_lo=0.10, max_export_dc_value=0.5, cfg=cfg)
    assert need == pytest.approx(cfg.capacity_kwh - cfg.firmware_floor_kwh)  # 10.0 − 0.5 = 9.5


def test_cheap_interior_hour_breaks_the_walk():
    # 6-hour gap, 800 W each. A cheap 4th hour (index 3) stops the walk there, so
    # need reflects only hours 0..2 — strictly less than the plain 6-hour sum.
    cfg = _cfg()
    pickup = T0 + timedelta(hours=6)
    prices = _hours(T0, [0.30, 0.30, 0.30, 0.10, 0.30, 0.30])
    load = {h: 800.0 for h in range(24)}
    _, need = overnight_terminal_params(T0, pickup, prices, load, v_lo=0.10, max_export_dc_value=1.0, cfg=cfg)

    per_hour = 800.0 / ETA_D / 1000.0
    assert need == pytest.approx(3 * per_hour)  # broke before the cheap hour 3
    assert need < 6 * per_hour  # smaller than the un-broken plain sum


def test_eta_curve_honored_for_need():
    # A curve with a lower low-power discharge eta draws more DC per hour → larger
    # need than the static walk (mirrors test_energy reserve-curve semantics).
    from custom_components.anker_x1_smartgrid.efficiency import BinStat, EfficiencyCurve

    cfg = _cfg(eta_charge=0.92, round_trip_eff=0.85)
    base = EfficiencyCurve.static(cfg)
    disch = list(base._discharge)
    disch[1] = BinStat(disch[1].lo_w, disch[1].hi_w, "discharge", 0.80, 0.80, 99, 9.0, True, "")
    curve = EfficiencyCurve(list(base._charge), disch, base._fc, base._fd)

    pickup = T0 + timedelta(hours=3)
    load = {h: 600.0 for h in range(24)}  # 600 W falls in discharge bin 1

    _, need_static = overnight_terminal_params(T0, pickup, {}, load, v_lo=0.10, max_export_dc_value=0.5, cfg=cfg)
    _, need_curve = overnight_terminal_params(
        T0, pickup, {}, load, v_lo=0.10, max_export_dc_value=0.5, cfg=cfg, eta_curve=curve
    )
    assert need_curve > need_static


# ---------------------------------------------------------------------------
# v_hi: cycle-cost term, clamps, load-weighted mean
# ---------------------------------------------------------------------------


def test_v_hi_subtracts_cycle_cost():
    # Uniform 0.30 €/kWh gap, clamps not binding → v_hi = mean·η_d − cycle_cost.
    cfg = _cfg()
    pickup = T0 + timedelta(hours=3)
    prices = _hours(T0, [0.30, 0.30, 0.30])
    v_hi, _ = overnight_terminal_params(T0, pickup, prices, {}, v_lo=0.10, max_export_dc_value=0.50, cfg=cfg)
    assert v_hi == pytest.approx(0.30 * ETA_D - 0.10)


def test_v_hi_upper_clamped_at_max_export_dc_value():
    # A single 2.0 €/kWh glitch hour inflates the mean; the upper clamp pins v_hi
    # to max_export_dc_value (holding can never beat exporting above it).
    cfg = _cfg()
    pickup = T0 + timedelta(hours=3)
    prices = _hours(T0, [0.30, 0.30, 2.0])
    v_hi, _ = overnight_terminal_params(T0, pickup, prices, {}, v_lo=0.10, max_export_dc_value=0.50, cfg=cfg)
    assert v_hi == pytest.approx(0.50)


def test_v_hi_floored_at_v_lo():
    # Cheap night → raw·η_d − cc goes negative; v_hi floors to v_lo.
    cfg = _cfg()
    pickup = T0 + timedelta(hours=3)
    prices = _hours(T0, [0.05, 0.05, 0.05])
    v_hi, _ = overnight_terminal_params(T0, pickup, prices, {}, v_lo=0.10, max_export_dc_value=0.50, cfg=cfg)
    assert v_hi == pytest.approx(0.10)


def test_v_hi_load_weighted_mean_asymmetry():
    # Cheap hour with light load, expensive hour with heavy load → the load-weighted
    # mean (0.46) exceeds the plain mean (0.30), so v_hi is driven by the weighting.
    cfg = _cfg()
    gap_start = datetime(2026, 7, 18, 1, 0, 0)  # hod 1
    pickup = gap_start + timedelta(hours=2)  # hours 1 and 2
    prices = _hours(gap_start, [0.10, 0.50])
    load = {1: 100.0, 2: 900.0}

    v_hi, _ = overnight_terminal_params(
        gap_start, pickup, prices, load, v_lo=0.10, max_export_dc_value=1.0, cfg=cfg
    )
    weighted_mean = (0.10 * 100.0 + 0.50 * 900.0) / (100.0 + 900.0)  # 0.46
    assert v_hi == pytest.approx(weighted_mean * ETA_D - 0.10)
    assert v_hi > (0.30 * ETA_D - 0.10)  # strictly above the plain-mean value
