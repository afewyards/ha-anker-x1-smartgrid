"""Two-segment overnight terminal-value credit in ``dp_common.select_end_state``.

See docs/superpowers/specs/2026-07-18-overnight-terminal-value-design.md
(spec econ-F4). ``select_end_state``'s ``water_value`` terminal mode credits
end-of-horizon SoC above the firmware floor at a single rate ``v``. This adds
a second, richer rate ``water_value_hi`` for the first ``overnight_need_kwh``
of that energy (the "must survive the night" slice); the surplus above that
stays priced at the original ``v``. The legacy single-rate behaviour (credit
anchored at the SOFT ``floor_kwh``) must stay byte-identical when
``water_value_hi`` is omitted/None -- this is a parity gate shared with
``optimize.optimize_grid`` / ``regret.hindsight_optimal_grid``.
"""

from __future__ import annotations

import pytest

from custom_components.anker_x1_smartgrid.dp_common import select_end_state
from custom_components.anker_x1_smartgrid.optimize import optimize_grid
from custom_components.anker_x1_smartgrid.regret import DayData, hindsight_optimal_grid
from tests.helpers import make_config

INF = float("inf")


def _bins(bin_kwh: float, n_states: int):
    """Simple to_bin/from_bin closures for a hand-picked bin width -- lets
    the unit tests below use round numbers instead of the real _BIN_KWH=0.05.
    ``select_end_state`` only depends on these callables, not on soc_bins().
    """

    def to_bin(soc: float) -> int:
        return max(0, min(n_states - 1, round(soc / bin_kwh)))

    def from_bin(b: int) -> float:
        return b * bin_kwh

    return to_bin, from_bin


class TestNoneVhiByteParity:
    """Legacy branch (water_value_hi is None) must stay byte-identical."""

    def test_direct_call_identical_with_and_without_new_kwargs(self):
        to_bin, from_bin = _bins(1.0, 11)
        dp = [0.1 * b for b in range(11)]
        dp[3] = INF  # a hole in the reachable set exercises the `continue`

        kwargs = dict(
            terminal_mode="water_value",
            water_value=0.07,
            firmware_floor_kwh=0.0,
            floor_kwh=2.0,
            target_kwh=8.0,
            to_bin=to_bin,
            from_bin=from_bin,
            n_states=11,
        )
        omitted = select_end_state(dp, **kwargs)
        explicit_none = select_end_state(dp, water_value_hi=None, overnight_need_kwh=0.0, **kwargs)
        assert omitted == explicit_none

    def test_optimize_oracle_parity_unaffected_by_new_signature(self):
        """Full-stack regression: optimize_grid <-> hindsight_optimal_grid
        parity (kwh/eur/schedule) for a live water_value scenario -- neither
        caller passes the two new params, so this pins that the added
        (unused) kwargs don't perturb the existing call path at all."""
        cfg = make_config(eta_charge=0.92)
        pv = [0.0] * 24
        load = [1.0] * 24
        price = [0.40] * 18 + [0.10] * 6
        soc_start = 22.0
        wv = 0.30

        day = DayData(pv_kwh=tuple(pv), load_kwh=tuple(load), price=tuple(price), soc_start=soc_start)
        hind = hindsight_optimal_grid(day, cfg, terminal_mode="water_value", water_value=wv)
        opt = optimize_grid(
            pv,
            load,
            price,
            soc_start=soc_start,
            cfg=cfg,
            window_start_h=0,
            window_len=24,
            terminal_mode="water_value",
            water_value=wv,
        )
        assert opt["kwh"] == pytest.approx(hind["kwh"], abs=1e-6)
        assert opt["eur"] == pytest.approx(hind["eur"], abs=1e-6)
        assert len(opt["schedule"]) == len(hind["schedule"]) == 24
        for h in range(24):
            assert opt["schedule"][h] == pytest.approx(hind["schedule"][h], abs=1e-6)


