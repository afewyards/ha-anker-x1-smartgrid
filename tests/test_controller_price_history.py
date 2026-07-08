"""Plan B (B1 wiring): Controller snapshots yesterday's realized prices once per
local-day rollover, idempotently."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.anker_x1_smartgrid import controller as ctrl
from custom_components.anker_x1_smartgrid.models import PriceSlot


class _RecordingPriceStore:
    def __init__(self):
        self.history = {}
        self.calls = []
    async def async_snapshot(self, date_iso, hourly):
        self.calls.append((date_iso, dict(hourly)))
        self.history[date_iso] = hourly


def _bare_controller(price_store):
    """A Controller instance without running __init__ I/O — only the fields the
    snapshot path reads.  (Mirrors how other controller unit tests stub state.)"""
    c = ctrl.Controller.__new__(ctrl.Controller)
    c._price_store = price_store
    c._price_history_day = None
    return c


@pytest.mark.asyncio
async def test_snapshot_fires_once_per_day_and_extracts_yesterday():
    ps = _RecordingPriceStore()
    c = _bare_controller(ps)
    now = datetime(2026, 6, 26, 0, 30, tzinfo=timezone.utc)   # just after local midnight (UTC CI)
    yday = datetime(2026, 6, 25, 0, 0, tzinfo=timezone.utc)
    slots = [PriceSlot(yday + timedelta(hours=i), 0.10 + 0.01 * i) for i in range(24)]

    from unittest.mock import patch
    with patch("homeassistant.util.dt.as_local", side_effect=lambda d: d):
        await c._snapshot_prices_on_rollover(now, slots)
        await c._snapshot_prices_on_rollover(now, slots)   # same day -> no second write

    assert len(ps.calls) == 1
    date_iso, hourly = ps.calls[0]
    assert date_iso == "2026-06-25"
    assert len(hourly) == 24 and hourly["0"] == pytest.approx(0.10)

    # Next local day -> a new snapshot fires.
    with patch("homeassistant.util.dt.as_local", side_effect=lambda d: d):
        await c._snapshot_prices_on_rollover(now + timedelta(days=1), slots)
    assert len(ps.calls) == 2


@pytest.mark.asyncio
async def test_snapshot_noop_without_price_store():
    c = _bare_controller(None)
    # Must not raise when price_store is absent.
    await c._snapshot_prices_on_rollover(
        datetime(2026, 6, 26, 0, 30, tzinfo=timezone.utc), []
    )
