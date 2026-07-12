import math
from datetime import datetime, timedelta

from custom_components.anker_x1_smartgrid.models import Config
from custom_components.anker_x1_smartgrid.efficiency import EfficiencyCurve, bin_index, segment_episodes, run_eta

# NOTE: run_eta (and, transitively, EfficiencyCurve.build) is live — see
# efficiency.py's module docstring. Since the meter/house-load refactor,
# load_w is a COMPUTED value (pv + meter + batt - loss), so the AC residual
# it feeds the measured-efficiency fit equals batt_w - inverter_loss rather
# than an independent AC measurement. That's fine here: the independent
# ground truth is ΔSoC (the BMS coulomb counter), and what's calibrated is
# the mapping from that same computed-AC "load" to measured ΔSoC — exactly
# the quantity the planner already schedules against.


def _charge_run(t0: datetime, resid_w: float = 10000.0):
    # 4 samples 60s apart, ΔSoC=4.8% over 3 intervals -> dc_power ~9.6kW (bin 5);
    # resid_w=10000W with dc_power=9600W gives eta=0.96, inside the plausibility envelope.
    return [
        {
            "ts": (t0 + timedelta(seconds=60 * i)).isoformat(),
            "soc": 50.0 + 1.6 * i,
            "batt_w": -resid_w,
            "residual_w": -resid_w,
        }
        for i in range(4)
    ]


def test_static_curve_returns_scalar_fallbacks():
    cfg = Config(eta_charge=0.92, round_trip_eff=0.85)
    c = EfficiencyCurve.static(cfg)
    assert c.eta_charge(0.0) == 0.92
    assert c.eta_charge(5000.0) == 0.92
    assert math.isclose(c.eta_discharge(300.0), min(0.85 / 0.92, 1.0))


def test_static_curve_guards_zero_eta_charge():
    c = EfficiencyCurve.static(Config(eta_charge=0.0, round_trip_eff=0.85))
    assert c.eta_charge(1000.0) == 1.0
    assert c.eta_discharge(1000.0) == min(0.85 / 1.0, 1.0)


def test_bin_boundaries_are_half_open():
    assert bin_index(399.9) == 0
    assert bin_index(400.0) == 1
    assert bin_index(4000.0) == 5


def test_as_attributes_has_full_table():
    attrs = EfficiencyCurve.static(Config()).as_attributes()
    assert len(attrs["charge"]) == 6 and len(attrs["discharge"]) == 6
    assert attrs["charge"][0]["confident"] is False
    assert attrs["any_over_unity"] is False


def _rows(seq):
    # seq: list of (secs, soc, batt_w); residual mirrors batt_w sign for these tests.
    # secs is elapsed seconds from a fixed base (not a raw 0-59 field) so gaps can
    # legitimately exceed one minute.
    base = datetime(2026, 7, 1, 0, 0, 0)
    return [
        {
            "ts": (base + timedelta(seconds=s)).isoformat(),
            "soc": soc,
            "batt_w": b,
            "residual_w": b,
        }
        for (s, soc, b) in seq
    ]


def test_sign_flip_splits_run():
    rows = _rows([(0, 50, -3000), (1, 51, -3000), (2, 51, 3000), (3, 50, 3000)])
    runs = segment_episodes(rows)
    assert len(runs) == 2
    assert all(r["batt_w"] < 0 for r in runs[0])
    assert all(r["batt_w"] > 0 for r in runs[1])


def test_band_crossing_splits_run():
    # -300 W (bin 0) then -1000 W (bin 2): a band crossing beyond hysteresis splits.
    rows = _rows([(0, 50, -300), (1, 50, -300), (2, 51, -1000), (3, 52, -1000)])
    runs = segment_episodes(rows)
    assert len(runs) == 2


def test_hysteresis_keeps_boundary_drift_together():
    # ~400 W boundary with +-150 W hysteresis: 380/420 stay one run.
    rows = _rows([(0, 50, -380), (1, 50, -420), (2, 51, -390), (3, 51, -410)])
    runs = segment_episodes(rows)
    assert len(runs) == 1


def test_time_gap_splits_run():
    # gap of 129s exceeds the 2x TICK_SECONDS (120s) tolerance -> splits.
    rows = _rows([(0, 50, -3000), (1, 51, -3000), (130, 55, -3000), (131, 56, -3000)])
    runs = segment_episodes(rows)
    assert len(runs) == 2


