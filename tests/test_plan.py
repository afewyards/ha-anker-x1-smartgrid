import itertools

import pytest
from datetime import datetime, timezone, timedelta, UTC
from custom_components.anker_x1_smartgrid.models import Config, PriceSlot, ForecastInterval
from custom_components.anker_x1_smartgrid import const, plan

BASE = datetime(2026, 6, 20, 11, 0, tzinfo=UTC)


def _slots(n, price=0.30):
    return [PriceSlot(BASE + timedelta(hours=i), price) for i in range(n)]


def test_empty_slots_returns_empty():
    assert plan.build_plan_horizon([], [], [], 50.0, BASE, Config()) == []


def test_modes_grid_solar_idle():
    cfg = Config(capacity_kwh=10.0, soc_target=100.0, max_charge_w=3000.0, eta_charge=1.0)
    slots = _slots(3)
    intervals = [
        ForecastInterval(BASE, pv_w=2000.0, load_w=300.0, dt_h=1.0),  # solar surplus
        ForecastInterval(BASE + timedelta(hours=1), pv_w=0.0, load_w=400.0, dt_h=1.0),  # no sun
        ForecastInterval(BASE + timedelta(hours=2), pv_w=0.0, load_w=400.0, dt_h=1.0),
    ]
    selected = [BASE + timedelta(hours=1)]  # planned grid charge at hour 1
    out = plan.build_plan_horizon(slots, intervals, selected, 50.0, BASE + timedelta(hours=3), cfg)
    assert [e["mode"] for e in out] == ["solar", "grid", "idle"]
    assert out[0]["pv_w"] == 2000.0
    assert out[0]["start"] == BASE.isoformat()
    assert out[0]["price"] == 0.30


def test_grid_import_limit_w_caps_grid_charge_w():
    """grid_import_limit_w tighter than the inverter rate clips the displayed grid bar.

    No solar: the inverter alone would allow 6000 W of grid charge, but the
    connection only allows 2000 W. soc starts at 0% (huge ceiling headroom)
    so the ceiling cap cannot be the one binding here. Mirrors regret._max_grid_dc.
    """
    cfg = Config(capacity_kwh=10.0, soc_target=100.0, max_charge_w=6000.0, eta_charge=1.0, grid_import_limit_w=2000.0)
    slots = _slots(1)
    intervals = [ForecastInterval(BASE, pv_w=0.0, load_w=0.0, dt_h=1.0)]
    selected = [BASE]
    out = plan.build_plan_horizon(slots, intervals, selected, 0.0, BASE + timedelta(hours=1), cfg)
    assert out[0]["grid_charge_w"] == 2000.0


def test_grid_import_limit_w_does_not_bind_solar_charge_w():
    """Solar charging is not grid import — it is untouched by grid_import_limit_w.

    4000 W solar surplus + a comfortably wider 5000 W import limit: solar
    still fills to the full inverter rate (4000 W), grid gets only the
    inverter remainder (2000 W) — the import limit never even has to bind for
    grid to fall below it, proving solar was never subject to it in the first
    place. soc starts at 0% so ceiling headroom cannot be the binding term.
    """
    cfg = Config(capacity_kwh=10.0, soc_target=100.0, max_charge_w=6000.0, eta_charge=1.0, grid_import_limit_w=5000.0)
    slots = _slots(1)
    intervals = [ForecastInterval(BASE, pv_w=4000.0, load_w=0.0, dt_h=1.0)]
    selected = [BASE]
    out = plan.build_plan_horizon(slots, intervals, selected, 0.0, BASE + timedelta(hours=1), cfg)
    assert out[0]["solar_charge_w"] == 4000.0
    assert out[0]["grid_charge_w"] == 2000.0  # inverter remainder (6000-4000), well under the 5000 import limit


def test_grid_import_limit_w_default_is_inert():
    """Default (17250 W) must not change any existing plan at the stock 6 kW rate."""
    cfg = Config(capacity_kwh=10.0, soc_target=100.0, max_charge_w=6000.0, eta_charge=1.0)
    assert cfg.grid_import_limit_w == const.DEFAULT_GRID_IMPORT_LIMIT_W
    slots = _slots(1)
    intervals = [ForecastInterval(BASE, pv_w=0.0, load_w=0.0, dt_h=1.0)]
    selected = [BASE]
    out = plan.build_plan_horizon(slots, intervals, selected, 0.0, BASE + timedelta(hours=1), cfg)
    assert out[0]["grid_charge_w"] == 6000.0


def _ceiling_reached_case():
    """Current slot: selected, but live SoC already at the solar-reservation ceiling.

    Reproduces the live 2026-07-29 shape — the modelled grid bar clamps to 0
    because forecast solar alone covers the remaining headroom to the ceiling,
    so an in-progress 6 kW grid charge renders as ``grid_charge_w == 0``.
    """
    cfg = Config(capacity_kwh=10.0, soc_target=100.0, max_charge_w=6000.0, eta_charge=1.0)
    slots = _slots(1)
    intervals = [ForecastInterval(BASE, pv_w=1261.0, load_w=345.0, dt_h=1.0)]
    return cfg, slots, intervals, [BASE], {BASE: 49.0}


def test_current_slot_grid_charge_clamps_to_zero_without_delivered():
    """Regression guard: the un-augmented model is what hid the live charge."""
    cfg, slots, intervals, selected, ceiling = _ceiling_reached_case()
    out = plan.build_plan_horizon(
        slots,
        intervals,
        selected,
        49.0,
        BASE + timedelta(hours=1),
        cfg,
        ceiling_by_hour=ceiling,
    )
    assert out[0]["grid_charge_w"] == 0.0
    assert out[0]["grid_charge_kwh"] == 0.0


def test_delivered_grid_charge_stays_visible_on_current_slot():
    cfg, slots, intervals, selected, ceiling = _ceiling_reached_case()
    out = plan.build_plan_horizon(
        slots,
        intervals,
        selected,
        49.0,
        BASE + timedelta(hours=1),
        cfg,
        ceiling_by_hour=ceiling,
        delivered_by_hour={BASE: {"grid_charge_kwh": 2.5}},
    )
    assert out[0]["grid_charge_kwh"] == 2.5
    assert out[0]["grid_charge_w"] == 2500.0


def test_delivered_grid_charge_does_not_advance_soc_projection():
    """Live SoC already contains the delivered energy — adding it would double-count."""
    cfg, slots, intervals, selected, ceiling = _ceiling_reached_case()
    common = dict(ceiling_by_hour=ceiling)
    baseline = plan.build_plan_horizon(slots, intervals, selected, 49.0, BASE + timedelta(hours=1), cfg, **common)
    augmented = plan.build_plan_horizon(
        slots,
        intervals,
        selected,
        49.0,
        BASE + timedelta(hours=1),
        cfg,
        delivered_by_hour={BASE: {"grid_charge_kwh": 2.5}},
        **common,
    )
    assert augmented[0]["soc"] == baseline[0]["soc"]


def test_delivered_adds_to_a_still_running_modelled_grid_charge():
    """Mid-charge: delivered-so-far plus the kWh still left to reach the ceiling."""
    cfg = Config(capacity_kwh=10.0, soc_target=100.0, max_charge_w=6000.0, eta_charge=1.0)
    intervals = [ForecastInterval(BASE, pv_w=0.0, load_w=0.0, dt_h=1.0)]
    out = plan.build_plan_horizon(
        _slots(1),
        intervals,
        [BASE],
        30.0,
        BASE + timedelta(hours=1),
        cfg,
        ceiling_by_hour={BASE: 49.0},
        delivered_by_hour={BASE: {"grid_charge_kwh": 0.6}},
    )
    # headroom 30% -> 49% of 10 kWh = 1.9 kWh still to go, plus 0.6 already in.
    assert out[0]["grid_charge_kwh"] == pytest.approx(2.5)


def test_delivered_lands_only_on_the_in_progress_slot_at_15_min():
    """The add-back is ONE slot's own delivered kWh, on that slot only.

    Live lab 2026-08-03: ``delivered_by_hour`` was clock-hour keyed AND
    hour-floored on lookup, so every not-yet-elapsed quarter of the running
    hour received a copy of the WHOLE hour's delivered energy — the 13:45
    quarter rendered 42.2 kW / 10.56 kWh on a 12 kW inverter (finding A4).

    Ceiling 50% with the SoC at 40% leaves 2 kWh of headroom, which the first
    quarter consumes in full; the remaining three are modelled at 0. Only the
    in-progress quarter (:45) may carry the 0.5 kWh already delivered.
    """
    cfg = Config(capacity_kwh=20.0, soc_target=100.0, max_charge_w=12000.0, eta_charge=1.0)
    starts = [BASE + timedelta(minutes=15 * i) for i in range(4)]
    slots = [PriceSlot(s, 0.30) for s in starts]
    intervals = [ForecastInterval(s, pv_w=0.0, load_w=0.0, dt_h=0.25) for s in starts]
    out = plan.build_plan_horizon(
        slots,
        intervals,
        starts,
        40.0,
        BASE + timedelta(hours=1),
        cfg,
        ceiling_by_hour={s: 50.0 for s in starts},
        delivered_by_hour={starts[3]: {"grid_charge_kwh": 0.5}},
        slot_minutes=15,
    )
    assert [e["grid_charge_kwh"] for e in out] == [2.0, 0.0, 0.0, 0.5]
    assert [e["grid_charge_w"] for e in out] == [8000.0, 0.0, 0.0, 2000.0]


