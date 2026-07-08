import pytest
from datetime import datetime, timezone, timedelta
from custom_components.anker_x1_smartgrid.models import Config, PriceSlot, ForecastInterval
from custom_components.anker_x1_smartgrid import plan

BASE = datetime(2026, 6, 20, 11, 0, tzinfo=timezone.utc)


def _slots(n, price=0.30):
    return [PriceSlot(BASE + timedelta(hours=i), price) for i in range(n)]


def test_empty_slots_returns_empty():
    assert plan.build_plan_horizon([], [], [], 50.0, BASE, Config()) == []


def test_modes_grid_solar_idle():
    cfg = Config(capacity_kwh=10.0, soc_target=100.0, max_charge_w=3000.0, eta_charge=1.0)
    slots = _slots(3)
    intervals = [
        ForecastInterval(BASE, pv_w=2000.0, load_w=300.0, dt_h=1.0),       # solar surplus
        ForecastInterval(BASE + timedelta(hours=1), pv_w=0.0, load_w=400.0, dt_h=1.0),  # no sun
        ForecastInterval(BASE + timedelta(hours=2), pv_w=0.0, load_w=400.0, dt_h=1.0),
    ]
    selected = [BASE + timedelta(hours=1)]  # planned grid charge at hour 1
    out = plan.build_plan_horizon(slots, intervals, selected, 50.0, BASE + timedelta(hours=3), cfg)
    assert [e["mode"] for e in out] == ["solar", "grid", "idle"]
    assert out[0]["pv_w"] == 2000.0
    assert out[0]["start"] == BASE.isoformat()
    assert out[0]["price"] == 0.30


def test_soc_projection_rises_and_caps():
    cfg = Config(capacity_kwh=10.0, soc_target=90.0, max_charge_w=5000.0, eta_charge=1.0)
    slots = _slots(4)
    # all grid-charge hours: 5000 W * 1 h = 5 kWh = 50% of a 10 kWh battery per hour
    selected = [s.start for s in slots]
    out = plan.build_plan_horizon(slots, [], selected, 0.0, BASE + timedelta(hours=4), cfg)
    socs = [e["soc"] for e in out]
    assert socs[0] == 50.0           # 0 -> 50
    assert socs[1] == 90.0           # capped at target (would be 100)
    assert socs[-1] == 90.0          # stays capped
    assert all(e["mode"] == "grid" for e in out)


def test_past_deadline_flag_and_missing_interval():
    cfg = Config()
    slots = _slots(2)
    deadline = BASE + timedelta(hours=1)  # slot[1] is at/after deadline
    out = plan.build_plan_horizon(slots, [], [], 50.0, deadline, cfg)
    assert out[0]["is_past_horizon"] is False
    assert out[1]["is_past_horizon"] is True
    assert out[0]["pv_w"] is None and out[0]["load_w"] is None  # no intervals supplied
    assert out[0]["mode"] == "idle"


class _StubPredictor:
    def predict(self, when, temp, fallback, **kwargs):
        return 500.0


def test_display_intervals_fill_pv_and_load():
    # slots 09:00..15:00; now=11:00; PV only at 12:00
    now = datetime(2026, 6, 20, 11, 0, tzinfo=timezone.utc)
    slots = [PriceSlot(datetime(2026, 6, 20, h, 0, tzinfo=timezone.utc), 0.30) for h in range(9, 16)]
    pv_curve = [(datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc), 2000.0)]
    out = plan.build_display_intervals(slots, now, pv_curve, _StubPredictor(), None, 400.0)
    starts = [iv.start for iv in out]
    # past hours (09,10) dropped; starts at 11:00
    assert starts[0] == datetime(2026, 6, 20, 11, 0, tzinfo=timezone.utc)
    assert all(s >= now for s in starts)
    by_hour = {iv.start.hour: iv for iv in out}
    assert by_hour[12].pv_w == 2000.0          # daylight curve value
    assert by_hour[11].pv_w == 0.0             # no curve point -> 0
    assert all(iv.load_w == 500.0 for iv in out)  # predicted every hour
    assert all(iv.dt_h == 1.0 for iv in out)


def test_display_intervals_empty_slots():
    now = datetime(2026, 6, 20, 11, 0, tzinfo=timezone.utc)
    assert plan.build_display_intervals([], now, [], _StubPredictor(), None, 400.0) == []


def test_soc_discharges_on_deficit():
    # idle hour with load > pv must LOWER soc by discharge energy / eta_discharge
    cfg = Config(capacity_kwh=10.0, soc_floor=0.0, soc_target=100.0,
                 max_charge_w=6000.0, eta_charge=1.0, round_trip_eff=0.5)
    slots = _slots(1)
    intervals = [ForecastInterval(BASE, pv_w=0.0, load_w=1000.0, dt_h=1.0)]
    out = plan.build_plan_horizon(slots, intervals, [], 50.0, BASE + timedelta(hours=1), cfg)
    # eta_discharge = round_trip_eff/eta_charge = 0.5; dc drawn = 1000/0.5 = 2000 Wh
    # dSoC = -2000/10000*100 = -20  -> 30.0
    assert out[0]["mode"] == "idle"
    assert out[0]["soc"] == 30.0
    assert out[0]["charge_w"] == 0.0


