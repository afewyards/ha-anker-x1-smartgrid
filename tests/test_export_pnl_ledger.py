"""TDD tests for E3: realized-arbitrage-PnL ledger.

Covers:
- export_pnl_eur: worked-example calculation
- export_pnl_eur: negative PnL when export price is too low
- controller tick: per-interval PnL accumulated into today's total
- controller tick: accumulator resets on local-day rollover
- controller tick: export interval tagged in observability attributes
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.anker_x1_smartgrid import const
from custom_components.anker_x1_smartgrid import controller as ctrl_mod
from custom_components.anker_x1_smartgrid.controller import Controller
from custom_components.anker_x1_smartgrid.models import (
    Config,
    ControllerState,
    ExportState,
    PlanState,
    PriceSlot,
)
from custom_components.anker_x1_smartgrid.optimize import export_pnl_eur


# ---------------------------------------------------------------------------
# Time anchors
# ---------------------------------------------------------------------------

BASE = datetime(2026, 6, 25, 14, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers: stubs reused from test_controller_export_executor pattern
# ---------------------------------------------------------------------------


class _StubActuator:
    def __init__(self):
        self.calls: list[tuple] = []
        self.last_setpoint_w: float = 0.0
        self.engaged: bool = False

    async def engage_and_charge(self, setpoint_w: float) -> None:
        self.calls.append(("engage_and_charge", setpoint_w))
        self.last_setpoint_w = setpoint_w
        self.engaged = True

    async def engage_export(self, setpoint_w: float) -> None:
        if setpoint_w <= 0:
            raise ValueError(f"export-only: setpoint must be > 0, got {setpoint_w}")
        self.calls.append(("engage_export", setpoint_w))
        self.last_setpoint_w = setpoint_w
        self.engaged = True

    async def release_to_self(self) -> None:
        self.calls.append(("release_to_self",))
        self.last_setpoint_w = 0.0
        self.engaged = False


class _StubStore:
    def __init__(self):
        self.saved: dict = {}

    async def async_save(self, data: dict) -> None:
        self.saved = data


class _StubRecorder:
    def __init__(self):
        self.rows: list[dict] = []
        self.decision_rows: list[dict] = {}
        self.daily_regret_rows: dict[str, dict] = {}

    def append(self, row: dict) -> None:
        self.rows.append(row)

    def append_decision(self, **kwargs) -> None:
        self.decision_rows[kwargs.get("ts", "")] = kwargs

    def purge_older_than(self, ts, days) -> None:
        pass

    def purge_decisions_older_than(self, cutoff_iso) -> int:
        return 0

    def rollup_hours(self, now_iso) -> int:
        return 0

    def purge_hourly_older_than(self, cutoff_iso) -> int:
        return 0

    def wal_checkpoint(self) -> None:
        pass

    def read_load_samples(self, since_iso=None):
        return []

    def read_decisions(self, since_iso, until_iso=None):
        return []

    def read_feature_rows(self, since_iso=None):
        return []

    def read_hourly_rows(self):
        return []

    def upsert_daily_regret(self, **kwargs) -> None:
        self.daily_regret_rows[kwargs["day"]] = kwargs

    def read_latest_daily_regret(self):
        if not self.daily_regret_rows:
            return None
        latest_day = max(self.daily_regret_rows.keys())
        return self.daily_regret_rows[latest_day]

    def read_daily_regret_range(self, since_day, until_day=None):
        rows = [v for k, v in self.daily_regret_rows.items() if k >= since_day]
        if until_day is not None:
            rows = [r for r in rows if r["day"] < until_day]
        return sorted(rows, key=lambda r: r["day"])


class _StubHass:
    def __init__(self):
        self._states: dict = {}

    class _StateObj:
        def __init__(self, state, attributes=None):
            self.state = state
            self.attributes = attributes or {}

    def set_state(self, entity_id, state, attributes=None):
        self._states[entity_id] = self._StateObj(state, attributes)

    class _States:
        def __init__(self, parent):
            self._parent = parent

        def get(self, entity_id):
            return self._parent._states.get(entity_id)

    @property
    def states(self):
        return self._States(self)

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


def _make_export_cfg(**overrides) -> Config:
    defaults = dict(
        capacity_kwh=10.0,
        soc_floor=10.0,
        soc_target=97.0,
        max_charge_w=3000.0,
        max_export_w=3000.0,
        grid_export_limit_w=3000.0,
        eta_charge=1.0,
        round_trip_eff=1.0,     # eta_discharge=1.0 for simple arithmetic
        cycle_cost_eur_per_kwh=0.04,
        export_eps_lo_kwh=0.2,
        export_eps_hi_kwh=0.4,
        export_dwell_min=0,
        enable_export=True,
    )
    defaults.update(overrides)
    return Config(**defaults)


def _make_controller(hass, actuator=None, cfg_overrides=None):
    data = {
        const.CONF_ENT_SOC: "sensor.soc",
        const.CONF_ENT_PHASE: [
            "sensor.phase_l1",
            "sensor.phase_l2",
            "sensor.phase_l3",
        ],
        const.CONF_ENT_PRICE: "sensor.price",
        const.CONF_ENT_PV_TODAY: [],
        const.CONF_ENT_PV_TOMORROW: [],
        const.CONF_ENT_SUN: "sun.sun",
        const.CONF_ENT_BATTERY_POWER: "sensor.battery_power",
        const.CONF_ENT_PV_POWER: "sensor.pv_power",
        const.CONF_ENT_SETPOINT: "number.setpoint",
        const.CONF_ENT_ENGAGE: "switch.engage",
        const.CONF_ENT_WORKMODE: "select.workmode",
        const.CONF_ENT_IRRADIANCE: "sensor.irradiance",
        const.CONF_ENT_TEMP: "weather.home",
        const.CONF_ENT_EXPORT_PRICE: "sensor.export_price",
    }
    act = actuator or _StubActuator()
    store = _StubStore()
    rec = _StubRecorder()
    ctrl = Controller(
        hass=hass,
        data=data,
        recorder=rec,
        actuator=act,
        store=store,
    )
    cfg = _make_export_cfg(**(cfg_overrides or {}))
    ctrl.cfg = cfg
    return ctrl, act, store, rec


def _seed_export_inputs(hass, *, soc="80.0", export_price="0.30"):
    hass.set_state("sensor.soc", soc)
    hass.set_state("sensor.phase_l1", "100.0")
    hass.set_state("sensor.phase_l2", "100.0")
    hass.set_state("sensor.phase_l3", "100.0")
    hass.set_state("sensor.pv_power", "2000.0")
    hass.set_state("sensor.battery_power", "0.0")
    hass.set_state("sensor.irradiance", "600.0")
    hass.set_state("weather.home", "sunny", {"temperature": 22.0})
    hass.set_state("sensor.export_price", export_price)
    sunset_iso = (BASE + timedelta(hours=6)).isoformat()
    hass.set_state("sun.sun", "above_horizon", {"next_setting": sunset_iso})
    # Price forecast: current hour expensive (0.30), later hours cheap (0.10).
    # This gives find_next_trough a cheap slot to return, producing a low
    # keep_value so that exporting at 0.30 yields a genuinely positive PnL.
    prices = [0.30, 0.30, 0.10, 0.10, 0.10, 0.10, 0.10, 0.10, 0.10, 0.10, 0.10, 0.10]
    hass.set_state("sensor.price", "0.30", {
        "forecast": [
            {
                "datetime": (BASE + timedelta(hours=i)).isoformat(),
                "electricity_price": int(prices[i] * const.PRICE_SCALE),
            }
            for i in range(12)
        ]
    })


# ---------------------------------------------------------------------------
# E3-1: export_pnl_eur pure helper — worked example
# ---------------------------------------------------------------------------


class TestExportPnlEur:
    """Unit tests for optimize.export_pnl_eur helper."""

    def test_worked_example_positive_pnl(self):
        """Worked example: eta=1.0, export=0.30, cycle_cost=0.04, keep=0.10 → positive.

        export_kwh=0.25, export_price=0.30, eta=1.0, cycle_cost=0.04, keep_value=0.10
        pnl = 0.25 * 0.30 * 1.0 - 0.04 * 0.25 - 0.10 * 0.25
          = 0.075 - 0.01 - 0.025
          = 0.040
        """
        cfg = _make_export_cfg(
            eta_charge=1.0, round_trip_eff=1.0, cycle_cost_eur_per_kwh=0.04
        )
        result = export_pnl_eur(
            export_kwh=0.25,
            export_price=0.30,
            keep_value=0.10,
            cfg=cfg,
        )
        assert result == pytest.approx(0.040, abs=1e-9)

    def test_negative_pnl_when_price_too_low(self):
        """Export price below hurdle → negative PnL (cost exceeds revenue)."""
        cfg = _make_export_cfg(
            eta_charge=1.0, round_trip_eff=1.0, cycle_cost_eur_per_kwh=0.04
        )
        # export_price = 0.05 < cycle_cost + keep_value → negative net
        result = export_pnl_eur(
            export_kwh=0.25,
            export_price=0.05,
            keep_value=0.10,
            cfg=cfg,
        )
        assert result < 0.0, f"Expected negative PnL for sub-hurdle export, got {result}"

    def test_zero_export_kwh_gives_zero_pnl(self):
        """Zero kWh exported → zero PnL regardless of price."""
        cfg = _make_export_cfg()
        result = export_pnl_eur(
            export_kwh=0.0,
            export_price=0.30,
            keep_value=0.10,
            cfg=cfg,
        )
        assert result == pytest.approx(0.0)

    def test_eta_discharge_applied(self):
        """eta_discharge < 1 reduces revenue (AC price scaled to DC basis)."""
        # round_trip_eff=0.85, eta_charge=0.95 → eta_discharge ≈ 0.8947
        cfg = _make_export_cfg(round_trip_eff=0.85, eta_charge=0.95)
        import math
        eta_d = 0.85 / 0.95

        result = export_pnl_eur(
            export_kwh=1.0,
            export_price=0.30,
            keep_value=0.05,
            cfg=cfg,
        )
        expected = 1.0 * 0.30 * eta_d - cfg.cycle_cost_eur_per_kwh - 0.05
        assert result == pytest.approx(expected, rel=1e-9)


# ---------------------------------------------------------------------------
# E3-2: controller accumulates today's export PnL across intervals
# ---------------------------------------------------------------------------


class TestExportPnlAccumulator:
    """Controller state: today's PnL total accumulates on engaged export ticks."""

    @pytest.mark.asyncio
    async def test_pnl_accumulates_on_export_tick(self):
        """After an engaged export tick, today_export_pnl_eur > 0."""
        hass = _StubHass()
        ctrl, act, _, rec = _make_controller(hass)
        _seed_export_inputs(hass, soc="80.0", export_price="0.30")

        # Force already-engaged so dwell is satisfied
        ctrl.export_state = ExportState(engaged=True, state_since=BASE - timedelta(hours=1))

        await ctrl.tick()

        export_calls = [c for c in act.calls if c[0] == "engage_export"]
        assert len(export_calls) >= 1, (
            f"engage_export must fire when surplus clears hurdle; calls={act.calls}"
        )
        # PnL should be positive (price 0.30 > hurdle with eta=1.0, cost=0.04, keep≈0.10)
        assert ctrl.today_export_pnl_eur > 0.0, (
            f"Expected positive accumulated PnL after export tick, "
            f"got {ctrl.today_export_pnl_eur}"
        )

    @pytest.mark.asyncio
    async def test_pnl_accumulates_across_multiple_ticks(self):
        """Multiple export ticks accumulate monotonically."""
        hass = _StubHass()
        ctrl, act, _, rec = _make_controller(hass)
        _seed_export_inputs(hass, soc="80.0", export_price="0.30")

        ctrl.export_state = ExportState(engaged=True, state_since=BASE - timedelta(hours=1))

        await ctrl.tick()
        pnl_after_first = ctrl.today_export_pnl_eur

        # Second tick (SoC still sufficient after first 5-min tick)
        await ctrl.tick()
        pnl_after_second = ctrl.today_export_pnl_eur

        assert pnl_after_second >= pnl_after_first, (
            "Accumulated PnL must be non-decreasing across export ticks"
        )

    @pytest.mark.asyncio
    async def test_pnl_resets_on_day_rollover(self, monkeypatch):
        """Accumulator resets to 0 when local day changes."""
        hass = _StubHass()
        ctrl, act, _, rec = _make_controller(hass)
        _seed_export_inputs(hass, soc="80.0", export_price="0.30")

        ctrl.export_state = ExportState(engaged=True, state_since=BASE - timedelta(hours=1))

        # Run a tick to accumulate some PnL
        await ctrl.tick()
        assert ctrl.today_export_pnl_eur >= 0.0

        # Seed the accumulator with a non-zero value to prove it resets
        ctrl.today_export_pnl_eur = 1.234
        ctrl._export_pnl_day = "2026-06-24"  # yesterday → triggers reset on next tick

        await ctrl.tick()

        # After day rollover, today's PnL should be fresh (from this tick only,
        # max possible is a few cents for one 5-min interval).
        # It must NOT include the 1.234 seeded for "yesterday".
        assert ctrl.today_export_pnl_eur < 0.50, (
            "today_export_pnl_eur must reset on local day rollover; "
            f"got {ctrl.today_export_pnl_eur} (> 0.50 means yesterday's total leaked)"
        )
        # PnL can be slightly negative in corner cases (e.g., very high keep_value);
        # the key invariant is that it does NOT carry forward the 1.234 sentinel.
        assert ctrl.today_export_pnl_eur > -0.10, (
            f"today_export_pnl_eur unexpectedly very negative after reset: "
            f"{ctrl.today_export_pnl_eur}"
        )


