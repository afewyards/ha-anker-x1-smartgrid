# tests/test_controller_water_value.py
from datetime import datetime, timedelta, timezone, UTC


from custom_components.anker_x1_smartgrid import controller as ctrl
from custom_components.anker_x1_smartgrid import decision as dec_mod
from custom_components.anker_x1_smartgrid.models import (
    Config,
    PlanState,
    PlantInputs,
    PriceSlot,
)


class _FlatPredictor:
    def predict(self, start, temp, fallback, quantile=0.5):
        return 300.0  # constant 300 W house load


def _price_slots(now, prices):
    return [
        PriceSlot(now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=i), p) for i, p in enumerate(prices)
    ]


def _base_kwargs(now, slots):
    return dict(
        plan=PlanState.initial(now),
        inputs=PlantInputs(soc=50.0, meter_w=0.0, now=now),
        slots=slots,
        pv_remaining=0.0,
        sunset=now + timedelta(hours=2),
        predictor=_FlatPredictor(),
        cur_temp=10.0,
        cfg=Config(),
    )


def test_horizon_extends_to_trough_not_deadline():
    now = datetime(2026, 6, 23, 18, 0, tzinfo=UTC)
    # Trough at +8h (02:00); deadline (sunset-buffer) is ~1h out.
    prices = [0.30] * 8 + [0.08] + [0.30] * 6
    slots = _price_slots(now, prices)
    out: dict = {}
    plan, sp, deadline, horizon, hm, _ = ctrl.compute_decision(
        **_base_kwargs(now, slots),
        _out=out,
    )
    # The fictive/plan horizon spans through the trough hour (02:00), well past
    # the legacy deadline.
    starts = [datetime.fromisoformat(e["start"]) for e in horizon]
    assert max(starts) >= now + timedelta(hours=8)


def test_water_value_terminal_does_not_force_target_when_full_enough():
    now = datetime(2026, 6, 23, 18, 0, tzinfo=UTC)
    prices = [0.30] * 8 + [0.08] + [0.30] * 6
    slots = _price_slots(now, prices)
    kw = _base_kwargs(now, slots)
    kw["inputs"] = PlantInputs(soc=80.0, meter_w=0.0, now=now)
    out: dict = {}
    plan, sp, deadline, horizon, hm, _ = ctrl.compute_decision(**kw, _out=out)
    # With water value keyed to the cheap trough and ample SoC, the DP should not
    # select expensive (0.30) hours to force-fill toward target.
    grid_hours = [e for e in horizon if e["mode"] == "grid" and e["price"] >= 0.29]
    assert grid_hours == []


def test_heavy_drain_does_not_set_dp_infeasible():
    """Economic-only (A1): heavy load draining past floor does NOT set dp_infeasible.

    Under A1 the live planner uses water_value terminal mode.  In water_value mode
    dp_infeasible is NEVER set True for floor/sub-floor scenarios — the DP accepts
    draining to the firmware floor and prices below-floor house load as direct grid
    imports (floor_import_cost accounting).  The "survival floor unreachable" concept
    is retired; survival is the firmware's hard-floor, not a DP constraint.

    Scenario: 12 kW load >> 6 kW max charge, soc=12%.  The battery drains well below
    the soft floor during the horizon, but dp_infeasible stays False.  The DP may
    economically select the cheap trough slot regardless of load magnitude.
    """
    now = datetime(2026, 6, 23, 18, 0, tzinfo=UTC)

    class _HeavyPredictor:
        def predict(self, start, temp, fallback, quantile=0.5):
            return 12000.0  # 12 kW load >> 6 kW max charge

    prices = [0.30] * 8 + [0.08] + [0.30] * 6
    slots = [
        PriceSlot(now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=i), p) for i, p in enumerate(prices)
    ]
    out: dict = {}
    ctrl.compute_decision(
        plan=PlanState.initial(now),
        inputs=PlantInputs(soc=12.0, meter_w=0.0, now=now),
        slots=slots,
        pv_remaining=0.0,
        sunset=now + timedelta(hours=2),
        predictor=_HeavyPredictor(),
        cur_temp=10.0,
        cfg=Config(),
        _out=out,
    )
    # Economic-only (A1): water_value mode never sets dp_infeasible=True for
    # floor/sub-floor scenarios.  Battery rides to firmware floor; below-floor
    # house load is served by direct grid import (floor_import_cost accounting).
    assert out.get("dp_infeasible") is False


def test_new_mode_shield_does_not_inflate_safe_schedule():
    """New-mode DP shield: ample SoC + light load must not force-inflate the schedule.

    Scenario: 20:00, SoC=85%, load=100 W, no PV.  With almost full battery and
    minimal consumption, the bridge deficit to soc_floor=10% is effectively zero.
    The DP should schedule little to no grid charging even when cheap slots exist.
    """
    now = datetime(2026, 6, 23, 20, 0, tzinfo=UTC)

    class _LightPredictor:
        def predict(self, start, temp, fallback, quantile=0.5):
            return 100.0  # very light 100 W load

    prices = [0.30] * 8 + [0.07] + [0.30] * 6
    slots = _price_slots(now, prices)
    out: dict = {}
    ctrl.compute_decision(
        plan=PlanState.initial(now),
        inputs=PlantInputs(soc=85.0, meter_w=0.0, now=now),
        slots=slots,
        pv_remaining=0.0,
        sunset=now + timedelta(hours=1),
        predictor=_LightPredictor(),
        cur_temp=10.0,
        cfg=Config(),
        _out=out,
    )
    # Primary shield assertion: floor is easily reachable → dp_infeasible False.
    assert out.get("dp_infeasible") is False, "Expected feasible schedule for healthy SoC"
    # Force-inflation detector: no expensive (0.30 EUR) hours should appear in grid_request.
    # If the shield wrongly fired, it would shove charge into expensive slots to meet a
    # spurious deficit. With soc=85% and soc_floor=10%, the bridge deficit is zero —
    # any charge at 0.30 slots is force-inflation and a shield bug.
    # (The DP may legitimately charge at the cheap trough slot 0.07 for economic reasons;
    # that is correct water-value behaviour, NOT force-inflation.)
    price_by_hour = {s.start: s.price for s in slots}
    grid_req = out.get("grid_request", {})
    expensive_grid_wh = sum(wh for h, wh in grid_req.items() if price_by_hour.get(h, 0.0) >= 0.25)
    assert expensive_grid_wh == 0.0, (
        f"Force-inflation detected: {expensive_grid_wh:.0f} Wh charged at expensive slots "
        f"(≥0.25 EUR/kWh) despite ample SoC — shield incorrectly fired"
    )