def test_soc_discharge_clamped_at_floor():
    cfg = Config(capacity_kwh=10.0, soc_floor=10.0, soc_target=100.0,
                 max_charge_w=6000.0, eta_charge=1.0, round_trip_eff=1.0)
    slots = _slots(3)
    intervals = [
        ForecastInterval(BASE + timedelta(hours=i), pv_w=0.0, load_w=6000.0, dt_h=1.0)
        for i in range(3)
    ]
    out = plan.build_plan_horizon(slots, intervals, [], 20.0, BASE + timedelta(hours=3), cfg)
    socs = [e["soc"] for e in out]
    # hour0: 20 - 60 -> floored to 10; stays at 10 thereafter
    assert socs == [10.0, 10.0, 10.0]


def test_soc_discharge_capped_load_by_max_charge_w():
    # discharge AC is capped at max_charge_w even if load is huge
    cfg = Config(capacity_kwh=10.0, soc_floor=0.0, soc_target=100.0,
                 max_charge_w=2000.0, eta_charge=1.0, round_trip_eff=1.0)
    slots = _slots(1)
    intervals = [ForecastInterval(BASE, pv_w=0.0, load_w=9000.0, dt_h=1.0)]
    out = plan.build_plan_horizon(slots, intervals, [], 50.0, BASE + timedelta(hours=1), cfg)
    # discharge capped at 2000 W; dSoC = -2000/10000*100 = -20 -> 30
    assert out[0]["soc"] == 30.0


def test_eta_discharge_clamped_to_one():
    # When round_trip_eff/eta_charge > 1.0 the ratio must be clamped to 1.0.
    # eta_charge=0.8, round_trip_eff=1.0 -> raw ratio 1.25, clamped to 1.0.
    # idle hour with load_w=1000, pv_w=0 -> discharge=1000W ->
    # dSoC = -(1000/1.0) * 1 / 10000 * 100 = -10.0, NOT -8.0 (unclamped).
    cfg = Config(capacity_kwh=10.0, soc_floor=0.0, soc_target=100.0,
                 max_charge_w=6000.0, eta_charge=0.8, round_trip_eff=1.0)
    slots = _slots(1)
    intervals = [ForecastInterval(BASE, pv_w=0.0, load_w=1000.0, dt_h=1.0)]
    out = plan.build_plan_horizon(slots, intervals, [], 50.0, BASE + timedelta(hours=1), cfg)
    assert out[0]["soc"] == 40.0  # 50 - 10 = 40 (clamped ratio → -10, not -8)


def test_soc_no_discharge_when_interval_missing():
    # no interval supplied -> no discharge, soc flat (regression for null handling)
    cfg = Config(capacity_kwh=10.0, soc_floor=0.0, soc_target=100.0)
    slots = _slots(1)
    out = plan.build_plan_horizon(slots, [], [], 50.0, BASE + timedelta(hours=1), cfg)
    assert out[0]["soc"] == 50.0
    assert out[0]["mode"] == "idle"


def test_build_display_horizon_none_sun_times_returns_empty():
    now = datetime(2026, 6, 20, 11, 0, tzinfo=timezone.utc)
    slots = _slots(3)
    out = plan.build_display_horizon(
        slots, now, today_arrays=[(1.0, None)], tomorrow_arrays=[(6.0, None)], sun_times=None,
        predictor=_StubPredictor(), cur_temp=None, fallback_w=400.0,
        soc=50.0, selected=[], horizon_edge=now, cfg=Config(),
    )
    assert out == []


def test_build_display_horizon_self_consumption_no_grid():
    now = datetime(2026, 6, 20, 17, 0, tzinfo=timezone.utc)
    slots = [PriceSlot(now + timedelta(hours=i), 0.30) for i in range(30)]
    sun_times = (
        datetime(2026, 6, 20, 20, 0, tzinfo=timezone.utc),   # today_sunset
        datetime(2026, 6, 21, 6, 0, tzinfo=timezone.utc),    # tomorrow_sunrise
        datetime(2026, 6, 21, 20, 0, tzinfo=timezone.utc),   # tomorrow_sunset
    )
    out = plan.build_display_horizon(
        slots, now, today_arrays=[(1.0, None)], tomorrow_arrays=[(6.0, None)],
        sun_times=sun_times,
        predictor=_StubPredictor(), cur_temp=15.0, fallback_w=400.0,
        soc=50.0, selected=[], horizon_edge=now, cfg=Config(),
    )
    assert out, "expected a non-empty horizon"
    assert all(e["mode"] != "grid" for e in out)        # selected=[] -> never grid
    assert any(e["pv_w"] and e["pv_w"] > 0 for e in out)  # tomorrow daytime PV present
    assert all(e["load_w"] == 500.0 for e in out)          # _StubPredictor returns 500


def test_build_display_horizon_energy_conserved():
    """Tomorrow-only PV: total pv_w in horizon ≈ sum of kWh * 1000 Wh."""
    now = datetime(2026, 6, 20, 17, 0, tzinfo=timezone.utc)
    slots = [PriceSlot(now + timedelta(hours=i), 0.30) for i in range(30)]
    sun_times = (
        datetime(2026, 6, 20, 20, 0, tzinfo=timezone.utc),   # today_sunset
        datetime(2026, 6, 21, 6, 0, tzinfo=timezone.utc),    # tomorrow_sunrise
        datetime(2026, 6, 21, 20, 0, tzinfo=timezone.utc),   # tomorrow_sunset
    )
    tomorrow_kwh = 6.0
    out = plan.build_display_horizon(
        slots, now, today_arrays=None, tomorrow_arrays=[(tomorrow_kwh, None)],
        sun_times=sun_times,
        predictor=_StubPredictor(), cur_temp=None, fallback_w=400.0,
        soc=50.0, selected=[], horizon_edge=now, cfg=Config(),
    )
    assert out, "expected a non-empty horizon"
    total_pv_wh = sum(e["pv_w"] for e in out if e["pv_w"])
    # Each horizon entry is 1 hour; pv_w in watts → energy in Wh per slot.
    # All tomorrow daytime slots are in the future so no hours are clipped.
    assert abs(total_pv_wh - tomorrow_kwh * 1000) < 100, (
        f"Expected ~{tomorrow_kwh * 1000} Wh, got {total_pv_wh:.1f} Wh"
    )


