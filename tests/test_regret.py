"""
TDD tests for regret.py — hindsight regret scoring of grid-charge decisions.

All tests use hand-computable synthetic days with round numbers (eta_charge=1.0
so AC kWh == DC kWh).  Config: capacity=10 kWh, floor=20% (2 kWh),
target=80% (8 kWh), max_charge=3 kWh/h, eta=1.0.
"""
import pytest
from custom_components.anker_x1_smartgrid.regret import (
    DayData,
    hindsight_optimal_grid,
    realized_grid_cost,
    score_regret,
)
from custom_components.anker_x1_smartgrid.models import Config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_cfg(**overrides) -> Config:
    """Config with clean test defaults; all other fields take their defaults."""
    defaults = dict(
        capacity_kwh=10.0,
        soc_floor=20.0,   # 2 kWh floor
        soc_target=80.0,  # 8 kWh target
        max_charge_w=3000.0,  # 3 kWh/h
        eta_charge=1.0,   # AC == DC (simplifies test arithmetic)
    )
    defaults.update(overrides)
    return Config(**defaults)


def make_day(
    pv,
    load,
    price,
    soc_start: float,
) -> DayData:
    """Broadcast scalar or list to 24-element tuples and return DayData."""

    def _to24(v):
        if isinstance(v, (int, float)):
            return tuple(float(v) for _ in range(24))
        lst = list(v)
        assert len(lst) == 24, f"expected 24 elements, got {len(lst)}"
        return tuple(float(x) for x in lst)

    return DayData(
        pv_kwh=_to24(pv),
        load_kwh=_to24(load),
        price=_to24(price),
        soc_start=float(soc_start),
    )


def flat_grid(kwh_by_hour: list[float], price: list[float]) -> dict:
    """Local stand-in for the removed regret.realized_grid convenience API."""
    return {
        "kwh": sum(kwh_by_hour),
        "eur": sum(k * p for k, p in zip(kwh_by_hour, price)),
    }


# ---------------------------------------------------------------------------
# hindsight_optimal_grid
# ---------------------------------------------------------------------------

class TestHindsightOptimal:
    """Solar fills battery -> zero grid needed."""

    def test_zero_need_solar_covers(self):
        """PV fills battery from floor to target without any grid charge.

        soc_start=20% (2 kWh). PV 1.5 kWh/h from h6-h9 = 6 kWh total.
        SoC trajectory: 2→2→…→2→3.5→5→6.5→8 (capped at target=8). No floor
        breach, target met. Optimal grid = 0.
        """
        cfg = make_cfg()
        pv = [0.0] * 6 + [1.5] * 4 + [0.0] * 14
        d = make_day(pv, 0, 0.10, soc_start=20.0)
        opt = hindsight_optimal_grid(d, cfg)
        assert opt["kwh"] == pytest.approx(0.0, abs=1e-6)
        assert opt["eur"] == pytest.approx(0.0, abs=1e-6)

    def test_no_pv_no_load_target_shortfall(self):
        """No PV, no load. Battery at 50% must reach 80% target via grid.

        raw SoC stays at 5 kWh all day. End deficit = 3 kWh DC.
        Optimal: 3 kWh at h0 (price 0.10 €/kWh) = 0.30 €.
        """
        cfg = make_cfg()
        d = make_day(0, 0, 0.10, soc_start=50.0)
        opt = hindsight_optimal_grid(d, cfg)
        assert opt["kwh"] == pytest.approx(3.0, abs=1e-6)
        assert opt["eur"] == pytest.approx(0.30, abs=1e-6)

    def test_floor_breach_requires_grid(self):
        """Evening load drains battery below floor; target shortfall at end.

        soc_start=40% (4 kWh). Load 1.5 kWh/h at h20-h21 (3 kWh total).
        Price: 0.05 €/kWh h0-h19, 0.50 h20-h21, 0.05 h22-h23.

        Solar-only trajectory: 4 kWh flat until h20, then 2.5 at h21-end,
        1.0 at h22-end (breach < 2 kWh floor). End raw = 1 kWh.

        DP optimal: 7 kWh from cheapest hours (h0-h19, h22-h23 at 0.05).
        Cost = 7 × 0.05 = 0.35 €.
        """
        cfg = make_cfg()
        load = [0.0] * 20 + [1.5, 1.5] + [0.0] * 2
        price = [0.05] * 20 + [0.50, 0.50, 0.05, 0.05]
        d = make_day(0, load, price, soc_start=40.0)
        opt = hindsight_optimal_grid(d, cfg)
        assert opt["kwh"] == pytest.approx(7.0, abs=1e-6)
        assert opt["eur"] == pytest.approx(7.0 * 0.05, abs=1e-6)

    def test_schedule_has_24_elements(self):
        """Returned schedule list has exactly 24 hourly entries."""
        cfg = make_cfg()
        d = make_day(0, 0, 0.10, 50.0)
        opt = hindsight_optimal_grid(d, cfg)
        assert len(opt["schedule"]) == 24

    def test_cheapest_hours_chosen_first(self):
        """Grid energy is allocated to the cheapest feasible hour.

        soc_start=50% (5 kWh). No PV, no load. Need 3 kWh DC for target.
        Price: 0.30 €/kWh h0, 0.10 €/kWh h1-h23.
        Greedy should allocate all 3 kWh to h1 (cheaper), not h0.
        """
        cfg = make_cfg()
        price = [0.30] + [0.10] * 23
        d = make_day(0, 0, price, soc_start=50.0)
        opt = hindsight_optimal_grid(d, cfg)
        # Cost = 3 × 0.10 = 0.30 € (if cheapest, not 3 × 0.30 = 0.90 €)
        assert opt["kwh"] == pytest.approx(3.0, abs=1e-6)
        assert opt["eur"] == pytest.approx(0.30, abs=1e-6)

    def test_fully_sunny_already_at_target(self):
        """Battery already at target, continuous solar surplus -> no grid needed."""
        cfg = make_cfg()
        d = make_day(0.5, 0, 0.10, soc_start=80.0)  # 8 kWh = target
        opt = hindsight_optimal_grid(d, cfg)
        assert opt["kwh"] == pytest.approx(0.0, abs=1e-6)
        assert opt["eur"] == pytest.approx(0.0, abs=1e-6)

    def test_all_night_no_pv(self):
        """Night scenario: 0 PV, 0 load, battery at 30% must reach 80%.

        Need 5 kWh DC. Flat price 0.15 €/kWh. Optimal cost = 5 × 0.15 = 0.75 €.
        """
        cfg = make_cfg()
        d = make_day(0, 0, 0.15, soc_start=30.0)  # 3 kWh → need 5 kWh
        opt = hindsight_optimal_grid(d, cfg)
        assert opt["kwh"] == pytest.approx(5.0, abs=1e-6)
        assert opt["eur"] == pytest.approx(5.0 * 0.15, abs=1e-6)

    def test_schedule_nonnegative(self):
        """Every hour in the schedule has non-negative grid kWh."""
        cfg = make_cfg()
        load = [0.0] * 20 + [1.5, 1.5] + [0.0] * 2
        price = [0.05] * 20 + [0.50, 0.50, 0.05, 0.05]
        d = make_day(0, load, price, soc_start=40.0)
        opt = hindsight_optimal_grid(d, cfg)
        assert all(v >= -1e-9 for v in opt["schedule"])


