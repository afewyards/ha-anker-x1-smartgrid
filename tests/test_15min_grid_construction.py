"""T4: DP window is built on the slot grid (15-min resolvable, 60-min byte-identical)."""

from datetime import datetime, timedelta, timezone, UTC

from custom_components.anker_x1_smartgrid import controller as ctrl
from custom_components.anker_x1_smartgrid.models import (
    Config,
    ForecastInterval,
    PlantInputs,
    PriceSlot,
)

UTC = UTC
NOW = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)


def _cfg():
    return Config(
        capacity_kwh=10.0,
        soc_floor=20.0,
        soc_target=80.0,
        max_charge_w=3000.0,
        eta_charge=1.0,
        charge_window_price_band=1.0,
    )


def test_cheapest_quarter_is_individually_resolvable_not_hour_collapsed():
    prices = [0.30, 0.10, 0.30, 0.30, 0.30, 0.30, 0.30, 0.30]  # cheap quarter at 10:15
    slots = [PriceSlot(NOW + timedelta(minutes=15 * i), p) for i, p in enumerate(prices)]
    ivs = [ForecastInterval(NOW + timedelta(minutes=15 * i), 0.0, 400.0, 0.25) for i in range(8)]
    inputs = PlantInputs(soc=50.0, meter_w=0.0, now=NOW)
    sel, grid, infeasible, exp, rev, ceil = ctrl._dp_select_slots(
        inputs=inputs,
        slots=slots,
        deadline=NOW + timedelta(hours=2),
        ceiling=0.20,
        cfg=_cfg(),
        export_price=None,
        intervals=ivs,
        slot_minutes=15,
        dt_h=0.25,
    )
    assert (NOW + timedelta(minutes=15)) in grid  # cheap quarter selected
    assert (NOW + timedelta(minutes=30)) not in grid  # expensive quarter not
    # every DP grid key lands on the 15-min slot grid (no hour-collapse)
    assert all(k.minute in (0, 15, 30, 45) for k in grid)


def test_live_dp_path_uses_slot_scaled_dawn_boundary(monkeypatch):
    # T5 wiring: _dp_select_slots must thread slot_minutes into solar_cycle_end_idx
    # so the dawn cycle boundary the ceiling receives is a SLOT count, not an hour
    # count.  Sunrise 4h ahead ⇒ correct boundary is slot 16 at 15-min, slot 4 at
    # 60-min.  We spy on the ceiling call the live DP makes and assert on the exact
    # cycle_end_idx array it hands in — a real end-to-end assertion (a stale hourly
    # boundary would put the switch at slot 4 in BOTH runs).
    now = datetime(2026, 8, 1, 2, 0, tzinfo=UTC)
    sun = (now + timedelta(hours=16), now + timedelta(hours=4), now + timedelta(hours=40))
    inputs = PlantInputs(soc=50.0, meter_w=0.0, now=now)

    captured: dict = {}
    real_ceiling = ctrl.optimize_mod.solar_reservation_ceiling

    def _spy(*args, **kwargs):
        captured["cycle_end_idx"] = kwargs.get("cycle_end_idx")
        return real_ceiling(*args, **kwargs)

    monkeypatch.setattr(ctrl.optimize_mod, "solar_reservation_ceiling", _spy)

    # 15-min run: 24 slots over a 6h window; sunrise at slot 16.
    slots15 = [PriceSlot(now + timedelta(minutes=15 * i), 0.10) for i in range(24)]
    ivs15 = [ForecastInterval(now + timedelta(minutes=15 * i), 0.0, 0.0, 0.25) for i in range(24)]
    ctrl._dp_select_slots(
        inputs=inputs,
        slots=slots15,
        deadline=now + timedelta(hours=6),
        ceiling=0.20,
        cfg=_cfg(),
        export_price=None,
        sun_times=sun,
        intervals=ivs15,
        slot_minutes=15,
        dt_h=0.25,
    )
    cyc15 = captured["cycle_end_idx"]
    assert cyc15[0] == 16  # 4h == 16 quarter-hour slots
    assert cyc15[15] == 16 and cyc15[16] == 24  # dawn switch lands AT slot 16

    # 60-min run: 6 slots over the same 6h window; sunrise at slot 4.
    slots60 = [PriceSlot(now + timedelta(hours=i), 0.10) for i in range(6)]
    ivs60 = [ForecastInterval(now + timedelta(hours=i), 0.0, 0.0, 1.0) for i in range(6)]
    ctrl._dp_select_slots(
        inputs=inputs,
        slots=slots60,
        deadline=now + timedelta(hours=6),
        ceiling=0.20,
        cfg=_cfg(),
        export_price=None,
        sun_times=sun,
        intervals=ivs60,
        slot_minutes=60,
        dt_h=1.0,
    )
    cyc60 = captured["cycle_end_idx"]
    assert cyc60[0] == 4  # legacy: 4 hourly slots
    assert cyc60[3] == 4 and cyc60[4] == 6  # dawn switch lands AT slot 4


def test_dp_window_is_byte_identical_at_slot_minutes_60():
    prices = [0.30, 0.10, 0.30, 0.30]
    slots = [PriceSlot(NOW + timedelta(hours=i), p) for i, p in enumerate(prices)]
    ivs = [ForecastInterval(NOW + timedelta(hours=i), 0.0, 400.0, 1.0) for i in range(4)]
    inputs = PlantInputs(soc=50.0, meter_w=0.0, now=NOW)
    common = dict(
        inputs=inputs,
        slots=slots,
        deadline=NOW + timedelta(hours=4),
        ceiling=0.20,
        cfg=_cfg(),
        export_price=None,
        intervals=ivs,
    )
    default = ctrl._dp_select_slots(**common)
    explicit = ctrl._dp_select_slots(**common, slot_minutes=60, dt_h=1.0)
    # passing slot_minutes=60 / dt_h=1.0 is byte-identical to the defaults
    assert default == explicit
    _, grid, *_ = explicit
    # at 60 the stride is a clock hour: every grid key is hour-aligned
    assert all(k.minute == 0 for k in grid)