# ---------------------------------------------------------------------------
# E3-2b: PnL revenue must equal AC×price (no double eta_d)
# ---------------------------------------------------------------------------


class TestExportPnlBasis:
    """Regression: callsite must convert AC→DC before export_pnl_eur so that
    revenue = AC_kWh × price (no spurious second eta_discharge factor)."""

    @pytest.mark.asyncio
    async def test_pnl_revenue_is_ac_times_price_not_double_eta(self, monkeypatch):
        hass = _StubHass()
        # eta_discharge = 0.85/0.95 ≈ 0.8947 (<1) so a spurious η would be visible.
        ctrl, act, _, rec = _make_controller(
            hass, cfg_overrides=dict(eta_charge=0.95, round_trip_eff=0.85,
                                     cycle_cost_eur_per_kwh=0.0))
        _seed_export_inputs(hass, soc="80.0", export_price="0.30")
        # Zero out opportunity cost so PnL == revenue == AC_metered_kwh * eff_price.
        monkeypatch.setattr(ctrl_mod.optimize_mod, "compute_water_value",
                            lambda *a, **k: 0.0)
        ctrl.export_state = ExportState(engaged=True, state_since=BASE - timedelta(hours=1))
        await ctrl.tick()

        assert [c for c in act.calls if c[0] == "engage_export"], "expected an export tick"
        ac_kwh = rec.rows[-1]["export_kwh"]          # AC metered net the controller exported
        assert ac_kwh and ac_kwh > 0
        eff_price = ctrl_mod.optimize_mod.effective_export_price(0.30, ctrl.cfg)
        # Fixed: revenue = (ac/eta_d)*eff_price*eta_d = ac*eff_price (eta cancels).
        # Buggy:  ac*eff_price*eta_d  → ~10.5% lower → assertion fails.
        assert ctrl.today_export_pnl_eur == pytest.approx(ac_kwh * eff_price, rel=1e-6)


