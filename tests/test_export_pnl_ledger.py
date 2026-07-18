"""TDD tests for E3: realized-arbitrage-PnL ledger.

Covers:
- export_pnl_eur: worked-example calculation
- export_pnl_eur: negative PnL when export price is too low
- controller tick: per-interval PnL accumulated into today's total
- controller tick: accumulator resets on local-day rollover
- controller tick: export interval tagged in observability attributes
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, UTC

import pytest

from custom_components.anker_x1_smartgrid import const
from custom_components.anker_x1_smartgrid import controller as ctrl_mod
from custom_components.anker_x1_smartgrid import scheduler as sch_mod
from custom_components.anker_x1_smartgrid.controller import Controller
from custom_components.anker_x1_smartgrid.models import (
    Config,
    ControllerState,
    ExportState,
    PlanState,
    PriceSlot,
)
from custom_components.anker_x1_smartgrid.optimize import compute_water_value, export_pnl_eur
from tests.helpers import (
    CapturingStore as _StubStore,
    StubActuator as _StubActuator,
    StubHass as _StubHass,
    StubRecorder as _StubRecorder,
)


# ---------------------------------------------------------------------------
# Time anchors
# ---------------------------------------------------------------------------

BASE = datetime(2026, 6, 25, 14, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers: StubActuator/StubStore(->CapturingStore)/StubRecorder/StubHass
# imported from tests.helpers above (aliased to the local names used
# throughout this module) — this file's variants were behaviorally
# equivalent (the local _StubStore's capture-last-save behavior matches
# CapturingStore; _StubRecorder's read_decisions was an always-empty stub
# never exercised by production code, so swapping to the real filtering
# implementation is a no-op here). _StubHass and _make_controller are
# imported by tests/test_cash_ledger.py — kept re-exportable by name.
# ---------------------------------------------------------------------------


def _make_export_cfg(**overrides) -> Config:
    defaults = dict(
        capacity_kwh=10.0,
        soc_floor=10.0,
        soc_target=97.0,
        max_charge_w=3000.0,
        max_export_w=3000.0,
        grid_export_limit_w=3000.0,
        eta_charge=1.0,
        round_trip_eff=1.0,  # eta_discharge=1.0 for simple arithmetic
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
        const.CONF_ENT_METER_POWER: "sensor.meter_power",
        const.CONF_ENT_PRICE: "sensor.price",
        const.CONF_ENT_PV_TODAY: [],
        const.CONF_ENT_PV_TOMORROW: [],
        const.CONF_ENT_SUN: "sun.sun",
        const.CONF_ENT_BATTERY_POWER: "sensor.battery_power",
        const.CONF_ENT_PV_POWER: "sensor.pv_power",
        const.CONF_ENT_INVERTER_LOSS: "sensor.inverter_loss",
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
    # R1: meter/battery must reflect an ACTUAL metered export (grid meter
    # negative = exporting, battery positive = discharging) — PnL is now
    # measured (min(meter export, battery discharge)), not derived from the
    # commanded setpoint, so a fixture with meter_power positive (importing)
    # would always metering-export 0 regardless of what the executor commands.
    hass.set_state("sensor.meter_power", "-1500.0")
    hass.set_state("sensor.pv_power", "2000.0")
    hass.set_state("sensor.battery_power", "1800.0")
    hass.set_state("sensor.irradiance", "600.0")
    hass.set_state("weather.home", "sunny", {"temperature": 22.0})
    hass.set_state("sensor.export_price", export_price)
    sunset_iso = (BASE + timedelta(hours=6)).isoformat()
    hass.set_state("sun.sun", "above_horizon", {"next_setting": sunset_iso})
    # Price forecast: current hour expensive (0.30), later hours cheap (0.10).
    # This gives find_next_trough a cheap slot to return, producing a low
    # keep_value so that exporting at 0.30 yields a genuinely positive PnL.
    prices = [0.30, 0.30, 0.10, 0.10, 0.10, 0.10, 0.10, 0.10, 0.10, 0.10, 0.10, 0.10]
    hass.set_state(
        "sensor.price",
        "0.30",
        {
            "forecast": [
                {
                    "datetime": (BASE + timedelta(hours=i)).isoformat(),
                    "electricity_price": int(prices[i] * const.PRICE_SCALE),
                }
                for i in range(12)
            ]
        },
    )


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
        cfg = _make_export_cfg(eta_charge=1.0, round_trip_eff=1.0, cycle_cost_eur_per_kwh=0.04)
        result = export_pnl_eur(
            export_kwh=0.25,
            export_price=0.30,
            keep_value=0.10,
            cfg=cfg,
        )
        assert result == pytest.approx(0.040, abs=1e-9)

    def test_negative_pnl_when_price_too_low(self):
        """Export price below hurdle → negative PnL (cost exceeds revenue)."""
        cfg = _make_export_cfg(eta_charge=1.0, round_trip_eff=1.0, cycle_cost_eur_per_kwh=0.04)
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
        assert len(export_calls) >= 1, f"engage_export must fire when surplus clears hurdle; calls={act.calls}"
        # PnL should be positive (price 0.30 > hurdle with eta=1.0, cost=0.04, keep≈0.10)
        assert ctrl.today_export_pnl_eur > 0.0, (
            f"Expected positive accumulated PnL after export tick, got {ctrl.today_export_pnl_eur}"
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

        assert pnl_after_second >= pnl_after_first, "Accumulated PnL must be non-decreasing across export ticks"

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
            f"today_export_pnl_eur unexpectedly very negative after reset: {ctrl.today_export_pnl_eur}"
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
            hass, cfg_overrides=dict(eta_charge=0.95, round_trip_eff=0.85, cycle_cost_eur_per_kwh=0.0)
        )
        _seed_export_inputs(hass, soc="80.0", export_price="0.30")
        # Zero out opportunity cost so PnL == revenue == AC_metered_kwh * eff_price.
        monkeypatch.setattr(ctrl_mod.optimize_mod, "compute_water_value", lambda *a, **k: 0.0)
        ctrl.export_state = ExportState(engaged=True, state_since=BASE - timedelta(hours=1))
        await ctrl.tick()

        assert [c for c in act.calls if c[0] == "engage_export"], "expected an export tick"
        ac_kwh = rec.rows[-1]["export_kwh"]  # AC metered net the controller exported
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
        ctrl, act, _, rec = _make_controller(hass, cfg_overrides={"enable_export": False})
        _seed_export_inputs(hass, soc="80.0", export_price="0.30")

        await ctrl.tick()

        val = ctrl.last_status.get("today_export_pnl_eur")
        assert val == pytest.approx(0.0), f"today_export_pnl_eur should be 0.0 when export is disabled, got {val!r}"


# ---------------------------------------------------------------------------
# N2 / R1: the ACTUATION gross setpoint (export_load_comp_factor compensation)
# falls back to the cached last-known house load when pv/batt are
# unavailable — that mechanism is unchanged and covered below.  The recorded
# export_kwh / PnL, however, are now MEASURED directly from the live meter +
# battery sensors (R1), so they are independent of both the setpoint and the
# house-load cache: when the battery sensor itself is unavailable, the
# measured battery-sourced export honestly falls back to 0 (safe
# under-count) rather than any cached/derived estimate.
# ---------------------------------------------------------------------------


class TestMeteredNetHouseLoadFallback:
    """C3/N2/R1: cached house load feeds only actuation; PnL/export_kwh are measured."""

    @pytest.mark.asyncio
    async def test_metered_export_kwh_is_zero_when_battery_sensor_unavailable(self):
        """R1: export_kwh/PnL are measured from the meter+battery power sensors,
        not derived from the commanded setpoint or the house-load cache. When
        the battery sensor is unavailable this tick, the measured
        battery-sourced export honestly falls back to 0 — even though the
        ACTUATION gross setpoint still fires normally (house-load N2 cache
        fallback is unaffected by this change; see
        test_actuation_gross_setpoint_ignores_cache below)."""
        hass = _StubHass()
        ctrl, act, _, rec = _make_controller(hass)
        _seed_export_inputs(hass, soc="80.0", export_price="0.30")
        # Battery sensor unavailable this tick -> measured battery-sourced
        # export must be 0, regardless of the cached house load below.
        hass.set_state("sensor.battery_power", "unknown")
        ctrl.export_state = ExportState(engaged=True, state_since=BASE - timedelta(hours=1))
        ctrl._last_house_load_w = 400.0  # cached previous reading — must not matter here

        await ctrl.tick()

        export_calls = [c for c in act.calls if c[0] == "engage_export"]
        assert export_calls, f"expected an export tick (actuation unaffected), calls={act.calls}"
        assert export_calls[-1][1] > 0, "actuation gross setpoint must still fire"

        recorded_kwh = rec.rows[-1]["export_kwh"]
        assert recorded_kwh == pytest.approx(0.0, abs=1e-9), (
            "measured export_kwh must be 0 when the battery sensor is "
            f"unavailable (safe under-count); got {recorded_kwh}"
        )
        assert ctrl.today_export_pnl_eur == pytest.approx(0.0, abs=1e-9), (
            "PnL must not accrue when the measured battery-sourced export is 0"
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
        """R1: recorded export_kwh == metered_export_w / 1000 * (TICK_SECONDS /
        3600), where metered_export_w = min(meter export, battery discharge) —
        the MEASURED battery-sourced export, not the commanded setpoint."""
        hass = _StubHass()
        ctrl, act, _, rec = _make_controller(hass)
        _seed_export_inputs(hass, soc="80.0", export_price="0.30")
        # Concrete metered scenario: 2000W exported at the meter, 2500W battery
        # discharge -> battery-sourced (min) export = 2000W.
        hass.set_state("sensor.meter_power", "-2000.0")
        hass.set_state("sensor.battery_power", "2500.0")
        ctrl.export_state = ExportState(engaged=True, state_since=BASE - timedelta(hours=1))

        await ctrl.tick()

        export_calls = [c for c in act.calls if c[0] == "engage_export"]
        assert export_calls, f"engage_export must fire; calls={act.calls}"

        expected_metered_w = min(2000.0, 2500.0)
        expected_kwh = expected_metered_w / 1000.0 * (const.TICK_SECONDS / 3600.0)
        row = rec.rows[-1]
        assert row["export_kwh"] == pytest.approx(expected_kwh, rel=1e-9), (
            f"export_kwh must equal min(meter export, battery discharge) x "
            f"TICK_SECONDS/3600 ({expected_kwh}), got {row['export_kwh']} — "
            "ledger cadence is out of sync with TICK_SECONDS"
        )


# ---------------------------------------------------------------------------
# R1: live per-tick export PnL is MEASURED (min(meter export, battery
# discharge)), not derived from the commanded setpoint.
# ---------------------------------------------------------------------------


class TestExportPnlMeasuredNotSetpoint:
    """R1-B: PnL/export_kwh track the live meter+battery reading only."""

    @pytest.mark.asyncio
    async def test_pnl_matches_metered_min_rule_and_ignores_setpoint(self, monkeypatch):
        """p1_raw=-2000 (2000W export), batt_raw=1500 (1500W discharge),
        price=0.30 -> pnl == min(2000,1500)/1000 * (TICK_SECONDS/3600) *
        eff_price, regardless of how large the commanded setpoint is (proven
        by wildly inflating export_load_comp_factor)."""
        hass = _StubHass()
        ctrl, act, _, rec = _make_controller(
            hass,
            cfg_overrides=dict(
                cycle_cost_eur_per_kwh=0.0,
                export_load_comp_factor=50.0,  # wildly inflates the commanded setpoint
            ),
        )
        _seed_export_inputs(hass, soc="80.0", export_price="0.30")
        hass.set_state("sensor.meter_power", "-2000.0")
        hass.set_state("sensor.battery_power", "1500.0")
        # Zero out opportunity cost so pnl == revenue == metered_kwh * eff_price.
        monkeypatch.setattr(ctrl_mod.optimize_mod, "compute_water_value", lambda *a, **k: 0.0)
        ctrl.export_state = ExportState(engaged=True, state_since=BASE - timedelta(hours=1))

        await ctrl.tick()

        export_calls = [c for c in act.calls if c[0] == "engage_export"]
        assert export_calls, f"expected an export tick, calls={act.calls}"
        wild_setpoint_w = export_calls[-1][1]
        # Sanity: the inflated load-comp factor really did push the commanded
        # setpoint far above the metered 2000W/1500W scenario -- proving PnL
        # below is disconnected from it.
        assert wild_setpoint_w > 4000.0, f"fixture setpoint not wild enough to prove the point: {wild_setpoint_w}"

        eff_price = ctrl_mod.optimize_mod.effective_export_price(0.30, ctrl.cfg)
        expected_kwh = min(2000.0, 1500.0) / 1000.0 * (const.TICK_SECONDS / 3600.0)
        expected_pnl = expected_kwh * eff_price

        assert rec.rows[-1]["export_kwh"] == pytest.approx(expected_kwh, rel=1e-6)
        assert ctrl.today_export_pnl_eur == pytest.approx(expected_pnl, rel=1e-6), (
            f"PnL must equal the metered min-rule revenue ({expected_pnl}), "
            f"not something derived from the wild setpoint ({wild_setpoint_w}); "
            f"got {ctrl.today_export_pnl_eur}"
        )


# ---------------------------------------------------------------------------
# C3 keep_value anchor: MINIMUM price over the remaining horizon, not the
# next LOCAL price trough (find_next_trough picks the EARLIEST qualifying
# local minimum, which can be far shallower than a deeper refill sitting
# later in the same horizon — the same defect fixed in decision.py's
# terminal water value).
# ---------------------------------------------------------------------------


def _patched_compute_decision_with_terminal(
    export_request: dict,
    *,
    terminal_v_hi: float | None = None,
    terminal_need_kwh: float | None = None,
):
    """Factory: like ``_patched_compute_decision`` (test_controller_export_executor.py)
    but also injects the two-segment terminal keys into ``_out`` when given,
    so the executor's ``_keep_value`` picks up ``terminal_v_hi`` /
    ``terminal_need_kwh`` exactly as the live DP stashes them
    (``decision.compute_decision``). Omitting either leaves the corresponding
    key absent from ``_out``, matching the flag-off / stale-plan case.
    """

    def _stub(
        plan,
        inputs,
        slots,
        pv_remaining,
        sunset,
        predictor,
        cur_temp,
        cfg,
        tomorrow_total=None,
        sun_times=None,
        today_arrays=None,
        tomorrow_arrays=None,
        today_watts=None,
        tomorrow_watts=None,
        export_price=None,
        _out=None,
        _shadow_dp=False,
        export_price_matches_import=False,
        estimated_tomorrow=None,
        past_actuals_by_hour=None,
        **kwargs,
    ):
        if _out is not None:
            _out["export_request"] = export_request
            _out["dp_selected"] = []
            _out["intervals"] = []
            _out["grid_request"] = {}
            if terminal_v_hi is not None:
                _out["terminal_v_hi"] = terminal_v_hi
            if terminal_need_kwh is not None:
                _out["terminal_need_kwh"] = terminal_need_kwh
        passive = PlanState(ControllerState.PASSIVE, inputs.now, ())
        deadline = inputs.now + timedelta(hours=8)
        return passive, 0.0, deadline, [], "water_value", []

    return _stub


class TestExportKeepValueHorizonMinAnchor:
    """The live C3 executor's ``_keep_value`` (opportunity-cost term fed into
    the PnL ledger) must use ``compute_water_value(min(remaining_prices), cfg)``
    — the same anchor as the DP's terminal water value — not
    ``find_next_trough``'s earliest-qualifying local minimum."""

    @pytest.mark.asyncio
    async def test_keep_value_uses_horizon_min_not_local_trough(self, monkeypatch):
        """Shallow local trough at +6h (0.09) vs. a much deeper true minimum
        at +11h (0.02). ``find_next_trough`` (old anchor) picks 0.09, the
        earliest qualifying candidate; the fix must use min(prices) = 0.02.
        """
        hass = _StubHass()
        ctrl, act, _, rec = _make_controller(hass)
        # Freeze time so the price forecast (anchored to BASE) is in-window —
        # matches the C3 executor test harness pattern (test_controller_export_executor.py).
        monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
        _seed_export_inputs(hass, soc="80.0", export_price="0.30")
        prices = [0.30, 0.30, 0.10, 0.10, 0.10, 0.10, 0.09, 0.12, 0.12, 0.12, 0.12, 0.02, 0.10]
        hass.set_state(
            "sensor.price",
            "0.30",
            {
                "forecast": [
                    {
                        "datetime": (BASE + timedelta(hours=i)).isoformat(),
                        "electricity_price": int(prices[i] * const.PRICE_SCALE),
                    }
                    for i in range(len(prices))
                ]
            },
        )
        ctrl.export_state = ExportState(engaged=True, state_since=BASE - timedelta(hours=1))

        await ctrl.tick()

        export_calls = [c for c in act.calls if c[0] == "engage_export"]
        assert export_calls, f"expected an export tick, calls={act.calls}"
        ac_kwh = rec.rows[-1]["export_kwh"]
        assert ac_kwh and ac_kwh > 0

        cfg = ctrl.cfg
        slots = [PriceSlot(BASE + timedelta(hours=i), p) for i, p in enumerate(prices)]
        _, old_trough_price = sch_mod.find_next_trough(BASE, slots, cfg)
        keep_old = compute_water_value(old_trough_price, cfg)
        keep_new = compute_water_value(min(prices), cfg)
        assert old_trough_price == pytest.approx(0.09) and min(prices) == pytest.approx(0.02), (
            "fixture precondition: find_next_trough must disagree with the horizon min"
        )
        assert keep_old != pytest.approx(keep_new), "fixture must actually distinguish the two anchors"

        eff_price = ctrl_mod.optimize_mod.effective_export_price(0.30, cfg)
        # eta_charge=1.0/round_trip_eff=1.0 in _make_export_cfg -> eta_discharge=1.0,
        # so DC kWh == AC kWh and no conversion factor is needed here.
        expected_pnl_new = ac_kwh * eff_price - cfg.cycle_cost_eur_per_kwh * ac_kwh - keep_new * ac_kwh
        expected_pnl_old = ac_kwh * eff_price - cfg.cycle_cost_eur_per_kwh * ac_kwh - keep_old * ac_kwh

        assert ctrl.today_export_pnl_eur == pytest.approx(expected_pnl_new, rel=1e-6), (
            f"today_export_pnl_eur must use the horizon-min keep_value ({keep_new}); "
            f"got {ctrl.today_export_pnl_eur} (expected {expected_pnl_new}; "
            f"old next-local-trough anchor would give {expected_pnl_old})"
        )
        assert ctrl.today_export_pnl_eur != pytest.approx(expected_pnl_old, rel=1e-6), (
            "PnL must not match the stale next-local-trough anchor"
        )

    @pytest.mark.asyncio
    async def test_keep_value_uses_v_hi_below_threshold(self, monkeypatch):
        """SoC within the overnight-need band above the firmware floor ->
        _keep_value must be the DP's terminal_v_hi, not the legacy horizon-min
        water value."""
        monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
        monkeypatch.setattr(ctrl_mod.energy, "ride_out_reserve_kwh", lambda *a, **k: 0.0)
        hass = _StubHass()
        ctrl, act, _, rec = _make_controller(hass, cfg_overrides=dict(soc_floor=0.0))
        # capacity_kwh=10.0 -> firmware_floor_kwh=0.5 (5%); need=1.0 -> threshold
        # is 1.5 kWh (15% SoC). soc=10% (1.0 kWh) sits inside the band.
        _seed_export_inputs(hass, soc="10.0", export_price="0.30")
        cur_h = BASE.replace(minute=0, second=0, microsecond=0)
        monkeypatch.setattr(
            ctrl_mod,
            "compute_decision",
            _patched_compute_decision_with_terminal(
                export_request={cur_h: 3000.0}, terminal_v_hi=0.15, terminal_need_kwh=1.0
            ),
        )
        ctrl.export_state = ExportState(engaged=True, state_since=BASE - timedelta(hours=1))

        await ctrl.tick()

        export_calls = [c for c in act.calls if c[0] == "engage_export"]
        assert export_calls, f"expected an export tick, calls={act.calls}"
        ac_kwh = rec.rows[-1]["export_kwh"]
        assert ac_kwh and ac_kwh > 0

        cfg = ctrl.cfg
        eff_price = ctrl_mod.optimize_mod.effective_export_price(0.30, cfg)
        # eta_charge=1.0/round_trip_eff=1.0 -> eta_discharge=1.0, so DC==AC kWh.
        expected_pnl = ac_kwh * eff_price - cfg.cycle_cost_eur_per_kwh * ac_kwh - 0.15 * ac_kwh
        assert ctrl.today_export_pnl_eur == pytest.approx(expected_pnl, rel=1e-6), (
            f"below-threshold SoC must use terminal_v_hi (0.15) as keep_value; "
            f"got {ctrl.today_export_pnl_eur} (expected {expected_pnl})"
        )

    @pytest.mark.asyncio
    async def test_keep_value_uses_legacy_above_threshold(self, monkeypatch):
        """SoC above the overnight-need band -> _keep_value falls back to the
        legacy compute_water_value(min(remaining_prices), cfg) expression, even
        though the DP stashed terminal keys."""
        monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
        monkeypatch.setattr(ctrl_mod.energy, "ride_out_reserve_kwh", lambda *a, **k: 0.0)
        hass = _StubHass()
        ctrl, act, _, rec = _make_controller(hass, cfg_overrides=dict(soc_floor=0.0))
        # threshold is 1.5 kWh (15% SoC); soc=80% (8.0 kWh) is well above it.
        _seed_export_inputs(hass, soc="80.0", export_price="0.30")
        cur_h = BASE.replace(minute=0, second=0, microsecond=0)
        monkeypatch.setattr(
            ctrl_mod,
            "compute_decision",
            _patched_compute_decision_with_terminal(
                export_request={cur_h: 3000.0}, terminal_v_hi=0.15, terminal_need_kwh=1.0
            ),
        )
        ctrl.export_state = ExportState(engaged=True, state_since=BASE - timedelta(hours=1))

        await ctrl.tick()

        export_calls = [c for c in act.calls if c[0] == "engage_export"]
        assert export_calls, f"expected an export tick, calls={act.calls}"
        ac_kwh = rec.rows[-1]["export_kwh"]
        assert ac_kwh and ac_kwh > 0

        cfg = ctrl.cfg
        # Same forecast as _seed_export_inputs: current hour 0.30, rest 0.10.
        keep_legacy = compute_water_value(0.10, cfg)
        assert keep_legacy != pytest.approx(0.15), "fixture must actually distinguish v_hi from the legacy anchor"
        eff_price = ctrl_mod.optimize_mod.effective_export_price(0.30, cfg)
        expected_pnl = ac_kwh * eff_price - cfg.cycle_cost_eur_per_kwh * ac_kwh - keep_legacy * ac_kwh
        assert ctrl.today_export_pnl_eur == pytest.approx(expected_pnl, rel=1e-6), (
            f"above-threshold SoC must ignore terminal_v_hi and use the legacy anchor ({keep_legacy}); "
            f"got {ctrl.today_export_pnl_eur} (expected {expected_pnl})"
        )

    @pytest.mark.asyncio
    async def test_keep_value_legacy_when_keys_absent(self, monkeypatch):
        """No terminal keys in _dp_out (flag off / stale plan) -> legacy
        expression is used unchanged, regardless of how low SoC is."""
        monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
        monkeypatch.setattr(ctrl_mod.energy, "ride_out_reserve_kwh", lambda *a, **k: 0.0)
        hass = _StubHass()
        ctrl, act, _, rec = _make_controller(hass, cfg_overrides=dict(soc_floor=0.0))
        # Same low SoC as the below-threshold test above -- proves absence of
        # the keys (not SoC position) drives the legacy branch.
        _seed_export_inputs(hass, soc="10.0", export_price="0.30")
        cur_h = BASE.replace(minute=0, second=0, microsecond=0)
        monkeypatch.setattr(
            ctrl_mod,
            "compute_decision",
            _patched_compute_decision_with_terminal(export_request={cur_h: 3000.0}),
        )
        ctrl.export_state = ExportState(engaged=True, state_since=BASE - timedelta(hours=1))

        await ctrl.tick()

        export_calls = [c for c in act.calls if c[0] == "engage_export"]
        assert export_calls, f"expected an export tick, calls={act.calls}"
        ac_kwh = rec.rows[-1]["export_kwh"]
        assert ac_kwh and ac_kwh > 0

        cfg = ctrl.cfg
        keep_legacy = compute_water_value(0.10, cfg)
        eff_price = ctrl_mod.optimize_mod.effective_export_price(0.30, cfg)
        expected_pnl = ac_kwh * eff_price - cfg.cycle_cost_eur_per_kwh * ac_kwh - keep_legacy * ac_kwh
        assert ctrl.today_export_pnl_eur == pytest.approx(expected_pnl, rel=1e-6), (
            f"absent terminal keys must fall back to the legacy anchor ({keep_legacy}) even at low SoC; "
            f"got {ctrl.today_export_pnl_eur} (expected {expected_pnl})"
        )

    @pytest.mark.asyncio
    async def test_dp_scheduled_export_books_nonnegative_pnl(self, monkeypatch):
        """econ-F5/parity-M4 guard: terminal_v_hi already bakes in ``− cycle_cost``
        (wear-symmetric with the export leg's own ``− cycle_cost`` term in
        export_pnl_eur). A DP-committed export priced at exactly the terminal
        parity point (effective_export_price − cycle_cost) must book PnL == 0,
        not a spurious loss of ``-cycle_cost * kwh`` from double-subtracting the
        wear term."""
        monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
        monkeypatch.setattr(ctrl_mod.energy, "ride_out_reserve_kwh", lambda *a, **k: 0.0)
        hass = _StubHass()
        ctrl, act, _, rec = _make_controller(hass, cfg_overrides=dict(soc_floor=0.0))
        _seed_export_inputs(hass, soc="10.0", export_price="0.30")
        cfg = ctrl.cfg
        eff_price = ctrl_mod.optimize_mod.effective_export_price(0.30, cfg)
        terminal_v_hi = eff_price - cfg.cycle_cost_eur_per_kwh
        cur_h = BASE.replace(minute=0, second=0, microsecond=0)
        monkeypatch.setattr(
            ctrl_mod,
            "compute_decision",
            _patched_compute_decision_with_terminal(
                export_request={cur_h: 3000.0}, terminal_v_hi=terminal_v_hi, terminal_need_kwh=1.0
            ),
        )
        ctrl.export_state = ExportState(engaged=True, state_since=BASE - timedelta(hours=1))

        await ctrl.tick()

        export_calls = [c for c in act.calls if c[0] == "engage_export"]
        assert export_calls, f"expected an export tick, calls={act.calls}"
        ac_kwh = rec.rows[-1]["export_kwh"]
        assert ac_kwh and ac_kwh > 0

        assert ctrl.today_export_pnl_eur >= -1e-9, (
            "DP-committed export at the terminal parity price must not book a spurious "
            f"loss; got {ctrl.today_export_pnl_eur}"
        )
        assert ctrl.today_export_pnl_eur == pytest.approx(0.0, abs=1e-9), (
            f"expected exactly zero PnL at the parity price (cycle_cost paid once, not "
            f"double-subtracted between v_hi and export_pnl_eur); got {ctrl.today_export_pnl_eur}"
        )
