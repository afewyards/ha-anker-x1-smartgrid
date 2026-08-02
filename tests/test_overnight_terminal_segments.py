"""Unit tests for optimize.overnight_terminal_segments (per-hour piecewise
overnight terminal-credit builder; spec rev-3).

Replaces the retired scalar two-segment terminal-credit builder with a
chronological per-hour walk over the post-horizon gap: every *priced* gap
hour becomes its own ``(dc_kwh, value_eur_per_dc_kwh)`` segment, valued
independently — no cross-hour averaging, no ``-cycle_cost`` term, no
is_cheap early break, no global upper clamp. See
``docs/superpowers/specs/2026-08-01-terminal-piecewise-credit-design.md``
section B for the normative formulas.
"""

from datetime import datetime, timedelta

import pytest

from custom_components.anker_x1_smartgrid.models import Config
from custom_components.anker_x1_smartgrid.optimize import overnight_terminal_segments

ETA_D = min(0.90 / 0.95, 1.0)  # eta_discharge_static for the default test cfg


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


def test_empty_gap_returns_empty_segments_zero_need_and_v_lo():
    segs, need, v_hi = overnight_terminal_segments(T0, T0, {}, {}, v_lo=0.13, cfg=_cfg())
    assert segs == []
    assert need == 0.0
    assert v_hi == 0.13


def test_gap_start_after_pickup_is_empty():
    segs, need, v_hi = overnight_terminal_segments(T0, T0 - timedelta(hours=2), {}, {}, v_lo=0.13, cfg=_cfg())
    assert segs == []
    assert need == 0.0
    assert v_hi == 0.13


def test_no_priced_hours_returns_empty_segments_and_v_lo():
    # 4-hour gap, no prices at all → no segments can be built at all (unlike
    # the legacy walk, need is no longer derived independently of price).
    pickup = T0 + timedelta(hours=4)
    segs, need, v_hi = overnight_terminal_segments(T0, pickup, {}, {}, v_lo=0.13, cfg=_cfg())
    assert segs == []
    assert need == 0.0
    assert v_hi == 0.13


# ---------------------------------------------------------------------------
# Idle dilution / no cycle-cost term
# ---------------------------------------------------------------------------


def test_idle_drain_dilutes_raw_value_below_price_times_eta():
    # With idle_drain_w>0, dc_h grows (extra DC drawn for standby) while the
    # load-serving numerator is unchanged → raw_v_h < price * eta_d.
    pickup = T0 + timedelta(hours=1)
    prices = _hours(T0, [0.30])
    load = {T0.hour: 800.0}

    cfg_idle = _cfg(idle_drain_w=200.0)
    segs_idle, _, _ = overnight_terminal_segments(T0, pickup, prices, load, v_lo=0.0, cfg=cfg_idle)
    assert len(segs_idle) == 1
    _, v_idle = segs_idle[0]
    assert v_idle < 0.30 * ETA_D

    cfg_no_idle = _cfg(idle_drain_w=0.0)
    segs_no_idle, _, _ = overnight_terminal_segments(T0, pickup, prices, load, v_lo=0.0, cfg=cfg_no_idle)
    _, v_no_idle = segs_no_idle[0]
    assert v_no_idle == pytest.approx(0.30 * ETA_D)


def test_seg_v_independent_of_cycle_cost():
    # No "- cycle_cost" term anywhere: seg_v is unchanged across cycle_cost values.
    pickup = T0 + timedelta(hours=1)
    prices = _hours(T0, [0.30])
    load = {T0.hour: 800.0}

    segs_low, _, _ = overnight_terminal_segments(
        T0, pickup, prices, load, v_lo=0.0, cfg=_cfg(cycle_cost_eur_per_kwh=0.0)
    )
    segs_high, _, _ = overnight_terminal_segments(
        T0, pickup, prices, load, v_lo=0.0, cfg=_cfg(cycle_cost_eur_per_kwh=0.50)
    )
    assert len(segs_low) == len(segs_high) == 1
    assert segs_low[0][0] == pytest.approx(segs_high[0][0])
    assert segs_low[0][1] == pytest.approx(segs_high[0][1])
    assert segs_low[0][1] == pytest.approx(0.30 * ETA_D)


# ---------------------------------------------------------------------------
# Per-segment v_lo floor
# ---------------------------------------------------------------------------


def test_per_segment_v_lo_floor():
    # A cheap gap hour's raw value floors at v_lo instead of going negative-ish.
    pickup = T0 + timedelta(hours=1)
    prices = _hours(T0, [0.05])
    load = {T0.hour: 800.0}
    cfg = _cfg()

    segs, need, _ = overnight_terminal_segments(T0, pickup, prices, load, v_lo=0.10, cfg=cfg)
    assert len(segs) == 1
    _, seg_v = segs[0]
    assert seg_v == pytest.approx(0.10)
    assert need == 0.0  # raw_v_h (0.05 * eta_d) <= v_lo → excluded from need


# ---------------------------------------------------------------------------
# Cap trims lowest-value segments first
# ---------------------------------------------------------------------------