def test_build_display_horizon_shoulder_lift():
    """E/W split arrays: early-peak and late-peak hours are HIGHER than single centred array,
    while midday (13:00) is LOWER — proves timing fidelity, not a higher global peak."""
    now = datetime(2026, 6, 20, 17, 0, tzinfo=timezone.utc)
    slots = [PriceSlot(now + timedelta(hours=i), 0.30) for i in range(30)]
    sun_times = (
        datetime(2026, 6, 20, 20, 0, tzinfo=timezone.utc),
        datetime(2026, 6, 21, 6, 0, tzinfo=timezone.utc),
        datetime(2026, 6, 21, 20, 0, tzinfo=timezone.utc),
    )
    early_peak = datetime(2026, 6, 21, 9, 0, tzinfo=timezone.utc)   # E-array peak
    late_peak = datetime(2026, 6, 21, 17, 0, tzinfo=timezone.utc)  # W-array peak
    mid_hour = datetime(2026, 6, 21, 13, 0, tzinfo=timezone.utc)   # midday valley

    ew_arrays = [(3.0, early_peak), (3.0, late_peak)]
    centered_arrays = [(6.0, None)]  # peaks at window midpoint ≈ 13:00

    def _build(tomorrow_arrays):
        return plan.build_display_horizon(
            slots, now, today_arrays=None, tomorrow_arrays=tomorrow_arrays,
            sun_times=sun_times,
            predictor=_StubPredictor(), cur_temp=None, fallback_w=400.0,
            soc=50.0, selected=[], horizon_edge=now, cfg=Config(),
        )

    ew_horizon = _build(ew_arrays)
    centered_horizon = _build(centered_arrays)

    def _pv_at(horizon, dt):
        key = dt.isoformat()
        for e in horizon:
            if e["start"] == key:
                return e["pv_w"]
        return None

    ew_at_early = _pv_at(ew_horizon, early_peak)
    ew_at_late = _pv_at(ew_horizon, late_peak)
    ew_at_mid = _pv_at(ew_horizon, mid_hour)
    centered_at_early = _pv_at(centered_horizon, early_peak)
    centered_at_late = _pv_at(centered_horizon, late_peak)
    centered_at_mid = _pv_at(centered_horizon, mid_hour)

    assert ew_at_early is not None and centered_at_early is not None
    assert ew_at_late is not None and centered_at_late is not None
    assert ew_at_mid is not None and centered_at_mid is not None

    # Shoulders are LIFTED in the E/W case (timing fidelity)
    assert ew_at_early > centered_at_early, (
        f"E/W pv_w at 09:00 ({ew_at_early:.1f}) must exceed centred ({centered_at_early:.1f})"
    )
    assert ew_at_late > centered_at_late, (
        f"E/W pv_w at 17:00 ({ew_at_late:.1f}) must exceed centred ({centered_at_late:.1f})"
    )
    # Midday is a valley in the E/W case
    assert ew_at_mid < centered_at_mid, (
        f"E/W pv_w at 13:00 ({ew_at_mid:.1f}) must be below centred ({centered_at_mid:.1f})"
    )


def test_charge_w_in_horizon_entries():
    """Each horizon entry must expose charge_w reflecting actual AC power, not price."""
    cfg = Config(capacity_kwh=10.0, soc_target=100.0, max_charge_w=3000.0, eta_charge=1.0)
    slots = _slots(3)
    intervals = [
        # hour 0: solar surplus below max_charge_w  → charge_w == pv_w - load_w
        ForecastInterval(BASE, pv_w=2000.0, load_w=500.0, dt_h=1.0),
        # hour 1: grid charge                        → charge_w == max_charge_w
        ForecastInterval(BASE + timedelta(hours=1), pv_w=0.0, load_w=400.0, dt_h=1.0),
        # hour 2: idle (no sun, not grid)            → charge_w == 0.0
        ForecastInterval(BASE + timedelta(hours=2), pv_w=0.0, load_w=400.0, dt_h=1.0),
    ]
    selected = [BASE + timedelta(hours=1)]  # only hour 1 is a grid-charge hour
    out = plan.build_plan_horizon(slots, intervals, selected, 50.0, BASE + timedelta(hours=3), cfg)

    assert [e["mode"] for e in out] == ["solar", "grid", "idle"]

    # solar hour: AC = pv_w - load_w = 2000 - 500 = 1500 W (below max_charge_w)
    assert out[0]["charge_w"] == 1500.0
    # solar hour: solar bar = surplus, grid bar = 0
    assert out[0]["solar_charge_w"] == 1500.0
    assert out[0]["grid_charge_w"] == 0.0

    # grid hour: AC = max_charge_w
    assert out[1]["charge_w"] == 3000.0
    # grid hour (no solar): grid bar = max_charge_w, solar bar = 0
    assert out[1]["solar_charge_w"] == 0.0
    assert out[1]["grid_charge_w"] == 3000.0

    # idle hour: AC = 0
    assert out[2]["charge_w"] == 0.0


