"""Current-slot delivered-energy plumbing: keeps an in-progress grid charge on
the plan card instead of letting the forward model decay it to 0.

See plan.build_plan_horizon's ``delivered_by_hour`` contract and the live
2026-07-29 case (2.5 kWh grid charge rendered as mode="grid", grid_charge_w=0).
"""

from __future__ import annotations

from datetime import datetime, UTC

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
    return PriceSlot(start=datetime(2026, 7, 29, h, tzinfo=UTC), price=0.13)


class _Pred:
    def predict(self, *a, **k):
        return 300.0


def test_compute_decision_threads_delivered_into_horizon():
    now = datetime(2026, 7, 29, 11, tzinfo=UTC)
    _plan, _sp, _edge, horizon, _hm, _ivr = ctrl.compute_decision(
        PlanState.initial(now),
        PlantInputs(soc=49.0, meter_w=0.0, now=now),
        [_slot(10), _slot(11), _slot(12)],
        0.0,
        now,
        _Pred(),
        15.0,
        Config(),
        sun_times=None,
        delivered_by_hour={now: {"grid_charge_kwh": 2.5}},
    )
    cur = [e for e in horizon if e["start"] == "2026-07-29T11:00:00+00:00"]
    assert cur and cur[0]["grid_charge_kwh"] == 2.5


@pytest.mark.asyncio
async def test_get_current_delivered_returns_only_the_in_progress_hour():
    rows = [
        {
            "ts": datetime(2026, 7, 29, h, m, tzinfo=UTC).isoformat(),
            "pv_w": 0.0,
            "load_w": 0.0,
            "batt_w": -6000.0,
            "p1_w": 6000.0,
            "soc": 40.0,
            "batt_charge_kwh": 0.1,
            "pv_kwh": 0.0,
            "house_load_kwh": 0.0,
            "grid_export_kwh": 0.0,
        }
        for h, m in ((10, 30), (11, 10), (11, 20))
    ]
    seen = {"since": None, "n": 0}

    class _Rec:
        def read_feature_rows(self, since_iso=None):
            seen["since"] = since_iso
            seen["n"] += 1
            return [r for r in rows if r["ts"] >= (since_iso or "")]

    c = ctrl.Controller.__new__(ctrl.Controller)
    c._hass = _Hass()
    c._recorder = _Rec()
    c.cfg = Config()

    now = datetime(2026, 7, 29, 11, 24, tzinfo=UTC)
    out = await c._get_current_delivered(now)

    # Scoped to this hour only — never the 48 h past-actuals window.
    assert seen["since"] == "2026-07-29T11:00:00+00:00"
    assert list(out) == [datetime(2026, 7, 29, 11, tzinfo=UTC)]
    assert out[datetime(2026, 7, 29, 11, tzinfo=UTC)]["grid_charge_kwh"] > 0


@pytest.mark.asyncio
async def test_get_current_delivered_is_not_cached_within_the_hour():
    """The in-progress hour changes under us — unlike completed past hours."""

    class _Rec:
        def __init__(self):
            self.n = 0

        def read_feature_rows(self, since_iso=None):
            self.n += 1
            return []

    c = ctrl.Controller.__new__(ctrl.Controller)
    c._hass = _Hass()
    c._recorder = _Rec()
    c.cfg = Config()

    now = datetime(2026, 7, 29, 11, 24, tzinfo=UTC)
    await c._get_current_delivered(now)
    await c._get_current_delivered(now)
    assert c._recorder.n == 2


@pytest.mark.asyncio
async def test_run_compute_decision_forwards_delivered_on_the_live_path(monkeypatch):
    seen = {}

    def _capture(*a, **kw):
        seen.update(kw)
        return (None, 0.0, None, [], "", [])

    monkeypatch.setattr(ctrl, "compute_decision", _capture)
    c = ctrl.Controller.__new__(ctrl.Controller)
    c._hass = _Hass()
    c.cfg = Config()
    monkeypatch.setattr(type(c), "_planner_curve", lambda self: None, raising=False)

    delivered = {datetime(2026, 7, 29, 11, tzinfo=UTC): {"grid_charge_kwh": 2.5}}
    await c._run_compute_decision(
        None,
        None,
        None,
        None,
        0.0,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        export_price=None,
        export_price_matches_import=False,
        temp_by_hour={},
        slot_minutes=60,
        dp_out={},
        delivered_by_hour=delivered,
    )
    assert seen["delivered_by_hour"] == delivered


@pytest.mark.asyncio
async def test_run_compute_decision_omits_delivered_on_the_shadow_path(monkeypatch):
    """Shadow mirrors the disabled path — live-only kwargs stay off it."""
    seen = {}

    def _capture(*a, **kw):
        seen.update(kw)
        return (None, 0.0, None, [], "", [])

    monkeypatch.setattr(ctrl, "compute_decision", _capture)
    c = ctrl.Controller.__new__(ctrl.Controller)
    c._hass = _Hass()
    c.cfg = Config()
    monkeypatch.setattr(type(c), "_planner_curve", lambda self: None, raising=False)

    await c._run_compute_decision(
        None,
        None,
        None,
        None,
        0.0,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        export_price=None,
        export_price_matches_import=False,
        temp_by_hour={},
        slot_minutes=60,
        dp_out={},
        shadow=True,
        delivered_by_hour={datetime(2026, 7, 29, 11, tzinfo=UTC): {"grid_charge_kwh": 2.5}},
    )
    assert "delivered_by_hour" not in seen


@pytest.mark.asyncio
async def test_get_current_delivered_swallows_recorder_errors():
    class _Rec:
        def read_feature_rows(self, since_iso=None):
            raise RuntimeError("db gone")

    c = ctrl.Controller.__new__(ctrl.Controller)
    c._hass = _Hass()
    c._recorder = _Rec()
    c.cfg = Config()

    assert await c._get_current_delivered(datetime(2026, 7, 29, 11, 24, tzinfo=UTC)) == {}