# ---------------------------------------------------------------------------
# score_regret
# ---------------------------------------------------------------------------

class TestScoreRegret:
    """
    (a) Over-buy: grid-charged unnecessarily (solar covers it) -> over_buy_kwh > 0.
    (b) Under-buy: no pre-charge, forced floor import at peak -> under_buy_kwh > 0.
    (c) Optimal: realized == optimal -> zero regret.
    """

    # -- (a) Over-buy --------------------------------------------------------

    def test_over_buy_regret_positive(self):
        """Over-buying produces positive regret_eur."""
        cfg = make_cfg()
        # Solar fills from 2 kWh to 8 kWh without any grid (PV h6-h9)
        pv = [0.0] * 6 + [1.5] * 4 + [0.0] * 14
        d = make_day(pv, 0, 0.10, soc_start=20.0)
        opt = hindsight_optimal_grid(d, cfg)          # = 0 kWh, 0 €
        # Realized: charged 3 kWh unnecessarily in h0
        actual = [3.0] + [0.0] * 23
        real = flat_grid(actual, [0.10] * 24)
        result = score_regret(real, opt)
        assert result["regret_eur"] == pytest.approx(0.30, abs=1e-6)
        assert result["regret_eur"] > 0

    def test_over_buy_kwh_correct(self):
        cfg = make_cfg()
        pv = [0.0] * 6 + [1.5] * 4 + [0.0] * 14
        d = make_day(pv, 0, 0.10, soc_start=20.0)
        opt = hindsight_optimal_grid(d, cfg)
        actual = [3.0] + [0.0] * 23
        real = flat_grid(actual, [0.10] * 24)
        result = score_regret(real, opt)
        assert result["over_buy_kwh"] == pytest.approx(3.0, abs=1e-6)
        assert result["over_buy_eur"] == pytest.approx(0.30, abs=1e-6)
        assert result["under_buy_kwh"] == pytest.approx(0.0, abs=1e-6)
        assert result["cost_regret_eur"] == pytest.approx(0.0, abs=1e-6)

    def test_over_buy_eur_uses_gross_import_price_when_export_profitable(self):
        """F3: over_buy_eur must price at GROSS import cost (eur + export_revenue_eur),
        not net eur — net goes negative on export-profitable days and would price
        the excess purchase at a nonsensical negative rate."""
        # eur is NET (gross import − export revenue): gross 10 kWh @0.20 = 2.0, export 3.0 → -1.0
        realized = {"kwh": 10.0, "eur": -1.0, "export_revenue_eur": 3.0}
        optimal = {"kwh": 6.0, "eur": -2.0, "export_revenue_eur": 3.0}
        out = score_regret(realized, optimal)
        assert out["over_buy_eur"] == pytest.approx(0.80, abs=1e-6)   # 4 kWh × 0.20 gross

    # -- (b) Under-buy -------------------------------------------------------

    def test_under_buy_regret_positive(self):
        """Under-buying (forced floor import at peak) produces positive regret.

        Setup:
          soc_start=40% (4 kWh). No PV. Load 1.5 kWh/h at h20-h21.
          Cheap price 0.05 h0-h19 & h22-h23; peak 0.50 h20-h21.

        Optimal: 7 kWh DC pre-charged at 0.05 € -> 0.35 € total.
        Realized: zero deliberate charges; forced floor import 1 kWh at h21
          (0.50 €/kWh) -> realized_eur = 0.50 €.

        regret = 0.50 - 0.35 = 0.15 > 0.
        """
        cfg = make_cfg()
        load = [0.0] * 20 + [1.5, 1.5] + [0.0] * 2
        price = [0.05] * 20 + [0.50, 0.50, 0.05, 0.05]
        d = make_day(0, load, price, soc_start=40.0)
        opt = hindsight_optimal_grid(d, cfg)
        actual = [0.0] * 21 + [1.0] + [0.0] * 2  # forced import at h21
        real = flat_grid(actual, price)
        result = score_regret(real, opt)
        assert result["regret_eur"] == pytest.approx(0.15, abs=1e-6)
        assert result["regret_eur"] > 0

    def test_under_buy_kwh_correct(self):
        cfg = make_cfg()
        load = [0.0] * 20 + [1.5, 1.5] + [0.0] * 2
        price = [0.05] * 20 + [0.50, 0.50, 0.05, 0.05]
        d = make_day(0, load, price, soc_start=40.0)
        opt = hindsight_optimal_grid(d, cfg)
        actual = [0.0] * 21 + [1.0] + [0.0] * 2
        real = flat_grid(actual, price)
        result = score_regret(real, opt)
        assert result["under_buy_kwh"] == pytest.approx(6.0, abs=1e-6)
        assert result["over_buy_kwh"] == pytest.approx(0.0, abs=1e-6)

    # -- (c) Optimal ---------------------------------------------------------

    def test_optimal_day_zero_regret(self):
        """When realized equals optimal, all regret metrics are zero."""
        cfg = make_cfg()
        pv = [0.0] * 6 + [1.5] * 4 + [0.0] * 14
        d = make_day(pv, 0, 0.10, soc_start=20.0)
        opt = hindsight_optimal_grid(d, cfg)    # = 0 kWh
        real = flat_grid([0.0] * 24, [0.10] * 24)
        result = score_regret(real, opt)
        assert result["regret_eur"] == pytest.approx(0.0, abs=1e-6)
        assert result["over_buy_kwh"] == pytest.approx(0.0, abs=1e-6)
        assert result["under_buy_kwh"] == pytest.approx(0.0, abs=1e-6)

    def test_optimal_nonzero_grid_zero_regret(self):
        """Realized exactly matches a non-zero optimal -> zero regret."""
        cfg = make_cfg()
        d = make_day(0, 0, 0.10, soc_start=50.0)  # optimal = 3 kWh
        opt = hindsight_optimal_grid(d, cfg)
        # Realized: exactly 3 kWh grid charge at h0
        real = flat_grid([3.0] + [0.0] * 23, [0.10] * 24)
        result = score_regret(real, opt)
        assert result["regret_eur"] == pytest.approx(0.0, abs=1e-6)
        assert result["over_buy_kwh"] == pytest.approx(0.0, abs=1e-6)
        assert result["under_buy_kwh"] == pytest.approx(0.0, abs=1e-6)

    # -- Edge cases ----------------------------------------------------------

    def test_fully_sunny_zero_deficit_optimal_zero(self):
        """Fully-sunny day with battery already at target: optimal grid = 0."""
        cfg = make_cfg()
        d = make_day(0.5, 0, 0.10, soc_start=80.0)
        opt = hindsight_optimal_grid(d, cfg)
        assert opt["kwh"] == pytest.approx(0.0, abs=1e-6)

    def test_all_night_no_pv_optimal_nonzero(self):
        """No PV, no load night: optimal grid fills the target shortfall."""
        cfg = make_cfg()
        d = make_day(0, 0, 0.15, soc_start=30.0)  # need 5 kWh
        opt = hindsight_optimal_grid(d, cfg)
        assert opt["kwh"] == pytest.approx(5.0, abs=1e-6)

    def test_zero_realized_negative_regret_impossible_when_optimal_zero(self):
        """When optimal = 0 and realized = 0, regret = 0 (not negative)."""
        opt = {"kwh": 0.0, "eur": 0.0}
        real = {"kwh": 0.0, "eur": 0.0}
        result = score_regret(real, opt)
        assert result["regret_eur"] == pytest.approx(0.0, abs=1e-9)


