"""T2: apply_min_export_block — pure filter + revenue recompute.

Spec: docs/superpowers/specs/2026-06-27-minimum-export-block-design.md §Mechanism.

Test structure (TDD — written before implementation):
  - Drop / keep decisions (per-RUN segmentation, threshold boundary)
  - In-progress exemption (exempt_index protects its run)
  - Revenue formula correctness vs. hand-computed expected value
  - No-op guard paths (min=0.0 and export_price=None)
"""

from typing import Any

import pytest

from custom_components.anker_x1_smartgrid.export_filter import apply_min_export_block
from custom_components.anker_x1_smartgrid.models import Config
from custom_components.anker_x1_smartgrid.optimize import eta_discharge


def _cfg(**kw: Any) -> Config:
    """Minimal Config with eta_d=1 and cycle_cost=0.04 by default."""
    defaults: dict[str, Any] = dict(
        capacity_kwh=10.0,
        soc_floor=10.0,
        soc_target=90.0,
        max_charge_w=3000.0,
        eta_charge=1.0,
        round_trip_eff=1.0,
        cycle_cost_eur_per_kwh=0.04,
        export_fee_eur_per_kwh=0.0,
        max_export_w=3000.0,
        grid_export_limit_w=3000.0,
        export_min_block_kwh=0.5,
    )
    defaults.update(kw)
    return Config(**defaults)


# ---------------------------------------------------------------------------
# Drop / keep decisions
# ---------------------------------------------------------------------------


def test_single_sub_min_run_is_dropped():
    """A single-hour sub-threshold run (0.23 < 0.5) is zeroed out."""
    cfg = _cfg(export_min_block_kwh=0.5)
    export_ac = [0.0, 0.0, 0.23, 0.0]
    prices = [0.20, 0.20, 0.30, 0.20]
    filtered, _ = apply_min_export_block(export_ac, prices, cfg, exempt_index=10)
    assert filtered == [0.0, 0.0, 0.0, 0.0]
    assert len(filtered) == len(export_ac)


def test_multi_hour_run_summing_above_min_kept_whole():
    """A multi-hour run totalling >= min is kept whole even though each hour
    is individually below min (validates per-run, not per-hour, logic)."""
    cfg = _cfg(export_min_block_kwh=0.5)
    # 0.2 + 0.2 + 0.2 = 0.6 >= 0.5 → kept
    export_ac = [0.2, 0.2, 0.2]
    prices = [0.30, 0.30, 0.30]
    filtered, _ = apply_min_export_block(export_ac, prices, cfg, exempt_index=10)
    assert filtered == pytest.approx([0.2, 0.2, 0.2])


def test_run_exactly_at_threshold_kept():
    """Boundary condition: sum == min → KEPT (strictly-less-than drops)."""
    cfg = _cfg(export_min_block_kwh=0.5)
    export_ac = [0.25, 0.25]  # sum exactly == 0.5
    prices = [0.30, 0.30]
    filtered, _ = apply_min_export_block(export_ac, prices, cfg, exempt_index=10)
    assert filtered == pytest.approx([0.25, 0.25])


def test_two_adjacent_runs_split_by_zero_hour_judged_independently():
    """A zero hour breaks a run; each sub-run is judged separately."""
    cfg = _cfg(export_min_block_kwh=0.5)
    # Run 1: h=0, total=0.6 → kept; zero gap at h=1; Run 2: h=2, total=0.3 → dropped
    export_ac = [0.6, 0.0, 0.3]
    prices = [0.30, 0.30, 0.30]
    filtered, _ = apply_min_export_block(export_ac, prices, cfg, exempt_index=10)
    assert filtered[0] == pytest.approx(0.6)
    assert filtered[1] == pytest.approx(0.0)
    assert filtered[2] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# In-progress exemption
# ---------------------------------------------------------------------------


def test_in_progress_run_at_index_0_kept_even_if_sub_min():
    """Run containing exempt_index=0 is never dropped regardless of size."""
    cfg = _cfg(export_min_block_kwh=0.5)
    export_ac = [0.1, 0.0, 0.0]  # sub-min single-hour run at h=0
    prices = [0.30, 0.30, 0.30]
    filtered, _ = apply_min_export_block(export_ac, prices, cfg, exempt_index=0)
    assert filtered[0] == pytest.approx(0.1)  # kept (in-progress)


