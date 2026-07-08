"""Forward-looking solar-reservation grid-charge cap.

Per-hour ceiling reserves headroom for the CURRENT day's remaining forecast
solar, so grid charge never fills capacity that free solar will reach.  No
survival lower-bound: insufficient solar -> ride to firmware floor (grid serves
load), never a pre-emptive ride-out charge.
"""
from datetime import datetime, timezone

import pytest

from custom_components.anker_x1_smartgrid.models import Config
from custom_components.anker_x1_smartgrid.optimize import (
    optimize_grid,
    solar_cycle_end_idx,
    solar_reservation_ceiling,
)


def _cfg(**kw) -> Config:
    d = dict(capacity_kwh=10.0, soc_floor=20.0, soc_target=80.0,
             max_charge_w=3000.0, eta_charge=1.0)
    d.update(kw)
    return Config(**d)


def test_ceiling_current_cycle_only_two_day():
    """(a) midday reserves TODAY's remaining solar; (b) evening==target despite
    TOMORROW's solar in the array (tomorrow = new cycle)."""
    cfg = _cfg()  # floor=2, target=8, rate=3, eta=1
    #  idx:  0  1  2  3  4  5  6  7  8   9  10 11
    #        today afternoon solar @2,3 | overnight | tomorrow solar @9,10
    pv   = [0, 0, 3, 3, 0, 0, 0, 0, 0,  3, 3, 0]
    load = [0] * 12
    # next-sunrise boundary at idx 9: today hours 0..8 -> 9; tomorrow hours 9..11 -> 12
    cycle_end_idx = [9, 9, 9, 9, 9, 9, 9, 9, 9, 12, 12, 12]
    c = solar_reservation_ceiling(pv, load, cfg, cycle_end_idx=cycle_end_idx)
    # (a) midday idx0 reserves today's afternoon solar (idx2,3 = 6 DC):
    assert c[0] == pytest.approx(2.0)   # 8 - 6
    assert c[2] == pytest.approx(5.0)   # only idx3 (3 DC) ahead within cycle
    # (b) evening idx5/idx8 reserve nothing despite tomorrow's solar:
    assert c[5] == pytest.approx(8.0)
    assert c[8] == pytest.approx(8.0)
    # tomorrow's hours reserve tomorrow's remaining solar (current cycle = tomorrow):
    assert c[9] == pytest.approx(5.0)   # 8 - idx10(3)
    assert c[11] == pytest.approx(8.0)


def test_ceiling_cloud_dip_does_not_end_cycle():
    """A midday net<=0 dip contributes 0 but does NOT terminate the cycle sum."""
    cfg = _cfg()
    pv   = [0, 3, 0, 3, 0, 0]   # solar @1 and @3, a dip at idx2
    load = [0] * 6
    c = solar_reservation_ceiling(pv, load, cfg)  # cumulative (single cycle)
    # future_solar_dc[0] = idx1(3) + idx3(3) = 6  (dip at idx2 not a boundary)
    assert c[0] == pytest.approx(2.0)


def test_solar_cycle_end_idx_from_sun_times():
    now_h = datetime(2026, 6, 27, 14, 0, tzinfo=timezone.utc)
    sun_times = (
        datetime(2026, 6, 27, 19, 0, tzinfo=timezone.utc),  # today_sunset
        datetime(2026, 6, 28, 4, 0, tzinfo=timezone.utc),   # tomorrow_sunrise = +14h
        datetime(2026, 6, 28, 19, 0, tzinfo=timezone.utc),  # tomorrow_sunset
    )
    idx = solar_cycle_end_idx(now_h, window_len=20, sun_times=sun_times)
    assert idx[0] == 14 and idx[13] == 14   # today hours -> tomorrow sunrise
    assert idx[14] == 20 and idx[19] == 20  # tomorrow hours -> window end (no day-after)
    # No sun info -> single-cycle/cumulative fallback.
    assert solar_cycle_end_idx(now_h, 6, None) == [6] * 6


