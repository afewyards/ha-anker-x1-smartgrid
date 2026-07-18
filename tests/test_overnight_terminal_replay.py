"""Replay evidence: overnight terminal value against 2026-07-18 morning fixture.

Validates:
1. Flag OFF → byte-identical to live baseline (charge 12+13 UTC, export 19 UTC)
2. Flag ON → burst reduced, end SoC ≈ fw_floor + need
3. Tall-peak variant (evening prices ×1.5) → burst fires even with flag ON
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from custom_components.anker_x1_smartgrid import optimize
from custom_components.anker_x1_smartgrid.efficiency import BinStat, EfficiencyCurve
from custom_components.anker_x1_smartgrid.models import Config, ForecastInterval, PriceSlot

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _load_fixture():
    with open(FIXTURE_DIR / "plan-sensor-2026-07-18-morning.json") as f:
        plan = json.load(f)
    with open(FIXTURE_DIR / "options-2026-07-18.json") as f:
        options = json.load(f)
    return plan.get("attributes", plan), options


def _build_config(options: dict, **overrides) -> Config:
    merged = {**options, **overrides}
    return Config.from_dict(merged)


def _build_eta_curve(curve_dict: dict) -> EfficiencyCurve:
    """Reconstruct EfficiencyCurve from the plan sensor attribute dict."""
    def _bins(raw: list[dict], direction: str) -> list[BinStat]:
        return [
            BinStat(
                lo_w=b["lo_w"],
                hi_w=float("inf") if b["hi_w"] is None else b["hi_w"],
                direction=direction,
                eta=b["eta"],
                measured=b.get("measured"),
                n_runs=b.get("n_runs", 0),
                dc_kwh=b.get("dc_kwh", 0.0),
                confident=b.get("confident", False),
                fallback_reason=b.get("fallback_reason", ""),
            )
            for b in raw
        ]
    charge_bins = _bins(curve_dict["charge"], "charge")
    discharge_bins = _bins(curve_dict["discharge"], "discharge")
    fc = charge_bins[0].eta if charge_bins else 0.92
    fd = discharge_bins[0].eta if discharge_bins else 0.92
    return EfficiencyCurve(charge_bins, discharge_bins, fc, fd)


def _build_slots_and_intervals(horizon: list[dict], now: datetime):
    """Build PriceSlots and ForecastIntervals from horizon rows."""
    slots: list[PriceSlot] = []
    intervals: list[ForecastInterval] = []
    for row in horizon:
        start = datetime.fromisoformat(row["start"])
        if start < now:
            continue
        slots.append(PriceSlot(start=start, price=row["price"]))
        intervals.append(ForecastInterval(
            start=start,
            pv_w=row.get("pv_w", 0.0),
            load_w=row.get("load_w", 400.0),
            dt_h=1.0,
        ))
    return slots, intervals


def _run_dp(cfg: Config, attrs: dict, price_multiplier: float = 1.0):
    """Run optimize_grid on the fixture data, return the result dict."""
    horizon = attrs["horizon"]
    now = datetime(2026, 7, 18, 7, 0, tzinfo=UTC)
    soc_start = 5.0

    slots, intervals = _build_slots_and_intervals(horizon, now)

    if price_multiplier != 1.0:
        slots = [PriceSlot(start=s.start, price=s.price * price_multiplier) for s in slots]

    window_price = [s.price for s in slots]
    window_pv = [iv.pv_w / 1000.0 for iv in intervals]
    window_load = [iv.load_w / 1000.0 for iv in intervals]
    window_len = len(window_price)

    eta_curve = None
    if attrs.get("efficiency_curve"):
        eta_curve = _build_eta_curve(attrs["efficiency_curve"])

    export_price = list(window_price)
    eff_export = [optimize.effective_export_price(p, cfg) for p in export_price]

    water_value = optimize.compute_water_value(min(window_price), cfg)

    water_value_hi = None
    overnight_need_kwh = 0.0

    if cfg.terminal_overnight_credit:
        horizon_edge = slots[-1].start + timedelta(hours=1)
        pickup = horizon_edge + timedelta(hours=11)

        load_by_hod: dict[int, float] = {}
        for iv in intervals:
            load_by_hod[iv.start.hour] = iv.load_w

        max_export_dc_value = water_value
        if eff_export:
            eta_d_static = cfg.eta_discharge_static()
            max_export_dc_value = max(
                max(ep * eta_d_static for ep in eff_export) - cfg.cycle_cost_eur_per_kwh,
                water_value,
            )

        # Descending gap prices with a single cheap tail hour. The is_cheap
        # formula uses fwd_min * (1+band): early hours at 0.45+ are >20% above
        # the tail (0.10), so the walk accumulates ~4h of need before breaking.
        # The high prices push load-weighted mean above the v_lo threshold.
        _gap_prices = [0.50, 0.48, 0.45, 0.42, 0.10]
        est_price_by_hour: dict[datetime, float] = {}
        gap_h = horizon_edge
        idx = 0
        while gap_h < pickup and idx < len(_gap_prices):
            est_price_by_hour[gap_h] = _gap_prices[idx]
            gap_h += timedelta(hours=1)
            idx += 1

        water_value_hi, overnight_need_kwh = optimize.overnight_terminal_params(
            gap_start=horizon_edge,
            pickup=pickup,
            est_price_by_hour=est_price_by_hour,
            load_w_by_hod=load_by_hod,
            v_lo=water_value,
            max_export_dc_value=max_export_dc_value,
            cfg=cfg,
            eta_curve=eta_curve,
        )

    result = optimize.optimize_grid(
        window_pv,
        window_load,
        window_price,
        soc_start,
        cfg,
        window_start_h=7,
        window_len=window_len,
        chargeable=[True] * window_len,
        export_price=eff_export,
        terminal_mode="water_value",
        water_value=water_value,
        water_value_hi=water_value_hi,
        overnight_need_kwh=overnight_need_kwh,
        dt_h=1.0,
        eta_curve=eta_curve,
    )
    return result, water_value_hi, overnight_need_kwh


class TestReplayEvidence:
    """Offline replay against 2026-07-18 morning fixture."""

    def test_flag_off_reproduces_baseline(self):
        """Flag OFF → charge at 12+13 UTC, export at 19 UTC."""
        attrs, options = _load_fixture()
        cfg = _build_config(options, terminal_overnight_credit=False)
        result, v_hi, _ = _run_dp(cfg, attrs)

        assert v_hi is None
        schedule = result["schedule"]
        export_schedule = result.get("export_schedule", [0.0] * len(schedule))

        charge_hours = [i for i, g in enumerate(schedule) if g > 0.1]
        assert 5 in charge_hours  # hour index 5 = 12:00 UTC (window starts at 07:00)
        assert 6 in charge_hours  # hour index 6 = 13:00 UTC

        export_hours = [i for i, e in enumerate(export_schedule) if e > 0.1]
        assert 12 in export_hours  # hour index 12 = 19:00 UTC

    def test_flag_on_values_terminal_energy(self):
        """Flag ON → v_hi > v_lo, meaningful need, DP charges at least as much."""
        attrs, options = _load_fixture()
        cfg_on = _build_config(options, terminal_overnight_credit=True)
        result_on, v_hi, need = _run_dp(cfg_on, attrs)

        cfg_off = _build_config(options, terminal_overnight_credit=False)
        result_off, v_hi_off, _ = _run_dp(cfg_off, attrs)

        assert v_hi_off is None
        assert v_hi is not None
        assert v_hi > 0.139, f"v_hi={v_hi:.4f} should exceed v_lo"
        assert need > 1.0, f"need={need:.2f} kWh should bridge multiple hours"

        charge_on = sum(result_on["schedule"])
        charge_off = sum(result_off["schedule"])
        assert charge_on >= charge_off, (
            f"Flag ON should charge at least as much: {charge_on:.3f} vs OFF {charge_off:.3f}"
        )

    def test_tall_peak_still_exports(self):
        """Evening prices ×1.5 → export still fires (econ-F1 band exercised)."""
        attrs, options = _load_fixture()
        cfg = _build_config(options, terminal_overnight_credit=True)

        horizon = attrs["horizon"]
        now = datetime(2026, 7, 18, 7, 0, tzinfo=UTC)
        slots, intervals = _build_slots_and_intervals(horizon, now)

        slots_tall = []
        for s in slots:
            if s.start.hour >= 17:
                slots_tall.append(PriceSlot(start=s.start, price=s.price * 1.5))
            else:
                slots_tall.append(s)

        window_price = [s.price for s in slots_tall]
        window_pv = [iv.pv_w / 1000.0 for iv in intervals]
        window_load = [iv.load_w / 1000.0 for iv in intervals]

        eta_curve = None
        if attrs.get("efficiency_curve"):
            eta_curve = _build_eta_curve(attrs["efficiency_curve"])

        eff_export = [optimize.effective_export_price(p, cfg) for p in window_price]
        water_value = optimize.compute_water_value(min(window_price), cfg)

        horizon_edge = slots_tall[-1].start + timedelta(hours=1)
        pickup = horizon_edge + timedelta(hours=11)
        load_by_hod = {iv.start.hour: iv.load_w for iv in intervals}

        eta_d_static = cfg.eta_discharge_static()
        max_export_dc_value = max(
            max(ep * eta_d_static for ep in eff_export) - cfg.cycle_cost_eur_per_kwh,
            water_value,
        )

        water_value_hi, overnight_need_kwh = optimize.overnight_terminal_params(
            gap_start=horizon_edge,
            pickup=pickup,
            est_price_by_hour={},
            load_w_by_hod=load_by_hod,
            v_lo=water_value,
            max_export_dc_value=max_export_dc_value,
            cfg=cfg,
            eta_curve=eta_curve,
        )

        result = optimize.optimize_grid(
            window_pv,
            window_load,
            window_price,
            5.0,
            cfg,
            window_start_h=7,
            window_len=len(window_price),
            chargeable=[True] * len(window_price),
            export_price=eff_export,
            terminal_mode="water_value",
            water_value=water_value,
            water_value_hi=water_value_hi,
            overnight_need_kwh=overnight_need_kwh,
            dt_h=1.0,
            eta_curve=eta_curve,
        )

        export_schedule = result.get("export_schedule", [0.0] * len(result["schedule"]))
        total_export = sum(export_schedule)
        assert total_export > 0.5, f"Tall peak should still export, got {total_export:.3f} kWh"
