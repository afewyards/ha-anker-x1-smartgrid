"""Piecewise per-hour terminal-value credit in ``dp_common.select_end_state``.

See docs/superpowers/specs/2026-08-01-terminal-piecewise-credit-design.md
(supersedes the two-segment credit of
docs/superpowers/specs/2026-07-18-overnight-terminal-value-design.md).
``select_end_state``'s ``water_value`` terminal mode credits end-of-horizon
SoC above the firmware floor at a single rate ``v``. This replaces the old
``water_value_hi``/``overnight_need_kwh`` two-segment params with
``terminal_segments``: a list of ``(dc_kwh, value_eur_per_dc_kwh)`` pairs,
walked richest-first, with any surplus beyond all segments credited at the
original ``v``. The legacy single-rate behaviour (credit anchored at the SOFT
``floor_kwh``) must stay byte-identical when ``terminal_segments`` is
omitted/None -- this is a parity gate shared with ``optimize.optimize_grid``
/ ``regret.hindsight_optimal_grid``.
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


class TestNoneSegmentsByteParity:
    """Legacy branch (terminal_segments is None) must stay byte-identical."""

    def test_none_segments_byte_parity(self):
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
        explicit_none = select_end_state(dp, terminal_segments=None, **kwargs)
        assert omitted == explicit_none

    def test_optimize_oracle_parity_unaffected_by_new_signature(self):
        """Full-stack regression: optimize_grid <-> hindsight_optimal_grid
        parity (kwh/eur/schedule) for a live water_value scenario -- neither
        caller passes terminal_segments, so this pins that the new param
        doesn't perturb the existing call path at all."""
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


class TestPiecewiseTwoSegmentsPlusSurplus:
    """Two segments (sorted desc by value) plus the water_value surplus tail
    -- hand-computed credit at three end bins spanning seg1-only,
    seg1+seg2-exactly, and into the surplus band beyond both segments.

    dp[b] = COST_PER_BIN * b models a per-kWh charging cost to reach end
    state b. COST_PER_BIN sits strictly between SEG2's value and SEG1's
    value, so the DP should charge through seg1 (where the marginal credit
    beats the charging cost) and stop exactly there (seg2's lower marginal
    credit no longer beats the cost).
    """

    BIN_KWH = 1.0
    N_STATES = 11  # 0..10 kWh
    FW_FLOOR = 0.0
    SOFT_FLOOR = 2.0
    TARGET = 10.0
    SEG1 = (2.0, 0.30)  # first 2 kWh above the firmware floor at 0.30
    SEG2 = (2.0, 0.20)  # next 2 kWh at 0.20
    V_LO = 0.05  # water_value surplus rate beyond both segments
    COST_PER_BIN = 0.25  # V_LO, SEG2-value < COST_PER_BIN < SEG1-value

    def test_piecewise_two_segments_plus_surplus(self):
        to_bin, from_bin = _bins(self.BIN_KWH, self.N_STATES)
        dp = [self.COST_PER_BIN * b for b in range(self.N_STATES)]

        kwargs = dict(
            terminal_mode="water_value",
            water_value=self.V_LO,
            firmware_floor_kwh=self.FW_FLOOR,
            floor_kwh=self.SOFT_FLOOR,
            target_kwh=self.TARGET,
            to_bin=to_bin,
            from_bin=from_bin,
            n_states=self.N_STATES,
            terminal_segments=[self.SEG1, self.SEG2],
        )

        # Hand-computed credit at three end bins (avail == end_b since
        # FW_FLOOR == 0):
        #   end_b=2 (avail=2, all in seg1):          2*0.30                   = 0.60
        #   end_b=4 (avail=4, seg1+seg2 exactly):    2*0.30 + 2*0.20          = 1.00
        #   end_b=6 (avail=6, 2 kWh surplus @ v_lo): 2*0.30 + 2*0.20 + 2*0.05 = 1.10
        # score(b) = dp[b] - credit(b):
        score_at_2 = self.COST_PER_BIN * 2 - 0.60
        score_at_4 = self.COST_PER_BIN * 4 - 1.00
        score_at_6 = self.COST_PER_BIN * 6 - 1.10
        assert score_at_2 == pytest.approx(-0.10)
        assert score_at_4 == pytest.approx(0.0)
        assert score_at_6 == pytest.approx(0.40)
        # -0.10 < 0.0 < 0.40 -- the optimum sits at the end of seg1: marginal
        # cost 0.25 beats seg1's 0.30 but loses to seg2's 0.20, so the DP
        # charges through seg1 and stops there.

        best_end_b, best_cost, infeasible = select_end_state(dp, **kwargs)
        assert not infeasible
        assert best_end_b == 2
        assert best_cost == pytest.approx(0.5)


class TestAnchorIsFirmwareFloor:
    """econ-F4/rev-3: the credit anchor shifts from the SOFT floor_kwh down
    to the HARD firmware_floor_kwh iff terminal_segments is set. Isolated by
    placing a tiny charging cost at end_b=1 (inside (fw_floor=0,
    soft_floor=2]) -- a cost cheap enough that it's only worth paying if
    that bin earns credit."""

    def test_anchor_is_firmware_floor(self):
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

        with_segments = select_end_state(dp, terminal_segments=[(4.0, 0.30)], **common)
        assert with_segments[0] == 1  # credit at b=1 (0.30) easily beats the 0.01 cost

        legacy = select_end_state(dp, **common)
        assert legacy[0] == 0  # anchored at soft floor_kwh=2 -> b=1 earns zero credit


class TestM2FallbackUsesSameFormula:
    """The M2 fallback (main scan finds nothing in [floor_b, target_b]) must
    price candidates with the exact same piecewise formula as the main scan.
    Verified by forcing the SAME reachable states to be found via two
    different code paths (direct main-scan hit vs. fallback) and asserting
    byte-identical output."""

    def test_m2_fallback_same_formula(self):
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
            terminal_segments=[(4.0, 0.30)],
        )

        # target_kwh=10 -> main scan alone covers b=6..10 directly.
        via_main_scan = select_end_state(dp, target_kwh=10.0, **common)
        # target_kwh=5 -> main scan range(0,6) is all-INF; must fall back to
        # scanning the full [floor_b, n_states) range to find b=6..10.
        via_fallback = select_end_state(dp, target_kwh=5.0, **common)

        assert via_main_scan == via_fallback
        assert via_main_scan[0] == 6
        assert via_main_scan[2] is False


class TestUnsortedSegmentsSortedDefensively:
    """Caller-supplied segment order must not matter -- select_end_state
    sorts by value descending internally, once per call."""

    def test_unsorted_segments_sorted_defensively(self):
        to_bin, from_bin = _bins(1.0, 11)
        dp = [0.25 * b for b in range(11)]

        common = dict(
            terminal_mode="water_value",
            water_value=0.05,
            firmware_floor_kwh=0.0,
            floor_kwh=2.0,
            target_kwh=10.0,
            to_bin=to_bin,
            from_bin=from_bin,
            n_states=11,
        )

        sorted_desc = select_end_state(dp, terminal_segments=[(2.0, 0.30), (2.0, 0.20)], **common)
        unsorted = select_end_state(dp, terminal_segments=[(2.0, 0.20), (2.0, 0.30)], **common)
        assert sorted_desc == unsorted
        assert sorted_desc[0] == 2  # same optimum as TestPiecewiseTwoSegmentsPlusSurplus