def test_solar_and_grid_coexist_in_grid_hour():
    # Grid-requested hour that ALSO has solar surplus: both bars > 0, summing
    # to the requested total, never exceeding max_charge_w.
    cfg = Config(capacity_kwh=10.0, soc_floor=0.0, soc_target=100.0,
                 max_charge_w=6000.0, eta_charge=1.0)
    slots = _slots(1)
    intervals = [ForecastInterval(BASE, pv_w=2000.0, load_w=800.0, dt_h=1.0)]  # surplus 1200
    selected = [BASE]
    out = plan.build_plan_horizon(
        slots, intervals, selected, 60.0, BASE + timedelta(hours=1), cfg,
        grid_request_by_hour={BASE: 6000.0},  # ask for full rate
    )
    e = out[0]
    assert e["mode"] == "grid"
    assert e["solar_charge_w"] == 1200.0           # solar first
    assert e["grid_charge_w"] == 2800.0            # headroom(4000) - solar(1200) = 2800
    assert e["charge_w"] == 4000.0                 # total lands exactly at soc_target


def test_grid_request_below_remaining_rate():
    cfg = Config(capacity_kwh=10.0, soc_floor=0.0, soc_target=100.0,
                 max_charge_w=6000.0, eta_charge=1.0)
    slots = _slots(1)
    intervals = [ForecastInterval(BASE, pv_w=1000.0, load_w=470.0, dt_h=1.0)]  # surplus 530
    out = plan.build_plan_horizon(
        slots, intervals, [BASE], 50.0, BASE + timedelta(hours=1), cfg,
        grid_request_by_hour={BASE: 800.0},
    )
    assert out[0]["solar_charge_w"] == 530.0
    assert out[0]["grid_charge_w"] == 800.0
    assert out[0]["charge_w"] == 1330.0


def test_grid_bar_collapses_when_battery_full():
    # Near-full battery: headroom ~0 -> grid bar ~0 (no phantom max_charge_w),
    # but mode stays "grid" (hour is selected) so planned_grid_hours is intact.
    cfg = Config(capacity_kwh=10.0, soc_floor=0.0, soc_target=97.0,
                 max_charge_w=6000.0, eta_charge=1.0)
    slots = _slots(1)
    intervals = [ForecastInterval(BASE, pv_w=0.0, load_w=0.0, dt_h=1.0)]
    out = plan.build_plan_horizon(
        slots, intervals, [BASE], 97.0, BASE + timedelta(hours=1), cfg,
        grid_request_by_hour={BASE: 6000.0},
    )
    assert out[0]["mode"] == "grid"
    assert out[0]["grid_charge_w"] == 0.0
    assert out[0]["solar_charge_w"] == 0.0


def test_heuristic_grid_hour_defaults_to_max_charge_w():
    # grid_request_by_hour=None -> selected grid hour with no solar requests
    # full rate (back-compat with the pre-change single-mode value).
    cfg = Config(capacity_kwh=10.0, soc_target=100.0, max_charge_w=3000.0, eta_charge=1.0)
    slots = _slots(1)
    intervals = [ForecastInterval(BASE, pv_w=0.0, load_w=400.0, dt_h=1.0)]
    out = plan.build_plan_horizon(slots, intervals, [BASE], 50.0,
                                  BASE + timedelta(hours=1), cfg)
    assert out[0]["mode"] == "grid"
    assert out[0]["grid_charge_w"] == 3000.0
    assert out[0]["solar_charge_w"] == 0.0
    assert out[0]["charge_w"] == 3000.0


# ---------------------------------------------------------------------------
# G1 tests: export/reserve fields in plan horizon
# ---------------------------------------------------------------------------


def test_export_hour_sets_grid_export_w_and_drains_soc():
    """Export hour: grid_export_w is set and projected SoC drops by exported energy."""
    cfg = Config(
        capacity_kwh=10.0, soc_floor=5.0, soc_target=100.0,
        max_charge_w=3000.0, eta_charge=1.0, round_trip_eff=1.0,
    )
    slots = _slots(2)
    # Hour 0: export 2000 W, pv covers load (no self-discharge from battery)
    # Hour 1: idle, no export
    intervals = [
        ForecastInterval(BASE, pv_w=3000.0, load_w=1000.0, dt_h=1.0),
        ForecastInterval(BASE + timedelta(hours=1), pv_w=0.0, load_w=500.0, dt_h=1.0),
    ]
    export_req = {BASE: 2000.0}
    out = plan.build_plan_horizon(
        slots, intervals, [], 80.0, BASE + timedelta(hours=2), cfg,
        export_request_by_hour=export_req,
    )
    # Export hour: field populated
    assert out[0]["grid_export_w"] == 2000.0
    # Export drains SoC: 2000 W * 1 h / 10000 Wh * 100 = 20 % drop (eta=1.0)
    # solar_charge from surplus (3000-1000=2000 W), but no grid charge (not selected).
    # soc_sim starts at 80. solar_charge_w = min(2000, 3000, headroom)
    # headroom = (100-80)/100 * 10000 / (1.0*1) = 2000 W
    # solar_charge_w = min(2000, 3000, 2000) = 2000
    # SoC after charge = 80 + 2000*1/10000*100 = 80 + 20 = 100
    # SoC after export = 100 - 2000*1/10000*100 = 100 - 20 = 80
    # capped to [soc_floor=5, soc_target=100] -> 80
    assert out[0]["soc"] == 80.0
    # Non-export hour: field is zero
    assert out[1]["grid_export_w"] == 0.0


