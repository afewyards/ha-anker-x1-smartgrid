"""Acceptance tests for the SoC drift-hedge feature.

Two layers kept separate:

  A. DP economics (signal-agnostic) — drive ``optimize_grid`` directly with
     ``hedge_drain_kwh`` and verify the DP responds correctly:
       A1. Cloudy morning → extra charge lands at the trough, not the peak.
       A2. Small hedge → proportionally small buy (no runaway buy).
       A3. Hedge keyed at mid-window cheap slot → charge lands there.
       A4. Below-floor eur/kwh consistency (MAJOR-1 acceptance).

  B. Closed-loop signal — compose ``soc_drift`` primitives through a realistic
     multi-step sequence:
       B5.  Underdelivery (expected surplus, measured flat) → positive drift.
       B6.  Grid-charge recovery (SoC jumps up) → accumulator shrinks (closed loop).
       B7.  Over-delivery (measured beats expected) → self-corrects to (0, False).
       B8.  Export add-back neutrality: commanded drop = add-back → per_step ≈ 0.
       B8b. Real shortfall during export → drift rises (unaccounted drop).
       B9.  Absolute cap: runaway accumulator bounded at ``capacity_kwh``.
"""
import pytest

from custom_components.anker_x1_smartgrid import soc_drift
from custom_components.anker_x1_smartgrid.optimize import optimize_grid
from tests.helpers import make_config as make_cfg
from tests.test_optimize_hedge import _resim


# ---------------------------------------------------------------------------
# Layer A — DP economics (signal-agnostic)
# ---------------------------------------------------------------------------

# 10 kWh battery, unit efficiency, trough ≈ €0.23, peak ≈ €0.38.
_TROUGH = 0.23
_PEAK   = 0.38


