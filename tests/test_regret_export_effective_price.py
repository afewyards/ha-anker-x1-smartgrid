"""Regression for review finding 2.1: the nightly regret scorer's HEURISTIC
realized-cost leg must value actual export at the SAME effective (post-fee)
price the oracle already uses — not the raw dynamic tariff.

Crediting the heuristic's export revenue at the raw price while the oracle is
scored at ``raw - cfg.export_fee_eur_per_kwh`` biases regret in the
heuristic's favor on every exporting day, corrupting the HGBR promotion-gate
metric fed by ``_run_daily_regret_sync``.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from custom_components.anker_x1_smartgrid import regret as regret_mod

from .test_shadow_logging import _make_controller, _seed_export_rows_for_day, _StubHass

# Capture the real implementations BEFORE any test patches them, so the spies
# below can delegate to real physics (not just record call args) — mirrors
# test_15min_regret_scaling.py's established pattern for this scorer.
_ORIG_REALIZED_GRID_COST = regret_mod.realized_grid_cost
_ORIG_HINDSIGHT_OPTIMAL_GRID = regret_mod.hindsight_optimal_grid


def test_regret_scorer_heuristic_realized_export_scored_at_effective_price():
    """Heuristic realized_eur must reflect export credited at the EFFECTIVE
    (post-fee) price — the same price vector the oracle uses — not raw.
    """
    hass = _StubHass()
    ctrl, _, rec = _make_controller(hass)
    fee = ctrl.cfg.export_fee_eur_per_kwh
    assert ctrl.cfg.enable_export is True, "test assumes the default enable_export=True"
    assert fee > 0.0, "test requires a nonzero export fee to be meaningful"

    day = "2026-06-21"
    raw_price = 0.40
    _seed_export_rows_for_day(
        rec, day, export_hour=14, export_w=2000.0, export_price_eur=raw_price
    )

    captured_realized: list[dict] = []
    captured_oracle: list[dict] = []

    def _spy_realized(day_data, realized_charge_by_hour, cfg, **kwargs):
        captured_realized.append({
            "day_data": day_data,
            "realized_charge_by_hour": list(realized_charge_by_hour),
            "realized_export_by_hour": (
                list(kwargs["realized_export_by_hour"])
                if kwargs.get("realized_export_by_hour") is not None else None
            ),
            "export_price": (
                list(kwargs["export_price"]) if kwargs.get("export_price") is not None else None
            ),
            "dt_h": kwargs.get("dt_h", 1.0),
        })
        return _ORIG_REALIZED_GRID_COST(day_data, realized_charge_by_hour, cfg, **kwargs)

    def _spy_hindsight(day_data, cfg, **kwargs):
        captured_oracle.append({
            "export_price": (
                list(kwargs["export_price"]) if kwargs.get("export_price") is not None else None
            ),
        })
        return _ORIG_HINDSIGHT_OPTIMAL_GRID(day_data, cfg, **kwargs)

    # pytest_homeassistant_custom_component's hass fixture sets
    # DEFAULT_TIME_ZONE to US/Pacific as an autouse side effect; patch
    # as_local to identity (NOT DEFAULT_TIME_ZONE itself, which upsets
    # verify_cleanup teardown — see test_pricing_store.py's established
    # idiom) so every seeded UTC hour buckets 1:1 into the local day this
    # test reasons about.
    ts_now = datetime(2026, 6, 22, 0, 5, tzinfo=timezone.utc).isoformat()
    with patch(
        "custom_components.anker_x1_smartgrid.regret.realized_grid_cost", side_effect=_spy_realized
    ), patch(
        "custom_components.anker_x1_smartgrid.regret.hindsight_optimal_grid", side_effect=_spy_hindsight
    ), patch(
        "homeassistant.util.dt.as_local", side_effect=lambda d: d
    ):
        ctrl._run_daily_regret_sync(day, ts_now)

    stored = rec.daily_regret_rows.get(day)
    assert stored is not None, "daily_regret row must be stored"
    assert stored.get("infeasible", 0) == 0, "test day must be feasible"
    assert captured_realized, "realized_grid_cost must have been called"
    assert captured_oracle, "hindsight_optimal_grid must have been called"

    # The MAIN (non-DP-shadow) leg is always the LAST captured realized_grid_cost
    # call — the shadow-DP leg (if it ran) is scored first. Mirrors
    # test_15min_regret_scaling.py's convention for isolating the main leg.
    main_call = captured_realized[-1]
    oracle_call = captured_oracle[-1]

    # --- 1. Oracle and heuristic must be scored at the SAME export price vector. ---
    assert main_call["export_price"] == oracle_call["export_price"], (
        "heuristic realized leg and oracle must share the same export price vector "
        f"(realized={main_call['export_price']}, oracle={oracle_call['export_price']})"
    )

    # --- 2. That shared vector must be the EFFECTIVE (post-fee) price. ---
    expected_eff = pytest.approx(raw_price - fee)
    assert all(p == expected_eff for p in main_call["export_price"]), (
        f"export_price must be raw ({raw_price}) minus fee ({fee}); "
        f"got {main_call['export_price']}"
    )

    # --- 3. Hand-computed check: re-run the REAL physics with the RAW price
    #     tuple and confirm the delta from the stored (effective-priced) eur
    #     is EXACTLY export_kwh * fee. This is an exact algebraic identity —
    #     price enters realized_grid_cost's revenue term purely linearly
    #     (see regret.py Step 4: gross_rev = e_ac_h * export_price[h]), so the
    #     identity holds independent of the underlying simulation physics and
    #     is not merely restating the implementation. ---
    n = len(main_call["realized_charge_by_hour"])
    raw_tuple = [raw_price] * n
    eur_raw = _ORIG_REALIZED_GRID_COST(
        main_call["day_data"], main_call["realized_charge_by_hour"], ctrl.cfg,
        realized_export_by_hour=main_call["realized_export_by_hour"],
        export_price=raw_tuple,
        dt_h=main_call["dt_h"],
    )["eur"]
    total_export_kwh = sum(main_call["realized_export_by_hour"])
    assert total_export_kwh > 0.0, "fixture must produce nonzero realized export"

    stored_realized_eur = stored["realized_eur"]
    assert stored_realized_eur == pytest.approx(eur_raw + fee * total_export_kwh, rel=1e-9), (
        f"realized_eur must equal the raw-priced eur ({eur_raw}) plus "
        f"fee*export_kwh ({fee * total_export_kwh}); got {stored_realized_eur}"
    )

    # --- 4. Belt-and-suspenders re-derivation directly against a hand-built
    #     EFFECTIVE price tuple (raw - fee), matching the plan's exact ask. ---
    eff_tuple = [raw_price - fee] * n
    eur_eff_direct = _ORIG_REALIZED_GRID_COST(
        main_call["day_data"], main_call["realized_charge_by_hour"], ctrl.cfg,
        realized_export_by_hour=main_call["realized_export_by_hour"],
        export_price=eff_tuple,
        dt_h=main_call["dt_h"],
    )["eur"]
    assert stored_realized_eur == pytest.approx(eur_eff_direct, rel=1e-9)


def test_regret_scorer_eff_export_none_when_export_disabled():
    """When enable_export is False, eff_export stays None on BOTH the oracle
    and heuristic sides (consistent — no export leg is scored either side).
    """
    hass = _StubHass()
    ctrl, _, rec = _make_controller(hass)
    import dataclasses
    ctrl.cfg = dataclasses.replace(ctrl.cfg, enable_export=False)

    day = "2026-06-21"
    _seed_export_rows_for_day(rec, day, export_hour=14, export_w=2000.0, export_price_eur=0.40)

    captured_realized: list[dict] = []
    captured_oracle: list[dict] = []

    def _spy_realized(day_data, realized_charge_by_hour, cfg, **kwargs):
        captured_realized.append({
            "export_price": kwargs.get("export_price"),
        })
        return _ORIG_REALIZED_GRID_COST(day_data, realized_charge_by_hour, cfg, **kwargs)

    def _spy_hindsight(day_data, cfg, **kwargs):
        captured_oracle.append({"export_price": kwargs.get("export_price")})
        return _ORIG_HINDSIGHT_OPTIMAL_GRID(day_data, cfg, **kwargs)

    ts_now = datetime(2026, 6, 22, 0, 5, tzinfo=timezone.utc).isoformat()
    with patch(
        "custom_components.anker_x1_smartgrid.regret.realized_grid_cost", side_effect=_spy_realized
    ), patch(
        "custom_components.anker_x1_smartgrid.regret.hindsight_optimal_grid", side_effect=_spy_hindsight
    ), patch(
        "homeassistant.util.dt.as_local", side_effect=lambda d: d
    ):
        ctrl._run_daily_regret_sync(day, ts_now)

    assert captured_realized, "realized_grid_cost must have been called"
    assert captured_oracle, "hindsight_optimal_grid must have been called"
    assert captured_realized[-1]["export_price"] is None
    assert captured_oracle[-1]["export_price"] is None


# ===========================================================================
# Review finding 2.2: realized export must be BATTERY-ONLY (exclude PV spill)
# ===========================================================================
#
# The oracle/shadow-DP can only credit export that comes from a battery-
# discharge ACTION — never free PV spill (PV > load with the battery idle).
# Before this fix, realized export was taken straight from metered −p1_w,
# which also rises during spill, crediting the heuristic for revenue the
# oracle structurally cannot earn and driving sunny-day regret artificially
# negative. Unlike ``_seed_export_rows_for_day`` (which always pins
# ``batt_w == export_w``), this helper controls battery power independently
# of the metered export so PV-spill-only and battery-capped scenarios can be
# isolated.


def _seed_spill_day_rows(
    rec,
    day_str: str,
    *,
    export_hour: int = 14,
    export_w: float = 1500.0,
    batt_discharge_w: float = 0.0,
    export_price_eur: float = 0.40,
) -> None:
    """Seed a day whose export_hour has metered net-export (p1_w < 0) with an
    independently-controlled battery power, so realized export crediting can
    be tested against battery discharge alone rather than total metered export.
    """
    day_date = date.fromisoformat(day_str)
    base_ts = datetime(day_date.year, day_date.month, day_date.day, 12, 0, tzinfo=timezone.utc)
    for h in range(24):
        ts = base_ts + timedelta(hours=h - 12)
        if h == export_hour:
            p1_w = -export_w              # negative = net-export at the meter
            batt_w = batt_discharge_w      # >0 = discharging; independent of export_w
            pv_w = 3000.0
        else:
            p1_w = 500.0                   # normal import
            batt_w = -200.0                # charging
            pv_w = 1000.0 if 8 <= h <= 18 else 0.0
        rec.rows.append({
            "ts": ts.isoformat(),
            "soc": 50.0 + h * 0.3,
            "pv_w": pv_w,
            "batt_w": batt_w,
            "p1_w": p1_w,
            "import_price": 0.20 if h < 8 else 0.35,
            "export_price": export_price_eur,
        })


def _spy_capture_realized_export():
    """Build a (spy_fn, captures) pair patched onto regret.realized_grid_cost
    to record the realized_export_by_hour array fed into it, while delegating
    to the real implementation so the stored daily_regret row is unaffected.
    """
    captured: list[dict] = []

    def _spy(day_data, realized_charge_by_hour, cfg, **kwargs):
        captured.append({
            "realized_export_by_hour": (
                list(kwargs["realized_export_by_hour"])
                if kwargs.get("realized_export_by_hour") is not None else None
            ),
        })
        return _ORIG_REALIZED_GRID_COST(day_data, realized_charge_by_hour, cfg, **kwargs)

    return _spy, captured


def test_regret_scorer_excludes_pv_spill_when_battery_idle():
    """RED case (finding 2.2): PV spill with the battery idle (batt_w=0,
    p1_w<0) must book NO realized export revenue — the oracle can never
    credit spill, so scoring it on the heuristic side is not like-for-like.
    """
    hass = _StubHass()
    ctrl, _, rec = _make_controller(hass)
    assert ctrl.cfg.enable_export is True, "test assumes the default enable_export=True"

    day = "2026-06-21"
    export_hour = 14
    _seed_spill_day_rows(
        rec, day, export_hour=export_hour, export_w=1500.0, batt_discharge_w=0.0,
    )

    spy, captured_realized = _spy_capture_realized_export()
    ts_now = datetime(2026, 6, 22, 0, 5, tzinfo=timezone.utc).isoformat()
    with patch(
        "custom_components.anker_x1_smartgrid.regret.realized_grid_cost", side_effect=spy
    ), patch(
        "homeassistant.util.dt.as_local", side_effect=lambda d: d
    ):
        ctrl._run_daily_regret_sync(day, ts_now)

    stored = rec.daily_regret_rows.get(day)
    assert stored is not None, "daily_regret row must be stored"
    assert stored.get("infeasible", 0) == 0, "test day must be feasible"
    assert captured_realized, "realized_grid_cost must have been called"

    main_call = captured_realized[-1]
    export_row = main_call["realized_export_by_hour"]
    assert export_row is not None, "export price was seeded, so export array must be built"
    assert export_row[export_hour] == pytest.approx(0.0), (
        "PV spill with the battery idle must NOT be credited as realized "
        f"export; got {export_row[export_hour]} kWh at hour {export_hour}"
    )


def test_regret_scorer_battery_discharge_export_capped_at_battery_power():
    """GREEN case (finding 2.2): battery discharging to grid (batt_w>0,
    p1_w<0) IS credited, but capped at the battery's discharge power — not
    the full metered −p1_w, which may also include simultaneous PV spill.
    """
    hass = _StubHass()
    ctrl, _, rec = _make_controller(hass)
    assert ctrl.cfg.enable_export is True

    day = "2026-06-21"
    export_hour = 14
    export_w = 1500.0
    batt_discharge_w = 1000.0  # < metered export -> credited export must be capped
    _seed_spill_day_rows(
        rec, day, export_hour=export_hour, export_w=export_w,
        batt_discharge_w=batt_discharge_w,
    )

    spy, captured_realized = _spy_capture_realized_export()
    ts_now = datetime(2026, 6, 22, 0, 5, tzinfo=timezone.utc).isoformat()
    with patch(
        "custom_components.anker_x1_smartgrid.regret.realized_grid_cost", side_effect=spy
    ), patch(
        "homeassistant.util.dt.as_local", side_effect=lambda d: d
    ):
        ctrl._run_daily_regret_sync(day, ts_now)

    stored = rec.daily_regret_rows.get(day)
    assert stored is not None
    assert stored.get("infeasible", 0) == 0
    assert captured_realized, "realized_grid_cost must have been called"

    main_call = captured_realized[-1]
    export_row = main_call["realized_export_by_hour"]
    assert export_row is not None

    # Single hourly sample at export_hour -> dt_h=1.0, so W -> kWh is a plain /1000.
    expected_kwh = batt_discharge_w / 1000.0
    metered_kwh = export_w / 1000.0
    assert export_row[export_hour] == pytest.approx(expected_kwh), (
        f"credited export must be capped at battery discharge power "
        f"({expected_kwh} kWh), not the full metered export ({metered_kwh} kWh); "
        f"got {export_row[export_hour]}"
    )
    assert export_row[export_hour] < metered_kwh, (
        "sanity: capped export must be strictly less than the full metered export "
        "(otherwise the fixture doesn't actually exercise the cap)"
    )
    # Revenue IS booked (unlike the pure-spill case above).
    assert stored["realized_eur"] is not None