def test_export_drains_soc_sim_no_solar():
    """Export from battery-only hour (no PV): SoC drops by the exported energy."""
    cfg = Config(
        capacity_kwh=10.0, soc_floor=5.0, soc_target=100.0,
        max_charge_w=5000.0, eta_charge=1.0, round_trip_eff=1.0,
    )
    slots = _slots(1)
    intervals = [ForecastInterval(BASE, pv_w=0.0, load_w=0.0, dt_h=1.0)]
    export_req = {BASE: 3000.0}  # 3000 W * 1 h = 3 kWh = 30% of 10 kWh
    out = plan.build_plan_horizon(
        slots, intervals, [], 70.0, BASE + timedelta(hours=1), cfg,
        export_request_by_hour=export_req,
    )
    assert out[0]["grid_export_w"] == 3000.0
    # SoC: 70 - 30 = 40, capped to [5, 100] -> 40
    assert out[0]["soc"] == 40.0


def test_non_export_hour_grid_export_w_is_zero():
    """Hours without an export request emit grid_export_w == 0."""
    cfg = Config(capacity_kwh=10.0, soc_floor=5.0, soc_target=100.0, max_charge_w=3000.0)
    slots = _slots(2)
    out = plan.build_plan_horizon(slots, [], [], 50.0, BASE + timedelta(hours=2), cfg)
    assert out[0]["grid_export_w"] == 0.0
    assert out[1]["grid_export_w"] == 0.0


def test_self_discharge_w_set_in_battery_covering_load():
    """Self-discharge: battery covers load deficit when no PV and not a grid hour."""
    cfg = Config(
        capacity_kwh=10.0, soc_floor=5.0, soc_target=100.0,
        max_charge_w=3000.0, eta_charge=1.0, round_trip_eff=1.0,
    )
    slots = _slots(1)
    # No PV, 1500 W load -> battery discharges 1500 W
    intervals = [ForecastInterval(BASE, pv_w=0.0, load_w=1500.0, dt_h=1.0)]
    out = plan.build_plan_horizon(slots, intervals, [], 80.0, BASE + timedelta(hours=1), cfg)
    assert out[0]["self_discharge_w"] == 1500.0
    assert out[0]["grid_export_w"] == 0.0


def test_self_discharge_w_zero_in_solar_surplus_hour():
    """Solar surplus hour: battery not discharging, self_discharge_w == 0."""
    cfg = Config(capacity_kwh=10.0, soc_floor=5.0, soc_target=100.0, max_charge_w=3000.0)
    slots = _slots(1)
    intervals = [ForecastInterval(BASE, pv_w=3000.0, load_w=500.0, dt_h=1.0)]
    out = plan.build_plan_horizon(slots, intervals, [], 50.0, BASE + timedelta(hours=1), cfg)
    assert out[0]["self_discharge_w"] == 0.0


def test_reserve_soc_present_and_within_bounds():
    """reserve_soc is present and within [soc_floor, 100] when reserve_by_hour supplied."""
    cap_kwh = 10.0
    cfg = Config(
        capacity_kwh=cap_kwh, soc_floor=5.0, soc_target=100.0, max_charge_w=3000.0,
    )
    slots = _slots(2)
    # 2 kWh reserve = 20% of 10 kWh
    reserve_by_hour = {BASE: 2.0, BASE + timedelta(hours=1): 3.0}
    out = plan.build_plan_horizon(
        slots, [], [], 50.0, BASE + timedelta(hours=2), cfg,
        reserve_by_hour=reserve_by_hour,
    )
    assert out[0]["reserve_soc"] == pytest.approx(20.0)
    assert out[1]["reserve_soc"] == pytest.approx(30.0)
    # Both within [soc_floor=5, 100]
    for entry in out:
        assert cfg.soc_floor <= entry["reserve_soc"] <= 100.0


def test_reserve_soc_defaults_to_soc_floor_when_no_reserve_by_hour():
    """When reserve_by_hour is None, reserve_soc defaults to cfg.soc_floor."""
    cfg = Config(capacity_kwh=10.0, soc_floor=5.0, soc_target=100.0, max_charge_w=3000.0)
    slots = _slots(1)
    out = plan.build_plan_horizon(slots, [], [], 50.0, BASE + timedelta(hours=1), cfg)
    assert out[0]["reserve_soc"] == pytest.approx(5.0)


def test_export_and_self_discharge_are_separate_fields():
    """Export hour: grid_export_w and self_discharge_w are independent fields."""
    cfg = Config(
        capacity_kwh=10.0, soc_floor=5.0, soc_target=100.0,
        max_charge_w=3000.0, eta_charge=1.0, round_trip_eff=1.0,
    )
    slots = _slots(1)
    # PV covers partial load, battery covers the rest (no export)
    intervals = [ForecastInterval(BASE, pv_w=500.0, load_w=1500.0, dt_h=1.0)]
    export_req = {BASE: 2000.0}
    out = plan.build_plan_horizon(
        slots, intervals, [], 80.0, BASE + timedelta(hours=1), cfg,
        export_request_by_hour=export_req,
    )
    # load_w (1500) > pv_w (500), so self-discharge = min(1500-500, max_charge_w) = 1000
    assert out[0]["self_discharge_w"] == 1000.0
    assert out[0]["grid_export_w"] == 2000.0


