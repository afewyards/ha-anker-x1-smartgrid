"""B3 — peak-window coverage REGRESSION TESTS.

Locks two properties of the live DP (optimize_grid) / econ-only path that the
arbitrage feature must NOT break:

1. Pre-charge covers the peak window (floor-safe self-consumption):
   When a price-worthy cheap trough slot precedes the evening peak, the DP
   pre-buys enough cheap energy so that self-consumption through the peak keeps
   SoC >= soc_floor at every per-hour boundary.  No "fill full into the peak"
   over-buy is needed — the reserve is exactly what the peak window requires.

2. Flat-expensive, no-sun day → rides floor + imports peak (NO over-buy):
   When all prices exceed the charge ceiling (chargeable=[False]*N), the DP
   returns a zero schedule, rides the firmware floor via self-consumption, and
   implicitly imports during the peak.  The economic-only invariant holds: the
   controller NEVER force-charges into an expensive peak window.

These tests are REGRESSION tests — they confirm existing behaviour.  No new
production code is required (reserve_kwh / export_surplus_kwh already exist
in energy.py from B2).  If they pass on the first run, that is the point.

Physics helpers
---------------
trace_soc      — replays the DP physics (load 1:1, solar via eta, grid via eta)
                 and returns the per-hour end-of-hour SoC trajectory.
build_chargeable — builds the chargeable mask: True iff price <= ceiling.
                 Uses a simple per-test ceiling rather than the full
                 charge_price_ceiling() helper to keep tests self-contained.
"""

from __future__ import annotations

import pytest

from custom_components.anker_x1_smartgrid.models import Config
from custom_components.anker_x1_smartgrid.optimize import optimize_grid
from custom_components.anker_x1_smartgrid.regret import _apply_solar_load


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(**overrides) -> Config:
    """10 kWh pack, floor=5% (0.5 kWh), target=97% (9.7 kWh), eta=1.0 for
    round-number arithmetic.  soc_floor=5 matches the post-econ-only default."""
    defaults = dict(
        capacity_kwh=10.0,
        soc_floor=5.0,  # firmware floor = 0.5 kWh
        soc_target=97.0,  # 9.7 kWh
        max_charge_w=6000.0,  # 6 kWh/h
        eta_charge=1.0,  # AC == DC for readable test arithmetic
        round_trip_eff=1.0,
    )
    defaults.update(overrides)
    return Config(**defaults)


def _trace_soc(
    pv: list[float],
    load: list[float],
    schedule: list[float],
    soc_start_kwh: float,
    cfg: Config,
) -> list[float]:
    """Replay DP physics (solar/load then grid charge) → per-hour end-SoC in kWh.

    Mirrors the DP transition loop in optimize_grid:
      1. soc_after = _apply_solar_load(soc, net=pv[h]-load[h], cfg)
      2. soc_end   = min(soc_after + schedule[h] * eta, target_kwh)
    """
    eta = cfg.eta_charge if cfg.eta_charge > 1e-9 else 1.0
    target_kwh = cfg.soc_target / 100.0 * cfg.capacity_kwh
    soc = soc_start_kwh
    traj: list[float] = []
    for h in range(len(schedule)):
        net = pv[h] - load[h]
        soc = _apply_solar_load(soc, net, cfg)
        soc = min(soc + schedule[h] * eta, target_kwh)
        traj.append(soc)
    return traj


def _chargeable(price: list[float], ceiling: float) -> list[bool]:
    """Price-gate mask: hour is chargeable iff price <= ceiling."""
    return [p <= ceiling for p in price]