def test_a_no_pre_solar_overcharge():
    """(a) Economic incentive to fill battery (cheap price < water_value) is
    blocked by the ceiling when afternoon solar will fill the pack instead.

    Without solar the DP correctly pre-charges in cheap hours (price=0.10 <
    water_value=0.20 → profitable).  With the same cheap hours but solar
    forecast arriving at h=2,3, the ceiling zeros the pre-solar allowance
    (future_solar_dc=6 ≥ target−soc_after=5 → ceiling=2 ≤ soc_after=3 →
    max_grid_dc=0), so no grid charge occurs.  The globally optimal DP also
    returns 0 in the solar case without ceiling (it sees the solar for free),
    confirming the ceiling merely makes explicit a constraint that would be
    satisfied optimally but is now enforced as a hard bound.
    """
    cfg = _cfg()
    pv    = [0.0, 0.0, 3.0, 3.0, 0.0, 0.0]
    load  = [0.0] * 6
    price = [0.10, 0.10, 0.30, 0.30, 0.30, 0.30]  # cheap pre-solar
    common = dict(soc_start=30.0, cfg=cfg, window_start_h=0, window_len=6,
                  terminal_mode="water_value", water_value=0.20)
    # Without solar: economic incentive exists → DP fills in cheap hours.
    base_no_solar = optimize_grid([0.0] * 6, load, price, **common)
    assert sum(base_no_solar["schedule"]) > 4.0 and base_no_solar["schedule"][0] > 0.0
    # With solar + ceiling: ceiling = target − future_solar_dc = 2 kWh ≤ soc_after = 3 kWh
    # → max_grid_dc = 0 at pre-solar hours → no grid charge.
    ceil = solar_reservation_ceiling(pv, load, cfg)          # single-cycle window
    res = optimize_grid(pv, load, price, grid_charge_ceiling=ceil, **common)
    assert sum(res["schedule"]) == pytest.approx(0.0, abs=0.1)
    assert res["schedule"][0] == pytest.approx(0.0, abs=1e-6)
    assert res["schedule"][1] == pytest.approx(0.0, abs=1e-6)


def test_b_solar_not_curtailed():
    """(b) Solar alone fills to target with zero grid charge (no displacement)."""
    cfg = _cfg()
    pv    = [0.0, 0.0, 3.0, 3.0, 0.0, 0.0]
    load  = [0.0] * 6
    price = [0.10, 0.10, 0.30, 0.30, 0.30, 0.30]
    ceil = solar_reservation_ceiling(pv, load, cfg)
    res = optimize_grid(pv, load, price, soc_start=30.0, cfg=cfg,
                        window_start_h=0, window_len=6,
                        terminal_mode="water_value", water_value=0.20,
                        grid_charge_ceiling=ceil)
    assert sum(res["schedule"]) == pytest.approx(0.0, abs=0.1)


def test_no_future_solar_ceiling_is_target():
    """No future solar -> ceiling==target -> ECONOMIC export top-up unrestricted
    (justified by a profitable export, NOT by survival)."""
    cfg = _cfg(round_trip_eff=1.0, cycle_cost_eur_per_kwh=0.04,
               max_export_w=3000.0, grid_export_limit_w=3000.0)
    n = 4
    pv    = [0.0] * 4
    load  = [0.0] * 4
    price = [0.10, 0.10, 0.30, 0.30]
    export_price = [0.0, 0.0, 0.60, 0.0]      # peak at idx2
    ceil = solar_reservation_ceiling(pv, load, cfg)
    assert all(c == pytest.approx(8.0) for c in ceil)
    res = optimize_grid(pv, load, price, soc_start=30.0, cfg=cfg,
                        window_start_h=0, window_len=n,
                        export_price=export_price, grid_charge_ceiling=ceil,
                        terminal_mode="water_value", water_value=0.0)
    assert res["schedule"][0] + res["schedule"][1] > 0.0   # grid top-up to target...
    assert res["export_schedule"][2] > 0.0                  # ...sold at the peak


def test_no_survival_grid_charge_on_no_export_night():
    """Removing the survival lower-bound introduces NO survival charge: with no
    export incentive the DP rides toward the floor and does not grid-charge."""
    cfg = _cfg()
    pv    = [0.0] * 6
    load  = [0.5] * 6
    price = [0.20] * 6
    ceil = solar_reservation_ceiling(pv, load, cfg)
    res = optimize_grid(pv, load, price, soc_start=80.0, cfg=cfg,   # high start: no floor breach
                        window_start_h=0, window_len=6,
                        terminal_mode="water_value", water_value=0.0,
                        grid_charge_ceiling=ceil)
    assert sum(res["schedule"]) == pytest.approx(0.0, abs=1e-6)