# ---------------------------------------------------------------------------
# Tests for build_display_horizon — export_request_by_hour + reserve_by_hour
# ---------------------------------------------------------------------------

def _sun_times_for(now: datetime):
    """Standard sun_times tuple starting from 'now'."""
    return (
        now + timedelta(hours=9),   # today_sunset
        now + timedelta(hours=19),  # tomorrow_sunrise
        now + timedelta(hours=33),  # tomorrow_sunset
    )


def test_build_display_horizon_export_request_sets_grid_export_w():
    """export_request_by_hour is threaded through: export hour has grid_export_w > 0,
    non-export hour has grid_export_w == 0."""
    now = datetime(2026, 6, 20, 17, 0, tzinfo=timezone.utc)
    slots = [PriceSlot(now + timedelta(hours=i), 0.30) for i in range(30)]
    sun_times = _sun_times_for(now)
    cfg = Config(
        capacity_kwh=10.0, soc_floor=5.0, soc_target=100.0,
        max_charge_w=3000.0, eta_charge=1.0, round_trip_eff=1.0,
        max_export_w=3000.0, grid_export_limit_w=6000.0,
    )
    export_hour = now.replace(minute=0, second=0, microsecond=0)
    export_req = {export_hour: 2000.0}
    out = plan.build_display_horizon(
        slots, now, today_arrays=[(1.0, None)], tomorrow_arrays=[(6.0, None)],
        sun_times=sun_times,
        predictor=_StubPredictor(), cur_temp=None, fallback_w=400.0,
        soc=80.0, selected=[], horizon_edge=now, cfg=cfg,
        export_request_by_hour=export_req,
    )
    assert out, "expected non-empty horizon"
    first = out[0]
    assert first["grid_export_w"] == 2000.0, (
        f"Export hour must have grid_export_w=2000.0, got {first['grid_export_w']}"
    )
    # Second hour onwards: no export scheduled → 0
    for entry in out[1:]:
        assert entry["grid_export_w"] == 0.0, (
            f"Non-export hour must have grid_export_w=0.0, got {entry['grid_export_w']} at {entry['start']}"
        )


def test_build_display_horizon_export_drains_soc():
    """Export drains the projected SoC — the SoC after the export hour is lower
    than it would be without export."""
    now = datetime(2026, 6, 20, 17, 0, tzinfo=timezone.utc)
    slots = [PriceSlot(now + timedelta(hours=i), 0.30) for i in range(30)]
    sun_times = _sun_times_for(now)
    cfg = Config(
        capacity_kwh=10.0, soc_floor=5.0, soc_target=100.0,
        max_charge_w=3000.0, eta_charge=1.0, round_trip_eff=1.0,
        max_export_w=3000.0, grid_export_limit_w=6000.0,
    )
    export_hour = now.replace(minute=0, second=0, microsecond=0)
    export_req = {export_hour: 2000.0}  # 2000 W for 1 hour = 2 kWh = 20% of 10 kWh

    without_export = plan.build_display_horizon(
        slots, now, today_arrays=[(1.0, None)], tomorrow_arrays=[(6.0, None)],
        sun_times=sun_times,
        predictor=_StubPredictor(), cur_temp=None, fallback_w=400.0,
        soc=80.0, selected=[], horizon_edge=now, cfg=cfg,
    )
    with_export = plan.build_display_horizon(
        slots, now, today_arrays=[(1.0, None)], tomorrow_arrays=[(6.0, None)],
        sun_times=sun_times,
        predictor=_StubPredictor(), cur_temp=None, fallback_w=400.0,
        soc=80.0, selected=[], horizon_edge=now, cfg=cfg,
        export_request_by_hour=export_req,
    )
    assert without_export and with_export, "both horizons must be non-empty"
    # SoC after export hour must be LOWER than without export
    soc_after_without = without_export[0]["soc"]
    soc_after_with = with_export[0]["soc"]
    assert soc_after_with < soc_after_without, (
        f"Export must drain SoC: with_export={soc_after_with:.1f}% >= without={soc_after_without:.1f}%"
    )