# ---------------------------------------------------------------------------
# Feasibility helper + tests (reviewer requirement)
# ---------------------------------------------------------------------------

def assert_feasible(schedule: list[float], day: DayData, cfg: Config, *, atol: float = 1e-3) -> None:
    """Simulate *schedule* forward and assert all feasibility constraints hold.

    Constraints checked:
    * per-hour grid AC ≤ max_charge_w/1000 (rate limit)
    * per-hour grid DC ≤ headroom (can't overflow battery above target)
    * SoC ≥ soc_floor at every hour boundary
    * end SoC ≥ soc_target (end-of-day reserve)

    Solar/load convention matches regret.py / energy.py:
    * net > 0: DC = min(net, rate) × eta, capped at target.
    * net ≤ 0: discharge 1:1 (no discharge eta).
    """
    cap_kwh = cfg.capacity_kwh
    floor_kwh = cfg.soc_floor / 100.0 * cap_kwh
    target_kwh = cfg.soc_target / 100.0 * cap_kwh
    max_ac_per_h = cfg.max_charge_w / 1000.0
    eta = cfg.eta_charge

    assert len(schedule) == 24, f"schedule length {len(schedule)}, expected 24"

    soc = day.soc_start / 100.0 * cap_kwh
    for h in range(24):
        net = day.pv_kwh[h] - day.load_kwh[h]
        if net > 0.0:
            dc = min(net, max_ac_per_h) * eta
            soc = min(soc + dc, target_kwh)
        else:
            soc = soc + net

        g_ac = schedule[h]
        assert g_ac >= -atol, f"h{h}: negative grid {g_ac:.4f}"
        assert g_ac <= max_ac_per_h + atol, f"h{h}: grid AC {g_ac:.4f} > rate {max_ac_per_h}"
        g_dc = g_ac * eta
        headroom = max(0.0, target_kwh - soc)
        assert g_dc <= headroom + atol, f"h{h}: grid DC {g_dc:.4f} > headroom {headroom:.4f}"
        soc = min(soc + g_dc, target_kwh)

        assert soc >= floor_kwh - atol, f"h{h}: SoC {soc:.4f} < floor {floor_kwh:.4f}"

    assert soc >= target_kwh - atol, f"End SoC {soc:.4f} < reserve {target_kwh:.4f}"


class TestFeasibility:
    """assert_feasible applied to multiple optimal outputs."""

    def test_feasible_solar_covers(self):
        """Zero-grid day: schedule is all zeros; feasibility check passes."""
        cfg = make_cfg()
        pv = [0.0] * 6 + [1.5] * 4 + [0.0] * 14
        d = make_day(pv, 0, 0.10, soc_start=20.0)
        opt = hindsight_optimal_grid(d, cfg)
        assert_feasible(opt["schedule"], d, cfg)

    def test_feasible_floor_breach_case(self):
        """Floor-breach day: DP schedule keeps SoC above floor at every hour."""
        cfg = make_cfg()
        load = [0.0] * 20 + [1.5, 1.5] + [0.0] * 2
        price = [0.05] * 20 + [0.50, 0.50, 0.05, 0.05]
        d = make_day(0, load, price, soc_start=40.0)
        opt = hindsight_optimal_grid(d, cfg)
        assert_feasible(opt["schedule"], d, cfg)

    def test_feasible_all_night(self):
        """All-night charging: 5 kWh in two hours; schedule stays in bounds."""
        cfg = make_cfg()
        d = make_day(0, 0, 0.15, soc_start=30.0)
        opt = hindsight_optimal_grid(d, cfg)
        assert_feasible(opt["schedule"], d, cfg)


# ---------------------------------------------------------------------------
# Reviewer's capacity-constrained counterexample
# ---------------------------------------------------------------------------

class TestCapacityConstrainedCounterexample:
    """Reviewer's pinned counterexample: battery near-full during cheapest hours.

    Old greedy (broken): assigned 8 kWh to cheap hours h6-h9 at 0.05 → 0.40 €,
    but only 4 kWh fits in the battery (1 kWh headroom per hour after load).
    That schedule was infeasible AND under-priced the true optimal.

    DP (correct): 4×1 kWh at h6-h9 (cheap, 0.05) + 4 kWh at 0.10 elsewhere
    = 8 kWh total, 0.60 €.  Schedule is self-consistent and reserve is met.
    """

    def test_optimal_kwh_and_eur(self):
        """DP finds the feasible optimum: 8 kWh, 0.60 €."""
        cfg = make_cfg()
        # soc_start=80%=8kWh = target.  Load 1 kWh/h at h6-h13.
        # Cheapest prices h6-h9 (0.05); everything else 0.10.
        load = [0.0] * 6 + [1.0] * 8 + [0.0] * 10
        price = [0.10] * 6 + [0.05] * 4 + [0.10] * 14
        d = make_day(0, load, price, soc_start=80.0)
        opt = hindsight_optimal_grid(d, cfg)
        # 4 kWh at 0.05 (max absorption during h6-h9) + 4 kWh at 0.10 = 0.60 €
        assert opt["kwh"] == pytest.approx(8.0, abs=0.06)
        assert opt["eur"] == pytest.approx(0.60, abs=0.02)

    def test_schedule_is_feasible(self):
        """Returned schedule passes the full feasibility simulation."""
        cfg = make_cfg()
        load = [0.0] * 6 + [1.0] * 8 + [0.0] * 10
        price = [0.10] * 6 + [0.05] * 4 + [0.10] * 14
        d = make_day(0, load, price, soc_start=80.0)
        opt = hindsight_optimal_grid(d, cfg)
        assert_feasible(opt["schedule"], d, cfg)


# ---------------------------------------------------------------------------
# Battery high during cheapest hours → limited absorption
# ---------------------------------------------------------------------------

class TestBatteryHighDuringCheapHours:
    """When cheap hours arrive with the battery near-full, less kWh can be absorbed.

    soc_start=70%=7 kWh. Load 0.5 kWh/h at h0-h3. Price 0.05 h0-h3, 0.20 h4-h23.

    h0: soc=7 → after load: 6.5. headroom=1.5. charge 1.5. soc=8.
    h1-h3: soc=8 → after load: 7.5. headroom=0.5. charge 0.5 each.
    Total cheap grid: 1.5+0.5+0.5+0.5 = 3.0 kWh at 0.05 = 0.15 €.
    End h3: soc=8 = target. h4-h23: soc=8 (no load). No further grid needed.
    """

    def test_optimal_values(self):
        cfg = make_cfg()
        load = [0.5] * 4 + [0.0] * 20
        price = [0.05] * 4 + [0.20] * 20
        d = make_day(0, load, price, soc_start=70.0)
        opt = hindsight_optimal_grid(d, cfg)
        assert opt["kwh"] == pytest.approx(3.0, abs=0.06)
        assert opt["eur"] == pytest.approx(0.15, abs=0.02)

    def test_schedule_is_feasible(self):
        cfg = make_cfg()
        load = [0.5] * 4 + [0.0] * 20
        price = [0.05] * 4 + [0.20] * 20
        d = make_day(0, load, price, soc_start=70.0)
        opt = hindsight_optimal_grid(d, cfg)
        assert_feasible(opt["schedule"], d, cfg)


