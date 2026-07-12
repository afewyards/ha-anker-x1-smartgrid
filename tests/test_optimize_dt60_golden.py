"""C3: end-to-end dt=60 golden + rollup-vs-15min boundary doc test.

REGEN: python -m tests.regen_goldens
"""
import pytest

from custom_components.anker_x1_smartgrid.optimize import optimize_grid
from custom_components.anker_x1_smartgrid.models import Config


def _cfg():
    return Config(capacity_kwh=10.0, soc_floor=10.0, soc_target=90.0,
                  max_charge_w=3000.0, eta_charge=0.95, round_trip_eff=0.90)


def _scenario():
    pv    = [0.0]*6 + [0.5, 1.0, 1.5, 2.0, 1.5, 1.0, 0.5] + [0.0]*11
    load  = [0.4]*24
    price = [0.10, 0.09, 0.08, 0.08, 0.09, 0.12, 0.18, 0.25, 0.22, 0.18, 0.15, 0.14,
             0.13, 0.14, 0.16, 0.20, 0.28, 0.35, 0.40, 0.34, 0.26, 0.20, 0.15, 0.12]
    return pv, load, price


def _assert_close(out, golden):
    assert set(out) == set(golden)
    for k, gv in golden.items():
        ov = out[k]
        if isinstance(gv, list):
            assert len(ov) == len(gv)
            for a, b in zip(ov, gv):
                assert a == pytest.approx(b, abs=1e-9)
        elif isinstance(gv, float):
            assert ov == pytest.approx(gv, abs=1e-9)
        else:
            assert ov == gv


GOLDEN = {
    "schedule": [
        0.0,
        0.0,
        0.0,
        1.5789473684210527,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.4210526315789474,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.210526315789474,
        3.0000000000000004,
    ],
    "kwh": 6.210526315789474,
    "eur": 0.726842105263158,
}


def test_optimize_grid_dt60_golden():
    pv, load, price = _scenario()
    out = optimize_grid(pv, load, price, soc_start=50.0, cfg=_cfg(),
                        window_start_h=0, window_len=24, dt_h=1.0, slots_per_day=24)
    _assert_close(out, GOLDEN)


def test_rollup_hour_locked_vs_15min_slot_boundary_documented():
    """DOC: at dt_h=1.0 the planner is hour-locked — a 15-min price refinement
    within an hour is not resolved until slot_minutes flips to 15."""
    pv, load, price = _scenario()
    hourly = optimize_grid(pv, load, price, soc_start=50.0, cfg=_cfg(),
                           window_start_h=0, window_len=24, dt_h=1.0, slots_per_day=24)
    assert len(hourly["schedule"]) == 24
