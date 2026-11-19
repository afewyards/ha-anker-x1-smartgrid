"""Backup reserve in both DP cores: the pack stops at it unless dipping under it pays, and refills."""

from __future__ import annotations

import random
from itertools import pairwise

import pytest

from custom_components.anker_x1_smartgrid import dp_common
from custom_components.anker_x1_smartgrid.models import Config
from custom_components.anker_x1_smartgrid.optimize import optimize_grid
from custom_components.anker_x1_smartgrid.regret import (
    _BIN_KWH,
    DayData,
    _apply_solar_load,
    hindsight_optimal_grid,
    realized_grid_cost,
)

CFG = Config(capacity_kwh=10.0, backup_reserve_soc=30.0)
ETA = CFG.eta_charge_safe()


def _opt(load, price, soc_start, cfg=CFG, pv=None, **kw):
    n = len(load)
    return optimize_grid(
        list(pv or [0.0] * n),
        list(load),
        list(price),
        soc_start,
        cfg,
        window_start_h=0,
        window_len=n,
        terminal_mode="water_value",
        **kw,
    )


def _charged_slots(res):
    return [h for h, g in enumerate(res["schedule"]) if g > 1e-9]


def _dc(res):
    return sum(res["schedule"]) * ETA


class _Curve:
    """Low powers discharge at 0.6, everything else at 0.95."""

    def eta_discharge(self, w: float) -> float:
        return 0.6 if w < 500.0 else 0.95

    def eta_charge(self, _w: float) -> float:
        return 1.0


def test_reserve_wall_is_grid_snapped_and_off_at_zero():
    assert dp_common.reserve_wall_kwh(Config()) == 0.0
    assert dp_common.reserve_wall_kwh(CFG) == pytest.approx(3.0)
    # 25% of 20.48 kWh is 5.12, between bins: rounded up so the pack is never usable below it.
    assert dp_common.reserve_wall_kwh(Config(capacity_kwh=20.48, backup_reserve_soc=25.0)) == pytest.approx(5.15)


def test_discharge_wall():
    assert dp_common.discharge_wall_kwh(6.0, 0.5, 3.0) == 3.0
    assert dp_common.discharge_wall_kwh(1.2, 0.5, 3.0) == 1.2
    assert dp_common.discharge_wall_kwh(0.2, 0.5, 3.0) == 0.5
    assert dp_common.discharge_wall_kwh(6.0, 0.5, 0.0) == 0.5


def test_house_discharge_stops_at_reserve():
    n = 8
    res = _opt([1.0] * n, [0.30] * n, 60.0, chargeable=[False] * n)
    assert res["schedule"] == [0.0] * n
    # 6 kWh start, 3 kWh usable above the 3 kWh reserve; the rest of the 8 kWh comes from the grid.
    assert res["eur"] == pytest.approx(0.30 * (8 - 3), abs=0.30 * _BIN_KWH)
    assert res["hold_schedule"] == [False] * 3 + [True] * 5
    # Without the reserve the pack runs down to the 5% firmware floor.
    off = _opt([1.0] * n, [0.30] * n, 60.0, cfg=Config(capacity_kwh=10.0), chargeable=[False] * n)
    assert off["eur"] < res["eur"] - 0.30 * 2
    assert "hold_schedule" not in off


def test_start_under_reserve_holds_not_lifted():
    cfg = Config(capacity_kwh=10.0, backup_reserve_soc=30.0, idle_drain_w=100.0)
    load = [0.5, 0.8, 0.3, 1.0, 0.6, 0.4]
    price = [0.30, 0.25, 0.10, 0.40, 0.35, 0.20]
    n = len(load)
    res = _opt(load, price, 12.0, cfg=cfg, chargeable=[False] * n)
    assert res["schedule"] == [0.0] * n
    # The pack neither serves the house nor is lifted to the reserve: the meter buys exactly the deficit + standby.
    assert res["eur"] == pytest.approx(sum((d + 0.1) * p for d, p in zip(load, price)), abs=1e-9)
    assert res["hold_schedule"] == [True] * n


def test_refill_in_cheapest_slot():
    price = [0.30] * 3 + [0.10] + [0.30] * 8
    res = _opt([0.0] * 12, price, 12.0)
    assert _charged_slots(res) == [3]
    assert _dc(res) == pytest.approx(1.8, abs=1e-9)