# ---------------------------------------------------------------------------
# Wrong-timing / right-volume → cost_regret_eur semantics
# ---------------------------------------------------------------------------

class TestCostRegretEurSemantics:
    """cost_regret_eur captures timing penalty; under_buy_kwh stays 0 when kWh match."""

    def test_wrong_timing_right_volume(self):
        """Same kWh volume as optimal but at expensive timing.

        soc_start=50%=5 kWh. No PV, no load. Need 3 kWh to reach target=8.
        Price: 0.10 h0-h10, 0.50 h11-h23.

        Optimal: 3 kWh at h0 (0.10 €) = 0.30 €.
        Realized: 3 kWh at h11 (0.50 €) = 1.50 €.

        Same volume (3 kWh): under_buy_kwh=0, over_buy_kwh=0.
        Timing penalty: cost_regret_eur = max(0, 1.20 - 0) = 1.20 €.
        """
        cfg = make_cfg()
        price = [0.10] * 11 + [0.50] * 13
        d = make_day(0, 0, price, soc_start=50.0)
        opt = hindsight_optimal_grid(d, cfg)   # 3 kWh at 0.10, eur=0.30
        actual = [0.0] * 11 + [3.0] + [0.0] * 12   # bought at h11 (expensive)
        real = flat_grid(actual, price)
        result = score_regret(real, opt)
        assert result["regret_eur"] == pytest.approx(1.20, abs=0.02)
        assert result["under_buy_kwh"] == pytest.approx(0.0, abs=1e-6)
        assert result["over_buy_kwh"] == pytest.approx(0.0, abs=1e-6)
        assert result["cost_regret_eur"] == pytest.approx(1.20, abs=0.02)


# ---------------------------------------------------------------------------
# Validation — len != 24 raises ValueError
# ---------------------------------------------------------------------------

class TestValidation:
    def test_hindsight_raises_on_short_pv(self):
        cfg = make_cfg()
        d = DayData(
            pv_kwh=(0.0,) * 23,   # wrong length!
            load_kwh=(0.0,) * 24,
            price=(0.10,) * 24,
            soc_start=50.0,
        )
        with pytest.raises(ValueError, match="pv_kwh"):
            hindsight_optimal_grid(d, cfg)

    def test_hindsight_raises_on_short_load(self):
        cfg = make_cfg()
        d = DayData(
            pv_kwh=(0.0,) * 24,
            load_kwh=(0.0,) * 25,  # wrong length!
            price=(0.10,) * 24,
            soc_start=50.0,
        )
        with pytest.raises(ValueError, match="load_kwh"):
            hindsight_optimal_grid(d, cfg)




# ---------------------------------------------------------------------------
# eta != 1.0 — guards AC↔DC accounting under real charge efficiency
# ---------------------------------------------------------------------------

class TestEtaLessThanOne:
    """Verify AC↔DC accounting: cost = g_dc/eta, rate cap = rate×eta, headroom in DC.

    Every other test uses eta=1.0 (AC==DC), so a regression in the eta path
    would go undetected.  This class pins one known case with eta=0.9.
    """

    def test_counterexample_eta_0_9_kwh_and_eur(self):
        """Same counterexample as TestCapacityConstrainedCounterexample but eta=0.9.

        DC rate cap = 3.0×0.9 = 2.7 kWh/h.
        h6-h9 (cheap, 0.05): 1 kWh/h headroom (load drains 1 kWh then battery
        refills) → 4×1 DC, g_ac = 4/0.9 ≈ 4.444 kWh, cost = 4/0.9×0.05 = 0.222 €.
        h10-h13: battery drains 8→4 kWh (no cheap grid).
        h14-h15: 2.7+1.3=4 DC at 0.10 → g_ac = 4/0.9 ≈ 4.444 kWh, cost = 0.444 €.
        Total: 8/0.9 ≈ 8.889 kWh AC, 0.6/0.9 = 2/3 ≈ 0.667 €.
        """
        cfg = make_cfg(eta_charge=0.9)
        load = [0.0] * 6 + [1.0] * 8 + [0.0] * 10
        price = [0.10] * 6 + [0.05] * 4 + [0.10] * 14
        d = make_day(0, load, price, soc_start=80.0)
        opt = hindsight_optimal_grid(d, cfg)
        assert opt["kwh"] == pytest.approx(8 / 0.9, abs=0.06)
        assert opt["eur"] == pytest.approx(2 / 3, abs=0.02)

    def test_counterexample_eta_0_9_feasible(self):
        """assert_feasible on the eta=0.9 schedule confirms rate/floor/reserve."""
        cfg = make_cfg(eta_charge=0.9)
        load = [0.0] * 6 + [1.0] * 8 + [0.0] * 10
        price = [0.10] * 6 + [0.05] * 4 + [0.10] * 14
        d = make_day(0, load, price, soc_start=80.0)
        opt = hindsight_optimal_grid(d, cfg)
        assert_feasible(opt["schedule"], d, cfg)


# ---------------------------------------------------------------------------
# realized_grid_cost — battery simulation with forced floor-hit imports
# ---------------------------------------------------------------------------

