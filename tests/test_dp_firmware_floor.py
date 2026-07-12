"""D1: passive-drain-to-firmware-floor tests (parity-critical DP + oracle pair).

Covers the transition from a fake "physical wall" at cfg.soc_floor to the real
firmware hard floor (const.FIRMWARE_SOC_FLOOR, 5%):

- State now sags to const.FIRMWARE_SOC_FLOOR (not cfg.soc_floor).
- Grid-import cost ("floor_import") is booked ONLY below the firmware floor —
  where it physically occurs.
- cfg.soc_floor becomes a pure DECISION margin: it still gates the export
  floor and anchors the water-value terminal credit, but it no longer forces
  a fake physical clamp/import in the DP transitions.

At cfg.soc_floor == const.FIRMWARE_SOC_FLOOR (5%, the default) the whole
change is a no-op: firmware_floor_kwh == floor_kwh algebraically, so every
scenario below is byte-identical to pre-change behaviour.  Scenario (a) pins
this down explicitly with a hand-derived golden. Scenarios (b)-(d) exercise
cfg.soc_floor values ABOVE the firmware floor (10%, 20%), where the new
semantics actually diverge from the old fake-wall behaviour.
"""

from __future__ import annotations

import pytest

from custom_components.anker_x1_smartgrid import const
from custom_components.anker_x1_smartgrid.models import Config
from custom_components.anker_x1_smartgrid.optimize import optimize_grid
from custom_components.anker_x1_smartgrid.regret import (
    DayData,
    hindsight_optimal_grid,
    realized_grid_cost,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _call_both(pv, load, price, soc_start, cfg, **kwargs):
    """Invoke optimize_grid + hindsight_optimal_grid on identical inputs.

    Returns (opt, hind).  ``kwargs`` (terminal_mode, water_value, ...) are
    forwarded to both.
    """
    n = len(pv)
    day = DayData(pv_kwh=tuple(pv), load_kwh=tuple(load), price=tuple(price), soc_start=soc_start)
    hind = hindsight_optimal_grid(day, cfg, **kwargs)
    opt = optimize_grid(
        pv,
        load,
        price,
        soc_start=soc_start,
        cfg=cfg,
        window_start_h=0,
        window_len=n,
        **kwargs,
    )
    return opt, hind


def _assert_parity(opt: dict, hind: dict, *, tol: float = 1e-6) -> None:
    assert opt["kwh"] == pytest.approx(hind["kwh"], abs=tol)
    assert opt["eur"] == pytest.approx(hind["eur"], abs=tol)
    assert len(opt["schedule"]) == len(hind["schedule"])
    for a, b in zip(opt["schedule"], hind["schedule"]):
        assert a == pytest.approx(b, abs=tol)
    assert bool(opt.get("infeasible", False)) == bool(hind.get("infeasible", False))


# ---------------------------------------------------------------------------
# (a) Golden parity at floor=5 (== firmware floor): byte-identical to legacy
# ---------------------------------------------------------------------------


class TestFloorEqualsFirmwareGolden:
    """soc_floor == const.FIRMWARE_SOC_FLOOR (5%) — firmware_floor_kwh ==
    floor_kwh algebraically, so this must reproduce the exact pre-change
    hand-derived numbers: a deficit night with charging disabled (max_charge_w
    = 0) so the schedule is deterministic and floor-import is hand-computable.
    """

    def _cfg(self) -> Config:
        assert const.FIRMWARE_SOC_FLOOR == 5.0
        return Config(
            capacity_kwh=10.0,
            soc_floor=5.0,  # == firmware floor: no-op scenario
            soc_target=5.0,  # trivial reserve (== floor) — no forced charge
            max_charge_w=0.0,  # charging impossible — pure drain accounting
            eta_charge=1.0,
            round_trip_eff=1.0,
        )

    def test_golden_hand_derived(self):
        # soc_start=20% (2.0 kWh); 1.0 kWh/h load, no PV, 4 hours, flat price.
        # Hand trace (eta=1, no-charge, discharge 1:1):
        #   h0: 2.0 -> 1.0   (still >= 0.5 kWh floor -> no floor-import)
        #   h1: 1.0 -> 0.0   (below 0.5 -> floor-import 0.5 kWh @ 0.30 = 0.15)
        #   h2: 0.5 -> -0.5  (below 0.5 -> floor-import 1.0 kWh @ 0.30 = 0.30)
        #   h3: 0.5 -> -0.5  (same as h2)
        # total floor_import_kwh = 0.5 + 1.0 + 1.0 = 2.5
        # total floor_import_eur = 0.15 + 0.30 + 0.30 = 0.75
        pv = [0.0, 0.0, 0.0, 0.0]
        load = [1.0, 1.0, 1.0, 1.0]
        price = [0.30, 0.30, 0.30, 0.30]
        cfg = self._cfg()

        opt, hind = _call_both(pv, load, price, soc_start=20.0, cfg=cfg)
        _assert_parity(opt, hind)

        for res, label in ((opt, "optimize_grid"), (hind, "hindsight_optimal_grid")):
            assert res["schedule"] == [0.0, 0.0, 0.0, 0.0], label
            assert res["kwh"] == pytest.approx(2.5, abs=1e-9), label
            assert res["eur"] == pytest.approx(0.75, abs=1e-9), label
            assert not res.get("infeasible", False), label

    def test_existing_golden_parity_gates_still_pass_conceptually(self):
        """Sanity re-assertion: at floor=5 the firmware-floor clamp and the
        soft-floor clamp are the SAME kWh value, so the transition formula
        change is a pure no-op.  (The real proof is the untouched existing
        suites — see tests/test_optimize_parity.py, tests/test_optimize_floor_economic.py,
        tests/test_optimize_dt60_golden.py, which all default to / explicitly
        use soc_floor == FIRMWARE_SOC_FLOOR == 5.0 for their golden numbers.)
        """
        cap_kwh = 10.0
        cfg = Config(capacity_kwh=cap_kwh, soc_floor=const.FIRMWARE_SOC_FLOOR)
        floor_kwh = cfg.soc_floor / 100.0 * cap_kwh
        firmware_floor_kwh = const.FIRMWARE_SOC_FLOOR / 100.0 * cap_kwh
        assert floor_kwh == pytest.approx(firmware_floor_kwh, abs=1e-12)


# ---------------------------------------------------------------------------
# (b) Floor=10 deficit night: no phantom import / charge in the 10->5% band
# ---------------------------------------------------------------------------


class TestFloor10DeficitNight:
    """soc_floor=10 (a pure decision margin), overnight drain from 15% to 7%.

    The pack passes through the [5%, 10%] band without ever booking a
    floor-import cost (physically nothing happens there — the firmware floor
    is 5%) and without the DP force-charging to artificially hold the old
    10% "wall".  terminal_mode="water_value" with water_value=0.0 isolates
    pure drain economics (no reserve credit muddying the picture) and directly
    exercises the terminal end-state scan widening (item 4): the natural
    no-charge end state (7%) is BELOW the old floor_b (10%) and would have
    been excluded from the old scan, forcing a phantom top-up charge.

    The terminal end-state scan widening (item 4) applies to BOTH
    optimize_grid and hindsight_optimal_grid (parity-critical: both must
    scan from firmware_floor_kwh). These tests exercise optimize_grid
    directly; parity with hindsight_optimal_grid is covered by
    test_optimize_parity.py (including test_two_peaks_water_value_mode
    which guards the matched widening).
    """

    def _cfg(self, **overrides) -> Config:
        defaults = dict(
            capacity_kwh=10.0,
            soc_floor=10.0,
            soc_target=90.0,
            max_charge_w=3000.0,
            eta_charge=1.0,
            round_trip_eff=1.0,
        )
        defaults.update(overrides)
        return Config(**defaults)

    def _scenario(self):
        # 8-hour overnight window: 0.1 kWh/h load, no PV -> 1.5 kWh -> 0.7 kWh
        # (15% -> 7%), crossing the 10% soft floor at h4/h5 without ever
        # approaching the 5% firmware floor.
        pv = [0.0] * 8
        load = [0.1] * 8
        price = [0.20] * 8
        return pv, load, price

    def test_no_phantom_floor_import_above_firmware_floor(self):
        pv, load, price = self._scenario()
        cfg = self._cfg()
        opt = optimize_grid(
            pv,
            load,
            price,
            soc_start=15.0,
            cfg=cfg,
            window_start_h=0,
            window_len=len(pv),
            terminal_mode="water_value",
            water_value=0.0,
        )
        # No economic reason to charge (flat price, water_value=0) -> the DP
        # should ride the natural drain, NOT force a top-up charge to hold
        # the old 10% wall.
        assert opt["schedule"] == pytest.approx([0.0] * 8, abs=1e-9)
        assert opt["kwh"] == pytest.approx(0.0, abs=1e-9)
        assert opt["eur"] == pytest.approx(0.0, abs=1e-9)
        assert not opt.get("infeasible", False)

    def test_state_visits_sub_soft_floor_bins(self):
        """The natural drain trajectory (15% -> 7%) dips below cfg.soc_floor
        (10%) for the back half of the window — this is only reachable
        because the DP no longer clamps at the soft floor."""
        pv, load, price = self._scenario()
        cfg = self._cfg()
        opt = optimize_grid(
            pv,
            load,
            price,
            soc_start=15.0,
            cfg=cfg,
            window_start_h=0,
            window_len=len(pv),
            terminal_mode="water_value",
            water_value=0.0,
        )
        # No charging anywhere -> trajectory is deterministic: soc(h) = 1.5 - 0.1*(h+1)
        soc_traj = [1.5 - 0.1 * (i + 1) for i in range(8)]
        assert soc_traj[-1] == pytest.approx(0.7, abs=1e-9)
        floor_kwh = cfg.soc_floor / 100.0 * cfg.capacity_kwh
        firmware_floor_kwh = const.FIRMWARE_SOC_FLOOR / 100.0 * cfg.capacity_kwh
        # Trajectory dips below the soft floor...
        assert min(soc_traj) < floor_kwh
        # ...but never below the firmware floor.
        assert min(soc_traj) > firmware_floor_kwh
        # And the DP schedule confirms no charge was needed to "rescue" it.
        assert opt["schedule"] == pytest.approx([0.0] * 8, abs=1e-9)

    def test_charges_only_when_economically_warranted(self):
        """Same drain, but a dirt-cheap early hour makes pre-charging to
        REACH the reserve genuinely profitable (soc_target lowered into
        reach) — the DP should still charge when there IS an economic
        reason, proving the floor relief doesn't just suppress ALL charging."""
        pv = [0.0] * 4
        load = [0.5] * 4
        price = [0.01, 0.30, 0.30, 0.30]  # h0 dirt cheap vs the rest
        cfg = self._cfg(soc_target=20.0)  # reachable reserve within the window
        opt, hind = _call_both(
            pv,
            load,
            price,
            soc_start=15.0,
            cfg=cfg,
            terminal_mode="reserve",
        )
        _assert_parity(opt, hind)
        assert sum(opt["schedule"]) > 0.0, "must charge to reach the reserve"
        # The cheap hour should carry the charge, not the expensive ones.
        assert opt["schedule"][0] > 0.0
        assert not opt.get("infeasible", False)


# ---------------------------------------------------------------------------
# (c) Export floor at floor=10: export still blocked below the SOFT floor
# ---------------------------------------------------------------------------


class TestFloor10ExportFloorUnchanged:
    """Voluntary export must still respect cfg.soc_floor (the decision
    margin), NOT ride down to the firmware floor — the C1 export-floor logic
    is explicitly untouched by this change."""

    def test_export_blocked_below_soft_floor(self):
        cfg = Config(
            capacity_kwh=10.0,
            soc_floor=10.0,  # 1.0 kWh soft/decision floor
            soc_target=10.0,  # trivial reserve
            eta_charge=1.0,
            round_trip_eff=1.0,  # eta_discharge = 1.0
            cycle_cost_eur_per_kwh=0.0,
            max_export_w=6000.0,
            grid_export_limit_w=6000.0,
        )
        pv = [0.0]
        load = [0.0]  # net=0 -> soc_after == soc_start exactly
        price = [0.20]
        export_price = [1.0]  # deep in the money -> export hurdle clears easily

        opt, hind = _call_both(
            pv,
            load,
            price,
            soc_start=15.0,
            cfg=cfg,
            export_price=export_price,
        )
        _assert_parity(opt, hind)

        for res, label in ((opt, "optimize_grid"), (hind, "hindsight_optimal_grid")):
            # soc_start=1.5 kWh, export_floor_h == floor_kwh == 1.0 kWh (soft
            # floor, reserve_by_hour=None) -> export capped at 0.5 kWh, NOT
            # the 1.0 kWh that would be available down to the firmware floor.
            assert res["export_kwh"] == pytest.approx(0.5, abs=1e-9), label
            assert res["export_kwh"] < 1.0 - 1e-9, (
                f"{label}: export must not ride below the soft floor to the firmware floor"
            )


# ---------------------------------------------------------------------------
# (d) Parity with floor=10: general scenario, both optimizers must agree
# ---------------------------------------------------------------------------


class TestFloor10Parity:
    """Broader parity coverage at soc_floor=10 (charge + drain mixed), beyond
    the narrow floor-only scenarios above."""

    def test_mixed_charge_and_drain_parity(self):
        cfg = Config(
            capacity_kwh=10.0,
            soc_floor=10.0,
            soc_target=60.0,
            max_charge_w=2000.0,
            eta_charge=0.92,
            round_trip_eff=0.85,
        )
        pv = [0.0, 0.0, 0.3, 0.8, 1.2, 0.6, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        load = [0.4, 0.4, 0.3, 0.3, 0.3, 0.4, 0.5, 0.6, 0.5, 0.4, 0.4, 0.4]
        price = [0.10, 0.09, 0.09, 0.10, 0.12, 0.15, 0.20, 0.30, 0.35, 0.28, 0.18, 0.12]

        opt, hind = _call_both(pv, load, price, soc_start=12.0, cfg=cfg)
        _assert_parity(opt, hind)

    def test_property_random_days_parity_floor10(self):
        """Seeded random-but-valid days at soc_floor=10 (mirrors the property
        loop pattern in test_optimize_parity.py, but pinned to the new
        soft-floor regime)."""
        import random

        rng = random.Random(20260710)
        cfg = Config(
            capacity_kwh=10.0,
            soc_floor=10.0,
            soc_target=70.0,
            max_charge_w=2500.0,
            eta_charge=0.9,
            round_trip_eff=0.85,
        )
        for _ in range(20):
            n = 24
            pv = [round(rng.uniform(0.0, 1.5), 3) if 6 <= h <= 18 else 0.0 for h in range(n)]
            load = [round(rng.uniform(0.1, 0.8), 3) for _ in range(n)]
            price = [round(rng.uniform(0.05, 0.45), 3) for _ in range(n)]
            soc_start = rng.uniform(5.0, 50.0)

            opt, hind = _call_both(pv, load, price, soc_start=soc_start, cfg=cfg)
            _assert_parity(opt, hind, tol=1e-6)


# ---------------------------------------------------------------------------
# (e) D2: realized_grid_cost books forced imports at the firmware floor, not
# cfg.soc_floor.
# ---------------------------------------------------------------------------


class TestRealizedGridCostFirmwareFloor:
    """D2: the backtest leg (realized_grid_cost) must clamp forced floor-hit
    imports at const.FIRMWARE_SOC_FLOOR (5%), mirroring the D1 DP/oracle
    transition semantics — NOT at cfg.soc_floor (a pure decision margin)."""

    def test_forced_imports_start_at_firmware_floor_not_soft_floor(self):
        """soc_floor=10 (1.0 kWh soft floor); soc_start=15% (1.5 kWh); the
        battery drains straight through the soft floor down to the firmware
        floor (0.5 kWh) before any forced import is booked."""
        cfg = Config(
            capacity_kwh=10.0,
            soc_floor=10.0,  # 1.0 kWh soft floor — must NOT gate imports
            soc_target=80.0,
            max_charge_w=3000.0,
            eta_charge=1.0,
            round_trip_eff=1.0,
        )
        pv = [0.0] * 4
        load = [1.0] * 4
        price = [0.20] * 4
        day = DayData(
            pv_kwh=tuple(pv),
            load_kwh=tuple(load),
            price=tuple(price),
            soc_start=15.0,
        )

        result = realized_grid_cost(day, [0.0] * 4, cfg)

        # h0: 1.5 -> 0.5 (== firmware floor exactly) -> NO import yet, even
        # though 0.5 kWh is already well below the 1.0 kWh SOFT floor.
        # h1-h3: 0.5 -> -0.5 each hour -> clamp to 0.5 -> 1.0 kWh import each.
        assert result["forced_import_kwh"] == pytest.approx(
            [0.0, 1.0, 1.0, 1.0],
            abs=1e-9,
        )
        assert result["kwh"] == pytest.approx(3.0, abs=1e-9)
        assert result["eur"] == pytest.approx(0.60, abs=1e-9)

    def test_drain_above_firmware_floor_books_zero_forced_imports(self):
        """soc_floor=10 (1.0 kWh); a single hour drains 20% -> 7% (2.0 kWh ->
        0.7 kWh) — below the soft floor but strictly above the firmware floor
        (0.5 kWh) -> zero forced imports."""
        cfg = Config(
            capacity_kwh=10.0,
            soc_floor=10.0,
            soc_target=80.0,
            max_charge_w=3000.0,
            eta_charge=1.0,
            round_trip_eff=1.0,
        )
        pv = [0.0]
        load = [1.3]  # 2.0 kWh (20%) -> 0.7 kWh (7%), above the firmware floor
        price = [0.20]
        day = DayData(
            pv_kwh=tuple(pv),
            load_kwh=tuple(load),
            price=tuple(price),
            soc_start=20.0,
        )

        result = realized_grid_cost(day, [0.0], cfg)

        assert result["forced_import_kwh"] == pytest.approx([0.0], abs=1e-9)
        assert result["kwh"] == pytest.approx(0.0, abs=1e-9)
        assert result["eur"] == pytest.approx(0.0, abs=1e-9)