def test_refill_flat_price_first_slot():
    res = _opt([0.0] * 12, [0.25] * 12, 12.0)
    assert _charged_slots(res) == [0]
    assert _dc(res) == pytest.approx(1.8, abs=1e-9)


def test_refill_waits_only_if_saving_beats_rent():
    # Waiting one slot saves 1.8 / 0.92 × 0.005 ≈ €0.0098. Rent for the slot is spread × 1.8² / (16 × 2.5):
    # €0.0016 at the 0.02 floor (wait), €0.04 once a 0.80 peak widens the spread (refill now).
    flat = _opt([0.0] * 12, [0.30, 0.295] + [0.30] * 10, 12.0)
    assert _charged_slots(flat) == [1]
    wide = _opt([0.0] * 12, [0.30, 0.295] + [0.30] * 9 + [0.80], 12.0)
    assert 0 in _charged_slots(wide)


def _bins():
    return 40, (lambda b: b * 0.25)


@pytest.mark.parametrize("refill_h", [8.0, 4.0])
def test_urgency_at_floor_bin(refill_h):
    n, from_bin = _bins()
    u = dp_common.reserve_urgency_by_bin(3.0, 0.5, 0.10, refill_h, n, from_bin, 1.0)
    assert u[2] == pytest.approx(0.10 / refill_h * (3.0 - 0.5) / 2)
    assert u[12] == 0.0 and u[30] == 0.0


def test_urgency_off_and_convex():
    n, from_bin = _bins()
    assert dp_common.reserve_urgency_by_bin(0.0, 0.5, 0.10, 8.0, n, from_bin, 1.0) is None
    u = dp_common.reserve_urgency_by_bin(3.0, 0.5, 0.10, 8.0, n, from_bin, 1.0)
    steps = [u[b] - u[b + 1] for b in range(2, 12)]
    assert all(a > b for a, b in pairwise(steps))


def test_published_spread():
    assert dp_common.published_spread([0.10, 0.30, 0.20]) == pytest.approx(0.20)
    assert dp_common.published_spread([0.2, 0.2]) == 0.02


def test_refill_h_config():
    assert Config().backup_reserve_refill_h == 8.0
    assert Config.from_dict({"backup_reserve_refill_h": 4.0}).backup_reserve_refill_h == 4.0


def test_refill_through_closed_mask():
    n = 12
    price = [0.10] * n
    # A water value well above the charge cost: with the mask open the DP fills far past the reserve.
    opened = _opt([0.0] * n, price, 12.0, water_value=0.5, chargeable=[True] * n)
    assert _dc(opened) > 1.8 + 1.0
    closed = _opt([0.0] * n, price, 12.0, water_value=0.5, chargeable=[False] * n)
    assert closed["schedule"] == [0.0] * n
    res = _opt([0.0] * n, price, 12.0, water_value=0.5, chargeable=[False] * n, refill_ok=[True] * n)
    assert _dc(res) == pytest.approx(1.8, abs=1e-9)


def test_no_topup_loop_at_reserve():
    cfg = Config(capacity_kwh=10.0, backup_reserve_soc=30.0, idle_drain_w=100.0)
    res = _opt([0.0] * 12, [0.25] * 12, 30.0, cfg=cfg)
    assert res["schedule"] == [0.0] * 12
    # The standby is bought at the reserve rather than drawn from the pack and bought back.
    assert res["eur"] == pytest.approx(0.1 * 0.25 * 12, abs=1e-9)
    assert res["hold_schedule"] == [True] * 12


def test_start_at_off_grid_reserve_does_not_top_up():
    # 25% of 20.48 kWh = 5.12 kWh bins to 5.10, under the 5.15 snapped reserve.
    cfg = Config(capacity_kwh=20.48, backup_reserve_soc=25.0)
    res = _opt([0.0] * 12, [0.25] * 12, 25.0, cfg=cfg)
    assert res["schedule"] == [0.0] * 12


@pytest.mark.parametrize("cap", [10.0, 20.48])
def test_start_a_fraction_under_reserve_does_not_top_up(cap):
    # The executor holds anywhere under reserve + 1, so a pack that sagged a little while held is not refilled.
    cfg = Config(capacity_kwh=cap, backup_reserve_soc=30.0, idle_drain_w=100.0)
    load, price = [0.5] * 12, [0.25] * 12
    kw: dict = dict(water_value=0.12, chargeable=[False] * 12, refill_ok=[True] * 12)
    assert _opt(load, price, 29.5, cfg=cfg, **kw)["schedule"] == [0.0] * 12
    day = DayData(pv_kwh=(0.0,) * 12, load_kwh=tuple(load), price=tuple(price), soc_start=29.5)
    assert hindsight_optimal_grid(day, cfg, terminal_mode="water_value", water_value=0.12)["schedule"] == [0.0] * 12
    # The dead band is one point: further under, the pack still refills.
    assert _charged_slots(_opt(load, price, 28.5, cfg=cfg, **kw)) == [0]