def test_same_sub_min_run_dropped_when_not_exempt():
    """Identical sub-min run at h=0 is dropped when exempt_index points elsewhere."""
    cfg = _cfg(export_min_block_kwh=0.5)
    export_ac = [0.1, 0.0, 0.0]
    prices = [0.30, 0.30, 0.30]
    filtered, _ = apply_min_export_block(export_ac, prices, cfg, exempt_index=5)
    assert filtered[0] == pytest.approx(0.0)  # dropped (future run)


# ---------------------------------------------------------------------------
# Revenue formula
# ---------------------------------------------------------------------------


def test_revenue_formula_correctness():
    """Returned revenue mirrors optimize.py:828-832:
    net_rev = Σ ac[h]·price[h] − Σ (ac[h]/eta_d)·cycle_cost.

    Using eta_charge=1.0, round_trip_eff=1.0 → eta_d=1.0 simplifies to:
        rev = Σ ac·(price − cycle_cost)
    Making hand-computation exact.
    """
    cfg = _cfg(eta_charge=1.0, round_trip_eff=1.0, cycle_cost_eur_per_kwh=0.04)
    # Run sums to 0.7 >= 0.5 → kept
    export_ac = [0.0, 0.3, 0.4]
    prices = [0.0, 0.30, 0.25]

    _, net_rev = apply_min_export_block(export_ac, prices, cfg, exempt_index=10)

    eta_d = eta_discharge(cfg)  # == 1.0
    expected = sum(
        export_ac[h] * prices[h] - (export_ac[h] / eta_d) * cfg.cycle_cost_eur_per_kwh for h in range(len(export_ac))
    )
    assert net_rev == pytest.approx(expected)


def test_revenue_zero_after_all_runs_dropped():
    """When all runs are dropped, returned revenue is 0.0."""
    cfg = _cfg(export_min_block_kwh=0.5)
    export_ac = [0.0, 0.23, 0.0]  # single sub-min run, not exempt
    prices = [0.30, 0.30, 0.30]
    _, net_rev = apply_min_export_block(export_ac, prices, cfg, exempt_index=10)
    assert net_rev == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# No-op guard paths
# ---------------------------------------------------------------------------


def test_min_zero_is_noop_values_identical():
    """min=0.0 → no filtering applied; returned values match input and revenue
    equals the full-schedule recompute (locks the documented no-op contract)."""
    cfg = _cfg(export_min_block_kwh=0.0)
    export_ac = [0.1, 0.0, 0.3, 0.0, 0.05]
    prices = [0.30] * 5
    filtered, rev = apply_min_export_block(export_ac, prices, cfg, exempt_index=10)
    assert filtered == pytest.approx(export_ac)
    eta_d = eta_discharge(cfg)
    expected_rev = sum(
        export_ac[h] * prices[h] - (export_ac[h] / eta_d) * cfg.cycle_cost_eur_per_kwh for h in range(len(export_ac))
    )
    assert rev == pytest.approx(expected_rev)


def test_min_zero_returns_defensive_copy():
    """min=0.0 no-op still returns a copy, not the original list object."""
    cfg = _cfg(export_min_block_kwh=0.0)
    export_ac = [0.1, 0.2]
    prices = [0.30, 0.30]
    filtered, _ = apply_min_export_block(export_ac, prices, cfg, exempt_index=10)
    assert filtered is not export_ac


def test_original_list_values_unchanged_after_drop():
    """Dropping runs must not mutate the caller's export_ac list."""
    cfg = _cfg(export_min_block_kwh=0.5)
    export_ac = [0.0, 0.23, 0.0]  # sub-min run that will be dropped
    original_snapshot = list(export_ac)
    prices = [0.30, 0.30, 0.30]
    apply_min_export_block(export_ac, prices, cfg, exempt_index=10)
    assert export_ac == original_snapshot  # caller's list untouched


def test_negative_min_kwh_is_noop():
    """Negative min_kwh behaves like 0 — no filtering applied."""
    cfg = _cfg(export_min_block_kwh=-1.0)
    export_ac = [0.1, 0.0, 0.05]
    prices = [0.30, 0.30, 0.30]
    filtered, _ = apply_min_export_block(export_ac, prices, cfg, exempt_index=10)
    assert filtered == pytest.approx(export_ac)


def test_none_price_returns_copy_and_zero_revenue():
    """export_price=None → defensive no-op: copy of schedule, revenue=0.0."""
    cfg = _cfg()
    export_ac = [0.1, 0.2, 0.3]
    filtered, rev = apply_min_export_block(export_ac, None, cfg, exempt_index=0)
    assert filtered == export_ac
    assert filtered is not export_ac
    assert rev == pytest.approx(0.0)


