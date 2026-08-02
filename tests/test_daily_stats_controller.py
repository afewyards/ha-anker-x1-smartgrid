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

    async def test_matching_export_entity_prices_each_slot_off_the_import_curve(self):
        """The lab / salderen config: ``ent_export_price == ent_price``.

        ``_resolve_export_slots`` returns [] for that config BY DESIGN (a curve
        would just duplicate the import curve), so the curve branch never fires
        and every future export slot fell through to the flat CURRENT price.
        Live on 2026-08-02 that booked an evening peak of 0.36 €/kWh at the
        midday spot of 0.2319, under-reporting today's net by €1.36.

        ``decision.py`` values this config at ``import price − fee`` per slot
        (the ``export_price_matches_import`` branch); the table must agree.
        """
        from tests.helpers import make_controller

        ctrl, _act = make_controller()
        ctrl._daily_actuals = {}
        ctrl._daily_actuals_day = None
        ctrl.cfg = replace(ctrl.cfg, export_fee_eur_per_kwh=0.02)

        now = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
        horizon = [
            {
                "start": start.isoformat(),
                "price": price,
                "grid_charge_kwh": 0.0,
                "grid_export_kwh": 1.0,
                "estimated": False,
                "mode": "export",
            }
            for start, price in (
                (datetime(2026, 8, 2, 17, 0, tzinfo=UTC), 0.36),
                (datetime(2026, 8, 2, 11, 0, tzinfo=UTC), 0.12),
            )
        ]
        ctrl._publish_daily_stats(
            now,
            horizon,
            export_price=0.2319,
            export_slots=None,
            slot_minutes=60,
            export_matches_import=True,
        )

        future = next(r for r in ctrl.last_status["daily_stats"] if r["source"] == "plan")
        # 1.0 x (0.36-0.02) + 1.0 x (0.12-0.02) — NOT 2.0 x (0.2319-0.02).
        assert future["revenue_eur"] == pytest.approx(0.34 + 0.10)

    async def test_separate_scalar_export_entity_ratio_scales_the_import_curve(self):
        """Mirrors ``decision.py``'s fourth branch: a distinct export entity with

        no per-slot price attribute. The DP scales the import curve by
        ``export_price / current_import``; the flat fallback would instead
        smear one price over every slot and lose the curve's shape entirely.
        """
        from tests.helpers import make_controller

        ctrl, _act = make_controller()
        ctrl._daily_actuals = {}
        ctrl._daily_actuals_day = None
        ctrl.cfg = replace(ctrl.cfg, export_fee_eur_per_kwh=0.0)

        now = datetime(2026, 8, 1, 10, 30, tzinfo=UTC)
        horizon = [
            # The slot containing `now` — supplies cur_import, exactly as
            # decision.py's window_price[0] does.
            {
                "start": datetime(2026, 8, 1, 10, 0, tzinfo=UTC).isoformat(),
                "price": 0.40,
                "grid_charge_kwh": 0.0,
                "grid_export_kwh": 0.0,
                "estimated": False,
                "mode": "actual",
            },
            {
                "start": datetime(2026, 8, 2, 17, 0, tzinfo=UTC).isoformat(),
                "price": 0.80,
                "grid_charge_kwh": 0.0,
                "grid_export_kwh": 1.0,
                "estimated": False,
                "mode": "export",
            },
        ]
        # export 0.20 against a current import of 0.40 → ratio 0.5.
        ctrl._publish_daily_stats(now, horizon, export_price=0.20, export_slots=None, slot_minutes=60)

        future = next(r for r in ctrl.last_status["daily_stats"] if r["source"] == "plan")
        assert future["revenue_eur"] == pytest.approx(1.0 * 0.80 * 0.5)

    async def test_static_tariff_mode_never_tracks_the_import_curve(self):
        """Static mode broadcasts the configured constant flat — the same rule

        ``decision.py`` states explicitly: ratio-scaling a fixed export credit
        would make it swing with an HP/HC import schedule.
        """
        from custom_components.anker_x1_smartgrid import const
        from tests.helpers import make_controller

        ctrl, _act = make_controller()
        ctrl._daily_actuals = {}
        ctrl._daily_actuals_day = None
        ctrl.cfg = replace(ctrl.cfg, export_fee_eur_per_kwh=0.0, price_mode=const.PRICE_MODE_STATIC)

        now = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
        horizon = [
            {
                "start": datetime(2026, 8, 2, 17, 0, tzinfo=UTC).isoformat(),
                "price": 0.30,  # HP hour — must NOT leak into the export leg
                "grid_charge_kwh": 0.0,
                "grid_export_kwh": 2.0,
                "estimated": False,
                "mode": "export",
            }
        ]
        ctrl._publish_daily_stats(now, horizon, export_price=0.10, export_slots=None, slot_minutes=60)

        future = next(r for r in ctrl.last_status["daily_stats"] if r["source"] == "plan")
        assert future["revenue_eur"] == pytest.approx(2.0 * 0.10)

    async def test_todays_row_does_not_count_the_in_progress_hour_twice(self):
        """Review finding I1, at the 15-min resolution both deployments run.

        The tick hands ``_publish_daily_stats`` the same ``delivered_by_hour``
        it handed ``build_display_horizon``, so the hour's already-delivered
        kWh — which that builder folded into EVERY quarter row of the hour, and
        which the live ledger has ALREADY booked — is taken back out of the
        planned half.  Elapsed quarters are dropped outright.

        Before the fix this row read 2.0 (ledger) + 4 x 2.5 (quarters) = 12.0
        kWh for 2.5 kWh of real activity.
        """
        from tests.helpers import make_controller

        ctrl, _act = make_controller()
        ctrl._daily_actuals = {}
        ctrl._daily_actuals_day = None
        # The ledger already holds this clock-hour's delivered charge.
        ctrl.today_grid_charge_kwh = 2.0
        ctrl.today_charge_cost_eur = 0.60

        now = datetime(2026, 8, 1, 10, 37, tzinfo=UTC)  # mid 10:00 clock-hour
        _hour = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
        delivered = {_hour: {"grid_charge_kwh": 2.0}}
        horizon = [
            {
                "start": (_hour + timedelta(minutes=15 * i)).isoformat(),
                "price": 0.30,
                # 0.5 modelled remainder + the 2.0 hour-wide add-back, exactly
                # as plan.build_horizon emits it for the in-progress hour.
                "grid_charge_kwh": 2.5,
                "grid_export_kwh": 0.0,
                "estimated": False,
                "mode": "grid",
            }
            for i in range(4)
        ]
        ctrl._publish_daily_stats(now, horizon, None, None, 15, delivered)

        today_row = next(r for r in ctrl.last_status["daily_stats"] if r["source"] == "mixed")
        # 2.0 measured (ledger) + 0.5 still-planned remainder of the 10:45 slot.
        assert today_row["grid_charge_kwh"] == pytest.approx(2.5)
        assert today_row["cost_eur"] == pytest.approx(0.60 + 0.5 * 0.30)


class TestStatusCarriesTheTable:
    """``_status`` REBINDS last_status, and only the enabled path republishes
    ``daily_stats`` — so without an explicit carry-over a single transient
    failsafe tick (price entity reloading, a startup race) blanks the card,
    and a disabled period hides the table entirely even though every past day
    is still sitting in the recorder.
    """

    async def test_failsafe_status_rebuild_keeps_the_previous_table(self):
        from tests.helpers import make_controller

        ctrl, _act = make_controller()
        _table = [{"date": "2026-07-31", "net_eur": 1.5, "source": "actual"}]
        ctrl.last_status["daily_stats"] = _table

        status = ctrl._status(datetime(2026, 8, 1, 10, 0, tzinfo=UTC), 0.0, None, "failsafe")

        assert status["daily_stats"] == _table
        assert ctrl.last_status["daily_stats"] == _table

    async def test_status_does_not_invent_the_key_when_nothing_was_published(self):
        from tests.helpers import make_controller

        ctrl, _act = make_controller()
        status = ctrl._status(datetime(2026, 8, 1, 10, 0, tzinfo=UTC), 0.0, None, "failsafe")
        assert "daily_stats" not in status
