"""T9 — executor/scheduler slot lookup + committed-state clear on latched change.

Covers:
- scheduler.decide_state resolves quarter-slot membership at slot_minutes=15,
  and stays byte-identical to the legacy hour-membership check at 60 (given brief).
- The anti-fight guard (compute_decision) still fires at 15-min (given brief,
  weak/inline check kept for cheap coverage).
- GENUINE regression: the edge-hysteresis `live_grid_request` lookup must scale
  back by dt_h to recover per-slot kWh (T7 changed live_grid_request to hold
  average W, not per-slot kWh) — this is the mandatory follow-up fix from the
  T7 review, verified here against the REAL compute_decision path.
- GENUINE regression: the anti-fight guard's own `cur_h` must be slot-floored
  (not hour-floored) or it silently stops firing at sub-hour resolution outside
  the hour's first slot — an additional fix beyond the literal brief (see
  report for rationale), verified against the REAL compute_decision path.
- Committed persisted state (self.plan.committed_slots/committed_charge_kwh) is
  cleared ONLY when the controller's LATCHED slot_minutes changes tick-to-tick,
  never on a stable resolution (parity-critical at 60).
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from custom_components.anker_x1_smartgrid import const
from custom_components.anker_x1_smartgrid import controller as ctrl_mod
from custom_components.anker_x1_smartgrid import scheduler
from custom_components.anker_x1_smartgrid.controller import Controller
from custom_components.anker_x1_smartgrid.models import (
    Config, ControllerState, PlanState, PlantInputs, PriceSlot,
)

UTC = timezone.utc


# ---------------------------------------------------------------------------
# §1 — scheduler.decide_state slot-membership (given brief, verbatim)
# ---------------------------------------------------------------------------


def test_decide_state_resolves_quarter_slot_membership():
    cfg = Config(min_dwell_min=0)
    now = datetime(2026, 8, 1, 10, 37, tzinfo=UTC)
    selected = [datetime(2026, 8, 1, 10, 30, tzinfo=UTC)]      # 10:30 quarter
    plan = PlanState(ControllerState.PASSIVE, now - timedelta(hours=1), ())
    out = scheduler.decide_state(plan, soc=50.0, now=now, selected_slots=selected,
                                  cfg=cfg, slot_minutes=15)
    assert out.state is ControllerState.FORCING


def test_decide_state_60min_membership_unchanged():
    cfg = Config(min_dwell_min=0)
    now = datetime(2026, 8, 1, 10, 37, tzinfo=UTC)
    selected = [datetime(2026, 8, 1, 10, 0, tzinfo=UTC)]
    plan = PlanState(ControllerState.PASSIVE, now - timedelta(hours=1), ())
    out = scheduler.decide_state(plan, soc=50.0, now=now, selected_slots=selected, cfg=cfg)
    assert out.state is ControllerState.FORCING


# ---------------------------------------------------------------------------
# §2 — anti-fight guard, weak/inline check (given brief, verbatim; kept cheap)
# ---------------------------------------------------------------------------


def test_anti_fight_guard_still_fires_at_15min():
    # A current 15-min slot both selected (stale hysteresis) and export-committed
    # is dropped from selected (export wins).  Modeled on test_controller_hysteresis.
    from custom_components.anker_x1_smartgrid import resolution
    now = datetime(2026, 8, 1, 18, 22, tzinfo=UTC)
    cur = resolution.floor_to_slot(now, 15)                 # 18:15
    selected = [cur]
    export_req = {cur: 3000.0}
    eps = 0.01 * 1000.0 / 0.25
    assert cur in selected and export_req.get(cur, 0.0) > eps
    selected = [h for h in selected if h != cur]             # guard result
    assert cur not in selected


# ---------------------------------------------------------------------------
# §3 — compute_decision-level GENUINE regressions (real path, DP mocked)
# ---------------------------------------------------------------------------


class _FlatPredictor:
    def predict(self, start, temp, fallback, quantile=0.5):
        return 300.0


def _slots(now, prices):
    base = now.replace(minute=0, second=0, microsecond=0)
    return [PriceSlot(base + timedelta(hours=i), p) for i, p in enumerate(prices)]


def _dp_charge_first_slot(kwh_first_slot):
    """Mock optimize_grid: charge `kwh_first_slot` kWh in window index 0, else idle."""
    def _dp(*args, **kwargs):
        wl = kwargs.get("window_len", len(args[0]) if args else 1)
        return {
            "schedule": [kwh_first_slot] + [0.0] * (wl - 1),
            "export_schedule": [0.0] * wl,
            "kwh": 0.0,
            "eur": 0.0,
        }
    return _dp


def _dp_zero_charge_committed_export(*args, **kwargs):
    """Mock optimize_grid: 0 charge everywhere, current slot committed to export."""
    wl = kwargs.get("window_len", len(args[0]) if args else 1)
    return {
        "schedule": [0.0] * wl,
        "export_schedule": [1.0] + [0.0] * (wl - 1),
        "kwh": 0.0,
        "eur": 0.0,
    }


def test_hysteresis_recovers_per_slot_kwh_not_hour_kwh():
    """T7-review follow-up: live_grid_request holds average W (T7); the edge-
    hysteresis lookup must scale back by dt_h to recover per-slot AC kWh.

    `now` sits exactly on an hour boundary so the slot-floored `cur_h` (02:00)
    equals the hour-floor too — isolating this test to the dt_h scaling bug
    only (not the separate cur_h slot-floor fix, covered below).  The DP
    commits 0.5 kWh in the first 15-min slot -> live_grid_request[02:00] =
    0.5 * 1000 / 0.25 = 2000 W (T7's average-W conversion).  Un-scaled
    (pre-fix) code recovers 2000/1000 = 2.0 kWh -- 4x too much energy for a
    15-min slot.  Fixed code recovers 2000/1000 * 0.25 = 0.5 kWh (identity).
    """
    now = datetime(2026, 8, 1, 2, 0, tzinfo=UTC)
    prices = [0.30] * 8 + [0.08] + [0.30] * 6
    slots = _slots(now, prices)
    plan = PlanState(ControllerState.PASSIVE, now - timedelta(hours=2), (),
                      committed_charge_kwh=0.0)

    with patch(
        "custom_components.anker_x1_smartgrid.optimize.optimize_grid",
        side_effect=_dp_charge_first_slot(0.5),
    ):
        new_plan, *_ = ctrl_mod.compute_decision(
            plan=plan,
            inputs=PlantInputs(soc=30.0, meter_w=0.0, now=now),
            slots=slots, pv_remaining=0.0, sunset=now + timedelta(hours=2),
            predictor=_FlatPredictor(), cur_temp=10.0,
            cfg=Config(end_soc_deadband=0.25, min_dwell_min=0),
            slot_minutes=15,
        )

    assert new_plan.committed_charge_kwh == pytest.approx(0.5), (
        f"expected 0.5 kWh (2000W * 0.25h), got {new_plan.committed_charge_kwh!r} "
        "-- looks like the dp_cur_kwh /1000 lookup is missing the * dt_h scale-back"
    )
    assert new_plan.committed_charge_kwh != pytest.approx(2.0)


def test_hysteresis_recovers_per_slot_kwh_not_hour_kwh_fails_without_dt_h_fix():
    """Same scenario, asserted directly against the UN-scaled formula so the
    test file documents (and pins) the exact pre-fix failure mode: this is
    NOT tautological -- it independently recomputes what the buggy code would
    have produced and shows it differs from the fixed code's real output.
    """
    now = datetime(2026, 8, 1, 2, 0, tzinfo=UTC)
    prices = [0.30] * 8 + [0.08] + [0.30] * 6
    slots = _slots(now, prices)
    plan = PlanState(ControllerState.PASSIVE, now - timedelta(hours=2), (),
                      committed_charge_kwh=0.0)

    with patch(
        "custom_components.anker_x1_smartgrid.optimize.optimize_grid",
        side_effect=_dp_charge_first_slot(0.5),
    ):
        new_plan, *_ = ctrl_mod.compute_decision(
            plan=plan,
            inputs=PlantInputs(soc=30.0, meter_w=0.0, now=now),
            slots=slots, pv_remaining=0.0, sunset=now + timedelta(hours=2),
            predictor=_FlatPredictor(), cur_temp=10.0,
            cfg=Config(end_soc_deadband=0.25, min_dwell_min=0),
            slot_minutes=15,
        )

    pre_fix_buggy_value = 2000.0 / 1000.0  # what the un-scaled `/1000.0` alone yields
    assert new_plan.committed_charge_kwh != pytest.approx(pre_fix_buggy_value)


def test_anti_fight_guard_slot_floors_cur_h_mid_hour():
    """Additional fix beyond the literal brief (see report): the anti-fight
    guard's own `cur_h = now_h` reused compute_decision's HOUR-floored `now_h`
    (unaffected by T4, which only slot-floored `_dp_select_slots`'s internal
    scope). At `now` mid-hour and slot_minutes=15, `selected`/
    `live_export_request` are keyed on the SLOT grid starting at the CURRENT
    slot -- the hour-floor key predates the DP's window and is simply absent,
    so `cur_h in selected` / `live_export_request.get(cur_h, ...)` silently
    misses and the guard never fires outside the hour's first quarter.  This
    reproduces the exact live "W"-shaped-SoC scenario from commit b75f85f at
    15-min resolution: without the slot-floor fix, decide_state flips to
    FORCING and grid-charges straight through a genuine committed export.
    """
    from custom_components.anker_x1_smartgrid import resolution
    now = datetime(2026, 8, 1, 18, 22, tzinfo=UTC)   # mid-hour: hour-floor 18:00 != slot-floor 18:15
    prices = [0.30] * 8 + [0.08] + [0.30] * 6
    slots = _slots(now, prices)
    # Stale previous-tick commitment: small, but within end_soc_deadband (0.25)
    # of the fresh DP's 0 kWh charge -- triggers the hysteresis re-injection
    # branch, re-adding the CURRENT SLOT (18:15) to `selected`.
    # committed_charge_slot bound to the CURRENT slot (18:15, review 1.3):
    # keeps this an intra-slot commit so the deadband-hold still re-injects
    # cur_h and the anti-fight guard's removal branch actually runs.
    plan = PlanState(
        ControllerState.PASSIVE, now - timedelta(hours=2), (),
        committed_charge_kwh=0.1,
        committed_charge_slot=resolution.floor_to_slot(now, 15),
    )

    with patch(
        "custom_components.anker_x1_smartgrid.optimize.optimize_grid",
        side_effect=_dp_zero_charge_committed_export,
    ):
        new_plan, setpoint, *_ = ctrl_mod.compute_decision(
            plan=plan,
            inputs=PlantInputs(soc=30.0, meter_w=0.0, now=now),
            slots=slots, pv_remaining=0.0, sunset=now + timedelta(hours=2),
            predictor=_FlatPredictor(), cur_temp=10.0,
            cfg=Config(end_soc_deadband=0.25, min_dwell_min=0),
            slot_minutes=15,
        )

    # The committed export must win: no force-charge through the export slot.
    assert new_plan.state is not ControllerState.FORCING, (
        "anti-fight guard did not fire at 15-min mid-hour -- cur_h is likely "
        "still hour-floored and missed the slot-keyed committed export"
    )
    assert setpoint == 0.0
    assert new_plan.committed_charge_kwh == 0.0


def _mock_flat_ceiling(ceiling_kwh_first_slot):
    """Mock optimize.solar_reservation_ceiling: a distinct ceiling in window
    index 0 (the current slot), zero elsewhere -- lets the test pin exactly
    which slot key `charge_ceiling_soc` resolved from.
    """
    def _ceiling(window_pv, window_load_reserve, cfg, cycle_end_idx=None, dt_h=1.0):
        return [ceiling_kwh_first_slot] + [0.0] * (len(window_pv) - 1)
    return _ceiling


def test_charge_ceiling_soc_slot_floors_lookup_mid_hour():
    """T9 review follow-up (3rd slot-keyed lookup): `charge_ceiling_soc` is
    built from `live_ceiling_by_hour.get(now_h)` where `now_h` was
    compute_decision's HOUR-floored local (L727) -- the SAME bug class as the
    edge-hysteresis `cur_h` (T7) and the anti-fight guard `cur_h` (T9), fixed
    above.  `live_ceiling_by_hour` is `_dp_ceiling`, keyed on the SLOT grid
    (`_dp_select_slots`'s own slot-floored `now_h`, see `ceiling_by_hour` at
    controller.py:427-430) -- NOT the hour grid.  At `now` mid-hour and
    slot_minutes=15, `.get(now_h)` (hour-floor 10:00) misses the slot-keyed
    dict (whose first key is the slot-floor 10:30) and silently returns None,
    disabling the solar-reservation charge-stop guard for 3/4 of ticks: the
    executor then charges straight to `soc_target` instead of stopping at the
    ceiling, buying grid ahead of free forecast solar.
    """
    now = datetime(2026, 8, 1, 10, 37, tzinfo=UTC)   # mid-hour: hour-floor 10:00 != slot-floor 10:30
    prices = [0.30] * 8 + [0.08] + [0.30] * 6
    slots = _slots(now, prices)
    plan = PlanState(ControllerState.PASSIVE, now - timedelta(hours=2), (),
                      committed_charge_kwh=0.0)
    cfg = Config(end_soc_deadband=0.25, min_dwell_min=0)

    captured: dict = {}
    real_decide_state = scheduler.decide_state

    def _capture_decide_state(*args, **kwargs):
        captured["charge_ceiling_soc"] = kwargs.get("charge_ceiling_soc")
        return real_decide_state(*args, **kwargs)

    with patch(
        "custom_components.anker_x1_smartgrid.optimize.optimize_grid",
        side_effect=_dp_charge_first_slot(0.0),
    ), patch(
        "custom_components.anker_x1_smartgrid.optimize.solar_reservation_ceiling",
        side_effect=_mock_flat_ceiling(2.4),
    ), patch(
        "custom_components.anker_x1_smartgrid.scheduler.decide_state",
        side_effect=_capture_decide_state,
    ):
        ctrl_mod.compute_decision(
            plan=plan,
            inputs=PlantInputs(soc=30.0, meter_w=0.0, now=now),
            slots=slots, pv_remaining=0.0, sunset=now + timedelta(hours=2),
            predictor=_FlatPredictor(), cur_temp=10.0,
            cfg=cfg,
            slot_minutes=15,
        )

    expected_ceiling_soc = 2.4 / cfg.capacity_kwh * 100.0
    assert captured["charge_ceiling_soc"] == pytest.approx(expected_ceiling_soc), (
        "charge_ceiling_soc lookup missed the slot-keyed live_ceiling_by_hour dict "
        "at 15-min mid-hour -- the lookup key is likely still hour-floored `now_h` "
        f"(got {captured.get('charge_ceiling_soc')!r}, expected the 10:30 slot's "
        f"ceiling {expected_ceiling_soc!r})"
    )


# ---------------------------------------------------------------------------
# §4 — Controller.tick()-level committed-state clear on latched change
# ---------------------------------------------------------------------------

BASE = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


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
        self.decision_rows: list[dict] = []
        self.daily_regret_rows: dict[str, dict] = {}

    def append(self, row: dict) -> None:
        self.rows.append(row)

    def append_decision(self, **kwargs) -> None:
        self.decision_rows.append(kwargs)

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
    """Minimal HA stub with a state registry."""

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


def _cfg(**overrides) -> Config:
    defaults = dict(enable_export=False)   # keep the C3 export executor out of scope
    defaults.update(overrides)
    return Config(**defaults)


def _make_controller(hass, cfg_overrides=None):
    data = {
        const.CONF_ENT_SOC: "sensor.soc",
        const.CONF_ENT_METER_POWER: "sensor.meter_power",
        const.CONF_ENT_INVERTER_LOSS: "sensor.inverter_loss",
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
    act = _StubActuator()
    store = _StubStore()
    rec = _StubRecorder()
    ctrl = Controller(hass=hass, data=data, recorder=rec, actuator=act, store=store)
    ctrl.cfg = _cfg(**(cfg_overrides or {}))
    return ctrl, act, store


def _seed_inputs(hass):
    hass.set_state("sensor.soc", "50.0")
    hass.set_state("sensor.meter_power", "300.0")
    hass.set_state("sensor.pv_power", "0.0")
    hass.set_state("sensor.battery_power", "0.0")
    hass.set_state("sensor.irradiance", "0.0")
    hass.set_state("weather.home", "sunny", {"temperature": 10.0})
    hass.set_state("sensor.export_price", "0.30")
    sunset_iso = (BASE + timedelta(hours=6)).isoformat()
    hass.set_state("sun.sun", "above_horizon", {"next_setting": sunset_iso})
    hass.set_state("sensor.price", "0.30", {
        "forecast": [
            {
                "datetime": (BASE + timedelta(hours=i)).isoformat(),
                "electricity_price": int(0.30 * const.PRICE_SCALE),
            }
            for i in range(12)
        ]
    })


def _capture_plan_stub(captured: dict):
    """compute_decision stub: records the `plan` it was CALLED with (the state
    tick() built BEFORE overwriting self.plan with the stub's own return value),
    bypasses the real DP entirely, and returns an empty-committed PASSIVE plan.
    """
    def _stub(
        plan, inputs, slots, pv_remaining, sunset,
        predictor, cur_temp, cfg,
        tomorrow_total=None, sun_times=None, today_arrays=None, tomorrow_arrays=None,
        today_watts=None, tomorrow_watts=None,
        export_price=None, _out=None, _shadow_dp=False, export_price_matches_import=False,
        estimated_tomorrow=None, past_actuals_by_hour=None, **kwargs,
    ):
        captured["plan"] = plan
        if _out is not None:
            _out["export_request"] = {}
            _out["dp_selected"] = []
            _out["intervals"] = []
            _out["grid_request"] = {}
        passive = PlanState(ControllerState.PASSIVE, inputs.now, ())
        deadline = inputs.now + timedelta(hours=8)
        return passive, 0.0, deadline, [], "water_value", []
    return _stub


@pytest.mark.asyncio
async def test_committed_state_cleared_on_latched_resolution_change(monkeypatch):
    """A latched slot_minutes change (60 -> 15) must clear stale hour-keyed
    committed state BEFORE compute_decision runs, so it cannot mis-align with
    the new quarter-slot keys.
    """
    monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
    hass = _StubHass()
    ctrl, _act, _store = _make_controller(
        hass, cfg_overrides=dict(slot_resolution="15", min_dwell_min=0),
    )
    _seed_inputs(hass)

    stale_slot = BASE.replace(minute=0, second=0, microsecond=0)
    ctrl.plan = PlanState(
        ControllerState.PASSIVE, BASE - timedelta(hours=1), (stale_slot,),
        committed_charge_kwh=1.23,
    )
    assert ctrl._committed_slot_minutes == 60   # init value, before this tick

    captured: dict = {}
    monkeypatch.setattr(ctrl_mod, "compute_decision", _capture_plan_stub(captured))

    await ctrl.tick()

    assert ctrl._committed_slot_minutes == 15
    seen_plan = captured["plan"]
    assert seen_plan.committed_slots == (), (
        f"committed_slots not cleared on latch change: {seen_plan.committed_slots!r}"
    )
    assert seen_plan.committed_charge_kwh == 0.0, (
        f"committed_charge_kwh not cleared on latch change: {seen_plan.committed_charge_kwh!r}"
    )


@pytest.mark.asyncio
async def test_committed_state_not_cleared_at_stable_60(monkeypatch):
    """Parity-critical: at a stable 60-min resolution (the default/legacy
    behaviour) the latched slot_minutes never changes tick-to-tick, so the
    clear must NEVER fire -- committed state carries forward exactly as before.
    """
    monkeypatch.setattr(ctrl_mod.dt_util, "utcnow", lambda: BASE)
    hass = _StubHass()
    ctrl, _act, _store = _make_controller(hass, cfg_overrides=dict(min_dwell_min=0))
    _seed_inputs(hass)

    stale_slot = BASE.replace(minute=0, second=0, microsecond=0)
    ctrl.plan = PlanState(
        ControllerState.PASSIVE, BASE - timedelta(hours=1), (stale_slot,),
        committed_charge_kwh=1.23,
    )
    assert ctrl._committed_slot_minutes == 60

    captured: dict = {}
    monkeypatch.setattr(ctrl_mod, "compute_decision", _capture_plan_stub(captured))

    await ctrl.tick()

    assert ctrl._committed_slot_minutes == 60
    seen_plan = captured["plan"]
    assert seen_plan.committed_slots == (stale_slot,), (
        f"committed_slots cleared at stable 60 (parity break): {seen_plan.committed_slots!r}"
    )
    assert seen_plan.committed_charge_kwh == 1.23, (
        f"committed_charge_kwh cleared at stable 60 (parity break): {seen_plan.committed_charge_kwh!r}"
    )
