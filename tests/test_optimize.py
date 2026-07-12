"""TDD tests for optimize.py — forecast-fed DP grid optimizer.

All tests use hand-computable synthetic scenarios with round numbers
(eta_charge=1.0 so AC kWh == DC kWh).  Config: capacity=10 kWh,
floor=20% (2 kWh), target=80% (8 kWh), max_charge=3 kWh/h, eta=1.0.

These tests are written FIRST (TDD), before optimize.py exists.
"""

import pytest
from custom_components.anker_x1_smartgrid.models import Config
from custom_components.anker_x1_smartgrid.regret import _apply_solar_load
from custom_components.anker_x1_smartgrid.optimize import build_charge_mask, optimize_grid


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_cfg(**overrides) -> Config:
    """Config with clean test defaults; all other fields take their defaults."""
    defaults = dict(
        capacity_kwh=10.0,
        soc_floor=20.0,  # 2 kWh floor
        soc_target=80.0,  # 8 kWh target
        max_charge_w=3000.0,  # 3 kWh/h
        eta_charge=1.0,  # AC == DC (simplifies test arithmetic)
    )
    defaults.update(overrides)
    return Config(**defaults)


def trace_soc(
    pv: list[float],
    load: list[float],
    schedule: list[float],
    soc_start_kwh: float,
    cfg: Config,
) -> list[float]:
    """Return list of per-hour end-of-hour SoC values (kWh) for the window.

    Mirrors the DP physics: _apply_solar_load first, then add grid charge,
    capped at target.  Used by trajectory tests to verify floor/target.
    """
    eta = cfg.eta_charge if cfg.eta_charge > 1e-9 else 1.0
    target_kwh = cfg.soc_target / 100.0 * cfg.capacity_kwh
    soc = soc_start_kwh
    trajectory = []
    for h in range(len(schedule)):
        net = pv[h] - load[h]
        soc = _apply_solar_load(soc, net, cfg)
        g_dc = schedule[h] * eta
        soc = min(soc + g_dc, target_kwh)
        trajectory.append(soc)
    return trajectory


# ---------------------------------------------------------------------------
# Test 1: deficit=0 (full-PV day) → all-zero schedule, end SoC ≥ target
# ---------------------------------------------------------------------------


class TestFullPvDayZeroSchedule:
    """When PV alone covers load + fills battery, grid schedule should be zero."""

    def test_full_pv_covers_deficit(self):
        """24-h window: PV fills battery from 20% to 80%; no load; no grid needed.

        soc_start=20% (2 kWh).  PV 1.5 kWh/h for hours 4-11 (8 × 1.5 = 12 kWh).
        SoC trajectory: charges from 2 kWh → hits target 8 kWh during h4-h9.
        Optimal grid = all zeros.
        """
        cfg = make_cfg()
        window_len = 24
        pv = [0.0] * 4 + [1.5] * 8 + [0.0] * 12
        load = [0.0] * window_len
        price = [0.10] * window_len

        result = optimize_grid(
            pv,
            load,
            price,
            soc_start=20.0,
            cfg=cfg,
            window_start_h=0,
            window_len=window_len,
        )

        assert all(g == pytest.approx(0.0, abs=1e-6) for g in result["schedule"]), (
            f"Expected all-zero schedule, got {result['schedule']}"
        )
        assert result["kwh"] == pytest.approx(0.0, abs=1e-6)
        assert result["eur"] == pytest.approx(0.0, abs=1e-6)
        assert not result.get("infeasible", False)

        # Verify trajectory: end SoC should reach target (8 kWh)
        traj = trace_soc(pv, load, result["schedule"], soc_start_kwh=2.0, cfg=cfg)
        assert traj[-1] == pytest.approx(8.0, abs=0.1), f"End SoC {traj[-1]:.3f} kWh < target 8 kWh"

    def test_partial_window_full_pv(self):
        """Partial 12-h window: PV surplus fills battery; grid stays zero."""
        cfg = make_cfg()
        window_len = 12
        pv = [1.5] * window_len  # 18 kWh total; battery fills quickly
        load = [0.0] * window_len
        price = [0.10] * window_len

        result = optimize_grid(
            pv,
            load,
            price,
            soc_start=20.0,
            cfg=cfg,
            window_start_h=6,
            window_len=window_len,
        )

        assert all(g == pytest.approx(0.0, abs=1e-6) for g in result["schedule"])
        assert result["kwh"] == pytest.approx(0.0, abs=1e-6)
        assert not result.get("infeasible", False)