def test_cap_trims_lowest_value_segment_first():
    # capacity_kwh=2.0 → firmware_floor=0.10 → cap 1.90 DC-kWh available.
    # Hour0 (0.40 €/kWh, 1000 W) is the higher-value segment and stays intact;
    # Hour1 (0.10 €/kWh, 1500 W) is the lower-value segment and absorbs the trim.
    cfg = _cfg(capacity_kwh=2.0)
    pickup = T0 + timedelta(hours=2)
    prices = _hours(T0, [0.40, 0.10])
    load = {T0.hour: 1000.0, (T0 + timedelta(hours=1)).hour: 1500.0}

    segs, need, v_hi = overnight_terminal_segments(T0, pickup, prices, load, v_lo=0.20, cfg=cfg)

    assert len(segs) == 2
    dc0, v0 = segs[0]
    dc1, v1 = segs[1]
    full_dc0 = 1000.0 / ETA_D / 1000.0
    full_dc1 = 1500.0 / ETA_D / 1000.0
    cap = cfg.capacity_kwh - cfg.firmware_floor_kwh

    assert v0 > v1  # sanity: segment 0 is genuinely the higher-value one
    assert dc0 == pytest.approx(full_dc0)  # untouched — the higher-value segment
    assert dc1 == pytest.approx(full_dc1 - (full_dc0 + full_dc1 - cap))  # trimmed
    assert dc0 + dc1 == pytest.approx(cap)
    assert need == pytest.approx(full_dc0)  # only the above-v_lo segment counts
    assert v1 < v_hi < v0  # dc-weighted mean of the two kept (trimmed) segments


# ---------------------------------------------------------------------------
# Unpriced-hour skip
# ---------------------------------------------------------------------------


def test_unpriced_hour_contributes_no_segment():
    cfg = _cfg()
    pickup = T0 + timedelta(hours=3)
    prices = {T0: 0.30, T0 + timedelta(hours=1): 0.30}  # hour index 2 unpriced
    load = {h: 800.0 for h in range(24)}

    segs, _, _ = overnight_terminal_segments(T0, pickup, prices, load, v_lo=0.10, cfg=cfg)

    assert len(segs) == 2
    per_hour_dc = 800.0 / ETA_D / 1000.0
    assert sum(dc for dc, _ in segs) == pytest.approx(2 * per_hour_dc)


# ---------------------------------------------------------------------------
# need excludes cheap-hour segments
# ---------------------------------------------------------------------------


def test_need_excludes_cheap_hour_segments():
    cfg = _cfg()
    pickup = T0 + timedelta(hours=2)
    prices = _hours(T0, [0.30, 0.05])  # hour0 expensive, hour1 cheap
    load = {h: 800.0 for h in range(24)}

    segs, need, _ = overnight_terminal_segments(T0, pickup, prices, load, v_lo=0.10, cfg=cfg)

    per_hour_dc = 800.0 / ETA_D / 1000.0
    assert len(segs) == 2  # the cheap hour still produces a segment (floored)
    assert segs[1][1] == pytest.approx(0.10)
    assert need == pytest.approx(per_hour_dc)  # only hour0 counted


# ---------------------------------------------------------------------------
# eta_curve honored
# ---------------------------------------------------------------------------


def test_eta_curve_honored():
    from custom_components.anker_x1_smartgrid.efficiency import BinStat, EfficiencyCurve

    cfg = _cfg(eta_charge=0.92, round_trip_eff=0.85)
    base = EfficiencyCurve.static(cfg)
    disch = list(base._discharge)
    disch[1] = BinStat(disch[1].lo_w, disch[1].hi_w, "discharge", 0.80, 0.80, 99, 9.0, True, "")
    curve = EfficiencyCurve(list(base._charge), disch, base._fc, base._fd)

    pickup = T0 + timedelta(hours=1)
    prices = _hours(T0, [0.30])
    load = {T0.hour: 600.0}  # falls in discharge bin 1

    segs_static, _, _ = overnight_terminal_segments(T0, pickup, prices, load, v_lo=0.0, cfg=cfg)
    segs_curve, _, _ = overnight_terminal_segments(T0, pickup, prices, load, v_lo=0.0, cfg=cfg, eta_curve=curve)

    assert segs_curve[0][0] > segs_static[0][0]  # lower curve eta ⇒ more DC drawn


# ---------------------------------------------------------------------------
# Glitch hour only inflates its own segment (no global clamp)
# ---------------------------------------------------------------------------


def test_glitch_hour_only_inflates_own_segment():
    cfg = _cfg()
    pickup = T0 + timedelta(hours=3)
    prices = _hours(T0, [0.30, 0.30, 2.0])
    load = {h: 800.0 for h in range(24)}

    segs, _, v_hi = overnight_terminal_segments(T0, pickup, prices, load, v_lo=0.10, cfg=cfg)

    assert len(segs) == 3
    (_, v0), (_, v1), (_, v2) = segs
    assert v0 == pytest.approx(0.30 * ETA_D)
    assert v1 == pytest.approx(0.30 * ETA_D)  # untouched by the glitch hour
    assert v2 == pytest.approx(2.0 * ETA_D)  # no upper clamp — inflates only itself

    per_hour_dc = 800.0 / ETA_D / 1000.0
    expected_mean = (v0 + v1 + v2) * per_hour_dc / (3 * per_hour_dc)
    assert v_hi == pytest.approx(expected_mean)


# ---------------------------------------------------------------------------
# v_hi_mean is dc-weighted, not a plain average
# ---------------------------------------------------------------------------


def test_v_hi_mean_is_dc_weighted_not_simple_average():
    cfg = _cfg()
    gap_start = datetime(2026, 7, 18, 1, 0, 0)  # hod 1
    pickup = gap_start + timedelta(hours=2)  # hours 1 and 2
    prices = _hours(gap_start, [0.10, 0.50])
    load = {1: 100.0, 2: 2000.0}  # heavier load on the more valuable hour

    segs, _, v_hi = overnight_terminal_segments(gap_start, pickup, prices, load, v_lo=0.0, cfg=cfg)

    dc0, v0 = segs[0]
    dc1, v1 = segs[1]
    simple_mean = (v0 + v1) / 2.0
    weighted_mean = (dc0 * v0 + dc1 * v1) / (dc0 + dc1)

    assert v_hi == pytest.approx(weighted_mean)
    assert v_hi > simple_mean
