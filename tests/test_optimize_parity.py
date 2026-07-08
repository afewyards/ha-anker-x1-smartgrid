"""Parity gate: optimize_grid ≡ hindsight_optimal_grid for full 24-h windows.

Safety-critical trust anchor (T0.1b): proves that the online optimizer
``optimize_grid`` is **provably identical** to the proven
``regret.hindsight_optimal_grid`` when invoked on a full realized day
(window_start_h=0, window_len=24).

Only the data source differs — DayData vs. raw arrays — the DP physics are
shared because optimize.py imports ``_apply_solar_load`` and ``_BIN_KWH``
directly from regret.py.  Any change to those helpers propagates to both
paths automatically, preserving the parity invariant.

Coverage
--------
- Charge-leg (strict): Scenario (a) low-PV / high-load day
- Charge-leg (strict): Scenario (b) solar-rich day
- Charge-leg (strict): Scenario (c) near-infeasible day (target unreachable — exercises fallback)
- Charge-leg (strict): Property loop: N=20 random-but-valid days (seeded for determinism)
- Charge-leg (strict): Infeasible-flag sharp edge: compared as truthiness (key present only when True)
- Export-leg (strict): Export-ON parity: optimize_grid ≡ oracle when export_price supplied
  (pv=0 → no solar spill).  See TestExportOnParity below.
"""
import random

import pytest

