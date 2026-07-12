"""BC1 (before-cutover): DP reserve floor must be built on the SLOT grid.

Bug: ``compute_decision``'s ``_reserve_list`` was built at HOURLY stride
(``now_h + timedelta(hours=i)``) but ``_dp_select_slots`` consumes it
POSITIONALLY, one entry per DP SLOT (padded to ``window_len`` with the
firmware floor). At ``slot_minutes=60`` hours == slots so entry ``i`` happens
to land on slot ``i`` — byte-identical. At ``slot_minutes=15`` the list only
has ~window_len/4 entries at hourly stride: entry ``i`` lands on DP slot
``i`` (``i`` quarter-hours after now), not on the hour it was meant for, and
everything past the list's short length collapses to the bare firmware
floor — under-setting the export discharge floor for most of the afternoon.

This test drives the REAL ``compute_decision`` path at ``slot_minutes=15``
with ``_build_reserve_by_hour`` patched to return exactly one distinct
(non-floor) reserve value at 18:00, and ``optimize.optimize_grid`` patched to
record the ``reserve_by_hour`` list it actually receives. It asserts that ALL
FOUR 15-minute slots covering 18:00-19:00 carry that hour's reserve — not the
firmware floor. Under the pre-fix hourly-strided list this fails (proven
below by running against the unpatched pre-fix code and observing the
failure — see the task report for the stash/restore transcript).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from custom_components.anker_x1_smartgrid.controller import compute_decision
from custom_components.anker_x1_smartgrid.forecast import LoadPredictor
from custom_components.anker_x1_smartgrid.models import (
    Config,
    ControllerState,
    PlanState,
    PlantInputs,
    PriceSlot,
)

BASE = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)  # midnight, hour-aligned
_PREDICTOR = LoadPredictor.from_profile({})

HOUR18 = BASE + timedelta(hours=18)
_DISTINCT_RESERVE_KWH = 5.0   # far above the firmware floor (0.5 kWh below)
_FLOOR_KWH = 0.5              # soc_floor=5% x capacity_kwh=10.0


def _cfg() -> Config:
    return Config(
        capacity_kwh=10.0,
        soc_floor=5.0,
        soc_target=97.0,
        eta_charge=1.0,
        round_trip_eff=1.0,
        max_charge_w=3000.0,
    )


def _slots(hours: int = 30) -> list[PriceSlot]:
    return [PriceSlot(BASE + timedelta(hours=i), 0.20) for i in range(hours)]


def _make_reserve_capture():
    """optimize_grid side-effect that records the reserve_by_hour list it receives."""
    captured: dict = {}

    def _side_effect(*args, **kwargs):
        wl = kwargs.get("window_len", len(args[0]) if args else 1)
        captured["reserve_by_hour"] = kwargs.get("reserve_by_hour")
        return {
            "schedule": [0.0] * wl,
            "export_schedule": [0.0] * wl,
            "kwh": 0.0,
            "eur": 0.0,
        }

    return captured, _side_effect


def _run(slot_minutes: int) -> dict:
    """Drive the real compute_decision path and return the captured reserve list."""
    captured, side_effect = _make_reserve_capture()
    cfg = _cfg()
    plan = PlanState(ControllerState.PASSIVE, BASE - timedelta(hours=2), ())
    inputs = PlantInputs(soc=50.0, meter_w=0.0, now=BASE)
    sunset = BASE + timedelta(hours=8)
    slots = _slots(30)

    with patch(
        "custom_components.anker_x1_smartgrid.decision._build_reserve_by_hour",
        return_value={HOUR18: _DISTINCT_RESERVE_KWH},
    ), patch(
        "custom_components.anker_x1_smartgrid.optimize.optimize_grid",
        side_effect=side_effect,
    ):
        compute_decision(
            plan, inputs, slots, 0.0, sunset,
            _PREDICTOR, None, cfg,
            slot_minutes=slot_minutes,
        )
    return captured


def test_afternoon_quarter_slots_get_hours_reserve_not_floor_at_15min():
    """At slot_minutes=15, the 4 slots spanning 18:00-19:00 must all carry
    the 18:00 hour's reserve (5.0 kWh) -- not the firmware floor (0.5 kWh).

    now_h at slot_minutes=15 for BASE (already hour-aligned) == BASE, so
    18:00 = BASE + 18h lands at DP slot index 72 (18h / 15min).
    """
    captured = _run(15)
    rbh = captured["reserve_by_hour"]
    assert rbh is not None, "reserve_by_hour must not be dropped to None"

    idx_1800 = 72
    for q in range(4):  # the 4 quarter-hour slots covering 18:00-19:00
        idx = idx_1800 + q
        assert idx < len(rbh), f"reserve list too short to cover slot {idx}"
        assert rbh[idx] == pytest.approx(_DISTINCT_RESERVE_KWH), (
            f"slot {idx} (18:{q * 15:02d}) must carry the 18:00 hour's reserve "
            f"({_DISTINCT_RESERVE_KWH} kWh); got {rbh[idx]}. "
            "BC1: the hourly-strided reserve list was mis-indexed onto the "
            "15-min slot grid and/or padded with the bare firmware floor."
        )

    # Sanity: an hour NOT patched with a distinct value falls back to the
    # firmware floor (proves the assertion above isn't vacuously true because
    # everything defaults to _DISTINCT_RESERVE_KWH).
    idx_0000 = 0
    assert rbh[idx_0000] == pytest.approx(_FLOOR_KWH)


def test_60min_reserve_list_stays_byte_identical():
    """Parity guard: at slot_minutes=60 the fix must reduce to the legacy
    hourly-stride list exactly -- hour 18 holds the distinct reserve, its
    neighbors hold the firmware floor."""
    captured = _run(60)
    rbh = captured["reserve_by_hour"]
    assert rbh is not None
    idx_1800 = 18
    assert rbh[idx_1800] == pytest.approx(_DISTINCT_RESERVE_KWH)
    assert rbh[idx_1800 - 1] == pytest.approx(_FLOOR_KWH)
    assert rbh[idx_1800 + 1] == pytest.approx(_FLOOR_KWH)