def test_none_price_does_not_crash_on_empty():
    """export_price=None with empty schedule doesn't crash."""
    cfg = _cfg()
    filtered, rev = apply_min_export_block([], None, cfg, exempt_index=0)
    assert filtered == []
    assert rev == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Per-slot tail-trim (spec: 2026-06-28-export-min-block-tail-trim-design.md)
# ---------------------------------------------------------------------------


def test_tail_trim_removes_trailing_sub_threshold_slot():
    """[0.65, 0.28] → [0.65, 0] — trailing sub-min slot is zeroed.

    Tonight's live case: a 0.93 kWh run survives the per-run drop but its
    second hour (0.28 kWh) is below the 0.5 kWh threshold.  The tail-trim
    should zero the trailing slot and leave the core (0.65) intact.
    """
    cfg = _cfg(export_min_block_kwh=0.5)
    export_ac = [0.65, 0.28]
    prices = [0.30, 0.30]
    filtered, _ = apply_min_export_block(export_ac, prices, cfg, exempt_index=10)
    assert filtered == pytest.approx([0.65, 0.0])


def test_tail_trim_removes_leading_sub_threshold_slot():
    """[0.28, 0.65] → [0, 0.65] — leading sub-min slot is zeroed."""
    cfg = _cfg(export_min_block_kwh=0.5)
    export_ac = [0.28, 0.65]
    prices = [0.30, 0.30]
    filtered, _ = apply_min_export_block(export_ac, prices, cfg, exempt_index=10)
    assert filtered == pytest.approx([0.0, 0.65])


def test_tail_trim_no_core_run_kept_whole():
    """[0.40, 0.40] → [0.40, 0.40] — no slot >= threshold, kept whole.

    The run total (0.80) >= 0.5 so it survived the per-run drop, but neither
    slot is a 'core'.  Without an anchor the tail-trim leaves it untouched.
    """
    cfg = _cfg(export_min_block_kwh=0.5)
    export_ac = [0.40, 0.40]
    prices = [0.30, 0.30]
    filtered, _ = apply_min_export_block(export_ac, prices, cfg, exempt_index=10)
    assert filtered == pytest.approx([0.40, 0.40])


def test_tail_trim_interior_slot_between_two_cores_kept():
    """[0.60, 0.30, 0.60] → [0.60, 0.30, 0.60] — interior sub-min slot kept.

    Both endpoints are cores; the 0.30 slot sits between them.  Only leading
    slots before the first core and trailing slots after the last core are
    eligible for trim — interior slots are never zeroed.
    """
    cfg = _cfg(export_min_block_kwh=0.5)
    export_ac = [0.60, 0.30, 0.60]
    prices = [0.30, 0.30, 0.30]
    filtered, _ = apply_min_export_block(export_ac, prices, cfg, exempt_index=10)
    assert filtered == pytest.approx([0.60, 0.30, 0.60])


def test_tail_trim_leading_and_trailing_both_trimmed():
    """[0.28, 0.65, 0.28] → [0, 0.65, 0] — both ends trimmed."""
    cfg = _cfg(export_min_block_kwh=0.5)
    export_ac = [0.28, 0.65, 0.28]
    prices = [0.30, 0.30, 0.30]
    filtered, _ = apply_min_export_block(export_ac, prices, cfg, exempt_index=10)
    assert filtered == pytest.approx([0.0, 0.65, 0.0])


def test_tail_trim_exempt_index_stops_leading_trim():
    """[0.28, 0.65] with exempt_index=0 → [0.28, 0.65] — lead NOT trimmed.

    The slot at idx 0 is in-progress (exempt).  Leading trim stops
    immediately upon reaching exempt_index, so idx 0 is preserved.
    """
    cfg = _cfg(export_min_block_kwh=0.5)
    export_ac = [0.28, 0.65]
    prices = [0.30, 0.30]
    filtered, _ = apply_min_export_block(export_ac, prices, cfg, exempt_index=0)
    assert filtered == pytest.approx([0.28, 0.65])


def test_tail_trim_exempt_index_stops_trailing_trim():
    """[0.65, 0.28] with exempt_index=1 → [0.65, 0.28] — tail NOT trimmed.

    The slot at idx 1 is in-progress (exempt).  Trailing trim stops upon
    reaching exempt_index, so the sub-threshold tail is preserved.
    """
    cfg = _cfg(export_min_block_kwh=0.5)
    export_ac = [0.65, 0.28]
    prices = [0.30, 0.30]
    filtered, _ = apply_min_export_block(export_ac, prices, cfg, exempt_index=1)
    assert filtered == pytest.approx([0.65, 0.28])


