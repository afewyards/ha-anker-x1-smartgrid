"""Tests for forecast.build_intervals."""

from datetime import datetime, timezone, timedelta, UTC

from custom_components.anker_x1_smartgrid.models import Config
from custom_components.anker_x1_smartgrid import forecast
from custom_components.anker_x1_smartgrid.forecast import LoadPredictor
from custom_components.anker_x1_smartgrid.hgbr import HGBRQuantileModel

NOW = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)


class _StubHGBR(HGBRQuantileModel):
    """Minimal HGBRQuantileModel subclass that records each quantile received.

    Passes isinstance(x, HGBRQuantileModel) so LoadPredictor threads the
    quantile kwarg through — the behaviour we want to verify.
    """

    def __init__(self, load_by_quantile: dict) -> None:
        super().__init__()
        self._load_by_quantile = load_by_quantile
        self.recorded_quantiles: list = []

    def predict_load_w(self, when, temp, fallback_w, *, quantile=0.5):
        self.recorded_quantiles.append(quantile)
        return self._load_by_quantile.get(quantile, fallback_w)


def test_build_intervals_attaches_load_and_dt():
    pv_curve = [(NOW, 3000.0), (NOW + timedelta(hours=1), 2000.0)]
    # NOW is 2026-06-20 (Saturday), so is_weekend=True
    profile = {(True, 12): 500.0, (True, 13): 700.0}
    ivs = forecast.build_intervals(pv_curve, profile, fallback_load_w=400.0, cfg=Config())
    assert len(ivs) == 2
    assert ivs[0].pv_w == 3000.0
    assert ivs[0].load_w == 500.0
    assert ivs[0].dt_h == 1.0
    assert ivs[1].load_w == 700.0


def test_build_intervals_uses_fallback_load():
    pv_curve = [(NOW, 3000.0)]
    ivs = forecast.build_intervals(pv_curve, {}, fallback_load_w=450.0, cfg=Config())
    assert ivs[0].load_w == 450.0


def test_build_intervals_empty():
    assert forecast.build_intervals([], {}, 400.0, Config()) == []


# ---------------------------------------------------------------------------
# quantile threading — P80 vs P50 with an HGBR predictor
# ---------------------------------------------------------------------------


def test_build_intervals_threads_quantile_p80_to_hgbr():
    """quantile=0.8 is forwarded to HGBRQuantileModel.predict_load_w on every call."""
    hgbr = _StubHGBR({0.5: 500.0, 0.8: 999.0})
    predictor = LoadPredictor.from_model(hgbr)
    pv_curve = [(NOW, 3000.0), (NOW + timedelta(hours=1), 2000.0)]

    ivs = forecast.build_intervals(pv_curve, predictor, fallback_load_w=400.0, cfg=Config(), quantile=0.8)

    # M4 clamp: each P80 request also fetches P50 to enforce p80>=p50;
    # assert 0.8 is still threaded once per hour (2 hours → 2 P80 calls).
    assert hgbr.recorded_quantiles.count(0.8) == 2, hgbr.recorded_quantiles
    assert all(iv.load_w == 999.0 for iv in ivs)


def test_build_intervals_default_quantile_is_p50():
    """Default call (no quantile kwarg) passes 0.5 — preserves all existing callers."""
    hgbr = _StubHGBR({0.5: 500.0, 0.8: 999.0})
    predictor = LoadPredictor.from_model(hgbr)
    pv_curve = [(NOW, 3000.0)]

    ivs = forecast.build_intervals(pv_curve, predictor, fallback_load_w=400.0, cfg=Config())

    assert hgbr.recorded_quantiles == [0.5]
    assert ivs[0].load_w == 500.0


def test_build_intervals_legacy_dict_quantile_unchanged():
    """Legacy dict profile ignores quantile — P80 output == P50 output (graceful no-op)."""
    profile = {(True, 12): 600.0}  # NOW is Saturday (weekend=True), hour=12
    pv_curve = [(NOW, 3000.0)]

    ivs_p50 = forecast.build_intervals(pv_curve, profile, fallback_load_w=400.0, cfg=Config(), quantile=0.5)
    ivs_p80 = forecast.build_intervals(pv_curve, profile, fallback_load_w=400.0, cfg=Config(), quantile=0.8)

    # Profile lookup is quantile-agnostic — both return the same bucket value.
    assert ivs_p50[0].load_w == ivs_p80[0].load_w == 600.0