def _run(soc0, soc1, resid_w, n=4, step_s=60):
    return [
        {
            "ts": f"2026-07-01T00:{i * step_s // 60:02d}:{i * step_s % 60:02d}",
            "soc": soc0 + (soc1 - soc0) * i / (n - 1),
            "batt_w": resid_w,
            "residual_w": resid_w,
        }
        for i in range(n)
    ]


def test_run_eta_returns_valid_run_for_charge_and_discharge():
    """run_eta computes real per-run efficiency now — see efficiency.py's
    module docstring: the independent axis is ΔSoC (BMS coulomb counter),
    not the AC residual, so the gate is lifted. (was:
    test_run_eta_gated_off_returns_none_for_previously_valid_run)
    """
    cfg = Config(capacity_kwh=10.0)
    charge_run = [
        {"ts": f"2026-07-01T00:0{i}:00", "soc": 50 + 2 * i, "batt_w": -13000, "residual_w": -13000} for i in range(4)
    ]
    r = run_eta(charge_run, cfg)
    assert r is not None
    assert r.direction == "charge"
    assert math.isclose(r.dc_kwh, 0.6)
    assert math.isclose(r.dc_power_w, 12000.0)
    assert math.isclose(r.eta, 0.6 / 0.65, rel_tol=1e-9)

    discharge_run = [
        {"ts": f"2026-07-01T00:0{i}:00", "soc": 50 - 2 * i, "batt_w": 12000, "residual_w": 9000} for i in range(4)
    ]
    r2 = run_eta(discharge_run, cfg)
    assert r2 is not None
    assert r2.direction == "discharge"
    assert math.isclose(r2.dc_kwh, 0.6)
    assert math.isclose(r2.dc_power_w, 12000.0)
    assert math.isclose(r2.eta, 0.45 / 0.6, rel_tol=1e-9)


def test_dsoc_gate_rejects_small_runs():
    cfg = Config(capacity_kwh=10.0)
    run = _run(50.0, 51.0, -3000)
    assert run_eta(run, cfg) is None


def test_envelope_rejects_over_unity_run():
    cfg = Config(capacity_kwh=10.0)
    run = _run(50.0, 60.0, -100)
    assert run_eta(run, cfg) is None


def test_discharge_eta_subtracts_idle_drain():
    """cfg.idle_drain_w (Change A) models a constant standby DC drain that
    the planner now accounts for separately. A discharge run's raw ΔSoC
    bundles BOTH conversion loss and idle drain, so the DISCHARGE branch of
    _run_eta_impl must subtract the modeled idle energy from the DC side
    before computing eta — otherwise the fit absorbs the standby term and
    the planner double-counts it (once via eta, once via idle_drain_w).

    Real 2026-07-11 overnight shape: idle_drain_w=135 W, ~4h run,
    dc_kwh=1.50, AC delivered ~0.8333 kWh -> eta = 0.8333/(1.50-0.54)
    ~= 0.868, NOT 0.8333/1.50 ~= 0.556.
    """
    cfg = Config(capacity_kwh=10.0, idle_drain_w=135.0)
    t0 = datetime(2026, 7, 11, 0, 0, 0)
    resid_w = 208.333333  # 4h * resid_w/1000 ~= 0.8333 kWh AC delivered
    batt_w = 375.0  # matches the ΔSoC-implied gross DC power (1.5 kWh / 4h)
    run = [
        {"ts": t0.isoformat(), "soc": 65.0, "batt_w": batt_w, "residual_w": resid_w},
        {"ts": (t0 + timedelta(hours=4)).isoformat(), "soc": 50.0, "batt_w": batt_w, "residual_w": resid_w},
    ]
    r = run_eta(run, cfg)
    assert r is not None
    assert r.direction == "discharge"
    assert math.isclose(r.dc_kwh, 1.5, rel_tol=1e-6)  # gross DC, unchanged
    ac_delivered = resid_w * 4.0 / 1000.0
    idle_kwh = 135.0 * 4.0 / 1000.0
    expected_eta = ac_delivered / (1.5 - idle_kwh)
    assert math.isclose(r.eta, expected_eta, rel_tol=1e-9)
    assert math.isclose(r.eta, 0.868, rel_tol=2e-3)
    # sanity: without the idle subtraction this would have been ~0.556
    assert not math.isclose(r.eta, ac_delivered / 1.5, rel_tol=0.05)