def _closed_refill(cfg, soc_start, load, price, chargeable, **kw):
    n = len(load)
    kw = dict(window_start_h=0, window_len=n, terminal_mode="water_value", chargeable=chargeable, **kw)
    args = ([0.0] * n, list(load), list(price), soc_start, cfg)
    return optimize_grid(*args, **kw), optimize_grid(*args, refill_ok=[True] * n, **kw)


_QUARTERS: dict = dict(water_value=0.05, dt_h=0.25, slots_per_day=96)
# One 5 kW quarter at a negative price, in a slot the mask closes.
_NEG_PRICE = [0.10, -0.02, 0.10] + [0.12] * 5


def test_refill_ok_is_inert_without_reserve():
    cfg = Config(capacity_kwh=20.48, idle_drain_w=100.0)
    plain, refill = _closed_refill(cfg, 5.0, [0.3, 1.25] + [0.3] * 6, _NEG_PRICE, [False] * 8, **_QUARTERS)
    assert refill == plain
    rng = random.Random(1)
    for _ in range(40):
        eta = rng.choice([1.0, 0.92])
        cfg = Config(
            capacity_kwh=10.0,
            eta_charge=eta,
            round_trip_eff=eta * 0.9 if eta < 1.0 else 1.0,
            idle_drain_w=rng.choice([0.0, 100.0]),
        )
        load = [rng.choice([0.3, 0.8, 1.5, 2.5]) for _ in range(12)]
        price = [rng.choice([-0.05, 0.0, 0.1, 0.25, 0.4]) for _ in range(12)]
        mask = [rng.random() < 0.3 for _ in range(12)]
        plain, refill = _closed_refill(cfg, rng.choice([5.0, 6.0, 8.0, 15.0]), load, price, mask, water_value=0.1)
        assert refill == plain


@pytest.mark.parametrize(
    ("cap", "quarter_kwh", "soc_start"),
    [(20.48, 1.25, 30.0), (20.48, 1.25, 35.0), (10.0, 3.0, 30.0)],
)
def test_refill_ok_does_not_charge_a_pack_at_or_above_reserve(cap, quarter_kwh, soc_start):
    # The wall already stops the pack at the reserve; a "charge" here would only buy house load at -0.02,
    # the last case modelled down past the firmware floor.
    cfg = Config(capacity_kwh=cap, backup_reserve_soc=30.0, idle_drain_w=100.0)
    plain, refill = _closed_refill(cfg, soc_start, [0.3, quarter_kwh] + [0.3] * 6, _NEG_PRICE, [False] * 8, **_QUARTERS)
    assert refill["schedule"] == [0.0] * 8
    assert refill == plain


def test_eur_excludes_penalties():
    load = [0.5, 0.8, 0.3, 1.0]
    price = [0.30, 0.25, 0.10, 0.40]
    res = _opt(load, price, 12.0, grid_charge_ceiling=[1.0] * 4)
    assert res["schedule"] == [0.0] * 4
    assert res["eur"] == pytest.approx(sum(d * p for d, p in zip(load, price)), abs=1e-9)
    assert res["kwh"] == pytest.approx(sum(load), abs=1e-9)
    assert "infeasible" not in res


def test_export_never_sells_reserve():
    n = 6
    export_price = [0.05, 0.05, 0.60, 0.05, 0.05, 0.05]
    kw = dict(export_price=export_price, chargeable=[False] * n)
    res = _opt([0.0] * n, [0.20] * n, 35.0, **kw)
    eta_d = CFG.eta_discharge_static()
    sold_dc = sum(res["export_schedule"]) / eta_d
    assert 0.0 < sold_dc <= 0.5 + 1e-9
    # Without the reserve the same peak sells the pack down to the soft floor.
    off = _opt([0.0] * n, [0.20] * n, 35.0, cfg=Config(capacity_kwh=10.0), **kw)
    assert sum(off["export_schedule"]) / eta_d > 2.5