# ---------------------------------------------------------------------------
# Test 2: single cheap hour, zero PV → charges exactly deficit in cheapest hour
# ---------------------------------------------------------------------------


class TestCheapestHourChargesDeficit:
    """DP selects the single cheapest hour to charge the full deficit."""

    def test_single_cheap_hour_full_window(self):
        """No PV, no load. soc_start=50% (5 kWh); target=80% (8 kWh); deficit=3 kWh.

        price: h0=0.20, h1=0.10 (cheapest), h2-h23=0.20.
        max_charge=3 kWh/h == deficit → all charged in h1 in one shot.
        Expected: schedule[1]=3.0, all others=0.0.
        """
        cfg = make_cfg()
        window_len = 24
        pv = [0.0] * window_len
        load = [0.0] * window_len
        price = [0.20, 0.10] + [0.20] * 22

        result = optimize_grid(
            pv,
            load,
            price,
            soc_start=50.0,
            cfg=cfg,
            window_start_h=0,
            window_len=window_len,
        )

        assert result["schedule"][1] == pytest.approx(3.0, abs=1e-6), (
            f"Expected 3.0 kWh at h1, got {result['schedule'][1]}"
        )
        assert result["kwh"] == pytest.approx(3.0, abs=1e-6)
        assert result["eur"] == pytest.approx(0.30, abs=1e-6)  # 3 × 0.10
        assert not result.get("infeasible", False)

        # All other slots should be zero
        for h in range(window_len):
            if h != 1:
                assert result["schedule"][h] == pytest.approx(0.0, abs=1e-6), (
                    f"Expected 0 at h={h}, got {result['schedule'][h]}"
                )

    def test_cheap_hour_in_partial_window(self):
        """Partial 6-h window; cheapest hour is h1 (index 1 in window).

        soc_start=50% (5 kWh); deficit=3 kWh; max_charge=3 kWh/h.
        price=[0.20, 0.10, 0.15, 0.25, 0.18, 0.12] → h1 cheapest.
        Expected: schedule[1]=3.0.
        """
        cfg = make_cfg()
        window_len = 6
        pv = [0.0] * window_len
        load = [0.0] * window_len
        price = [0.20, 0.10, 0.15, 0.25, 0.18, 0.12]

        result = optimize_grid(
            pv,
            load,
            price,
            soc_start=50.0,
            cfg=cfg,
            window_start_h=10,
            window_len=window_len,
        )

        assert len(result["schedule"]) == window_len
        assert result["schedule"][1] == pytest.approx(3.0, abs=1e-6)
        assert result["kwh"] == pytest.approx(3.0, abs=1e-6)
        assert result["eur"] == pytest.approx(0.30, abs=1e-6)
        assert not result.get("infeasible", False)


# ---------------------------------------------------------------------------
# Test 3: floor never breached at any per-hour boundary (trajectory test)
# ---------------------------------------------------------------------------


def trace_soc_clamped(
    pv: list[float],
    load: list[float],
    schedule: list[float],
    soc_start_kwh: float,
    cfg: Config,
) -> list[float]:
    """Per-hour end-of-hour SoC under the REAL economic-only floor physics.

    Mirrors the post-A1 DP: _apply_solar_load, then CLAMP up to the floor (the
    firmware holds the floor; below-floor load is met by direct grid->load
    import — not represented in `schedule`), then add the grid charge capped at
    target.  Used to verify the clamped trajectory never drops below the floor.
    """
    eta = cfg.eta_charge if cfg.eta_charge > 1e-9 else 1.0
    floor_kwh = cfg.soc_floor / 100.0 * cfg.capacity_kwh
    target_kwh = cfg.soc_target / 100.0 * cfg.capacity_kwh
    soc = soc_start_kwh
    trajectory = []
    for h in range(len(schedule)):
        net = pv[h] - load[h]
        soc = _apply_solar_load(soc, net, cfg)
        soc = max(soc, floor_kwh)  # firmware holds floor via direct import
        soc = min(soc + schedule[h] * eta, target_kwh)
        trajectory.append(soc)
    return trajectory