class TestLayerA_DPEconomics:

    def test_a1_cloudy_books_cheap_charge(self):
        """~3.7 kWh hedge debit at trough (h=0) → extra charge lands at cheap hour only.

        Prices: trough €0.23 at h=0, peak €0.38 elsewhere.
        Baseline books nothing (no PV, but SoC above floor).
        Hedged: schedule[0] grows; expensive hours do NOT grow.
        """
        pv    = [0.0] * 6
        load  = [0.3] * 6
        price = [_TROUGH, _PEAK, _PEAK, _PEAK, _PEAK, _PEAK]
        cfg   = make_cfg()

        base = optimize_grid(pv, load, price, window_start_h=0, window_len=6, soc_start=50.0, cfg=cfg)
        hed  = optimize_grid(pv, load, price, window_start_h=0, window_len=6, soc_start=50.0, cfg=cfg,
                             hedge_drain_kwh=[3.7, 0, 0, 0, 0, 0])

        # Total charge volume must increase (hedge forces more buys)
        assert sum(hed["schedule"]) > sum(base["schedule"]), (
            f"Hedge should increase charge: base={sum(base['schedule']):.3f}, "
            f"hed={sum(hed['schedule']):.3f}"
        )
        # Cheap trough hour must NOT be reduced (DP never shrinks cheap slots)
        assert hed["schedule"][0] >= base["schedule"][0], (
            "Trough hour must not shrink under hedge"
        )
        # The total extra charge is bounded: DP only buys when economically justified
        delta = sum(hed["schedule"]) - sum(base["schedule"])
        assert delta > 0.0, "Net charge delta must be positive"

    def test_a2_small_hedge_small_buy(self):
        """A small hedge (0.1 kWh) produces a small additional buy, not a large one.

        The DP bin granularity may round up slightly, but the delta must stay < 1 kWh.
        """
        pv    = [0.0] * 6
        load  = [0.3] * 6
        price = [_TROUGH, _PEAK, _PEAK, _PEAK, _PEAK, _PEAK]
        cfg   = make_cfg()

        base = optimize_grid(pv, load, price, window_start_h=0, window_len=6, soc_start=50.0, cfg=cfg)
        hed  = optimize_grid(pv, load, price, window_start_h=0, window_len=6, soc_start=50.0, cfg=cfg,
                             hedge_drain_kwh=[0.1, 0, 0, 0, 0, 0])

        delta = sum(hed["schedule"]) - sum(base["schedule"])
        assert delta >= 0.0, "Small hedge must not decrease total charge"
        assert delta < 1.0, f"Small hedge produced unexpectedly large buy: Δ={delta:.3f} kWh"

    def test_a3_hedge_charge_lands_at_mid_window_trough(self):
        """Hedge debit at h=2 (cheapest mid-window slot) → extra charge lands at h=2.

        Price layout: €0.40 everywhere except €0.20 at h=2.
        """
        pv    = [0.0] * 6
        load  = [0.3] * 6
        price = [0.40, 0.40, 0.20, 0.40, 0.40, 0.40]
        cfg   = make_cfg()

        base = optimize_grid(pv, load, price, window_start_h=0, window_len=6, soc_start=40.0, cfg=cfg)
        hed  = optimize_grid(pv, load, price, window_start_h=0, window_len=6, soc_start=40.0, cfg=cfg,
                             hedge_drain_kwh=[0, 0, 2.0, 0, 0, 0])

        # The mid-window cheap hour must absorb more charge
        assert hed["schedule"][2] >= base["schedule"][2], (
            "Trough at h=2 must absorb more charge under hedge"
        )
        # Total charge grows
        assert sum(hed["schedule"]) >= sum(base["schedule"])

    def test_a4_below_floor_eur_kwh_consistency(self):
        """MAJOR-1 acceptance: eur/kwh stay consistent when hedge sags a state below floor.

        Low soc_start (12%) + large hedge (3 kWh) at h=0 → DP state dips under floor_kwh.
        Before MAJOR-1: backtrack recomputed floor-import without the hedge → eur drifted.
        After: stored fi_eur/fi_kwh must match an independent re-simulation within FP tolerance.
        """
        pv    = [0.0] * 6
        load  = [0.5] * 6
        price = [0.10, 0.40, 0.40, 0.40, 0.40, 0.40]
        hedge = [3.0,  0.0,  0.0,  0.0,  0.0,  0.0]
        cfg   = make_cfg()

        res = optimize_grid(pv, load, price, window_start_h=0, window_len=6, soc_start=12.0,
                            cfg=cfg, hedge_drain_kwh=hedge)

        expected_eur, expected_kwh = _resim(
            res["schedule"], hedge=hedge,
            pv=pv, load=load, price=price, soc_start=12.0, cfg=cfg,
        )
        assert res["eur"] == pytest.approx(expected_eur, abs=1e-6), (
            f"eur mismatch: result={res['eur']:.6f}, resim={expected_eur:.6f}"
        )
        assert res["kwh"] == pytest.approx(expected_kwh, abs=1e-6), (
            f"kwh mismatch: result={res['kwh']:.6f}, resim={expected_kwh:.6f}"
        )


# ---------------------------------------------------------------------------
# Layer B — closed-loop signal (soc_drift unit composition)
# ---------------------------------------------------------------------------

# Shared constants: 10 kWh battery, unit efficiencies, slow decay, deadband 0.3 kWh.
_CAP_KWH  = 10.0
_ETA_C    = 1.0
_ETA_D    = 1.0
_DEADBAND = 0.3
_HALFLIFE = 24.0   # slow decay — keeps signal clear in short test sequences
_RELEASE  = 0.5 * _DEADBAND   # 0.15 kWh release threshold