class TestRealizedGridCost:
    """realized_grid_cost: simulate realized schedule + forced floor-hit imports.

    All cases: capacity=10 kWh, floor=20% (2 kWh), target=80% (8 kWh),
    rate=3 kWh/h, eta=1.0 (so AC == DC).

    (a) Under-charge: battery drains to floor, forced import appears at peak price.
    (b) Over-charge beyond headroom: excess AC is paid but not stored.
    (c) Realized equals optimal: forced_import_kwh all-zero, totals match optimal.
    (d) Partial over-charge: headroom partially absorbs deliberate charge, waste paid.
    (e) Validation: wrong-length input raises ValueError.
    """

    # -- (a) Under-charge → forced floor import ---------------------------------

    def test_under_charge_forced_floor_import_appears(self):
        """No deliberate charges; evening load drains battery below firmware floor.

        soc_start=15% (1.5 kWh). Load 1.5 kWh/h at h20-h21. No PV.
        Prices: 0.05 €/kWh h0-h19 & h22-h23; 0.50 €/kWh h20-h21.
        Firmware floor = 0.5 kWh (5% of 10 kWh); soft floor = 2.0 kWh (20%).

        h0-h19: no change. soc=1.5.
        h20: 1.5 → 0.0. Below firmware floor (0.5).
             forced_import = 0.5 kWh at 0.50. soc clamps to 0.5.
        h21: 0.5 → -1.0. Below firmware floor.
             forced_import = 1.5 kWh at 0.50. soc clamps to 0.5.
        h22-h23: soc=0.5. No change.

        charge_kwh  = 0 (no deliberate charges stored)
        forced_import_kwh[20] = 0.5, [21] = 1.5, all others 0
        grid_kwh = 0 + 2.0 = 2.0
        eur      = 0.5 × 0.50 + 1.5 × 0.50 = 1.00
        """
        cfg = make_cfg()
        load = [0.0] * 20 + [1.5, 1.5] + [0.0] * 2
        price = [0.05] * 20 + [0.50, 0.50, 0.05, 0.05]
        d = make_day(0, load, price, soc_start=15.0)
        result = realized_grid_cost(d, [0.0] * 24, cfg)
        assert result["charge_kwh"] == pytest.approx(0.0, abs=1e-6)
        assert result["forced_import_kwh"][20] == pytest.approx(0.5, abs=1e-6)
        assert result["forced_import_kwh"][21] == pytest.approx(1.5, abs=1e-6)
        assert sum(result["forced_import_kwh"]) == pytest.approx(2.0, abs=1e-6)
        assert result["grid_kwh"] == pytest.approx(2.0, abs=1e-6)
        assert result["eur"] == pytest.approx(1.00, abs=1e-6)

    def test_under_charge_forced_import_at_peak_price(self):
        """Forced import is charged at the hour it occurs (peak price)."""
        cfg = make_cfg()
        load = [0.0] * 20 + [1.5, 1.5] + [0.0] * 2
        price = [0.05] * 20 + [0.50, 0.50, 0.05, 0.05]
        d = make_day(0, load, price, soc_start=15.0)
        result = realized_grid_cost(d, [0.0] * 24, cfg)
        # Forced imports (0.5 + 1.5 kWh) at h20-h21, both at price 0.50
        assert result["eur"] == pytest.approx(2.0 * 0.50, abs=1e-6)

    # -- (b) Over-charge beyond headroom ----------------------------------------

    def test_over_charge_full_battery_nothing_stored(self):
        """Battery already at target; entire deliberate charge is waste.

        soc_start=80% (8 kWh) = target. headroom=0.
        Charge 3 AC kWh at h0: g_dc = min(3×1, 0) = 0 stored. 3 AC still paid.
        """
        cfg = make_cfg()
        d = make_day(0, 0, 0.10, soc_start=80.0)
        result = realized_grid_cost(d, [3.0] + [0.0] * 23, cfg)
        assert result["charge_kwh"] == pytest.approx(0.0, abs=1e-6)
        assert result["grid_kwh"] == pytest.approx(3.0, abs=1e-6)
        assert result["eur"] == pytest.approx(0.30, abs=1e-6)
        assert all(x < 1e-9 for x in result["forced_import_kwh"])

    def test_over_charge_partial_headroom_waste_paid(self):
        """Deliberate charge exceeds partial headroom; only headroom DC is stored.

        soc_start=70% (7 kWh). headroom = 8-7 = 1 kWh (DC=AC, eta=1).
        Charge 3 AC at h0: g_dc = min(3×1, 1) = 1 stored. Full 3 AC paid.

        charge_kwh = 1.0 (DC stored)
        grid_kwh   = 3.0 (AC paid)
        eur        = 3 × 0.10 = 0.30
        """
        cfg = make_cfg()
        d = make_day(0, 0, 0.10, soc_start=70.0)
        result = realized_grid_cost(d, [3.0] + [0.0] * 23, cfg)
        assert result["charge_kwh"] == pytest.approx(1.0, abs=1e-6)
        assert result["grid_kwh"] == pytest.approx(3.0, abs=1e-6)
        assert result["eur"] == pytest.approx(0.30, abs=1e-6)
        assert all(x < 1e-9 for x in result["forced_import_kwh"])

    # -- (c) Realized == optimal → no forced imports ----------------------------

    def test_realized_equals_optimal_no_forced_imports(self):
        """Realized schedule matches optimal; no forced imports, totals match optimal.

        soc_start=30% (3 kWh). No PV, no load. Need 5 kWh to reach target.
        Flat price 0.15. Optimal charges 5 kWh cheapest possible.

        charge_kwh           = 5.0 DC (eta=1, so AC==DC)
        forced_import_kwh[h] = 0.0 for all h
        grid_kwh             = 5.0  (matches opt["kwh"])
        eur                  = 0.75 (matches opt["eur"])
        """
        cfg = make_cfg()
        d = make_day(0, 0, 0.15, soc_start=30.0)
        opt = hindsight_optimal_grid(d, cfg)
        assert_feasible(opt["schedule"], d, cfg)   # sanity: schedule itself is valid
        result = realized_grid_cost(d, opt["schedule"], cfg)
        assert result["grid_kwh"] == pytest.approx(opt["kwh"], abs=1e-6)
        assert result["eur"] == pytest.approx(opt["eur"], abs=1e-6)
        assert result["charge_kwh"] == pytest.approx(5.0, abs=1e-6)
        assert all(x < 1e-9 for x in result["forced_import_kwh"])

    def test_realized_equals_optimal_floor_breach_day(self):
        """Optimal schedule on floor-breach day → no forced imports.

        soc_start=40% (4 kWh). Load 1.5 kWh/h at h20-h21.
        Optimal pre-charges to prevent floor breach; realized follows it exactly.
        """
        cfg = make_cfg()
        load = [0.0] * 20 + [1.5, 1.5] + [0.0] * 2
        price = [0.05] * 20 + [0.50, 0.50, 0.05, 0.05]
        d = make_day(0, load, price, soc_start=40.0)
        opt = hindsight_optimal_grid(d, cfg)
        result = realized_grid_cost(d, opt["schedule"], cfg)
        assert all(x < 1e-9 for x in result["forced_import_kwh"])
        assert result["grid_kwh"] == pytest.approx(opt["kwh"], abs=1e-6)
        assert result["eur"] == pytest.approx(opt["eur"], abs=1e-6)

    # -- (d) Return-dict structure ----------------------------------------------

    def test_return_dict_has_all_keys(self):
        """Return dict contains all four expected keys."""
        cfg = make_cfg()
        d = make_day(0, 0, 0.10, soc_start=50.0)
        result = realized_grid_cost(d, [0.0] * 24, cfg)
        assert "grid_kwh" in result
        assert "eur" in result
        assert "charge_kwh" in result
        assert "forced_import_kwh" in result

    def test_forced_import_kwh_is_list_of_24(self):
        """forced_import_kwh is a per-hour list with exactly 24 elements."""
        cfg = make_cfg()
        d = make_day(0, 0, 0.10, soc_start=50.0)
        result = realized_grid_cost(d, [0.0] * 24, cfg)
        assert isinstance(result["forced_import_kwh"], list)
        assert len(result["forced_import_kwh"]) == 24

    # -- (e) Validation ---------------------------------------------------------

    def test_raises_on_wrong_length_realized_charge(self):
        """realized_charge_by_hour with != 24 elements raises ValueError."""
        cfg = make_cfg()
        d = make_day(0, 0, 0.10, soc_start=50.0)
        with pytest.raises(ValueError, match="realized_charge_by_hour"):
            realized_grid_cost(d, [0.0] * 23, cfg)


# ---------------------------------------------------------------------------
# hindsight_optimal_grid — export leg (F1)
# ---------------------------------------------------------------------------