# ===========================================================================
# Terminal water-value anchor: MINIMUM remaining-horizon price, not the next
# LOCAL price trough (regression for the "sells nothing at the horizon's own
# peak" defect — the earliest qualifying local minimum can be far shallower
# than a deeper refill sitting later in the same horizon; find_next_trough
# picks the EARLIEST qualifying candidate, not the deepest).
# ===========================================================================


def _flat_export_kwargs(now, slots, cfg):
    return dict(
        plan=PlanState.initial(now),
        inputs=PlantInputs(soc=80.0, meter_w=0.0, now=now),
        slots=slots,
        pv_remaining=0.0,
        sunset=now + timedelta(hours=2),
        predictor=_FlatPredictor(),
        cur_temp=10.0,
        cfg=cfg,
        export_price=slots[0].price,
        export_price_matches_import=True,
    )


def test_terminal_water_value_anchored_to_horizon_min(monkeypatch):
    """Direct pin: the terminal water value must be computed from
    ``compute_water_value(min(remaining_prices), cfg)``, NOT from
    ``find_next_trough``'s earliest-qualifying local minimum.

    Fixture: a shallow local trough at +6h (0.29, easily found by
    ``find_next_trough`` since most of the horizon sits at 0.34) versus a much
    deeper true minimum at +17h (0.15). The old anchor picks 0.29 (the
    earliest qualifying candidate); the fix must pick 0.15 (the true horizon
    minimum).
    """
    now = datetime(2026, 6, 23, 18, 0, tzinfo=UTC)
    prices = [0.34] * 6 + [0.29] + [0.34] * 10 + [0.15] + [0.34] * 6
    slots = _price_slots(now, prices)
    cfg = Config(enable_export=True)

    real_compute_water_value = dec_mod.optimize_mod.compute_water_value
    captured: dict = {}

    def _spy(trough_price, cfg_arg):
        captured["price"] = trough_price
        return real_compute_water_value(trough_price, cfg_arg)

    monkeypatch.setattr(dec_mod.optimize_mod, "compute_water_value", _spy)

    ctrl.compute_decision(**_flat_export_kwargs(now, slots, cfg), _out={})

    assert captured["price"] == 0.15, (
        f"terminal water value must be anchored to the horizon minimum (0.15), "
        f"got {captured['price']} (0.29 would mean the stale next-local-trough anchor is still live)"
    )


def test_export_scheduled_at_peak_when_horizon_min_beats_local_trough():
    """A cheaper refill later in the horizon must not be shadowed by an
    earlier, shallower local trough: with the horizon-min anchor, exporting
    at the horizon's top price hour beats holding for the (shallow) local
    trough, so the DP must schedule export there.

    Under the OLD next-local-trough anchor, the shallow +6h dip (0.29) prices
    the terminal water value at ~0.315 (0.29 / eta_charge=0.92), which beats
    the net top-price-hour revenue (0.40 − fee 0.02 − wear 0.10 = 0.28) → the
    DP holds and exports nothing, even at the horizon's own peak.
    """
    now = datetime(2026, 6, 23, 18, 0, tzinfo=UTC)
    prices = [0.34] * 6 + [0.29] + [0.34] * 10 + [0.15] + [0.34] * 2 + [0.40] + [0.34] * 3
    slots = _price_slots(now, prices)
    cfg = Config(enable_export=True)
    out: dict = {}

    ctrl.compute_decision(**_flat_export_kwargs(now, slots, cfg), _out=out)

    peak_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=20)
    assert peak_hour in out.get("export_request", {}), (
        "export_request must include the horizon's top price hour (+20h, 0.40 EUR/kWh) once the "
        "terminal water value is anchored to the horizon minimum (0.15), not the shallow local "
        f"trough (0.29); got {sorted(out.get('export_request', {}))}"
    )


def test_export_still_held_when_horizon_min_is_not_cheap_enough():
    """Converse guard: when nothing in the remaining horizon is cheap enough
    to make replacement attractive, the DP must still hold — the fix must not
    turn into an "always export" bias.
    """
    now = datetime(2026, 6, 23, 18, 0, tzinfo=UTC)
    # No deep refill anywhere in the horizon; only a modest peak at +20h.
    prices = [0.30] * 20 + [0.34] + [0.30] * 3
    slots = _price_slots(now, prices)
    cfg = Config(enable_export=True)
    out: dict = {}

    ctrl.compute_decision(**_flat_export_kwargs(now, slots, cfg), _out=out)

    assert out.get("export_request", {}) == {}, (
        "DP must hold when the horizon minimum (0.30) is not cheap enough to beat the net "
        f"top-price-hour revenue; got export_request={out.get('export_request')}"
    )
