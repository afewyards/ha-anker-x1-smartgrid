"""Integration tests for min-export-block filter wiring in _dp_select_slots.

Spec: docs/superpowers/specs/2026-06-27-minimum-export-block-design.md
§Mechanism + §Single-source guarantee + §Receding-horizon handling + §Testing

Six scenarios:
1. Evening regression (06-28) — peak run kept, sub-min dribble dropped; revenue recomputed.
2. Receding-horizon non-truncation — in-progress run (idx 0) is never dropped.
3. Reserve feasibility — filtered SoC ≥ unfiltered SoC element-wise; strictly higher after
   the dropped hour (proves dropping export actually raised SoC, not just a clamp assertion).
4. Battery-surplus / filter-scope guard — grid_request left untouched by filter while
   export dribble is dropped (filter touches export only, never grid).
5. No-op — export_min_block_kwh=0.0 keeps every DP-scheduled hour intact.
6. Real DP (no mock) — actual optimize_grid emits peak + sub-min dribble; filter drops it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from custom_components.anker_x1_smartgrid.controller import _dp_select_slots
from custom_components.anker_x1_smartgrid.models import (
    Config,
    ForecastInterval,
    PlantInputs,
    PriceSlot,
)
from custom_components.anker_x1_smartgrid.optimize import effective_export_price, eta_discharge

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

NOW = datetime(2026, 6, 28, 17, 0, tzinfo=timezone.utc)  # 17:00 UTC


def _cfg(**overrides) -> Config:
    """Config with sensible evening-export defaults.

    export_peak_band_frac=1.0 and export_peak_lookback_h=0 open the DP band
    gate fully so only the block filter (export_min_block_kwh) is under test
    (except in test 6 which uses the real band filter).
    """
    defaults = dict(
        capacity_kwh=10.0,
        soc_target=97.0,
        soc_floor=10.0,
        eta_charge=0.92,
        max_charge_w=6000.0,
        round_trip_eff=0.85,
        cycle_cost_eur_per_kwh=0.04,
        export_fee_eur_per_kwh=0.0,
        max_export_w=3000.0,
        grid_export_limit_w=3000.0,
        export_peak_band_frac=1.0,      # open band — only block filter in scope
        export_peak_lookback_h=0,
        export_min_block_kwh=0.5,       # default threshold under test
    )
    defaults.update(overrides)
    return Config(**defaults)


def _ivs(now: datetime, n: int, load_w: float = 200.0) -> list:
    """Build *n* × 1-hour ForecastIntervals starting at *now*, zero PV."""
    return [
        ForecastInterval(start=now + timedelta(hours=h), pv_w=0.0, load_w=load_w, dt_h=1.0)
        for h in range(n)
    ]


def _slots(now: datetime, prices: list[float]) -> list:
    return [PriceSlot(now + timedelta(hours=h), p) for h, p in enumerate(prices)]


def _call_dp(
    cfg: Config,
    now: datetime,
    prices: list[float],
    export_schedule: list[float],
    *,
    soc: float = 80.0,
    export_price: float = 0.30,
    export_price_matches_import: bool = True,
    schedule: list[float] | None = None,
):
    """Drive ``_dp_select_slots`` with ``optimize_grid`` mocked.

    The mock returns *export_schedule* as the DP output.
    *schedule* (optional) sets the DP charge schedule (default: all zeros).
    ``export_revenue_eur`` is set to ``999.0`` as a sentinel — the wired filter
    must replace it with the recomputed revenue.
    """
    n = len(prices)
    deadline = now + timedelta(hours=n)
    inputs = PlantInputs(soc=soc, phase_import_w=(0.0, 0.0, 0.0), now=now)
    charge_schedule = schedule if schedule is not None else [0.0] * n
    mock_result = {
        "schedule": charge_schedule,
        "kwh": sum(charge_schedule),
        "eur": 0.0,
        "export_schedule": export_schedule,
        "export_revenue_eur": 999.0,   # sentinel — never valid revenue
    }
    with patch(
        "custom_components.anker_x1_smartgrid.optimize.optimize_grid",
        return_value=mock_result,
    ):
        return _dp_select_slots(
            inputs=inputs,
            slots=_slots(now, prices),
            deadline=deadline,
            ceiling=0.30,
            cfg=cfg,
            export_price=export_price,
            export_price_matches_import=export_price_matches_import,
            reserve_by_hour=[0.5] * n,
            intervals=_ivs(now, n),
        )


def _net_rev(ac: list[float], prices: list[float], cfg: Config) -> float:
    """Net export revenue — mirrors the formula in export_filter.apply_min_export_block."""
    eta_d = eta_discharge(cfg)
    cc = cfg.cycle_cost_eur_per_kwh
    return sum(ac[h] * prices[h] - (ac[h] / eta_d) * cc for h in range(len(ac)))


# ===========================================================================
# 1. Evening regression (06-28) — peak run kept, sub-min dribble dropped
# ===========================================================================

def test_evening_peak_kept_submin_dribble_dropped():
    """06-28 evening case: DP emits a 1.617 kWh peak run and a 0.23 kWh dribble.

    Layout (window indices, NOW=17:00 UTC):
      idx 0 (17:00): 0.0 kWh
      idx 1 (18:00): 0.0 kWh
      idx 2 (19:00): 1.617 kWh  ← peak run (≥ 0.5 min) — MUST BE KEPT
      idx 3 (20:00): 0.0 kWh    ← gap between runs
      idx 4 (21:00): 0.23 kWh   ← sub-min dribble (< 0.5 min) — MUST BE DROPPED
      idx 5 (22:00): 0.0 kWh

    Assertions:
    - 19:00 present in export_request at 1617 W.
    - 21:00 absent from export_request.
    - export_revenue_eur equals revenue from idx 2 only (not 999.0 sentinel).
    - export_revenue_eur is exactly the recomputed filtered net revenue.
    """
    prices = [0.25, 0.28, 0.32, 0.31, 0.30, 0.25]
    export_schedule = [0.0, 0.0, 1.617, 0.0, 0.23, 0.0]
    cfg = _cfg(export_min_block_kwh=0.5)

    _, _, _, export_request, export_revenue_eur, _ = _call_dp(
        cfg, NOW, prices, export_schedule
    )

    hour_19 = NOW + timedelta(hours=2)  # idx 2
    hour_21 = NOW + timedelta(hours=4)  # idx 4

    # Peak run at 19:00 — kept
    assert hour_19 in export_request, "Peak run (1.617 kWh) at 19:00 must remain"
    assert export_request[hour_19] == pytest.approx(1617.0, abs=1.0)

    # Sub-min dribble at 21:00 — dropped
    assert hour_21 not in export_request, "Sub-min dribble (0.23 kWh) at 21:00 must be filtered"

    # Revenue: sentinel (999.0) must be replaced; must equal filtered net revenue
    assert export_revenue_eur != pytest.approx(999.0, abs=1e-3), (
        "export_revenue_eur must be recomputed by the filter, not the DP sentinel"
    )
    ep = [effective_export_price(p, cfg) for p in prices]
    filtered_ac = [0.0, 0.0, 1.617, 0.0, 0.0, 0.0]  # dribble zeroed
    expected_rev = _net_rev(filtered_ac, ep, cfg)
    assert export_revenue_eur == pytest.approx(expected_rev, abs=1e-6), (
        f"export_revenue_eur {export_revenue_eur:.6f} != expected {expected_rev:.6f}"
    )
    assert export_revenue_eur > 0.0, "Kept peak run must yield positive net revenue"


# ===========================================================================
# 2. Receding-horizon non-truncation — in-progress run exempt at idx 0
# ===========================================================================

def test_receding_horizon_inprogress_run_not_truncated():
    """A run starting at idx 0 is exempt even if its remaining total < min.

    Simulates the receding-horizon C1 fix: a 0.6 kWh run [0.2, 0.2, 0.2]
    was admitted when fully future.  One tick later the horizon shrinks: the
    window now starts mid-run with [0.2, 0.2] = 0.4 kWh < 0.5 min.  Without
    the exemption it would be dropped mid-export.  With exempt_index=0 it must
    survive.

    The test represents a tick where the in-progress run occupies idx 0 and 1.
    Total remaining = 0.4 kWh < 0.5 → would be dropped without exemption.
    """
    prices = [0.30, 0.30, 0.28, 0.25, 0.22]
    # In-progress run at idx 0-1: total 0.4 kWh < 0.5 min.
    export_schedule = [0.2, 0.2, 0.0, 0.0, 0.0]
    cfg = _cfg(export_min_block_kwh=0.5)

    _, _, _, export_request, _, _ = _call_dp(cfg, NOW, prices, export_schedule)

    hour_0 = NOW                         # idx 0 — in-progress
    hour_1 = NOW + timedelta(hours=1)    # idx 1 — same run

    # Both hours of the in-progress run must survive (exempt_index=0 protects them)
    assert hour_0 in export_request, (
        "In-progress run at idx 0 must be kept regardless of total (C1 exemption)"
    )
    assert hour_1 in export_request, (
        "Continuation of in-progress run at idx 1 must also be kept"
    )
    assert export_request[hour_0] == pytest.approx(200.0, abs=1.0)
    assert export_request[hour_1] == pytest.approx(200.0, abs=1.0)


# ===========================================================================
# 3. Reserve feasibility — filtered SoC ≥ unfiltered SoC at every hour;
#    strictly higher at/after the dropped dribble hour
# ===========================================================================

def test_reserve_feasibility_filtered_soc_geq_unfiltered():
    """Dropping an export run can only RAISE projected SoC — never lower it.

    Build the SoC trajectory twice via build_plan_horizon:
      (a) with the filtered export_request (min=0.5 drops the dribble at idx 4)
      (b) with the unfiltered export_request (min=0.0 keeps everything)

    Assertions:
    - filtered SoC ≥ unfiltered SoC at every hour (dropping export raises SoC).
    - filtered SoC > unfiltered SoC at idx 4 (strictly higher at the dropped hour).
    - 21:00 is absent from the filtered export_request (precondition).
    """
    from custom_components.anker_x1_smartgrid.plan import build_plan_horizon

    prices = [0.25, 0.28, 0.32, 0.31, 0.30, 0.25]
    export_schedule = [0.0, 0.0, 1.617, 0.0, 0.23, 0.0]
    soc_start = 95.0

    cfg_filtered = _cfg(export_min_block_kwh=0.5, soc_target=97.0, soc_floor=10.0)
    cfg_unfiltered = _cfg(export_min_block_kwh=0.0, soc_target=97.0, soc_floor=10.0)

    selected_f, grid_req_f, _, export_req_f, _, ceil_f = _call_dp(
        cfg_filtered, NOW, prices, export_schedule, soc=soc_start
    )
    _, _, _, export_req_u, _, _ = _call_dp(
        cfg_unfiltered, NOW, prices, export_schedule, soc=soc_start
    )

    hour_21 = NOW + timedelta(hours=4)  # idx 4 — dropped dribble

    # Precondition: dribble IS absent from filtered, present in unfiltered
    assert hour_21 not in export_req_f, "Precondition: dribble must be dropped at idx 4"
    assert hour_21 in export_req_u, "Precondition: unfiltered must keep dribble at idx 4"

    slots = _slots(NOW, prices)
    ivs = _ivs(NOW, len(prices))
    horizon_edge = NOW + timedelta(hours=len(prices))

    def _socs(export_req):
        rows = build_plan_horizon(
            slots,
            intervals=ivs,
            selected=selected_f,
            soc=soc_start,
            horizon_edge=horizon_edge,
            cfg=cfg_filtered,
            grid_request_by_hour=grid_req_f,
            export_request_by_hour=export_req,
            ceiling_by_hour=ceil_f,
        )
        return [row["soc"] for row in rows]

    socs_f = _socs(export_req_f)
    socs_u = _socs(export_req_u)

    assert socs_f, "build_plan_horizon returned no rows"
    assert len(socs_f) == len(socs_u), "Both horizons must have the same number of rows"

    # Filtered SoC must be ≥ unfiltered at every hour (dropping export only raises SoC)
    for i, (sf, su) in enumerate(zip(socs_f, socs_u)):
        assert sf >= su - 1e-6, (
            f"Filtered SoC {sf:.2f}% < unfiltered {su:.2f}% at row {i} — "
            f"filter must never lower SoC"
        )

    # At idx 4 (21:00 UTC) the dribble is dropped → filtered SoC must be strictly higher
    assert socs_f[4] > socs_u[4] + 1e-6, (
        f"Filtered SoC {socs_f[4]:.2f}% must be strictly > unfiltered {socs_u[4]:.2f}% "
        f"at the dropped-dribble hour (idx 4 = 21:00 UTC)"
    )


# ===========================================================================
# 4. Filter-scope guard — filter touches export only, never grid_request
# ===========================================================================

def test_filter_touches_export_only_not_grid():
    """Filter drops sub-min export run but leaves grid_request completely untouched.

    The mock returns a DP result where:
      - charge schedule: idx 3 → 0.5 kWh (grid charging at 20:00, NO overlap with export)
      - export schedule: idx 2 → 1.617 kWh (peak), idx 4 → 0.23 kWh (dribble)

    After filtering (min=0.5):
      - grid_request[20:00] still present at 500 W (filter never touches charging)
      - export_request[19:00] still present at 1617 W (peak ≥ 0.5 kWh, kept)
      - export_request[21:00] ABSENT (0.23 kWh < 0.5 kWh, dropped)

    No charge/export overlap → net-out pass is a no-op; distinguishes "filter dropped
    it" from "DP never scheduled it" without triggering the net-out mechanism.
    """
    prices = [0.25, 0.28, 0.32, 0.31, 0.30, 0.25]
    export_schedule = [0.0, 0.0, 1.617, 0.0, 0.23, 0.0]
    # Grid charge at idx 3 (20:00) — separate from export hours (no overlap)
    charge_schedule = [0.0, 0.0, 0.0, 0.5, 0.0, 0.0]
    cfg = _cfg(export_min_block_kwh=0.5)

    _, grid_request, _, export_request, _, _ = _call_dp(
        cfg, NOW, prices, export_schedule, schedule=charge_schedule
    )

    hour_19 = NOW + timedelta(hours=2)  # idx 2 — peak export
    hour_20 = NOW + timedelta(hours=3)  # idx 3 — grid charge (non-overlapping)
    hour_21 = NOW + timedelta(hours=4)  # idx 4 — sub-min export (dropped)

    # Grid charge at 20:00 must survive (filter does NOT touch grid_request; no overlap)
    assert hour_20 in grid_request, (
        "Grid charge at idx 3 must remain — filter touches export only"
    )
    assert grid_request[hour_20] == pytest.approx(500.0, abs=1.0), (
        f"grid_request[20:00] must be 500W, got {grid_request.get(hour_20)}"
    )

    # Peak export at 19:00 kept
    assert hour_19 in export_request, "Peak export at idx 2 must be kept"
    assert export_request[hour_19] == pytest.approx(1617.0, abs=1.0)

    # Sub-min export dribble at 21:00 dropped
    assert hour_21 not in export_request, "Sub-min dribble at idx 4 must be dropped"

    # Grid has no entry at 21:00 either (DP never charged then — untouched by filter)
    assert hour_21 not in grid_request, "grid_request at idx 4 must be absent (DP chose 0)"


# ===========================================================================
# 5. No-op — export_min_block_kwh=0.0 keeps all DP-scheduled hours intact
# ===========================================================================

def test_noop_when_min_block_zero():
    """Escape hatch: export_min_block_kwh=0.0 → filter is a no-op.

    Both the peak run AND the sub-min dribble must appear in export_request,
    matching the raw DP schedule exactly.  The revenue must be recomputed
    (not 999.0 sentinel) but must equal the full-schedule net revenue.

    Note: with min=0.0 the filter recomputes revenue identically to the DP's
    stored value (within ~1e-15 float precision); export_request is byte-identical
    to what it would be without the filter.
    """
    prices = [0.25, 0.28, 0.32, 0.31, 0.30, 0.25]
    export_schedule = [0.0, 0.0, 1.617, 0.0, 0.23, 0.0]
    cfg = _cfg(export_min_block_kwh=0.0)   # filter disabled

    _, _, _, export_request, export_revenue_eur, _ = _call_dp(
        cfg, NOW, prices, export_schedule
    )

    hour_19 = NOW + timedelta(hours=2)  # idx 2
    hour_21 = NOW + timedelta(hours=4)  # idx 4

    # Both runs present — no filtering
    assert hour_19 in export_request, "Peak run must be kept when min=0"
    assert hour_21 in export_request, "Sub-min run must also be kept when min=0"
    assert export_request[hour_19] == pytest.approx(1617.0, abs=1.0)
    assert export_request[hour_21] == pytest.approx(230.0, abs=1.0)

    # Revenue recomputed from FULL schedule (not 999 sentinel)
    assert export_revenue_eur != pytest.approx(999.0, abs=1e-3), (
        "export_revenue_eur must be recomputed from the full schedule"
    )
    ep = [effective_export_price(p, cfg) for p in prices]
    expected_rev = _net_rev(export_schedule, ep, cfg)
    assert export_revenue_eur == pytest.approx(expected_rev, abs=1e-6), (
        f"No-op revenue {export_revenue_eur:.6f} != full-schedule revenue {expected_rev:.6f}"
    )


# ===========================================================================
# 6. Real DP (no mock) — optimize_grid naturally emits peak + sub-min dribble;
#    filter drops the dribble
# ===========================================================================

def test_real_dp_submin_dribble_dropped():
    """End-to-end with the REAL optimize_grid: DP emits peak + dribble; filter drops dribble.

    Scenario (6-hour window starting at NOW=17:00 UTC):
      Prices:     [0.25, 0.35, 0.15, 0.10, 0.31, 0.20]
      Band gate:  export_peak_band_frac=0.12, lookback=4 → peak ref=0.35,
                  floor=0.35×0.88=0.308.
      Export OK:  h=1 (0.35 ≥ 0.308), h=4 (0.31 ≥ 0.308) — split by gap at h=2-3.

    Battery: 20% SoC (2.0 kWh DC), soc_floor=5% (0.5 kWh DC), max_export=1 kWh/h.
    DP allocates ~0.97 kWh AC at h=1 (peak, hits max_export_w), leaving ~0.42 kWh AC
    for h=4 (dribble < 0.5 kWh min → dropped by filter).

    Precondition: with filter DISABLED (min=0), the DP DOES produce a sub-min run at h=4.
    Assertion:    with filter ENABLED (min=0.5), h=4 is absent from export_request.
    """
    prices = [0.25, 0.35, 0.15, 0.10, 0.31, 0.20]
    n = len(prices)
    now = NOW
    deadline = now + timedelta(hours=n)
    # Pure export scenario: no load, no PV — battery exports only.
    ivs = [
        ForecastInterval(start=now + timedelta(hours=h), pv_w=0.0, load_w=0.0, dt_h=1.0)
        for h in range(n)
    ]
    slots = _slots(now, prices)

    def _run(export_min_block_kwh: float):
        cfg = _cfg(
            soc_floor=5.0,
            max_export_w=1000.0,
            export_peak_band_frac=0.12,
            export_peak_lookback_h=4,
            export_min_block_kwh=export_min_block_kwh,
        )
        inputs = PlantInputs(soc=20.0, phase_import_w=(0.0, 0.0, 0.0), now=now)
        return _dp_select_slots(
            inputs=inputs,
            slots=slots,
            deadline=deadline,
            ceiling=None,       # no grid charging — pure export test
            cfg=cfg,
            export_price=0.31,  # current-hour export price (scaled by match ratio)
            export_price_matches_import=True,
            terminal_mode="water_value",
            water_value=0.0,
            reserve_by_hour=[0.5] * n,
            intervals=ivs,
        )

    hour_1 = now + timedelta(hours=1)   # 18:00 UTC — peak run
    hour_4 = now + timedelta(hours=4)   # 21:00 UTC — potential dribble

    # Precondition: with filter disabled, DP produces a sub-min dribble at h=4.
    _, _, _, unfiltered_req, _, _ = _run(export_min_block_kwh=0.0)
    assert hour_4 in unfiltered_req, (
        "Precondition: DP must schedule export at h=4 when filter is disabled. "
        "If the DP doesn't emit a dribble here the test scenario is misconfigured."
    )
    dribble_kwh = unfiltered_req[hour_4] / 1000.0
    assert dribble_kwh < 0.5, (
        f"Precondition: h=4 export {dribble_kwh:.3f} kWh must be < 0.5 kWh min "
        f"(otherwise it's not a dribble scenario)."
    )

    # Peak at h=1 exists even without filter
    assert hour_1 in unfiltered_req, "DP must export at peak hour h=1"
    assert unfiltered_req[hour_1] / 1000.0 >= 0.5, "Peak run at h=1 must be ≥ 0.5 kWh"

    # With filter enabled: h=4 dribble must be dropped; h=1 peak must survive.
    _, _, _, filtered_req, export_rev, _ = _run(export_min_block_kwh=0.5)
    assert hour_4 not in filtered_req, (
        f"Filter must drop the sub-min dribble ({dribble_kwh:.3f} kWh < 0.5) at h=4"
    )
    assert hour_1 in filtered_req, "Peak run at h=1 must be kept by filter"
    assert filtered_req[hour_1] / 1000.0 >= 0.5, "Kept peak must be ≥ min_block_kwh"
    assert export_rev > 0.0, "Filtered revenue must be positive (peak run generates revenue)"
    # Revenue from peak only — must be less than full-schedule (peak+dribble) revenue
    assert export_rev < (unfiltered_req[hour_1] / 1000.0) * prices[1], (
        "Revenue must be bounded by gross export * price (sanity check)"
    )


# ===========================================================================
# 7. Charge/export overlap net-out — dominant action preserved, no dual-action
# ===========================================================================

def test_export_dominant_hour_netted_to_export_only():
    """When export > charge for the same hour, export_request keeps the net; charge removed.

    charge idx 2: 108.7 W (0.1087 kWh) — within epsilon of a live observation
    export idx 2: 3279.9 W (3.2799 kWh)
    net = 3279.9 − 108.7 = 3171.2 W  (export dominates)

    export_min_block_kwh=0.0 so the block filter is a no-op and the export survives
    to be processed by the net-out pass.
    """
    prices = [0.25, 0.28, 0.55, 0.31, 0.30, 0.25]
    charge_schedule = [0.0, 0.0, 0.1087, 0.0, 0.0, 0.0]
    export_schedule = [0.0, 0.0, 3.2799, 0.0, 0.0, 0.0]
    cfg = _cfg(export_min_block_kwh=0.0)

    selected, grid_request, _, export_request, _, _ = _call_dp(
        cfg, NOW, prices, export_schedule, schedule=charge_schedule
    )

    hour_2 = NOW + timedelta(hours=2)  # idx 2 — the overlapping hour

    # Mutual exclusion: no hour may appear in both dicts
    assert set(grid_request) & set(export_request) == set(), (
        "No hour may appear in both grid_request and export_request"
    )
    # Export dominates: hour_2 only in export_request with net value
    assert hour_2 not in grid_request, "Charge must be removed when export dominates"
    assert hour_2 not in selected, "hour_2 must not be in selected (not a charging slot)"
    assert hour_2 in export_request, "hour_2 must be in export_request (export dominant)"
    assert export_request[hour_2] == pytest.approx(3171.2, abs=1.0), (
        f"Netted export must be 3279.9 − 108.7 = 3171.2 W, got {export_request[hour_2]:.1f}"
    )


def test_charge_dominant_hour_netted_to_charge_only():
    """When charge > export for the same hour, grid_request keeps the net; export removed.

    charge idx 2: 2000 W (2.0 kWh)
    export idx 2:  500 W (0.5 kWh)
    net = 500 − 2000 = −1500 W  (charge dominates → −net = 1500 W kept)

    export_min_block_kwh=0.0 so the block filter is a no-op.
    """
    prices = [0.25, 0.28, 0.55, 0.31, 0.30, 0.25]
    charge_schedule = [0.0, 0.0, 2.0, 0.0, 0.0, 0.0]
    export_schedule = [0.0, 0.0, 0.5, 0.0, 0.0, 0.0]
    cfg = _cfg(export_min_block_kwh=0.0)

    selected, grid_request, _, export_request, _, _ = _call_dp(
        cfg, NOW, prices, export_schedule, schedule=charge_schedule
    )

    hour_2 = NOW + timedelta(hours=2)  # idx 2 — the overlapping hour

    assert set(grid_request) & set(export_request) == set(), (
        "No hour may appear in both grid_request and export_request"
    )
    # Charge dominates: hour_2 only in grid_request with net value
    assert hour_2 not in export_request, "Export must be removed when charge dominates"
    assert hour_2 in selected, "hour_2 must be in selected (it is a charging slot)"
    assert hour_2 in grid_request, "hour_2 must be in grid_request (charge dominant)"
    assert grid_request[hour_2] == pytest.approx(1500.0, abs=1.0), (
        f"Netted charge must be 2000 − 500 = 1500 W, got {grid_request[hour_2]:.1f}"
    )


def test_near_equal_charge_export_cancels_to_idle():
    """When charge ≈ export (within epsilon), both cancel and the hour becomes idle.

    charge idx 2: 1000 W (1.0 kWh)
    export idx 2: 1000 W (1.0 kWh)
    net = 0 W — within _DP_EPSILON_SCHEDULE_KWH * 1000 = 10 W → idle

    export_min_block_kwh=0.0 so the block filter is a no-op.
    """
    prices = [0.25, 0.28, 0.55, 0.31, 0.30, 0.25]
    charge_schedule = [0.0, 0.0, 1.0, 0.0, 0.0, 0.0]
    export_schedule = [0.0, 0.0, 1.0, 0.0, 0.0, 0.0]
    cfg = _cfg(export_min_block_kwh=0.0)

    selected, grid_request, _, export_request, _, _ = _call_dp(
        cfg, NOW, prices, export_schedule, schedule=charge_schedule
    )

    hour_2 = NOW + timedelta(hours=2)  # idx 2 — the overlapping hour

    assert hour_2 not in selected, (
        "Cancelled hour must be absent from selected (idle, not charging)"
    )
    assert hour_2 not in grid_request, (
        "Cancelled hour must be absent from grid_request"
    )
    assert hour_2 not in export_request, (
        "Cancelled hour must be absent from export_request"
    )


def test_revenue_reduced_when_export_netted_down():
    """Revenue telemetry is adjusted when the net-out removes part of the export AC energy.

    Same geometry as test_export_dominant_hour_netted_to_export_only:
      charge 108.7 W, export 3279.9 W → net export 3171.2 W.
    The removed export AC = 108.7 W = 0.1087 kWh must be subtracted from revenue
    so that export_revenue_eur reflects only the energy actually exported, not the
    full pre-net 3.2799 kWh.
    """
    prices = [0.25, 0.28, 0.55, 0.31, 0.30, 0.25]
    charge_schedule = [0.0, 0.0, 0.1087, 0.0, 0.0, 0.0]
    export_schedule = [0.0, 0.0, 3.2799, 0.0, 0.0, 0.0]
    cfg = _cfg(export_min_block_kwh=0.0)

    _, _, _, export_request, export_revenue_eur, _ = _call_dp(
        cfg, NOW, prices, export_schedule, schedule=charge_schedule
    )

    hour_2 = NOW + timedelta(hours=2)

    # Compute the un-netted reference revenue for the full 3.2799 kWh at hour 2
    ep = [effective_export_price(p, cfg) for p in prices]
    full_ac = [0.0, 0.0, 3.2799, 0.0, 0.0, 0.0]
    full_revenue = _net_rev(full_ac, ep, cfg)

    # Netted revenue must be strictly less (0.1087 kWh of export was removed)
    assert export_revenue_eur < full_revenue, (
        f"Netted revenue {export_revenue_eur:.6f} must be < full revenue "
        f"{full_revenue:.6f} (0.1087 kWh export was netted out)"
    )
    # Must still be positive — profitable export at 0.55 EUR/kWh
    assert export_revenue_eur > 0.0, "Netted export revenue must be positive"
    # Sanity: export_request carries the netted value
    assert hour_2 in export_request
    assert export_request[hour_2] == pytest.approx(3171.2, abs=1.0)
