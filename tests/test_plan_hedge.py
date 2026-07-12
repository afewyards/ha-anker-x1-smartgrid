"""Tests for the optional hedge_drain_by_hour debit in build_plan_horizon.

Task 4 — TDD: write tests first, then implement in plan.py.
"""

from datetime import datetime, timezone, UTC

from custom_components.anker_x1_smartgrid.plan import build_plan_horizon
from custom_components.anker_x1_smartgrid.models import PriceSlot, ForecastInterval
from tests.helpers import make_config as make_cfg

UTC = UTC


def _h(n):
    return datetime(2026, 6, 29, n, tzinfo=UTC)


def _inputs():
    slots = [PriceSlot(_h(n), 0.20) for n in range(10, 14)]
    ivs = [ForecastInterval(_h(n), 0.0, 0.0, 1.0) for n in range(10, 14)]
    return slots, ivs


def test_none_is_noop():
    slots, ivs = _inputs()
    base = build_plan_horizon(slots, ivs, [], 80.0, _h(14), make_cfg())
    hed = build_plan_horizon(slots, ivs, [], 80.0, _h(14), make_cfg(), hedge_drain_by_hour=None)
    assert [r["soc"] for r in base] == [r["soc"] for r in hed]


def test_hedge_lowers_published_soc_from_first_forward_hour():
    slots, ivs = _inputs()
    cfg = make_cfg()  # capacity 10 kWh → 1 kWh = 10%
    base = build_plan_horizon(slots, ivs, [], 80.0, _h(14), cfg)
    hed = build_plan_horizon(slots, ivs, [], 80.0, _h(14), cfg, hedge_drain_by_hour={_h(10): 1.0})
    assert hed[0]["soc"] == round(base[0]["soc"] - 10.0, 1)
    assert hed[-1]["soc"] == round(base[-1]["soc"] - 10.0, 1)