def _partial_slot_case():
    """One 15-min grid slot, 12 kW rate, 20 kWh pack, ceiling far above.

    Rate-bound by construction (headroom at 40% is 12 kWh, far more than a
    quarter can take), which is the case where modelling the WHOLE slot is
    wrong once part of it has already elapsed.
    """
    cfg = Config(capacity_kwh=20.0, soc_target=100.0, max_charge_w=12000.0, eta_charge=1.0)
    return (
        cfg,
        [PriceSlot(BASE, 0.30)],
        [ForecastInterval(BASE, pv_w=0.0, load_w=0.0, dt_h=0.25)],
        [BASE],
    )


def test_in_progress_slot_is_modelled_over_its_remaining_minutes_only():
    """``now`` 10 min into a quarter leaves 5 min of modelled charge, not 15.

    The live SoC the walk starts from ALREADY contains the elapsed minutes, so
    crediting the full slot double-counts them. Live lab 2026-08-03 12:41Z: a
    live SoC of ~56.5% was published as 71.5% for the in-progress quarter, and
    the whole forward line carried the offset until it saturated.
    """
    cfg, slots, intervals, selected = _partial_slot_case()
    out = plan.build_plan_horizon(
        slots,
        intervals,
        selected,
        40.0,
        BASE + timedelta(minutes=15),
        cfg,
        slot_minutes=15,
        now=BASE + timedelta(minutes=10),
    )
    # 12 kW x 5 min = 1.0 kWh (not 3.0), so +5 pp of a 20 kWh pack (not +15).
    assert out[0]["grid_charge_kwh"] == pytest.approx(1.0)
    assert out[0]["soc"] == pytest.approx(45.0)
    # kWh == W x dt_h / 1000 must still hold: the bar is the slot AVERAGE.
    assert out[0]["grid_charge_w"] == pytest.approx(4000.0)


def test_delivered_plus_remaining_reconstructs_the_in_progress_slot_total():
    """The measured elapsed part and the modelled remainder are now disjoint.

    2.0 kWh delivered in the elapsed 10 min + 1.0 kWh modelled for the last
    5 min = the 3.0 kWh a quarter can hold at 12 kW, with the clamp never
    having to bind. The SoC still advances by the REMAINDER alone.
    """
    cfg, slots, intervals, selected = _partial_slot_case()
    out = plan.build_plan_horizon(
        slots,
        intervals,
        selected,
        40.0,
        BASE + timedelta(minutes=15),
        cfg,
        slot_minutes=15,
        delivered_by_hour={BASE: {"grid_charge_kwh": 2.0}},
        now=BASE + timedelta(minutes=10),
    )
    assert out[0]["grid_charge_kwh"] == pytest.approx(3.0)
    assert out[0]["grid_charge_w"] == pytest.approx(12000.0)
    assert out[0]["soc"] == pytest.approx(45.0)


def test_now_at_a_slot_boundary_is_byte_identical_to_no_now():
    """Parity seam: a slot that has not started yet is modelled in full.

    Guards every non-current row in the horizon, and the whole hourly
    deployment where ``now`` lands on the slot start after each tick's floor.
    """
    cfg, slots, intervals, selected = _partial_slot_case()
    common = dict(slot_minutes=15, delivered_by_hour={BASE: {"grid_charge_kwh": 0.4}})
    baseline = plan.build_plan_horizon(slots, intervals, selected, 40.0, BASE + timedelta(minutes=15), cfg, **common)
    with_now = plan.build_plan_horizon(
        slots, intervals, selected, 40.0, BASE + timedelta(minutes=15), cfg, now=BASE, **common
    )
    assert with_now == baseline


def test_delivered_cannot_push_a_slot_past_its_physical_import_cap():
    """A slot can never import more than the connection allows for its duration.

    The modelled remainder is a FULL-slot rate. When the charge is RATE-bound
    (deep in a cheap window, ceiling far above) rather than headroom-bound, it
    and the already-delivered kWh both cover the minutes already elapsed, so
    they no longer sum to the slot total. Live lab 2026-08-03: 12 kW modelled
    + 2.3 kWh delivered = 5.3 kWh into a 15-min quarter — still impossible
    even once the add-back stopped being hour-keyed.
    """
    cfg = Config(capacity_kwh=20.0, soc_target=100.0, max_charge_w=12000.0, eta_charge=1.0)
    out = plan.build_plan_horizon(
        [PriceSlot(BASE, 0.30)],
        [ForecastInterval(BASE, pv_w=0.0, load_w=0.0, dt_h=0.25)],
        [BASE],
        40.0,
        BASE + timedelta(minutes=15),
        cfg,
        delivered_by_hour={BASE: {"grid_charge_kwh": 2.3}},
        slot_minutes=15,
    )
    assert out[0]["grid_charge_w"] == 12000.0
    assert out[0]["grid_charge_kwh"] == 3.0


def test_delivered_ignored_on_past_slots():
    """Past slots already carry measured actuals; delivered must not double up."""
    cfg = Config(capacity_kwh=10.0, soc_target=100.0, max_charge_w=6000.0, eta_charge=1.0)
    act = {
        "pv_w": 0.0,
        "load_w": 0.0,
        "soc": 40.0,
        "solar_charge_w": 0.0,
        "grid_charge_w": 4000.0,
        "grid_export_w": 0.0,
        "pv_kwh": 0.0,
        "load_kwh": 0.0,
        "solar_charge_kwh": 0.0,
        "grid_charge_kwh": 4.0,
        "grid_export_kwh": 0.0,
    }
    out = plan.build_plan_horizon(
        _slots(1),
        [],
        [BASE],
        49.0,
        BASE + timedelta(hours=1),
        cfg,
        past_actuals_by_slot={BASE: act},
        delivered_by_hour={BASE: {"grid_charge_kwh": 2.5}},
    )
    assert out[0]["mode"] == "actual"
    assert out[0]["grid_charge_kwh"] == 4.0


def test_soc_projection_rises_and_caps():
    cfg = Config(capacity_kwh=10.0, soc_target=90.0, max_charge_w=5000.0, eta_charge=1.0)
    slots = _slots(4)
    # all grid-charge hours: 5000 W * 1 h = 5 kWh = 50% of a 10 kWh battery per hour
    selected = [s.start for s in slots]
    out = plan.build_plan_horizon(slots, [], selected, 0.0, BASE + timedelta(hours=4), cfg)
    socs = [e["soc"] for e in out]
    assert socs[0] == 50.0  # 0 -> 50
    assert socs[1] == 90.0  # capped at target (would be 100)
    assert socs[-1] == 90.0  # stays capped
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
    now = datetime(2026, 6, 20, 11, 0, tzinfo=UTC)
    slots = [PriceSlot(datetime(2026, 6, 20, h, 0, tzinfo=UTC), 0.30) for h in range(9, 16)]
    pv_curve = [(datetime(2026, 6, 20, 12, 0, tzinfo=UTC), 2000.0)]
    out = plan.build_display_intervals(slots, now, pv_curve, _StubPredictor(), None, 400.0)
    starts = [iv.start for iv in out]
    # past hours (09,10) dropped; starts at 11:00
    assert starts[0] == datetime(2026, 6, 20, 11, 0, tzinfo=UTC)
    assert all(s >= now for s in starts)
    by_hour = {iv.start.hour: iv for iv in out}
    assert by_hour[12].pv_w == 2000.0  # daylight curve value
    assert by_hour[11].pv_w == 0.0  # no curve point -> 0
    assert all(iv.load_w == 500.0 for iv in out)  # predicted every hour
    assert all(iv.dt_h == 1.0 for iv in out)


def test_display_intervals_empty_slots():
    now = datetime(2026, 6, 20, 11, 0, tzinfo=UTC)
    assert plan.build_display_intervals([], now, [], _StubPredictor(), None, 400.0) == []


