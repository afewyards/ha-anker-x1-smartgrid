"""T13 — DP wall-time benchmark at 48h / 192 slots (compute source of truth).

Records ``optimize_grid`` wall-time for a 2-day 15-min window (192 slots)
and asserts it fits well within the 60-s TICK_SECONDS budget, leaving
margin for the shadow + live + recompute DP passes per tick.

``n_states = round(cap_kwh / bin_kwh) + 1`` does NOT grow with resolution
-- only ``n_steps`` (the window length) does. This benchmark is the
compute source-of-truth for that scaling claim.  A failure here is the
ONLY intended trigger to pull in the deferred far-horizon-coarsening
spec -- do not weaken the budget or coarsen the DP to force a pass.
"""

import time

import pytest

from custom_components.anker_x1_smartgrid.models import Config
from custom_components.anker_x1_smartgrid.optimize import optimize_grid


def _cfg():
    return Config(
        capacity_kwh=10.0,
        soc_floor=20.0,
        soc_target=80.0,
        max_charge_w=4000.0,
        eta_charge=0.92,
        round_trip_eff=0.85,
        max_export_w=4000.0,
        grid_export_limit_w=6000.0,
        enable_export=True,
        export_peak_band_frac=0.10,
        export_peak_lookback_h=4,
    )


@pytest.mark.benchmark
def test_dp_walltime_192_slots_under_budget(capsys):
    n = 192  # 48h at 15-min resolution
    pv = [0.0 if (i % 96) < 28 or (i % 96) > 76 else 2.5 for i in range(n)]
    load = [0.2] * n
    price = [0.15 + 0.15 * ((i // 4) % 6) / 5.0 for i in range(n)]
    day_index = [i // 96 for i in range(n)]

    t0 = time.perf_counter()
    out = optimize_grid(
        pv,
        load,
        price,
        soc_start=50.0,
        cfg=_cfg(),
        window_start_h=0,
        window_len=n,
        dt_h=0.25,
        slots_per_day=96,
        export_price=price,
        feed_in=price,
        day_index=day_index,
    )
    elapsed = time.perf_counter() - t0

    with capsys.disabled():
        print(f"\n[15min-benchmark] optimize_grid({n} slots) = {elapsed * 1000:.1f} ms")

    assert len(out["schedule"]) == n
    # Budget: well under the 60-s TICK_SECONDS; 5 s leaves margin for the
    # shadow + live + recompute passes per tick on HAOS-class hardware.
    assert elapsed < 5.0, f"DP over budget: {elapsed:.2f}s at 192 slots"
