"""Task 3 tests: compute_decision threads past_actuals into horizon;
_get_past_actuals caches per clock-hour and filters to hours < now_h."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, UTC

import pytest

from custom_components.anker_x1_smartgrid import controller as ctrl
from custom_components.anker_x1_smartgrid.models import (
    Config,
    PlantInputs,
    PlanState,
    PriceSlot,
)
from tests.helpers import StubHass as _Hass


def _slot(h):
    return PriceSlot(start=datetime(2026, 6, 29, h, tzinfo=UTC), price=0.20)


def test_compute_decision_threads_past_actuals_into_horizon():
    """past_actuals_by_hour is plumbed into build_plan_horizon (sun_times=None path)."""
    cfg = Config()
    now = datetime(2026, 6, 29, 10, tzinfo=UTC)
    inputs = PlantInputs(soc=50.0, meter_w=0.0, now=now)
    slots = [_slot(8), _slot(9), _slot(10), _slot(11)]
    past = {
        datetime(2026, 6, 29, 8, tzinfo=UTC): {
            "pv_w": 800.0,
            "load_w": 250.0,
            "soc": 30.0,
            "solar_charge_w": 400.0,
            "grid_charge_w": 0.0,
            "grid_export_w": 0.0,
        }
    }

    class _Pred:
        def predict(self, *a, **k):
            return 300.0

    # sun_times=None → build_plan_horizon path (not build_display_horizon)
    _plan, _sp, _edge, horizon, _hm, _ivr = ctrl.compute_decision(
        PlanState.initial(now),
        inputs,
        slots,
        0.0,
        now,
        _Pred(),
        15.0,
        cfg,
        sun_times=None,
        past_actuals_by_slot=past,
    )
    h8 = [e for e in horizon if e["start"] == "2026-06-29T08:00:00+00:00"]
    assert h8 and h8[0]["pv_w"] == 800.0 and h8[0]["mode"] == "actual"


@pytest.mark.asyncio
async def test_get_past_actuals_caches_per_hour_and_filters_future():
    """_get_past_actuals: reads once per clock-hour, returns only hours < now_h."""
    rows = [
        {
            "ts": datetime(2026, 6, 29, 9, tzinfo=UTC).isoformat(),
            "pv_w": 500.0,
            "load_w": 200.0,
            "batt_w": 0.0,
            "p1_w": 0.0,
            "soc": 40.0,
        }
    ]
    calls = {"n": 0}

    # _Rec kept local (not migrated to helpers.StubRecorder): this test asserts
    # caching behaviour of Controller._get_past_actuals itself — it needs a
    # call-counter on read_feature_rows and must ignore since_iso (always
    # return the same fixed rows), which is a different contract than
    # StubRecorder's accumulate-and-filter-by-since_iso semantics.
    class _Rec:
        def read_feature_rows(self, since_iso=None):
            calls["n"] += 1
            return rows

    c = ctrl.Controller.__new__(ctrl.Controller)
    c._hass = _Hass()
    c._recorder = _Rec()
    c.cfg = Config()
    c._past_actuals_cache = None
    c._past_actuals_hour = None

    now = datetime(2026, 6, 29, 10, 30, tzinfo=UTC)
    out1 = await c._get_past_actuals(now)
    out2 = await c._get_past_actuals(now)
    assert datetime(2026, 6, 29, 9, tzinfo=UTC) in out1
    assert calls["n"] == 1  # second call served from cache (same clock-hour)
    assert out1 == out2


# --- Slot-grid display actuals (2026-08-03 history-gap fix) ----------------


def _tick_rows(hour, minutes):
    return [
        {
            "ts": datetime(2026, 6, 29, hour, m, tzinfo=UTC).isoformat(),
            "pv_w": 500.0,
            "load_w": 200.0,
            "batt_w": 0.0,
            "p1_w": 0.0,
            "soc": 40.0,
        }
        for m in minutes
    ]


def _stub_controller(rows):
    class _Rec:
        def __init__(self):
            self.n = 0

        def read_feature_rows(self, since_iso=None):
            self.n += 1
            return [r for r in rows if r["ts"] >= (since_iso or "")]

    c = ctrl.Controller.__new__(ctrl.Controller)
    c._hass = _Hass()
    c._recorder = _Rec()
    c.cfg = Config()
    c._past_actuals_cache = None
    c._past_actuals_slot_cache = None
    c._past_actuals_slot_minutes = None
    c._past_actuals_hour = None
    c._running_rows = None
    c._running_rows_at = None
    return c


@pytest.mark.asyncio
async def test_slot_actuals_cover_the_elapsed_quarters_of_the_running_hour():
    """The reported gap: at 15-min, 10:00/10:15/10:30 are past but their CLOCK
    HOUR is still running, so the hourly aggregate never released them and the
    forecast (which starts at now's slot, 10:45) never covered them either."""
    rows = _tick_rows(9, range(60)) + _tick_rows(10, range(47))
    c = _stub_controller(rows)
    now = datetime(2026, 6, 29, 10, 47, tzinfo=UTC)

    out = await c._get_past_actuals_slots(now, 15)

    assert datetime(2026, 6, 29, 10, 30, tzinfo=UTC) in out, "elapsed quarter missing → card draws a hole"
    assert sorted(out) == [datetime(2026, 6, 29, 9, m, tzinfo=UTC) for m in (0, 15, 30, 45)] + [
        datetime(2026, 6, 29, 10, m, tzinfo=UTC) for m in (0, 15, 30)
    ]
    # now's own slot is the forecast's first row — it must NOT also be an actual.
    assert datetime(2026, 6, 29, 10, 45, tzinfo=UTC) not in out


@pytest.mark.asyncio
async def test_load_adapt_input_stays_hour_bucketed_at_15min():
    """load_adapt matches now_h - back against an HOURLY prediction log and reads
    load_kwh as a whole clock-hour's energy — it must not see slot buckets."""
    rows = _tick_rows(9, range(60)) + _tick_rows(10, range(47))
    c = _stub_controller(rows)
    now = datetime(2026, 6, 29, 10, 47, tzinfo=UTC)

    hourly = await c._get_past_actuals(now, 15)

    assert list(hourly) == [datetime(2026, 6, 29, 9, tzinfo=UTC)]


@pytest.mark.asyncio
async def test_slot_actuals_reduce_to_the_hourly_cache_at_60min():
    """slot_minutes=60: now's slot IS now's hour, so there is no running-hour
    slice and no second recorder read — byte-identical to the legacy path."""
    rows = _tick_rows(9, range(60)) + _tick_rows(10, range(47))
    c = _stub_controller(rows)
    now = datetime(2026, 6, 29, 10, 47, tzinfo=UTC)

    slots = await c._get_past_actuals_slots(now, 60)
    hourly = await c._get_past_actuals(now, 60)

    assert slots == hourly
    assert c._recorder.n == 1  # one 48h read, no running-hour read


@pytest.mark.asyncio
async def test_slot_actuals_share_one_recorder_read_per_tick():
    """The 48h read is cached per clock-hour; the running-hour read is memoised
    on the tick's own `now` and shared with the delivered add-back."""
    rows = _tick_rows(9, range(60)) + _tick_rows(10, range(47))
    c = _stub_controller(rows)
    now = datetime(2026, 6, 29, 10, 47, tzinfo=UTC)

    await c._get_past_actuals_slots(now, 15)
    await c._get_current_delivered(now)
    assert c._recorder.n == 2  # one 48h + one running-hour, not three

    await c._get_past_actuals_slots(now + timedelta(minutes=1), 15)
    assert c._recorder.n == 3  # next tick re-reads the running hour only
