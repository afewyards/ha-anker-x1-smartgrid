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
from custom_components.anker_x1_smartgrid.models import Config, PlantInputs, PriceSlot

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
