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

    async def test_aggregation_failure_leaves_the_cache_intact(self, monkeypatch):
        """A raise from aggregate_actual_days itself (not just the recorder read)

        must not propagate out of _tick_impl and must not advance the day key —
        else the display-only stats table can pin the control loop in failsafe
        for the whole read window (review finding, Task 8 round 1).
        """
        from tests.helpers import make_controller

        ctrl, _act = make_controller()
        ctrl._daily_actuals = {date(2026, 7, 31): daily_stats.new_day_totals()}
        ctrl._daily_actuals_day = "2026-07-31"
        ctrl._recorder.read_feature_rows = lambda since_iso: []

        def _boom(*_args, **_kwargs):
            raise RuntimeError("aggregation is unhappy")

        monkeypatch.setattr(daily_stats, "aggregate_actual_days", _boom)

        await ctrl._refresh_daily_actuals(datetime(2026, 8, 1, 10, 0, tzinfo=UTC))

        assert set(ctrl._daily_actuals) == {date(2026, 7, 31)}
        assert ctrl._daily_actuals_day == "2026-07-31", "a failed aggregation must not advance the key"


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

    async def test_per_slot_export_curve_prices_each_slot_and_falls_back_flat(self):
        """The curve branch of ``_export_price_at`` — previously untested.

        Three future export slots: two covered by the per-slot export curve at
        DIFFERENT prices (so a single flat price cannot fake the result), one
        past the curve's end which must fall back to the flat entity price.
        Every leg is asserted post-fee, which pins that the fee is subtracted
        exactly once — neither skipped on the curve branch nor applied twice
        (once by resample and again by effective_export_price).
        """
        from custom_components.anker_x1_smartgrid.models import PriceSlot
        from tests.helpers import make_controller

        ctrl, _act = make_controller()
        ctrl._daily_actuals = {}
        ctrl._daily_actuals_day = None
        ctrl.cfg = replace(ctrl.cfg, export_fee_eur_per_kwh=0.02)

        now = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
        _t17 = datetime(2026, 8, 2, 17, 0, tzinfo=UTC)
        _t18 = datetime(2026, 8, 2, 18, 0, tzinfo=UTC)
        _t19 = datetime(2026, 8, 2, 19, 0, tzinfo=UTC)
        export_slots = [PriceSlot(start=_t17, price=0.40), PriceSlot(start=_t18, price=0.10)]
        horizon = [
            {
                "start": start.isoformat(),
                "price": 0.30,
                "grid_charge_kwh": 0.0,
                "grid_export_kwh": kwh,
                "estimated": False,
                "mode": "export",
            }
            for start, kwh in ((_t17, 2.0), (_t18, 1.0), (_t19, 1.0))
        ]
        ctrl._publish_daily_stats(now, horizon, export_price=0.22, export_slots=export_slots, slot_minutes=60)

        future = next(r for r in ctrl.last_status["daily_stats"] if r["source"] == "plan")
        # 2.0 x (0.40-0.02) + 1.0 x (0.10-0.02) + 1.0 x (0.22-0.02) uncovered
        assert future["revenue_eur"] == pytest.approx(0.76 + 0.08 + 0.20)

