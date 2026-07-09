"""N1 discriminating test: reserve_by_hour short by one must be PADDED, not silently dropped.

Root cause: ``_dp_select_slots`` CEILS the deadline to build ``window_len``, but
``compute_decision``'s ``_win_len`` FLOORs the same deadline — producing a reserve list
one element shorter than ``window_len`` when the deadline has a non-zero minute component
(e.g. the legacy path where sunset−buffer lands at 14:30, not 15:00).

Pre-fix guard (line 195-199, controller.py)::

    reserve_by_hour=(
        reserve_by_hour[:window_len]
        if reserve_by_hour is not None and len(reserve_by_hour) >= window_len
        else None          # ← silently drops the floor when list is 1 short!
    ),

Post-fix: pad with the firmware floor value (``cfg.soc_floor / 100 * capacity_kwh``)
instead of dropping to ``None``, so the per-hour export floor is always enforced.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from custom_components.anker_x1_smartgrid import controller as ctrl
from custom_components.anker_x1_smartgrid.models import (
    Config,
    ForecastInterval,
    PlantInputs,
    PriceSlot,
)

# now=10:00, deadline=14:30:
#   • compute_decision _win_len  = floor(14:00 − 10:00) = 4 → _reserve_list has 4 elements
#   • _dp_select_slots window_len = ceil(14:30 → 15:00) − 10:00 = 5
# → len(4) < window_len(5) → pre-fix drops to None
NOW = datetime(2026, 6, 26, 10, 0, tzinfo=timezone.utc)
DEADLINE = datetime(2026, 6, 26, 14, 30, tzinfo=timezone.utc)


def _cfg(**kw) -> Config:
    return Config(**{"capacity_kwh": 10.0, "soc_floor": 10.0, "eta_charge": 1.0, **kw})


def _spy_factory(captured: dict):
    """Return an optimize_grid side-effect that records reserve_by_hour and returns
    a minimal valid response (zero-charge schedule of the correct length).
    """
    def _side_effect(*args, **kwargs):
        captured["reserve_by_hour"] = kwargs.get("reserve_by_hour")
        wl = kwargs.get("window_len", len(args[0]) if args else 1)
        return {"schedule": [0.0] * wl, "kwh": 0.0, "eur": 0.0}
    return _side_effect


def test_n1_short_reserve_list_is_padded_not_dropped():
    """Discriminating test: reserve_by_hour len=4 with window_len=5 must be padded, not None.

    Before fix → optimize_grid receives ``reserve_by_hour=None``  (FAILS assertion).
    After fix  → optimize_grid receives a 5-element list padded with ``floor_kwh`` (PASSES).
    """
    cfg = _cfg()
    floor_kwh = cfg.soc_floor / 100.0 * cfg.capacity_kwh  # 10% × 10 kWh = 1.0 kWh

    # 4-element list — exactly 1 shorter than window_len=5 (the mismatch compute_decision
    # produces when _win_len uses floor logic and _dp_select_slots uses ceil logic).
    reserve_4 = [3.0, 2.5, 2.0, floor_kwh]

    captured: dict = {}
    spy = _spy_factory(captured)

    inputs = PlantInputs(soc=50.0, meter_w=0.0, now=NOW)
    # 6 price slots spanning [10:00, 15:00] — window_len=5 uses hours 10..14
    slots = [PriceSlot(NOW + timedelta(hours=i), 0.20) for i in range(6)]
    # 6 intervals with zero PV and 500 W load
    intervals = [
        ForecastInterval(NOW + timedelta(hours=i), 0.0, 500.0, 1.0) for i in range(6)
    ]

    with patch("custom_components.anker_x1_smartgrid.optimize.optimize_grid", side_effect=spy):
        ctrl._dp_select_slots(
            inputs=inputs,
            slots=slots,
            deadline=DEADLINE,           # 14:30 → ceil → window_len=5
            ceiling=0.30,
            cfg=cfg,
            export_price=None,
            reserve_by_hour=reserve_4,   # 4 elements → was silently dropped
            intervals=intervals,
        )

    rbh = captured.get("reserve_by_hour")

    # ── After fix ──────────────────────────────────────────────────────────────
    assert rbh is not None, (
        "BUG N1: reserve_by_hour was dropped to None because len(4) < window_len(5). "
        "Must be PADDED to window_len instead of silently reverting to the firmware floor."
    )
    assert len(rbh) == 5, (
        f"Expected 5-element list (window_len) after padding; got len={len(rbh)}"
    )
    # The original 4 elements must be preserved unchanged
    assert list(rbh[:4]) == reserve_4, (
        f"First 4 elements must be preserved; got {list(rbh[:4])}"
    )
    # The 5th (padded) element must be exactly the firmware floor
    assert rbh[4] == pytest.approx(floor_kwh), (
        f"Padded element must equal firmware floor {floor_kwh} kWh; got {rbh[4]}"
    )


def test_n1_exact_length_reserve_list_is_not_changed():
    """Sanity: when reserve list already has exactly window_len elements, no change."""
    cfg = _cfg()
    floor_kwh = cfg.soc_floor / 100.0 * cfg.capacity_kwh

    # window_len = 5 (DEADLINE = 14:30 → ceil → 15:00 − 10:00 = 5)
    reserve_5 = [3.0, 2.5, 2.0, 1.5, floor_kwh]

    captured: dict = {}
    spy = _spy_factory(captured)

    inputs = PlantInputs(soc=50.0, meter_w=0.0, now=NOW)
    slots = [PriceSlot(NOW + timedelta(hours=i), 0.20) for i in range(6)]
    intervals = [
        ForecastInterval(NOW + timedelta(hours=i), 0.0, 500.0, 1.0) for i in range(6)
    ]

    with patch("custom_components.anker_x1_smartgrid.optimize.optimize_grid", side_effect=spy):
        ctrl._dp_select_slots(
            inputs=inputs,
            slots=slots,
            deadline=DEADLINE,
            ceiling=0.30,
            cfg=cfg,
            export_price=None,
            reserve_by_hour=reserve_5,   # exactly window_len elements → no padding needed
            intervals=intervals,
        )

    rbh = captured.get("reserve_by_hour")
    assert rbh is not None
    assert len(rbh) == 5
    assert list(rbh) == reserve_5


def test_n1_none_reserve_stays_none():
    """Sanity: reserve_by_hour=None must pass through as None (no reserve configured)."""
    captured: dict = {}
    spy = _spy_factory(captured)

    cfg = _cfg()
    inputs = PlantInputs(soc=50.0, meter_w=0.0, now=NOW)
    slots = [PriceSlot(NOW + timedelta(hours=i), 0.20) for i in range(6)]
    intervals = [
        ForecastInterval(NOW + timedelta(hours=i), 0.0, 500.0, 1.0) for i in range(6)
    ]

    with patch("custom_components.anker_x1_smartgrid.optimize.optimize_grid", side_effect=spy):
        ctrl._dp_select_slots(
            inputs=inputs,
            slots=slots,
            deadline=DEADLINE,
            ceiling=0.30,
            cfg=cfg,
            export_price=None,
            reserve_by_hour=None,   # no reserve → must stay None
            intervals=intervals,
        )

    assert captured.get("reserve_by_hour") is None
