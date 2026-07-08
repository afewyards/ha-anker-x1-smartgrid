import math
from datetime import datetime, timedelta

from custom_components.anker_x1_smartgrid.models import Config
from custom_components.anker_x1_smartgrid.efficiency import EfficiencyCurve, segment_episodes, run_eta


def _charge_run(t0: datetime, resid_w: float = 10000.0):
    # 4 samples 60s apart, ΔSoC=4.8% over 3 intervals -> dc_power ~9.6kW (bin 5);
    # resid_w=10000W with dc_power=9600W gives eta=0.96, inside the plausibility envelope.
    return [
        {"ts": (t0 + timedelta(seconds=60 * i)).isoformat(),
         "soc": 50.0 + 1.6 * i, "batt_w": -resid_w, "residual_w": -resid_w}
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
    c = EfficiencyCurve.static(Config())
    assert c._bin_index(399.9) == 0
    assert c._bin_index(400.0) == 1
    assert c._bin_index(4000.0) == 5


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
        {"ts": f"2026-07-01T00:{i*step_s//60:02d}:{i*step_s%60:02d}",
         "soc": soc0 + (soc1 - soc0) * i / (n - 1),
         "batt_w": resid_w, "residual_w": resid_w}
        for i in range(n)
    ]


def test_charge_run_eta_below_one():
    cfg = Config(capacity_kwh=10.0)
    run = [
        {"ts": f"2026-07-01T00:0{i}:00", "soc": 50 + 2 * i, "batt_w": -13000, "residual_w": -13000}
        for i in range(4)
    ]
    res = run_eta(run, cfg)
    assert res is not None and res.direction == "charge"
    assert 0.50 <= res.eta <= 1.02
    assert res.dc_power_w > 0


def test_dsoc_gate_rejects_small_runs():
    cfg = Config(capacity_kwh=10.0)
    run = _run(50.0, 51.0, -3000)
    assert run_eta(run, cfg) is None


def test_envelope_rejects_over_unity_run():
    cfg = Config(capacity_kwh=10.0)
    run = _run(50.0, 60.0, -100)
    assert run_eta(run, cfg) is None


def test_build_confident_bin_uses_median():
    cfg = Config(capacity_kwh=10.0, eta_charge=0.92, round_trip_eff=0.85)
    now = datetime(2026, 7, 4, 12, 0, 0)
    rows = []
    for k in range(12):
        rows += _charge_run(now - timedelta(hours=k + 1))
    rows.sort(key=lambda r: r["ts"])  # segment_episodes expects chronological input
    c = EfficiencyCurve.build(rows, cfg, now)
    top = c._charge[5]
    assert top.confident is True
    assert top.n_runs >= 10 and top.dc_kwh >= 2.0
    assert 0.85 <= top.eta <= 1.0


def test_build_low_confidence_bin_falls_back():
    cfg = Config(capacity_kwh=10.0, eta_charge=0.92)
    now = datetime(2026, 7, 4, 12, 0, 0)
    rows = _charge_run(now - timedelta(hours=1))
    c = EfficiencyCurve.build(rows, cfg, now)
    top = c._charge[5]
    assert top.confident is False and top.fallback_reason == "low_confidence"
    assert top.eta == 0.92


def test_build_drops_out_of_window_rows():
    cfg = Config(capacity_kwh=10.0)
    now = datetime(2026, 7, 4, 12, 0, 0)
    old = _charge_run(now - timedelta(days=40))
    c = EfficiencyCurve.build(old, cfg, now)
    assert all(not b.confident for b in c._charge)