def test_display_intervals_fan_hourly_pv_across_15min_quarters():
    """Live France defect (slot_minutes=15): the PV curve is intrinsically HOURLY
    (one point/hour from every parsers.py builder), but the old PV bucketing keyed
    pv_by_slot on floor_to_slot(start, slot_minutes) — so only the :00 quarter
    matched a curve point and :15/:30/:45 fell back to 0.0. Live evidence: pv_w =
    2391.6 at 15:00 then 0.0/0.0/0.0, 1397.9 at 16:00 then 0.0/0.0/0.0. Fixed: PV
    is hour-bucketed/looked-up (mirrors the D1 temp_by_hour pattern) so every
    quarter of an hour inherits that hour's watts."""
    now = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)
    slots = [PriceSlot(now + timedelta(minutes=15 * i), 0.20) for i in range(8)]
    pv_curve = [(now, 2391.6), (now + timedelta(hours=1), 1397.9)]
    ivs = plan.build_display_intervals(slots, now, pv_curve, _StubPredictor(), None, 400.0, slot_minutes=15)
    assert len(ivs) == 8
    pv_ws = [iv.pv_w for iv in ivs]
    # NOT the pre-fix [2391.6, 0.0, 0.0, 0.0, 1397.9, 0.0, 0.0, 0.0] shape.
    assert pv_ws == [2391.6, 2391.6, 2391.6, 2391.6, 1397.9, 1397.9, 1397.9, 1397.9]


def test_display_intervals_pv_slot_minutes_60_byte_identical():
    """floor_to_slot(x, 60) == hour_floor(x), so an explicit slot_minutes=60 (and
    the implicit 60-min default) must be byte-identical to the pre-fix PV
    bucketing — zero behavior change at the legacy hourly resolution."""
    now = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)
    slots = [PriceSlot(now + timedelta(hours=i), 0.20) for i in range(3)]
    pv_curve = [(now, 2391.6), (now + timedelta(hours=1), 1397.9)]
    implicit = plan.build_display_intervals(slots, now, pv_curve, _StubPredictor(), None, 400.0)
    explicit = plan.build_display_intervals(slots, now, pv_curve, _StubPredictor(), None, 400.0, slot_minutes=60)
    assert implicit == explicit
    assert [iv.pv_w for iv in implicit] == [2391.6, 1397.9, 0.0]


def test_display_intervals_pv_energy_conserved_at_15min():
    """Window-energy sanity: sum(pv_w * dt_h) over a day must be equal whether the
    slot grid is hourly or 15-min — the hourly PV curve's energy must be fanned
    across quarters, not quartered (pre-fix: ~1/4 of solar energy reached the
    DP/reserve integration at decision.py:288)."""
    now = datetime(2026, 7, 31, 0, 0, tzinfo=UTC)
    hours = 4
    pv_curve = [(now + timedelta(hours=i), 1000.0 * (i + 1)) for i in range(hours)]
    slots_hourly = [PriceSlot(now + timedelta(hours=i), 0.20) for i in range(hours)]
    slots_15 = [PriceSlot(now + timedelta(minutes=15 * i), 0.20) for i in range(hours * 4)]
    ivs_hourly = plan.build_display_intervals(
        slots_hourly, now, pv_curve, _StubPredictor(), None, 400.0, slot_minutes=60
    )
    ivs_15 = plan.build_display_intervals(slots_15, now, pv_curve, _StubPredictor(), None, 400.0, slot_minutes=15)
    energy_hourly = sum(iv.pv_w * iv.dt_h for iv in ivs_hourly)
    energy_15 = sum(iv.pv_w * iv.dt_h for iv in ivs_15)
    assert energy_15 == pytest.approx(energy_hourly)


def test_display_intervals_dense_30min_curve_no_doubling():
    """Dense sub-hourly curve (post-Task-1 from_watts shape): each quarter reads ITS OWN
    value — the old hour-SUM would have read 386+2145=2531 in every quarter (2x energy)."""
    now = datetime(2026, 8, 2, 11, 0, tzinfo=UTC)
    slots = [PriceSlot(now + timedelta(minutes=15 * i), 0.20) for i in range(4)]
    pv_curve = [
        (now, 386.0),
        (now + timedelta(minutes=15), 386.0),
        (now + timedelta(minutes=30), 2145.0),
        (now + timedelta(minutes=45), 2145.0),
    ]
    ivs = plan.build_display_intervals(slots, now, pv_curve, _StubPredictor(), None, 400.0, slot_minutes=15)
    assert [iv.pv_w for iv in ivs] == [386.0, 386.0, 2145.0, 2145.0]
    assert sum(iv.pv_w * iv.dt_h for iv in ivs) / 1000 == pytest.approx(1.2655)


def test_display_intervals_stale_point_zeroes_after_1h():
    now = datetime(2026, 8, 2, 11, 0, tzinfo=UTC)
    slots = [PriceSlot(now + timedelta(minutes=15 * i), 0.20) for i in range(10)]
    pv_curve = [(now, 1000.0)]  # single point, no successor
    ivs = plan.build_display_intervals(slots, now, pv_curve, _StubPredictor(), None, 400.0, slot_minutes=15)
    assert [iv.pv_w for iv in ivs][:4] == [1000.0] * 4
    assert all(iv.pv_w == 0.0 for iv in ivs[4:])


def test_soc_discharges_on_deficit():
    # idle hour with load > pv must LOWER soc by discharge energy / eta_discharge
    cfg = Config(
        capacity_kwh=10.0, soc_floor=0.0, soc_target=100.0, max_charge_w=6000.0, eta_charge=1.0, round_trip_eff=0.5
    )
    slots = _slots(1)
    intervals = [ForecastInterval(BASE, pv_w=0.0, load_w=1000.0, dt_h=1.0)]
    out = plan.build_plan_horizon(slots, intervals, [], 50.0, BASE + timedelta(hours=1), cfg)
    # eta_discharge = round_trip_eff/eta_charge = 0.5; dc drawn = 1000/0.5 = 2000 Wh
    # dSoC = -2000/10000*100 = -20  -> 30.0
    assert out[0]["mode"] == "idle"
    assert out[0]["soc"] == 30.0
    assert out[0]["charge_w"] == 0.0


def test_idle_drain_sags_projected_soc():
    """idle_drain_w (constant inverter-standby DC drain) applies only on passive-
    discharge (deficit) slots — sags soc_sim below the idle_drain_w=0 baseline by
    the idle term (130 W * 1 h / 1000 = 0.13 kWh -> 1.3% of a 10 kWh battery).
    Charge and export-only slots are unaffected (idle drain is not paid there)."""
    base_kwargs = dict(
        capacity_kwh=10.0, soc_floor=0.0, soc_target=100.0, max_charge_w=6000.0, eta_charge=1.0, round_trip_eff=1.0
    )
    cfg0 = Config(**base_kwargs, idle_drain_w=0.0)
    cfg_idle = Config(**base_kwargs, idle_drain_w=130.0)
    deadline = BASE + timedelta(hours=1)

    # Deficit slot: load > pv, no grid, no export -> passive-discharge branch.
    slots = _slots(1)
    deficit_intervals = [ForecastInterval(BASE, pv_w=0.0, load_w=1000.0, dt_h=1.0)]
    out0 = plan.build_plan_horizon(slots, deficit_intervals, [], 50.0, deadline, cfg0)
    out_idle = plan.build_plan_horizon(slots, deficit_intervals, [], 50.0, deadline, cfg_idle)
    assert out0[0]["mode"] == "idle"
    assert out0[0]["soc"] == 40.0
    assert out_idle[0]["soc"] == 38.7

    # Grid-charge slot: idle drain must NOT apply.
    selected = [BASE]
    out0_c = plan.build_plan_horizon(slots, deficit_intervals, selected, 50.0, deadline, cfg0)
    out_idle_c = plan.build_plan_horizon(slots, deficit_intervals, selected, 50.0, deadline, cfg_idle)
    assert out0_c[0]["mode"] == "grid"
    assert out0_c[0]["soc"] == out_idle_c[0]["soc"]

    # Export-only slot (pv == load: no deficit branch taken, export applied separately):
    # idle drain must NOT apply.
    export_intervals = [ForecastInterval(BASE, pv_w=500.0, load_w=500.0, dt_h=1.0)]
    out0_e = plan.build_plan_horizon(
        slots,
        export_intervals,
        [],
        50.0,
        deadline,
        cfg0,
        export_request_by_hour={BASE: 1000.0},
    )
    out_idle_e = plan.build_plan_horizon(
        slots,
        export_intervals,
        [],
        50.0,
        deadline,
        cfg_idle,
        export_request_by_hour={BASE: 1000.0},
    )
    assert out0_e[0]["mode"] == "export"
    assert out0_e[0]["soc"] == out_idle_e[0]["soc"]


def test_plan_idle_zero_parity():
    """idle_drain_w=0.0 reproduces the pre-idle-drain deficit-slot SoC math exactly
    (regression guard: same scenario/expected values as test_soc_discharges_on_deficit,
    with idle_drain_w explicit)."""
    cfg = Config(
        capacity_kwh=10.0,
        soc_floor=0.0,
        soc_target=100.0,
        max_charge_w=6000.0,
        eta_charge=1.0,
        round_trip_eff=0.5,
        idle_drain_w=0.0,
    )
    slots = _slots(1)
    intervals = [ForecastInterval(BASE, pv_w=0.0, load_w=1000.0, dt_h=1.0)]
    out = plan.build_plan_horizon(slots, intervals, [], 50.0, BASE + timedelta(hours=1), cfg)
    assert out[0]["mode"] == "idle"
    assert out[0]["soc"] == 30.0
    assert out[0]["charge_w"] == 0.0