def test_d_export_topup_to_max_after_solar_done():
    """(d) After the current cycle's solar finishes, ceiling==target: full grid
    inventory build for a peak export."""
    cfg = _cfg(round_trip_eff=1.0, cycle_cost_eur_per_kwh=0.04,
               max_export_w=3000.0, grid_export_limit_w=3000.0)
    n = 8
    pv    = [1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]   # small solar @0,1
    load  = [0.0] * 8
    price = [0.30, 0.30, 0.30, 0.30, 0.10, 0.10, 0.30, 0.30]   # cheap post-solar 4,5
    export_price = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.60, 0.60]  # peak @6,7
    ceil = solar_reservation_ceiling(pv, load, cfg)
    assert ceil[4] == pytest.approx(8.0) and ceil[5] == pytest.approx(8.0)
    res = optimize_grid(pv, load, price, soc_start=30.0, cfg=cfg,
                        window_start_h=0, window_len=n,
                        export_price=export_price, grid_charge_ceiling=ceil,
                        terminal_mode="water_value", water_value=0.0)
    assert res["schedule"][4] + res["schedule"][5] > 0.0   # post-solar grid inventory
    assert res["export_schedule"][6] > 0.0                  # exported at the peak


def test_ceiling_prevents_afternoon_solar_spill_live_tariff():
    """Live-representative scenario: spiky NL tariff + salderen export + peak-band gate.

    Config: 5 kWh battery, 10% SoC start (0.5 kWh), target 97% (4.85 kWh).

    Window (8 h):
      h=0,1  cheap import 0.08 €, no solar
      h=2,3,4 afternoon solar 3 kWh/h; import 0.20, export BLOCKED (< band_floor=0.264)
      h=5    normal 0.20, no solar
      h=6,7  evening peak 0.30, export ADMITTED (0.30 ≥ 0.264); no solar

    peak_from=0.30, export_peak_band_frac=0.12 → band_floor=0.264.

    Hypothesis (reviewer): BASE grid-charges midday AND spills afternoon solar;
    CEILING does neither and total value ≥ BASE.

    FINDING — Hypothesis FALSE for globally optimal DP with full solar forecast:

    The DP avoids pre-charging when afternoon solar delivers equivalent export
    inventory for free (same terminal SoC → same export revenue; any pre-charge
    only adds import cost). Both BASE (no ceiling) and CEILING produce the same
    schedule: 0 kWh grid charge. Solar spill arises purely from rate-limited
    absorption when the battery hits target in h=3,4 — identical in both cases.

    Ceiling values at h=0,1: 0.25 kWh (floor, from max(floor, 4.85−9.0)).
    Since ceiling ≤ soc_after (0.25 ≤ 0.5), the clamp sets max_grid_dc=0 — the
    ceiling IS mechanically binding, but the optimal DP would also choose 0.

    Representative numbers (eta=1.0, rt=1.0):
      BASE:    grid_charge=0.00 kWh, solar_spill≈4.65 kWh, eur≈−1.196
      CEILING: grid_charge=0.00 kWh, solar_spill≈4.65 kWh, eur≈−1.196

    The ceiling's real-world value lies in non-DP-optimal scenarios:
      1. Force-charge paths (live HAOS pre-economic-only-branch) bypass the DP
         and fill to target ahead of solar — ceiling blocks this at the executor.
      2. Forecast-miss: if window_pv is stale/zero the DP overcharges; ceiling
         (from a better solar source) prevents the fill-to-target.
    In both cases the ceiling acts as a hard bound external to DP optimisation.
    """
    from custom_components.anker_x1_smartgrid.regret import _apply_solar_load

    def _replay_spill(
        schedule: list[float],
        export_sched: list[float],
        pv: list[float],
        load: list[float],
        soc_start_pct: float,
        cfg_: Config,
    ) -> float:
        """Solar curtailment (AC kWh) from an SoC-trajectory replay."""
        cap = cfg_.capacity_kwh
        eta = cfg_.eta_charge if cfg_.eta_charge > 1e-9 else 1.0
        eta_d = min(cfg_.round_trip_eff / eta, 1.0)
        target_kwh = cfg_.soc_target / 100.0 * cap
        floor_kwh = cfg_.soc_floor / 100.0 * cap
        soc = soc_start_pct / 100.0 * cap
        spill = 0.0
        for h in range(len(pv)):
            net_ac = pv[h] - load[h]
            soc_prev = soc
            soc = _apply_solar_load(soc, net_ac, cfg_)
            if net_ac > 0:
                dc_stored = soc - soc_prev
                spill += max(0.0, net_ac - dc_stored / eta)
            g_ac = schedule[h] if h < len(schedule) else 0.0
            if g_ac > 0:
                g_dc = min(g_ac * eta, target_kwh - soc)
                soc = min(soc + max(0.0, g_dc), target_kwh)
            if h < len(export_sched):
                e_ac = export_sched[h]
                if e_ac > 0:
                    e_dc = e_ac / eta_d if eta_d > 1e-9 else e_ac
                    soc = max(floor_kwh, soc - e_dc)
        return spill

    cfg = _cfg(
        capacity_kwh=5.0,
        soc_floor=5.0,            # 0.25 kWh
        soc_target=97.0,          # 4.85 kWh
        max_charge_w=3000.0,
        eta_charge=1.0,
        round_trip_eff=1.0,
        cycle_cost_eur_per_kwh=0.04,
        max_export_w=3000.0,
        grid_export_limit_w=3000.0,
        export_peak_band_frac=0.12,
    )
    pv           = [0.0, 0.0, 3.0, 3.0, 3.0, 0.0, 0.0, 0.0]
    load         = [0.0] * 8
    price        = [0.08, 0.08, 0.20, 0.20, 0.20, 0.20, 0.30, 0.30]
    export_price = [0.08, 0.08, 0.20, 0.20, 0.20, 0.20, 0.30, 0.30]  # salderen

    common: dict = dict(
        soc_start=10.0, cfg=cfg,
        window_start_h=0, window_len=8,
        terminal_mode="water_value", water_value=0.0,
        export_price=export_price,
    )

    # --- BASE: globally optimal DP with full solar forecast, no ceiling ---
    base = optimize_grid(pv, load, price, **common)
    base_e = base.get("export_schedule", [0.0] * 8)
    base_grid_kwh = sum(base["schedule"])
    base_spill_kwh = _replay_spill(base["schedule"], base_e, pv, load, 10.0, cfg)
    base_eur = base["eur"]

    # --- CEILING: same DP inputs + solar-reservation ceiling ---
    ceil = solar_reservation_ceiling(pv, load, cfg)
    c_res = optimize_grid(pv, load, price, grid_charge_ceiling=ceil, **common)
    c_e = c_res.get("export_schedule", [0.0] * 8)
    ceil_grid_kwh = sum(c_res["schedule"])
    ceil_spill_kwh = _replay_spill(c_res["schedule"], c_e, pv, load, 10.0, cfg)
    ceil_eur = c_res["eur"]

    # Ceiling mechanism correct: h=0,1 clamped to floor (9 DC kWh solar >> 4.35 gap)
    assert ceil[0] == pytest.approx(0.25)   # max(0.25, 4.85 - 9.0)
    assert ceil[1] == pytest.approx(0.25)
    assert ceil[5] == pytest.approx(4.85)   # no future solar → ceiling=target
    assert ceil[6] == pytest.approx(4.85)

    # Both produce 0 grid charge — hypothesis FALSE for optimal DP
    assert base_grid_kwh == pytest.approx(0.0, abs=0.1), (
        f"BASE grid_kwh={base_grid_kwh:.3f}: DP pre-charged — investigate scenario"
    )
    assert ceil_grid_kwh == pytest.approx(0.0, abs=0.1), (
        f"CEILING grid_kwh={ceil_grid_kwh:.3f}"
    )

    # Solar spill identical: from rate-limited absorption, not prior overcharging
    assert abs(ceil_spill_kwh - base_spill_kwh) < 0.05, (
        f"base_spill={base_spill_kwh:.2f} ceil_spill={ceil_spill_kwh:.2f}"
    )

    # Ceiling never increases cost vs base (equal here: neither pre-charges)
    assert ceil_eur <= base_eur + 1e-3, (
        f"Ceiling raised cost: base_eur={base_eur:.3f} ceil_eur={ceil_eur:.3f}"
    )