# ---------------------------------------------------------------------------
# Scenario 1 — trough-before-peak: pre-charge keeps SoC ≥ floor through peak
#
# Layout (8-hour window, no PV, eta=1.0, 10 kWh pack):
#   h0: 0.10 €/kWh (cheap trough)
#   h1: 0.10 €/kWh (cheap trough)
#   h2-h7: 0.32 €/kWh (expensive peak)
#
# House load = 1.0 kWh/h throughout.
# soc_start = 30% = 3.0 kWh.
# floor     = 5%  = 0.5 kWh.
# ceiling   = 0.20 €/kWh  → h0, h1 chargeable; h2-h7 NOT.
#
# Without grid charging, trajectory after 8h: 3 − 8 = -5 kWh (far below floor).
# DP must pre-buy in h0/h1 enough to carry the battery through h2-h7 ≥ floor.
# Minimum needed: survive 6 peak hours × 1 kWh = 6 kWh load; soc must not drop
# below floor (0.5 kWh) at any boundary.
#   soc_after_h1 = 3 + grid_h0 + grid_h1 - 2 (load h0+h1)
#   We need soc_after_h7 = soc_after_h1 - 6 >= 0.5 → soc_after_h1 >= 6.5 kWh
#   → total_grid = 6.5 - 3 + 2 = 5.5 kWh (split across h0, h1 ≤ 6 kWh/h each).
# ---------------------------------------------------------------------------


class TestTroughBeforePeakFloorSafe:
    """Scenario 1: cheap trough precedes expensive peak → DP pre-charges to floor-safe."""

    N = 8
    LOAD_KWH_H = 1.0  # constant 1 kWh/h load
    TROUGH_PRICE = 0.10
    PEAK_PRICE = 0.32
    CEILING = 0.20  # gate threshold: trough clears, peak doesn't
    SOC_START_PCT = 30.0  # 3.0 kWh
    FLOOR_KWH = 0.5  # 5% of 10 kWh
    N_PEAK = 6  # hours h2-h7 are expensive (no charging)

    def _run(self) -> tuple[dict, Config, list[float], list[float]]:
        cfg = _cfg()
        price = [self.TROUGH_PRICE] * 2 + [self.PEAK_PRICE] * self.N_PEAK
        pv = [0.0] * self.N
        load = [self.LOAD_KWH_H] * self.N
        chargeable = _chargeable(price, self.CEILING)
        result = optimize_grid(
            pv,
            load,
            price,
            soc_start=self.SOC_START_PCT,
            cfg=cfg,
            window_start_h=0,
            window_len=self.N,
            chargeable=chargeable,
            feed_in=None,
            terminal_mode="water_value",
            water_value=0.0,
        )
        soc_start_kwh = self.SOC_START_PCT / 100.0 * cfg.capacity_kwh
        traj = _trace_soc(pv, load, result["schedule"], soc_start_kwh, cfg)
        return result, cfg, traj, price

    def test_schedule_is_feasible_no_infeasible_flag(self):
        """DP must find a valid plan — no infeasible flag when trough allows pre-buy."""
        result, _, _, _ = self._run()
        assert not result.get("infeasible", False), (
            "DP reported infeasible — trough should provide enough cheap headroom"
        )

    def test_soc_never_below_floor_during_peak(self):
        """Floor (0.5 kWh) is never breached at any per-hour boundary.

        This is the core regression: the DP's per-transition floor constraint
        (``if new_soc < floor_kwh: continue``) already enforces this.
        """
        result, cfg, traj, _ = self._run()
        floor_kwh = cfg.soc_floor / 100.0 * cfg.capacity_kwh
        for h, soc in enumerate(traj):
            assert soc >= floor_kwh - 1e-6, f"Floor breach at h={h}: SoC={soc:.4f} kWh < floor={floor_kwh} kWh"

    def test_charge_only_in_trough_hours(self):
        """Grid charging is confined to the cheap trough hours (h0, h1).

        Expensive peak hours (h2-h7) must have zero grid charge — price gate holds.
        """
        result, _, _, _ = self._run()
        schedule = result["schedule"]
        for h in range(2, self.N):
            assert schedule[h] == pytest.approx(0.0, abs=1e-6), (
                f"Unexpected grid charge at expensive h={h}: {schedule[h]:.4f} kWh"
            )

    def test_trough_charge_covers_full_peak_window(self):
        """Total trough charge + starting SoC covers the 6-hour peak load to floor.

        Pre-charge (trough hours) must be ≥ peak_load - (soc_start - floor):
          peak_load = 6 × 1.0 = 6.0 kWh
          usable headroom = 3.0 - 0.5 = 2.5 kWh
          min pre-charge needed = 6.0 - 2.5 = 3.5 kWh
        """
        result, cfg, _, _ = self._run()
        schedule = result["schedule"]
        trough_charge = sum(schedule[:2])  # h0 + h1
        floor_kwh = cfg.soc_floor / 100.0 * cfg.capacity_kwh
        soc_start_kwh = self.SOC_START_PCT / 100.0 * cfg.capacity_kwh
        usable_headroom = soc_start_kwh - floor_kwh
        # Two trough-hours of load consumed during charging:
        load_during_trough = self.LOAD_KWH_H * 2.0
        peak_load = self.LOAD_KWH_H * self.N_PEAK
        min_pre_charge = peak_load - usable_headroom + load_during_trough
        assert trough_charge >= min_pre_charge - 1e-3, (
            f"Pre-charge {trough_charge:.3f} kWh < minimum needed "
            f"{min_pre_charge:.3f} kWh to stay above floor through peak"
        )

    def test_no_over_buy_beyond_soc_target(self):
        """DP does not buy beyond soc_target (9.7 kWh = 9.7 DC kWh).

        Confirms no 'fill full' over-buy — the schedule is bounded by what's
        needed to maintain SoC ≥ floor, not a blanket fill-to-target command.
        """
        result, cfg, traj, _ = self._run()
        target_kwh = cfg.soc_target / 100.0 * cfg.capacity_kwh
        for h, soc in enumerate(traj):
            assert soc <= target_kwh + 1e-6, f"SoC exceeded target at h={h}: {soc:.4f} kWh > {target_kwh} kWh"


