"""Controller wiring for the per-day statistics table.

Spec: docs/superpowers/specs/2026-08-01-card-crop-daily-stats-design.md
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, UTC

import pytest

from custom_components.anker_x1_smartgrid import daily_stats

# make_controller returns (controller, actuator); the controller holds its
# StubRecorder at ._recorder and its Config at .cfg (Config is frozen, so
# overrides go through dataclasses.replace).


class TestActualsCache:
    async def test_backfill_runs_once_per_local_day(self):
        from tests.helpers import make_controller

        ctrl, _act = make_controller()
        calls = []

        def _fake_read(since_iso):
            calls.append(since_iso)
            return []

        ctrl._recorder.read_feature_rows = _fake_read

        now = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
        await ctrl._refresh_daily_actuals(now)
        await ctrl._refresh_daily_actuals(now + timedelta(hours=3))
        assert len(calls) == 1, "same local day must not re-query"

        await ctrl._refresh_daily_actuals(now + timedelta(days=1))
        assert len(calls) == 2, "new local day must re-query"

    async def test_backfill_window_reaches_one_day_past_the_table_window(self):
        from tests.helpers import make_controller

        ctrl, _act = make_controller()
        seen = []
        ctrl._recorder.read_feature_rows = lambda since_iso: seen.append(since_iso) or []

        now = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
        await ctrl._refresh_daily_actuals(now)
        since = datetime.fromisoformat(seen[0])
        assert (now - since).days == daily_stats.WINDOW_DAYS + 1

    async def test_recorder_failure_leaves_the_cache_intact(self):
        from tests.helpers import make_controller

        ctrl, _act = make_controller()
        ctrl._daily_actuals = {date(2026, 7, 31): daily_stats.new_day_totals()}
        ctrl._daily_actuals_day = "2026-07-31"

        def _boom(_since):
            raise RuntimeError("sqlite is unhappy")

        ctrl._recorder.read_feature_rows = _boom
        await ctrl._refresh_daily_actuals(datetime(2026, 8, 1, 10, 0, tzinfo=UTC))
        assert set(ctrl._daily_actuals) == {date(2026, 7, 31)}
        assert ctrl._daily_actuals_day == "2026-07-31", "a failed refresh must not advance the key"


class TestPublishDailyStats:
    async def test_publishes_a_merged_table_with_today_from_the_ledger(self):
        from tests.helpers import make_controller

        ctrl, _act = make_controller()
        ctrl._daily_actuals = {}
        ctrl._daily_actuals_day = None
        ctrl.today_grid_charge_kwh = 2.0
        ctrl.today_export_kwh = 1.0
        ctrl.today_charge_cost_eur = 0.60
        ctrl.today_export_revenue_eur = 0.25

        now = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
        ctrl._publish_daily_stats(now, horizon=[], export_price=None, export_slots=None, slot_minutes=60)

        table = ctrl.last_status["daily_stats"]
        assert isinstance(table, list) and table, table
        today_row = table[-1]
        assert today_row["grid_charge_kwh"] == pytest.approx(2.0)
        assert today_row["grid_export_kwh"] == pytest.approx(1.0)
        assert today_row["net_eur"] == pytest.approx(-0.35)

    async def test_flat_export_price_has_the_fee_subtracted(self):
        from tests.helpers import make_controller

        ctrl, _act = make_controller()
        ctrl._daily_actuals = {}
        ctrl._daily_actuals_day = None
        # Config is @dataclass(frozen=True) — assignment to a field raises
        # FrozenInstanceError; swap the whole object instead.
        ctrl.cfg = replace(ctrl.cfg, export_fee_eur_per_kwh=0.02)

        now = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
        horizon = [
            {
                "start": datetime(2026, 8, 2, 17, 0, tzinfo=UTC).isoformat(),
                "price": 0.30,
                "grid_charge_kwh": 0.0,
                "grid_export_kwh": 2.0,
                "estimated": False,
                "mode": "export",
            }
        ]
        ctrl._publish_daily_stats(now, horizon=horizon, export_price=0.22, export_slots=None, slot_minutes=60)

        future = next(r for r in ctrl.last_status["daily_stats"] if r["source"] == "plan")
        assert future["revenue_eur"] == pytest.approx(2.0 * 0.20)