# ---------------------------------------------------------------------------
# Ceiling ENFORCEMENT (not just the DP schedule): the executor charge-stop and
# the display must both stop grid charging at the ceiling, never at soc_target —
# otherwise the pack fills from the grid ahead of the forecast solar surplus.
# ---------------------------------------------------------------------------

def test_decide_state_stops_charging_at_ceiling():
    """decide_state hard-stops grid charge at the per-hour ceiling, not soc_target."""
    from datetime import timedelta
    from custom_components.anker_x1_smartgrid.models import PlanState, ControllerState
    from custom_components.anker_x1_smartgrid import scheduler

    cfg = Config(capacity_kwh=10.0, soc_floor=5.0, soc_target=97.0,
                 max_charge_w=6000.0, eta_charge=0.92, min_dwell_min=0)
    now = datetime(2026, 6, 27, 11, tzinfo=timezone.utc)
    plan = PlanState(ControllerState.FORCING, now - timedelta(hours=1), (now,))

    # soc 91% < soc_target-1 (96%): without a ceiling the executor keeps charging.
    no_ceiling = scheduler.decide_state(
        plan, soc=91.0, now=now, selected_slots=[now], cfg=cfg,
    )
    assert no_ceiling.state is ControllerState.FORCING

    # With ceiling=91% and soc=91%, charging must STOP (room left for solar).
    with_ceiling = scheduler.decide_state(
        plan, soc=91.0, now=now, selected_slots=[now], cfg=cfg,
        charge_ceiling_soc=91.0,
    )
    assert with_ceiling.state is ControllerState.PASSIVE

    # Below the ceiling, charging continues.
    below = scheduler.decide_state(
        plan, soc=80.0, now=now, selected_slots=[now], cfg=cfg,
        charge_ceiling_soc=91.0,
    )
    assert below.state is ControllerState.FORCING