def test_tail_trim_revenue_reflects_trimmed_zeros():
    """Revenue recompute runs over the FINAL filtered schedule after tail-trim.

    [0.65, 0.28] → [0.65, 0.0] after trim.  Revenue must be computed from
    the trimmed schedule (0.65 kWh only), not from the pre-trim values.
    """
    cfg = _cfg(
        export_min_block_kwh=0.5,
        eta_charge=1.0,
        round_trip_eff=1.0,
        cycle_cost_eur_per_kwh=0.04,
    )
    export_ac = [0.65, 0.28]
    prices = [0.30, 0.28]
    filtered, net_rev = apply_min_export_block(export_ac, prices, cfg, exempt_index=10)

    # Verify the trim happened first
    assert filtered == pytest.approx([0.65, 0.0])

    # eta_d = 1.0 with these settings; revenue from trimmed schedule only
    eta_d = eta_discharge(cfg)
    expected_rev = sum(
        filtered[h] * prices[h] - (filtered[h] / eta_d) * cfg.cycle_cost_eur_per_kwh for h in range(len(filtered))
    )
    assert net_rev == pytest.approx(expected_rev)
    # Sanity: revenue must NOT include the trimmed 0.28 kWh slot
    # (i.e., gross contribution of slot 1 must be absent)
    full_rev = sum(
        export_ac[h] * prices[h] - (export_ac[h] / eta_d) * cfg.cycle_cost_eur_per_kwh for h in range(len(export_ac))
    )
    assert net_rev < full_rev, "Trimmed revenue must be less than pre-trim full revenue"


def test_tail_trim_slot_exactly_at_threshold_is_core():
    """A slot with value exactly == threshold counts as a core (not trimmed).

    Uses >= threshold - 1e-9 guard so a slot at exactly 0.5 kWh is a core
    and triggers no trimming of adjacent slots.
    """
    cfg = _cfg(export_min_block_kwh=0.5)
    export_ac = [0.28, 0.50, 0.28]
    prices = [0.30, 0.30, 0.30]
    filtered, _ = apply_min_export_block(export_ac, prices, cfg, exempt_index=10)
    # 0.50 is the core; both 0.28 slots are lead/tail → trimmed
    assert filtered == pytest.approx([0.0, 0.50, 0.0])


def test_tail_trim_threshold_zero_disables_tail_trim_too():
    """export_min_block_kwh=0 disables BOTH per-run drop and tail-trim."""
    cfg = _cfg(export_min_block_kwh=0.0)
    export_ac = [0.65, 0.28]
    prices = [0.30, 0.30]
    filtered, _ = apply_min_export_block(export_ac, prices, cfg, exempt_index=10)
    assert filtered == pytest.approx([0.65, 0.28])


def test_tail_trim_no_core_exempt_run_kept_whole():
    """A sub-threshold TOTAL run kept only by exempt_index has no core → kept whole.

    export_ac=[0.10, 0.10]: total=0.20 < 0.5 min, would be dropped — but the run
    contains exempt_index=0 so the per-run drop spares it.  Neither slot is a core
    (0.10 < 0.5), so the tail-trim has no anchor and leaves the run untouched.
    """
    cfg = _cfg(export_min_block_kwh=0.5)
    export_ac = [0.10, 0.10]
    prices = [0.30, 0.30]
    filtered, _ = apply_min_export_block(export_ac, prices, cfg, exempt_index=0)
    assert filtered == pytest.approx([0.10, 0.10])


def test_tail_trim_multiple_cores_interior_smalls_kept_both_ends_trimmed():
    """Dual-end trim with two cores and two interior sub-threshold slots.

    export_ac=[0.28, 0.60, 0.30, 0.30, 0.60, 0.28]:
      - Cores at idx 1 (0.60) and idx 4 (0.60).
      - Leading slot idx 0 (0.28) is before the first core → trimmed.
      - Trailing slot idx 5 (0.28) is after the last core → trimmed.
      - Interior smalls idx 2 (0.30) and idx 3 (0.30) sit between the two
        cores → kept.
    """
    cfg = _cfg(export_min_block_kwh=0.5)
    export_ac = [0.28, 0.60, 0.30, 0.30, 0.60, 0.28]
    prices = [0.30] * 6
    filtered, _ = apply_min_export_block(export_ac, prices, cfg, exempt_index=99)
    assert filtered == pytest.approx([0.0, 0.60, 0.30, 0.30, 0.60, 0.0])