def test_soc_discharge_clamped_at_firmware_floor():
    """Deficit night (load > pv, no charge, no export): the projected-SoC sim sags to
    the firmware hard floor (5%), NOT the soft cfg.soc_floor planning margin — nothing
    force-charges to hold soc_floor, so the real battery keeps draining past it."""
    cfg = Config(
        capacity_kwh=10.0, soc_floor=10.0, soc_target=100.0, max_charge_w=6000.0, eta_charge=1.0, round_trip_eff=1.0
    )
    slots = _slots(3)
    intervals = [ForecastInterval(BASE + timedelta(hours=i), pv_w=0.0, load_w=6000.0, dt_h=1.0) for i in range(3)]
    out = plan.build_plan_horizon(slots, intervals, [], 20.0, BASE + timedelta(hours=3), cfg)
    socs = [e["soc"] for e in out]
    # hour0: 20 - 60 -> sags well below soc_floor=10, clamps at the firmware floor
    # (5.0), never at the soft soc_floor=10 config value.
    assert socs == [5.0, 5.0, 5.0]


def test_soc_discharge_clamp_identical_at_live_default_soc_floor():
    """soc_floor=5 (live default) == FIRMWARE_SOC_FLOOR, so the clamp result is
    byte-identical to the pre-change behaviour (regression guard for the firmware-
    floor clamp: same deficit scenario as test_soc_discharge_clamped_at_firmware_floor,
    just with soc_floor lowered to the firmware value)."""
    cfg = Config(
        capacity_kwh=10.0, soc_floor=5.0, soc_target=100.0, max_charge_w=6000.0, eta_charge=1.0, round_trip_eff=1.0
    )
    slots = _slots(3)
    intervals = [ForecastInterval(BASE + timedelta(hours=i), pv_w=0.0, load_w=6000.0, dt_h=1.0) for i in range(3)]
    out = plan.build_plan_horizon(slots, intervals, [], 20.0, BASE + timedelta(hours=3), cfg)
    socs = [e["soc"] for e in out]
    assert socs == [5.0, 5.0, 5.0]


def test_soc_discharge_never_below_firmware_floor_when_soc_floor_set_lower():
    """soc_floor=3 (below the firmware floor) must never let the sim display below
    5.0 — the firmware refuses to discharge past its hard floor regardless of the
    (soft) config value."""
    cfg = Config(
        capacity_kwh=10.0, soc_floor=3.0, soc_target=100.0, max_charge_w=6000.0, eta_charge=1.0, round_trip_eff=1.0
    )
    slots = _slots(3)
    intervals = [ForecastInterval(BASE + timedelta(hours=i), pv_w=0.0, load_w=6000.0, dt_h=1.0) for i in range(3)]
    out = plan.build_plan_horizon(slots, intervals, [], 20.0, BASE + timedelta(hours=3), cfg)
    socs = [e["soc"] for e in out]
    assert socs == [5.0, 5.0, 5.0]
    assert all(s >= 5.0 for s in socs)


def test_soc_discharge_capped_load_by_max_charge_w():
    # discharge AC is capped at max_charge_w even if load is huge
    cfg = Config(
        capacity_kwh=10.0, soc_floor=0.0, soc_target=100.0, max_charge_w=2000.0, eta_charge=1.0, round_trip_eff=1.0
    )
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
    cfg = Config(
        capacity_kwh=10.0, soc_floor=0.0, soc_target=100.0, max_charge_w=6000.0, eta_charge=0.8, round_trip_eff=1.0
    )
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


def test_build_display_horizon_threads_now_into_the_in_progress_slot():
    """Wiring guard: dropping ``now=now`` at the call site must fail a test.

    Night-time so PV is 0 and the grid bar is rate-bound: 10 min into the 22:00
    quarter only 5 min are left to model, so 1.0 kWh (not 3.0) and +5 pp of a
    20 kWh pack (not +15). Nothing else in the suite covers the threading.
    """
    cfg = Config(capacity_kwh=20.0, soc_target=100.0, max_charge_w=12000.0, eta_charge=1.0)
    slot0 = datetime(2026, 6, 20, 22, 0, tzinfo=UTC)
    now = slot0 + timedelta(minutes=10)
    slots = [PriceSlot(slot0 + timedelta(minutes=15 * i), 0.30) for i in range(4)]
    sun_times = (
        datetime(2026, 6, 20, 20, 0, tzinfo=UTC),  # today_sunset (already past)
        datetime(2026, 6, 21, 6, 0, tzinfo=UTC),
        datetime(2026, 6, 21, 20, 0, tzinfo=UTC),
    )
    out = plan.build_display_horizon(
        slots,
        now,
        today_arrays=[(1.0, None)],
        tomorrow_arrays=[(6.0, None)],
        sun_times=sun_times,
        predictor=_StubPredictor(),
        cur_temp=15.0,
        fallback_w=400.0,
        soc=40.0,
        selected=[slot0],
        horizon_edge=slot0 + timedelta(hours=1),
        cfg=cfg,
        slot_minutes=15,
    )
    cur = next(r for r in out if r["start"] == slot0.isoformat())
    assert cur["mode"] == "grid"
    assert cur["grid_charge_kwh"] == pytest.approx(1.0)
    assert cur["soc"] == pytest.approx(45.0)


def test_build_display_horizon_none_sun_times_returns_empty():
    now = datetime(2026, 6, 20, 11, 0, tzinfo=UTC)
    slots = _slots(3)
    out = plan.build_display_horizon(
        slots,
        now,
        today_arrays=[(1.0, None)],
        tomorrow_arrays=[(6.0, None)],
        sun_times=None,
        predictor=_StubPredictor(),
        cur_temp=None,
        fallback_w=400.0,
        soc=50.0,
        selected=[],
        horizon_edge=now,
        cfg=Config(),
    )
    assert out == []


def test_build_display_horizon_self_consumption_no_grid():
    now = datetime(2026, 6, 20, 17, 0, tzinfo=UTC)
    slots = [PriceSlot(now + timedelta(hours=i), 0.30) for i in range(30)]
    sun_times = (
        datetime(2026, 6, 20, 20, 0, tzinfo=UTC),  # today_sunset
        datetime(2026, 6, 21, 6, 0, tzinfo=UTC),  # tomorrow_sunrise
        datetime(2026, 6, 21, 20, 0, tzinfo=UTC),  # tomorrow_sunset
    )
    out = plan.build_display_horizon(
        slots,
        now,
        today_arrays=[(1.0, None)],
        tomorrow_arrays=[(6.0, None)],
        sun_times=sun_times,
        predictor=_StubPredictor(),
        cur_temp=15.0,
        fallback_w=400.0,
        soc=50.0,
        selected=[],
        horizon_edge=now,
        cfg=Config(),
    )
    assert out, "expected a non-empty horizon"
    assert all(e["mode"] != "grid" for e in out)  # selected=[] -> never grid
    assert any(e["pv_w"] and e["pv_w"] > 0 for e in out)  # tomorrow daytime PV present
    assert all(e["load_w"] == 500.0 for e in out)  # _StubPredictor returns 500


def test_build_display_horizon_energy_conserved():
    """Tomorrow-only PV: total pv_w in horizon ≈ sum of kWh * 1000 Wh."""
    now = datetime(2026, 6, 20, 17, 0, tzinfo=UTC)
    slots = [PriceSlot(now + timedelta(hours=i), 0.30) for i in range(30)]
    sun_times = (
        datetime(2026, 6, 20, 20, 0, tzinfo=UTC),  # today_sunset
        datetime(2026, 6, 21, 6, 0, tzinfo=UTC),  # tomorrow_sunrise
        datetime(2026, 6, 21, 20, 0, tzinfo=UTC),  # tomorrow_sunset
    )
    tomorrow_kwh = 6.0
    out = plan.build_display_horizon(
        slots,
        now,
        today_arrays=None,
        tomorrow_arrays=[(tomorrow_kwh, None)],
        sun_times=sun_times,
        predictor=_StubPredictor(),
        cur_temp=None,
        fallback_w=400.0,
        soc=50.0,
        selected=[],
        horizon_edge=now,
        cfg=Config(),
    )
    assert out, "expected a non-empty horizon"
    total_pv_wh = sum(e["pv_w"] for e in out if e["pv_w"])
    # Each horizon entry is 1 hour; pv_w in watts → energy in Wh per slot.
    # All tomorrow daytime slots are in the future so no hours are clipped.
    assert abs(total_pv_wh - tomorrow_kwh * 1000) < 100, f"Expected ~{tomorrow_kwh * 1000} Wh, got {total_pv_wh:.1f} Wh"


