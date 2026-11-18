"""Plan B pricing_store: B1 realized-price ring buffer + extract."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, UTC

import statistics

import pytest

from custom_components.anker_x1_smartgrid import pricing_store
from custom_components.anker_x1_smartgrid.models import PriceSlot


class _FakeStore:
    """Duck-typed HA Store: in-memory async load/save (no .storage I/O)."""

    def __init__(self, initial=None):
        self.saved = initial

    async def async_load(self):
        return self.saved

    async def async_save(self, data):
        self.saved = data


def test_prune_history_keeps_most_recent_days():
    hist = {f"2026-06-{d:02d}": {"0": 0.2} for d in range(1, 13)}  # 12 days
    pruned = pricing_store.prune_history(hist, max_days=8)
    assert len(pruned) == 8
    assert min(pruned) == "2026-06-05"  # oldest 4 dropped
    assert max(pruned) == "2026-06-12"


def test_extract_realized_day_buckets_by_local_hour():
    # Patch as_local to identity so the test is timezone-independent.
    # (pytest_homeassistant_custom_component may set DEFAULT_TIME_ZONE to the
    # system local TZ via its autouse hass fixture; we must NOT mutate
    # DEFAULT_TIME_ZONE here — that interferes with verify_cleanup teardown.)
    from unittest.mock import patch

    day = datetime(2026, 6, 25, 0, 0, tzinfo=UTC)
    slots = [PriceSlot(day + timedelta(hours=i), 0.10 + 0.01 * i) for i in range(26)]
    with patch("homeassistant.util.dt.as_local", side_effect=lambda d: d):
        got = pricing_store.extract_realized_day(slots, day.date())
    assert len(got) == 24
    assert got["0"] == pytest.approx(0.10)
    assert got["23"] == pytest.approx(0.10 + 0.23)
    assert "24" not in got  # hour 0 of the NEXT day is a different date, excluded


def test_extract_realized_day_means_quarter_hour_slots():
    from unittest.mock import patch

    day = datetime(2026, 6, 25, 0, 0, tzinfo=UTC)
    slots = [PriceSlot(day + timedelta(minutes=15 * i), 0.10 + 0.01 * (i % 4)) for i in range(96)]
    with patch("homeassistant.util.dt.as_local", side_effect=lambda d: d):
        got = pricing_store.extract_realized_day(slots, day.date())
    assert len(got) == 24
    assert got["7"] == pytest.approx(0.115)


@pytest.mark.asyncio
async def test_store_snapshot_is_date_keyed_idempotent_and_pruned():
    store = pricing_store.PriceHistoryStore(_FakeStore(), max_days=2)
    await store.async_load()
    await store.async_snapshot("2026-06-24", {"0": 0.20})
    await store.async_snapshot("2026-06-25", {"0": 0.21})
    await store.async_snapshot("2026-06-26", {"0": 0.22})
    # Ring-pruned to 2 most-recent dates.
    assert set(store.history) == {"2026-06-25", "2026-06-26"}
    # Re-snapshotting with equal-or-fewer keys keeps the existing entry (completeness guard).
    await store.async_snapshot("2026-06-26", {"0": 0.99})
    assert set(store.history) == {"2026-06-25", "2026-06-26"}
    assert store.history["2026-06-26"] == {"0": 0.22}  # original preserved
    # A MORE-COMPLETE snapshot (extra keys) does update the entry.
    await store.async_snapshot("2026-06-26", {"0": 0.99, "1": 0.98})
    assert store.history["2026-06-26"] == {"0": 0.99, "1": 0.98}


@pytest.mark.asyncio
async def test_store_round_trips_through_underlying_store():
    backing = _FakeStore()
    s1 = pricing_store.PriceHistoryStore(backing, max_days=8)
    await s1.async_snapshot("2026-06-26", {"0": 0.30})
    # A fresh wrapper over the same backing store reloads the saved history.
    s2 = pricing_store.PriceHistoryStore(backing, max_days=8)
    await s2.async_load()
    assert s2.history == {"2026-06-26": {"0": 0.30}}


@pytest.mark.asyncio
async def test_snapshot_completeness_guard_preserves_full_day():
    """N1: a partial re-snapshot (e.g. mid-day restart) must not clobber a full stored day."""
    store = pricing_store.PriceHistoryStore(_FakeStore(), max_days=8)
    await store.async_load()
    full_day = {str(h): 0.20 + 0.001 * h for h in range(24)}  # 24 keys
    await store.async_snapshot("2026-06-25", full_day)
    # Simulate a partial re-snapshot (Zonneplan back-horizon only 12 h after restart).
    partial = {str(h): 0.25 for h in range(12)}  # only 12 keys
    await store.async_snapshot("2026-06-25", partial)
    # Full day must be preserved — partial must NOT win.
    assert len(store.history["2026-06-25"]) == 24
    assert store.history["2026-06-25"]["0"] == pytest.approx(0.20)
    assert store.history["2026-06-25"]["23"] == pytest.approx(0.20 + 0.023)


# ── B2: blended tomorrow estimate ─────────────────────────────────────────────

from datetime import date as _date, UTC


def _full_day(base: float) -> dict[str, float]:
    return {str(h): base + 0.001 * h for h in range(24)}


def test_blend_today_and_same_weekday():
    tomorrow = _date(2026, 6, 27)  # Saturday
    history = {
        "2026-06-26": _full_day(0.30),  # today
        "2026-06-20": _full_day(0.10),  # same weekday last week (Sat - 7d)
    }
    est = pricing_store.blend_price_prior(history, tomorrow, weight_today=0.5)
    assert est is not None and len(est) == 24
    # 0.5*0.30 + 0.5*0.10 = 0.20 at hour 0.
    assert est[0] == pytest.approx(0.20)


def test_blend_same_weekday_missing_falls_back_to_today_only():
    tomorrow = _date(2026, 6, 27)
    history = {"2026-06-26": _full_day(0.30)}  # no same-weekday day
    est = pricing_store.blend_price_prior(history, tomorrow, weight_today=0.5)
    assert est is not None
    assert est[0] == pytest.approx(0.30)  # w forced to 1.0


def test_blend_today_incomplete_uses_most_recent_full_day():
    tomorrow = _date(2026, 6, 27)
    history = {
        "2026-06-26": {"0": 0.30, "1": 0.31},  # today: only 2 hours -> incomplete
        "2026-06-24": _full_day(0.40),  # most recent FULL day -> the 'today' term
    }
    est = pricing_store.blend_price_prior(history, tomorrow, weight_today=0.5)
    assert est is not None
    assert est[0] == pytest.approx(0.40)  # full-day fallback, today-only (no same-weekday)


def test_blend_returns_none_without_any_full_day():
    est = pricing_store.blend_price_prior({"2026-06-26": {"0": 0.30}}, _date(2026, 6, 27), weight_today=0.5)
    assert est is None


# ── B3: estimated slots bounded to the pre-solar window ───────────────────────


def test_build_estimated_slots_bounds_to_presolar_window():
    est = [0.10 + 0.01 * h for h in range(24)]  # local-hour-indexed
    real_end = datetime(2026, 6, 27, 0, 0, tzinfo=UTC)  # tonight ends 00:00
    pickup = datetime(2026, 6, 27, 8, 0, tzinfo=UTC)  # winter-late pickup 08:00
    # Patch as_local to identity (UTC) so slot hours index by UTC hour.
    from unittest.mock import patch

    with patch("homeassistant.util.dt.as_local", side_effect=lambda d: d):
        slots = pricing_store.build_estimated_slots(est, real_end, pickup)
    # Exactly the 8 pre-solar hours [00:00, 08:00) carry the estimate (by local hour).
    assert [s.start for s in slots] == [real_end + timedelta(hours=i) for i in range(8)]
    assert slots[0].price == pytest.approx(est[0])
    assert slots[7].price == pytest.approx(est[7])


def test_build_estimated_slots_never_repeats_the_estimated_day():
    """A sunless multi-day gap gets one estimated day, not the same day five times."""
    real_end = datetime(2026, 11, 15, 23, 0, tzinfo=UTC)
    pickup = real_end + timedelta(hours=132)
    slots = pricing_store.build_estimated_slots([0.30] * 24, real_end, pickup)
    assert [s.start for s in slots] == [real_end + timedelta(hours=i) for i in range(24)]


def test_build_estimated_slots_empty_post_publication():
    est = [0.20] * 24
    # Real prices already extend PAST tomorrow's pickup -> nothing to estimate.
    real_end = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)
    pickup = datetime(2026, 6, 27, 8, 0, tzinfo=UTC)
    assert pricing_store.build_estimated_slots(est, real_end, pickup) == []


def test_build_estimated_slots_empty_without_pickup_or_estimate():
    real_end = datetime(2026, 6, 27, 0, 0, tzinfo=UTC)
    pickup = datetime(2026, 6, 27, 8, 0, tzinfo=UTC)
    assert pricing_store.build_estimated_slots(None, real_end, pickup) == []
    assert pricing_store.build_estimated_slots([0.2] * 24, real_end, None) == []


def test_estimated_slots_are_disjoint_from_real_slots():
    """Containment: estimated PriceSlots never coincide with the real horizon."""
    est = [0.20] * 24
    real_end = datetime(2026, 6, 27, 0, 0, tzinfo=UTC)
    pickup = datetime(2026, 6, 27, 8, 0, tzinfo=UTC)
    real_slots = [
        PriceSlot(datetime(2026, 6, 26, 18, 0, tzinfo=UTC) + timedelta(hours=i), 0.30) for i in range(6)
    ]  # 18:00..23:00 tonight
    from unittest.mock import patch

    with patch("homeassistant.util.dt.as_local", side_effect=lambda d: d):
        est_slots = pricing_store.build_estimated_slots(est, real_end, pickup)
    real_starts = {s.start for s in real_slots}
    assert all(s.start >= real_end for s in est_slots)  # strictly after tonight
    assert all(s.start not in real_starts for s in est_slots)


def test_blend_uses_median_so_one_spike_day_is_ignored():
    tomorrow = _date(2026, 11, 16)
    history = {f"2026-11-{d:02d}": _full_day(0.40 + 0.01 * (d % 3)) for d in range(8, 16)}
    history["2026-11-09"] = _full_day(0.88)  # spike, exactly 7 days before target
    est = pricing_store.blend_price_prior(history, tomorrow, weight_today=0.5)
    today = history["2026-11-15"]
    for h in range(24):
        med = statistics.median(d[str(h)] for d in history.values())
        assert est[h] == pytest.approx(0.5 * today[str(h)] + 0.5 * med)
    assert max(est) < 0.5 * today["23"] + 0.5 * 0.88 - 0.1


def _estimate(history, slots, tomorrow):
    from unittest.mock import patch

    with patch("homeassistant.util.dt.as_local", side_effect=lambda d: d):
        return pricing_store.estimate_day_after_horizon(history, slots, tomorrow, weight_today=0.5)


def test_estimate_uses_published_tomorrow_as_base_day():
    tomorrow = _date(2026, 11, 16)
    start = datetime(2026, 11, 16, tzinfo=UTC)
    slots = [PriceSlot(start + timedelta(hours=h), 0.26) for h in range(24)]
    history = {"2026-11-15": _full_day(0.38), "2026-11-14": _full_day(0.38), "2026-11-13": _full_day(0.38)}
    est = _estimate(history, slots, tomorrow)
    # median over 4 full days (0.38 x3, 0.26) = 0.38; 0.5*0.26 + 0.5*0.38
    assert est[0] == pytest.approx(0.32)
    assert "2026-11-16" not in history


def test_estimate_unchanged_when_tomorrow_incomplete():
    tomorrow = _date(2026, 11, 16)
    start = datetime(2026, 11, 16, tzinfo=UTC)
    slots = [PriceSlot(start + timedelta(hours=h), 0.26) for h in range(10)]
    history = {"2026-11-15": _full_day(0.38)}
    assert _estimate(history, slots, tomorrow) == pricing_store.blend_price_prior(history, tomorrow, weight_today=0.5)


def test_estimate_uses_today_from_slots_when_tomorrow_unpublished():
    tomorrow = _date(2026, 11, 16)
    start = datetime(2026, 11, 15, tzinfo=UTC)
    slots = [PriceSlot(start + timedelta(hours=h), 0.50 + 0.001 * h) for h in range(24)]
    history = {"2026-11-14": _full_day(0.30)}
    est = _estimate(history, slots, tomorrow)
    # one stored day plus today: same weekday absent, so the estimate is today's prices
    assert est == [pytest.approx(0.50 + 0.001 * h) for h in range(24)]
    assert "2026-11-15" not in history


def test_estimate_ignores_partial_today_in_slots():
    tomorrow = _date(2026, 11, 16)
    start = datetime(2026, 11, 15, tzinfo=UTC)
    slots = [PriceSlot(start + timedelta(hours=h), 0.50) for h in range(10)]
    history = {"2026-11-14": _full_day(0.30)}
    assert _estimate(history, slots, tomorrow) == pricing_store.blend_price_prior(history, tomorrow, weight_today=0.5)