class TestHindsightExport:
    """Export leg: oracle credits discharge→export revenue when export_price supplied.

    All cases use clean numbers: capacity=10 kWh, floor=20% (2 kWh),
    target=80% (8 kWh), max_charge_w=3000 (3 kWh/h), eta_charge=1.0,
    round_trip_eff=1.0 (so eta_discharge=1.0, simpler arithmetic),
    cycle_cost=0.0 unless stated.
    """

    # -- helper ---------------------------------------------------------------

    @staticmethod
    def _export_cfg(**overrides) -> "Config":
        """Config tuned for export tests: round_trip_eff=1.0, cycle_cost=0."""
        defaults = dict(
            capacity_kwh=10.0,
            soc_floor=20.0,
            soc_target=80.0,
            max_charge_w=3000.0,
            eta_charge=1.0,
            round_trip_eff=1.0,
            cycle_cost_eur_per_kwh=0.0,
        )
        defaults.update(overrides)
        return Config(**defaults)

    # -- volatile day with export price credits export revenue ----------------

    def test_export_credits_revenue_and_lowers_net_eur(self):
        """Oracle imports cheap, exports expensive, re-imports cheap — net eur negative.

        Setup:
          soc_start=20% (2 kWh = floor).  No PV, no load.
          max_export_w not set → defaults to 6000W (6 kWh/h DC).
          price: 0.10 h0-h7, 0.50 h8-h15, 0.10 h16-h23.
          export_price: 0.40 at h8-h15, 0.0 elsewhere.
          eta_discharge=1.0, cycle_cost=0.0 → net export revenue = 0.40/DC kWh.

        Oracle:
          Import 6 kWh (3+3) at h0-h1 → soc=8. Cost=0.60 €.
          Export 6 kWh at h8 (soc 8→2=floor). Revenue=2.40 €.
          Re-import 6 kWh (3+3) at h16-h17 → soc=8. Cost=0.60 €.
          Total import: 12 kWh. Import cost: 1.20 €.
          Net eur = 1.20 − 2.40 = −1.20 €.

        Without export_price: import 6 kWh at 0.10, eur = 0.60 €.
        """
        cfg = self._export_cfg()
        price = [0.10] * 8 + [0.50] * 8 + [0.10] * 8
        ep = [0.0] * 8 + [0.40] * 8 + [0.0] * 8
        d = make_day(0, 0, price, soc_start=20.0)

        # No export: fill to target cheaply.
        opt_no_export = hindsight_optimal_grid(d, cfg)
        assert opt_no_export["eur"] == pytest.approx(0.60, abs=0.10)

        # With export: import → export → re-import arbitrage is profitable.
        opt = hindsight_optimal_grid(d, cfg, export_price=ep)
        assert opt["eur"] < opt_no_export["eur"] - 0.50, (
            "Export-price scenario must meaningfully lower net eur vs no-export"
        )
        assert opt["export_kwh"] >= 5.0  # at least 5 kWh exported
        assert opt["export_revenue_eur"] >= 2.0  # at least 2 € revenue
        # Net eur = import_cost − export_revenue_eur
        import_cost = sum(opt["schedule"][h] * price[h] for h in range(24))
        assert opt["eur"] == pytest.approx(import_cost - opt["export_revenue_eur"], abs=0.05)

    # -- no export_price → identical result to charge-only -------------------

    def test_no_export_price_identical_to_charge_only(self):
        """Passing export_price=None must produce bit-identical result as omitting it."""
        cfg = self._export_cfg()
        price = [0.10] * 8 + [0.50] * 8 + [0.10] * 8
        d = make_day(0, 0, price, soc_start=20.0)

        opt_default = hindsight_optimal_grid(d, cfg)
        opt_none = hindsight_optimal_grid(d, cfg, export_price=None)

        assert opt_default["kwh"] == pytest.approx(opt_none["kwh"], abs=1e-9)
        assert opt_default["eur"] == pytest.approx(opt_none["eur"], abs=1e-9)
        assert opt_default["schedule"] == opt_none["schedule"]
        # export_kwh and export_revenue_eur absent or zero
        assert opt_none.get("export_kwh", 0.0) == pytest.approx(0.0, abs=1e-9)
        assert opt_none.get("export_revenue_eur", 0.0) == pytest.approx(0.0, abs=1e-9)

    # -- export never drives end-SoC below floor ------------------------------

    def test_export_respects_floor(self):
        """Oracle never drives SoC below soc_floor during export.

        soc_start=30% (3 kWh), floor=20% (2 kWh) → at most 1 kWh exportable
        from initial charge before hitting floor.  Regardless of how many hours
        have high export_price, total exported DC must not exceed what puts SoC
        at floor.
        """
        cfg = self._export_cfg()
        # Huge export incentive every hour.
        export_price = [1.00] * 24
        price = [0.05] * 24  # cheap to re-import
        d = make_day(0, 0, price, soc_start=30.0)  # 3 kWh, floor=2 kWh

        opt = hindsight_optimal_grid(d, cfg, export_price=export_price)

        # Verify SoC never below floor via forward simulation.
        from custom_components.anker_x1_smartgrid.regret import _apply_solar_load
        cap_kwh = cfg.capacity_kwh
        floor_kwh = cfg.soc_floor / 100.0 * cap_kwh
        target_kwh = cfg.soc_target / 100.0 * cap_kwh
        soc = d.soc_start / 100.0 * cap_kwh
        schedule = opt["schedule"]
        export_sched = opt.get("export_schedule", [0.0] * 24)
        # eta_d = round_trip_eff / eta_charge (clamped to 1.0) — same as optimize.eta_discharge.
        eta_d = min(cfg.round_trip_eff / (cfg.eta_charge if cfg.eta_charge > 1e-9 else 1.0), 1.0)
        for h in range(24):
            net = d.pv_kwh[h] - d.load_kwh[h]
            soc = _apply_solar_load(soc, net, cfg)
            # Apply grid charge (DC = AC * eta).
            g_ac = schedule[h]
            g_dc = g_ac * (cfg.eta_charge if cfg.eta_charge > 1e-9 else 1.0)
            headroom = max(0.0, target_kwh - soc)
            soc = soc + min(g_dc, headroom)
            # Apply export discharge: export_schedule contains AC kWh exported.
            # DC discharged from battery = AC / eta_d  (since AC_out = DC * eta_d).
            e_ac = export_sched[h]
            e_dc = e_ac / eta_d if eta_d > 1e-9 else e_ac
            soc = soc - e_dc
            assert soc >= floor_kwh - 1e-3, f"h{h}: SoC {soc:.3f} below floor {floor_kwh:.3f}"

    # -- export no-op when hurdle does not clear ------------------------------

    def test_export_noop_when_hurdle_fails(self):
        """When export net revenue ≤ 0, export term is a no-op.

        cycle_cost=0.04, export_price=0.03 → net = 0.03×1.0 − 0.04 = −0.01 < 0.
        Oracle must produce identical result to charge-only (no export).
        """
        cfg = self._export_cfg(cycle_cost_eur_per_kwh=0.04)
        price = [0.10] * 8 + [0.50] * 8 + [0.10] * 8
        export_price = [0.03] * 24  # below hurdle: 0.03×1.0 − 0.04 = −0.01 < 0
        d = make_day(0, 0, price, soc_start=20.0)

        opt_charge_only = hindsight_optimal_grid(d, cfg)
        opt_with_export = hindsight_optimal_grid(d, cfg, export_price=export_price)

        assert opt_charge_only["kwh"] == pytest.approx(opt_with_export["kwh"], abs=1e-6)
        assert opt_charge_only["eur"] == pytest.approx(opt_with_export["eur"], abs=1e-6)
        assert opt_with_export.get("export_kwh", 0.0) == pytest.approx(0.0, abs=1e-6)
        assert opt_with_export.get("export_revenue_eur", 0.0) == pytest.approx(0.0, abs=1e-6)

    # -- return structure always complete (parity gate supplement) ------------

    def test_return_structure_always_has_export_keys(self):
        """hindsight_optimal_grid always returns export_kwh / export_revenue_eur / export_schedule.

        Both the no-export_price path and the export_price path must contain
        these keys so callers can read them unconditionally.
        """
        cfg = make_cfg()  # standard config
        d = make_day(0, 0, 0.10, soc_start=50.0)

        opt_no = hindsight_optimal_grid(d, cfg)
        assert "export_kwh" in opt_no
        assert "export_revenue_eur" in opt_no
        assert "export_schedule" in opt_no
        assert len(opt_no["export_schedule"]) == 24
        assert opt_no["export_kwh"] == pytest.approx(0.0, abs=1e-9)
        assert opt_no["export_revenue_eur"] == pytest.approx(0.0, abs=1e-9)

        opt_with = hindsight_optimal_grid(d, cfg, export_price=[0.40] * 24)
        assert "export_kwh" in opt_with
        assert "export_revenue_eur" in opt_with
        assert "export_schedule" in opt_with
        assert len(opt_with["export_schedule"]) == 24


