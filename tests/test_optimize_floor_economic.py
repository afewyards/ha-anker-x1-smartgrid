"""Economic-only floor behaviour (Task A1).

The DP must NO LONGER force-charge the battery from the grid purely to hold the
firmware SoC floor.  Instead it rides the pack down to the floor and serves any
below-floor house load by direct grid->load import (priced 1:1, no eta, no fee —
matching ``regret.realized_grid_cost``).  Grid CHARGING happens only when
arbitrage pays.

Scenario constants for ``test_no_morning_force_charge_rides_to_floor`` are mined
from ``scratchpad/decisive.py`` (the proof-of-concept that isolated the bug).
That script demonstrates, by toggling the firmware floor off (soc_floor≈0), that
at a P80/P50 drain ratio of 1.6 the ONLY morning grid buy in today's code is the
floor-survival charge: ``B full`` morning(05-10) = 54 W vs ``B FLOOR off`` = 0 W.
The economic-only fix must drive that morning charge to exactly zero while still
charging at the cheap midday trough.
"""

from __future__ import annotations

import pytest

from custom_components.anker_x1_smartgrid import const
from custom_components.anker_x1_smartgrid.models import Config
from custom_components.anker_x1_smartgrid.optimize import (
    build_charge_mask,
    compute_water_value,
    effective_export_price,
    optimize_grid,
    solar_reservation_ceiling,
)
from custom_components.anker_x1_smartgrid.regret import _apply_solar_load


# ---------------------------------------------------------------------------
# Fixtures mined from scratchpad/decisive.py (copied, not imported)
# ---------------------------------------------------------------------------

# (hour, all-in price, pv_w, load50_w) — a real forecast window 05:00..21:00.
_TABLE = [
    (5, 0.263, 134, 773),
    (6, 0.259, 204, 383),
    (7, 0.222, 287, 312),
    (8, 0.174, 400, 257),
    (9, 0.146, 651, 318),
    (10, 0.133, 233, 258),
    (11, 0.131, 554, 203),
    (12, 0.135, 1446, 344),
    (13, 0.146, 1655, 343),
    (14, 0.191, 1614, 401),
    (15, 0.250, 1404, 470),
    (16, 0.282, 1095, 467),
    (17, 0.341, 638, 550),
    (18, 0.412, 175, 451),
    (19, 0.419, 48, 418),
    (20, 0.348, 0, 397),
    (21, 0.310, 0, 278),
]
_N = len(_TABLE)
_PRICE = [r[1] for r in _TABLE]
_PV = [r[2] / 1000.0 for r in _TABLE]
_LOAD50 = [r[3] / 1000.0 for r in _TABLE]
_SOC_START = 22.0

_BASE = dict(
    capacity_kwh=10.0,
    soc_floor=5.0,
    soc_target=100.0,
    eta_charge=0.92,
    max_charge_w=6000.0,
    max_export_w=6000.0,
    grid_export_limit_w=6000.0,
    enable_export=True,
    export_fee_eur_per_kwh=0.02,
    export_peak_band_frac=0.12,
    round_trip_eff=0.85,
    cycle_cost_eur_per_kwh=0.04,
    water_value_factor=1.0,
    clamp_water_value_nonneg=True,
)


def test_no_morning_force_charge_rides_to_floor():
    """P80 drain: no morning survival charge; the DP charges only at the trough.

    soc_start=22%, P80 load = P50 x 1.6 (deficit 0.084 kWh — fully servable at the
    cheap 11:00 trough), evening peak 0.419, water-value terminal mode, realistic
    per-hour reserve and solar-reservation ceiling.

    At this drain ratio the deficit is tiny, so the trough alone covers it; ANY
    pre-trough grid buy can therefore only be a floor-survival charge.  Today's
    line-571 floor prune forces exactly such a buy (54 W at 07:00 in decisive.py),
    so this test FAILS before the fix.  After the economic-only fix the morning is
    served (where needed) by direct grid->load import (not a battery charge), so
    the grid CHARGE schedule before the trough is zero while the trough still
    charges.

    Window starts at 05:00, so schedule index i maps to hour 5+i; the trough is at
    index 6 (11:00, price 0.131).
    """
    cfg = Config.from_dict(_BASE)
    # P80 drain = P50 x 1.6, clamped to the 6 kW rate ceiling (as in decisive.py).
    load = [min(p50 * 1.6, 6.0) for p50 in _LOAD50]

    ceiling_price = max(_PRICE) * cfg.round_trip_eff
    chargeable = build_charge_mask(_PRICE, ceiling_price)
    export_price = [effective_export_price(p, cfg) for p in _PRICE]
    feed_in = list(export_price)
    wv = compute_water_value(min(_PRICE), cfg)
    # Ceiling derived from the P50 load (matches the live config in decisive.py).
    gcc = solar_reservation_ceiling(_PV, _LOAD50, cfg, cycle_end_idx=[_N] * _N)
    # Realistic per-hour ride-out reserve: simple decaying remaining-load proxy.
    reserve = _reserve_by_hour(load, cfg)

    res = optimize_grid(
        _PV,
        load,
        _PRICE,
        soc_start=_SOC_START,
        cfg=cfg,
        window_start_h=5,
        window_len=_N,
        chargeable=chargeable,
        feed_in=feed_in,
        export_price=export_price,
        terminal_mode="water_value",
        water_value=wv,
        reserve_by_hour=reserve,
        grid_charge_ceiling=gcc,
    )

    trough_idx = _PRICE.index(min(_PRICE))  # == 6 (11:00, 0.131)
    schedule = res["schedule"]

    # No grid CHARGE before the cheap trough (the retired floor-survival buy).
    assert sum(schedule[:trough_idx]) == pytest.approx(0.0, abs=1e-9), (
        f"morning grid charge should be zero, got {schedule[:trough_idx]}"
    )
    # The DP still charges at the cheap trough (arbitrage pays).
    assert schedule[trough_idx] > 0.0, "expected a grid charge at the cheap trough"