@pytest.mark.parametrize("idle_w", [0.0, 100.0])
def test_wall_import_with_eta_curve(idle_w):
    # 250 W deficits discharge at eta 0.6: the wall must book the AC the meter buys, not the DC the pack lost,
    # and the standby 1:1, as a held pack books it.
    cfg = Config(capacity_kwh=10.0, backup_reserve_soc=30.0, idle_drain_w=idle_w)
    n = 6
    load = [0.25] * n
    price = [0.30, 0.25, 0.10, 0.40, 0.35, 0.20]
    bought = sum((d + idle_w / 1000.0) * p for d, p in zip(load, price))
    passive = _opt(load, price, 30.0, cfg=cfg, chargeable=[False] * n, eta_curve=_Curve())
    held = _opt(load, price, 30.0, cfg=cfg, chargeable=[False] * n, eta_curve=_Curve(), hold=[True] * n)
    assert passive["wall_schedule"] == [True] * n
    assert passive["eur"] == pytest.approx(held["eur"], abs=1e-6)
    assert passive["eur"] == pytest.approx(bought, abs=1e-6)
    day = DayData(pv_kwh=(0.0,) * n, load_kwh=tuple(load), price=tuple(price), soc_start=30.0)
    walled = realized_grid_cost(day, [0.0] * n, cfg, eta_curve=_Curve(), wall_by_hour=passive["wall_schedule"])
    assert walled["eur"] == pytest.approx(bought, abs=1e-9)


_PV = [0.0] * 8 + [0.5, 1.5, 2.5, 3.0, 3.0, 2.5, 1.5, 0.5] + [0.0] * 8
_LOAD = [0.4] * 6 + [0.8, 1.0, 0.6, 0.5, 0.4, 0.4, 0.4, 0.5, 0.7, 0.9, 1.2, 1.5, 1.4, 1.2, 1.0, 0.8, 0.6, 0.5]
_PRICE = [0.18, 0.16, 0.15, 0.14, 0.16, 0.20, 0.28, 0.33, 0.27, 0.20, 0.12, 0.08]
_PRICE += [0.07, 0.10, 0.15, 0.22, 0.30, 0.38, 0.42, 0.36, 0.30, 0.25, 0.21, 0.19]


@pytest.mark.parametrize("export_on", [False, True])
@pytest.mark.parametrize("soc_start", [15.0, 45.0])
def test_parity_with_reserve(export_on, soc_start):
    kw: dict = dict(terminal_mode="water_value", water_value=0.12)
    if export_on:
        kw["export_price"] = [p - 0.02 for p in _PRICE]
    day = DayData(pv_kwh=tuple(_PV), load_kwh=tuple(_LOAD), price=tuple(_PRICE), soc_start=soc_start)
    hind = hindsight_optimal_grid(day, CFG, **kw)
    opt = optimize_grid(list(_PV), list(_LOAD), list(_PRICE), soc_start, CFG, window_start_h=0, window_len=24, **kw)
    assert opt["schedule"] == pytest.approx(hind["schedule"], abs=1e-9)
    assert opt["kwh"] == pytest.approx(hind["kwh"], abs=1e-9)
    assert opt["eur"] == pytest.approx(hind["eur"], abs=1e-9)
    if export_on:
        assert opt["export_schedule"] == pytest.approx(hind["export_schedule"], abs=1e-9)
    # The reserve binds on this day: the plan differs from the one without it.
    off_cfg = Config(capacity_kwh=10.0)
    off = optimize_grid(list(_PV), list(_LOAD), list(_PRICE), soc_start, off_cfg, window_start_h=0, window_len=24, **kw)
    assert off["eur"] != pytest.approx(opt["eur"], abs=1e-6)