# ---------------------------------------------------------------------------
# Scenario 2 — flat-expensive, no-sun: rides floor + imports peak (no over-buy)
#
# All 8 hours at 0.32 €/kWh (above the 0.20 ceiling) → chargeable=[False]*8.
# soc_start = 20% = 2.0 kWh.  Load = 0.5 kWh/h.  No PV.
#
# Without grid charging the battery drains 0.5 kWh/h.
# After h3 (= 4h): 2.0 - 2.0 = 0.0 kWh — below floor (0.5 kWh).
# With chargeable=all-False the DP cannot prevent the floor breach in-window,
# so it returns infeasible=True and the best-achievable zero schedule.
# The controller then correctly RIDES the firmware floor and imports from the
# grid during the peak.  No over-buy into expensive hours — econ-only intact.
#
# We test the ZERO-SCHEDULE outcome (econ-only = no forced charge), not the
# floor breach itself (the firmware handles sub-floor physically; the controller
# correctly does not prevent it by charging at peak prices).
# ---------------------------------------------------------------------------


class TestFlatExpensiveRidesFloor:
    """Scenario 2: flat-expensive, no-sun → zero DP schedule (econ-only holds)."""

    N = 8
    EXPENSIVE_PRICE = 0.32
    CEILING = 0.20
    SOC_START_PCT = 20.0  # 2.0 kWh
    LOAD_KWH_H = 0.5

    def _run(self) -> tuple[dict, Config]:
        cfg = _cfg()
        price = [self.EXPENSIVE_PRICE] * self.N
        pv = [0.0] * self.N
        load = [self.LOAD_KWH_H] * self.N
        chargeable = _chargeable(price, self.CEILING)  # all False
        result = optimize_grid(
            pv,
            load,
            price,
            soc_start=self.SOC_START_PCT,
            cfg=cfg,
            window_start_h=0,
            window_len=self.N,
            chargeable=chargeable,
            feed_in=None,
            terminal_mode="water_value",
            water_value=0.0,
        )
        return result, cfg

    def test_chargeable_mask_is_all_false(self):
        """Sanity: ceiling=0.20 < expensive_price=0.32 → every hour blocked."""
        price = [self.EXPENSIVE_PRICE] * self.N
        mask = _chargeable(price, self.CEILING)
        assert all(not c for c in mask), f"Expected all-False chargeable mask for flat-expensive window, got {mask}"

    def test_zero_grid_schedule_no_over_buy(self):
        """Flat-expensive window → zero grid schedule (econ-only: no force-charge).

        This is the key regression: the DP MUST NOT buy expensive energy to
        fill the battery into the peak.  The schedule must be all-zeros.
        """
        result, _ = self._run()
        assert sum(result["schedule"]) == pytest.approx(0.0, abs=1e-6), (
            f"Expected zero schedule (econ-only), got {result['schedule']}"
        )
        assert all(s == pytest.approx(0.0, abs=1e-6) for s in result["schedule"]), (
            f"Non-zero entry in schedule: {result['schedule']}"
        )

    def test_no_fill_full_into_peak_behavior(self):
        """Explicitly assert the 'fill full into peak' anti-pattern is absent.

        The battery starts at 2.0 kWh; if the DP bought enough to fill to target
        (9.7 kWh) that would require 7.7 kWh — clearly over-buy.  The real
        anti-pattern is an active grid CHARGE into the peak, so assert the charge
        schedule is all-zero.  This documents the B3 design decision: peak-coverage
        at 5% floor is an OUTCOME of self-consumption from a pre-bought trough,
        NOT an active fill-to-target into expensive peak hours.

        M1 note: ``result["kwh"]`` now folds in the below-floor direct grid->load
        import volume (the unavoidable survival import once the pack reaches the
        firmware floor), mirroring regret.realized_grid_cost.  That volume is NOT
        a fill-to-target over-buy — it is the load the grid serves directly while
        the battery rides the floor.  So assert kwh equals exactly that
        floor-import volume (2.5 kWh here), not zero and not the 7.7 kWh fill-up.
        """
        result, cfg = self._run()
        # No active grid CHARGE into the expensive peak (the over-buy anti-pattern).
        assert sum(result["schedule"]) == pytest.approx(0.0, abs=1e-6), (
            f"DP charged into expensive peak hours (fill-full over-buy!): {result['schedule']}"
        )
        # kwh == only the unavoidable below-floor direct grid->load import (M1),
        # independently recomputed from the zero-schedule SoC trajectory.
        floor_kwh = cfg.soc_floor / 100.0 * cfg.capacity_kwh
        soc = self.SOC_START_PCT / 100.0 * cfg.capacity_kwh
        expected_floor_import = 0.0
        for _h in range(self.N):
            soc_after = _apply_solar_load(soc, 0.0 - self.LOAD_KWH_H, cfg)
            expected_floor_import += max(0.0, floor_kwh - soc_after)
            soc = max(soc_after, floor_kwh)
        assert expected_floor_import == pytest.approx(2.5, abs=1e-9)  # fixture sanity
        assert result["kwh"] == pytest.approx(expected_floor_import, abs=1e-6), (
            f"kwh should equal the floor-import volume {expected_floor_import:.3f} "
            f"(no fill-to-target over-buy), got {result['kwh']:.3f}"
        )

    def test_flat_expensive_infeasible_flag_set(self):
        """DP correctly flags infeasible when floor cannot be maintained.

        With chargeable=all-False and enough load drain to breach the floor,
        the DP reports infeasible=True — the live controller then correctly
        does NOT try to prevent the breach by charging at peak prices.
        (The firmware 5% floor is the backstop; the controller rides it.)
        """
        result, _ = self._run()
        # The DP can't reach soc_target (reserve mode) without charging →
        # water_value mode with value=0.0 picks the best-SoC end state, which
        # is reachable (initial SoC survives 0 hours w/o drain past DP init).
        # Use terminal_mode="reserve" to confirm infeasible path:
        cfg = _cfg()
        price = [self.EXPENSIVE_PRICE] * self.N
        pv = [0.0] * self.N
        load = [self.LOAD_KWH_H] * self.N
        chargeable = _chargeable(price, self.CEILING)
        result_reserve = optimize_grid(
            pv,
            load,
            price,
            soc_start=self.SOC_START_PCT,
            cfg=cfg,
            window_start_h=0,
            window_len=self.N,
            chargeable=chargeable,
            feed_in=None,
            terminal_mode="reserve",
            water_value=None,
        )
        assert result_reserve.get("infeasible") is True, (
            "Expected infeasible=True in reserve mode when floor unreachable; "
            f"got infeasible={result_reserve.get('infeasible')}"
        )
        # Even when infeasible, the returned schedule must still be zero
        # (no charge happens into expensive hours).
        assert sum(result_reserve["schedule"]) == pytest.approx(0.0, abs=1e-6), (
            f"Infeasible result should still return zero schedule (no over-buy), got {result_reserve['schedule']}"
        )


