"""BC3: nightly regret scorer (``_run_daily_regret_sync``) must dt_h-scale
average-W -> kWh bucket conversions and thread dt_h into the DP-optimal /
realized-cost calls at 15-min slot resolution.

Regression for: at ``_detected_slot_minutes=15`` each recorder bucket spans
0.25h, but the scorer's ``_mean(<watts>) / 1000.0`` conversions and its
``realized_grid_cost`` / DP-optimal calls did not multiply by ``dt_h``,
4x-overcounting every energy figure (pv_kwh, load_kwh, realized_charge,
realized export) and running the optimal side at the wrong per-slot rate
cap (dt_h defaulted to 1.0 downstream). This corrupts ``last_dp_regret_7d``
telemetry and the HGBR promotion-gate metric at 15-min.

At 60-min, dt_h = 1.0 so every scaled value and every ``dt_h=`` kwarg is
identical to the pre-fix behaviour (see test_shadow_logging.py's existing
60-min regret-sync coverage, unmodified by this file).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone, UTC
from unittest.mock import patch

import pytest

from custom_components.anker_x1_smartgrid import optimize as optimize_mod
from custom_components.anker_x1_smartgrid import regret as regret_mod

from .test_shadow_logging import _make_controller, _StubHass

# Capture the real implementations BEFORE any test patches them, so the spies
# below can delegate to real physics (not just record call args).
_ORIG_REALIZED_GRID_COST = regret_mod.realized_grid_cost
_ORIG_HINDSIGHT_OPTIMAL_GRID = regret_mod.hindsight_optimal_grid
_ORIG_OPTIMIZE_GRID = optimize_mod.optimize_grid


def _seed_sample_rows_15min(
    rec,
    day_str: str,
    n_slots: int = 96,
    *,
    pv_w: float = 800.0,
    load_w: float = 500.0,
    batt_w: float = -200.0,
    p1_w: float = 500.0,
    price: float = 0.20,
) -> None:
    """Seed n_slots (default 96 = a full 15-min day) of CONSTANT-watt rows.

    Anchored at UTC noon of day_str (safe for any real-world UTC offset,
    mirroring test_shadow_logging._seed_sample_rows_for_day's convention),
    stepping by 15 minutes so every slot bucket gets exactly one sample.
    """
    day_date = date.fromisoformat(day_str)
    base_ts = datetime(day_date.year, day_date.month, day_date.day, 12, 0, tzinfo=UTC)
    for i in range(n_slots):
        ts = base_ts + timedelta(minutes=15 * i - 12 * 60)
        rec.rows.append(
            {
                "ts": ts.isoformat(),
                "soc": 50.0,
                "pv_w": pv_w,
                "batt_w": batt_w,
                "p1_w": p1_w,
                "load_w": load_w,
                "import_price": price,
            }
        )


def _run_with_spies(ctrl, day, ts_now):
    """Run _run_daily_regret_sync with spies on the dt_h-sensitive call sites.

    Returns the captured-call dict. Spies delegate to the REAL functions
    (captured before patching) so the actual regret math still executes;
    only the call arguments are additionally recorded.
    """
    captured = {"realized_grid_cost": [], "hindsight_optimal_grid": [], "optimize_grid": []}

    def _spy_realized(day_data, realized_charge_by_hour, cfg, **kwargs):
        captured["realized_grid_cost"].append(
            {
                "realized_charge_by_hour": list(realized_charge_by_hour),
                "dt_h": kwargs.get("dt_h", 1.0),
                "pv_kwh": tuple(day_data.pv_kwh),
                "load_kwh": tuple(day_data.load_kwh),
                "price": tuple(day_data.price),
            }
        )
        return _ORIG_REALIZED_GRID_COST(day_data, realized_charge_by_hour, cfg, **kwargs)

    def _spy_hindsight(day_data, cfg, **kwargs):
        captured["hindsight_optimal_grid"].append({"dt_h": kwargs.get("dt_h", 1.0)})
        return _ORIG_HINDSIGHT_OPTIMAL_GRID(day_data, cfg, **kwargs)

    def _spy_optimize_grid(*args, **kwargs):
        captured["optimize_grid"].append({"dt_h": kwargs.get("dt_h", 1.0)})
        return _ORIG_OPTIMIZE_GRID(*args, **kwargs)

    with (
        patch("custom_components.anker_x1_smartgrid.regret.realized_grid_cost", side_effect=_spy_realized),
        patch("custom_components.anker_x1_smartgrid.regret.hindsight_optimal_grid", side_effect=_spy_hindsight),
        patch("custom_components.anker_x1_smartgrid.optimize.optimize_grid", side_effect=_spy_optimize_grid),
    ):
        ctrl._run_daily_regret_sync(day, ts_now)

    return captured


def test_regret_scorer_scales_energy_and_threads_dt_h_at_15min():
    """At 15-min, pv/load/charge W->kWh conversions must be dt_h-scaled (0.25),
    and dt_h=0.25 must reach hindsight_optimal_grid + realized_grid_cost so the
    OPTIMAL and REALIZED sides share the same slot width. Price stays unscaled.
    """
    hass = _StubHass()
    ctrl, _, rec = _make_controller(hass)
    ctrl._detected_slot_minutes = 15

    day = "2026-06-21"
    _seed_sample_rows_15min(rec, day)

    ts_now = datetime(2026, 6, 22, 0, 5, tzinfo=UTC).isoformat()
    captured = _run_with_spies(ctrl, day, ts_now)

    stored = rec.daily_regret_rows.get(day)
    assert stored is not None, "daily_regret row must be stored (day must not be infeasible)"
    assert stored.get("infeasible", 0) == 0, (
        "test day must be feasible for the assertions below to exercise realized_grid_cost"
    )

    assert captured["hindsight_optimal_grid"], "hindsight_optimal_grid must have been called"
    assert captured["realized_grid_cost"], "realized_grid_cost (main leg) must have been called"

    _dt_h = 15.0 / 60.0  # 0.25

    # --- dt_h must reach every downstream physics call (apples-to-apples). ---
    for call in captured["hindsight_optimal_grid"]:
        assert call["dt_h"] == pytest.approx(_dt_h), f"hindsight_optimal_grid got dt_h={call['dt_h']}, want {_dt_h}"
    for call in captured["realized_grid_cost"]:
        assert call["dt_h"] == pytest.approx(_dt_h), f"realized_grid_cost got dt_h={call['dt_h']}, want {_dt_h}"
    for call in captured["optimize_grid"]:
        assert call["dt_h"] == pytest.approx(_dt_h), f"optimize_grid (shadow DP) got dt_h={call['dt_h']}, want {_dt_h}"

    # --- Energy conversions must be dt_h-scaled: 0.25x of the naive /1000.0 value. ---
    # The LAST realized_grid_cost call is the main (non-DP-shadow) leg, using the
    # actual realized_charge array built from recorder samples.
    main_call = captured["realized_grid_cost"][-1]

    expected_pv_kwh = 800.0 / 1000.0 * _dt_h  # 0.2 (naive/unscaled would be 0.8 -> 4x)
    expected_load_kwh = 500.0 / 1000.0 * _dt_h  # 0.125 (naive 0.5 -> 4x)
    # grid_charge_w = max(0, battery_charge_w - solar_surplus_w)
    #   battery_charge_w = max(0, -batt_w) = 200
    #   solar_surplus_w  = max(0, -(p1_w+batt_w)) = max(0, -(500-200)) = 0
    #   grid_charge_w = 200
    expected_charge_kwh = 200.0 / 1000.0 * _dt_h  # 0.05 (naive 0.2 -> 4x)

    assert main_call["pv_kwh"][0] == pytest.approx(expected_pv_kwh, rel=1e-6), (
        f"pv_kwh not dt_h-scaled: got {main_call['pv_kwh'][0]}, want {expected_pv_kwh} "
        f"(un-scaled/4x-over-count would be {expected_pv_kwh * 4})"
    )
    assert main_call["load_kwh"][0] == pytest.approx(expected_load_kwh, rel=1e-6), (
        f"load_kwh not dt_h-scaled: got {main_call['load_kwh'][0]}, want {expected_load_kwh} "
        f"(un-scaled/4x-over-count would be {expected_load_kwh * 4})"
    )
    assert main_call["realized_charge_by_hour"][0] == pytest.approx(expected_charge_kwh, rel=1e-6), (
        f"realized_charge not dt_h-scaled: got {main_call['realized_charge_by_hour'][0]}, "
        f"want {expected_charge_kwh} (un-scaled/4x-over-count would be {expected_charge_kwh * 4})"
    )

    # --- Price (€/kWh) must NOT be scaled by dt_h. ---
    assert main_call["price"][0] == pytest.approx(0.20, rel=1e-6), (
        f"price must stay unscaled (€/kWh, not energy): got {main_call['price'][0]}"
    )


def test_regret_scorer_unchanged_at_60min():
    """Sanity: at 60-min (_detected_slot_minutes=60, the default), dt_h=1.0 reaches
    every call and energy conversions are the plain /1000.0 value (byte-identical
    to pre-BC3 behaviour).
    """
    hass = _StubHass()
    ctrl, _, rec = _make_controller(hass)
    assert ctrl._detected_slot_minutes == 60  # default; not overridden

    day = "2026-06-21"
    # Reuse the project's existing hourly seed helper for a realistic 60-min day.
    from .test_shadow_logging import _seed_sample_rows_for_day

    _seed_sample_rows_for_day(rec, day, n_hours=24)

    ts_now = datetime(2026, 6, 22, 0, 5, tzinfo=UTC).isoformat()
    captured = _run_with_spies(ctrl, day, ts_now)

    stored = rec.daily_regret_rows.get(day)
    assert stored is not None

    for call in captured["hindsight_optimal_grid"]:
        assert call["dt_h"] == pytest.approx(1.0)
    for call in captured["realized_grid_cost"]:
        assert call["dt_h"] == pytest.approx(1.0)
    for call in captured["optimize_grid"]:
        assert call["dt_h"] == pytest.approx(1.0)