# ---------------------------------------------------------------------------
# E3-3: export interval tagged in last_status observability
# ---------------------------------------------------------------------------


class TestExportPnlTagging:
    """last_status exposes today's PnL total for G2 sensor."""

    @pytest.mark.asyncio
    async def test_today_export_pnl_present_in_last_status(self):
        """last_status must contain 'today_export_pnl_eur' key after any tick."""
        hass = _StubHass()
        ctrl, act, _, rec = _make_controller(hass)
        _seed_export_inputs(hass, soc="80.0", export_price="0.30")

        ctrl.export_state = ExportState(engaged=True, state_since=BASE - timedelta(hours=1))

        await ctrl.tick()

        assert "today_export_pnl_eur" in ctrl.last_status, (
            "last_status must contain 'today_export_pnl_eur' for G2 sensor consumption"
        )

    @pytest.mark.asyncio
    async def test_today_export_pnl_zero_when_no_export(self):
        """No export → today_export_pnl_eur is 0.0 in last_status (not None)."""
        hass = _StubHass()
        # Disable export
        ctrl, act, _, rec = _make_controller(
            hass, cfg_overrides={"enable_export": False}
        )
        _seed_export_inputs(hass, soc="80.0", export_price="0.30")

        await ctrl.tick()

        val = ctrl.last_status.get("today_export_pnl_eur")
        assert val == pytest.approx(0.0), (
            f"today_export_pnl_eur should be 0.0 when export is disabled, got {val!r}"
        )