def test_build_display_horizon_shoulder_lift():
    """E/W split arrays: early-peak and late-peak hours are HIGHER than single centred array,
    while midday (13:00) is LOWER — proves timing fidelity, not a higher global peak."""
    now = datetime(2026, 6, 20, 17, 0, tzinfo=UTC)
    slots = [PriceSlot(now + timedelta(hours=i), 0.30) for i in range(30)]
    sun_times = (
        datetime(2026, 6, 20, 20, 0, tzinfo=UTC),
        datetime(2026, 6, 21, 6, 0, tzinfo=UTC),
        datetime(2026, 6, 21, 20, 0, tzinfo=UTC),
    )
    early_peak = datetime(2026, 6, 21, 9, 0, tzinfo=UTC)  # E-array peak
    late_peak = datetime(2026, 6, 21, 17, 0, tzinfo=UTC)  # W-array peak
    mid_hour = datetime(2026, 6, 21, 13, 0, tzinfo=UTC)  # midday valley

    ew_arrays = [(3.0, early_peak), (3.0, late_peak)]
    centered_arrays = [(6.0, None)]  # peaks at window midpoint ≈ 13:00

    def _build(tomorrow_arrays):
        return plan.build_display_horizon(
            slots,
            now,
            today_arrays=None,
            tomorrow_arrays=tomorrow_arrays,
            sun_times=sun_times,
            predictor=_StubPredictor(),
            cur_temp=None,
            fallback_w=400.0,
            soc=50.0,
            selected=[],
            horizon_edge=now,
            cfg=Config(),
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
    cfg = Config(capacity_kwh=10.0, soc_floor=0.0, soc_target=100.0, max_charge_w=6000.0, eta_charge=1.0)
    slots = _slots(1)
    intervals = [ForecastInterval(BASE, pv_w=2000.0, load_w=800.0, dt_h=1.0)]  # surplus 1200
    selected = [BASE]
    out = plan.build_plan_horizon(
        slots,
        intervals,
        selected,
        60.0,
        BASE + timedelta(hours=1),
        cfg,
        grid_request_by_hour={BASE: 6000.0},  # ask for full rate
    )
    e = out[0]
    assert e["mode"] == "grid"
    assert e["solar_charge_w"] == 1200.0  # solar first
    assert e["grid_charge_w"] == 2800.0  # headroom(4000) - solar(1200) = 2800
    assert e["charge_w"] == 4000.0  # total lands exactly at soc_target


def test_grid_request_below_remaining_rate():
    cfg = Config(capacity_kwh=10.0, soc_floor=0.0, soc_target=100.0, max_charge_w=6000.0, eta_charge=1.0)
    slots = _slots(1)
    intervals = [ForecastInterval(BASE, pv_w=1000.0, load_w=470.0, dt_h=1.0)]  # surplus 530
    out = plan.build_plan_horizon(
        slots,
        intervals,
        [BASE],
        50.0,
        BASE + timedelta(hours=1),
        cfg,
        grid_request_by_hour={BASE: 800.0},
    )
    assert out[0]["solar_charge_w"] == 530.0
    assert out[0]["grid_charge_w"] == 800.0
    assert out[0]["charge_w"] == 1330.0


def test_grid_bar_collapses_when_battery_full():
    # Near-full battery: headroom ~0 -> grid bar ~0 (no phantom max_charge_w),
    # but mode stays "grid" (hour is selected) so planned_grid_hours is intact.
    cfg = Config(capacity_kwh=10.0, soc_floor=0.0, soc_target=97.0, max_charge_w=6000.0, eta_charge=1.0)
    slots = _slots(1)
    intervals = [ForecastInterval(BASE, pv_w=0.0, load_w=0.0, dt_h=1.0)]
    out = plan.build_plan_horizon(
        slots,
        intervals,
        [BASE],
        97.0,
        BASE + timedelta(hours=1),
        cfg,
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
    out = plan.build_plan_horizon(slots, intervals, [BASE], 50.0, BASE + timedelta(hours=1), cfg)
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
        capacity_kwh=10.0,
        soc_floor=5.0,
        soc_target=100.0,
        max_charge_w=3000.0,
        eta_charge=1.0,
        round_trip_eff=1.0,
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
        slots,
        intervals,
        [],
        80.0,
        BASE + timedelta(hours=2),
        cfg,
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
        capacity_kwh=10.0,
        soc_floor=5.0,
        soc_target=100.0,
        max_charge_w=5000.0,
        eta_charge=1.0,
        round_trip_eff=1.0,
    )
    slots = _slots(1)
    intervals = [ForecastInterval(BASE, pv_w=0.0, load_w=0.0, dt_h=1.0)]
    export_req = {BASE: 3000.0}  # 3000 W * 1 h = 3 kWh = 30% of 10 kWh
    out = plan.build_plan_horizon(
        slots,
        intervals,
        [],
        70.0,
        BASE + timedelta(hours=1),
        cfg,
        export_request_by_hour=export_req,
    )
    assert out[0]["grid_export_w"] == 3000.0
    # SoC: 70 - 30 = 40, capped to [5, 100] -> 40
    assert out[0]["soc"] == 40.0


def test_export_row_mode_is_export():
    """Export hour not selected for grid charge gets mode == 'export' (not 'idle')."""
    cfg = Config(
        capacity_kwh=10.0,
        soc_floor=5.0,
        soc_target=100.0,
        max_charge_w=5000.0,
        eta_charge=1.0,
        round_trip_eff=1.0,
    )
    slots = _slots(1)
    intervals = [ForecastInterval(BASE, pv_w=0.0, load_w=0.0, dt_h=1.0)]
    export_req = {BASE: 3000.0}  # export hour, not selected for grid charge
    out = plan.build_plan_horizon(
        slots,
        intervals,
        [],
        70.0,
        BASE + timedelta(hours=1),
        cfg,
        export_request_by_hour=export_req,
    )
    assert out[0]["grid_export_w"] == 3000.0
    assert out[0]["mode"] == "export"


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
        capacity_kwh=10.0,
        soc_floor=5.0,
        soc_target=100.0,
        max_charge_w=3000.0,
        eta_charge=1.0,
        round_trip_eff=1.0,
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
        capacity_kwh=cap_kwh,
        soc_floor=5.0,
        soc_target=100.0,
        max_charge_w=3000.0,
    )
    slots = _slots(2)
    # 2 kWh reserve = 20% of 10 kWh
    reserve_by_hour = {BASE: 2.0, BASE + timedelta(hours=1): 3.0}
    out = plan.build_plan_horizon(
        slots,
        [],
        [],
        50.0,
        BASE + timedelta(hours=2),
        cfg,
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
        capacity_kwh=10.0,
        soc_floor=5.0,
        soc_target=100.0,
        max_charge_w=3000.0,
        eta_charge=1.0,
        round_trip_eff=1.0,
    )
    slots = _slots(1)
    # PV covers partial load, battery covers the rest (no export)
    intervals = [ForecastInterval(BASE, pv_w=500.0, load_w=1500.0, dt_h=1.0)]
    export_req = {BASE: 2000.0}
    out = plan.build_plan_horizon(
        slots,
        intervals,
        [],
        80.0,
        BASE + timedelta(hours=1),
        cfg,
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
        now + timedelta(hours=9),  # today_sunset
        now + timedelta(hours=19),  # tomorrow_sunrise
        now + timedelta(hours=33),  # tomorrow_sunset
    )


def test_build_display_horizon_export_request_sets_grid_export_w():
    """export_request_by_hour is threaded through: export hour has grid_export_w > 0,
    non-export hour has grid_export_w == 0."""
    now = datetime(2026, 6, 20, 17, 0, tzinfo=UTC)
    slots = [PriceSlot(now + timedelta(hours=i), 0.30) for i in range(30)]
    sun_times = _sun_times_for(now)
    cfg = Config(
        capacity_kwh=10.0,
        soc_floor=5.0,
        soc_target=100.0,
        max_charge_w=3000.0,
        eta_charge=1.0,
        round_trip_eff=1.0,
        max_export_w=3000.0,
        grid_export_limit_w=6000.0,
    )
    export_hour = now.replace(minute=0, second=0, microsecond=0)
    export_req = {export_hour: 2000.0}
    out = plan.build_display_horizon(
        slots,
        now,
        today_arrays=[(1.0, None)],
        tomorrow_arrays=[(6.0, None)],
        sun_times=sun_times,
        predictor=_StubPredictor(),
        cur_temp=None,
        fallback_w=400.0,
        soc=80.0,
        selected=[],
        horizon_edge=now,
        cfg=cfg,
        export_request_by_hour=export_req,
    )
    assert out, "expected non-empty horizon"
    first = out[0]
    assert first["grid_export_w"] == 2000.0, f"Export hour must have grid_export_w=2000.0, got {first['grid_export_w']}"
    # Second hour onwards: no export scheduled → 0
    for entry in out[1:]:
        assert entry["grid_export_w"] == 0.0, (
            f"Non-export hour must have grid_export_w=0.0, got {entry['grid_export_w']} at {entry['start']}"
        )