def test_below_floor_drain_priced_as_load_import():
    """All-expensive drain crossing the floor: served by priced direct import.

    capacity 10 kWh, soc_floor 20% (2 kWh, a pure DECISION margin — see D1:
    passive-drain-to-firmware-floor), no PV, flat expensive price, water-value
    terminal mode with water_value=0 so there is no incentive to charge.  The
    pack rides all the way down to the FIRMWARE floor (const.FIRMWARE_SOC_FLOOR,
    5% = 0.5 kWh) — not the soft cfg.soc_floor margin — and the below-firmware-
    floor load is met by direct grid->load import.

    eta_charge=0.92 (the live value): charging the battery to serve future load
    costs price/eta per delivered kWh, which is STRICTLY more than direct
    grid->load import (1:1, no eta).  So the DP never charges ahead — it rides to
    the firmware floor with a zero schedule and imports the deficit directly.
    (At eta=1.0 the two are exactly tied and the DP may pick an equal-cost
    charge-ahead path; the eur is identical but the schedule is no longer
    all-zero.)

    SoC stays on 0.05 kWh bins (no PV, integer-kWh load / firmware floor), so
    the import arithmetic is still exact despite eta != 1.

    Expected (soc_start=3 kWh, load 1 kWh/h, 6 h, firmware floor=0.5 kWh):
      h0: 3   -> 2                     import 0   (above both floors)
      h1: 2   -> 1                     import 0   (above the firmware floor)
      h2: 1   -> 0   -> clamp 0.5      import 0.5 kWh
      h3..h5: 0.5 -> -0.5 -> clamp 0.5 import 1.0 kWh each (3 hours)
    -> kwh = 0.5 + 3x1.0 = 3.5 ; eur = (0.5 + 3.0) x 0.50 = 1.75 ; schedule
    all-zero (no force-charge to hold either floor).
    """
    cfg = Config(
        capacity_kwh=10.0,
        soc_floor=20.0,
        soc_target=80.0,
        max_charge_w=3000.0,
        eta_charge=0.92,
    )
    n = 6
    pv = [0.0] * n
    load = [1.0] * n
    price = [0.50] * n
    soc_start = 30.0  # 3 kWh

    res = optimize_grid(
        pv,
        load,
        price,
        soc_start=soc_start,
        cfg=cfg,
        window_start_h=0,
        window_len=n,
        terminal_mode="water_value",
        water_value=0.0,
    )

    # Independently compute the expected below-firmware-floor direct-import cost.
    firmware_floor_kwh = const.FIRMWARE_SOC_FLOOR / 100.0 * cfg.capacity_kwh  # 0.5
    soc = soc_start / 100.0 * cfg.capacity_kwh
    expected_eur = 0.0
    for h in range(n):
        soc_after = _apply_solar_load(soc, pv[h] - load[h], cfg)
        expected_eur += max(0.0, firmware_floor_kwh - soc_after) * price[h]
        soc = max(soc_after, firmware_floor_kwh)

    assert sum(res["schedule"]) == pytest.approx(0.0, abs=1e-9), (
        f"DP must not force-charge to hold the floor, got {res['schedule']}"
    )
    assert res["eur"] == pytest.approx(expected_eur, abs=1e-6)
    assert expected_eur == pytest.approx(1.75, abs=1e-9)  # sanity on the fixture
    assert res["kwh"] == pytest.approx(3.5, abs=1e-9)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reserve_by_hour(load: list[float], cfg: Config) -> list[float]:
    """Decaying remaining-load ride-out reserve (DC kWh), floored at the firmware floor.

    A self-contained stand-in for ``energy.ride_out_reserve_kwh`` good enough to
    exercise the export floor: reserve[h] = max(firmware_floor, remaining load
    after h, capped at capacity).  Pure/deterministic so the DP is reproducible.
    """
    floor_kwh = cfg.soc_floor / 100.0 * cfg.capacity_kwh
    n = len(load)
    out: list[float] = []
    for h in range(n):
        remaining = sum(load[h + 1 :])
        out.append(min(cfg.capacity_kwh, max(floor_kwh, remaining)))
    return out