def test_build_display_horizon_reserve_by_hour_sets_reserve_soc():
    """reserve_by_hour is threaded through: reserve_soc reflects supplied per-hour reserve
    (NOT flat soc_floor)."""
    now = datetime(2026, 6, 20, 17, 0, tzinfo=timezone.utc)
    slots = [PriceSlot(now + timedelta(hours=i), 0.30) for i in range(30)]
    sun_times = _sun_times_for(now)
    cap_kwh = 10.0
    cfg = Config(
        capacity_kwh=cap_kwh, soc_floor=5.0, soc_target=100.0, max_charge_w=3000.0,
    )
    h0 = now.replace(minute=0, second=0, microsecond=0)
    h1 = h0 + timedelta(hours=1)
    # 3 kWh reserve = 30%, 4 kWh reserve = 40%
    reserve_by_hour = {h0: 3.0, h1: 4.0}
    out = plan.build_display_horizon(
        slots, now, today_arrays=[(1.0, None)], tomorrow_arrays=[(6.0, None)],
        sun_times=sun_times,
        predictor=_StubPredictor(), cur_temp=None, fallback_w=400.0,
        soc=50.0, selected=[], horizon_edge=now, cfg=cfg,
        reserve_by_hour=reserve_by_hour,
    )
    assert out, "expected non-empty horizon"
    # Hour 0 should have reserve_soc ~30% (3 kWh / 10 kWh)
    assert out[0]["reserve_soc"] == pytest.approx(30.0), (
        f"reserve_soc for h0 expected ~30.0, got {out[0]['reserve_soc']}"
    )
    # Hour 1 should have reserve_soc ~40% (4 kWh / 10 kWh)
    assert out[1]["reserve_soc"] == pytest.approx(40.0), (
        f"reserve_soc for h1 expected ~40.0, got {out[1]['reserve_soc']}"
    )
    # All within [floor, 100]
    for entry in out:
        assert cfg.soc_floor <= entry["reserve_soc"] <= 100.0, (
            f"reserve_soc {entry['reserve_soc']} out of bounds at {entry['start']}"
        )


def test_build_display_intervals_uses_per_hour_temp_map():
    """A per-hour temp_by_hour map overrides the scalar cur_temp per slot."""
    from datetime import datetime, timezone, timedelta
    from custom_components.anker_x1_smartgrid.plan import build_display_intervals
    from custom_components.anker_x1_smartgrid.models import PriceSlot

    now = datetime(2026, 6, 29, 0, tzinfo=timezone.utc)
    slots = [PriceSlot(start=now + timedelta(hours=h), price=0.2) for h in range(3)]
    pv_curve = []
    seen = {}

    class _RecordingPredictor:
        def predict(self, when, temp, fallback_w, *, quantile=0.5):
            seen[when] = temp
            return 500.0

    temp_by_hour = {now: 5.0, now + timedelta(hours=1): 12.0}  # hour 2 absent → falls back
    build_display_intervals(
        slots, now, pv_curve, _RecordingPredictor(), -99.0, 400.0,
        temp_by_hour=temp_by_hour,
    )
    assert seen[now] == 5.0
    assert seen[now + timedelta(hours=1)] == 12.0
    assert seen[now + timedelta(hours=2)] == -99.0  # absent hour → scalar cur_temp


def test_eta_charge_guard_unified_at_subnano_boundary():
    """Sub-1e-9 eta_charge must hit the same fallback as eta_charge=0:
    finite projected SoC, no blow-up, identical trajectory (eta_discharge=1.0)."""
    common = dict(capacity_kwh=10.0, soc_target=90.0, soc_floor=5.0,
                  max_charge_w=3000.0, round_trip_eff=0.85)
    slots = _slots(2)
    # An idle hour where load>pv self-discharges the SoC sim by load/eta_discharge.
    intervals = [ForecastInterval(BASE, pv_w=0.0, load_w=1000.0, dt_h=1.0)]
    out_tiny = plan.build_plan_horizon(
        slots, intervals, [], 50.0, BASE + timedelta(hours=2),
        Config(eta_charge=5e-10, **common))
    out_zero = plan.build_plan_horizon(
        slots, intervals, [], 50.0, BASE + timedelta(hours=2),
        Config(eta_charge=0.0, **common))
    socs_tiny = [e["soc"] for e in out_tiny]
    assert all(s == s and abs(s) < 1e6 for s in socs_tiny)          # finite, no inf/nan
    assert socs_tiny == [e["soc"] for e in out_zero]                # same fallback path


def test_build_plan_horizon_accepts_eta_curve_none_identical():
    """Adding the eta_curve kwarg must not change default behaviour: a call that
    omits it must be byte-identical to one that passes eta_curve=None explicitly."""
    cfg = Config(capacity_kwh=10.0, soc_floor=5.0, soc_target=90.0,
                 max_charge_w=3000.0, eta_charge=0.9, round_trip_eff=0.8)
    slots = _slots(3)
    intervals = [
        ForecastInterval(BASE, pv_w=2000.0, load_w=300.0, dt_h=1.0),
        ForecastInterval(BASE + timedelta(hours=1), pv_w=0.0, load_w=1200.0, dt_h=1.0),
        ForecastInterval(BASE + timedelta(hours=2), pv_w=0.0, load_w=400.0, dt_h=1.0),
    ]
    selected = [BASE + timedelta(hours=1)]
    export = {BASE + timedelta(hours=2): 500.0}

    out_omitted = plan.build_plan_horizon(
        slots, intervals, selected, 50.0, BASE + timedelta(hours=3), cfg,
        export_request_by_hour=export,
    )
    out_explicit_none = plan.build_plan_horizon(
        slots, intervals, selected, 50.0, BASE + timedelta(hours=3), cfg,
        export_request_by_hour=export, eta_curve=None,
    )
    assert out_omitted == out_explicit_none