def test_tail_trim_exempt_stops_lead_trim_preserving_slot_before_core():
    """Leading trim stops at exempt_index even when a small slot precedes it.

    export_ac=[0.10, 0.10, 0.60], exempt_index=1, threshold=0.5:
      - Per-run drop: run contains exempt_index=1 → kept.
      - Core at idx 2 (0.60); first_core=2.
      - Leading trim: idx 0 (0.10, not exempt, before core) → zeroed.
                      idx 1 == exempt_index → STOP (idx 1 preserved).
      - Trailing trim: idx 2 is the core → STOP immediately.
    Result: [0, 0.10, 0.60].
    """
    cfg = _cfg(export_min_block_kwh=0.5)
    export_ac = [0.10, 0.10, 0.60]
    prices = [0.30, 0.30, 0.30]
    filtered, _ = apply_min_export_block(export_ac, prices, cfg, exempt_index=1)
    assert filtered == pytest.approx([0.0, 0.10, 0.60])


# ---------------------------------------------------------------------------
# eta_curve threading (Task 13) — optional curve param, None branch byte-identical
# ---------------------------------------------------------------------------


def test_eta_curve_none_is_byte_identical():
    """eta_curve=None (default) reproduces today's static-eta_d revenue exactly."""
    cfg = _cfg(export_min_block_kwh=0.0)
    export_ac = [0.6, 0.0, 0.7]
    prices = [0.4, 0.4, 0.4]
    a = apply_min_export_block(export_ac, prices, cfg, exempt_index=0)
    b = apply_min_export_block(export_ac, prices, cfg, exempt_index=0, eta_curve=None)
    assert a == b


def test_eta_curve_lower_discharge_eta_lowers_net_revenue():
    """A curve with a lower discharge eta than the static scalar must yield
    strictly lower net revenue than the eta_curve=None (static) path — proves
    the curve is actually threaded into the revenue recompute, not ignored."""
    from custom_components.anker_x1_smartgrid.efficiency import BinStat, EfficiencyCurve

    cfg = _cfg(export_min_block_kwh=0.0, eta_charge=0.92, round_trip_eff=0.85)
    base = EfficiencyCurve.static(cfg)
    lowered = [
        BinStat(
            b.lo_w,
            b.hi_w,
            "discharge",
            b.eta * 0.5,
            b.measured,
            b.n_runs,
            b.dc_kwh,
            b.confident,
            b.fallback_reason,
        )
        for b in base._discharge
    ]
    curve = EfficiencyCurve(list(base._charge), lowered, base._fc, base._fd)

    export_ac = [0.6, 0.0, 0.7]
    prices = [0.4, 0.4, 0.4]
    _, rev_static = apply_min_export_block(export_ac, prices, cfg, exempt_index=10)
    _, rev_curve = apply_min_export_block(export_ac, prices, cfg, exempt_index=10, eta_curve=curve)
    assert rev_curve < rev_static


def test_tail_trim_cores_scale_with_dt_h():
    """Core threshold is an energy-RATE, not an absolute kWh — it must scale
    with dt_h so tail-trim still fires at 15-min resolution.

    At dt_h=0.25, a 0.15 kWh slot is 0.6 kW average power — the same average
    power as a 0.6 kWh 60-min core.  The per-run total (0.05+0.15*3+0.04=0.54)
    survives the per-run drop (>= 0.5) regardless of dt_h; the fix under test
    is whether the 0.15 kWh slots qualify as *cores* for the tail-trim anchor.
    """
    cfg = _cfg(export_min_block_kwh=0.5)
    # run: [0.05, 0.15, 0.15, 0.15, 0.04] kWh over 15-min slots (total .54 survives per-run)
    ac = [0.0, 0.05, 0.15, 0.15, 0.15, 0.04]
    price = [0.30] * 6
    out, _ = apply_min_export_block(ac, price, cfg, exempt_index=0, dt_h=0.25)
    assert out[1] == 0.0 and out[5] == 0.0  # lead/tail trimmed
    assert out[2] == out[3] == out[4] == 0.15  # cores kept
    # regression: identical input at dt_h=1.0 keeps run whole (no core at 60-min)
    out_60min, _ = apply_min_export_block(ac, price, cfg, exempt_index=0)
    assert out_60min == pytest.approx([0.0, 0.05, 0.15, 0.15, 0.15, 0.04])