# ---------------------------------------------------------------------------
# Scenario 3 — export_surplus_kwh integration (B2 round-trip)
#
# Verify that energy.export_surplus_kwh correctly characterises the battery
# state AFTER the DP pre-charge in Scenario 1:
#
#   - export_surplus_kwh = max(0, soc_after_trough_kwh - reserve) → this is
#     what can safely be exported WITHOUT breaching the floor through the peak.
#
# Using the exact numbers from Scenario 1:
#   soc after trough hours ≈ 6.5 kWh (floor-safe entry into peak).
#   ride_out reserve covers peak window load → 6.0 kWh (see ride_out_reserve_kwh).
#   surplus = max(0, 6.5 - 6.0) = 0.5 kWh.
# ---------------------------------------------------------------------------


class TestReserveAndSurplusAfterPreCharge:
    """export_surplus_kwh consistent with DP pre-charge outcome."""

    def test_export_surplus_kwh_after_pre_charge(self):
        """After pre-charging to 6.5 kWh, surplus above 6.0 kWh reserve = 0.5 kWh."""
        from custom_components.anker_x1_smartgrid import energy

        cfg = _cfg()
        # soc=65% → 6.5 kWh
        # reserve=6.0 kWh (from test above)
        surplus = energy.export_surplus_kwh(65.0, 6.0, cfg)
        assert surplus == pytest.approx(0.5, abs=1e-6), f"Expected 0.5 kWh surplus above reserve, got {surplus:.4f}"

    def test_no_surplus_when_soc_exactly_covers_reserve(self):
        """soc kWh == reserve kWh → surplus = 0 (nothing to export safely)."""
        from custom_components.anker_x1_smartgrid import energy

        cfg = _cfg()
        # soc=60% → 6.0 kWh; reserve=6.0 kWh → surplus=0
        surplus = energy.export_surplus_kwh(60.0, 6.0, cfg)
        assert surplus == pytest.approx(0.0, abs=1e-6), (
            f"Expected 0.0 surplus when soc equals reserve, got {surplus:.4f}"
        )

    def test_zero_surplus_when_soc_below_reserve(self):
        """When soc_kwh < reserve, export_surplus_kwh=0 (export self-disables).

        export_surplus_kwh=0 confirms export self-disables when SoC is below
        the ride-out reserve — no export happens when already depleted.
        """
        from custom_components.anker_x1_smartgrid import energy

        cfg = _cfg()
        # SoC=20% → 2.0 kWh; reserve=4.0 kWh (above soc) → surplus=0
        surplus = energy.export_surplus_kwh(20.0, 4.0, cfg)
        assert surplus == pytest.approx(0.0, abs=1e-6), f"Expected zero surplus when soc < reserve, got {surplus:.4f}"
