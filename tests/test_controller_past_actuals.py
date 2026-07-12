"""Task 3 tests: compute_decision threads past_actuals into horizon;
_get_past_actuals caches per clock-hour and filters to hours < now_h."""
from __future__ import annotations

from datetime import datetime, timezone

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
    return PriceSlot(start=datetime(2026, 6, 29, h, tzinfo=timezone.utc), price=0.20)


def test_compute_decision_threads_past_actuals_into_horizon():
    """past_actuals_by_hour is plumbed into build_plan_horizon (sun_times=None path)."""
    cfg = Config()
    now = datetime(2026, 6, 29, 10, tzinfo=timezone.utc)
    inputs = PlantInputs(soc=50.0, meter_w=0.0, now=now)
    slots = [_slot(8), _slot(9), _slot(10), _slot(11)]
    past = {
        datetime(2026, 6, 29, 8, tzinfo=timezone.utc): {
            "pv_w": 800.0, "load_w": 250.0, "soc": 30.0,
            "solar_charge_w": 400.0, "grid_charge_w": 0.0, "grid_export_w": 0.0,
        }
    }

    class _Pred:
        def predict(self, *a, **k):
            return 300.0

    # sun_times=None → build_plan_horizon path (not build_display_horizon)
    _plan, _sp, _edge, horizon, _hm, _ivr = ctrl.compute_decision(
        PlanState.initial(now), inputs, slots, 0.0, now, _Pred(), 15.0, cfg,
        sun_times=None, past_actuals_by_hour=past,
    )
    h8 = [e for e in horizon if e["start"] == "2026-06-29T08:00:00+00:00"]
    assert h8 and h8[0]["pv_w"] == 800.0 and h8[0]["mode"] == "actual"


@pytest.mark.asyncio
async def test_get_past_actuals_caches_per_hour_and_filters_future():
    """_get_past_actuals: reads once per clock-hour, returns only hours < now_h."""
    rows = [{"ts": datetime(2026, 6, 29, 9, tzinfo=timezone.utc).isoformat(),
             "pv_w": 500.0, "load_w": 200.0, "batt_w": 0.0, "p1_w": 0.0, "soc": 40.0}]
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

    now = datetime(2026, 6, 29, 10, 30, tzinfo=timezone.utc)
    out1 = await c._get_past_actuals(now)
    out2 = await c._get_past_actuals(now)
    assert datetime(2026, 6, 29, 9, tzinfo=timezone.utc) in out1
    assert calls["n"] == 1  # second call served from cache (same clock-hour)
    assert out1 == out2