class TestFloorNeverBreached:
    """Economic-only floor contract (A1): the DP rides to the floor and serves
    sub-floor load by direct grid->load import — it does NOT grid-charge purely
    to hold the floor.  The clamped (real-physics) trajectory stays >= floor.

    Retired contract: these tests previously asserted that the DP grid-charged
    enough to keep the UNCLAMPED trajectory >= floor (a survival charge).  That
    behaviour is gone; below-floor load is now priced direct import, so the
    schedule no longer contains floor-survival charges.
    """

    def test_heavy_load_floor_respected(self):
        """Continuous 1 kWh/h load: no survival charge; clamped trajectory >= floor.

        Config: capacity=10, floor=20% (2 kWh), target=80% (8 kWh).
        soc_start=40% (4 kWh).  Load=1 kWh/h × 12h.  water_value=0 (no incentive
        to charge) and a flat price → the DP charges NOTHING; the pack rides to
        the floor and the sub-floor deficit is met by direct grid->load import.

        eta_charge=0.92 (live value): direct grid->load import (1:1) is STRICTLY
        cheaper than charging ahead (price/eta), so the DP never force-charges to
        hold the floor.  (At eta=1.0 the two tie and the DP may pick an equal-cost
        charge-ahead schedule.)
        """
        cfg = make_cfg(eta_charge=0.92)
        window_len = 12
        pv = [0.0] * window_len
        load = [1.0] * window_len
        price = [0.10] * window_len

        result = optimize_grid(
            pv,
            load,
            price,
            soc_start=40.0,
            cfg=cfg,
            window_start_h=0,
            window_len=window_len,
            terminal_mode="water_value",
            water_value=0.0,
        )

        # DP does not grid-charge purely to hold the floor.
        assert sum(result["schedule"]) == pytest.approx(0.0, abs=1e-9), (
            f"no floor-survival charge expected, got {result['schedule']}"
        )

        # Clamped (real-physics) trajectory stays >= floor.
        floor_kwh = cfg.soc_floor / 100.0 * cfg.capacity_kwh  # 2.0 kWh
        traj = trace_soc_clamped(pv, load, result["schedule"], soc_start_kwh=4.0, cfg=cfg)
        for h, soc in enumerate(traj):
            assert soc >= floor_kwh - 1e-6, f"Floor breached at hour {h}: SoC={soc:.4f} kWh < floor={floor_kwh} kWh"

    def test_single_spike_load_floor_respected(self):
        """Single load spike that would breach the floor is met by direct import.

        soc_start=30% (3 kWh); floor=20% (2 kWh); load=2 kWh at h0 only.
        Without grid: after h0, SoC = 3 - 2 = 1 kWh < 2 kWh floor.  Under the
        economic-only contract the DP does NOT charge to cover it (water_value=0):
        the firmware holds the floor and the 1 kWh deficit is direct grid import.

        eta_charge=0.92 (live value) makes direct import strictly cheaper than a
        charge-ahead, so the DP never force-charges to hold the floor.
        """
        cfg = make_cfg(eta_charge=0.92)
        window_len = 6
        pv = [0.0] * window_len
        load = [2.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        price = [0.10] * window_len

        result = optimize_grid(
            pv,
            load,
            price,
            soc_start=30.0,
            cfg=cfg,
            window_start_h=0,
            window_len=window_len,
            terminal_mode="water_value",
            water_value=0.0,
        )

        # DP does not grid-charge purely to hold the floor.
        assert sum(result["schedule"]) == pytest.approx(0.0, abs=1e-9), (
            f"no floor-survival charge expected, got {result['schedule']}"
        )

        floor_kwh = cfg.soc_floor / 100.0 * cfg.capacity_kwh  # 2.0 kWh
        traj = trace_soc_clamped(pv, load, result["schedule"], soc_start_kwh=3.0, cfg=cfg)
        for h, soc in enumerate(traj):
            assert soc >= floor_kwh - 1e-6, f"Floor breached at hour {h}: SoC={soc:.4f} kWh < floor={floor_kwh} kWh"


# ---------------------------------------------------------------------------
# Test 4: partial-day [now, deadline] window → only schedules within window
# ---------------------------------------------------------------------------


class TestPartialDayWindow:
    """optimize_grid accepts arbitrary window lengths and returns matching schedule."""

    def test_schedule_length_matches_window_len(self):
        """Output schedule has exactly window_len elements (not 24)."""
        cfg = make_cfg()
        for window_len in [1, 4, 6, 10, 12, 18, 24]:
            pv = [0.0] * window_len
            load = [0.0] * window_len
            price = [0.10] * window_len

            result = optimize_grid(
                pv,
                load,
                price,
                soc_start=80.0,
                cfg=cfg,
                window_start_h=0,
                window_len=window_len,
            )

            assert len(result["schedule"]) == window_len, (
                f"window_len={window_len}: expected {window_len} elements, got {len(result['schedule'])}"
            )

    def test_window_start_h_does_not_affect_schedule(self):
        """window_start_h is metadata; shifting it must not change the schedule.

        Same physical window (same pv/load/price/soc_start) with two different
        window_start_h values must yield identical schedules.
        """
        cfg = make_cfg()
        window_len = 6
        pv = [0.0] * window_len
        load = [0.0] * window_len
        price = [0.20, 0.10, 0.15, 0.25, 0.18, 0.12]

        result_a = optimize_grid(
            pv,
            load,
            price,
            soc_start=50.0,
            cfg=cfg,
            window_start_h=0,
            window_len=window_len,
        )
        result_b = optimize_grid(
            pv,
            load,
            price,
            soc_start=50.0,
            cfg=cfg,
            window_start_h=10,
            window_len=window_len,
        )

        for h in range(window_len):
            assert result_a["schedule"][h] == pytest.approx(result_b["schedule"][h], abs=1e-6), (
                f"Schedule differs at h={h}: {result_a['schedule'][h]} vs {result_b['schedule'][h]}"
            )

    def test_mismatched_array_length_raises(self):
        """ValueError raised when input arrays don't match window_len."""
        cfg = make_cfg()
        with pytest.raises(ValueError, match="window_len"):
            optimize_grid(
                [0.0] * 5,  # wrong length
                [0.0] * 6,
                [0.10] * 6,
                soc_start=50.0,
                cfg=cfg,
                window_start_h=0,
                window_len=6,
            )

    def test_zero_window_len_raises(self):
        """ValueError raised for window_len < 1 (defense-in-depth guard)."""
        cfg = make_cfg()
        with pytest.raises(ValueError, match="window_len"):
            optimize_grid(
                [],
                [],
                [],
                soc_start=50.0,
                cfg=cfg,
                window_start_h=0,
                window_len=0,
            )

    def test_single_hour_window(self):
        """1-hour window: schedule has exactly 1 element."""
        cfg = make_cfg()
        result = optimize_grid(
            [0.0],
            [0.0],
            [0.10],
            soc_start=50.0,
            cfg=cfg,
            window_start_h=14,
            window_len=1,
        )
        assert len(result["schedule"]) == 1
        # deficit = 3 kWh, max_charge = 3 kWh/h → can fill in 1 hour
        assert result["schedule"][0] == pytest.approx(3.0, abs=1e-6)

    def test_now_deadline_window_boundaries(self):
        """Charging only happens inside the [now, deadline] window.

        soc_start=50% (5 kWh). window_len=4 hours starting at h18.
        No PV, no load. price=[0.30, 0.10, 0.20, 0.25] → h1 (19:00) cheapest.
        Expected: schedule[1]=3.0 kWh, rest=0.
        """
        cfg = make_cfg()
        window_len = 4
        pv = [0.0] * window_len
        load = [0.0] * window_len
        price = [0.30, 0.10, 0.20, 0.25]

        result = optimize_grid(
            pv,
            load,
            price,
            soc_start=50.0,
            cfg=cfg,
            window_start_h=18,
            window_len=window_len,
        )

        assert len(result["schedule"]) == window_len
        assert result["schedule"][1] == pytest.approx(3.0, abs=1e-6)
        assert result["schedule"][0] == pytest.approx(0.0, abs=1e-6)
        assert result["schedule"][2] == pytest.approx(0.0, abs=1e-6)
        assert result["schedule"][3] == pytest.approx(0.0, abs=1e-6)


# ---------------------------------------------------------------------------
# Test 5: build_charge_mask unit tests
# ---------------------------------------------------------------------------


class TestBuildChargeMask:
    """Unit tests for the thin build_charge_mask helper."""

    def test_none_ceiling_returns_all_false(self):
        """ceiling=None (peak unknown) → fail-closed: all hours not chargeable."""
        mask = build_charge_mask([0.10, 0.20, 0.30], ceiling=None)
        assert mask == [False, False, False]

    def test_none_ceiling_empty_list(self):
        """ceiling=None with empty list → empty all-False list."""
        mask = build_charge_mask([], ceiling=None)
        assert mask == []

    def test_none_ceiling_all_prices(self):
        """ceiling=None regardless of price values → all False."""
        prices = [0.01, 0.50, 0.99]
        mask = build_charge_mask(prices, ceiling=None)
        assert mask == [False, False, False]

    def test_empty_price_list_with_ceiling(self):
        """Empty price list with a ceiling → empty mask."""
        mask = build_charge_mask([], ceiling=0.25)
        assert mask == []

    def test_boundary_price_equal_ceiling_is_chargeable(self):
        """price[h] == ceiling → chargeable (<=, not <)."""
        mask = build_charge_mask([0.20], ceiling=0.20)
        assert mask == [True]

    def test_price_above_ceiling_not_chargeable(self):
        """price[h] > ceiling → not chargeable."""
        mask = build_charge_mask([0.21], ceiling=0.20)
        assert mask == [False]

    def test_price_below_ceiling_chargeable(self):
        """price[h] < ceiling → chargeable."""
        mask = build_charge_mask([0.19], ceiling=0.20)
        assert mask == [True]

    def test_mixed_prices_against_ceiling(self):
        """Mixed prices against a ceiling are masked correctly."""
        prices = [0.10, 0.20, 0.25, 0.30]
        mask = build_charge_mask(prices, ceiling=0.20)
        assert mask == [True, True, False, False]

    def test_all_prices_below_ceiling(self):
        """All prices below ceiling → all True."""
        prices = [0.05, 0.10, 0.15]
        mask = build_charge_mask(prices, ceiling=0.50)
        assert mask == [True, True, True]

    def test_all_prices_above_ceiling(self):
        """All prices above ceiling → all False."""
        prices = [0.30, 0.40, 0.50]
        mask = build_charge_mask(prices, ceiling=0.20)
        assert mask == [False, False, False]


# ---------------------------------------------------------------------------
# Test 6: per-stage action mask in optimize_grid
# ---------------------------------------------------------------------------


class TestChargeMaskActionMask:
    """Tests that the chargeable mask restricts grid charge per hour."""

    def test_masked_hours_get_zero_grid_charge(self):
        """Hours where chargeable=False get exactly zero grid charge.

        soc_start=50% (5 kWh), deficit=3 kWh, 3-h window.
        price: [0.10, 0.20, 0.15] — h0 cheapest, but masked.
        Mask: h0=False, h1=True, h2=True.
        DP must charge in h2 (0.15 — cheapest among chargeable hours).
        h0 must be exactly zero regardless of price.
        """
        cfg = make_cfg()
        window_len = 3
        pv = [0.0] * window_len
        load = [0.0] * window_len
        price = [0.10, 0.20, 0.15]
        chargeable = [False, True, True]  # h0 blocked

        result = optimize_grid(
            pv,
            load,
            price,
            soc_start=50.0,
            cfg=cfg,
            window_start_h=0,
            window_len=window_len,
            chargeable=chargeable,
        )

        # Masked hour must be zero even though it's the cheapest
        assert result["schedule"][0] == pytest.approx(0.0, abs=1e-6), (
            f"Masked hour h0 must have zero charge, got {result['schedule'][0]}"
        )
        # Full 3 kWh deficit must still be covered (feasible)
        assert result["kwh"] == pytest.approx(3.0, abs=1e-6)
        assert not result.get("infeasible", False)

    def test_all_hours_masked_no_grid_charge(self):
        """All hours masked → schedule is all-zero, no grid energy imported.

        chargeable=[False]*window_len forces max_grid_dc=0 every hour.
        With soc_start=50% and no load/pv, no charging can happen.
        """
        cfg = make_cfg()
        window_len = 4
        pv = [0.0] * window_len
        load = [0.0] * window_len
        price = [0.10, 0.15, 0.20, 0.25]
        chargeable = [False, False, False, False]

        result = optimize_grid(
            pv,
            load,
            price,
            soc_start=50.0,
            cfg=cfg,
            window_start_h=0,
            window_len=window_len,
            chargeable=chargeable,
        )

        for h, g in enumerate(result["schedule"]):
            assert g == pytest.approx(0.0, abs=1e-6), f"Hour h={h} must be zero (masked), got {g}"
        assert result["kwh"] == pytest.approx(0.0, abs=1e-6)

    def test_expensive_hours_masked_charge_in_cheap_hour(self):
        """Mask built from ceiling directs charge to the only cheap hour.

        Prices: [0.10, 0.30, 0.25, 0.35]. Ceiling=0.20.
        Only h0 (0.10 ≤ 0.20) is chargeable → all deficit charged there.
        """
        cfg = make_cfg()
        window_len = 4
        pv = [0.0] * window_len
        load = [0.0] * window_len
        price = [0.10, 0.30, 0.25, 0.35]
        chargeable = build_charge_mask(price, ceiling=0.20)
        assert chargeable == [True, False, False, False]

        result = optimize_grid(
            pv,
            load,
            price,
            soc_start=50.0,
            cfg=cfg,
            window_start_h=0,
            window_len=window_len,
            chargeable=chargeable,
        )

        # Masked hours must be zero
        assert result["schedule"][1] == pytest.approx(0.0, abs=1e-6)
        assert result["schedule"][2] == pytest.approx(0.0, abs=1e-6)
        assert result["schedule"][3] == pytest.approx(0.0, abs=1e-6)
        # h0 gets the full 3 kWh deficit (max_charge=3 kWh/h = deficit)
        assert result["schedule"][0] == pytest.approx(3.0, abs=1e-6)
        assert not result.get("infeasible", False)

    def test_chargeable_none_is_fully_permissive(self):
        """chargeable=None (default) is equivalent to all-True mask.

        Both must produce identical schedules — this protects the T0.1b
        parity invariant: the default path is unchanged.
        """
        cfg = make_cfg()
        window_len = 6
        pv = [0.0] * window_len
        load = [0.0] * window_len
        price = [0.20, 0.10, 0.15, 0.25, 0.18, 0.12]

        result_none = optimize_grid(
            pv,
            load,
            price,
            soc_start=50.0,
            cfg=cfg,
            window_start_h=0,
            window_len=window_len,
            chargeable=None,
        )
        result_all_true = optimize_grid(
            pv,
            load,
            price,
            soc_start=50.0,
            cfg=cfg,
            window_start_h=0,
            window_len=window_len,
            chargeable=[True] * window_len,
        )

        assert result_none["kwh"] == pytest.approx(result_all_true["kwh"], abs=1e-6)
        for h in range(window_len):
            assert result_none["schedule"][h] == pytest.approx(result_all_true["schedule"][h], abs=1e-6), (
                f"Schedule mismatch at h={h}"
            )

    def test_chargeable_wrong_length_raises(self):
        """chargeable list with wrong length raises ValueError."""
        cfg = make_cfg()
        window_len = 4
        pv = [0.0] * window_len
        load = [0.0] * window_len
        price = [0.10] * window_len

        with pytest.raises(ValueError, match="chargeable"):
            optimize_grid(
                pv,
                load,
                price,
                soc_start=50.0,
                cfg=cfg,
                window_start_h=0,
                window_len=window_len,
                chargeable=[True, True, True],  # wrong: 3 != 4
            )

    def test_ceiling_none_mask_no_grid_charge(self):
        """build_charge_mask(price, ceiling=None) → all-False → no grid charge.

        End-to-end: ceiling=None (peak unknown) → fail-closed mask →
        passed to optimize_grid → zero schedule output.
        """
        cfg = make_cfg()
        window_len = 4
        pv = [0.0] * window_len
        load = [0.0] * window_len
        price = [0.10, 0.15, 0.20, 0.25]
        chargeable = build_charge_mask(price, ceiling=None)
        assert chargeable == [False, False, False, False]

        result = optimize_grid(
            pv,
            load,
            price,
            soc_start=50.0,
            cfg=cfg,
            window_start_h=0,
            window_len=window_len,
            chargeable=chargeable,
        )

        for h, g in enumerate(result["schedule"]):
            assert g == pytest.approx(0.0, abs=1e-6), f"Hour h={h} must be zero (fail-closed mask), got {g}"
        assert result["kwh"] == pytest.approx(0.0, abs=1e-6)


# ---------------------------------------------------------------------------
# Test 7: mask makes reserve unreachable → infeasible=True surfaces
# ---------------------------------------------------------------------------


class TestMaskInfeasibleSurfaces:
    """Masking that makes reserve unreachable must surface infeasible=True.

    The mask must NOT silently swallow a floor/reserve breach — the existing
    infeasible path in the DP must still fire when appropriate.
    """

    def test_all_masked_reserve_unreachable_infeasible(self):
        """All hours masked → no charging → reserve unreachable → infeasible=True.

        soc_start=20% (2 kWh), target=80% (8 kWh), deficit=6 kWh.
        With all hours masked, no grid charge is possible → target unreachable.
        """
        cfg = make_cfg()
        window_len = 4
        pv = [0.0] * window_len
        load = [0.0] * window_len
        price = [0.10] * window_len
        chargeable = [False, False, False, False]

        result = optimize_grid(
            pv,
            load,
            price,
            soc_start=20.0,
            cfg=cfg,
            window_start_h=0,
            window_len=window_len,
            chargeable=chargeable,
        )

        assert result.get("infeasible", False), "Reserve unreachable due to full mask → must surface infeasible=True"
        # Schedule must still be all-zero (mask was enforced)
        for h, g in enumerate(result["schedule"]):
            assert g == pytest.approx(0.0, abs=1e-6)

    def test_partial_mask_still_feasible_when_enough_capacity(self):
        """Partial mask: one chargeable hour covers full 3 kWh deficit → feasible.

        soc_start=50% (5 kWh), deficit=3 kWh, max_charge=3 kWh/h.
        Only h0 chargeable — but 3 kWh in one hour equals the deficit.
        """
        cfg = make_cfg()
        window_len = 4
        pv = [0.0] * window_len
        load = [0.0] * window_len
        price = [0.10, 0.15, 0.20, 0.25]
        chargeable = [True, False, False, False]

        result = optimize_grid(
            pv,
            load,
            price,
            soc_start=50.0,
            cfg=cfg,
            window_start_h=0,
            window_len=window_len,
            chargeable=chargeable,
        )

        assert not result.get("infeasible", False), "h0 alone covers 3 kWh deficit → should be feasible"
        assert result["schedule"][0] == pytest.approx(3.0, abs=1e-6)

    def test_mask_prevents_floor_recovery_surfaces_infeasible(self):
        """Load drains battery below floor; masking blocks recovery → infeasible.

        soc_start=25% (2.5 kWh), load=2.0 kWh/h, floor=20% (2 kWh).
        h0: soc_after = 2.5 − 2.0 = 0.5 kWh < floor (2 kWh).
        With chargeable=[False, ...], no grid charge → all transitions blocked
        at h0 (new_soc < floor for g_dc=0) → pathological infeasible.
        """
        cfg = make_cfg()
        window_len = 3
        pv = [0.0] * window_len
        load = [2.0] * window_len
        price = [0.10] * window_len
        chargeable = [False, False, False]

        result = optimize_grid(
            pv,
            load,
            price,
            soc_start=25.0,
            cfg=cfg,
            window_start_h=0,
            window_len=window_len,
            chargeable=chargeable,
        )

        assert result.get("infeasible", False), (
            "Floor breach + all masked → infeasible must surface (mask must not silently swallow the floor breach)"
        )


def test_optimize_grid_export_keys_present_and_absent():
    """export_price=None → no export keys; export_price=list → export keys populated."""
    from custom_components.anker_x1_smartgrid.optimize import optimize_grid
    from custom_components.anker_x1_smartgrid.models import Config

    cfg = Config(
        capacity_kwh=10.0,
        soc_floor=20.0,
        soc_target=80.0,
        max_charge_w=3000.0,
        eta_charge=1.0,
        round_trip_eff=1.0,
        cycle_cost_eur_per_kwh=0.04,
        max_export_w=3000.0,
        grid_export_limit_w=3000.0,
    )
    pv = [0.0] * 24
    load = [0.0] * 24
    price = [0.20] * 24
    charge_only = optimize_grid(pv, load, price, soc_start=80.0, cfg=cfg, window_start_h=0, window_len=24)
    assert "export_schedule" not in charge_only
    ep = [0.0] * 24
    ep[18] = 0.50
    with_export = optimize_grid(
        pv, load, price, soc_start=80.0, cfg=cfg, window_start_h=0, window_len=24, export_price=ep
    )
    assert with_export["export_schedule"][18] > 0.0
    assert with_export["export_kwh"] > 0.0
    assert with_export["export_revenue_eur"] > 0.0


def test_optimize_grid_48h_export_runtime_under_2s():
    """Benchmark gate: one 48h co-optimized pass replaces up-to-10 greedy passes."""
    import time
    from custom_components.anker_x1_smartgrid.optimize import optimize_grid
    from custom_components.anker_x1_smartgrid.models import Config

    cfg = Config(
        capacity_kwh=10.0,
        soc_floor=5.0,
        soc_target=97.0,
        max_charge_w=6000.0,
        eta_charge=0.92,
        round_trip_eff=0.85,
        cycle_cost_eur_per_kwh=0.04,
        max_export_w=6000.0,
        grid_export_limit_w=6000.0,
    )
    n = 48
    pv = ([0.0] * 8 + [2.0] * 8 + [0.0] * 8) * 2
    load = [0.4] * n
    price = [0.10 + 0.3 * (i % 24) / 24.0 for i in range(n)]
    ep = list(price)
    t0 = time.perf_counter()
    result = optimize_grid(pv, load, price, soc_start=60.0, cfg=cfg, window_start_h=0, window_len=n, export_price=ep)
    elapsed = time.perf_counter() - t0
    assert len(result["schedule"]) == n
    assert elapsed < 2.0, f"48h co-optimized DP took {elapsed:.3f}s (>2s budget); coarsen bins if persistent"


def test_effective_export_price_subtracts_fee():
    from custom_components.anker_x1_smartgrid.optimize import effective_export_price
    from custom_components.anker_x1_smartgrid.models import Config

    cfg = Config(export_fee_eur_per_kwh=0.02)
    assert effective_export_price(0.50, cfg) == pytest.approx(0.48)
    # Config picks up the default and tolerates the new field via from_dict.
    cfg2 = Config.from_dict({"export_fee_eur_per_kwh": 0.05, "unknown_key": 1})
    assert effective_export_price(0.50, cfg2) == pytest.approx(0.45)