@pytest.mark.parametrize("soc_start", [25.0, 24.0, 40.0])
def test_parity_with_reserve_quarter_hourly(soc_start):
    # 25% of 20.48 kWh is off the SoC grid; 15-min slots scale the urgency and the standby by dt.
    cfg = Config(capacity_kwh=20.48, backup_reserve_soc=25.0, idle_drain_w=100.0)
    pv, load = ([x / 4 for x in a for _ in range(4)] for a in (_PV, _LOAD))
    price = [p for p in _PRICE for _ in range(4)]
    kw: dict = dict(terminal_mode="water_value", water_value=0.12, export_price=[p - 0.02 for p in price], dt_h=0.25)
    day = DayData(pv_kwh=tuple(pv), load_kwh=tuple(load), price=tuple(price), soc_start=soc_start)
    hind = hindsight_optimal_grid(day, cfg, **kw)
    opt = optimize_grid(pv, load, price, soc_start, cfg, window_start_h=0, window_len=96, slots_per_day=96, **kw)
    assert opt["schedule"] == pytest.approx(hind["schedule"], abs=1e-9)
    assert opt["export_schedule"] == pytest.approx(hind["export_schedule"], abs=1e-9)
    assert opt["kwh"] == pytest.approx(hind["kwh"], abs=1e-9)
    assert opt["eur"] == pytest.approx(hind["eur"], abs=1e-9)


def test_realized_cost_stops_at_reserve_only_in_walled_slots():
    load = (0.5, 0.8, 0.3, 1.0, 0.6, 0.4)
    price = (0.30, 0.25, 0.10, 0.40, 0.35, 0.20)
    day = DayData(pv_kwh=(0.0,) * 6, load_kwh=load, price=price, soc_start=30.0)
    res = realized_grid_cost(day, [0.0] * 6, CFG, wall_by_hour=[True] * 6)
    assert sum(res["forced_import_kwh"]) == pytest.approx(sum(load), abs=1e-9)
    assert res["eur"] == pytest.approx(sum(d * p for d, p in zip(load, price)), abs=1e-9)
    # Unwalled, the pack serves the house down to the firmware floor.
    assert sum(realized_grid_cost(day, [0.0] * 6, CFG)["forced_import_kwh"]) < sum(load) - 2.0


def test_realized_cost_replays_the_plan():
    load = [0.5, 0.8, 0.3, 1.0, 0.6, 0.4]
    price = [0.30, 0.25, 0.10, 0.40, 0.35, 0.20]
    res = _opt(load, price, 30.0)
    assert any(res["wall_schedule"]) and not all(res["wall_schedule"])
    day = DayData(pv_kwh=(0.0,) * 6, load_kwh=tuple(load), price=tuple(price), soc_start=30.0)
    replay = realized_grid_cost(day, res["schedule"], CFG, wall_by_hour=res["wall_schedule"])
    assert replay["eur"] == pytest.approx(res["eur"], abs=1e-9)


def _q(xs, dt_h):
    return [x for x in xs for _ in range(round(1 / dt_h))]


def _soc_path(res, load, soc_start, cfg, dt_h):
    """SoC % at each slot end, replaying the plan's charges, exports and walls (static eta, no PV)."""
    _, to_bin, from_bin = dp_common.soc_bins(cfg.capacity_kwh)
    floor = dp_common.grid_floor_kwh(cfg.firmware_floor_kwh)
    reserve = dp_common.reserve_wall_kwh(cfg)
    n = len(load)
    walled = res.get("wall_schedule") or [False] * n
    export = res.get("export_schedule") or [0.0] * n
    soc = from_bin(to_bin(dp_common.start_soc_kwh(cfg.pct_to_kwh(soc_start), cfg, floor, reserve)))
    path = []
    for h in range(n):
        stop = dp_common.discharge_wall_kwh(soc, floor, reserve) if walled[h] else floor
        after = _apply_solar_load(soc, -load[h], cfg, dt_h)
        soc = from_bin(to_bin(max(after + res["schedule"][h] * ETA - export[h] / cfg.eta_discharge_static(), stop)))
        path.append(cfg.kwh_to_pct(soc))
    return path


def _both(load, price, soc_start, dt_h, cfg=CFG, **kw):
    """DP plan and its SoC path, after checking the oracle chose the same plan."""
    n = len(load)
    kw = dict(terminal_mode="water_value", water_value=0.10, dt_h=dt_h, **kw)
    res = optimize_grid(
        [0.0] * n,
        list(load),
        list(price),
        soc_start,
        cfg,
        window_start_h=0,
        window_len=n,
        slots_per_day=round(24 / dt_h),
        **kw,
    )
    hind = hindsight_optimal_grid(DayData((0.0,) * n, tuple(load), tuple(price), soc_start), cfg, **kw)
    assert res["schedule"] == pytest.approx(hind["schedule"], abs=1e-9)
    assert res["kwh"] == pytest.approx(hind["kwh"], abs=1e-9)
    assert res["eur"] == pytest.approx(hind["eur"], abs=1e-9)
    if "export_price" in kw:
        assert res["export_schedule"] == pytest.approx(hind["export_schedule"], abs=1e-9)
    return res, _soc_path(res, load, soc_start, cfg, dt_h)