# ---------------------------------------------------------------------------
# realized_grid_cost — eta != 1.0 (guards AC↔DC accounting for forced imports)
# ---------------------------------------------------------------------------

class TestRealizedGridCostEta:
    """Verify forced-import and deliberate-charge physics under real eta (0.9).

    Forced imports are 1:1 (grid→load directly, no battery eta) so the same
    1 kWh DC shortfall → 1.0 AC regardless of eta.  Deliberate charges still
    convert through eta (g_dc = g_ac × eta).  These tests pin both behaviours
    to guard against regressions in the /eta path.
    """

    def test_eta_0_9_forced_import_is_1_to_1_not_divided_by_eta(self):
        """0.5 kWh DC shortfall at eta=0.9 must give forced_import=0.5, eur=0.25.

        Bug: forced_ac = shortfall/eta → 0.556 kWh (WRONG).
        Fix: forced_ac = shortfall (1:1, grid serves load directly).

        Setup: soc_start=15% (1.5 kWh), load 1.5 kWh/h at h20, no PV, eta=0.9.
        Firmware floor = 0.5 kWh (5% of 10 kWh).
        h20: 1.5→0.0 < firmware floor 0.5. shortfall=0.5.
             forced_ac=0.5 (NOT 0.5/0.9). eur = 0.5 × 0.50 = 0.25.
        """
        cfg = make_cfg(eta_charge=0.9)
        load = [0.0] * 20 + [1.5] + [0.0] * 3
        price = [0.05] * 20 + [0.50] + [0.05] * 3
        d = make_day(0, load, price, soc_start=15.0)
        result = realized_grid_cost(d, [0.0] * 24, cfg)
        assert result["forced_import_kwh"][20] == pytest.approx(0.5, abs=1e-6)
        assert sum(result["forced_import_kwh"]) == pytest.approx(0.5, abs=1e-6)
        assert result["kwh"] == pytest.approx(0.5, abs=1e-6)
        assert result["eur"] == pytest.approx(0.25, abs=1e-6)

    def test_eta_0_9_realized_equals_optimal_no_forced_imports(self):
        """Realized == optimal on a simple night-charge day with eta=0.9.

        soc_start=30% (3 kWh DC). No PV, no load. Need 5 kWh DC to reach 8 kWh.
        eta=0.9 → optimal charges 5/0.9 ≈ 5.556 AC at flat price 0.15.
        optimal["kwh"] ≈ 5.556, optimal["eur"] ≈ 0.833.

        realized_grid_cost with the optimal schedule must:
        * forced_import_kwh all-zero (schedule is feasible by construction).
        * kwh and eur match optimal to floating-point tolerance.
        * charge_kwh = 5.0 DC (eta converts AC to DC).
        """
        cfg = make_cfg(eta_charge=0.9)
        d = make_day(0, 0, 0.15, soc_start=30.0)
        opt = hindsight_optimal_grid(d, cfg)
        assert_feasible(opt["schedule"], d, cfg)   # sanity: schedule is valid
        result = realized_grid_cost(d, opt["schedule"], cfg)
        assert all(x < 1e-9 for x in result["forced_import_kwh"])
        assert result["kwh"] == pytest.approx(opt["kwh"], abs=1e-4)
        assert result["eur"] == pytest.approx(opt["eur"], abs=1e-4)
        assert result["charge_kwh"] == pytest.approx(5.0, abs=0.06)  # 5 kWh DC stored


# ---------------------------------------------------------------------------
# realized_grid_cost — export revenue leg (F3)
# ---------------------------------------------------------------------------