from custom_components.anker_x1_smartgrid.models import Config
from custom_components.anker_x1_smartgrid.optimize import optimize_grid, solar_reservation_ceiling
from custom_components.anker_x1_smartgrid.regret import (
    DayData,
    _BIN_KWH,
    hindsight_optimal_grid,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_cfg(**overrides) -> Config:
    """Return a Config with clean parity-test defaults.

    eta_charge=1.0 so AC kWh == DC kWh — simplifies arithmetic and allows
    exact (not just approximate) bin arithmetic.  Override any field via kwargs.
    """
    defaults = dict(
        capacity_kwh=10.0,
        soc_floor=20.0,   # 2 kWh floor
        soc_target=80.0,  # 8 kWh target / end-reserve
        max_charge_w=3000.0,  # 3 kWh/h AC
        eta_charge=1.0,   # AC == DC (exact bin arithmetic)
    )
    defaults.update(overrides)
    return Config(**defaults)


def call_both(
    pv: list[float],
    load: list[float],
    price: list[float],
    soc_start: float,
    cfg: Config,
) -> tuple[dict, dict]:
    """Invoke both optimizers with identical 24-h inputs and return (opt, hind)."""
    assert len(pv) == len(load) == len(price) == 24, "call_both requires exactly 24 h"

    day = DayData(
        pv_kwh=tuple(pv),
        load_kwh=tuple(load),
        price=tuple(price),
        soc_start=soc_start,
    )
    hind = hindsight_optimal_grid(day, cfg)
    opt = optimize_grid(
        pv, load, price,
        soc_start=soc_start,
        cfg=cfg,
        window_start_h=0,
        window_len=24,
    )
    return opt, hind


def assert_parity(opt: dict, hind: dict, *, tol: float = 1e-6, label: str = "") -> None:
    """Assert exact parity between optimize_grid and hindsight_optimal_grid results.

    Checks:
    1. infeasible flag (truthiness — key only present when True in both impls)
    2. total kWh within *tol*
    3. total EUR within *tol*
    4. per-hour schedule elementwise within *tol*
    """
    prefix = f"[{label}] " if label else ""

    # 1. infeasible flag — compare truthiness (both emit key only when True)
    opt_inf = bool(opt.get("infeasible", False))
    hind_inf = bool(hind.get("infeasible", False))
    assert opt_inf == hind_inf, (
        f"{prefix}infeasible mismatch: optimize_grid={opt_inf} hindsight={hind_inf}"
    )

    # 2. total kWh
    assert opt["kwh"] == pytest.approx(hind["kwh"], abs=tol), (
        f"{prefix}kwh mismatch: optimize_grid={opt['kwh']:.8f} "
        f"hindsight={hind['kwh']:.8f} (diff={abs(opt['kwh']-hind['kwh']):.2e})"
    )

    # 3. total EUR
    assert opt["eur"] == pytest.approx(hind["eur"], abs=tol), (
        f"{prefix}eur mismatch: optimize_grid={opt['eur']:.8f} "
        f"hindsight={hind['eur']:.8f} (diff={abs(opt['eur']-hind['eur']):.2e})"
    )

    # 4. per-hour schedule — elementwise
    assert len(opt["schedule"]) == 24, (
        f"{prefix}optimize_grid schedule len {len(opt['schedule'])} != 24"
    )
    assert len(hind["schedule"]) == 24, (
        f"{prefix}hindsight schedule len {len(hind['schedule'])} != 24"
    )
    for h in range(24):
        assert opt["schedule"][h] == pytest.approx(hind["schedule"][h], abs=tol), (
            f"{prefix}schedule[h={h:02d}] mismatch: "
            f"optimize_grid={opt['schedule'][h]:.8f} "
            f"hindsight={hind['schedule'][h]:.8f}"
        )


def assert_charge_leg_parity(opt: dict, hind: dict, *, tol: float = 1e-6, label: str = "") -> None:
    """Strict charge-leg parity between optimize_grid and hindsight_optimal_grid.

    Alias of assert_parity with an explicit name for charge-only scenarios.
    Use when export_price=None so that export keys need not be compared.
    """
    assert_parity(opt, hind, tol=tol, label=label)


# ---------------------------------------------------------------------------
# Scenario (a): low-PV / high-load day
# ---------------------------------------------------------------------------


class TestParityLowPvHighLoad:
    """Scenario (a): sparse PV + continuous heavy load — DP must actively charge."""

    def test_low_pv_high_load_varying_prices(self):
        """Full 24h: low midday PV + 1 kWh/h load with varying prices.

        soc_start=60% (6 kWh).  PV 0.5 kWh/h hours 9-12 (2 kWh total).
        Load 1 kWh/h all day (24 kWh).  Net energy negative — DP must charge
        in the cheapest hours (00-05) to meet end reserve.
        """
        cfg = make_cfg()
        pv   = [0.0] * 9 + [0.5] * 4 + [0.0] * 11
        load = [1.0] * 24
        price = [
            0.30, 0.28, 0.25, 0.22, 0.20, 0.18,  # 00-05 cheapest
            0.22, 0.28, 0.32, 0.35, 0.33, 0.31,  # 06-11
            0.29, 0.27, 0.25, 0.28, 0.32, 0.35,  # 12-17
            0.38, 0.40, 0.36, 0.32, 0.28, 0.25,  # 18-23 evening peak
        ]
        opt, hind = call_both(pv, load, price, soc_start=60.0, cfg=cfg)
        assert_parity(opt, hind, tol=1e-6, label="low_pv_high_load_varying")

    def test_zero_pv_uniform_load_flat_price(self):
        """No PV, flat prices: DP charges just enough to satisfy end reserve."""
        cfg = make_cfg()
        pv   = [0.0] * 24
        load = [0.5] * 24
        price = [0.20] * 24
        opt, hind = call_both(pv, load, price, soc_start=40.0, cfg=cfg)
        assert_parity(opt, hind, tol=1e-6, label="zero_pv_flat_price")

    def test_heavy_overnight_load_cheap_predawn(self):
        """Heavy load overnight; cheapest slot pre-dawn forces early pre-charge.

        soc_start=50% (5 kWh).  Load 2 kWh/h hours 20-23 + 0.3 kWh/h rest.
        Cheapest hours: h3-h5 (0.12 €/kWh).  DP should pre-charge there.
        """
        cfg = make_cfg()
        pv   = [0.0] * 24
        load = [0.3] * 20 + [2.0] * 4
        price = (
            [0.30] * 3
            + [0.12, 0.12, 0.12]  # h3-h5 cheapest
            + [0.25] * 15
            + [0.35] * 2
            + [0.40]
        )
        opt, hind = call_both(pv, load, price, soc_start=50.0, cfg=cfg)
        assert_parity(opt, hind, tol=1e-6, label="heavy_overnight_load")


# ---------------------------------------------------------------------------
# Scenario (b): solar-rich day
# ---------------------------------------------------------------------------


class TestParitySolarRich:
    """Scenario (b): abundant midday PV covers load and fills battery."""

    def test_solar_rich_low_start(self):
        """Full 24h: strong PV (2.5 kWh/h × 8h), load 0.5 kWh/h.

        soc_start=20% (2 kWh).  High midday PV exercises the solar-surplus
        charging path in _apply_solar_load.  Parity validates that both
        implementations handle PV saturation (capped at target) identically.
        """
        cfg = make_cfg()
        pv   = [0.0] * 8 + [2.5] * 8 + [0.0] * 8
        load = [0.5] * 24
        price = [
            0.15, 0.14, 0.13, 0.12, 0.11, 0.10,  # 00-05 cheapest
            0.14, 0.18, 0.22, 0.25, 0.27, 0.28,  # 06-11
            0.26, 0.24, 0.22, 0.21, 0.22, 0.25,  # 12-17
            0.28, 0.30, 0.28, 0.24, 0.20, 0.16,  # 18-23
        ]
        opt, hind = call_both(pv, load, price, soc_start=20.0, cfg=cfg)
        assert_parity(opt, hind, tol=1e-6, label="solar_rich_low_start")

    def test_solar_rich_full_battery_start(self):
        """Battery near full at start; PV surplus fills quickly; late-day load may require grid."""
        cfg = make_cfg()
        pv   = [0.0] * 6 + [3.0] * 6 + [0.0] * 12
        load = [0.3] * 24
        price = [0.20] * 24
        opt, hind = call_both(pv, load, price, soc_start=80.0, cfg=cfg)
        assert_parity(opt, hind, tol=1e-6, label="solar_rich_full_start")

    def test_solar_exceeds_charge_rate(self):
        """PV surplus exceeds max_charge rate — excess is clipped by _apply_solar_load."""
        cfg = make_cfg()  # max_charge = 3 kWh/h
        pv   = [0.0] * 9 + [5.0] * 6 + [0.0] * 9  # 5 kWh/h >> 3 kWh/h rate
        load = [0.5] * 24
        price = [0.25] * 24
        opt, hind = call_both(pv, load, price, soc_start=30.0, cfg=cfg)
        assert_parity(opt, hind, tol=1e-6, label="solar_exceeds_rate")


# ---------------------------------------------------------------------------
# Scenario (c): near-infeasible day (exercises infeasible fallback)
# ---------------------------------------------------------------------------


class TestParityNearInfeasible:
    """Scenario (c): end-reserve target unreachable — exercises infeasible paths."""

    def test_target_unreachable_low_rate(self):
        """Target unreachable: max_charge too low to bridge gap.

        soc_start=20% (2 kWh).  max_charge=100W (0.1 kWh/h), no PV, no load.
        Max achievable after 24h = 2 + 24×0.1 = 4.4 kWh < target 8 kWh.
        Both must return infeasible=True with an identical best-effort schedule.
        Note: in the infeasible fallback the DP charges at max rate unconditionally,
        so the schedule is fully constrained by capacity — price ordering does not
        affect which path is selected.
        """
        cfg = make_cfg(max_charge_w=100.0)
        pv   = [0.0] * 24
        load = [0.0] * 24
        price = [0.20, 0.10] + [0.15] * 22
        opt, hind = call_both(pv, load, price, soc_start=20.0, cfg=cfg)
        assert bool(opt.get("infeasible", False)), (
            "optimize_grid should flag infeasible"
        )
        assert bool(hind.get("infeasible", False)), (
            "hindsight should flag infeasible"
        )
        assert_parity(opt, hind, tol=1e-6, label="target_unreachable_low_rate")

    def test_all_paths_blocked_pathological(self):
        """Heavy drain, target unreachable: now RESERVE-mode infeasible (A1).

        soc_start=20% (2 kWh, at floor).  Load=1 kWh/h.  max_charge=0.5 kWh/h.
        Pre-A1 the floor prune blocked every transition from hour 0 (new_soc_max =
        1.5 kWh < 2 kWh floor) and both DPs returned schedule=[0]*24, kwh=0,
        eur=0, infeasible=True.

        Post-A1 the floor no longer prunes: the pack drains to the floor each hour
        and the sub-floor load is met by direct grid->load import, so the SoC path
        is always FEASIBLE.  Infeasibility now comes ONLY from the RESERVE target
        (8 kWh) being unreachable — max_charge (0.5/h) < load (1/h) means SoC can
        never grow, so reserve mode flags infeasible and returns a best-effort
        max-charge schedule (kwh > 0).  Both DPs apply the identical clamp +
        floor_import_cost, so the result stays byte-identical (parity locked).
        """
        cfg = make_cfg(max_charge_w=500.0)
        pv   = [0.0] * 24
        load = [1.0] * 24
        price = [0.20] * 24
        opt, hind = call_both(pv, load, price, soc_start=20.0, cfg=cfg)
        assert bool(opt.get("infeasible", False)), (
            "optimize_grid should flag infeasible (reserve target unreachable)"
        )
        assert bool(hind.get("infeasible", False)), (
            "hindsight should flag infeasible (reserve target unreachable)"
        )
        assert_parity(opt, hind, tol=1e-6, label="reserve_unreachable_drain")
        # Sanity: drain-to-floor is feasible now, so the best-effort fallback
        # charges at max rate (no longer the old all-zero pathological schedule).
        assert opt["kwh"] > 0.0
        assert hind["kwh"] > 0.0

    def test_borderline_feasible_then_infeasible(self):
        """Two runs: one just-feasible, one just-infeasible (differ only by 1 W).

        Feasible:   max_charge=300W (0.3 kWh/h) → max 2+24×0.3=9.2 > 8 kWh target.
        Infeasible: max_charge=200W (0.2 kWh/h) → max 2+24×0.2=6.8 < 8 kWh target.
        Both pairs must agree between optimize_grid and hindsight.

        A1 note: there is NO load here, so the SoC floor never binds and the
        economic-only floor change is a strict no-op for this scenario.  The
        infeasibility is RESERVE-driven (the 8 kWh target is unreachable at the
        starved charge rate), not floor-driven, so the expectations are unchanged.
        """
        pv   = [0.0] * 24
        load = [0.0] * 24
        price = [0.20] * 24

        # Just-feasible
        cfg_ok = make_cfg(max_charge_w=300.0)
        opt_ok, hind_ok = call_both(pv, load, price, soc_start=20.0, cfg=cfg_ok)
        assert not bool(opt_ok.get("infeasible", False)), (
            "300W should be feasible"
        )
        assert_parity(opt_ok, hind_ok, tol=1e-6, label="borderline_feasible")

        # Just-infeasible
        cfg_nok = make_cfg(max_charge_w=200.0)
        opt_nok, hind_nok = call_both(pv, load, price, soc_start=20.0, cfg=cfg_nok)
        assert bool(opt_nok.get("infeasible", False)), (
            "200W should be infeasible"
        )
        assert bool(hind_nok.get("infeasible", False)), (
            "200W hindsight should be infeasible"
        )
        assert_parity(opt_nok, hind_nok, tol=1e-6, label="borderline_infeasible")


# ---------------------------------------------------------------------------
# Floor-binding parity (A1): locks the regret economic-only floor mirror
# ---------------------------------------------------------------------------


class TestFloorBindingParity:
    """optimize_grid ≡ hindsight_optimal_grid when the SoC floor genuinely binds.

    The A1 economic-only floor change (clamp + below-floor direct-import cost) is
    duplicated in BOTH optimize.optimize_grid and regret.hindsight_optimal_grid.
    This test drives a window where the pack drains below the floor so the new
    code path is actually exercised, and asserts byte-parity — pinning the regret
    mirror to the optimizer.
    """

    def test_floor_binding_parity(self):
        """soc_start just above floor + heavy drain → sub-floor direct import path.

        floor=20% (2 kWh), soc_start=22% (2.2 kWh), continuous 1 kWh/h load, no PV,
        eta_charge=0.92 (so direct import is STRICTLY cheaper than a charge-ahead —
        the floor genuinely binds instead of tie-breaking into a charge).

        Expensive morning (0.40) → no economic charge, the pack drains below the
        floor and is served by direct grid->load import.  Cheap evening trough
        (0.10) with a high water value (0.30) rewards charging — so the SAME window
        exercises BOTH the floor leg (early) and the charge leg (late).  Both DPs
        must apply the identical clamp + floor_import_cost to stay byte-identical.
        """
        cfg = make_cfg(eta_charge=0.92)  # floor=20% (2 kWh)
        pv = [0.0] * 24
        load = [1.0] * 24
        # Expensive morning, cheap evening trough — drains below floor early.
        price = [0.40] * 18 + [0.10] * 6
        soc_start = 22.0  # 2.2 kWh — one hour of drain crosses the 2 kWh floor
        wv = 0.30  # > trough/eta, so the cheap evening hours are worth charging

        day = DayData(
            pv_kwh=tuple(pv), load_kwh=tuple(load),
            price=tuple(price), soc_start=soc_start,
        )
        hind = hindsight_optimal_grid(
            day, cfg, terminal_mode="water_value", water_value=wv,
        )
        opt = optimize_grid(
            pv, load, price, soc_start=soc_start, cfg=cfg,
            window_start_h=0, window_len=24,
            terminal_mode="water_value", water_value=wv,
        )
        # Floor binds across the expensive morning: no force-charge there.
        assert sum(opt["schedule"][:6]) == pytest.approx(0.0, abs=1e-9), (
            "expected the floor-binding (no-charge) drain in the expensive morning"
        )
        # The DP still charges at the cheap evening trough (exercises charge leg).
        assert sum(opt["schedule"][18:]) > 0.0, "expected a water-value charge at the trough"
        # eur must STRICTLY exceed the pure schedule import cost: the difference is
        # the below-floor direct-import cost the floor path adds (proves it fired).
        import_eur = sum(opt["schedule"][h] * price[h] for h in range(24))
        assert opt["eur"] - import_eur > 1e-6, (
            f"floor path should add sub-floor import cost; eur={opt['eur']} import={import_eur}"
        )
        assert_parity(opt, hind, tol=1e-6, label="floor_binding")


# ---------------------------------------------------------------------------
# Property loop: N random-but-valid days
# ---------------------------------------------------------------------------


class TestParityRandomDays:
    """Property-style loop over N random-but-valid full 24h days.

    Seeded RNG ensures full determinism across test runs.  Asserts complete
    parity (schedule, kWh, EUR, infeasible) for each generated day.
    """

    N_DAYS = 20
    SEED = 42

    def _random_day(self, rng: random.Random) -> tuple:
        """Generate a random valid 24-h day scenario (pv, load, price, soc_start)."""
        soc_start = rng.uniform(20.0, 80.0)
        # Realistic PV shape: zero at night, random midday output
        pv = (
            [0.0] * 6
            + [rng.uniform(0.0, 3.0) for _ in range(10)]
            + [0.0] * 8
        )
        # Load: random 0.2–2.0 kWh/h each hour
        load = [rng.uniform(0.2, 2.0) for _ in range(24)]
        # Prices: 0.08–0.45 €/kWh with realistic daily shape noise
        price = [rng.uniform(0.08, 0.45) for _ in range(24)]
        return pv, load, price, soc_start

    def test_random_days_full_parity(self):
        """Full parity (schedule, kWh, EUR, infeasible) for N=20 random days (seeded).

        Uses eta_charge=1.0 (exact bin arithmetic).  Any mismatch indicates a
        divergence between the DP implementations.
        """
        rng = random.Random(self.SEED)
        cfg = make_cfg()

        for i in range(self.N_DAYS):
            pv, load, price, soc_start = self._random_day(rng)
            opt, hind = call_both(pv, load, price, soc_start, cfg)
            assert_parity(opt, hind, tol=1e-6, label=f"random_day_{i:02d}")

    def test_random_days_nonunit_eta_parity(self):
        """Parity holds with eta_charge=0.92 (realistic non-unit efficiency).

        Non-unit eta exercises the AC↔DC conversion paths in both optimizers.
        """
        rng = random.Random(self.SEED + 1)
        cfg = make_cfg(eta_charge=0.92)

        for i in range(self.N_DAYS):
            pv, load, price, soc_start = self._random_day(rng)
            opt, hind = call_both(pv, load, price, soc_start, cfg)
            assert_parity(opt, hind, tol=1e-6, label=f"eta092_day_{i:02d}")

    def test_random_days_larger_battery_parity(self):
        """Parity holds with a larger battery (20 kWh capacity, realistic defaults).

        Tests that n_states scaling doesn't introduce a divergence.
        """
        rng = random.Random(self.SEED + 2)
        cfg = make_cfg(
            capacity_kwh=20.0,
            soc_floor=10.0,   # 2 kWh floor
            soc_target=90.0,  # 18 kWh target
            max_charge_w=7000.0,  # 7 kWh/h
            eta_charge=1.0,
        )
        for i in range(self.N_DAYS):
            pv, load, price, soc_start = self._random_day(rng)
            opt, hind = call_both(pv, load, price, soc_start, cfg)
            assert_parity(opt, hind, tol=1e-6, label=f"20kwh_day_{i:02d}")

    def test_random_days_with_infeasible_coverage(self):
        """Infeasible-parity coverage in the property loop.

        Uses a starved max_charge_w (80W = 0.08 kWh/h) so that a fraction of
        random days are infeasible (max achievable ≤ 2+24×0.08=3.92 kWh < 8 kWh
        target), exercising infeasible-path parity at scale.
        """
        rng = random.Random(self.SEED + 3)
        cfg = make_cfg(max_charge_w=80.0)  # 0.08 kWh/h → most days infeasible
        for i in range(self.N_DAYS):
            pv, load, price, soc_start = self._random_day(rng)
            opt, hind = call_both(pv, load, price, soc_start, cfg)
            assert_parity(opt, hind, tol=1e-6, label=f"infeasible_random_{i:02d}")


# ---------------------------------------------------------------------------
# Parity-critical branch tests: tie-break and floor-boundary
# ---------------------------------------------------------------------------


class TestParityTiebreakerAndBoundary:
    """Targeted tests for the two parity-critical branches.

    The DP comment (optimize.py and regret.py) explicitly warns that strict ``<``
    tie-breaking is load-bearing for T0.1b parity.  Continuous-uniform random
    inputs virtually never produce exact cost ties, so the property loops cannot
    detect a ``< → <=`` mutation.  These tests manufacture the divergence
    deliberately and verify it is caught.
    """

    def test_flat_price_tiebreak_resolution(self):
        """Flat price → every single-charge-hour assignment has identical EUR.

        soc_start=50% (5 kWh), no PV, no load, price=[0.20]*24, deficit=3 kWh.
        Under strict-< tie-break both implementations resolve the tie to the
        SAME per-hour schedule.  A ``< → <=`` mutation in optimize_grid would
        reverse the tie resolution (last writer wins → first writer wins,
        flipping the charge from the last feasible hour to the first), causing
        an elementwise schedule mismatch detected by assert_parity.
        """
        cfg = make_cfg()
        pv    = [0.0] * 24
        load  = [0.0] * 24
        price = [0.20] * 24  # perfectly flat — any single charge-hour ties on EUR
        opt, hind = call_both(pv, load, price, soc_start=50.0, cfg=cfg)
        assert_parity(opt, hind, tol=1e-6, label="flat_price_tiebreak")
        # EUR must be exactly 3 kWh × 0.20 €/kWh
        assert opt["eur"] == pytest.approx(0.60, abs=1e-6)
        assert hind["eur"] == pytest.approx(0.60, abs=1e-6)

    def test_flat_price_partial_charge_tiebreak(self):
        """Multiple partial-charge paths tie on EUR — verifies tie-break consistency.

        soc_start=65% (6.5 kWh), no PV, no load, flat price.  Deficit=1.5 kWh.
        max_charge=3 kWh/h > deficit so the optimal single-hour charge exists.
        Under flat price, spreading the 1.5 kWh across any set of hours ties.
        Both implementations must agree on the same schedule elementwise.
        """
        cfg = make_cfg()
        pv    = [0.0] * 24
        load  = [0.0] * 24
        price = [0.15] * 24
        opt, hind = call_both(pv, load, price, soc_start=65.0, cfg=cfg)
        assert_parity(opt, hind, tol=1e-6, label="flat_price_partial_tiebreak")
        assert opt["eur"] == pytest.approx(hind["eur"], abs=1e-6)

    def test_floor_boundary_exact_landing(self):
        """soc_after lands exactly on floor_kwh — floor-epsilon guard admits it.

        soc_start=40% (4 kWh), load=2.0 kWh at h0 (only).
        soc_after = 4.0 − 2.0 = 2.0 kWh = exactly floor_kwh (2.0 kWh).
        The guard ``new_soc < floor_kwh - 1e-9`` admits g_dc=0 (new_soc==floor)
        and positive g_dc.  A ``- 1e-9`` → ``0`` mutation would still pass here
        (since 2.0 < 2.0 is False in both variants); parity confirms identical
        treatment of the boundary in both implementations.
        """
        cfg = make_cfg()
        pv    = [0.0] * 24
        load  = [2.0] + [0.0] * 23  # h0 spike drives soc to exactly floor
        price = [0.15] * 24
        opt, hind = call_both(pv, load, price, soc_start=40.0, cfg=cfg)
        assert_parity(opt, hind, tol=1e-6, label="floor_boundary_exact")

    def test_below_floor_transition_rejected(self):
        """Transitions that land below floor are rejected in both implementations.

        soc_start=25% (2.5 kWh), load=1.0 kWh/h.  At h0: soc_after=1.5 kWh.
        g_dc=0 → new_soc=1.5 < floor(2.0) → rejected.
        g_dc=0.05 → new_soc=1.55 < floor(2.0) → rejected.
        ...
        g_dc=0.50 → new_soc=2.00 ≥ floor → accepted.
        The DP must charge at least 0.5 kWh at h0 to stay feasible.
        Both implementations must agree on the minimum-cost schedule.
        """
        cfg = make_cfg()
        pv    = [0.0] * 24
        load  = [1.0] + [0.0] * 23
        price = [0.20] * 24
        opt, hind = call_both(pv, load, price, soc_start=25.0, cfg=cfg)
        assert_parity(opt, hind, tol=1e-6, label="floor_enforcement")


# ---------------------------------------------------------------------------
# Export-leg helpers (shared by TestExportOnParity below)
# ---------------------------------------------------------------------------


def _make_export_cfg(**overrides) -> Config:
    """Export-leg Config: unit eta_charge + unit round_trip_eff so eta_d=1.0.

    With eta_charge=1.0, eta_d = min(round_trip_eff / eta_charge, 1.0) = 1.0,
    so AC export == DC kWh discharged.  This simplifies the arithmetic and makes
    gap analysis easy (no efficiency haircut on either side).
    """
    defaults = dict(
        capacity_kwh=10.0,
        soc_floor=20.0,      # 2 kWh floor
        soc_target=80.0,     # 8 kWh target
        max_charge_w=3000.0, # 3 kWh/h
        eta_charge=1.0,      # AC == DC on charge side
        round_trip_eff=1.0,  # AC == DC on discharge side (eta_d = 1.0)
        cycle_cost_eur_per_kwh=0.04,
        max_export_w=3000.0,
        grid_export_limit_w=3000.0,
    )
    defaults.update(overrides)
    return Config(**defaults)


def _oracle_export_revenue(
    pv: list[float],
    load: list[float],
    price: list[float],
    export_price: list[float],
    soc_start: float,
    cfg: Config,
) -> float:
    """Run oracle (hindsight DP with export) and return net export_revenue_eur."""
    day = DayData(
        pv_kwh=tuple(pv),
        load_kwh=tuple(load),
        price=tuple(price),
        soc_start=soc_start,
    )
    result = hindsight_optimal_grid(day, cfg, export_price=export_price)
    return result["export_revenue_eur"]


# ---------------------------------------------------------------------------
# Export-ON parity: optimize_grid ≡ oracle when export_price is supplied
# ---------------------------------------------------------------------------


def _call_both_export(pv, load, price, export_price, soc_start, cfg, *, terminal_mode="reserve", water_value=None):
    """Invoke both optimizers with export_price and return (opt, hind)."""
    assert len(pv) == len(load) == len(price) == len(export_price) == 24
    day = DayData(pv_kwh=tuple(pv), load_kwh=tuple(load), price=tuple(price), soc_start=soc_start)
    hind = hindsight_optimal_grid(day, cfg, terminal_mode=terminal_mode, water_value=water_value, export_price=export_price)
    opt = optimize_grid(
        pv, load, price, soc_start=soc_start, cfg=cfg,
        window_start_h=0, window_len=24, export_price=export_price,
        terminal_mode=terminal_mode, water_value=water_value,
    )
    return opt, hind


def assert_export_parity(opt, hind, *, tol=1e-6, label=""):
    """optimize_grid ≡ oracle on the export leg (no solar spill in these scenarios)."""
    assert_parity(opt, hind, tol=tol, label=label)  # schedule/kwh/eur/infeasible
    assert opt["export_kwh"] == pytest.approx(hind["export_kwh"], abs=tol), f"[{label}] export_kwh"
    assert opt["export_revenue_eur"] == pytest.approx(hind["export_revenue_eur"], abs=tol), f"[{label}] export_revenue_eur"
    for h in range(24):
        assert opt["export_schedule"][h] == pytest.approx(hind["export_schedule"][h], abs=tol), (
            f"[{label}] export_schedule[{h}] opt={opt['export_schedule'][h]} hind={hind['export_schedule'][h]}"
        )


class TestExportOnParity:
    """optimize_grid byte-identical to the oracle when export_price is supplied (pv=0 → no spill)."""

    def test_single_peak_export_reserve_mode(self):
        cfg = _make_export_cfg()
        pv = [0.0] * 24
        load = [0.0] * 24
        price = [0.20] * 24
        export_price = [0.0] * 24
        export_price[18] = 0.50  # one rich evening hour clears the hurdle
        opt, hind = _call_both_export(pv, load, price, export_price, soc_start=80.0, cfg=cfg)
        assert opt["export_kwh"] > 0.0, "expected the DP to export into the peak hour"
        assert_export_parity(opt, hind, label="single_peak_reserve")

    def test_two_peaks_water_value_mode(self):
        cfg = _make_export_cfg()
        pv = [0.0] * 24
        load = [0.3] * 24
        price = [0.10] * 6 + [0.20] * 18      # cheap pre-dawn trough
        export_price = [0.0] * 24
        export_price[19] = 0.55
        export_price[20] = 0.45
        opt, hind = _call_both_export(
            pv, load, price, export_price, soc_start=70.0, cfg=cfg,
            terminal_mode="water_value", water_value=0.10,
        )
        assert_export_parity(opt, hind, label="two_peaks_wv")

    def test_reserve_floor_export_parity(self):
        """Identical reserve_by_hour to both DPs stays byte-identical (mirror lock)."""
        cfg = _make_export_cfg()
        pv = [0.0] * 24
        load = [0.0] * 24
        price = [0.20] * 24
        export_price = [0.0] * 24
        export_price[18] = 0.55
        reserve = [4.0] * 24  # 40% reserve floor every hour
        day = DayData(pv_kwh=tuple(pv), load_kwh=tuple(load), price=tuple(price), soc_start=80.0)
        hind = hindsight_optimal_grid(day, cfg, export_price=export_price, reserve_by_hour=reserve)
        opt = optimize_grid(
            pv, load, price, soc_start=80.0, cfg=cfg, window_start_h=0, window_len=24,
            export_price=export_price, reserve_by_hour=reserve,
        )
        assert_export_parity(opt, hind, label="reserve_floor_parity")


# ---------------------------------------------------------------------------
# Solar-reservation ceiling parity
# ---------------------------------------------------------------------------


class TestSolarReservationParity:
    """optimize_grid ≡ hindsight_optimal_grid when the SAME ceiling is passed to both."""

    def test_ceiling_parity_solar_rich(self):
        cfg = make_cfg()
        pv    = [0.0] * 8 + [2.5] * 8 + [0.0] * 8   # ceiling binds midday
        load  = [0.5] * 24
        price = [0.20] * 24
        ceil = solar_reservation_ceiling(pv, load, cfg)   # single-cycle, no reserve param
        day = DayData(pv_kwh=tuple(pv), load_kwh=tuple(load),
                      price=tuple(price), soc_start=20.0)
        hind = hindsight_optimal_grid(day, cfg, grid_charge_ceiling=ceil)
        opt = optimize_grid(pv, load, price, soc_start=20.0, cfg=cfg,
                            window_start_h=0, window_len=24, grid_charge_ceiling=ceil)
        assert_parity(opt, hind, label="ceiling_parity_solar_rich")

    def test_ceiling_with_reserve_and_export_parity(self):
        """Ceiling (no reserve) AND reserve_by_hour (export floor) together stay byte-identical."""
        cfg = _make_export_cfg()
        pv    = [0.0] * 6 + [2.0] * 6 + [0.0] * 12
        load  = [0.3] * 24
        price = [0.20] * 24
        export_price = [0.0] * 24
        export_price[19] = 0.55
        reserve = [3.0] * 24                                # export discharge floor (NOT the ceiling)
        ceil = solar_reservation_ceiling(pv, load, cfg)     # ceiling derived without reserve
        day = DayData(pv_kwh=tuple(pv), load_kwh=tuple(load),
                      price=tuple(price), soc_start=60.0)
        hind = hindsight_optimal_grid(day, cfg, export_price=export_price,
                                      reserve_by_hour=reserve, grid_charge_ceiling=ceil)
        opt = optimize_grid(pv, load, price, soc_start=60.0, cfg=cfg,
                            window_start_h=0, window_len=24, export_price=export_price,
                            reserve_by_hour=reserve, grid_charge_ceiling=ceil)
        assert_export_parity(opt, hind, label="ceiling_reserve_export_parity")


# ---------------------------------------------------------------------------
# H1: combined-AC export cap parity (binding scenarios)
# ---------------------------------------------------------------------------


class TestExportCapBindingParity:
    """H1: optimize_grid ≡ oracle when the combined-AC export cap BINDS.

    Before the regret mirror, hindsight_optimal_grid ignores ac_cap and over-
    exports battery; assert_export_parity then fails on export_kwh/schedule.
    After the mirror both DPs agree.  Each scenario makes the cap bind (asserted
    explicitly) so the test cannot pass trivially.
    """

    def test_export_cap_binds_via_grid_limit(self):
        # grid_export_limit_w (1000) < max_export_w (3000) → ac_cap = 1.0 kWh AC/h.
        # Single rich hour: without the cap the oracle exports up to max_export_dc_h
        # (3.0 kWh); optimize_grid caps battery export at 1.0 kWh.
        cfg = _make_export_cfg(grid_export_limit_w=1000.0)  # max_export_w stays 3000
        assert cfg.grid_export_limit_w < cfg.max_export_w     # binding condition holds
        pv = [0.0] * 24
        load = [0.0] * 24
        price = [0.20] * 24
        export_price = [0.0] * 24
        export_price[18] = 0.50                                # one rich evening hour
        opt, hind = _call_both_export(pv, load, price, export_price, soc_start=80.0, cfg=cfg)
        # Cap BINDS: hour-18 battery export limited to ac_cap (1.0 kWh AC), not 3.0.
        assert opt["export_schedule"][18] == pytest.approx(1.0, abs=1e-6)
        assert_export_parity(opt, hind, label="export_cap_grid_limit")

    def test_export_cap_binds_via_solar_spill(self):
        # PV surplus in the export hour saturates the AC export cap on its own:
        # solar_export_ac (3.0) >= ac_cap (3.0) → batt_ac_headroom = 0 → battery
        # export fully blocked.  Without the mirror the oracle still discharges the
        # battery on top of the spill, exceeding the grid AC cap.
        cfg = _make_export_cfg()                              # max == grid == 3000 → ac_cap = 3.0
        pv = [0.0] * 24
        pv[18] = 3.0                                          # 3 kWh AC PV at the peak hour
        load = [0.0] * 24
        price = [0.20] * 24
        export_price = [0.0] * 24
        export_price[18] = 0.50
        opt, hind = _call_both_export(pv, load, price, export_price, soc_start=80.0, cfg=cfg)
        # Cap BINDS: solar already fills the AC export budget → 0 battery export at h18.
        assert opt["export_schedule"][18] == pytest.approx(0.0, abs=1e-6)
        assert_export_parity(opt, hind, label="export_cap_solar_spill")


# ---------------------------------------------------------------------------
# charge_margin_eur_per_kwh: per-DC-kWh economic penalty on grid CHARGE
# ---------------------------------------------------------------------------


class TestChargeMarginEurPerKwh:
    """cfg.charge_margin_eur_per_kwh gates grid CHARGE behind an arbitrage
    hurdle: the DP only imports-to-battery when the saving/revenue clears this
    per-DC-kWh margin.  Default 0.0 is a no-op (byte parity with every existing
    scenario is unaffected).  The margin term is added IDENTICALLY to the
    forward DP (optimize_grid) and the hindsight oracle (hindsight_optimal_grid)
    — both the per-step transition cost AND the reconstructed eur billing — or
    the T0.1b parity gate breaks the moment margin > 0.
    """

    def test_parity_holds_with_charge_margin(self):
        """Locks the matched pair: margin > 0 must still be byte-parity-exact.

        Reuses the low-PV/high-load scenario (DP must actively charge in the
        cheapest overnight hours), so the margin term is actually exercised on
        a non-trivial charge schedule, not just a zero-charge no-op.
        """
        cfg = make_cfg(charge_margin_eur_per_kwh=0.05)
        pv = [0.0] * 9 + [0.5] * 4 + [0.0] * 11
        load = [1.0] * 24
        price = [
            0.30, 0.28, 0.25, 0.22, 0.20, 0.18,  # 00-05 cheapest
            0.22, 0.28, 0.32, 0.35, 0.33, 0.31,  # 06-11
            0.29, 0.27, 0.25, 0.28, 0.32, 0.35,  # 12-17
            0.38, 0.40, 0.36, 0.32, 0.28, 0.25,  # 18-23 evening peak
        ]
        opt, hind = call_both(pv, load, price, soc_start=60.0, cfg=cfg)
        assert sum(opt["schedule"]) > 0.0, "fixture sanity: DP must actually charge"
        assert_parity(opt, hind, tol=1e-6, label="charge_margin_parity")

    def test_margin_blocks_marginal_charge_but_not_profitable_arbitrage(self):
        """Routing proof — the margin changes WHAT gets scheduled, not just the bill.

        Scenario A (marginal, ~0.004 EUR/kWh advantage pre-margin): eta_charge
        = 1.0 (no efficiency haircut), so charging 1 kWh at 0.310 to avoid a
        1 kWh below-floor direct import later (priced 1:1 at 0.314) saves
        exactly 0.004/kWh.  Without a margin the DP takes that razor-thin
        saving and pre-charges; a 0.04 margin overwhelms it (effective charge
        cost 0.310 + 0.04 = 0.350 > 0.314), so the DP should instead ride to
        the floor and pay the (now cheaper) direct import — schedule ~= 0.

        Scenario B (genuinely profitable, ~0.11 EUR/kWh net after the SAME
        margin): charge at 0.24, export at 0.43 (eta_charge = eta_discharge =
        1.0, cycle_cost = 0.04).  Net profit per kWh even after the 0.04
        margin is 0.43 - 0.04(cycle) - 0.24(buy) - 0.04(margin) = 0.11 > 0, so
        the DP should still schedule the charge.

        Both use terminal_mode="water_value" with water_value=0.0 so the ONLY
        incentive to charge is the arbitrage itself (no mandatory reserve).
        """
        margin = 0.04

        # --- Scenario A: marginal charge-ahead-of-floor-import ---
        cfg_a = Config(
            capacity_kwh=10.0,
            soc_floor=20.0,   # 2 kWh floor
            soc_target=80.0,  # headroom only; unreachable/irrelevant in 2h
            max_charge_w=3000.0,
            eta_charge=1.0,
            charge_margin_eur_per_kwh=margin,
        )
        pv_a = [0.0, 0.0]
        load_a = [0.0, 1.0]
        price_a = [0.310, 0.314]  # buy now vs. displaced below-floor import later
        res_a = optimize_grid(
            pv_a, load_a, price_a, soc_start=20.0, cfg=cfg_a,
            window_start_h=0, window_len=2,
            terminal_mode="water_value", water_value=0.0,
        )
        assert res_a["schedule"][0] == pytest.approx(0.0, abs=1e-9), (
            f"marginal charge should be blocked by the margin, got {res_a['schedule']}"
        )

        # --- Scenario B: genuinely profitable trough-charge-to-export ---
        cfg_b = Config(
            capacity_kwh=10.0,
            soc_floor=20.0,
            soc_target=80.0,
            max_charge_w=3000.0,
            max_export_w=3000.0,
            grid_export_limit_w=3000.0,
            eta_charge=1.0,
            round_trip_eff=1.0,        # eta_discharge = 1.0
            cycle_cost_eur_per_kwh=0.04,
            charge_margin_eur_per_kwh=margin,
        )
        pv_b = [0.0, 0.0]
        load_b = [0.0, 0.0]
        price_b = [0.24, 0.20]
        export_price_b = [0.0, 0.43]
        res_b = optimize_grid(
            pv_b, load_b, price_b, soc_start=20.0, cfg=cfg_b,
            window_start_h=0, window_len=2,
            terminal_mode="water_value", water_value=0.0,
            export_price=export_price_b,
        )
        assert res_b["schedule"][0] > 0.0, (
            "profitable trough charge should still clear the margin"
        )


# ---------------------------------------------------------------------------
# Oracle eta_curve threading smoke test (T8)
# ---------------------------------------------------------------------------


def test_oracle_curve_smoke_self_consistent():
    """hindsight_optimal_grid accepts eta_curve and stays self-consistent.

    eta_curve=None (default) is the byte-identical parity path (proven by the
    rest of this module).  This is a smoke test that the curve-ON branch
    (eta_curve=EfficiencyCurve.static(cfg), which reproduces the SAME scalar
    etas as the None path via per-bin fallback values) runs to completion and
    yields finite, sane output.
    """
    from custom_components.anker_x1_smartgrid.efficiency import EfficiencyCurve

    cfg = make_cfg(eta_charge=0.92)
    curve = EfficiencyCurve.static(cfg)
    pv = [0.0] * 24
    load = [1.0] * 24
    price = [0.20] * 24
    day = DayData(pv_kwh=tuple(pv), load_kwh=tuple(load), price=tuple(price), soc_start=60.0)
    off = hindsight_optimal_grid(day, cfg)
    on = hindsight_optimal_grid(day, cfg, eta_curve=curve)
    assert all(v == v for v in on["schedule"])  # no NaNs
    assert off["eur"] == off["eur"]  # no NaN
    assert on["eur"] == on["eur"]  # no NaN


# ---------------------------------------------------------------------------
# optimize_grid eta_curve threading (T9) — mirror of the T8 oracle test
# ---------------------------------------------------------------------------


def test_optimize_grid_accepts_eta_curve_kwarg_none_is_identical():
    """eta_curve=None (default) must be byte-identical to omitting it."""
    cfg = make_cfg(eta_charge=0.92)
    pv = [0.0] * 24
    load = [1.0] * 24
    price = [0.20] * 24
    base = optimize_grid(pv, load, price, soc_start=60.0, cfg=cfg,
                         window_start_h=0, window_len=24)
    with_none = optimize_grid(pv, load, price, soc_start=60.0, cfg=cfg,
                              window_start_h=0, window_len=24, eta_curve=None)
    assert base["schedule"] == with_none["schedule"]
    assert base["eur"] == with_none["eur"]


# ---------------------------------------------------------------------------
# T10: flag-ON 0-regret matched-pair property gate for the eta curve
# ---------------------------------------------------------------------------


class TestCurveOnMatchedPair:
    """optimize_grid ≡ hindsight_optimal_grid when the SAME eta_curve is passed."""

    def _curve(self, cfg):
        from custom_components.anker_x1_smartgrid.efficiency import EfficiencyCurve, BinStat
        base = EfficiencyCurve.static(cfg)
        charge = [BinStat(b.lo_w, b.hi_w, "charge", 0.85 + 0.02 * i, 0.85, 99, 9.0, True, "")
                  for i, b in enumerate(base._charge)]
        discharge = [BinStat(b.lo_w, b.hi_w, "discharge", 0.80 + 0.03 * i, 0.80, 99, 9.0, True, "")
                     for i, b in enumerate(base._discharge)]
        return EfficiencyCurve(charge, discharge, base._fc, base._fd)

    def test_charge_leg_zero_regret(self):
        cfg = make_cfg(eta_charge=0.92)
        curve = self._curve(cfg)
        pv = [0.0] * 9 + [0.5] * 4 + [0.0] * 11
        load = [1.0] * 24
        price = [0.30, 0.28, 0.25, 0.22, 0.20, 0.18] + [0.30] * 18
        day = DayData(pv_kwh=tuple(pv), load_kwh=tuple(load), price=tuple(price), soc_start=60.0)
        hind = hindsight_optimal_grid(day, cfg, eta_curve=curve)
        opt = optimize_grid(pv, load, price, soc_start=60.0, cfg=cfg,
                            window_start_h=0, window_len=24, eta_curve=curve)
        assert_parity(opt, hind, tol=1e-6, label="curve_on_charge")

    def test_export_leg_zero_regret(self):
        cfg = _make_export_cfg()
        curve = self._curve(cfg)
        pv = [0.0] * 24
        load = [0.3] * 24
        price = [0.20] * 24
        export_price = [0.0] * 24
        export_price[18] = 0.55
        day = DayData(pv_kwh=tuple(pv), load_kwh=tuple(load), price=tuple(price), soc_start=80.0)
        hind = hindsight_optimal_grid(day, cfg, export_price=export_price, eta_curve=curve)
        opt = optimize_grid(pv, load, price, soc_start=80.0, cfg=cfg, window_start_h=0,
                            window_len=24, export_price=export_price, eta_curve=curve)
        assert_export_parity(opt, hind, label="curve_on_export")


# ---------------------------------------------------------------------------
# P1: per-step discharge eta in the export leg — bin-crossing parity
# ---------------------------------------------------------------------------


def _two_bin_discharge_curve(cfg, *, lo_eta, hi_eta, split_w):
    """EfficiencyCurve whose discharge eta is lo_eta below split_w, hi_eta at/above.
    Charge eta stays flat at cfg.eta_charge. Bin edges = const.EFFICIENCY_DC_BIN_EDGES_W
    = [400,800,1500,2500,4000] (6 bins)."""
    from custom_components.anker_x1_smartgrid.efficiency import EfficiencyCurve, BinStat
    from custom_components.anker_x1_smartgrid import const
    edges = const.EFFICIENCY_DC_BIN_EDGES_W
    los, his = [0.0] + edges, edges + [float("inf")]
    disc = [BinStat(lo, hi, "discharge", (lo_eta if lo < split_w else hi_eta),
                    None, 5, 1.0, True, "measured") for lo, hi in zip(los, his)]
    chg = [BinStat(lo, hi, "charge", cfg.eta_charge, None, 5, 1.0, True, "measured")
           for lo, hi in zip(los, his)]
    return EfficiencyCurve(chg, disc, cfg.eta_charge,
                           min(cfg.round_trip_eff / cfg.eta_charge, 1.0))


class TestExportEtaCurveParity:
    """P1: with a measured eta_curve, optimize_grid's export leg must route AND
    report on per-step eta so it stays == the oracle when the optimal export lands
    in a NON-top efficiency bin (different from max_export_w's bin)."""

    def test_export_leg_eta_curve_bin_crossing_parity(self):
        from custom_components.anker_x1_smartgrid.efficiency import bin_index
        cfg = _make_export_cfg()          # cap 10, floor 20% (2 kWh), max_export_w=3000
        curve = _two_bin_discharge_curve(cfg, lo_eta=0.98, hi_eta=0.80, split_w=1500.0)
        pv    = [0.0] * 24
        load  = [0.0] * 24
        # NOTE ON PRICE (deviation from the plan's flat 0.20 — verified empirically):
        # at 0.20, grid-arbitrage recharge is profitable in EITHER efficiency bin
        # (0.55*0.98-0.04=0.499 and 0.55*0.80-0.04=0.40 both clear 0.20), so the DP
        # always recharges up to the AC-cap-sized export — which, by construction,
        # sits in max_export_w's OWN bin (bin 4) regardless of per-step awareness.
        # opt and the oracle then converge on the SAME bin-4 number even pre-fix,
        # so the parity assertion cannot distinguish the bug at that price.
        # 0.45 sits BETWEEN the two per-kWh margins: recharging past the free
        # ~1 kWh headroom is still worth it while the extra export stays in the
        # low-eta bin (0.499 > 0.45) but stops being worth it once it crosses into
        # the high-eta bin (0.40 < 0.45). The true optimum is then JUST BELOW the
        # bin split (~1.45 kWh DC, bin 2) — genuinely different from max_export_w's
        # bin 4 — so a flat-eta router (pre-fix) and a per-step router disagree on
        # both how much to export AND how much to recharge for it.
        price = [0.45] * 24
        export_price = [0.0] * 24
        export_price[18] = 0.55
        # soc_start=30% (3 kWh) with a 2 kWh floor ⇒ ~1 kWh exportable ⇒ ~1000 W
        # per hour ⇒ discharge bin 2; max_export_w=3000 ⇒ bin 4 ⇒ DIFFERENT bins.
        assert bin_index(1000.0) != bin_index(cfg.max_export_w)
        day = DayData(pv_kwh=tuple(pv), load_kwh=tuple(load), price=tuple(price), soc_start=30.0)
        hind = hindsight_optimal_grid(day, cfg, export_price=export_price,
                                      terminal_mode="water_value", water_value=0.0, eta_curve=curve)
        opt = optimize_grid(pv, load, price, soc_start=30.0, cfg=cfg,
                            window_start_h=0, window_len=24, export_price=export_price,
                            terminal_mode="water_value", water_value=0.0, eta_curve=curve)
        assert opt["export_schedule"][18] > 0.0      # export actually fires (non-vacuous)
        assert_export_parity(opt, hind, label="export_eta_curve_bin_crossing")


# ---------------------------------------------------------------------------
# T3: DP<->oracle parity sweep across live cfg permutations (Task 11)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("overrides,dt_h", [
    ({"reserve_anchor": "ride_to_trough"}, 1.0),   # live default anchor (DP-invariant)
    ({"export_peak_lookback_h": 4.0}, 1.0),         # windowed peak w/ day_index
    ({"soc_hedge_fraction": 0.5}, 1.0),             # controller-level; DP-invariant
    ({}, 0.25),                                      # 15-min slot resolution
])
def test_parity_holds_across_live_cfg_permutations(overrides, dt_h):
    """T3: parity must hold across live cfg permutations that reach production
    but were never swept together: the live-default reserve_anchor, a non-zero
    export peak look-back, a non-zero SoC drift-hedge fraction, and 15-min slot
    resolution.

    reserve_anchor / soc_hedge_fraction are controller-level knobs that never
    enter optimize_grid / hindsight_optimal_grid directly -- swept here to PIN
    that DP-invariance (a future wiring of either into the DP must not
    silently break parity). export_peak_lookback_h and dt_h=0.25 ARE
    substantive: both DPs derive the windowed export-peak band from
    cfg.export_peak_lookback_h / dt_h (optimize.py's export routing and
    regret.py:413).

    Base cfg is _make_export_cfg (has max_export_w / cycle_cost) so every
    permutation is a non-vacuous export scenario -- a make_cfg-based scenario
    would pass parity trivially on an empty export schedule.
    """
    cfg = _make_export_cfg(**overrides)
    pv    = [0.0] * 24
    load  = [0.0] * 24
    price = [0.20] * 24
    export_price = [0.0] * 24
    export_price[18] = 0.55
    day = DayData(pv_kwh=tuple(pv), load_kwh=tuple(load), price=tuple(price), soc_start=80.0)
    hind = hindsight_optimal_grid(day, cfg, export_price=export_price,
                                  terminal_mode="water_value", water_value=0.0, dt_h=dt_h)
    opt = optimize_grid(pv, load, price, soc_start=80.0, cfg=cfg,
                        window_start_h=0, window_len=24, export_price=export_price,
                        terminal_mode="water_value", water_value=0.0, dt_h=dt_h)
    assert sum(hind["export_schedule"]) > 0.0     # sweep is non-vacuous
    assert_export_parity(opt, hind, label=f"parity[{overrides},dt={dt_h}]")
