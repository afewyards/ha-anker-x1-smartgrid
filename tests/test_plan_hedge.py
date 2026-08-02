"""Tests for the optional hedge_drain_by_hour debit in build_plan_horizon.

Task 4 — TDD: write tests first, then implement in plan.py.
"""

from datetime import datetime, timedelta, timezone, UTC

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


def _q(n, q=0):
    """Quarter-hour datetime: _q(10, 2) == 10:30."""
    return datetime(2026, 6, 29, n, tzinfo=UTC) + timedelta(minutes=15 * q)


def _inputs_15():
    starts = [_q(10, q) for q in range(8)]  # 10:00 .. 11:45
    slots = [PriceSlot(s, 0.20) for s in starts]
    ivs = [ForecastInterval(s, 0.0, 0.0, 0.25) for s in starts]
    return slots, ivs


def test_hedge_is_debited_once_per_hour_at_quarter_hour_resolution():
    """The hedge is a ONE-SHOT kWh debit on a single clock-hour, not a per-hour rate.

    ``controller._apply_drift_hedge`` emits ``{trough_hour: hedge_kwh}`` and the DP
    (``decision.py``) looks it up on slot-stride keys, so it lands on exactly one
    slot.  The published horizon must match: an hour-keyed lookup would apply the
    full debit to all four quarters of that hour (4x over-debit).
    """
    slots, ivs = _inputs_15()
    cfg = make_cfg()  # capacity 10 kWh → 1 kWh = 10%
    base = build_plan_horizon(slots, ivs, [], 80.0, _q(12), cfg, slot_minutes=15)
    hed = build_plan_horizon(slots, ivs, [], 80.0, _q(12), cfg, hedge_drain_by_hour={_q(10): 1.0}, slot_minutes=15)
    assert hed[-1]["soc"] == round(base[-1]["soc"] - 10.0, 1)