def test_build_display_horizon_export_drains_soc():
    """Export drains the projected SoC — the SoC after the export hour is lower
    than it would be without export."""
    now = datetime(2026, 6, 20, 17, 0, tzinfo=UTC)
    slots = [PriceSlot(now + timedelta(hours=i), 0.30) for i in range(30)]
    sun_times = _sun_times_for(now)
    cfg = Config(
        capacity_kwh=10.0,
        soc_floor=5.0,
        soc_target=100.0,
        max_charge_w=3000.0,
        eta_charge=1.0,
        round_trip_eff=1.0,
        max_export_w=3000.0,
        grid_export_limit_w=6000.0,
    )
    export_hour = now.replace(minute=0, second=0, microsecond=0)
    export_req = {export_hour: 2000.0}  # 2000 W for 1 hour = 2 kWh = 20% of 10 kWh

    without_export = plan.build_display_horizon(
        slots,
        now,
        today_arrays=[(1.0, None)],
        tomorrow_arrays=[(6.0, None)],
        sun_times=sun_times,
        predictor=_StubPredictor(),
        cur_temp=None,
        fallback_w=400.0,
        soc=80.0,
        selected=[],
        horizon_edge=now,
        cfg=cfg,
    )
    with_export = plan.build_display_horizon(
        slots,
        now,
        today_arrays=[(1.0, None)],
        tomorrow_arrays=[(6.0, None)],
        sun_times=sun_times,
        predictor=_StubPredictor(),
        cur_temp=None,
        fallback_w=400.0,
        soc=80.0,
        selected=[],
        horizon_edge=now,
        cfg=cfg,
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
    now = datetime(2026, 6, 20, 17, 0, tzinfo=UTC)
    slots = [PriceSlot(now + timedelta(hours=i), 0.30) for i in range(30)]
    sun_times = _sun_times_for(now)
    cap_kwh = 10.0
    cfg = Config(
        capacity_kwh=cap_kwh,
        soc_floor=5.0,
        soc_target=100.0,
        max_charge_w=3000.0,
    )
    h0 = now.replace(minute=0, second=0, microsecond=0)
    h1 = h0 + timedelta(hours=1)
    # 3 kWh reserve = 30%, 4 kWh reserve = 40%
    reserve_by_hour = {h0: 3.0, h1: 4.0}
    out = plan.build_display_horizon(
        slots,
        now,
        today_arrays=[(1.0, None)],
        tomorrow_arrays=[(6.0, None)],
        sun_times=sun_times,
        predictor=_StubPredictor(),
        cur_temp=None,
        fallback_w=400.0,
        soc=50.0,
        selected=[],
        horizon_edge=now,
        cfg=cfg,
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

    now = datetime(2026, 6, 29, 0, tzinfo=UTC)
    slots = [PriceSlot(start=now + timedelta(hours=h), price=0.2) for h in range(3)]
    pv_curve = []
    seen = {}

    class _RecordingPredictor:
        def predict(self, when, temp, fallback_w, *, quantile=0.5):
            seen[when] = temp
            return 500.0

    temp_by_hour = {now: 5.0, now + timedelta(hours=1): 12.0}  # hour 2 absent → falls back
    build_display_intervals(
        slots,
        now,
        pv_curve,
        _RecordingPredictor(),
        -99.0,
        400.0,
        temp_by_hour=temp_by_hour,
    )
    assert seen[now] == 5.0
    assert seen[now + timedelta(hours=1)] == 12.0
    assert seen[now + timedelta(hours=2)] == -99.0  # absent hour → scalar cur_temp


def test_eta_charge_guard_unified_at_subnano_boundary():
    """Sub-1e-9 eta_charge must hit the same fallback as eta_charge=0:
    finite projected SoC, no blow-up, identical trajectory (eta_discharge=1.0)."""
    common = dict(capacity_kwh=10.0, soc_target=90.0, soc_floor=5.0, max_charge_w=3000.0, round_trip_eff=0.85)
    slots = _slots(2)
    # An idle hour where load>pv self-discharges the SoC sim by load/eta_discharge.
    intervals = [ForecastInterval(BASE, pv_w=0.0, load_w=1000.0, dt_h=1.0)]
    out_tiny = plan.build_plan_horizon(
        slots, intervals, [], 50.0, BASE + timedelta(hours=2), Config(eta_charge=5e-10, **common)
    )
    out_zero = plan.build_plan_horizon(
        slots, intervals, [], 50.0, BASE + timedelta(hours=2), Config(eta_charge=0.0, **common)
    )
    socs_tiny = [e["soc"] for e in out_tiny]
    assert all(s == s and abs(s) < 1e6 for s in socs_tiny)  # finite, no inf/nan
    assert socs_tiny == [e["soc"] for e in out_zero]  # same fallback path


def test_build_plan_horizon_no_zerodiv_at_zero_round_trip_eff():
    """F3: round_trip_eff=0.0 (eta_charge>0) drives eta_discharge to 0.0, which is
    NOT caught by the eta_charge>1e-9 guard at line 128. The self_discharge_w/
    grid_export_w division sites must guard their own denominator so an export
    slot at zero round-trip efficiency doesn't ZeroDivisionError the display sim."""
    cfg = Config.from_dict(
        {
            "capacity_kwh": 10.0,
            "soc_floor": 10.0,
            "soc_target": 90.0,
            "max_charge_w": 3000.0,
            "eta_charge": 1.0,
            "round_trip_eff": 0.0,
        }
    )
    t = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)
    out = plan.build_plan_horizon([PriceSlot(t, 0.20)], [], [], 80.0, t, cfg, export_request_by_hour={t: 500.0})
    assert out  # completes without ZeroDivisionError


def test_build_plan_horizon_accepts_eta_curve_none_identical():
    """Adding the eta_curve kwarg must not change default behaviour: a call that
    omits it must be byte-identical to one that passes eta_curve=None explicitly."""
    cfg = Config(
        capacity_kwh=10.0, soc_floor=5.0, soc_target=90.0, max_charge_w=3000.0, eta_charge=0.9, round_trip_eff=0.8
    )
    slots = _slots(3)
    intervals = [
        ForecastInterval(BASE, pv_w=2000.0, load_w=300.0, dt_h=1.0),
        ForecastInterval(BASE + timedelta(hours=1), pv_w=0.0, load_w=1200.0, dt_h=1.0),
        ForecastInterval(BASE + timedelta(hours=2), pv_w=0.0, load_w=400.0, dt_h=1.0),
    ]
    selected = [BASE + timedelta(hours=1)]
    export = {BASE + timedelta(hours=2): 500.0}

    out_omitted = plan.build_plan_horizon(
        slots,
        intervals,
        selected,
        50.0,
        BASE + timedelta(hours=3),
        cfg,
        export_request_by_hour=export,
    )
    out_explicit_none = plan.build_plan_horizon(
        slots,
        intervals,
        selected,
        50.0,
        BASE + timedelta(hours=3),
        cfg,
        export_request_by_hour=export,
        eta_curve=None,
    )
    assert out_omitted == out_explicit_none


def test_build_plan_horizon_eta_curve_static_matches_default():
    """EfficiencyCurve.static(cfg) encodes the exact same scalars the eta_curve=None
    path derives from cfg — so substituting it must produce a byte-identical horizon."""
    from custom_components.anker_x1_smartgrid.efficiency import EfficiencyCurve

    cfg = Config(
        capacity_kwh=10.0, soc_floor=5.0, soc_target=90.0, max_charge_w=3000.0, eta_charge=0.9, round_trip_eff=0.8
    )
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
        slots,
        intervals,
        selected,
        50.0,
        BASE + timedelta(hours=3),
        cfg,
        export_request_by_hour=export,
        eta_curve=None,
    )
    out_curve = plan.build_plan_horizon(
        slots,
        intervals,
        selected,
        50.0,
        BASE + timedelta(hours=3),
        cfg,
        export_request_by_hour=export,
        eta_curve=curve,
    )
    assert out_none == out_curve


