"""End-to-end coverage for the recorder -> EfficiencyCurve.build pipeline.

Now that the measured-eta gate is lifted (B1) and discharge fits subtract the
modeled idle drain (B2), this proves the full path a live deployment
exercises: ``DataRecorder.append()`` writes real rows through the append()
write path (schema migrations, v9 energy-delta bookkeeping, ts normalization
all included) -> ``read_efficiency_samples()`` reads them back in the exact
ts/soc/batt_w/residual_w shape ``EfficiencyCurve.build()`` expects ->
``build()`` promotes a low-power discharge bin to a measured median once
enough episodes clear EFFICIENCY_MIN_RUNS / EFFICIENCY_MIN_DC_KWH, and falls
back to the static scalar when they don't.

Controller-level gating (``cfg.use_measured_eta`` decides whether
``EfficiencyCurve.build`` is even called — see controller.py's
``_refresh_efficiency_curve``/``_planner_curve``) is out of scope here;
``EfficiencyCurve.build`` itself is a pure function of ``rows``/``cfg``/``now``
and does not read that flag.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from custom_components.anker_x1_smartgrid.efficiency import EfficiencyCurve
from custom_components.anker_x1_smartgrid.models import Config
from custom_components.anker_x1_smartgrid.recorder import DataRecorder

# Design point: 1%/25min SoC drop on a 10 kWh pack == 0.1 kWh / (25/60 h) ==
# 240 W average DC power -> lands in the lowest bin ([0, 400) W).
_SOC_DROP_PCT_PER_MIN = 1.0 / 25.0
_BATT_W = 240.0
# AC delivered (via load_w, p1_w=pv_w=0 so residual_w == load_w). Tuned so
# that, after debiting the modeled idle_drain_w=130 standby drain from the
# 80-minute episode's DC side, eta lands ~0.87 (a plausible measured value,
# distinct from the static eta_charge/round_trip_eff fallback).
_RESIDUAL_W = 95.7
_EPISODE_MINUTES = 80  # 80 * (1/25)%/min == 3.2% dSoC, clears EFFICIENCY_DSOC_GATE_PCT=3.0
_IDLE_DRAIN_W = 130.0


def _write_discharge_episode(rec: DataRecorder, t0: datetime, soc0: float) -> tuple[datetime, float]:
    """Append one contiguous per-minute discharge episode starting at (t0, soc0).

    batt_w stays constant at 240 W (same band throughout -> one low-power-bin
    run), residual_w constant so the AC-side trapezoid integration is exact.
    Returns (end_ts, end_soc) so episodes can be chained.
    """
    soc = soc0
    for i in range(_EPISODE_MINUTES + 1):
        ts = t0 + timedelta(minutes=i)
        rec.append({
            "ts": ts.isoformat(),
            "soc": soc,
            "batt_w": _BATT_W,
            "p1_w": 0.0,
            "pv_w": 0.0,
            "load_w": _RESIDUAL_W,  # residual_w = load_w - p1_w - pv_w = load_w here
        })
        soc -= _SOC_DROP_PCT_PER_MIN
    return t0 + timedelta(minutes=_EPISODE_MINUTES), soc


def _write_idle_break(rec: DataRecorder, ts: datetime, soc: float) -> None:
    """A batt_w=0 tick — segment_episodes() ends any run on an idle tick, so
    this is what keeps successive episodes from merging into one giant run.
    """
    rec.append({
        "ts": ts.isoformat(), "soc": soc, "batt_w": 0.0,
        "p1_w": 0.0, "pv_w": 0.0, "load_w": 0.0,
    })


def _make_cfg() -> Config:
    return Config(
        capacity_kwh=10.0,
        use_measured_eta=True,
        idle_drain_w=_IDLE_DRAIN_W,
        eta_charge=0.92,
        round_trip_eff=0.85,
    )


def test_recorder_to_curve_promotes_low_power_discharge_bin(tmp_path):
    """Full pipeline: DB write -> read_efficiency_samples -> EfficiencyCurve.build.

    10 independent low-power discharge episodes (240 W, same band) clear
    EFFICIENCY_MIN_RUNS=10 and EFFICIENCY_MIN_DC_KWH=2.0 for bin 0, so it
    should be promoted to the measured median instead of the static fallback.
    """
    rec = DataRecorder(str(tmp_path / "efficiency_e2e.db"))
    t = datetime(2026, 7, 1, 0, 0, 0, tzinfo=timezone.utc)
    soc = 80.0
    for _ in range(10):
        t, soc = _write_discharge_episode(rec, t, soc)
        t += timedelta(minutes=1)
        _write_idle_break(rec, t, soc)
        t += timedelta(minutes=1)
    last_ts = t

    rows = rec.read_efficiency_samples()
    rec.close()

    # Shape assertion against the real read_efficiency_samples() signature —
    # segment_episodes()/run_eta()/EfficiencyCurve.build() all key off these
    # exact field names.
    assert len(rows) == 10 * (_EPISODE_MINUTES + 1) + 10  # episodes + idle breaks
    for row in rows[:5]:
        assert set(row) == {"ts", "soc", "batt_w", "residual_w"}

    cfg = _make_cfg()
    now = last_ts + timedelta(hours=1)
    curve = EfficiencyCurve.build(rows, cfg, now)

    low_bin = curve._discharge[0]
    assert low_bin.confident is True
    assert low_bin.fallback_reason == ""
    assert low_bin.n_runs == 10
    assert math.isclose(low_bin.dc_kwh, 3.2, rel_tol=1e-6)
    assert low_bin.measured is not None
    assert 0.80 <= low_bin.measured <= 0.95
    assert math.isclose(low_bin.measured, 0.87, abs_tol=0.005)
    # eta_discharge() at 240 W now reads the promoted measured value, not the
    # static cfg.round_trip_eff/cfg.eta_charge fallback (which would be
    # min(0.85/0.92, 1.0) ~= 0.9239).
    assert math.isclose(curve.eta_discharge(_BATT_W), low_bin.measured)
    static_fallback = min(cfg.round_trip_eff / cfg.eta_charge, 1.0)
    assert not math.isclose(curve.eta_discharge(_BATT_W), static_fallback, rel_tol=0.01)


def test_recorder_to_curve_falls_back_when_insufficient_episodes(tmp_path):
    """One episode (n_runs=1, dc_kwh=0.32) is well under EFFICIENCY_MIN_RUNS=10
    and EFFICIENCY_MIN_DC_KWH=2.0 -> the bin stays on the static fallback even
    though a real (if unconfident) median was computed from it."""
    rec = DataRecorder(str(tmp_path / "efficiency_e2e_sparse.db"))
    t0 = datetime(2026, 7, 1, 0, 0, 0, tzinfo=timezone.utc)
    end_ts, _ = _write_discharge_episode(rec, t0, soc0=80.0)

    rows = rec.read_efficiency_samples()
    rec.close()
    assert len(rows) == _EPISODE_MINUTES + 1

    cfg = _make_cfg()
    curve = EfficiencyCurve.build(rows, cfg, end_ts + timedelta(hours=1))

    low_bin = curve._discharge[0]
    assert low_bin.confident is False
    assert low_bin.fallback_reason == "low_confidence"  # a median WAS computed, just not confident
    assert low_bin.measured is not None
    assert math.isclose(low_bin.measured, 0.87, abs_tol=0.005)
    static_fallback = min(cfg.round_trip_eff / cfg.eta_charge, 1.0)
    assert low_bin.eta == static_fallback
    assert curve.eta_discharge(_BATT_W) == static_fallback
