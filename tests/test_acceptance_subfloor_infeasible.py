"""Acceptance test §7 — sub-floor start yields infeasible, no crash.

Real (un-mocked) ``optimize_grid`` call with soc_start=4% (below soc_floor=5%)
and an all-expensive (all-above-ceiling) price window must:

* Return a zero schedule (no grid charging).
* Set ``infeasible=True`` in the result.
* Raise no exception.

The controller-level low-SoC WARNING (Acceptance §7) is also asserted via
``compute_decision`` to confirm the silent no-op is observable.

Production code already behaves this way; this test locks the behaviour in.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.anker_x1_smartgrid import const
from custom_components.anker_x1_smartgrid import controller as ctrl_mod
from custom_components.anker_x1_smartgrid.controller import compute_decision
from custom_components.anker_x1_smartgrid.forecast import LoadPredictor
from custom_components.anker_x1_smartgrid.models import (
    Config,
    ControllerState,
    PlantInputs,
    PlanState,
    PriceSlot,
)
from custom_components.anker_x1_smartgrid.optimize import build_charge_mask, optimize_grid

# ---------------------------------------------------------------------------
# Shared constants / helpers
# ---------------------------------------------------------------------------

BASE = datetime(2026, 6, 25, 7, 0, tzinfo=timezone.utc)  # 07:00 UTC

# A price far above any realistic ceiling (peak * round_trip_eff).
# With round_trip_eff=0.85, even a peak of 0.70 gives ceiling ≈ 0.595.
# 0.90 €/kWh is always above ceiling → build_charge_mask returns all-False.
_ALL_EXPENSIVE_PRICE = 0.90  # €/kWh

_PREDICTOR = LoadPredictor.from_profile({})


def _cfg(**overrides) -> Config:
    """Return a Config with sensible test defaults; keyword args override."""
    return Config.from_dict({
        "capacity_kwh": 10.0,
        "soc_floor": 5.0,
        "soc_target": 97.0,
        "max_charge_w": 6000.0,
        "eta_charge": 0.92,
        "eps_hi_kwh": 0.4,
        "eps_lo_kwh": 0.2,
        "min_dwell_min": 0,
        "round_trip_eff": 0.85,
        **overrides,
    })


def _slots(prices: list[float], base: datetime = BASE) -> list[PriceSlot]:
    return [PriceSlot(base + timedelta(hours=i), p) for i, p in enumerate(prices)]


def _plan(state: ControllerState = ControllerState.PASSIVE, age_h: float = 2.0) -> PlanState:
    return PlanState(state, BASE - timedelta(hours=age_h), ())


# ---------------------------------------------------------------------------
# Acceptance §7 — pure optimize_grid (no controller, no mocks)
# ---------------------------------------------------------------------------

class TestSubFloorStartInfeasible:
    """optimize_grid: soc_start < soc_floor, all-expensive window → infeasible."""

    def test_subfloor_start_returns_infeasible_no_exception(self):
        """Real optimize_grid at soc=4% (below floor=5%), all-expensive → infeasible=True.

        The DP clamps sub-floor start states via to_bin (no crash / no div-by-zero).
        With all prices above ceiling the charge mask is all-False → no charging
        transition can occur → the reserve target is unreachable → infeasible=True,
        zero schedule.
        """
        cfg = _cfg(soc_floor=5.0)
        window_len = 9  # arbitrary multi-hour window
        pv    = [0.0] * window_len
        load  = [0.5] * window_len  # small load keeps it realistic
        price = [_ALL_EXPENSIVE_PRICE] * window_len

        # Ceiling = peak * round_trip_eff — all slots exceed this → all-False mask
        ceiling = _ALL_EXPENSIVE_PRICE * cfg.round_trip_eff  # 0.765
        chargeable = build_charge_mask(price, ceiling)
        assert not any(chargeable), "All slots must be masked (all-expensive sanity check)"

        result = optimize_grid(
            pv, load, price,
            soc_start=4.0,          # ← BELOW soc_floor=5%
            cfg=cfg,
            window_start_h=0,
            window_len=window_len,
            chargeable=chargeable,
        )

        # Must not raise (already asserted by reaching here without exception)
        assert result.get("infeasible") is True, (
            f"Expected infeasible=True for sub-floor start with all-expensive prices, "
            f"got result={result}"
        )
        schedule = result.get("schedule", [])
        assert all(s == pytest.approx(0.0) for s in schedule), (
            f"Expected all-zero schedule (no grid charging), got schedule={schedule}"
        )

    def test_subfloor_start_no_exception_raised_peak_unknown(self):
        """optimize_grid with soc_start below floor and peak unknown (all-False mask) must not raise."""
        cfg = _cfg(soc_floor=5.0)
        window_len = 4
        pv    = [0.0] * window_len
        load  = [0.3] * window_len
        price = [_ALL_EXPENSIVE_PRICE] * window_len
        # ceiling=None → all-False (fail-closed semantics)
        chargeable = build_charge_mask(price, ceiling=None)
        assert not any(chargeable), "ceiling=None must produce all-False mask"

        # Must complete without raising
        result = optimize_grid(
            pv, load, price,
            soc_start=4.0,
            cfg=cfg,
            window_start_h=0,
            window_len=window_len,
            chargeable=chargeable,
        )
        assert isinstance(result, dict)
        assert result.get("infeasible") is True

    def test_at_floor_all_expensive_also_infeasible(self):
        """Companion (Acceptance §6): soc_start == soc_floor, all-expensive → infeasible.

        Both the at-floor and sub-floor paths must idle — no force-charge occurs.
        """
        cfg = _cfg(soc_floor=5.0)
        window_len = 9
        pv    = [0.0] * window_len
        load  = [0.5] * window_len
        price = [_ALL_EXPENSIVE_PRICE] * window_len
        ceiling = _ALL_EXPENSIVE_PRICE * cfg.round_trip_eff
        chargeable = build_charge_mask(price, ceiling)

        result = optimize_grid(
            pv, load, price,
            soc_start=5.0,          # exactly at floor
            cfg=cfg,
            window_start_h=0,
            window_len=window_len,
            chargeable=chargeable,
        )

        assert result.get("infeasible") is True, (
            f"At-floor with all-expensive prices must also be infeasible, got result={result}"
        )
        schedule = result.get("schedule", [])
        assert all(s == pytest.approx(0.0) for s in schedule), (
            f"Expected zero schedule at-floor, got schedule={schedule}"
        )


# ---------------------------------------------------------------------------
# Minimal stubs for Controller.tick() tests (no HA runtime needed)
# ---------------------------------------------------------------------------

class _StubActuator:
    def __init__(self):
        self.last_setpoint_w = 0.0
        self.engaged = False
    async def engage_and_charge(self, setpoint_w: float) -> None:
        self.last_setpoint_w = setpoint_w
        self.engaged = True
    async def release_to_self(self) -> None:
        self.last_setpoint_w = 0.0
        self.engaged = False


class _StubStore:
    async def async_save(self, data) -> None:
        pass


class _StubRecorder:
    def __init__(self):
        self.rows: list = []
        self.decision_rows: list = []
        self.daily_regret_rows: dict = {}

    def append(self, row):
        self.rows.append(row)

    def append_decision(self, **kwargs):
        self.decision_rows.append(kwargs)

    def purge_older_than(self, ts, days):
        pass

    def purge_decisions_older_than(self, cutoff_iso):
        return 0

    def rollup_hours(self, now_iso):
        return 0

    def purge_hourly_older_than(self, cutoff_iso):
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

    def upsert_daily_regret(self, **kwargs):
        day = kwargs["day"]
        self.daily_regret_rows[day] = kwargs

    def read_latest_daily_regret(self):
        return None

    def read_daily_regret_range(self, since_day, until_day=None):
        return []


class _StubHass:
    """Minimal hass stub seeded with price + SoC states for tick()."""

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


def _make_floor_controller(hass, *, soc_pct: float = 5.0):
    """Build a Controller pre-seeded with all-expensive prices and SoC at the floor."""
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
        "soc_floor": 5.0,
        "capacity_kwh": 10.0,
        "soc_target": 97.0,
        "max_charge_w": 6000.0,
        "eta_charge": 0.92,
        "round_trip_eff": 0.85,
    }
    act = _StubActuator()
    rec = _StubRecorder()
    ctrl = ctrl_mod.Controller(
        hass=hass,
        data=data,
        recorder=rec,
        actuator=act,
        store=_StubStore(),
    )
    # Seed states
    hass.set_state("sensor.soc", str(soc_pct))
    hass.set_state("sensor.meter_power", "0.0")
    hass.set_state("sensor.pv_power", "0.0")
    hass.set_state("sensor.battery_power", "0.0")
    hass.set_state("sensor.inverter_loss", "0.0")
    hass.set_state("sensor.irradiance", "0.0")
    hass.set_state("weather.home", "cloudy", {"temperature": 15.0})
    sunset_iso = (BASE + timedelta(hours=8)).isoformat()
    hass.set_state("sun.sun", "above_horizon", {"next_setting": sunset_iso})
    # All-expensive price: 0.90 €/kWh → all above ceiling → no charging worthy
    hass.set_state("sensor.price", str(_ALL_EXPENSIVE_PRICE), {
        "forecast": [
            {
                "datetime": (BASE + timedelta(hours=i)).isoformat(),
                "electricity_price": int(_ALL_EXPENSIVE_PRICE * const.PRICE_SCALE),
            }
            for i in range(9)
        ]
    })
    return ctrl, act


# ---------------------------------------------------------------------------
# Acceptance §7 — controller-level WARNING: edge-triggered, enabled path only
# ---------------------------------------------------------------------------

class TestDrainedToFloorWarning:
    """Controller.tick() emits WARNING once when SoC first hits the floor (enabled path)."""

    @pytest.mark.asyncio
    async def test_warns_once_when_drained_to_floor(self, caplog):
        """Two consecutive ENABLED ticks at soc=5% (floor) → exactly ONE warning.

        The edge-trigger fires on the first tick (transition into at-floor state)
        and is suppressed on the second tick (already warned, flag still set).
        """
        hass = _StubHass()
        ctrl, _ = _make_floor_controller(hass, soc_pct=5.0)

        with caplog.at_level(logging.WARNING, logger="custom_components.anker_x1_smartgrid.controller"):
            await ctrl.tick()  # tick 1: transitions INTO drained-at-floor → warns
            await ctrl.tick()  # tick 2: already warned → suppressed

        floor_warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "firmware floor" in r.message
        ]
        assert len(floor_warnings) == 1, (
            f"Expected exactly 1 drained-to-floor WARNING across 2 ticks, "
            f"got {len(floor_warnings)}: {[r.message for r in floor_warnings]}"
        )
        # Verify message wording
        msg = floor_warnings[0].message
        assert "5.0%" in msg or "5.%" in msg, f"SoC not in message: {msg!r}"
        assert "floor" in msg.lower(), f"'floor' not in message: {msg!r}"

    @pytest.mark.asyncio
    async def test_warns_again_after_recovery(self, caplog):
        """Warns on entry, silences during episode, warns again after SoC recovers."""
        hass = _StubHass()
        ctrl, _ = _make_floor_controller(hass, soc_pct=5.0)

        with caplog.at_level(logging.WARNING, logger="custom_components.anker_x1_smartgrid.controller"):
            # Episode 1: at floor — warns on first tick
            await ctrl.tick()
            # Simulate SoC recovery above floor (e.g. PV recharged overnight)
            hass.set_state("sensor.soc", "30.0")
            await ctrl.tick()  # re-arm (no warning — above floor)
            # Episode 2: drained again
            hass.set_state("sensor.soc", "5.0")
            await ctrl.tick()  # new entry → warns again

        floor_warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "firmware floor" in r.message
        ]
        assert len(floor_warnings) == 2, (
            f"Expected 2 drained-to-floor WARNINGs (one per episode), "
            f"got {len(floor_warnings)}: {[r.message for r in floor_warnings]}"
        )

    @pytest.mark.asyncio
    async def test_no_warning_on_disabled_path(self, caplog):
        """Disabled controller: shadow path MUST NOT emit the drained-to-floor WARNING."""
        hass = _StubHass()
        ctrl, _ = _make_floor_controller(hass, soc_pct=5.0)
        ctrl.enabled = False  # shadow/disabled path

        with caplog.at_level(logging.WARNING, logger="custom_components.anker_x1_smartgrid.controller"):
            await ctrl.tick()
            await ctrl.tick()

        floor_warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "firmware floor" in r.message
        ]
        assert len(floor_warnings) == 0, (
            f"Disabled path must NOT emit drained-to-floor WARNING, "
            f"got {len(floor_warnings)}: {[r.message for r in floor_warnings]}"
        )

    @pytest.mark.asyncio
    async def test_dp_infeasible_still_recorded(self, caplog):
        """_out['dp_infeasible'] is recorded as False (economic-only: never floor-driven).

        Under the economic-only redesign (A1), compute_decision uses water_value terminal
        mode.  In water_value mode dp_infeasible is NEVER set True for floor/sub-floor
        scenarios — the DP accepts drain-to-firmware-floor as a valid outcome and prices
        below-floor house load as direct grid imports (floor_import_cost accounting).
        Reserve-TARGET unreachability (the only remaining infeasible signal) cannot fire
        in water_value mode.

        The controller still stays PASSIVE because all prices exceed the ceiling gate
        (no chargeable slot is available).
        """
        cfg = _cfg(soc_floor=5.0)
        slots = _slots([_ALL_EXPENSIVE_PRICE] * 9)
        sunset = BASE + timedelta(hours=8)
        inputs = PlantInputs(soc=5.0, meter_w=0.0, now=BASE)
        _out: dict = {}

        new_plan, *_ = compute_decision(
            _plan(), inputs, slots, 0.0, sunset,
            _PREDICTOR, None, cfg,
            _out=_out,
        )

        # Economic-only (A1): water_value terminal mode → dp_infeasible=False.
        # Floor-driven infeasibility is retired; battery rides to firmware floor.
        assert _out.get("dp_infeasible") is False, (
            f"compute_decision must record dp_infeasible=False in water_value mode; _out={_out}"
        )
        assert new_plan.state is ControllerState.PASSIVE, (
            f"Expected PASSIVE at floor with all-expensive prices, got {new_plan.state}"
        )
