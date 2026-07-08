"""Controller._resolve_slot_minutes: the T3 latch method wired into tick() by T4.

T3 added ``Controller._resolve_slot_minutes`` but nothing called it, so the
latch + ``_detected_slot_minutes`` diagnostic were dead code (always read the
``__init__`` default of 60).  These tests exercise the method directly:
override bypass, same-day latch-to-finest, and UTC-day rollover reset.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from custom_components.anker_x1_smartgrid import const
from custom_components.anker_x1_smartgrid import controller as ctrl
from custom_components.anker_x1_smartgrid.models import Config, PriceSlot

UTC = timezone.utc


def _bare_controller(slot_resolution: str) -> ctrl.Controller:
    """A Controller instance without running __init__ I/O — only the fields
    _resolve_slot_minutes reads/writes.  (Mirrors the pattern used by
    test_controller_price_history.py / test_controller_phase2.py.)"""
    c = ctrl.Controller.__new__(ctrl.Controller)
    c.cfg = Config(slot_resolution=slot_resolution)
    c._res_latch = None
    c._detected_slot_minutes = 60
    return c


def _slots_at(start: datetime, minutes: int, n: int) -> list[PriceSlot]:
    return [PriceSlot(start + timedelta(minutes=minutes * i), 0.10) for i in range(n)]


def test_explicit_override_bypasses_latch_every_call():
    """An explicit '30' override always wins, regardless of detected slots,
    and never touches the latch state."""
    c = _bare_controller("30")
    now = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
    with patch.object(ctrl.dt_util, "utcnow", return_value=now):
        fine = _slots_at(now, 15, 4)
        coarse = _slots_at(now, 60, 4)
        assert c._resolve_slot_minutes(fine) == 30
        assert c._res_latch is None  # latch untouched on override path
        assert c._resolve_slot_minutes(coarse) == 30
        assert c._res_latch is None
        assert c._detected_slot_minutes == 30


def test_auto_latches_finest_and_does_not_unlatch_on_coarser_read():
    """'auto' latches the finest slot length seen this UTC day; a later,
    coarser detection within the same day must not widen it back out."""
    c = _bare_controller(const.SLOT_RESOLUTION_AUTO)
    now = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
    with patch.object(ctrl.dt_util, "utcnow", return_value=now):
        fine = _slots_at(now, 15, 4)
        effective_fine = c._resolve_slot_minutes(fine)
        assert effective_fine == 15
        assert c._detected_slot_minutes == 15

        later = now + timedelta(hours=2)
    with patch.object(ctrl.dt_util, "utcnow", return_value=later):
        coarse = _slots_at(later, 60, 4)
        effective_coarse = c._resolve_slot_minutes(coarse)
        # Detected-this-refresh is 60, but the day's latch stays at 15.
        assert effective_coarse == 15
        assert c._detected_slot_minutes == 15


def test_day_rollover_resets_the_latch():
    """A UTC-day rollover clears the previous day's latch so a coarser
    detection on the new day is honoured (not stuck at yesterday's finest)."""
    c = _bare_controller(const.SLOT_RESOLUTION_AUTO)
    day1 = datetime(2026, 8, 1, 23, 0, tzinfo=UTC)
    with patch.object(ctrl.dt_util, "utcnow", return_value=day1):
        fine = _slots_at(day1, 15, 4)
        assert c._resolve_slot_minutes(fine) == 15

    day2 = datetime(2026, 8, 2, 0, 30, tzinfo=UTC)
    with patch.object(ctrl.dt_util, "utcnow", return_value=day2):
        coarse = _slots_at(day2, 60, 4)
        effective = c._resolve_slot_minutes(coarse)
        assert effective == 60
        assert c._detected_slot_minutes == 60
        assert c._res_latch == (60, day2.date())