class TestRealizedGridCostExport:
    """F3: realized_grid_cost honours actual export revenue when supplied.

    Tests cover:
    (a) A day with realized export → eur is net (import cost minus export revenue).
    (b) Commanded setpoint ≠ actual metered export → realized leg tracks ACTUAL.
    (c) Charge-only day (no export) → unchanged behaviour (exact parity).
    (d) export_revenue_eur key present in return dict when export supplied.
    (e) export_revenue_eur = 0.0 when no export supplied (backwards-compat).

    Config: capacity=10 kWh, floor=20% (2 kWh), target=80% (8 kWh),
    max_charge_w=3000 W, eta_charge=1.0, round_trip_eff=1.0,
    cycle_cost=0.0 (clean arithmetic).
    """

    @staticmethod
    def _cfg(**overrides) -> Config:
        defaults = dict(
            capacity_kwh=10.0,
            soc_floor=20.0,
            soc_target=80.0,
            max_charge_w=3000.0,
            eta_charge=1.0,
            round_trip_eff=1.0,
            cycle_cost_eur_per_kwh=0.0,
        )
        defaults.update(overrides)
        return Config(**defaults)

    # -- (a) Export day: eur is net of export revenue --------------------------

    def test_export_reduces_eur(self):
        """Realized export revenue is subtracted from grid cost.

        Setup: no PV, no load, soc_start=50%.
        Import 2 kWh at h0 → 0.20 €.
        Actual export 1 kWh at h8 → revenue = 1.0 × 0.40 = 0.40 €.
        Net eur = 0.20 − 0.40 = −0.20 €.
        """
        cfg = self._cfg()
        d = make_day(0, 0, [0.10] * 24, soc_start=50.0)
        charge_by_hour = [2.0] + [0.0] * 23
        actual_export = [0.0] * 8 + [1.0] + [0.0] * 15
        ep = [0.0] * 8 + [0.40] + [0.0] * 15

        result = realized_grid_cost(
            d, charge_by_hour, cfg,
            realized_export_by_hour=actual_export,
            export_price=ep,
        )

        import_eur = 2.0 * 0.10   # 0.20
        export_rev = 1.0 * 0.40   # 0.40
        assert result["eur"] == pytest.approx(import_eur - export_rev, abs=1e-6)
        assert result["export_revenue_eur"] == pytest.approx(export_rev, abs=1e-6)

    # -- (b) Commanded setpoint ≠ actual export → tracks ACTUAL ----------------

    def test_realized_tracks_actual_not_commanded_setpoint(self):
        """When commanded != actual, regret math uses actual (metered) export.

        commanded_export = 3.0 kWh (NOT used by realized_grid_cost).
        actual_export    = 1.5 kWh (the value passed as realized_export_by_hour).

        Revenue must be based on actual 1.5 kWh, not commanded 3.0 kWh.
        """
        cfg = self._cfg()
        d = make_day(0, 0, [0.10] * 24, soc_start=50.0)
        actual_export = [0.0] * 10 + [1.5] + [0.0] * 13   # actual metered
        # commanded_export_kwh = 3.0 is deliberately NOT passed
        ep = [0.0] * 10 + [0.50] + [0.0] * 13

        result = realized_grid_cost(
            d, [0.0] * 24, cfg,
            realized_export_by_hour=actual_export,
            export_price=ep,
        )

        # Revenue = 1.5 × 0.50 = 0.75 (NOT 3.0 × 0.50 = 1.50)
        assert result["export_revenue_eur"] == pytest.approx(0.75, abs=1e-6)
        # eur = 0 (no import) - 0.75 = -0.75
        assert result["eur"] == pytest.approx(-0.75, abs=1e-6)

    # -- (c) Charge-only day: no export → identical behaviour ------------------

    def test_charge_only_day_unchanged(self):
        """Passing export_by_hour=None must produce bit-identical result."""
        cfg = self._cfg()
        d = make_day(0, 0, [0.15] * 24, soc_start=30.0)
        charge_by_hour = [1.0] + [0.0] * 23

        result_no_export = realized_grid_cost(d, charge_by_hour, cfg)
        result_explicit_none = realized_grid_cost(
            d, charge_by_hour, cfg,
            realized_export_by_hour=None,
            export_price=None,
        )

        assert result_no_export["kwh"] == pytest.approx(result_explicit_none["kwh"], abs=1e-9)
        assert result_no_export["eur"] == pytest.approx(result_explicit_none["eur"], abs=1e-9)

    # -- (d) export_revenue_eur key in return dict when export supplied ---------

    def test_export_revenue_key_present(self):
        """Return dict always contains export_revenue_eur when export is supplied."""
        cfg = self._cfg()
        d = make_day(0, 0, [0.10] * 24, soc_start=50.0)
        actual_export = [0.0] * 12 + [1.0] + [0.0] * 11
        ep = [0.0] * 12 + [0.30] + [0.0] * 11

        result = realized_grid_cost(
            d, [0.0] * 24, cfg,
            realized_export_by_hour=actual_export,
            export_price=ep,
        )

        assert "export_revenue_eur" in result
        assert result["export_revenue_eur"] >= 0.0

    # -- (e) export_revenue_eur = 0.0 without export (backwards-compat) --------

    def test_export_revenue_zero_when_no_export(self):
        """export_revenue_eur is 0.0 when no export params are passed."""
        cfg = self._cfg()
        d = make_day(0, 0, [0.10] * 24, soc_start=50.0)
        result = realized_grid_cost(d, [0.0] * 24, cfg)
        assert result.get("export_revenue_eur", 0.0) == pytest.approx(0.0, abs=1e-9)

    # -- (f) regret score reflects export on both sides -----------------------

    def test_daily_regret_reflects_export_revenue_both_sides(self):
        """score_regret computes correct regret when both oracle and realized have export.

        Setup: soc_start=20% (2 kWh = floor). No PV, no load.
        Oracle: import 6 kWh cheap (h0-h1, 0.10), export 6 kWh (h8, 0.40).
        Realized: import 6 kWh (h0-h1), export 6 kWh (h8 — actual metered).
        Both sides identical → regret_eur ≈ 0.

        (Tests parity: realized export leg accounting mirrors oracle export leg.)
        """
        cfg = self._cfg()
        price = [0.10] * 8 + [0.50] * 8 + [0.10] * 8
        ep = [0.0] * 8 + [0.40] * 8 + [0.0] * 8
        d = make_day(0, 0, price, soc_start=20.0)

        # Oracle (F1) with export leg.
        opt = hindsight_optimal_grid(d, cfg, export_price=ep)

        # Build realized export from the oracle's export_schedule
        # (simulating metered actual == planned on a perfect day).
        realized_export = list(opt["export_schedule"])

        realized = realized_grid_cost(
            d, opt["schedule"], cfg,
            realized_export_by_hour=realized_export,
            export_price=list(ep),
        )

        score = score_regret(realized, opt)
        # regret_eur = realized["eur"] − opt["eur"]; both have export revenue → ~0.
        assert abs(score["regret_eur"]) < 0.10, (
            f"Regret on perfect export day must be near-zero; got {score['regret_eur']:.4f}"
        )

    # -- (g) Export drains SoC in the simulation loop -------------------------

    def test_export_drains_soc_and_triggers_forced_import(self):
        """Change B: realized_grid_cost subtracts exported DC from SoC each hour.

        Setup: cap=10 kWh, soc_floor=20% (2 kWh soft), firmware floor 0.5 kWh,
        soc_start=10% (1.0 kWh).
        No PV, no load, no deliberate charge.
        Export 1.5 AC kWh at h0 with eta_d=1.0 → e_dc=1.5 kWh.

        SoC trajectory with B:
          Before step 2.5: soc = 1.0 kWh
          After export drain:  soc = 1.0 − 1.5 = −0.5 kWh  (< firmware floor 0.5)
          Forced import step:  forced_ac = 0.5 − (−0.5) = 1.0 kWh, soc = 0.5

        With B (post-fix):
          kwh = 0.0 (no deliberate charge) + 1.0 (forced import) = 1.0
          eur = 1.0 × 0.20 (price) − 1.5 × 0.40 (export) = 0.20 − 0.60 = −0.40
          forced_import_kwh[0] = 1.0 > 0
        """
        cfg = self._cfg()
        price = [0.20] + [0.10] * 23
        ep = [0.40] + [0.0] * 23
        d = make_day(0, 0, price, soc_start=10.0)   # 1 kWh; firmware floor=0.5 kWh

        actual_export = [1.5] + [0.0] * 23          # 1.5 AC kWh at h0

        result = realized_grid_cost(
            d, [0.0] * 24, cfg,
            realized_export_by_hour=actual_export,
            export_price=ep,
        )

        # Forced import must appear: export drained SoC below firmware floor.
        assert result["forced_import_kwh"][0] == pytest.approx(1.0, abs=1e-6), (
            f"h0 forced import must be 1.0 kWh (SoC 1→−0.5→firmware floor=0.5); "
            f"got {result['forced_import_kwh'][0]:.6f}"
        )
        assert result["kwh"] == pytest.approx(1.0, abs=1e-6), (
            f"total kwh must be 1.0 (forced import only); got {result['kwh']:.6f}"
        )
        # eur = 1.0×0.20 − 1.5×0.40 = 0.20 − 0.60 = −0.40
        assert result["eur"] == pytest.approx(-0.40, abs=1e-6), (
            f"eur must be −0.40 (forced import cost − export revenue); got {result['eur']:.6f}"
        )
        assert result["export_revenue_eur"] == pytest.approx(0.60, abs=1e-6), (
            f"export revenue must be 1.5×0.40=0.60; got {result['export_revenue_eur']:.6f}"
        )