# ---------------------------------------------------------------------------
# N2: metered-net PnL falls back to the cached last-known house load when the
# live sensor reads None (sensor blip), instead of crediting the full gross
# setpoint as net export.  TELEMETRY ONLY — the actuation gross setpoint
# (export_load_comp_factor compensation) must keep its existing 0.0-on-None
# behavior; under-export is the safe direction there.
# ---------------------------------------------------------------------------


class TestMeteredNetHouseLoadFallback:
    """C3/N2: cached house load feeds metered-net telemetry, never actuation."""

    @pytest.mark.asyncio
    async def test_metered_net_uses_cached_load_when_sensor_is_none(self):
        """Live house-load sensor (sensor.power_usage) is never seeded in this
        file's stub, so it reads None on every tick. With a cached previous
        reading of 400W, the metered-net export_kwh must be computed against
        that cache, not the full gross setpoint."""
        hass = _StubHass()
        ctrl, act, _, rec = _make_controller(hass)
        _seed_export_inputs(hass, soc="80.0", export_price="0.30")
        ctrl.export_state = ExportState(engaged=True, state_since=BASE - timedelta(hours=1))
        ctrl._last_house_load_w = 400.0  # cached previous reading

        await ctrl.tick()

        export_calls = [c for c in act.calls if c[0] == "engage_export"]
        assert export_calls, f"expected an export tick, calls={act.calls}"
        gross_setpoint_w = export_calls[-1][1]
        gross_kwh = gross_setpoint_w / 1000.0 * (const.TICK_SECONDS / 3600.0)
        expected_metered_kwh = (
            max(0.0, gross_setpoint_w - 400.0) / 1000.0 * (const.TICK_SECONDS / 3600.0)
        )

        recorded_kwh = rec.rows[-1]["export_kwh"]
        assert recorded_kwh == pytest.approx(expected_metered_kwh, rel=1e-6), (
            "metered-net export_kwh must use the cached 400W load, not the full "
            f"gross setpoint; got {recorded_kwh}, expected {expected_metered_kwh}"
        )
        assert recorded_kwh < gross_kwh - 1e-9, (
            "cached load must reduce metered net below the full gross setpoint"
        )

    @pytest.mark.asyncio
    async def test_actuation_gross_setpoint_ignores_cache(self):
        """Regression: the ACTUATION gross setpoint (export_load_comp_factor *
        house load) must stay 0.0-on-None regardless of the telemetry cache —
        under-export is the safe direction (N2 scope line)."""
        hass_a = _StubHass()
        ctrl_a, act_a, _, _ = _make_controller(hass_a)
        _seed_export_inputs(hass_a, soc="80.0", export_price="0.30")
        ctrl_a.export_state = ExportState(engaged=True, state_since=BASE - timedelta(hours=1))
        # ctrl_a._last_house_load_w left at its default (0.0) — baseline.
        await ctrl_a.tick()
        baseline_setpoint = [c for c in act_a.calls if c[0] == "engage_export"][-1][1]

        hass_b = _StubHass()
        ctrl_b, act_b, _, _ = _make_controller(hass_b)
        _seed_export_inputs(hass_b, soc="80.0", export_price="0.30")
        ctrl_b.export_state = ExportState(engaged=True, state_since=BASE - timedelta(hours=1))
        ctrl_b._last_house_load_w = 400.0  # large cache — must NOT affect actuation
        await ctrl_b.tick()
        cached_setpoint = [c for c in act_b.calls if c[0] == "engage_export"][-1][1]

        assert cached_setpoint == pytest.approx(baseline_setpoint, rel=1e-9), (
            "actuation gross setpoint must ignore the telemetry cache when the "
            f"live sensor is None; baseline={baseline_setpoint}, cached={cached_setpoint}"
        )


