from datetime import datetime, timedelta, timezone, UTC

from custom_components.anker_x1_smartgrid.models import Config, ForecastInterval, PriceSlot
from custom_components.anker_x1_smartgrid.plan import build_plan_horizon


def test_is_past_horizon_key_marks_slots_past_the_edge():
    now = datetime(2026, 6, 23, 18, 0, tzinfo=UTC)
    slots = [PriceSlot(now + timedelta(hours=i), 0.20) for i in range(6)]
    ivs = [ForecastInterval(now + timedelta(hours=i), 0.0, 300.0, 1.0) for i in range(6)]
    edge = now + timedelta(hours=3)
    horizon = build_plan_horizon(slots, ivs, [], 50.0, edge, Config())
    assert all("is_past_horizon" in e for e in horizon)
    assert all("is_past_deadline" not in e for e in horizon)
    past = [e for e in horizon if e["is_past_horizon"]]
    assert len(past) == 3  # hours 3,4,5 are at/after the edge