def test_build_display_horizon_accepts_eta_curve_kwarg():
    """build_display_horizon threads eta_curve straight through to build_plan_horizon;
    eta_curve=None (default) must be byte-identical to omitting it."""
    now = datetime(2026, 6, 20, 17, 0, tzinfo=UTC)
    slots = [PriceSlot(now + timedelta(hours=i), 0.30) for i in range(30)]
    sun_times = (
        datetime(2026, 6, 20, 20, 0, tzinfo=UTC),  # today_sunset
        datetime(2026, 6, 21, 6, 0, tzinfo=UTC),  # tomorrow_sunrise
        datetime(2026, 6, 21, 20, 0, tzinfo=UTC),  # tomorrow_sunset
    )
    kwargs = dict(
        today_arrays=[(1.0, None)],
        tomorrow_arrays=[(6.0, None)],
        sun_times=sun_times,
        predictor=_StubPredictor(),
        cur_temp=15.0,
        fallback_w=400.0,
        soc=50.0,
        selected=[],
        horizon_edge=now,
        cfg=Config(),
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
    now = datetime(2026, 6, 20, 17, 0, tzinfo=UTC)
    slots = [PriceSlot(now + timedelta(hours=i), 0.30) for i in range(4)]
    sun_times = (
        datetime(2026, 6, 20, 20, 0, tzinfo=UTC),
        datetime(2026, 6, 21, 6, 0, tzinfo=UTC),
        datetime(2026, 6, 21, 20, 0, tzinfo=UTC),
    )
    hour0 = now.replace(minute=0, second=0, microsecond=0)
    hour1 = hour0 + timedelta(hours=1)
    hour2 = hour1 + timedelta(hours=1)  # intentionally absent from temp_by_hour
    temp_by_hour = {hour0: 5.0, hour1: 12.0}

    out = plan.build_display_horizon(
        slots,
        now,
        today_arrays=None,
        tomorrow_arrays=[(6.0, None)],
        sun_times=sun_times,
        predictor=_TempEchoPredictor(),
        cur_temp=20.0,
        fallback_w=400.0,
        soc=50.0,
        selected=[],
        horizon_edge=now,
        cfg=Config(),
        temp_by_hour=temp_by_hour,
    )
    by_start = {e["start"]: e for e in out}
    assert by_start[hour0.isoformat()]["load_w"] == 50.0  # 5.0 * 10 (per-hour temp)
    assert by_start[hour1.isoformat()]["load_w"] == 120.0  # 12.0 * 10 (per-hour temp)
    assert by_start[hour2.isoformat()]["load_w"] == 200.0  # missing hour -> cur_temp (20.0) * 10


def test_build_display_horizon_omitting_temp_by_hour_uses_cur_temp():
    """Omitting temp_by_hour must preserve old behaviour: every hour predicted at cur_temp."""
    now = datetime(2026, 6, 20, 17, 0, tzinfo=UTC)
    slots = [PriceSlot(now + timedelta(hours=i), 0.30) for i in range(4)]
    sun_times = (
        datetime(2026, 6, 20, 20, 0, tzinfo=UTC),
        datetime(2026, 6, 21, 6, 0, tzinfo=UTC),
        datetime(2026, 6, 21, 20, 0, tzinfo=UTC),
    )
    out = plan.build_display_horizon(
        slots,
        now,
        today_arrays=None,
        tomorrow_arrays=[(6.0, None)],
        sun_times=sun_times,
        predictor=_TempEchoPredictor(),
        cur_temp=20.0,
        fallback_w=400.0,
        soc=50.0,
        selected=[],
        horizon_edge=now,
        cfg=Config(),
    )
    assert out, "expected a non-empty horizon"
    assert all(e["load_w"] == 200.0 for e in out)  # 20.0 * 10 everywhere


class TestEstimatedTail:
    """`estimated` row flagging (est_starts / terminal_need_kwh) — flag plumbing only,
    no tail slots appended yet (that's a later task)."""

    def test_all_rows_carry_estimated_false_by_default(self):
        """No est_starts passed → every row still carries the estimated key, all False."""
        cfg = Config(capacity_kwh=10.0, soc_target=100.0, max_charge_w=3000.0, eta_charge=1.0)
        slots = _slots(3)
        intervals = [
            ForecastInterval(BASE, pv_w=2000.0, load_w=300.0, dt_h=1.0),
            ForecastInterval(BASE + timedelta(hours=1), pv_w=0.0, load_w=400.0, dt_h=1.0),
            ForecastInterval(BASE + timedelta(hours=2), pv_w=0.0, load_w=400.0, dt_h=1.0),
        ]
        selected = [BASE + timedelta(hours=1)]
        out = plan.build_plan_horizon(slots, intervals, selected, 50.0, BASE + timedelta(hours=3), cfg)
        assert [e["estimated"] for e in out] == [False, False, False]

    def test_est_rows_flagged_and_mode_estimated(self):
        """Last 2 of 6 hours in est_starts → flagged + mode="estimated", overriding solar."""
        cfg = Config(capacity_kwh=10.0, soc_target=100.0, max_charge_w=3000.0, eta_charge=1.0)
        slots = _slots(6)
        intervals = [ForecastInterval(BASE + timedelta(hours=i), pv_w=1000.0, load_w=200.0, dt_h=1.0) for i in range(6)]
        est_starts = frozenset({BASE + timedelta(hours=4), BASE + timedelta(hours=5)})
        out = plan.build_plan_horizon(slots, intervals, [], 50.0, BASE + timedelta(hours=6), cfg, est_starts=est_starts)
        assert [e["estimated"] for e in out] == [False, False, False, False, True, True]
        assert [e["mode"] for e in out[4:]] == ["estimated", "estimated"]
        assert "solar" not in [e["mode"] for e in out[4:]]

    def test_est_rows_zero_charge_export(self):
        """Est rows have no interval (beyond the forecast horizon) -> charge/export stay 0."""
        cfg = Config(capacity_kwh=10.0, soc_target=100.0, max_charge_w=3000.0, eta_charge=1.0)
        slots = _slots(2)
        intervals = [ForecastInterval(BASE, pv_w=1000.0, load_w=200.0, dt_h=1.0)]
        est_starts = frozenset({BASE + timedelta(hours=1)})
        out = plan.build_plan_horizon(slots, intervals, [], 50.0, BASE + timedelta(hours=2), cfg, est_starts=est_starts)
        est_row = out[1]
        assert est_row["estimated"] is True
        assert est_row["grid_charge_kwh"] == 0.0
        assert est_row["grid_export_kwh"] == 0.0

    def test_est_soc_walk_clamps_at_firmware_floor(self):
        """Low start SoC + heavy est load -> est-row soc clamps at the firmware floor,
        never below it, and the walk stays monotone non-increasing."""
        cfg = Config(
            capacity_kwh=10.0,
            soc_floor=0.0,
            soc_target=100.0,
            max_charge_w=6000.0,
            eta_charge=1.0,
            round_trip_eff=1.0,
        )
        slots = _slots(3)
        intervals = [ForecastInterval(BASE + timedelta(hours=i), pv_w=0.0, load_w=6000.0, dt_h=1.0) for i in range(3)]
        est_starts = frozenset({BASE + timedelta(hours=1), BASE + timedelta(hours=2)})
        out = plan.build_plan_horizon(slots, intervals, [], 6.0, BASE + timedelta(hours=3), cfg, est_starts=est_starts)
        socs = [e["soc"] for e in out]
        assert socs[1] >= const.FIRMWARE_SOC_FLOOR
        assert socs[2] >= const.FIRMWARE_SOC_FLOOR
        assert all(a >= b for a, b in itertools.pairwise(socs))  # monotone non-increasing

    def test_est_reserve_soc_shows_need_line(self):
        """terminal_need_kwh=2.0 at 20 kWh capacity -> est reserve_soc == firmware floor (5) + 10 = 15.0."""
        cfg = Config(capacity_kwh=20.0, soc_target=100.0)
        slots = _slots(2)
        intervals = [ForecastInterval(BASE, pv_w=0.0, load_w=0.0, dt_h=1.0)]
        est_starts = frozenset({BASE + timedelta(hours=1)})
        out = plan.build_plan_horizon(
            slots,
            intervals,
            [],
            50.0,
            BASE + timedelta(hours=2),
            cfg,
            est_starts=est_starts,
            terminal_need_kwh=2.0,
        )
        assert out[1]["reserve_soc"] == 15.0

    def test_est_rows_marked_past_horizon(self):
        """Est slot start >= horizon_edge -> is_past_horizon True (unmodified existing logic)."""
        cfg = Config(capacity_kwh=10.0, soc_target=100.0)
        slots = _slots(2)
        intervals = [ForecastInterval(BASE, pv_w=0.0, load_w=0.0, dt_h=1.0)]
        est_starts = frozenset({BASE + timedelta(hours=1)})
        # horizon_edge before the est slot's start -> it must read as past-horizon.
        out = plan.build_plan_horizon(slots, intervals, [], 50.0, BASE, cfg, est_starts=est_starts)
        assert out[1]["estimated"] is True
        assert out[1]["is_past_horizon"] is True


# ---------------------------------------------------------------------------
# 15-min display-path fixes (A1/A2/A3, code review 2026-07-31)
# ---------------------------------------------------------------------------


def test_build_plan_horizon_quarter_grid_fidelity_at_15min():
    """A2 regression: iv_by_hour/selected_set/req_by_hour/exp_by_hour/ceil_by_hour
    must be keyed on the SLOT grid at slot_minutes=15, not collapsed onto the
    shared clock-hour (which let a later unselected quarter silently overwrite
    an earlier committed one, and painted mode="grid" onto all 4 quarters)."""
    cfg = Config(capacity_kwh=10.0, soc_target=100.0, max_charge_w=6000.0, eta_charge=1.0)
    q0 = BASE
    q1 = BASE + timedelta(minutes=15)
    q2 = BASE + timedelta(minutes=30)
    q3 = BASE + timedelta(minutes=45)
    quarters = [q0, q1, q2, q3]
    slots = [PriceSlot(q, 0.30) for q in quarters]
    intervals = [ForecastInterval(q, pv_w=0.0, load_w=0.0, dt_h=0.25) for q in quarters]
    # Only q1 is committed: a 4000 W grid charge. q0/q2/q3 are NOT selected.
    selected = [q1]
    grid_request = {q1: 4000.0}
    out = plan.build_plan_horizon(
        slots,
        intervals,
        selected,
        0.0,
        BASE + timedelta(hours=1),
        cfg,
        grid_request_by_hour=grid_request,
        slot_minutes=15,
    )
    by_start = {e["start"]: e for e in out}
    assert by_start[q0.isoformat()]["mode"] == "idle"
    assert by_start[q1.isoformat()]["mode"] == "grid"
    assert by_start[q2.isoformat()]["mode"] == "idle"
    assert by_start[q3.isoformat()]["mode"] == "idle"
    # The committed 4000 W quarter survives...
    assert by_start[q1.isoformat()]["grid_charge_w"] == 4000.0
    # ...and does NOT bleed onto its unselected siblings.
    assert by_start[q0.isoformat()]["grid_charge_w"] == 0.0
    assert by_start[q2.isoformat()]["grid_charge_w"] == 0.0
    assert by_start[q3.isoformat()]["grid_charge_w"] == 0.0


def test_build_plan_horizon_slot_minutes_60_byte_identical():
    """A2 invariant: floor_to_slot(x, 60) == hour_floor(x), so an explicit
    slot_minutes=60 must be byte-identical to the (pre-fix) implicit default."""
    cfg = Config(capacity_kwh=10.0, soc_target=100.0, max_charge_w=3000.0, eta_charge=1.0)
    slots = _slots(3)
    intervals = [
        ForecastInterval(BASE, pv_w=2000.0, load_w=300.0, dt_h=1.0),
        ForecastInterval(BASE + timedelta(hours=1), pv_w=0.0, load_w=400.0, dt_h=1.0),
        ForecastInterval(BASE + timedelta(hours=2), pv_w=0.0, load_w=400.0, dt_h=1.0),
    ]
    selected = [BASE + timedelta(hours=1)]
    horizon_edge = BASE + timedelta(hours=3)
    implicit = plan.build_plan_horizon(slots, intervals, selected, 50.0, horizon_edge, cfg)
    explicit = plan.build_plan_horizon(slots, intervals, selected, 50.0, horizon_edge, cfg, slot_minutes=60)
    assert implicit == explicit
    assert [e["mode"] for e in implicit] == ["solar", "grid", "idle"]


def test_build_display_horizon_threads_slot_minutes_to_dt_h():
    """A1 regression: build_display_horizon must forward slot_minutes to
    build_display_intervals/build_plan_horizon so 15-min rows integrate energy
    at dt_h=0.25, not the hour-locked default of dt_h=1.0 (4x inflation)."""
    now = datetime(2026, 6, 20, 17, 0, tzinfo=UTC)
    slots = [PriceSlot(now + timedelta(minutes=15 * i), 0.30) for i in range(8)]
    sun_times = _sun_times_for(now)
    cfg = Config(capacity_kwh=10.0, soc_target=100.0, max_charge_w=4000.0, eta_charge=1.0)
    out = plan.build_display_horizon(
        slots,
        now,
        today_arrays=None,
        tomorrow_arrays=None,
        sun_times=sun_times,
        predictor=_StubPredictor(),
        cur_temp=None,
        fallback_w=400.0,
        soc=50.0,
        selected=[],
        horizon_edge=now + timedelta(hours=1),
        cfg=cfg,
        slot_minutes=15,
    )
    assert out, "expected non-empty horizon"
    # _StubPredictor returns a constant 500 W load regardless of dt/temp;
    # load_kwh = load_w * dt_h / 1000 must reflect the true 15-min slot width
    # (0.125 kWh), not the hour-locked default (0.5 kWh).
    assert out[0]["load_kwh"] == pytest.approx(0.125)


def test_build_display_horizon_slot_minutes_60_is_byte_identical_to_implicit():
    """A1 invariant: explicit slot_minutes=60 must match the implicit default —
    zero behavior change at the legacy hourly resolution."""
    now = datetime(2026, 6, 20, 17, 0, tzinfo=UTC)
    slots = [PriceSlot(now + timedelta(hours=i), 0.30) for i in range(6)]
    sun_times = _sun_times_for(now)
    cfg = Config(capacity_kwh=10.0, soc_target=100.0, max_charge_w=4000.0, eta_charge=1.0)
    kwargs = dict(
        today_arrays=[(1.0, None)],
        tomorrow_arrays=[(6.0, None)],
        sun_times=sun_times,
        predictor=_StubPredictor(),
        cur_temp=None,
        fallback_w=400.0,
        soc=50.0,
        selected=[now.replace(minute=0, second=0, microsecond=0)],
        horizon_edge=now + timedelta(hours=2),
        cfg=cfg,
    )
    implicit = plan.build_display_horizon(slots, now, **kwargs)
    explicit = plan.build_display_horizon(slots, now, slot_minutes=60, **kwargs)
    assert implicit == explicit
    assert implicit, "expected non-empty horizon"


def test_past_slot_kwh_read_per_slot_at_15min():
    """At slot_minutes=15 the actuals arrive already bucketed per SLOT, so each
    quarter row carries its own measurement verbatim — no hour total stamped
    onto every quarter (the 4x-inflated column totals of finding A3), and no
    even split of an hour total either (which flattened four real quarters into
    one repeated mean; superseded 2026-08-03 by slot-grid bucketing)."""
    cfg = Config(capacity_kwh=10.0, soc_target=100.0, max_charge_w=6000.0, eta_charge=1.0)
    hour_start = BASE
    quarters = [hour_start + timedelta(minutes=15 * i) for i in range(4)]
    # A rising PV quarter-hour: the four buckets must stay distinguishable.
    per_slot = {
        q: {
            "pv_w": pv,
            "load_w": 400.0,
            "soc": 50.0,
            "solar_charge_w": 400.0,
            "grid_charge_w": 0.0,
            "grid_export_w": 0.0,
            "pv_kwh": pv * 0.25 / 1000.0,
            "load_kwh": 0.1,
            "solar_charge_kwh": 0.1,
            "grid_charge_kwh": 0.0,
            "grid_export_kwh": 0.0,
        }
        for q, pv in zip(quarters, (200.0, 600.0, 1000.0, 1400.0))
    }
    slots = [PriceSlot(q, 0.30) for q in quarters]
    out = plan.build_plan_horizon(
        slots,
        [],
        [],
        49.0,
        hour_start + timedelta(hours=1),
        cfg,
        past_actuals_by_slot=per_slot,
        slot_minutes=15,
    )
    assert len(out) == 4
    assert all(e["mode"] == "actual" for e in out)
    assert [e["pv_w"] for e in out] == [200.0, 600.0, 1000.0, 1400.0]
    assert [e["pv_kwh"] for e in out] == pytest.approx([0.05, 0.15, 0.25, 0.35])
    assert sum(e["load_kwh"] for e in out) == pytest.approx(0.4)


def test_past_slot_kwh_unscaled_at_default_slot_minutes():
    """A3 invariant: slot_minutes=60 (default) leaves past-actual kwh columns
    byte-identical to the raw recorded values (the A3 fraction is 1.0)."""
    cfg = Config(capacity_kwh=10.0, soc_target=100.0, max_charge_w=6000.0, eta_charge=1.0)
    act = {
        "pv_w": 800.0,
        "load_w": 400.0,
        "soc": 50.0,
        "solar_charge_w": 400.0,
        "grid_charge_w": 0.0,
        "grid_export_w": 0.0,
        "pv_kwh": 0.837,
        "load_kwh": 0.412,
        "solar_charge_kwh": 0.4,
        "grid_charge_kwh": 0.0,
        "grid_export_kwh": 0.0,
    }
    out = plan.build_plan_horizon(
        _slots(1),
        [],
        [],
        49.0,
        BASE + timedelta(hours=1),
        cfg,
        past_actuals_by_slot={BASE: act},
    )
    assert out[0]["pv_kwh"] == 0.837
    assert out[0]["load_kwh"] == 0.412
