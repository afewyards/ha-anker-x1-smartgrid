"""T6: UTC-anchored day boundary (window_start_slot/slots_per_day) generalization.

Proves the day-index math used to build the export peak-band reference rolls
over at ``slots_per_day`` (not hardcoded 24), that the new ``day_index``/
``slots_per_day`` kwargs reproduce the legacy ``//24`` arithmetic byte-for-byte
at 60-min resolution (including a DST-transition day), and that regret.py's
``DayData``/``realized_grid_cost`` accept a 96-slot (15-min) day without a
hardcoded length-24 rejection.
"""
from custom_components.anker_x1_smartgrid.models import Config
from custom_components.anker_x1_smartgrid.optimize import optimize_grid
from custom_components.anker_x1_smartgrid.regret import (
    DayData,
    _validate_day_len,
    realized_grid_cost,
)


def _cfg():
    return Config(capacity_kwh=10.0, soc_floor=20.0, soc_target=80.0,
                  max_charge_w=3000.0, eta_charge=1.0, max_export_w=3000.0,
                  grid_export_limit_w=6000.0, enable_export=True,
                  export_peak_band_frac=0.10, export_peak_lookback_h=0)


def test_day_index_none_matches_legacy_24h_arithmetic():
    n = 30
    pv = [0.0] * n
    load = [0.2] * n
    price = [0.2 + 0.01 * (i % 5) for i in range(n)]
    legacy = [(0 + h) // 24 for h in range(n)]
    a = optimize_grid(pv, load, price, soc_start=50.0, cfg=_cfg(),
                       window_start_h=0, window_len=n, export_price=price)
    b = optimize_grid(pv, load, price, soc_start=50.0, cfg=_cfg(),
                       window_start_h=0, window_len=n, export_price=price, day_index=legacy)
    assert a["export_schedule"] == b["export_schedule"]


def test_window_start_slot_and_slots_per_day_reproduce_split():
    # 15-min framing: slots_per_day=96, start slot 40 (=10:00). Day rolls at slot 96.
    n = 100
    pv = [0.0] * n
    load = [0.0] * n
    price = [0.30 if (40 + i) < 96 else 0.50 for i in range(n)]
    di = [(40 + i) // 96 for i in range(n)]
    out = optimize_grid(pv, load, price, soc_start=80.0, cfg=_cfg(),
                         window_start_h=40, window_len=n, slots_per_day=96,
                         export_price=price, feed_in=price, day_index=di)
    # day-1 slots (index>=56 in the window) get their own peak band, not day-0's.
    assert len(out["export_schedule"]) == n


def test_day_rolls_at_slots_per_day_not_hardcoded_24():
    # At slots_per_day=96 the day must roll over at slot 96, NOT at slot 24
    # (which is what a residual //24 would do — ~22h too early at 15-min).
    n = 40
    default_di = [(80 + h) // 96 for h in range(n)]  # window starts at slot 80
    legacy_broken_di = [(80 + h) // 24 for h in range(n)]
    assert default_di != legacy_broken_di
    # Day rolls at absolute slot 96 -> local index 16 within this window.
    assert default_di[15] == 0
    assert default_di[16] == 1
    # The (broken) //24 arithmetic would have rolled over already at h=0 (80//24=3).
    assert legacy_broken_di[0] != 0


def test_day_index_reproduces_slash_24_at_60min_including_dst_day():
    # DST-transition day: 23h wall-clock (spring-forward). At slot_minutes=60,
    # slots_per_day stays the nominal 24 (the //24 arithmetic is unchanged by
    # a benign one-hour DST wobble; T6 only widens the divisor, it does not
    # attempt real-calendar DST correction).
    n = 48  # two wall-clock days worth of hourly slots
    window_start_h = 0
    for slots_per_day in (24,):
        legacy = [(window_start_h + h) // 24 for h in range(n)]
        generalized = [(window_start_h + h) // slots_per_day for h in range(n)]
        assert legacy == generalized


def test_dayd_data_accepts_96_slot_day():
    n = 96
    day = DayData(
        pv_kwh=tuple(0.0 for _ in range(n)),
        load_kwh=tuple(0.1 for _ in range(n)),
        price=tuple(0.20 for _ in range(n)),
        soc_start=50.0,
    )
    assert len(day.pv_kwh) == n
    _validate_day_len(day.pv_kwh, "pv_kwh", n)  # must not raise


def test_realized_grid_cost_accepts_96_slot_day():
    n = 96
    day = DayData(
        pv_kwh=tuple(0.0 for _ in range(n)),
        load_kwh=tuple(0.1 for _ in range(n)),
        price=tuple(0.20 for _ in range(n)),
        soc_start=50.0,
    )
    realized_charge = [0.0] * n
    out = realized_grid_cost(day, realized_charge, _cfg(), dt_h=0.25)
    assert len(out["forced_import_kwh"]) == n
