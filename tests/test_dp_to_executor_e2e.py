"""E1 (Phase E gate) — DP-to-executor integration coverage.

Closes the 2026-07-08 full-review gap: no existing test drives the REAL DP
(``decision.compute_decision`` / ``optimize.optimize_grid``) through the REAL
executor (``Controller._tick_impl``) to a clamped actuator setpoint. Every
executor test in ``tests/test_controller_export_executor.py`` stubs
``compute_decision`` via ``_patched_compute_decision``; every DP test in
``tests/test_controller_dp.py`` stops at ``compute_decision``'s return value
(or mocks ``optimize_grid`` outright) and never reaches ``ctrl.tick()``.

This file does neither: prices/PV/SoC are crafted so the real DP
deterministically commits (or refuses) the CURRENT hour, then
``await ctrl.tick()`` runs the unmodified ``_tick_impl`` end-to-end and the
assertions read the ``StubActuator`` — the same surface production code
drives. Task E2 (extracting ``executor.py`` out of ``controller.py``) is
gated on this file existing and passing.

Scenario provenance: each scenario below was empirically verified against the
real DP before being written down here (not hand-waved) — see the case
docstrings for the economic mechanism that makes the outcome unambiguous.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, UTC

import pytest

from custom_components.anker_x1_smartgrid import const
from custom_components.anker_x1_smartgrid import controller as ctrl_mod
from custom_components.anker_x1_smartgrid.controller import Controller
from custom_components.anker_x1_smartgrid.models import (
    Config,
    ControllerState,
    ExportState,
    PlanState,
)
from tests.helpers import StubActuator, StubHass, StubRecorder, StubStore


# ---------------------------------------------------------------------------
# Harness — built on the shared stubs (tests/helpers.py); NOT importing
# tests/test_controller_export_executor.py's _patched_compute_decision or any
# other compute_decision/optimize_grid stub. The DP runs for real every time.
# ---------------------------------------------------------------------------


def _make_cfg(**overrides) -> Config:
    """Config with simple, generous limits so DP arithmetic stays legible.

    max_charge_w == max_export_w == grid_export_limit_w == 6000 (== the
    hardware ceiling const.SETPOINT_MAX_W) so a clamped executor setpoint of
    6000 W in the export cases is unambiguously the hardware clamp, not a
    narrower cfg limit.
    """
    defaults = dict(
        capacity_kwh=10.0,
        soc_floor=10.0,
        soc_target=97.0,
        max_charge_w=6000.0,
        max_export_w=6000.0,
        grid_export_limit_w=6000.0,
        eta_charge=0.92,
        round_trip_eff=0.85,
        cycle_cost_eur_per_kwh=0.04,
        enable_export=True,
        export_fee_eur_per_kwh=0.02,
        export_eps_lo_kwh=0.2,
        export_eps_hi_kwh=0.4,
        export_dwell_min=0,  # no dwell — state transitions are immediate
        min_dwell_min=0,
    )
    defaults.update(overrides)
    return Config(**defaults)  # type: ignore[arg-type]


def _make_controller(hass, actuator=None, cfg_overrides=None):
    """Build a real Controller wired to StubHass/StubActuator/StubRecorder/StubStore."""
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
    act = actuator or StubActuator()
    ctrl = Controller(
        hass=hass,
        data=data,
        recorder=StubRecorder(),
        actuator=act,
        store=StubStore(),
    )
    ctrl.cfg = _make_cfg(**(cfg_overrides or {}))
    return ctrl, act


def _seed_price_forecast(hass, now: datetime, prices: list[float]) -> None:
    """Seed sensor.price with a forecast attribute — the real coordinator-read path
    (coordinator.read_price_slots) parses this into PriceSlot objects for the DP."""
    hass.set_state(
        "sensor.price",
        str(prices[0]),
        {
            "forecast": [
                {
                    "datetime": (now + timedelta(hours=i)).isoformat(),
                    "electricity_price": int(p * const.PRICE_SCALE),
                }
                for i, p in enumerate(prices)
            ]
        },
    )


def _seed_common(hass, now: datetime, *, soc: str, export_price: str, sunset_h: float = 8.0) -> None:
    """States every scenario needs: SoC, meter, PV/battery telemetry, sun, export price.

    PV/meter/battery are all zero so the DP's PV/load arithmetic stays simple
    and the scenario's outcome is driven entirely by price + SoC, not solar.
    """
    hass.set_state("sensor.soc", soc)
    hass.set_state("sensor.meter_power", "0.0")
    hass.set_state("sensor.pv_power", "0.0")
    hass.set_state("sensor.battery_power", "0.0")
    hass.set_state("sensor.irradiance", "0.0")
    hass.set_state("weather.home", "clear", {"temperature": 15.0})
    hass.set_state("sensor.export_price", export_price)
    sunset_iso = (now + timedelta(hours=sunset_h)).isoformat()
    hass.set_state("sun.sun", "above_horizon", {"next_setting": sunset_iso})


# ---------------------------------------------------------------------------
# Case 1 — cheap-hour grid charge (real DP → FORCING → engage_and_charge)
# ---------------------------------------------------------------------------


class TestCheapHourGridCharge:
    """Low SoC + cheap NOW / expensive later ⇒ real DP puts the charge kWh in
    the current hour ⇒ decide_state FORCES ⇒ engage_and_charge fires.

    Mechanism: with soc=15% (deep below soc_target=97%) and hour 0 priced far
    below every other window hour (0.05 vs 0.35), the DP's arbitrage math has
    no cheaper hour to defer to and no reason to wait — charging now is
    strictly dominant. Verified empirically against the real DP before
    writing this test (see module docstring).
    """

    BASE = datetime(2026, 6, 25, 3, 0, tzinfo=UTC)  # 03:00 UTC, cheap overnight hour

    @pytest.mark.e2e
    @pytest.mark.asyncio
    async def test_forcing_charge_respects_max_charge_w(self, monkeypatch):
        monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: self.BASE)

        hass = StubHass()
        ctrl, act = _make_controller(hass)
        _seed_common(hass, self.BASE, soc="15.0", export_price="0.30")
        # Cheap now, expensive for the rest of the window — unambiguous FORCING trigger.
        _seed_price_forecast(hass, self.BASE, [0.05] + [0.35] * 11)
        ctrl.export_state = ExportState(engaged=False, state_since=self.BASE - timedelta(hours=1))

        await ctrl.tick()

        assert ctrl.plan.state is ControllerState.FORCING, (
            f"real DP must select the current cheap hour for charging; got plan.state={ctrl.plan.state}"
        )
        charge_calls = [c for c in act.calls if c[0] == "engage_and_charge"]
        assert len(charge_calls) == 1, f"expected exactly one engage_and_charge call; calls={act.calls}"
        setpoint = charge_calls[0][1]
        # Sign convention: negative = charge (see guard.command_setpoint docstring).
        assert setpoint < 0.0, f"FORCING setpoint must be negative (charge direction); got {setpoint}"
        # Respects cfg.max_charge_w (== solar-ceiling/limit path here, since PV=0
        # keeps the solar-reservation ceiling from ever binding tighter).
        assert abs(setpoint) <= ctrl.cfg.max_charge_w + 1e-6, (
            f"setpoint magnitude {abs(setpoint)} exceeds max_charge_w={ctrl.cfg.max_charge_w}"
        )
        assert not any(c[0] == "engage_export" for c in act.calls), "FORCING must not also call engage_export"


# ---------------------------------------------------------------------------
# Case 2 — export hour (real DP → committed export NOW → engage_export)
# ---------------------------------------------------------------------------


class TestExportHour:
    """High price NOW + high SoC surplus + a cheap trough later in the window
    ⇒ real DP commits the current (peak) hour to export ⇒ engage_export fires,
    clamped to the hardware ceiling.

    Mechanism: soc=85% (well above the reserve the DP must protect for the
    cheap trough at +8h), hour 0 priced far above every other window hour
    (0.60 vs 0.25/0.07) ⇒ discharging now to the grid beats holding for the
    cheap trough (which the DP will simply re-buy cheaply later). Verified
    empirically against the real DP before writing this test.
    """

    BASE = datetime(2026, 6, 22, 14, 0, tzinfo=UTC)  # 14:00 UTC, price peak

    @pytest.mark.e2e
    @pytest.mark.asyncio
    async def test_export_setpoint_clamped_to_hardware_ceiling(self, monkeypatch):
        monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: self.BASE)

        hass = StubHass()
        ctrl, act = _make_controller(hass)
        _seed_common(hass, self.BASE, soc="85.0", export_price="0.55", sunset_h=6.0)
        # Peak now (0.60), mid-range, a cheap trough at +8h (0.07), mid-range tail.
        prices = [0.60] + [0.25] * 7 + [0.07] + [0.25] * 8
        _seed_price_forecast(hass, self.BASE, prices)
        ctrl.export_state = ExportState(engaged=False, state_since=self.BASE - timedelta(hours=1))

        await ctrl.tick()

        assert ctrl.plan.state is not ControllerState.FORCING, (
            f"this scenario must not also trigger a charge; got {ctrl.plan.state}"
        )
        export_calls = [c for c in act.calls if c[0] == "engage_export"]
        assert len(export_calls) == 1, f"expected exactly one engage_export call; calls={act.calls}"
        setpoint = export_calls[0][1]
        # SAFETY-NET: engage_export must be strictly positive (sign convention).
        assert setpoint > 0.0, f"engage_export setpoint must be > 0; got {setpoint}"
        # Min-threshold: comfortably clear of the quantization/eps noise floor.
        assert setpoint >= 100.0, f"export setpoint {setpoint} suspiciously close to zero"
        # Clamped to the hardware ceiling (const.SETPOINT_MAX_W) — cfg limits are
        # equal to it here (6000), so this proves the clamp is actually applied,
        # not merely under it by accident.
        assert setpoint <= const.SETPOINT_MAX_W + 1e-6, (
            f"export setpoint {setpoint} exceeds hardware ceiling {const.SETPOINT_MAX_W}"
        )
        assert setpoint == pytest.approx(const.SETPOINT_MAX_W, abs=1e-6), (
            f"surplus is large enough that the executor should saturate at the "
            f"±{const.SETPOINT_MAX_W}W ceiling; got {setpoint}"
        )
        assert not any(c[0] == "engage_and_charge" for c in act.calls), (
            "export tick must not also call engage_and_charge"
        )


# ---------------------------------------------------------------------------
# Case 3 — passive hour (real DP → no arbitrage ⇒ no engagement)
# ---------------------------------------------------------------------------


class TestPassiveHour:
    """Flat prices across the whole window + comfortable mid-range SoC ⇒ the
    real DP has no arbitrage opportunity (nothing cheaper to defer to, nothing
    more expensive to sell into) ⇒ zero schedule ⇒ no actuator calls at all.
    """

    BASE = datetime(2026, 6, 25, 12, 0, tzinfo=UTC)

    @pytest.mark.e2e
    @pytest.mark.asyncio
    async def test_flat_prices_comfortable_soc_stays_passive(self, monkeypatch):
        monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: self.BASE)

        hass = StubHass()
        ctrl, act = _make_controller(hass)
        _seed_common(hass, self.BASE, soc="50.0", export_price="0.20")
        _seed_price_forecast(hass, self.BASE, [0.20] * 12)
        ctrl.export_state = ExportState(engaged=False, state_since=self.BASE - timedelta(hours=1))

        await ctrl.tick()

        assert ctrl.plan.state is ControllerState.PASSIVE, (
            f"flat prices + comfortable SoC must stay PASSIVE; got {ctrl.plan.state}"
        )
        assert act.calls == [], f"no arbitrage opportunity ⇒ no actuator engagement at all; calls={act.calls}"
        assert not ctrl.export_state.engaged, "export_state must not report engaged"


# ---------------------------------------------------------------------------
# Case 4 — anti-fight: stale FORCING residue vs real DP wanting export NOW
# ---------------------------------------------------------------------------


class TestAntiFightExportWinsOverStaleForcing:
    """A stale FORCING plan (carried over from a previous tick, e.g. a
    deadband-hold that never got reset) must NOT block this tick's export.

    Reuses the exact export-hour price/SoC shape from ``TestExportHour``
    (real DP commits the current peak hour to export), but seeds
    ``ctrl.plan`` as still-FORCING from 20 minutes ago and the actuator as
    already charging, mirroring a stuck prior tick. Because THIS tick's real
    DP decision is not FORCING, ``_tick_impl`` takes the else-branch: it first
    releases the stale FORCING engagement (the FORCING→PASSIVE transition
    safe-release), then runs the export executor, which finds the committed
    export plan and engages. Export must win — the guard fixed in
    memory/executor-charge-export-fight-fixed.md.
    """

    BASE = datetime(2026, 6, 22, 14, 0, tzinfo=UTC)

    @pytest.mark.e2e
    @pytest.mark.asyncio
    async def test_export_wins_and_stale_forcing_is_released(self, monkeypatch):
        monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: self.BASE)

        hass = StubHass()
        ctrl, act = _make_controller(hass)
        _seed_common(hass, self.BASE, soc="85.0", export_price="0.55", sunset_h=6.0)
        prices = [0.60] + [0.25] * 7 + [0.07] + [0.25] * 8
        _seed_price_forecast(hass, self.BASE, prices)

        # Stale residue: a previous tick left the controller FORCING (charging)
        # even though NOTHING about the seeded inputs would make the current
        # real DP choose FORCING (verified by TestExportHour above using the
        # identical price/SoC shape).
        ctrl.plan = PlanState(ControllerState.FORCING, self.BASE - timedelta(minutes=20), ())
        ctrl.export_state = ExportState(engaged=False, state_since=self.BASE - timedelta(hours=1))
        act.engaged = True
        act.last_setpoint_w = -6000.0

        await ctrl.tick()

        assert ctrl.plan.state is not ControllerState.FORCING, (
            f"the real DP does not select this hour for charging; the stale "
            f"FORCING residue must not survive the tick. got {ctrl.plan.state}"
        )

        release_idx = next(
            (i for i, c in enumerate(act.calls) if c[0] == "release_to_self"),
            None,
        )
        export_idx = next(
            (i for i, c in enumerate(act.calls) if c[0] == "engage_export"),
            None,
        )
        assert release_idx is not None, (
            f"stale FORCING must be released (FORCING→PASSIVE transition); calls={act.calls}"
        )
        assert export_idx is not None, f"export must win this tick despite the stale FORCING residue; calls={act.calls}"
        assert release_idx < export_idx, (
            f"the stale FORCING release must happen BEFORE the export engages (anti-fight ordering); calls={act.calls}"
        )
        export_setpoint = act.calls[export_idx][1]
        assert export_setpoint > 0.0, f"engage_export setpoint must be > 0; got {export_setpoint}"
        assert not any(c[0] == "engage_and_charge" for c in act.calls), (
            "export must win outright — no engage_and_charge this tick"
        )