class TestTwoSegmentCredit:
    """Direct unit tests over a hand-built linear-cost dp array so the
    trade-off between charging cost and the two-segment credit can be
    verified by hand.

    dp[b] = COST_PER_BIN * b models a per-kWh charging cost to reach end
    state b. COST_PER_BIN sits strictly between V_LO and V_HI, so holding
    energy is worth it only while credited at V_HI (i.e. up to NEED above
    the firmware floor) -- and not worth it once the credit drops to V_LO.
    """

    BIN_KWH = 1.0
    N_STATES = 11  # 0..10 kWh
    FW_FLOOR = 0.0
    SOFT_FLOOR = 2.0
    TARGET = 10.0
    NEED = 4.0
    V_HI = 0.30
    V_LO = 0.05
    COST_PER_BIN = 0.15  # V_LO < COST_PER_BIN < V_HI

    def _dp_linear_cost(self):
        to_bin, from_bin = _bins(self.BIN_KWH, self.N_STATES)
        dp = [self.COST_PER_BIN * b for b in range(self.N_STATES)]
        return dp, to_bin, from_bin

    def _select(self, dp, to_bin, from_bin, *, water_value_hi=None):
        kwargs = dict(
            terminal_mode="water_value",
            water_value=self.V_LO,
            firmware_floor_kwh=self.FW_FLOOR,
            floor_kwh=self.SOFT_FLOOR,
            target_kwh=self.TARGET,
            to_bin=to_bin,
            from_bin=from_bin,
            n_states=self.N_STATES,
        )
        if water_value_hi is not None:
            kwargs["water_value_hi"] = water_value_hi
            kwargs["overnight_need_kwh"] = self.NEED
        return select_end_state(dp, **kwargs)

    def test_two_segment_prefers_holding_need(self):
        """With water_value_hi set, the DP should hold exactly up to NEED
        above the firmware floor (marginal V_HI credit beats the charging
        cost there) -- contrasted against the legacy single-rate call on the
        SAME cost curve, which never recovers the cost and holds nothing."""
        dp, to_bin, from_bin = self._dp_linear_cost()

        best_end_b, best_cost, infeasible = self._select(dp, to_bin, from_bin, water_value_hi=self.V_HI)
        assert not infeasible
        assert best_end_b == 4  # firmware_floor(0) + NEED(4)
        assert best_cost == pytest.approx(0.6)

        legacy_end_b, legacy_cost, legacy_infeasible = self._select(dp, to_bin, from_bin)
        assert not legacy_infeasible
        assert legacy_end_b == 0  # V_LO alone never beats COST_PER_BIN -- holds nothing
        assert legacy_cost == pytest.approx(0.0)

    def test_surplus_still_low_valued(self):
        """Beyond NEED the credit reverts to V_LO (not V_HI) -- so each extra
        bin costs (COST_PER_BIN - V_LO) net, and the optimum does not creep
        past the NEED boundary."""
        dp, to_bin, from_bin = self._dp_linear_cost()

        def score(b: int) -> float:
            avail = b - self.FW_FLOOR
            credit = self.V_HI * min(avail, self.NEED) + self.V_LO * max(0.0, avail - self.NEED)
            return dp[b] - credit

        assert score(5) - score(4) == pytest.approx(self.COST_PER_BIN - self.V_LO)
        assert score(5) > score(4)  # surplus bin doesn't pay for its own charging cost

        best_end_b, _best_cost, infeasible = self._select(dp, to_bin, from_bin, water_value_hi=self.V_HI)
        assert not infeasible
        assert best_end_b == 4


class TestAnchorIsFirmwareFloor:
    """econ-F4: the credit anchor shifts from the SOFT floor_kwh down to the
    HARD firmware_floor_kwh iff water_value_hi is set. Isolated by placing
    a tiny charging cost at end_b=1 (inside (fw_floor=0, soft_floor=2]) --
    a cost cheap enough that it's only worth paying if that bin earns
    credit."""

    def test_credit_in_subfloor_band_only_when_vhi_set(self):
        to_bin, from_bin = _bins(1.0, 3)
        dp = [0.0, 0.01, INF]  # end_b=2 unreachable/irrelevant; restrict scan via target_kwh

        common = dict(
            terminal_mode="water_value",
            water_value=0.05,
            firmware_floor_kwh=0.0,
            floor_kwh=2.0,
            target_kwh=1.0,  # target_b=1 -> scan only {0, 1}
            to_bin=to_bin,
            from_bin=from_bin,
            n_states=3,
        )

        with_hi = select_end_state(dp, water_value_hi=0.30, overnight_need_kwh=4.0, **common)
        assert with_hi[0] == 1  # credit at b=1 (0.30) easily beats the 0.01 cost

        legacy = select_end_state(dp, **common)
        assert legacy[0] == 0  # anchored at soft floor_kwh=2 -> b=1 earns zero credit


class TestM2FallbackUsesSameFormula:
    """The M2 fallback (main scan finds nothing in [floor_b, target_b]) must
    price candidates with the exact same two-segment formula as the main
    scan. Verified by forcing the SAME reachable states to be found via two
    different code paths (direct main-scan hit vs. fallback) and asserting
    byte-identical output."""

    def test_fallback_matches_direct_main_scan(self):
        to_bin, from_bin = _bins(1.0, 11)
        # States 0..5 unreachable; 6..10 reachable at a linear cost.
        dp = [INF] * 6 + [0.15 * b for b in range(6, 11)]

        common = dict(
            terminal_mode="water_value",
            water_value=0.05,
            firmware_floor_kwh=0.0,
            floor_kwh=2.0,
            to_bin=to_bin,
            from_bin=from_bin,
            n_states=11,
            water_value_hi=0.30,
            overnight_need_kwh=4.0,
        )

        # target_kwh=10 -> main scan alone covers b=6..10 directly.
        via_main_scan = select_end_state(dp, target_kwh=10.0, **common)
        # target_kwh=5 -> main scan range(0,6) is all-INF; must fall back to
        # scanning the full [floor_b, n_states) range to find b=6..10.
        via_fallback = select_end_state(dp, target_kwh=5.0, **common)

        assert via_main_scan == via_fallback
        assert via_main_scan[0] == 6
        assert via_main_scan[2] is False
