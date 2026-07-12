"""C4: Rip-cord regression lock — verify water-value planner boundary."""

from datetime import datetime, timedelta, timezone, UTC

from custom_components.anker_x1_smartgrid import controller as ctrl
from custom_components.anker_x1_smartgrid.models import (
    Config,
    PlanState,
    PlantInputs,
    PriceSlot,
)


class _FlatPredictor:
    def predict(self, start, temp, fallback, quantile=0.5):
        return 300.0


def _slots(now, prices):
    base = now.replace(minute=0, second=0, microsecond=0)
    return [PriceSlot(base + timedelta(hours=i), p) for i, p in enumerate(prices)]


def test_new_flag_uses_full_horizon_as_edge():
    """Water-value mode: horizon_edge = last_slot_start + 1h (full forecast, not trough+1h).

    Re-baselined from the old ``trough+1h`` assertion: the DP now optimizes
    over the entire forecast window so that export peaks beyond the trough are
    in-scope.  ``find_next_trough`` is still called for the water-value
    terminal but no longer determines the window boundary.
    """
    now = datetime(2026, 6, 23, 18, 0, tzinfo=UTC)
    prices = [0.30] * 8 + [0.08] + [0.30] * 6
    slots = _slots(now, prices)
    cfg = Config()
    _, _, returned_edge, _, hm, _ = ctrl.compute_decision(
        plan=PlanState.initial(now),
        inputs=PlantInputs(soc=50.0, meter_w=0.0, now=now),
        slots=slots,
        pv_remaining=0.0,
        sunset=now + timedelta(hours=2),
        predictor=_FlatPredictor(),
        cur_temp=10.0,
        cfg=cfg,
    )
    # New behaviour: horizon_edge = last_slot + 1h (full forecast end).
    # With 15 slots (indices 0–14), last_slot = now+14h; edge = now+15h.
    last_slot_start = max(s.start for s in slots).replace(minute=0, second=0, microsecond=0)
    assert returned_edge == last_slot_start + timedelta(hours=1)
    assert hm == "water-value"