def test_build_plan_horizon_caps_grid_at_ceiling_then_solar_fills():
    """Projected SoC stops grid at the ceiling and ramps to target on solar surplus."""
    from datetime import timedelta
    from custom_components.anker_x1_smartgrid.models import PriceSlot, ForecastInterval
    from custom_components.anker_x1_smartgrid import plan as plan_mod

    cfg = Config(capacity_kwh=10.0, soc_floor=5.0, soc_target=97.0,
                 max_charge_w=6000.0, eta_charge=0.92)
    base = datetime(2026, 6, 27, 11, tzinfo=timezone.utc)
    pv = [636, 622, 587, 531, 457]
    load = [203, 344, 343, 401, 470]
    slots = [PriceSlot(base + timedelta(hours=i), 0.13) for i in range(5)]
    ivals = [ForecastInterval(base + timedelta(hours=i), pv[i], load[i], 1.0) for i in range(5)]
    selected = [base]                          # only 11:00 is a grid hour
    grid_req = {base: 6000.0}                   # DP wants to charge hard
    ceiling = {base + timedelta(hours=i): 91.0 for i in range(5)}

    no_ceil = plan_mod.build_plan_horizon(
        slots, ivals, selected, 55.0, base + timedelta(hours=5), cfg,
        grid_request_by_hour=grid_req,
    )
    with_ceil = plan_mod.build_plan_horizon(
        slots, ivals, selected, 55.0, base + timedelta(hours=5), cfg,
        grid_request_by_hour=grid_req, ceiling_by_hour=ceiling,
    )
    # Old behaviour: grid fills straight to ~target at 11:00, afternoon solar lost.
    assert no_ceil[0]["soc"] >= 96.0
    assert no_ceil[1]["solar_charge_w"] == 0.0
    # New behaviour: grid stops at the 91% ceiling at 11:00 ...
    assert with_ceil[0]["soc"] <= 91.5, with_ceil[0]["soc"]
    # ... and the afternoon surplus charges the pack (solar > 0, SoC ramps up).
    assert with_ceil[1]["solar_charge_w"] > 0.0
    assert with_ceil[3]["soc"] > with_ceil[0]["soc"]