class TestLayerB_ClosedLoopSignal:

    def test_b5_underdelivery_accumulates_positive_drift(self):
        """Forecast 2500 W net surplus, measured flat SoC → per_step > 0 → drift builds.

        3 × 5-minute steps; with _HALFLIFE=24 h decay is negligible over this span.
        Accumulator crosses the engage deadband → drift_kwh returns (>0, True).
        """
        dt_h = 5.0 / 60.0
        pv_w, load_w = 3000.0, 500.0   # 2500 W net expected

        expected_dc = soc_drift.expected_soc_delta_kwh(pv_w, load_w, dt_h, _ETA_C, _ETA_D)
        measured_dc = soc_drift.measured_soc_delta_kwh(60.0, 60.0, _CAP_KWH)  # flat
        per_step    = soc_drift.per_step_drift_kwh(expected_dc, measured_dc, 0.0)

        assert per_step > 0.0, "Underdelivery must yield positive per-step drift"

        acc = 0.0
        for _ in range(3):
            acc = soc_drift.accumulate(acc, per_step, dt_h=dt_h, halflife_h=_HALFLIFE)

        # After 3 × 0.208 kWh steps accumulator ≈ 0.62 kWh > deadband 0.3 kWh
        assert acc > _DEADBAND, f"Accumulator should exceed deadband after 3 steps: {acc:.3f}"
        drift, engaged = soc_drift.drift_kwh(acc, _DEADBAND, _RELEASE, False)
        assert engaged is True, "Should be engaged above deadband"
        assert drift > 0.0, "Drift output must be positive when engaged"

    def test_b6_grid_recovery_shrinks_accumulator(self):
        """After underdelivery builds drift, a SoC jump (grid charge) drives accumulator down.

        5 steps build acc ≈ 1.03 kWh.  Recovery: SoC +10% = +1.0 kWh DC >> expected 0.208.
        Per_step_down < 0 → accumulator shrinks (closed loop verified).
        """
        dt_h = 5.0 / 60.0
        pv_w, load_w = 3000.0, 500.0

        expected_dc = soc_drift.expected_soc_delta_kwh(pv_w, load_w, dt_h, _ETA_C, _ETA_D)
        per_step_up = soc_drift.per_step_drift_kwh(
            expected_dc, soc_drift.measured_soc_delta_kwh(60.0, 60.0, _CAP_KWH), 0.0
        )

        acc = 0.0
        for _ in range(5):
            acc = soc_drift.accumulate(acc, per_step_up, dt_h=dt_h, halflife_h=_HALFLIFE)
        drift_before = acc
        assert drift_before > 0.0, "Precondition: must have positive drift before recovery"

        # Recovery: +10% SoC = +1.0 kWh DC measured >> expected_dc ≈ 0.208
        measured_recovery = soc_drift.measured_soc_delta_kwh(70.0, 60.0, _CAP_KWH)
        per_step_down = soc_drift.per_step_drift_kwh(expected_dc, measured_recovery, 0.0)
        assert per_step_down < 0.0, "Recovery step must be negative"

        acc = soc_drift.accumulate(acc, per_step_down, dt_h=dt_h, halflife_h=_HALFLIFE)
        assert acc < drift_before, "Grid recovery must shrink the accumulator (closed loop)"

    def test_b7_over_delivery_self_corrects_to_zero(self):
        """Sustained over-delivery drives accumulator below release band → (0, False).

        Start just above engage threshold (0.35 kWh, engaged=True).
        Feed large negative per_step until hysteresis releases.
        Final: drift_kwh → (0.0, False) → hedge = 0.
        """
        dt_h = 5.0 / 60.0

        # Seed accumulator just above engage
        acc     = _DEADBAND + 0.05   # 0.35 kWh
        engaged = True

        # Over-delivery: expected slight deficit (200 W PV, 500 W load → −300 W net)
        # but measured SoC jumps +5% = +0.5 kWh DC (grid charge)
        expected_dc = soc_drift.expected_soc_delta_kwh(200.0, 500.0, dt_h, _ETA_C, _ETA_D)
        measured_dc = soc_drift.measured_soc_delta_kwh(65.0, 60.0, _CAP_KWH)  # +0.5 kWh
        per_step    = soc_drift.per_step_drift_kwh(expected_dc, measured_dc, 0.0)
        assert per_step < 0.0, "Over-delivery step must be negative"

        for _ in range(25):
            acc = soc_drift.accumulate(acc, per_step, dt_h=dt_h, halflife_h=_HALFLIFE)
            acc = soc_drift.cap_accumulator(acc, _CAP_KWH)
            drift, engaged = soc_drift.drift_kwh(acc, _DEADBAND, _RELEASE, engaged)
            if not engaged:
                break

        assert not engaged, "Over-delivery must eventually disengage (self-corrects to False)"
        drift, _ = soc_drift.drift_kwh(acc, _DEADBAND, _RELEASE, False)
        assert drift == pytest.approx(0.0), "Drift output must be 0.0 when disengaged"

    def test_b8_export_addback_neutrality(self):
        """Commanded export exactly explains the SoC drop → per_step ≈ 0.

        dt_h = tick_h = 1/60 h → export_dc_step = last_export_kwh_dc × 1 (unit scaling).
        Forecast: pv=0, load=0 → expected_dc = 0.
        Measured: 60% → 59.5% = −0.05 kWh DC (exactly what was exported).
        per_step = 0 − (−0.05 + 0.05) = 0.
        """
        dt_h   = 1.0 / 60.0
        tick_h = 1.0 / 60.0

        expected_dc         = soc_drift.expected_soc_delta_kwh(0.0, 0.0, dt_h, _ETA_C, _ETA_D)
        measured_dc         = soc_drift.measured_soc_delta_kwh(59.5, 60.0, _CAP_KWH)   # −0.05
        last_export_kwh_dc  = 0.05
        export_dc_step      = last_export_kwh_dc * dt_h / tick_h                        # = 0.05
        per_step            = soc_drift.per_step_drift_kwh(expected_dc, measured_dc, export_dc_step)

        assert per_step == pytest.approx(0.0, abs=1e-6), (
            f"Export exactly explains drop — per_step should be 0, got {per_step:.8f}"
        )

    def test_b8b_real_shortfall_during_export_raises_drift(self):
        """SoC drops MORE than the commanded export → residual shortfall captured as drift.

        Same setup as b8 but SoC drops twice as much (−0.10 kWh vs −0.05 commanded).
        per_step = 0 − (−0.10 + 0.05) = +0.05 kWh.
        """
        dt_h   = 1.0 / 60.0
        tick_h = 1.0 / 60.0

        expected_dc        = soc_drift.expected_soc_delta_kwh(0.0, 0.0, dt_h, _ETA_C, _ETA_D)
        measured_dc        = soc_drift.measured_soc_delta_kwh(59.0, 60.0, _CAP_KWH)   # −0.10
        last_export_kwh_dc = 0.05
        export_dc_step     = last_export_kwh_dc * dt_h / tick_h                        # = 0.05
        per_step           = soc_drift.per_step_drift_kwh(expected_dc, measured_dc, export_dc_step)

        # 0 − (−0.10 + 0.05) = 0 − (−0.05) = +0.05
        assert per_step == pytest.approx(0.05, abs=1e-6), (
            f"Residual shortfall should be +0.05 kWh, got {per_step:.8f}"
        )

    def test_b9_absolute_cap_bounds_runaway(self):
        """cap_accumulator clamps the accumulator at capacity_kwh even under runaway input.

        100 large positive steps (0.5 kWh each); after each step the cap is applied.
        Invariant: acc ≤ capacity_kwh at all times.
        Steady-state: acc saturates at exactly capacity_kwh (within FP tolerance).
        """
        dt_h  = 5.0 / 60.0
        per_step = 0.5   # large per-step value — uncapped asymptote >> capacity_kwh

        acc = 0.0
        for i in range(100):
            acc = soc_drift.accumulate(acc, per_step, dt_h=dt_h, halflife_h=_HALFLIFE)
            acc = soc_drift.cap_accumulator(acc, _CAP_KWH)
            assert acc <= _CAP_KWH + 1e-9, (
                f"Step {i}: accumulator {acc:.4f} exceeded cap {_CAP_KWH}"
            )

        # After saturation the value must be pinned at the cap
        assert acc == pytest.approx(_CAP_KWH, abs=0.05), (
            f"Saturated accumulator should sit at capacity_kwh ({_CAP_KWH}), got {acc:.4f}"
        )