# ---------------------------------------------------------------------------
# E3-4: per-tick export kWh reflects the real tick cadence (TICK_SECONDS),
#       not a hardcoded 5-minute assumption.
# ---------------------------------------------------------------------------


class TestExportKwhCadence:
    """The per-tick export kWh (and thus PnL) must scale with TICK_SECONDS.

    Regression guard: the ledger previously divided by a hardcoded 12
    ("12 ticks/hour", i.e. a 5-min cadence) while the controller actually
    ticks every TICK_SECONDS (=60s → 60 ticks/hour), inflating recorded
    export_kwh and today_export_pnl_eur by 5x.
    """

    @pytest.mark.asyncio
    async def test_export_kwh_uses_tick_seconds(self):
        """Recorded export_kwh == setpoint_w / 1000 * (TICK_SECONDS / 3600)."""
        hass = _StubHass()
        ctrl, act, _, rec = _make_controller(hass)
        _seed_export_inputs(hass, soc="80.0", export_price="0.30")
        ctrl.export_state = ExportState(
            engaged=True, state_since=BASE - timedelta(hours=1)
        )

        await ctrl.tick()

        export_calls = [c for c in act.calls if c[0] == "engage_export"]
        assert export_calls, f"engage_export must fire; calls={act.calls}"

        row = rec.rows[-1]
        sp = row["export_setpoint_w"]
        assert sp is not None and sp > 0, f"expected positive export setpoint, got {sp!r}"

        expected_kwh = sp / 1000.0 * (const.TICK_SECONDS / 3600.0)
        assert row["export_kwh"] == pytest.approx(expected_kwh, rel=1e-9), (
            f"export_kwh must equal setpoint x TICK_SECONDS/3600 ({expected_kwh}), "
            f"got {row['export_kwh']} — ledger cadence is out of sync with TICK_SECONDS"
        )
