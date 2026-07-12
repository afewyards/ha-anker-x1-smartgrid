from datetime import datetime, timezone, timedelta, UTC
from custom_components.anker_x1_smartgrid.models import Config
from custom_components.anker_x1_smartgrid.dataquality import FeatureRow
from custom_components.anker_x1_smartgrid import loadmodel, forecast
from custom_components.anker_x1_smartgrid.hgbr import HGBRQuantileModel

NOW = datetime(2026, 6, 22, 8, 0, tzinfo=UTC)  # Monday hour 8


def test_predictor_from_profile_ignores_temp():
    lp = forecast.LoadPredictor.from_profile({(False, 8): 900.0})
    assert lp.predict(NOW, temp=2.0, fallback_w=0.0) == 900.0


def test_predictor_from_model_uses_temp():
    rows = [
        FeatureRow(datetime(2026, 6, 15, 8, tzinfo=UTC), 8, False, 1000.0, 2.0),
        FeatureRow(datetime(2026, 6, 16, 8, tzinfo=UTC), 8, False, 300.0, 18.0),
    ]
    model = loadmodel.BucketedLoadModel.fit(rows)
    lp = forecast.LoadPredictor.from_model(model)
    assert lp.predict(NOW, temp=2.0, fallback_w=0.0) == 1000.0
    assert lp.predict(NOW, temp=18.0, fallback_w=0.0) == 300.0


def test_build_intervals_with_predictor_and_temps():
    lp = forecast.LoadPredictor.from_profile({(False, 8): 800.0, (False, 9): 600.0})
    pv_curve = [(NOW, 3000.0), (NOW + timedelta(hours=1), 2000.0)]
    ivs = forecast.build_intervals(pv_curve, lp, fallback_load_w=400.0, cfg=Config(), temp_by_start={NOW: 5.0})
    assert ivs[0].load_w == 800.0
    assert ivs[1].load_w == 600.0


# ---------------------------------------------------------------------------
# Quantile kwarg tests (P3-T2)
# ---------------------------------------------------------------------------


class _NoQuantileModel:
    """Non-HGBR stub: raises TypeError if quantile kwarg is passed (like BucketedLoadModel)."""

    def predict_load_w(self, when, temp, fallback_w):
        return 400.0


def _make_hgbr_stub(return_w: float) -> tuple[HGBRQuantileModel, list]:
    """Return an (unfitted) HGBRQuantileModel with a monkeypatched predict_load_w.

    The real instance satisfies isinstance(model, HGBRQuantileModel); the
    monkeypatch records every quantile value passed so tests can assert on it.
    """
    calls: list[float] = []
    model = HGBRQuantileModel()

    def fake_predict(when, temp, fallback_w, *, quantile=0.5):
        calls.append(quantile)
        return return_w

    model.predict_load_w = fake_predict  # type: ignore[method-assign]
    return model, calls


def test_predictor_hgbr_threads_explicit_quantile():
    """quantile=0.8 must reach the HGBR model."""
    model, calls = _make_hgbr_stub(777.0)
    lp = forecast.LoadPredictor.from_model(model)
    result = lp.predict(NOW, temp=10.0, fallback_w=0.0, quantile=0.8)
    assert result == 777.0
    # The explicit quantile must be forwarded; after M4 the clamp also calls P50
    # (since 777==777 result is unchanged), so we check presence not exact list.
    assert 0.8 in calls


def test_predictor_hgbr_default_quantile_is_p50():
    """Omitting quantile kwarg must default to 0.5."""
    model, calls = _make_hgbr_stub(500.0)
    lp = forecast.LoadPredictor.from_model(model)
    lp.predict(NOW, temp=10.0, fallback_w=0.0)  # no quantile kwarg
    assert calls == [0.5]


def test_predictor_non_hgbr_model_no_quantile_kwarg():
    """Non-HGBR models must NOT receive quantile; they would raise TypeError."""
    lp = forecast.LoadPredictor.from_model(_NoQuantileModel())
    # Must not raise and must return the model's value, even with quantile passed
    result = lp.predict(NOW, temp=10.0, fallback_w=0.0, quantile=0.8)
    assert result == 400.0


def test_predictor_profile_ignores_quantile():
    """Profile path must return the profile value regardless of quantile."""
    lp = forecast.LoadPredictor.from_profile({(False, 8): 900.0})
    assert lp.predict(NOW, temp=2.0, fallback_w=0.0, quantile=0.8) == 900.0


def test_predictor_positional_call_backcompat():
    """predict(when, temp, fallback_w) positional call must still work — quantile is kw-only."""
    lp = forecast.LoadPredictor.from_profile({(False, 8): 900.0})
    assert lp.predict(NOW, 2.0, 0.0) == 900.0


def test_loadpredictor_clamps_crossed_hgbr_quantiles():
    """When the HGBR P80 < P50 (estimators crossed), predict(quantile=0.8) returns P50."""
    from datetime import datetime, timezone
    from custom_components.anker_x1_smartgrid.forecast import LoadPredictor

    class _CrossingModel:
        # p50=900 but p80=700 (inverted) — predict must clamp up to 900.
        def predict_load_w(self, when, temp, fallback_w, *, quantile=0.5):
            return 700.0 if quantile > 0.5 else 900.0

    p = LoadPredictor.from_model(_CrossingModel())
    when = datetime(2026, 6, 29, 18, tzinfo=UTC)
    assert p.predict(when, None, 400.0, quantile=0.8) == 900.0  # clamped to p50
    assert p.predict(when, None, 400.0, quantile=0.5) == 900.0  # median unchanged


def test_loadpredictor_passthrough_when_p80_above_p50():
    """Non-crossed quantiles are returned unchanged (no spurious clamp)."""
    from datetime import datetime, timezone
    from custom_components.anker_x1_smartgrid.forecast import LoadPredictor

    class _MonotoneModel:
        def predict_load_w(self, when, temp, fallback_w, *, quantile=0.5):
            return 1100.0 if quantile > 0.5 else 900.0

    p = LoadPredictor.from_model(_MonotoneModel())
    when = datetime(2026, 6, 29, 18, tzinfo=UTC)
    assert p.predict(when, None, 400.0, quantile=0.8) == 1100.0