_HOURS = pytest.mark.parametrize("dt_h", [1.0, 0.25])
# The house draws only in a 2 h peak, so a dip there waits under the reserve until the refill.
_PEAK_LOAD = [1.0, 1.0] + [0.0] * 22


@_HOURS
def test_dips_under_reserve_when_refill_is_near(dt_h):
    res, path = _both(_q([1.0 * dt_h] * 8, dt_h), _q([0.20, 0.20] + [0.00] * 6, dt_h), 30.0, dt_h)
    assert min(path) <= 10.0 + 1e-9
    assert path[-1] >= 30.0
    _, peak_only = _both(_q([x * dt_h for x in _PEAK_LOAD], dt_h), _q([0.20] * 2 + [0.00] * 22, dt_h), 30.0, dt_h)
    assert min(peak_only) <= 10.0 + 1e-9


@_HOURS
def test_dip_schedule_marks_the_slots_the_plan_runs_under_the_reserve(dt_h):
    res, path = _both(_q([1.0 * dt_h] * 8, dt_h), _q([0.20, 0.20] + [0.00] * 6, dt_h), 30.0, dt_h)
    expected = [not res["schedule"][h] and not res["wall_schedule"][h] and p < 30.0 for h, p in enumerate(path)]
    assert any(res["dip_schedule"])
    assert res["dip_schedule"] == expected


@_HOURS
@pytest.mark.parametrize("load_kw", [0.4, 1.0])
def test_flat_price_at_reserve_plans_no_dip(dt_h, load_kw):
    n = round(12 / dt_h)
    res, _ = _both([load_kw * dt_h] * n, [0.25] * n, 30.0, dt_h)
    assert res["dip_schedule"] == [False] * n


def test_pv_surplus_under_reserve_is_not_a_dip():
    # A forecast surplus under the reserve charges the pack, so the executor still holds if the house draws.
    res = _opt([0.4] * 4, [0.40, 0.40, 0.05, 0.05], 20.0, pv=[1.0, 0.0, 0.0, 0.0], water_value=0.10)
    assert res["schedule"][0] == 0.0
    assert res["dip_schedule"][:2] == [False, True]


def test_no_dip_schedule_without_a_reserve():
    assert "dip_schedule" not in _opt([1.0] * 4, [0.30] * 4, 50.0, cfg=Config(capacity_kwh=10.0))


@_HOURS
def test_far_refill_dips_only_as_deep_as_the_rent_allows(dt_h):
    load = _q([x * dt_h for x in _PEAK_LOAD], dt_h)
    _, near = _both(load, _q([0.20] * 2 + [0.00] * 22, dt_h), 30.0, dt_h)
    _, far = _both(load, _q([0.20] * 20 + [0.00] * 4, dt_h), 30.0, dt_h)
    band = CFG.pct_to_kwh(30.0 - 5.0)
    depth = CFG.pct_to_kwh(30.0 - min(far)) / band
    # The rent's marginal kWh breaks even at depth gain/spread × refill_h/T, the hold being the alternative.
    gain, spread, wait_h = 0.20 - CFG.cycle_cost_eur_per_kwh, 0.20, 20 - 2
    optimum = gain / spread * CFG.backup_reserve_refill_h / wait_h
    # A slot either dips its whole deficit or stops at the wall, so the plan lands within half a slot of it.
    assert depth <= optimum + max(_BIN_KWH, 1.0 * dt_h / 2) / band + 1e-9
    # At 60 min the only dip is a whole 1 kWh slot (depth 0.4): it and the hold straddle the 0.22 optimum
    # within the dip allowance (under 0.005 EUR), so the hold may win there.
    assert depth > 0.0 or dt_h == 1.0
    assert min(far) > min(near)


@_HOURS
def test_far_refill_with_house_load_waits_at_reserve_until_refill_is_near(dt_h):
    _, path = _both(_q([1.0 * dt_h] * 24, dt_h), _q([0.20] * 20 + [0.00] * 4, dt_h), 30.0, dt_h)
    waiting = path[: round(18 / dt_h)]
    assert min(waiting) == pytest.approx(30.0)
    assert min(path) < 30.0