def test_idle_zero_matches_b1():
    """idle_drain_w=0.0 (the B1 default) must reproduce the exact
    pre-idle-drain-fix RunEta for a discharge run — the idle subtraction
    only changes behavior once idle_drain_w is nonzero.
    """
    cfg = Config(capacity_kwh=10.0, idle_drain_w=0.0)
    discharge_run = [
        {"ts": f"2026-07-01T00:0{i}:00", "soc": 50 - 2 * i, "batt_w": 12000, "residual_w": 9000} for i in range(4)
    ]
    r = run_eta(discharge_run, cfg)
    assert r is not None
    assert r.direction == "discharge"
    assert math.isclose(r.dc_kwh, 0.6)
    assert math.isclose(r.dc_power_w, 12000.0)
    assert math.isclose(r.eta, 0.45 / 0.6, rel_tol=1e-9)


def test_charge_eta_unaffected_by_idle():
    """idle_drain_w only applies to the DISCHARGE branch — a charge run's
    RunEta must be bit-identical whether idle drain is 0 or 135 W.
    """
    charge_run = [
        {"ts": f"2026-07-01T00:0{i}:00", "soc": 50 + 2 * i, "batt_w": -13000, "residual_w": -13000} for i in range(4)
    ]
    r0 = run_eta(charge_run, Config(capacity_kwh=10.0, idle_drain_w=0.0))
    r135 = run_eta(charge_run, Config(capacity_kwh=10.0, idle_drain_w=135.0))
    assert r0 is not None and r135 is not None
    assert r0 == r135


def test_discharge_run_fully_idle_returns_none():
    """If the modeled idle drain would consume the run's entire ΔSoC (a
    tiny ΔSoC over a long duration), run_eta must return None rather than
    a garbage eta from a non-positive denominator.
    """
    cfg = Config(capacity_kwh=10.0, idle_drain_w=135.0)
    t0 = datetime(2026, 7, 11, 0, 0, 0)
    run = [
        {"ts": t0.isoformat(), "soc": 65.0, "batt_w": 50.0, "residual_w": 50.0},
        {"ts": (t0 + timedelta(hours=10)).isoformat(), "soc": 61.5, "batt_w": 50.0, "residual_w": 50.0},
    ]
    assert run_eta(run, cfg) is None


def test_build_promotes_confident_bin():
    """EfficiencyCurve.build() promotes a bin to the measured median once
    enough confident-shaped samples accumulate (n_runs and dc_kwh both
    over threshold). (was: test_build_stays_on_static_fallback_when_gated)
    """
    cfg = Config(capacity_kwh=10.0, eta_charge=0.92, round_trip_eff=0.85)
    now = datetime(2026, 7, 4, 12, 0, 0)
    rows = []
    for k in range(12):
        rows += _charge_run(now - timedelta(hours=k + 1))
    rows.sort(key=lambda r: r["ts"])  # segment_episodes expects chronological input
    c = EfficiencyCurve.build(rows, cfg, now)
    top = c._charge[5]
    assert top.confident is True
    assert top.fallback_reason == ""
    assert top.n_runs == 12
    assert math.isclose(top.dc_kwh, 12 * 0.48, rel_tol=1e-6)
    assert math.isclose(top.eta, 0.96, rel_tol=1e-9)  # measured median, not the 0.92 static fallback


def test_build_low_confidence_bin_falls_back():
    cfg = Config(capacity_kwh=10.0, eta_charge=0.92)
    now = datetime(2026, 7, 4, 12, 0, 0)
    rows = _charge_run(now - timedelta(hours=1))
    c = EfficiencyCurve.build(rows, cfg, now)
    top = c._charge[5]
    assert top.confident is False and top.fallback_reason == "low_confidence"
    assert top.eta == 0.92


def test_build_drops_out_of_window_rows():
    # Rows older than EFFICIENCY_WINDOW_DAYS are filtered out before
    # segmentation, so no bin can ever be confident here — regardless of
    # how much data existed 40 days ago.
    cfg = Config(capacity_kwh=10.0)
    now = datetime(2026, 7, 4, 12, 0, 0)
    old = _charge_run(now - timedelta(days=40))
    c = EfficiencyCurve.build(old, cfg, now)
    assert all(not b.confident for b in c._charge)


def _bin0_discharge_run(t0: datetime, n: int = 121, step_s: int = 120):
    # 4h run (120 x 120s ticks, the max hysteresis-tolerant gap): ΔSoC 15.4%
    # -> dc_kwh=1.54 (gross), avg dc_power=385 W (bin0, <400W). idle_drain_w
    # =135W over 4h -> idle_kwh=0.54, dc_kwh_eff=1.00. Constant residual_w
    # =217.5W over 4h -> ac_delivered=0.87 kWh (trapz of a constant
    # telescopes to resid_w * duration regardless of tick resolution).
    # eta = 0.87 / 1.00 = 0.87 exactly.
    soc0, soc1 = 80.0, 80.0 - 15.4
    return [
        {
            "ts": (t0 + timedelta(seconds=step_s * i)).isoformat(),
            "soc": soc0 + (soc1 - soc0) * i / (n - 1),
            "batt_w": 385.0,
            "residual_w": 217.5,
        }
        for i in range(n)
    ]