def test_build_plan_horizon_eta_curve_static_matches_default():
    """EfficiencyCurve.static(cfg) encodes the exact same scalars the eta_curve=None
    path derives from cfg — so substituting it must produce a byte-identical horizon."""
    from custom_components.anker_x1_smartgrid.efficiency import EfficiencyCurve

    cfg = Config(capacity_kwh=10.0, soc_floor=5.0, soc_target=90.0,
                 max_charge_w=3000.0, eta_charge=0.9, round_trip_eff=0.8)
    slots = _slots(3)
    intervals = [
        ForecastInterval(BASE, pv_w=2000.0, load_w=300.0, dt_h=1.0),
        ForecastInterval(BASE + timedelta(hours=1), pv_w=0.0, load_w=1200.0, dt_h=1.0),
        ForecastInterval(BASE + timedelta(hours=2), pv_w=0.0, load_w=400.0, dt_h=1.0),
    ]
    selected = [BASE + timedelta(hours=1)]
    export = {BASE + timedelta(hours=2): 500.0}
    curve = EfficiencyCurve.static(cfg)

    out_none = plan.build_plan_horizon(
        slots, intervals, selected, 50.0, BASE + timedelta(hours=3), cfg,
        export_request_by_hour=export, eta_curve=None,
    )
    out_curve = plan.build_plan_horizon(
        slots, intervals, selected, 50.0, BASE + timedelta(hours=3), cfg,
        export_request_by_hour=export, eta_curve=curve,
    )
    assert out_none == out_curve


def test_build_display_horizon_accepts_eta_curve_kwarg():
    """build_display_horizon threads eta_curve straight through to build_plan_horizon;
    eta_curve=None (default) must be byte-identical to omitting it."""
    now = datetime(2026, 6, 20, 17, 0, tzinfo=timezone.utc)
    slots = [PriceSlot(now + timedelta(hours=i), 0.30) for i in range(30)]
    sun_times = (
        datetime(2026, 6, 20, 20, 0, tzinfo=timezone.utc),   # today_sunset
        datetime(2026, 6, 21, 6, 0, tzinfo=timezone.utc),    # tomorrow_sunrise
        datetime(2026, 6, 21, 20, 0, tzinfo=timezone.utc),   # tomorrow_sunset
    )
    kwargs = dict(
        today_arrays=[(1.0, None)], tomorrow_arrays=[(6.0, None)],
        sun_times=sun_times,
        predictor=_StubPredictor(), cur_temp=15.0, fallback_w=400.0,
        soc=50.0, selected=[], horizon_edge=now, cfg=Config(),
    )
    out_omitted = plan.build_display_horizon(slots, now, **kwargs)
    out_explicit_none = plan.build_display_horizon(slots, now, eta_curve=None, **kwargs)
    assert out_omitted == out_explicit_none


class _TempEchoPredictor:
    """Returns temp * 10 so the observed load_w reveals which temp was used."""
    def predict(self, when, temp, fallback_w, *, quantile=0.5):
        return temp * 10.0


def test_build_display_horizon_forwards_temp_by_hour():
    """Regression: build_display_horizon must forward temp_by_hour to
    build_display_intervals so every FUTURE hour of the published horizon is
    predicted at its own forecast temp, not compute_decision's flat cur_temp
    scalar (the display-only load-inflation bug)."""
    now = datetime(2026, 6, 20, 17, 0, tzinfo=timezone.utc)
    slots = [PriceSlot(now + timedelta(hours=i), 0.30) for i in range(4)]
    sun_times = (
        datetime(2026, 6, 20, 20, 0, tzinfo=timezone.utc),
        datetime(2026, 6, 21, 6, 0, tzinfo=timezone.utc),
        datetime(2026, 6, 21, 20, 0, tzinfo=timezone.utc),
    )
    hour0 = now.replace(minute=0, second=0, microsecond=0)
    hour1 = hour0 + timedelta(hours=1)
    hour2 = hour1 + timedelta(hours=1)  # intentionally absent from temp_by_hour
    temp_by_hour = {hour0: 5.0, hour1: 12.0}

    out = plan.build_display_horizon(
        slots, now, today_arrays=None, tomorrow_arrays=[(6.0, None)], sun_times=sun_times,
        predictor=_TempEchoPredictor(), cur_temp=20.0, fallback_w=400.0,
        soc=50.0, selected=[], horizon_edge=now, cfg=Config(),
        temp_by_hour=temp_by_hour,
    )
    by_start = {e["start"]: e for e in out}
    assert by_start[hour0.isoformat()]["load_w"] == 50.0    # 5.0 * 10 (per-hour temp)
    assert by_start[hour1.isoformat()]["load_w"] == 120.0   # 12.0 * 10 (per-hour temp)
    assert by_start[hour2.isoformat()]["load_w"] == 200.0   # missing hour -> cur_temp (20.0) * 10


def test_build_display_horizon_omitting_temp_by_hour_uses_cur_temp():
    """Omitting temp_by_hour must preserve old behaviour: every hour predicted at cur_temp."""
    now = datetime(2026, 6, 20, 17, 0, tzinfo=timezone.utc)
    slots = [PriceSlot(now + timedelta(hours=i), 0.30) for i in range(4)]
    sun_times = (
        datetime(2026, 6, 20, 20, 0, tzinfo=timezone.utc),
        datetime(2026, 6, 21, 6, 0, tzinfo=timezone.utc),
        datetime(2026, 6, 21, 20, 0, tzinfo=timezone.utc),
    )
    out = plan.build_display_horizon(
        slots, now, today_arrays=None, tomorrow_arrays=[(6.0, None)], sun_times=sun_times,
        predictor=_TempEchoPredictor(), cur_temp=20.0, fallback_w=400.0,
        soc=50.0, selected=[], horizon_edge=now, cfg=Config(),
    )
    assert out, "expected a non-empty horizon"
    assert all(e["load_w"] == 200.0 for e in out)  # 20.0 * 10 everywhere
