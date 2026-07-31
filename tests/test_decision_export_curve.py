"""Per-slot export-price curve (Frank Energie market sensor) in the DP.

Priority under test:
  static → EXPORT CURVE (new) → export entity == import entity → ratio-scale.
Coverage is all-or-nothing: a curve that misses any priced window slot is
ignored entirely (no per-slot mixing).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.anker_x1_smartgrid import const
from custom_components.anker_x1_smartgrid import controller as ctrl_mod
from custom_components.anker_x1_smartgrid import optimize as optimize_mod
from custom_components.anker_x1_smartgrid import resolution
from custom_components.anker_x1_smartgrid.controller import compute_decision
from custom_components.anker_x1_smartgrid.forecast import LoadPredictor
from custom_components.anker_x1_smartgrid.models import (
    Config,
    ControllerState,
    PlanState,
    PlantInputs,
    PriceSlot,
)

BASE = datetime(2026, 7, 31, 10, 0, tzinfo=UTC)
IMPORT_PRICES = [0.20, 0.30, 0.40, 0.25]
EXPORT_PRICES = [0.05, 0.11, 0.19, 0.07]


def _cfg(**overrides) -> Config:
    return Config.from_dict(
        {
            "capacity_kwh": 10.0,
            "soc_target": 97.0,
            "eta_charge": 0.92,
            "round_trip_eff": 0.85,
            "min_dwell_min": 0,
            "max_charge_w": 6000.0,
            "enable_export": True,
            "export_fee_eur_per_kwh": 0.02,
            "export_min_block_kwh": 0.0,
            **overrides,
        }
    )


def _slots(prices: list[float], *, base: datetime = BASE) -> list[PriceSlot]:
    return [PriceSlot(base + timedelta(hours=i), p, duration_min=60.0) for i, p in enumerate(prices)]


def _run(cfg, monkeypatch, *, export_price, export_slots, matches=False) -> dict:
    """Invoke _dp_select_slots with optimize_grid stubbed; return its kwargs."""
    captured: dict = {}

    def _fake_optimize_grid(*args, **kwargs):
        captured.update(kwargs)
        wl = kwargs["window_len"]
        return {"schedule": [0.0] * wl, "export_schedule": [0.0] * wl, "kwh": 0.0, "eur": 0.0}

    monkeypatch.setattr(optimize_mod, "optimize_grid", _fake_optimize_grid)
    ctrl_mod._dp_select_slots(
        inputs=PlantInputs(soc=80.0, meter_w=0.0, now=BASE),
        slots=_slots(IMPORT_PRICES),
        deadline=BASE + timedelta(hours=len(IMPORT_PRICES)),
        ceiling=0.40,
        cfg=cfg,
        export_price=export_price,
        export_price_matches_import=matches,
        intervals=[],
        export_slots=export_slots,
    )
    return captured


def test_export_curve_beats_ratio_scale(monkeypatch):
    """A covering export curve is used verbatim (minus the fee), not ratio-scaled."""
    cfg = _cfg()
    captured = _run(cfg, monkeypatch, export_price=0.05, export_slots=_slots(EXPORT_PRICES))
    fee = cfg.export_fee_eur_per_kwh
    assert captured["export_price"] == pytest.approx([p - fee for p in EXPORT_PRICES])
    assert captured["feed_in"] == pytest.approx([p - fee for p in EXPORT_PRICES])


def test_no_export_curve_keeps_ratio_scale(monkeypatch):
    """export_slots=None → legacy ratio-scale of the import curve (unchanged)."""
    cfg = _cfg()
    captured = _run(cfg, monkeypatch, export_price=0.05, export_slots=None)
    fee = cfg.export_fee_eur_per_kwh
    ratio = 0.05 / IMPORT_PRICES[0]
    assert captured["export_price"] == pytest.approx([p * ratio - fee for p in IMPORT_PRICES])


def test_partial_export_curve_falls_back_all_or_nothing(monkeypatch):
    """A curve missing a priced window slot is ignored entirely — no mixing."""
    cfg = _cfg()
    partial = _slots(EXPORT_PRICES)[:2]  # only the first 2 of 4 window slots
    captured = _run(cfg, monkeypatch, export_price=0.05, export_slots=partial)
    fee = cfg.export_fee_eur_per_kwh
    ratio = 0.05 / IMPORT_PRICES[0]
    assert captured["export_price"] == pytest.approx([p * ratio - fee for p in IMPORT_PRICES])


def test_static_mode_ignores_export_curve(monkeypatch):
    """Static tariff mode still flat-broadcasts the configured constant."""
    cfg = _cfg(price_mode=const.PRICE_MODE_STATIC)
    captured = _run(cfg, monkeypatch, export_price=0.08, export_slots=_slots(EXPORT_PRICES))
    fee = cfg.export_fee_eur_per_kwh
    assert captured["export_price"] == pytest.approx([0.08 - fee] * len(IMPORT_PRICES))


def test_export_price_none_disables_credit_even_with_curve(monkeypatch):
    """No export entity value → export credit stays a strict no-op."""
    cfg = _cfg()
    captured = _run(cfg, monkeypatch, export_price=None, export_slots=_slots(EXPORT_PRICES))
    assert captured["export_price"] is None
    assert captured["feed_in"] is None


def test_export_curve_at_finer_resolution_than_window(monkeypatch):
    """A 15-min export curve forward-fills onto a 60-min window grid."""
    cfg = _cfg()
    fine = [
        PriceSlot(BASE + timedelta(minutes=15 * i), EXPORT_PRICES[i // 4], duration_min=15.0)
        for i in range(16)
    ]
    captured = _run(cfg, monkeypatch, export_price=0.05, export_slots=fine)
    fee = cfg.export_fee_eur_per_kwh
    assert captured["export_price"] == pytest.approx([p - fee for p in EXPORT_PRICES])


# ---------------------------------------------------------------------------
# Overnight v_hi clamp must use the same export curve the DP uses
# ---------------------------------------------------------------------------

_PREDICTOR = LoadPredictor.from_profile({})
_WV_BASE = datetime(2026, 7, 31, 14, 0, tzinfo=UTC)  # 14:00 UTC → horizon crosses midnight
_WV_IMPORT = [0.20, 0.28, 0.30, 0.32, 0.40, 0.45, 0.20, 0.18, 0.15, 0.13]
_WV_EXPORT = [0.06, 0.09, 0.10, 0.11, 0.14, 0.16, 0.07, 0.06, 0.05, 0.04]


def _wv_cfg(**overrides) -> Config:
    return Config.from_dict(
        {
            "capacity_kwh": 10.0,
            "soc_target": 97.0,
            "soc_floor": 5.0,
            "eta_charge": 0.92,
            "round_trip_eff": 0.85,
            "min_dwell_min": 0,
            "max_charge_w": 6000.0,
            "enable_export": True,
            "export_fee_eur_per_kwh": 0.02,
            "export_min_block_kwh": 0.0,
            "cycle_cost_eur_per_kwh": 0.10,
            **overrides,
        }
    )


def _clamp_from_compute_decision(monkeypatch, *, export_slots) -> float:
    """Spy on the terminal builder and return the max_export_dc_value it received."""
    captured: dict = {}

    def _spy_builder(*, max_export_dc_value, v_lo, **_):
        captured["max_export_dc_value"] = max_export_dc_value
        return (v_lo, 0.0)

    monkeypatch.setattr(optimize_mod, "overnight_terminal_params", _spy_builder)
    cfg = _wv_cfg()
    compute_decision(
        PlanState(ControllerState.PASSIVE, _WV_BASE - timedelta(hours=2), ()),
        PlantInputs(soc=80.0, meter_w=0.0, now=_WV_BASE),
        _slots(_WV_IMPORT, base=_WV_BASE),
        0.0,
        _WV_BASE + timedelta(hours=len(_WV_IMPORT)),
        _PREDICTOR,
        None,
        cfg,
        export_price=0.06,
        export_price_matches_import=False,
        export_slots=export_slots,
        _out={},
    )
    return captured["max_export_dc_value"]


def test_v_hi_clamp_uses_export_curve_max(monkeypatch):
    """With a covering export curve, the clamp is max(export curve), not the import max."""
    cfg = _wv_cfg()
    clamp = _clamp_from_compute_decision(monkeypatch, export_slots=_slots(_WV_EXPORT, base=_WV_BASE))
    best_eff = max(optimize_mod.effective_export_price(p, cfg) for p in _WV_EXPORT)
    expected = best_eff * cfg.eta_discharge_static() - cfg.cycle_cost_eur_per_kwh
    assert clamp == pytest.approx(expected)


def test_v_hi_clamp_without_curve_keeps_ratio_scale(monkeypatch):
    """No curve → unchanged legacy ratio-scale clamp."""
    cfg = _wv_cfg()
    clamp = _clamp_from_compute_decision(monkeypatch, export_slots=None)
    ratio = 0.06 / _WV_IMPORT[0]
    best_eff = max(optimize_mod.effective_export_price(p * ratio, cfg) for p in _WV_IMPORT)
    expected = best_eff * cfg.eta_discharge_static() - cfg.cycle_cost_eur_per_kwh
    assert clamp == pytest.approx(expected)


def _quarter_slots(hourly_prices: list[float], *, base: datetime) -> list[PriceSlot]:
    """Forward-fill hourly prices onto a 15-min grid (4 slots/hour)."""
    return [
        PriceSlot(base + timedelta(hours=i, minutes=15 * q), p, duration_min=15.0)
        for i, p in enumerate(hourly_prices)
        for q in range(4)
    ]


def test_v_hi_clamp_and_dp_share_window_anchor_at_15min_mid_hour(monkeypatch):
    """Regression (finding 3): the v_hi clamp used to filter its window from
    ``resolution.hour_floor(inputs.now)`` while the DP window (and the export-
    curve coverage check) uses ``resolution.floor_to_slot(inputs.now,
    slot_minutes)``. At slot_minutes=15 with ``now`` mid-hour those two
    anchors diverge by up to 3 slots, so a curve that (correctly) starts at
    the DP's own window edge used to fail the clamp's stricter coverage check
    and silently fall back to the ratio-scaled import curve, even though the
    DP itself used the curve verbatim. Both must now agree.
    """
    cfg = _wv_cfg()
    now = _WV_BASE + timedelta(minutes=30)  # hour_floor=14:00, floor_to_slot(15)=14:30
    slot_anchor = resolution.floor_to_slot(now, 15)
    assert slot_anchor != resolution.hour_floor(now)  # sanity: anchors genuinely diverge here

    import_slots = _quarter_slots(_WV_IMPORT, base=_WV_BASE)
    # Curve covers exactly the DP window -- NOT the two already-elapsed
    # quarters (14:00, 14:15) that only the buggy hour-floor anchor required.
    export_curve = [s for s in _quarter_slots(_WV_EXPORT, base=_WV_BASE) if s.start >= slot_anchor]

    dp_captured: dict = {}

    def _fake_optimize_grid(*args, **kwargs):
        dp_captured.update(kwargs)
        wl = kwargs["window_len"]
        return {"schedule": [0.0] * wl, "export_schedule": [0.0] * wl, "kwh": 0.0, "eur": 0.0}

    monkeypatch.setattr(optimize_mod, "optimize_grid", _fake_optimize_grid)

    clamp_captured: dict = {}

    def _spy_builder(*, max_export_dc_value, v_lo, **_):
        clamp_captured["max_export_dc_value"] = max_export_dc_value
        return (v_lo, 0.0)

    monkeypatch.setattr(optimize_mod, "overnight_terminal_params", _spy_builder)

    out: dict = {}
    compute_decision(
        PlanState(ControllerState.PASSIVE, now - timedelta(hours=2), ()),
        PlantInputs(soc=80.0, meter_w=0.0, now=now),
        import_slots,
        0.0,
        _WV_BASE + timedelta(hours=len(_WV_IMPORT)),
        _PREDICTOR,
        None,
        cfg,
        export_price=0.06,
        export_price_matches_import=False,
        export_slots=export_curve,
        slot_minutes=15,
        _out=out,
    )

    # The DP used the curve verbatim for its own first (14:30) window slot.
    fee = cfg.export_fee_eur_per_kwh
    assert dp_captured["export_price"][0] == pytest.approx(_WV_EXPORT[0] - fee)

    # The v_hi clamp must ALSO have used the curve -- not fallen back to the
    # ratio-scaled import curve -- proving both share the same window anchor.
    best_eff = max(optimize_mod.effective_export_price(p, cfg) for p in _WV_EXPORT)
    expected_clamp = best_eff * cfg.eta_discharge_static() - cfg.cycle_cost_eur_per_kwh
    assert clamp_captured["max_export_dc_value"] == pytest.approx(expected_clamp)

    # Finding 2 tie-in: the diagnostic must also report the curve as covered.
    assert out["export_curve_covered"] is True
    assert out["export_curve_slots"] == len(export_curve)


# ---------------------------------------------------------------------------
# Finding 2: export-curve engagement must be visible via the _out side-channel
# (and logged when a non-empty curve fails coverage), not silently swallowed.
# ---------------------------------------------------------------------------


def test_dp_select_slots_reports_curve_covered_in_out(monkeypatch):
    """A covering curve sets _out['export_curve_covered']=True and
    export_curve_slots to the raw supplied-slot count."""
    cfg = _cfg()

    def _fake_optimize_grid(*args, **kwargs):
        wl = kwargs["window_len"]
        return {"schedule": [0.0] * wl, "export_schedule": [0.0] * wl, "kwh": 0.0, "eur": 0.0}

    monkeypatch.setattr(optimize_mod, "optimize_grid", _fake_optimize_grid)
    out: dict = {}
    ctrl_mod._dp_select_slots(
        inputs=PlantInputs(soc=80.0, meter_w=0.0, now=BASE),
        slots=_slots(IMPORT_PRICES),
        deadline=BASE + timedelta(hours=len(IMPORT_PRICES)),
        ceiling=0.40,
        cfg=cfg,
        export_price=0.05,
        export_price_matches_import=False,
        intervals=[],
        export_slots=_slots(EXPORT_PRICES),
        _out=out,
    )
    assert out["export_curve_covered"] is True
    assert out["export_curve_slots"] == len(EXPORT_PRICES)


def test_dp_select_slots_reports_curve_not_covered_in_out(monkeypatch):
    """A partial curve sets export_curve_covered=False but still reports the
    raw supplied-slot count (not 0 -- the curve WAS present, just insufficient)."""
    cfg = _cfg()

    def _fake_optimize_grid(*args, **kwargs):
        wl = kwargs["window_len"]
        return {"schedule": [0.0] * wl, "export_schedule": [0.0] * wl, "kwh": 0.0, "eur": 0.0}

    monkeypatch.setattr(optimize_mod, "optimize_grid", _fake_optimize_grid)
    out: dict = {}
    partial = _slots(EXPORT_PRICES)[:2]
    ctrl_mod._dp_select_slots(
        inputs=PlantInputs(soc=80.0, meter_w=0.0, now=BASE),
        slots=_slots(IMPORT_PRICES),
        deadline=BASE + timedelta(hours=len(IMPORT_PRICES)),
        ceiling=0.40,
        cfg=cfg,
        export_price=0.05,
        export_price_matches_import=False,
        intervals=[],
        export_slots=partial,
        _out=out,
    )
    assert out["export_curve_covered"] is False
    assert out["export_curve_slots"] == 2


def test_dp_select_slots_reports_zero_slots_when_no_curve(monkeypatch):
    """No export_slots at all -> covered=False, slots=0 (not absent)."""
    cfg = _cfg()

    def _fake_optimize_grid(*args, **kwargs):
        wl = kwargs["window_len"]
        return {"schedule": [0.0] * wl, "export_schedule": [0.0] * wl, "kwh": 0.0, "eur": 0.0}

    monkeypatch.setattr(optimize_mod, "optimize_grid", _fake_optimize_grid)
    out: dict = {}
    ctrl_mod._dp_select_slots(
        inputs=PlantInputs(soc=80.0, meter_w=0.0, now=BASE),
        slots=_slots(IMPORT_PRICES),
        deadline=BASE + timedelta(hours=len(IMPORT_PRICES)),
        ceiling=0.40,
        cfg=cfg,
        export_price=0.05,
        export_price_matches_import=False,
        intervals=[],
        export_slots=None,
        _out=out,
    )
    assert out["export_curve_covered"] is False
    assert out["export_curve_slots"] == 0


def test_partial_export_curve_logs_coverage_failure_at_info(monkeypatch, caplog):
    """Finding 2c: a non-empty curve that fails all-or-nothing coverage logs
    one INFO line with covered-vs-required slot counts."""
    cfg = _cfg()

    def _fake_optimize_grid(*args, **kwargs):
        wl = kwargs["window_len"]
        return {"schedule": [0.0] * wl, "export_schedule": [0.0] * wl, "kwh": 0.0, "eur": 0.0}

    monkeypatch.setattr(optimize_mod, "optimize_grid", _fake_optimize_grid)
    partial = _slots(EXPORT_PRICES)[:2]  # covers 2 of 4 required window slots
    with caplog.at_level("INFO", logger="custom_components.anker_x1_smartgrid.decision"):
        ctrl_mod._dp_select_slots(
            inputs=PlantInputs(soc=80.0, meter_w=0.0, now=BASE),
            slots=_slots(IMPORT_PRICES),
            deadline=BASE + timedelta(hours=len(IMPORT_PRICES)),
            ceiling=0.40,
            cfg=cfg,
            export_price=0.05,
            export_price_matches_import=False,
            intervals=[],
            export_slots=partial,
        )
    info_msgs = [r.getMessage() for r in caplog.records if r.levelno == 20]
    assert any("2" in m and "4" in m for m in info_msgs), info_msgs


def test_full_export_curve_does_not_log_coverage_failure(monkeypatch, caplog):
    """No spurious INFO log when the curve fully covers the window."""
    cfg = _cfg()

    def _fake_optimize_grid(*args, **kwargs):
        wl = kwargs["window_len"]
        return {"schedule": [0.0] * wl, "export_schedule": [0.0] * wl, "kwh": 0.0, "eur": 0.0}

    monkeypatch.setattr(optimize_mod, "optimize_grid", _fake_optimize_grid)
    with caplog.at_level("INFO", logger="custom_components.anker_x1_smartgrid.decision"):
        ctrl_mod._dp_select_slots(
            inputs=PlantInputs(soc=80.0, meter_w=0.0, now=BASE),
            slots=_slots(IMPORT_PRICES),
            deadline=BASE + timedelta(hours=len(IMPORT_PRICES)),
            ceiling=0.40,
            cfg=cfg,
            export_price=0.05,
            export_price_matches_import=False,
            intervals=[],
            export_slots=_slots(EXPORT_PRICES),
        )
    assert caplog.records == []


def test_compute_decision_out_reports_curve_covered(monkeypatch):
    """End-to-end: compute_decision's _out side-channel carries the new
    export-curve diagnostics through from _dp_select_slots."""
    cfg = _wv_cfg()

    def _fake_optimize_grid(*args, **kwargs):
        wl = kwargs["window_len"]
        return {"schedule": [0.0] * wl, "export_schedule": [0.0] * wl, "kwh": 0.0, "eur": 0.0}

    monkeypatch.setattr(optimize_mod, "optimize_grid", _fake_optimize_grid)
    out: dict = {}
    compute_decision(
        PlanState(ControllerState.PASSIVE, _WV_BASE - timedelta(hours=2), ()),
        PlantInputs(soc=80.0, meter_w=0.0, now=_WV_BASE),
        _slots(_WV_IMPORT, base=_WV_BASE),
        0.0,
        _WV_BASE + timedelta(hours=len(_WV_IMPORT)),
        _PREDICTOR,
        None,
        cfg,
        export_price=0.06,
        export_price_matches_import=False,
        export_slots=_slots(_WV_EXPORT, base=_WV_BASE),
        _out=out,
    )
    assert out["export_curve_covered"] is True
    assert out["export_curve_slots"] == len(_WV_EXPORT)


def test_compute_decision_out_reports_curve_not_covered(monkeypatch):
    """End-to-end: a partial curve surfaces covered=False, not a missing key."""
    cfg = _wv_cfg()

    def _fake_optimize_grid(*args, **kwargs):
        wl = kwargs["window_len"]
        return {"schedule": [0.0] * wl, "export_schedule": [0.0] * wl, "kwh": 0.0, "eur": 0.0}

    monkeypatch.setattr(optimize_mod, "optimize_grid", _fake_optimize_grid)
    out: dict = {}
    partial = _slots(_WV_EXPORT, base=_WV_BASE)[:2]
    compute_decision(
        PlanState(ControllerState.PASSIVE, _WV_BASE - timedelta(hours=2), ()),
        PlantInputs(soc=80.0, meter_w=0.0, now=_WV_BASE),
        _slots(_WV_IMPORT, base=_WV_BASE),
        0.0,
        _WV_BASE + timedelta(hours=len(_WV_IMPORT)),
        _PREDICTOR,
        None,
        cfg,
        export_price=0.06,
        export_price_matches_import=False,
        export_slots=partial,
        _out=out,
    )
    assert out["export_curve_covered"] is False
    assert out["export_curve_slots"] == 2