def test_low_power_discharge_bin_promotes():
    """Bin0 (<400 W) overnight discharge is the live target regime (measured-
    eta re-enable): idle-debiased eta lands at ~0.87, comfortably inside the
    plausibility envelope (0.50, 1.02) and far from the 0.50 floor — without
    the idle_drain_w subtraction this same data would compute to
    ac_delivered/dc_kwh = 0.87/1.54 ~= 0.565, right near that floor.

    12 runs clears EFFICIENCY_MIN_RUNS=10; each run's gross ΔSoC (15.4%,
    1.54 kWh) individually clears EFFICIENCY_DSOC_GATE_PCT (3.0% = 0.3 kWh
    at 10 kWh capacity) and, summed (18.48 kWh), clears
    EFFICIENCY_MIN_DC_KWH=2.0 with wide margin.
    """
    cfg = Config(capacity_kwh=10.0, idle_drain_w=135.0, eta_charge=0.92, round_trip_eff=0.85)
    now = datetime(2026, 7, 11, 8, 0, 0)
    rows = []
    for k in range(12):
        rows += _bin0_discharge_run(now - timedelta(hours=5 * (k + 1)))
    rows.sort(key=lambda r: r["ts"])  # segment_episodes expects chronological input
    c = EfficiencyCurve.build(rows, cfg, now)
    bottom = c._discharge[0]
    assert bottom.confident is True
    assert bottom.fallback_reason == ""
    assert bottom.n_runs == 12
    assert math.isclose(bottom.dc_kwh, 12 * 1.54, rel_tol=1e-6)
    assert math.isclose(bottom.eta, 0.87, rel_tol=1e-9)
    assert bottom.eta == bottom.measured  # median, not the static fallback


def _agreement_run(batt_w: float, residual_w: float | None = None, n: int = 5, step_s: int = 240):
    # ΔSoC 5% over (n-1)*step_s: 0.5 kWh gross -> dc_power_w = 1875 at defaults.
    t0 = datetime(2026, 7, 1, 0, 0, 0)
    return [
        {
            "ts": (t0 + timedelta(seconds=step_s * i)).isoformat(),
            "soc": 80.0 - 5.0 * i / (n - 1),
            "batt_w": batt_w,
            "residual_w": batt_w if residual_w is None else residual_w,
        }
        for i in range(n)
    ]


def test_agreement_gate_rejects_dsoc_batt_disagreement():
    # dc_power 1875 W vs mean |batt_w| 1200 W -> ratio 1.56 > 1.25 -> discarded.
    cfg = Config(capacity_kwh=10.0, eta_charge=0.92, round_trip_eff=0.85)
    assert run_eta(_agreement_run(1200.0), cfg) is None


def test_agreement_gate_keeps_agreeing_run():
    # ratio 1875/1600 = 1.17 <= 1.25 -> kept; eta = 1600*0.2667/0.5 ~= 0.853.
    cfg = Config(capacity_kwh=10.0, eta_charge=0.92, round_trip_eff=0.85)
    r = run_eta(_agreement_run(1600.0), cfg)
    assert r is not None and r.direction == "discharge"
    assert math.isclose(r.eta, 1600.0 * (960 / 3600) / 500.0, rel_tol=1e-6)


def test_agreement_gate_boundary_inclusive():
    # ratio exactly 1875/1500 = 1.25 -> kept (<=); eta = 0.8, inside envelope.
    cfg = Config(capacity_kwh=10.0, eta_charge=0.92, round_trip_eff=0.85)
    assert run_eta(_agreement_run(1500.0), cfg) is not None


def test_agreement_gate_applies_to_charge_direction():
    # Rising SoC; residual (-1875 W) gives ac_absorbed 0.5 kWh -> eta 1.0,
    # INSIDE the envelope, so only the new gate can reject it: dc_power
    # 1875 W vs |batt_w| 1200 W -> ratio 1.56 -> discarded.
    cfg = Config(capacity_kwh=10.0, eta_charge=0.92, round_trip_eff=0.85)
    run = _agreement_run(-1200.0, residual_w=-1875.0)
    for i, row in enumerate(run):
        row["soc"] = 60.0 + 5.0 * i / (len(run) - 1)
    assert run_eta(run, cfg) is None