@_HOURS
@pytest.mark.parametrize(("soc_start", "load_kw"), [(30.0, 1.0), (60.0, 0.4)])
def test_flat_price_never_dips_under_reserve(dt_h, soc_start, load_kw):
    n = round(12 / dt_h)
    _, path = _both([load_kw * dt_h] * n, [0.25] * n, soc_start, dt_h)
    assert min(path) >= 30.0 - 1e-9


@pytest.mark.parametrize("deficit_w", [150.0, 180.0, 200.0, 250.0])
def test_steady_deficit_at_reserve_neither_dips_nor_charges(deficit_w):
    # With 100 W standby, 200 W drains exactly 1.5 bins a 15-min slot: rounding alone once made dipping and
    # charging back look cheaper than holding.
    cfg = Config(capacity_kwh=10.0, backup_reserve_soc=40.0, idle_drain_w=100.0)
    n = 96
    res, path = _both([deficit_w / 1000.0 * 0.25] * n, [0.25] * n, 40.0, 0.25, cfg=cfg)
    assert min(path) >= 40.0 - 1e-9
    assert res["schedule"] == [0.0] * n
    assert res["dip_schedule"] == [False] * n


def test_characterize_last_minute_wall_in_a_dip_slot():
    """Characterization of a planner artefact the executor absorbs, not a requirement; a DP-side fix may change it.

    Given only the last minute of a dip slot, the allowance at the peak price outweighs that minute's drain and
    the plan walls it (test_dip_slots_ride_out_a_gap_no_longer_than_the_dwell rides it). The live decision path
    feeds slot 0 a full slot of load, so it does not build this input.
    """
    cfg = Config(capacity_kwh=10.0, backup_reserve_soc=40.0, idle_drain_w=100.0)
    n = 96
    load = [0.25] * n
    load[0] = 1.0 / 60
    res = _opt(load, [0.40] * 6 + [0.02] * 8 + [0.25] * 82, 31.3, cfg=cfg, water_value=0.12, dt_h=0.25)
    assert res["wall_schedule"][0]
    assert res["dip_schedule"][:6] == [False] + [True] * 5


@_HOURS
def test_flat_price_start_under_reserve_refills(dt_h):
    n = round(12 / dt_h)
    _, path = _both([0.4 * dt_h] * n, [0.25] * n, 12.0, dt_h)
    assert path[-1] >= 30.0 - 1e-9


# Standby alone drains exactly half a 0.05 kWh bin per 15 min; round-half-even parks the pack free on an even
# bin above the reserve, so the DP charges up to one. origin/main plans the same charge.
_HALF_BIN_PARK = pytest.mark.xfail(strict=True, reason="half-bin standby drain parks on an even bin")


@pytest.mark.parametrize(
    ("cap", "reserve", "soc_start", "dt_h", "load_kw"),
    [
        pytest.param(cap, reserve, soc, dt_h, load_kw, marks=_HALF_BIN_PARK if (dt_h, load_kw) == (0.25, 0.0) else ())
        for cap, reserve, soc in ((10.0, 30.0, 29.0), (20.48, 25.0, 25.0), (20.48, 25.0, 24.0))
        for dt_h in (1.0, 0.25)
        for load_kw in (0.0, 0.4)
    ],
)
def test_one_point_under_reserve_is_not_topped_up(cap, reserve, soc_start, dt_h, load_kw):
    # 25 % of 20.48 kWh bins to 5.10, under the 5.15 snapped reserve; 24 % is the integer reading a point under.
    cfg = Config(capacity_kwh=cap, backup_reserve_soc=reserve, idle_drain_w=100.0)
    n = round(12 / dt_h)
    res, _ = _both([load_kw * dt_h] * n, [0.25] * n, soc_start, dt_h, cfg=cfg)
    assert res["schedule"][0] == 0.0


@_HOURS
def test_export_stops_at_reserve_while_house_goes_under(dt_h):
    price = _q([0.20, 0.20, 0.45, 0.45, 0.20, 0.05, 0.05, 0.20, 0.20, 0.20, 0.20, 0.20], dt_h)
    export_price = [p - 0.02 for p in price]
    res, path = _both(_q([0.5 * dt_h] * 12, dt_h), price, 60.0, dt_h, export_price=export_price)
    exported = [h for h, e in enumerate(res["export_schedule"]) if e > 0.0]
    assert exported
    assert min(path[h] for h in exported) >= 30.0 - 1e-9
    assert min(path) < 30.0
