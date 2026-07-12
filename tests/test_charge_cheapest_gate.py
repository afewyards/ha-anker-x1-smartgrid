"""TDD tests for charge-cheapest-hour gate (charge_window_price_band).

RED phase: these tests are written BEFORE the implementation.

Goal: restrict grid-charging to ONLY the cheapest tariff hour(s) of the
planning window.  When charge_window_price_band=0.005 (default), an hour is
chargeable only if price[h] <= window_min_price + 0.005.

The existing ceiling gate (peak * round_trip_eff - margin) was too permissive:
a mid-morning slot at 0.174 passed the ceiling (~0.356) even though the day's
trough was 0.131.  The new band ANDs the trough condition on top of the ceiling.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, UTC
from unittest.mock import patch


from custom_components.anker_x1_smartgrid.const import DEFAULT_CHARGE_WINDOW_PRICE_BAND
from custom_components.anker_x1_smartgrid.models import Config
from custom_components.anker_x1_smartgrid.optimize import build_charge_mask

# Real window prices from test_optimize_floor_economic.py _TABLE (05:00..21:00)
_PRICES = [
    0.263,
    0.259,
    0.222,
    0.174,
    0.146,
    0.133,
    0.131,
    0.135,
    0.146,
    0.191,
    0.250,
    0.282,
    0.341,
    0.412,
    0.419,
    0.348,
    0.310,
]
_TROUGH = 0.131  # min(_PRICES)
_CEILING = 0.419 * 0.85  # peak * round_trip_eff ≈ 0.356


# ===========================================================================
# 1. build_charge_mask with price_band parameter
# ===========================================================================


class TestBuildChargeMaskWithPriceBand:
    """Unit tests for the new price_band trough-gate in build_charge_mask."""

    def test_trough_band_only_admits_cheapest_hours(self):
        """band=0.005: only hours within 0.005 of the window trough are chargeable.

        trough=0.131, threshold=0.136.
        Expected chargeable: 0.131 (idx 6), 0.133 (idx 5), 0.135 (idx 7).
        Expected NOT chargeable: all higher prices including 0.146 (idx 4), 0.174 (idx 3).
        """
        mask = build_charge_mask(_PRICES, ceiling=_CEILING, price_band=0.005)
        for i, (p, chargeable) in enumerate(zip(_PRICES, mask)):
            if p <= _TROUGH + 0.005:
                assert chargeable, f"h{i} (price={p}) should be chargeable (trough+band={_TROUGH + 0.005:.4f})"
            else:
                assert not chargeable, f"h{i} (price={p}) should NOT be chargeable (trough+band={_TROUGH + 0.005:.4f})"

    def test_profitable_but_non_trough_hour_excluded(self):
        """0.174 is below the ~0.356 ceiling but excluded by the 0.005 band.

        This is the exact live bug: the planner buys at 0.174 when the day's
        trough is 0.131.  The trough band must block this.
        """
        prices = [0.263, 0.174, 0.131]  # trough=0.131; 0.174 >> 0.131+0.005=0.136
        mask = build_charge_mask(prices, ceiling=_CEILING, price_band=0.005)
        assert not mask[1], "0.174 should be excluded by trough band (0.131+0.005=0.136 < 0.174)"
        assert mask[2], "0.131 should be chargeable (== trough)"

    def test_wider_band_admits_more_hours(self):
        """band=0.05 → threshold=0.181 → the 0.174 hour is now chargeable."""
        prices = [0.263, 0.174, 0.131]
        mask = build_charge_mask(prices, ceiling=_CEILING, price_band=0.05)
        # 0.131 + 0.05 = 0.181; 0.174 <= 0.181 → chargeable
        assert mask[1], "0.174 should be chargeable with band=0.05 (threshold=0.181)"
        assert mask[2], "0.131 should be chargeable"

    def test_all_equal_prices_all_chargeable_below_ceiling(self):
        """Degenerate case: all prices equal → window_min == all prices → all chargeable."""
        prices = [0.15, 0.15, 0.15]
        mask = build_charge_mask(prices, ceiling=0.30, price_band=0.005)
        # trough=0.15, threshold=0.155; all prices=0.15 <= 0.155 → all True
        assert mask == [True, True, True]

    def test_empty_price_list_does_not_crash(self):
        """Empty window → empty mask, no crash."""
        mask = build_charge_mask([], ceiling=0.30, price_band=0.005)
        assert mask == []

    def test_ceiling_none_still_all_false_with_band(self):
        """Fail-closed semantics preserved: ceiling=None → all False, regardless of band."""
        mask = build_charge_mask([0.131, 0.133], ceiling=None, price_band=0.005)
        assert mask == [False, False]

    def test_no_band_behavior_unchanged(self):
        """price_band=None (default) → ceiling-only behaviour, identical to old code.

        Existing callers that pass no price_band must get the same result as before.
        """
        prices = [0.10, 0.20, 0.30]
        mask_no_band = build_charge_mask(prices, ceiling=0.25)  # old call style
        mask_explicit_none = build_charge_mask(prices, ceiling=0.25, price_band=None)
        # Both should be ceiling-only: [True, True, False]
        assert mask_no_band == [True, True, False]
        assert mask_explicit_none == [True, True, False]

    def test_band_boundary_is_inclusive(self):
        """price[h] == trough + band → chargeable (<=, not <)."""
        prices = [0.131, 0.136]  # 0.136 == 0.131 + 0.005
        mask = build_charge_mask(prices, ceiling=0.40, price_band=0.005)
        assert mask == [True, True], "boundary price (trough+band) must be chargeable"

    def test_band_applied_on_full_window_not_subset(self):
        """window_min is the global minimum of the price list, not just a suffix."""
        # Cheap price at the END of the window; mid-price in the middle.
        prices = [0.30, 0.25, 0.20, 0.15, 0.10]  # trough=0.10 at index 4
        mask = build_charge_mask(prices, ceiling=0.40, price_band=0.005)
        # trough=0.10, threshold=0.105
        # Only p=0.10 (idx 4) <= 0.105 → chargeable; all others blocked
        assert mask == [False, False, False, False, True]


# ===========================================================================
# 2. Config.charge_window_price_band field + DEFAULT_CHARGE_WINDOW_PRICE_BAND
# ===========================================================================


class TestConfigChargeWindowPriceBand:
    """Config must expose charge_window_price_band with the correct default."""

    def test_default_value_is_0_005(self):
        """Default charge_window_price_band is 0.005 €/kWh."""
        cfg = Config()
        assert cfg.charge_window_price_band == 0.005

    def test_const_default_matches(self):
        """DEFAULT_CHARGE_WINDOW_PRICE_BAND constant exists and equals 0.005."""
        assert DEFAULT_CHARGE_WINDOW_PRICE_BAND == 0.005

    def test_config_default_uses_const(self):
        """Config default is sourced from DEFAULT_CHARGE_WINDOW_PRICE_BAND."""
        cfg = Config()
        assert cfg.charge_window_price_band == DEFAULT_CHARGE_WINDOW_PRICE_BAND

    def test_from_dict_sets_custom_band(self):
        """Config.from_dict respects a custom charge_window_price_band value."""
        cfg = Config.from_dict({"charge_window_price_band": 0.02})
        assert cfg.charge_window_price_band == 0.02

    def test_zero_band_admits_only_exact_trough(self):
        """band=0.0 → only the exact minimum price is chargeable."""
        prices = [0.131, 0.132, 0.133]
        mask = build_charge_mask(prices, ceiling=0.40, price_band=0.0)
        assert mask == [True, False, False], "Only exact minimum must be chargeable with band=0"


# ===========================================================================
# 3. Heuristic _worthy path — via compute_decision with DP fallback
# ===========================================================================


class TestHeuristicWorthyTroughBand:
    """The heuristic _worthy gate must also apply the trough-band.

    When optimize_grid raises (DP fallback path), the controller falls back to
    the heuristic selected-slot list which is filtered by _worthy().  The new
    _worthy() must reject mid-price hours even when they pass the old ceiling.

    Scenario:
      now=10:00 UTC, slots h0..h8:
        h0=0.174 (current, mid-price — SHOULD BE BLOCKED)
        h1=0.160, h2=0.145, h3=0.140, h4=0.138, h5=0.136
        h6=0.131 (trough, 6h out at 16:00 ≥ min_horizon_h=6)
        h7=0.150, h8=0.419 (evening peak)

      peak = max(h0..h8) = 0.419
      ceiling = 0.419 * 0.85 ≈ 0.356 > 0.174 → old gate PASSES h0 (bug)
      trough = 0.131, band = 0.005 (default) → threshold = 0.136 < 0.174 → BLOCKS h0 ✓

      max_charge_w=500 → battery_input=0.46 kW/h → n=ceil(deficit/0.46) >> 2
      → select_charge_slots includes h0 (the mid-price slot) in the candidate list
      → _worthy(h0) must block it

      Expected: plan.state == PASSIVE (h0 is the current hour, not chargeable)
    """

    _BASE = datetime(2026, 6, 22, 10, 0, tzinfo=UTC)  # 10:00 UTC

    def _slots(self, prices: list[float]) -> list:
        from custom_components.anker_x1_smartgrid.models import PriceSlot

        return [PriceSlot(self._BASE + timedelta(hours=i), p) for i, p in enumerate(prices)]

    def _make_cfg(self, **overrides) -> Config:
        return Config.from_dict(
            {
                "capacity_kwh": 10.0,
                "soc_target": 97.0,
                "eta_charge": 0.92,
                "eps_hi_kwh": 0.4,
                "eps_lo_kwh": 0.2,
                "min_dwell_min": 0,
                "max_charge_w": 500.0,  # small → many hours needed → h0 in candidate list
                "round_trip_eff": 0.85,
                **overrides,
            }
        )

    def test_heuristic_path_blocks_mid_price_current_slot(self):
        """On the DP fallback path, h0=0.174 (mid-price) is NOT selected.

        Old behaviour (no band): h0 passes ceiling gate → FORCING.
        New behaviour (band=0.005): h0 blocked by trough band → PASSIVE.
        """
        from custom_components.anker_x1_smartgrid.controller import compute_decision
        from custom_components.anker_x1_smartgrid.forecast import LoadPredictor
        from custom_components.anker_x1_smartgrid.models import ControllerState, PlantInputs, PlanState

        prices = [0.174, 0.160, 0.145, 0.140, 0.138, 0.136, 0.131, 0.150, 0.419]
        slots = self._slots(prices)
        cfg = self._make_cfg()  # band=0.005 by default

        inputs = PlantInputs(soc=20.0, meter_w=0.0, now=self._BASE)
        sunset = self._BASE + timedelta(hours=10)
        plan = PlanState.initial(self._BASE - timedelta(hours=2))
        predictor = LoadPredictor.from_profile({})

        # Force DP fallback so the heuristic _worthy is exercised.
        with patch(
            "custom_components.anker_x1_smartgrid.optimize.optimize_grid",
            side_effect=RuntimeError("forced DP fallback for test"),
        ):
            new_plan, _setpoint, *_ = compute_decision(
                plan,
                inputs,
                slots,
                0.0,
                sunset,
                predictor,
                None,
                cfg,
            )

        # h0 (0.174) is the current slot but should be blocked by the trough band.
        assert new_plan.state is ControllerState.PASSIVE, (
            f"h0=0.174 is above trough+band=0.136, must not trigger FORCING; got state={new_plan.state}"
        )

    def test_heuristic_path_is_passive_on_dp_fallback(self):
        """Task 2 (P80-survival-removal): DP exception fallback is always PASSIVE.

        The heuristic charge-slot selection (_worthy gate + select_charge_slots)
        was deleted in Task 2.  When the DP raises, selected=[] → PASSIVE,
        regardless of whether the current slot would have passed the price gate.

        Prices: [0.131(h0=trough, current), 0.174, 0.419(peak), ...]
        Old behaviour: h0 passes ceiling+band → FORCING.
        New behaviour: no heuristic → PASSIVE.
        """
        from custom_components.anker_x1_smartgrid.controller import compute_decision
        from custom_components.anker_x1_smartgrid.forecast import LoadPredictor
        from custom_components.anker_x1_smartgrid.models import ControllerState, PlantInputs, PlanState

        prices = [0.131, 0.174, 0.419, 0.419, 0.419, 0.419, 0.419, 0.419, 0.419]
        slots = self._slots(prices)
        cfg = self._make_cfg()

        inputs = PlantInputs(soc=20.0, meter_w=0.0, now=self._BASE)
        sunset = self._BASE + timedelta(hours=10)
        plan = PlanState.initial(self._BASE - timedelta(hours=2))
        predictor = LoadPredictor.from_profile({})

        with patch(
            "custom_components.anker_x1_smartgrid.optimize.optimize_grid",
            side_effect=RuntimeError("forced DP fallback for test"),
        ):
            new_plan, _setpoint, *_ = compute_decision(
                plan,
                inputs,
                slots,
                0.0,
                sunset,
                predictor,
                None,
                cfg,
            )

        # Fallback is PASSIVE: heuristic deleted, selected=[] (Task 2).
        assert new_plan.state is ControllerState.PASSIVE, (
            f"DP fallback must be PASSIVE (heuristic removed); got state={new_plan.state}"
        )


# ===========================================================================
# 4. window_min parameter — A7 correctness bug fix
# ===========================================================================


class TestBuildChargeMaskWindowMin:
    """Tests for the window_min parameter that guards against 0.0-padding collapse.

    Bug: build_charge_mask computes trough = min(price).  On the DP path the
    controller pads missing price hours with 0.0, so min([..., 0.0, 0.0]) = 0.0
    → trough_threshold = 0.005 → EVERY real price fails p <= 0.005 → all-False
    mask → grid charging completely killed (silent severe regression).

    Fix: accept an explicit window_min: float | None = None.  When provided, use
    it as the trough instead of min(price).  The DP path computes window_min from
    real slot prices only (no padding) and passes it in.
    """

    def test_zero_padding_collapses_gate_without_window_min(self):
        """Regression doc: without window_min, 0.0 padding kills all real prices.

        This test documents the bug behavior so future readers can see why
        window_min is necessary.  price_band alone (without window_min) will
        use min([..., 0.0, 0.0]) = 0.0 as trough → trough_threshold = 0.005
        → real trough prices (0.131, 0.133) fail p <= 0.005 → False.
        """
        prices_padded = [0.174, 0.131, 0.133, 0.0, 0.0]
        ceiling = 0.40
        # Without window_min: min(prices_padded) = 0.0 → threshold = 0.005
        mask_bug = build_charge_mask(prices_padded, ceiling=ceiling, price_band=0.005)
        # Real trough hours fail because 0.131 > 0.005
        assert not mask_bug[1], (
            "BUG: 0.131 should fail when min([..., 0.0])=0.0 → threshold=0.005 "
            "(this documents why window_min is needed)"
        )
        assert not mask_bug[2], "BUG: 0.133 should fail when min([..., 0.0])=0.0 → threshold=0.005"

    def test_window_min_overrides_zero_padding_for_real_trough(self):
        """With window_min=0.131, real trough hours are correctly marked chargeable.

        The 0.0-padded hours no longer collapse the trough threshold because
        window_min is sourced from real slot prices, not the padded array.
        """
        prices_padded = [0.174, 0.131, 0.133, 0.0, 0.0]
        ceiling = 0.40
        # With window_min=0.131 (real trough from slots): threshold = 0.136
        mask_fixed = build_charge_mask(prices_padded, ceiling=ceiling, price_band=0.005, window_min=0.131)
        assert not mask_fixed[0], "0.174 > 0.131+0.005=0.136 → not chargeable"
        assert mask_fixed[1], "0.131 == trough → chargeable"
        assert mask_fixed[2], "0.133 ≤ 0.136 → chargeable"

    def test_window_min_none_falls_back_to_min_of_price(self):
        """window_min=None → falls back to min(price), preserving backward compat.

        When no padding is present, min(price) IS the real trough, so passing
        window_min=None gives the same result as the old two-parameter call.
        """
        prices = [0.263, 0.174, 0.131, 0.133, 0.135]
        ceiling = 0.40
        # Explicit None and absent parameter must give identical results
        mask_explicit_none = build_charge_mask(prices, ceiling=ceiling, price_band=0.005, window_min=None)
        mask_no_param = build_charge_mask(prices, ceiling=ceiling, price_band=0.005)
        assert mask_explicit_none == mask_no_param

    def test_window_min_does_not_affect_ceiling_only_path(self):
        """window_min is ignored when price_band is None (ceiling-only path)."""
        prices = [0.131, 0.174, 0.263]
        ceiling = 0.20
        # price_band=None → ceiling-only, window_min irrelevant
        mask_with_wm = build_charge_mask(prices, ceiling=ceiling, price_band=None, window_min=0.05)
        mask_without = build_charge_mask(prices, ceiling=ceiling)
        assert mask_with_wm == mask_without

    def test_window_min_provided_with_all_unpadded_prices(self):
        """window_min works correctly when all prices are real (no padding).

        Passing an explicit window_min that matches min(price) must give the
        same result as omitting window_min.
        """
        prices = [0.174, 0.131, 0.133]
        mask_auto = build_charge_mask(prices, ceiling=0.40, price_band=0.005)
        mask_explicit = build_charge_mask(prices, ceiling=0.40, price_band=0.005, window_min=0.131)
        assert mask_auto == mask_explicit

    def test_window_min_tighter_than_actual_min(self):
        """window_min can be higher than min(price) — e.g. real trough > padded min.

        This is the live scenario: real trough = 0.131, but min(padded) = 0.0.
        Passing window_min=0.131 gives threshold=0.136 (not 0.005).
        """
        prices = [0.174, 0.131, 0.0]  # 0.0 is padding
        mask = build_charge_mask(prices, ceiling=0.40, price_band=0.005, window_min=0.131)
        # trough_threshold = 0.131 + 0.005 = 0.136
        assert not mask[0], "0.174 > 0.136 → not chargeable"
        assert mask[1], "0.131 ≤ 0.136 → chargeable"
        # 0.0: passes ceiling(0.40) AND trough_threshold(0.136) → True
        assert mask[2], "0.0 passes both gates (pre-existing behaviour)"
