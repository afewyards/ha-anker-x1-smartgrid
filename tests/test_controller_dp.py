"""T0.4 — DP optimizer wiring tests.

Covers the DP path in ``compute_decision`` (DP always runs):

- dp-on    → DP path taken, routes through decide_state / command_setpoint
- fallback → DP raises → falls through to heuristic (no exception propagated)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from custom_components.anker_x1_smartgrid.controller import compute_decision
from custom_components.anker_x1_smartgrid.forecast import LoadPredictor
from tests.conftest import ANKER_TEST_ENTITIES
from custom_components.anker_x1_smartgrid.models import (
    Config,
    ControllerState,
    PlantInputs,
    PlanState,
    PriceSlot,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

BASE = datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc)  # 10:00 UTC, Mon

_PREDICTOR = LoadPredictor.from_profile({})


def _cfg(**overrides) -> Config:
    """Build a Config with sensible test defaults."""
    return Config.from_dict({
        "capacity_kwh": 10.0,
        "soc_target": 97.0,
        "eta_charge": 0.92,
        "eps_hi_kwh": 0.4,
        "eps_lo_kwh": 0.2,
        "min_dwell_min": 0,
        "max_charge_w": 6000.0,
        "round_trip_eff": 0.85,

        **overrides,
    })


def _slots(prices: list[float], base: datetime = BASE) -> list[PriceSlot]:
    return [PriceSlot(base + timedelta(hours=i), p) for i, p in enumerate(prices)]


def _plan(state: ControllerState = ControllerState.PASSIVE, age_h: float = 2.0) -> PlanState:
    return PlanState(state, BASE - timedelta(hours=age_h), ())


def _make_dp_mock(value_per_hour: float = 0.0):
    """Return a side_effect function for optimize_grid that produces correct window_len."""
    def _side_effect(*args, **kwargs):
        wl = kwargs.get("window_len", len(args[0]) if args else 1)
        return {
            "schedule": [value_per_hour] * wl,
            "kwh": value_per_hour * wl,
            "eur": 0.0,
        }
    return _side_effect


def _make_dp_mock_first_hour(charge_kwh: float = 5.0):
    """Return a side_effect that puts all charge in hour 0 (current slot)."""
    def _side_effect(*args, **kwargs):
        wl = kwargs.get("window_len", len(args[0]) if args else 1)
        schedule = [charge_kwh] + [0.0] * (wl - 1)
        return {"schedule": schedule, "kwh": charge_kwh, "eur": charge_kwh * 0.05}
    return _side_effect


# ---------------------------------------------------------------------------
# Helper: call compute_decision with minimal valid inputs
# ---------------------------------------------------------------------------

def _call(
    cfg: Config,
    *,
    soc: float = 20.0,
    slots: list[PriceSlot] | None = None,
    pv_remaining: float = 0.0,
    sunset_offset_h: float = 8.0,
    plan: PlanState | None = None,
    export_price: float | None = None,
):
    """Convenience wrapper around compute_decision for tests."""
    if slots is None:
        # Cheap first slot (triggers FORCING when deficit is large), then expensive.
        slots = _slots([0.05, 0.40, 0.40, 0.40, 0.40, 0.40, 0.40, 0.40, 0.40])
    if plan is None:
        plan = _plan()
    inputs = PlantInputs(soc=soc, meter_w=0.0, now=BASE)
    sunset = BASE + timedelta(hours=sunset_offset_h)
    return compute_decision(
        plan, inputs, slots, pv_remaining, sunset,
        _PREDICTOR, None, cfg,
        export_price=export_price,
    )


# ===========================================================================
# 1. DP path is taken (DP always runs)
# ===========================================================================

def test_flag_on_calls_optimize_grid():
    """With use_dp_optimizer=True, optimize_grid IS called."""
    cfg = _cfg()
    with patch("custom_components.anker_x1_smartgrid.optimize.optimize_grid",
               side_effect=_make_dp_mock(0.0)) as mock_dp:
        _call(cfg, soc=20.0)
    mock_dp.assert_called_once()


def test_flag_on_dp_schedule_selects_charging_hours():
    """Flag-on: DP puts charge in hour 0 (current slot) → selected=[BASE] → FORCING."""
    cfg = _cfg()

    with patch("custom_components.anker_x1_smartgrid.optimize.optimize_grid",
               side_effect=_make_dp_mock_first_hour(5.0)) as mock_dp:
        new_plan, setpoint, _deadline, _horizon, _hm, _ = _call(cfg, soc=20.0)

    # optimize_grid was called
    mock_dp.assert_called_once()
    # Hour 0 (BASE) is selected → decide_state should FORCE
    assert new_plan.state is ControllerState.FORCING
    assert setpoint < 0.0   # charging


def test_flag_on_dp_zero_schedule_stays_passive():
    """Flag-on: DP returns zero schedule → no slot selected → PASSIVE.

    For this test we give SoC near target so the deficit is very small;
    with no worthy slots and economic-only mode, the controller stays PASSIVE.
    """
    cfg = _cfg()

    with patch("custom_components.anker_x1_smartgrid.optimize.optimize_grid",
               side_effect=_make_dp_mock(0.0)):
        # soc=96.5% near target → DP returns zero schedule → no slots selected
        new_plan, setpoint, _deadline, _horizon, _hm, _ = _call(cfg, soc=96.5)

    # With near-zero deficit and zero DP schedule, no slots should be selected
    assert new_plan.state is ControllerState.PASSIVE
    assert setpoint == 0.0


def test_flag_on_passes_feed_in_to_dp():
    """Flag-on: export_price is forwarded to optimize_grid as feed_in array.

    When export_price_matches_import=False (default, differing entities), feed_in
    is a ratio-scaled version of window_price (not a flat broadcast).  This test
    verifies that feed_in is non-None and that its first element equals export_price
    (the ratio-scale anchors hour 0 to export_price, and scales all other hours
    proportionally to the import curve).
    """
    cfg = _cfg()
    EXPORT_PRICE = 0.08

    with patch("custom_components.anker_x1_smartgrid.optimize.optimize_grid",
               side_effect=_make_dp_mock(0.0)) as mock_dp:
        # export_price_matches_import defaults to False (differing entities)
        _call(cfg, soc=96.5, export_price=EXPORT_PRICE)

    # Check feed_in kwarg was provided and is non-None
    call_kwargs = mock_dp.call_args.kwargs
    feed_in_passed = call_kwargs.get("feed_in")
    assert feed_in_passed is not None, "feed_in must be passed when export_price is set"
    # With ratio-scaling + fee subtraction (Task 6):
    #   feed_in[0] = effective_export_price(window_price[0] × ratio, cfg)
    #              = effective_export_price(export_price, cfg)  (ratio anchors h0 to export_price)
    #              = export_price − fee (default 0.02)
    # Remaining hours scale proportionally and are also fee-reduced.
    FEE = cfg.export_fee_eur_per_kwh  # default 0.02
    assert feed_in_passed[0] == pytest.approx(EXPORT_PRICE - FEE, abs=1e-6), (
        f"feed_in[0] must equal export_price − fee = {EXPORT_PRICE} − {FEE} = {EXPORT_PRICE - FEE}, "
        f"got {feed_in_passed[0]}"
    )
    # All values must be non-negative
    assert all(v >= 0.0 for v in feed_in_passed), "feed_in values must be non-negative"


def test_flag_on_no_export_price_passes_none():
    """Flag-on: no export_price configured → feed_in=None passed to optimize_grid."""
    cfg = _cfg()

    with patch("custom_components.anker_x1_smartgrid.optimize.optimize_grid",
               side_effect=_make_dp_mock(0.0)) as mock_dp:
        _call(cfg, soc=96.5, export_price=None)

    call_kwargs = mock_dp.call_args.kwargs
    feed_in_passed = call_kwargs.get("feed_in")
    assert feed_in_passed is None, "feed_in must be None when export_price is not set"


def test_flag_on_passes_chargeable_mask():
    """Flag-on: price gate is forwarded to optimize_grid as chargeable mask."""
    cfg = _cfg()

    with patch("custom_components.anker_x1_smartgrid.optimize.optimize_grid",
               side_effect=_make_dp_mock(0.0)) as mock_dp:
        _call(cfg, soc=96.5)

    call_kwargs = mock_dp.call_args.kwargs
    chargeable = call_kwargs.get("chargeable")
    assert chargeable is not None, "chargeable mask must always be passed"
    assert isinstance(chargeable, list)
    assert all(isinstance(v, bool) for v in chargeable)


# ===========================================================================
# 3. FALLBACK — DP exception → PASSIVE, no raise
# ===========================================================================

def test_dp_fallback_on_exception_does_not_raise():
    """When optimize_grid raises, compute_decision returns (not raises) a PASSIVE plan."""
    cfg = _cfg()

    with patch("custom_components.anker_x1_smartgrid.optimize.optimize_grid") as mock_dp:
        mock_dp.side_effect = RuntimeError("DP exploded")
        # Must not raise
        result = _call(cfg, soc=20.0)

    assert result is not None


def test_dp_fallback_on_exception_is_passive():
    """When optimize_grid raises, compute_decision falls back to PASSIVE (no heuristic charge).

    Task 2 (P80-survival-removal): the heuristic charge-slot selection was deleted.
    On any DP exception, selected=[] → decide_state yields PASSIVE.
    Previously the heuristic could force-charge on cheap slots; that path is gone.
    """
    cfg = _cfg()

    with patch("custom_components.anker_x1_smartgrid.optimize.optimize_grid") as mock_dp:
        mock_dp.side_effect = RuntimeError("DP exploded")
        result = _call(cfg, soc=20.0)

    assert result is not None
    new_plan = result[0]
    assert new_plan.state is ControllerState.PASSIVE, (
        f"DP exception fallback must be PASSIVE (selected=[]); got {new_plan.state}"
    )


def test_dp_fallback_on_nan_schedule():
    """When optimize_grid returns a schedule with NaN, compute_decision falls back to PASSIVE."""
    cfg = _cfg()

    def _nan_mock(*args, **kwargs):
        wl = kwargs.get("window_len", 1)
        return {"schedule": [float("nan")] + [0.0] * (wl - 1), "kwh": float("nan"), "eur": 0.0}

    with patch("custom_components.anker_x1_smartgrid.optimize.optimize_grid", side_effect=_nan_mock):
        # Must not raise; NaN in schedule triggers ValueError → fallback to PASSIVE
        result = _call(cfg, soc=20.0)

    assert result is not None
    new_plan = result[0]
    # Fallback is PASSIVE: selected=[] since the heuristic was removed (Task 2).
    assert new_plan.state is ControllerState.PASSIVE


def test_dp_infeasible_at_low_soc_records_infeasible_and_stays_passive():
    """compute_decision at soc=floor with all-expensive prices: dp_infeasible=False, PASSIVE.

    Economic-only (A1) uses water_value terminal mode.  In water_value mode
    dp_infeasible is NEVER set True for floor/sub-floor scenarios — the DP accepts
    drain-to-firmware-floor as valid (floor_import_cost accounting).  Reserve-TARGET
    unreachability (the only remaining infeasible signal) cannot fire in water_value
    mode, even when no chargeable slot exists.

    With all prices above the ceiling gate (mask all-False), no charging occurs and
    dp_selected is empty → state PASSIVE.  The edge-triggered WARNING for the
    at-floor condition fires at the Controller.tick() level (see
    test_acceptance_subfloor_infeasible.py::TestDrainedToFloorWarning).
    """
    cfg = _cfg(soc_floor=5.0)
    # All slots far above the gate ceiling → mask all-False → DP cannot charge.
    slots = _slots([0.90, 0.90, 0.90, 0.90, 0.90, 0.90, 0.90, 0.90, 0.90])
    inputs = PlantInputs(soc=5.0, meter_w=0.0, now=BASE)
    sunset = BASE + timedelta(hours=8.0)
    _out: dict = {}
    new_plan, *_ = compute_decision(
        _plan(), inputs, slots, 0.0, sunset,
        _PREDICTOR, None, cfg,
        _out=_out,
    )
    assert new_plan.state is ControllerState.PASSIVE, (
        f"Expected PASSIVE at floor with all-expensive prices, got {new_plan.state}"
    )
    # Economic-only (A1): water_value terminal mode → dp_infeasible=False.
    # Survival-floor unreachability is no longer an infeasible signal.
    assert _out.get("dp_infeasible") is False, (
        f"compute_decision must record dp_infeasible=False (water_value mode); got _out={_out}"
    )


# ===========================================================================
# D2 — export_request side-channel plumbing tests
# ===========================================================================

class TestExportRequestPlumbing:
    """Verify that compute_decision populates _out["export_request"] when the DP
    runs with an export price, and that it is absent / empty when there is no
    export activity.

    These tests rely on use_dp_optimizer=True and export-price inputs.
    The export_request dict mirrors grid_request: {datetime: float} where the
    float is the planned export rate in W for that clock hour.
    """

    def test_export_request_key_present_after_dp_run(self):
        """_out["export_request"] is populated when DP runs successfully.

        With use_dp_optimizer=True, a successful DP run must write
        export_request to the _out side-channel (even if it is an empty dict
        when no export is planned — the key itself must be present so callers
        can use ``"export_request" in _out`` as a "DP ran" probe).
        """
        cfg = _cfg(enable_export=True)
        # Cheap slot to trigger charge, rest expensive
        slots = _slots([0.05, 0.40, 0.40, 0.40, 0.40, 0.40, 0.40, 0.40, 0.40])
        inputs = PlantInputs(soc=20.0, meter_w=0.0, now=BASE)
        sunset = BASE + timedelta(hours=8.0)
        _out: dict = {}
        compute_decision(
            _plan(), inputs, slots, 0.0, sunset,
            _PREDICTOR, None, cfg,
            export_price=0.30,
            _out=_out,
        )
        assert "export_request" in _out, (
            "DP ran successfully → _out must contain 'export_request' key; "
            f"got keys: {list(_out.keys())}"
        )
        assert isinstance(_out["export_request"], dict), (
            "export_request must be a dict (per-hour datetime→W mapping)"
        )

    def test_export_request_values_are_nonneg_watts(self):
        """All export_request values must be non-negative watts.

        export_request mirrors grid_request semantics: positive float = rate in W.
        (grid_request values are Wh/h ≡ W for 1-hour slots, stored as positive.)
        """
        cfg = _cfg(enable_export=True)
        slots = _slots([0.05, 0.40, 0.40, 0.40, 0.40, 0.40, 0.40, 0.40, 0.40])
        inputs = PlantInputs(soc=80.0, meter_w=0.0, now=BASE)
        sunset = BASE + timedelta(hours=8.0)
        _out: dict = {}
        compute_decision(
            _plan(), inputs, slots, 0.0, sunset,
            _PREDICTOR, None, cfg,
            export_price=0.35,
            _out=_out,
        )
        if "export_request" in _out:
            for dt, w in _out["export_request"].items():
                assert w >= 0.0, (
                    f"export_request[{dt}] = {w} is negative — must be non-negative watts"
                )


# ---------------------------------------------------------------------------
# Helper: call _dp_select_slots directly (Phase 2 wiring tests)
# ---------------------------------------------------------------------------

def _call_dp_select_slots(
    cfg: Config,
    *,
    soc: float = 80.0,
    export_price: float | None = None,
    export_price_matches_import: bool = False,
    prices: list[float] | None = None,
):
    """Build minimal inputs and invoke ``_dp_select_slots`` directly.

    Mirrors the inputs constructed by ``_call``/``compute_decision``:
    empty intervals (PV/load = 0), a deadline beyond all slots, ceiling=0.40.
    Useful for patching ``optimize_mod.optimize_grid`` and asserting the
    wiring between ``_dp_select_slots`` and the DP.
    """
    from custom_components.anker_x1_smartgrid import controller as ctrl_mod

    if prices is None:
        prices = [0.05, 0.40, 0.40, 0.40, 0.40, 0.40, 0.40, 0.40, 0.40]
    sl = _slots(prices)
    inputs = PlantInputs(soc=soc, meter_w=0.0, now=BASE)
    deadline = BASE + timedelta(hours=len(prices))
    return ctrl_mod._dp_select_slots(
        inputs=inputs,
        slots=sl,
        deadline=deadline,
        ceiling=0.40,
        cfg=cfg,
        export_price=export_price,
        export_price_matches_import=export_price_matches_import,
        intervals=[],
    )


# ===========================================================================
# Phase 2 — _dp_select_slots drives a single co-optimized optimize_grid call
# ===========================================================================

def test_dp_select_slots_calls_optimize_grid_with_export_and_builds_export_request(monkeypatch):
    """Phase 2: the controller drives a single optimize_grid call and reads export_schedule."""
    from custom_components.anker_x1_smartgrid import controller as ctrl_mod
    captured = {}

    def _fake_optimize_grid(*args, **kwargs):
        captured["export_price"] = kwargs.get("export_price")
        wl = kwargs["window_len"]
        sched = [0.0] * wl
        exp = [0.0] * wl
        exp[0] = 3.0  # 3 kWh exported in the first window hour
        return {"schedule": sched, "kwh": 0.0, "eur": 0.0,
                "export_schedule": exp, "export_kwh": 3.0, "export_revenue_eur": 1.0}

    monkeypatch.setattr(ctrl_mod.optimize_mod, "optimize_grid", _fake_optimize_grid)
    # plan_charge_and_export has been deleted (Task 10) — the greedy path no longer exists.

    cfg = _cfg()
    selected, grid_request, infeasible, export_request, export_rev, _ceiling = _call_dp_select_slots(cfg, soc=80.0, export_price=0.30)

    assert captured["export_price"] is not None, "optimize_grid must receive a per-hour export_price array"
    now_h = BASE.replace(minute=0, second=0, microsecond=0)
    assert export_request.get(now_h) == pytest.approx(3000.0), f"export_request should be 3 kWh×1000=3000W, got {export_request}"


@pytest.mark.asyncio
async def test_options_override_data_field(hass):
    """Options take precedence over data when the same key appears in both.

    entry.data has soc_target=97.0; entry.options overrides it to 80.0.
    The running controller must see 80.0.
    """
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.anker_x1_smartgrid.const import DOMAIN, DEFAULT_ENTITIES

    data = {**DEFAULT_ENTITIES, **ANKER_TEST_ENTITIES}
    data.update({"soc_target": 97.0})
    options = {"soc_target": 80.0}  # overrides data

    entry = MockConfigEntry(domain=DOMAIN, data=data, options=options)
    entry.add_to_hass(hass)

    hass.states.async_set(data["ent_soc"], "50")
    hass.states.async_set(data["ent_meter_power"], "0")

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    ctrl = hass.data[DOMAIN][entry.entry_id]["controller"]
    assert ctrl.cfg.soc_target == pytest.approx(80.0), (
        "entry.options must override entry.data (options take precedence)"
    )

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# Helper: call compute_decision with _out side-channel (Phase 3 horizon tests)
# ---------------------------------------------------------------------------

def _call_compute_decision(
    cfg: Config,
    *,
    soc: float,
    prices: list[float],
    export_price: float | None = None,
    out: dict | None = None,
    hedge_drain_by_hour: dict | None = None,
):
    """Wrapper around compute_decision that exposes the _out side-channel.

    Mirrors ``_call`` but also forwards ``_out`` so tests can inspect
    ``export_request`` and other DP artefacts.  Uses
    ``export_price_matches_import=True`` so the DP sees the real per-hour
    curve (same entity convention).

    ``hedge_drain_by_hour`` is forwarded to ``compute_decision`` to test the
    T5a hedge plumbing (default None → no behaviour change).
    """
    sl = _slots(prices)
    inputs = PlantInputs(soc=soc, meter_w=0.0, now=BASE)
    sunset = BASE + timedelta(hours=len(prices))
    return compute_decision(
        _plan(), inputs, sl, 0.0, sunset,
        _PREDICTOR, None, cfg,
        export_price=export_price,
        export_price_matches_import=True,
        _out=out,
        hedge_drain_by_hour=hedge_drain_by_hour,
    )


# ---------------------------------------------------------------------------
# Hedge plumbing — T5a: hedge_drain_by_hour threaded into DP + display
# ---------------------------------------------------------------------------

def test_compute_decision_hedge_none_is_noop():
    """hedge_drain_by_hour=None produces byte-identical horizon SoC values."""
    cfg = _cfg()
    prices = [0.10, 0.40, 0.40, 0.40, 0.40, 0.40, 0.40, 0.40, 0.40]
    a = _call_compute_decision(cfg, soc=50.0, prices=prices)
    b = _call_compute_decision(cfg, soc=50.0, prices=prices, hedge_drain_by_hour=None)
    assert [r["soc"] for r in a[3]] == [r["soc"] for r in b[3]]


def test_compute_decision_hedge_lowers_projection():
    """A hedge keyed at now_h lowers the published horizon SoC from that hour onward."""
    cfg = _cfg()
    prices = [0.10, 0.40, 0.40, 0.40, 0.40, 0.40, 0.40, 0.40, 0.40]
    now_h = BASE  # BASE is already hour-aligned (minute=0, second=0)
    base = _call_compute_decision(cfg, soc=50.0, prices=prices)
    hed = _call_compute_decision(cfg, soc=50.0, prices=prices,
                                  hedge_drain_by_hour={now_h: 1.5})
    assert hed[3][-1]["soc"] <= base[3][-1]["soc"]


# ===========================================================================
# Phase 3 — de-truncation: DP window spans the full forecast horizon
# ===========================================================================

def test_full_horizon_schedules_export_into_peak_beyond_trough():
    """Phase 3: a peak past the next trough is inside the DP window → export scheduled there.

    Design: ``find_next_trough`` is engineered to return the trough at +7h
    (local min at 0.07, below the 30th-percentile ≈ 0.25, past min_horizon_h=6).
    Old behaviour: horizon_edge = trough_dt + 1h = BASE+8h; window_len=8 →
    the +20h peak (0.60) is never seen by the DP.
    New behaviour: horizon_edge = last_slot + 1h = BASE+25h; window_len=25 →
    the +20h peak is in-window and the DP schedules export there.

    Prices layout (25 slots):
      [0..5]  0.25  (before min_horizon_h=6 — trough here would be disqualified)
      [6]     0.15  (descent)
      [7]     0.07  ← qualifying trough (local min, below P30, ≥6h from now)
      [8]     0.15  (recovery)
      [9..19] 0.25  (13 mid-range hours; index 9+11=20 is the peak index)
      [20]    0.60  ← tall export peak (past the trough)
      [21..24] 0.25
    """
    cfg = _cfg(enable_export=True)
    prices = [0.25] * 6 + [0.15, 0.07, 0.15] + [0.25] * 11 + [0.60] + [0.25] * 4
    assert len(prices) == 25
    out: dict = {}
    _new_plan, _setpoint, _deadline, _horizon, _hm, _ivs = _call_compute_decision(
        cfg, soc=80.0, prices=prices, export_price=0.20, out=out,
    )
    peak_hour = BASE.replace(minute=0, second=0, microsecond=0) + timedelta(hours=20)
    # Under the old [now, trough+1h=BASE+8h] truncation the peak is invisible.
    # After the fix the full 25h window reveals it and export is scheduled there.
    assert peak_hour in out.get("export_request", {}), (
        f"export_request must include the +20h peak (0.60 €/kWh); "
        f"got {sorted(out.get('export_request', {}))}"
    )


# ===========================================================================
# Phase 4 — effective_export_price applied to per-hour DP export prices
# ===========================================================================

def test_dp_export_prices_are_fee_reduced(monkeypatch):
    """Phase 4: window_export_price passed to the DP is raw export price minus the fee."""
    from custom_components.anker_x1_smartgrid import controller as ctrl_mod
    captured = {}

    def _fake(*args, **kwargs):
        captured["export_price"] = kwargs.get("export_price")
        wl = kwargs["window_len"]
        return {"schedule": [0.0] * wl, "kwh": 0.0, "eur": 0.0,
                "export_schedule": [0.0] * wl, "export_kwh": 0.0, "export_revenue_eur": 0.0}

    monkeypatch.setattr(ctrl_mod.optimize_mod, "optimize_grid", _fake)
    cfg = _cfg(enable_export=True, export_fee_eur_per_kwh=0.02)
    # export entity == import entity → window_export_price == window_price − fee.
    _call_dp_select_slots(cfg, soc=80.0, export_price=0.30, export_price_matches_import=True,
                          prices=[0.30] * 12)
    assert captured["export_price"] is not None
    assert captured["export_price"][0] == pytest.approx(0.28), (
        f"expected 0.30 − 0.02 fee = 0.28, got {captured['export_price'][0]}"
    )


# ===========================================================================
# H4 — per-hour temperature threading through compute_decision
# ===========================================================================

def test_compute_decision_threads_per_hour_temp_to_predictor():
    """temp_by_hour reaches the predictor for future hours (not a flat cur_temp)."""
    seen_temps: dict = {}

    class _RecordingPredictor:
        def predict(self, when, temp, fallback_w, *, quantile=0.5):
            seen_temps[when] = temp
            return 500.0

    cfg = _cfg()
    # 12 slots from BASE
    slots = _slots([0.20] * 12)
    inputs = PlantInputs(soc=50.0, meter_w=0.0, now=BASE)
    sunset = BASE + timedelta(hours=12)

    # Build distinct per-hour temps (0.0, 2.0, 4.0, ... 22.0)
    temp_by_hour = {
        (BASE + timedelta(hours=i)).replace(minute=0, second=0, microsecond=0): float(i * 2)
        for i in range(12)
    }

    compute_decision(
        _plan(), inputs, slots, 0.0, sunset,
        _RecordingPredictor(), 99.0, cfg,
        temp_by_hour=temp_by_hour,
    )
    # More than one distinct non-fallback temp observed → per-hour map is active.
    non_fallback = {t for t in seen_temps.values() if t != 99.0}
    assert len(non_fallback) > 1, (
        f"Expected multiple distinct per-hour temps, but saw: {sorted(non_fallback)}"
    )


# ===========================================================================
# Ride-to-trough (rev-2) — price-prior GATE respects reserve_anchor
# ===========================================================================

def test_price_prior_gate_respects_reserve_anchor(monkeypatch):
    """Gate (compute_decision L790-800): _apply_price_prior runs ONLY under the legacy
    anchor.  Spy the module global + drive the REAL compute_decision via _call for
    IDENTICAL inputs — NOT called for trough, CALLED for legacy."""
    # Task C2: _apply_price_prior now lives in decision.py; compute_decision's
    # internal call resolves against decision.py's own globals, so the spy
    # must patch it there (patching the controller.py re-export would be a
    # silent no-op on the moved function's call site).
    import custom_components.anker_x1_smartgrid.decision as decision_mod
    calls = {"n": 0}
    real_prior = decision_mod._apply_price_prior
    def _spy(*a, **k):
        calls["n"] += 1
        return real_prior(*a, **k)
    monkeypatch.setattr(decision_mod, "_apply_price_prior", _spy)

    calls["n"] = 0
    _call(_cfg(reserve_anchor="trough"), soc=20.0)
    assert calls["n"] == 0, "trough anchor must GATE OFF _apply_price_prior"

    calls["n"] = 0
    _call(_cfg(reserve_anchor="legacy"), soc=20.0)
    assert calls["n"] >= 1, "legacy anchor must still CALL _apply_price_prior"
