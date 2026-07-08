"""Tests for predictor.build_predict_payload — the fastapi-free /predict payload helper.

No fastapi import here (fastapi is not in the dev venv; the route itself is
validated on-box where fastapi is installed inside the Docker container).
"""
from __future__ import annotations

from types import SimpleNamespace

from predictor import build_predict_payload


def _make_state(*, ready: bool, promoted: bool) -> SimpleNamespace:
    """Duck-typed stand-in for TrainState — only ready/promoted are read."""
    return SimpleNamespace(ready=ready, promoted=promoted)


class TestBuildPredictPayloadReady:
    """Model is trained, promoted, and predictions are available."""

    def test_shape(self) -> None:
        state = _make_state(ready=True, promoted=True)
        preds = [
            {"ts": "2026-06-22T14:00:00+00:00", "p50_w": 820.0, "p80_w": 1040.0},
            {"ts": "2026-06-22T15:00:00+00:00", "p50_w": 760.0, "p80_w": 980.0},
        ]
        result = build_predict_payload(state, preds)
        assert set(result.keys()) == {"ready", "promoted", "predictions"}

    def test_ready_flag(self) -> None:
        state = _make_state(ready=True, promoted=True)
        result = build_predict_payload(state, [])
        assert result["ready"] is True

    def test_promoted_flag(self) -> None:
        state = _make_state(ready=True, promoted=True)
        result = build_predict_payload(state, [])
        assert result["promoted"] is True

    def test_predictions_passed_through(self) -> None:
        state = _make_state(ready=True, promoted=True)
        preds = [
            {"ts": "2026-06-22T14:00:00+00:00", "p50_w": 820.0, "p80_w": 1040.0},
        ]
        result = build_predict_payload(state, preds)
        assert result["predictions"] == preds

    def test_predictions_identity(self) -> None:
        """The returned predictions list is the exact object passed in."""
        state = _make_state(ready=True, promoted=True)
        preds: list[dict] = []
        result = build_predict_payload(state, preds)
        assert result["predictions"] is preds

    def test_not_promoted(self) -> None:
        """ready=True but promoted=False is reflected correctly."""
        state = _make_state(ready=True, promoted=False)
        result = build_predict_payload(state, [{"ts": "x", "p50_w": 1.0, "p80_w": 2.0}])
        assert result["ready"] is True
        assert result["promoted"] is False
        assert len(result["predictions"]) == 1


class TestBuildPredictPayloadDormant:
    """Model not yet trained (dormant / not-ready state)."""

    def test_shape(self) -> None:
        state = _make_state(ready=False, promoted=False)
        result = build_predict_payload(state, [])
        assert set(result.keys()) == {"ready", "promoted", "predictions"}

    def test_ready_false(self) -> None:
        state = _make_state(ready=False, promoted=False)
        result = build_predict_payload(state, [])
        assert result["ready"] is False

    def test_promoted_false(self) -> None:
        state = _make_state(ready=False, promoted=False)
        result = build_predict_payload(state, [])
        assert result["promoted"] is False

    def test_empty_predictions(self) -> None:
        state = _make_state(ready=False, promoted=False)
        result = build_predict_payload(state, [])
        assert result["predictions"] == []

    def test_predictions_still_passed_through(self) -> None:
        """Even with ready=False the helper returns whatever predictions list it receives.

        The caller (server.py) passes [] when not ready; the helper itself is
        agnostic and just reflects the list, making it easy to test both paths.
        """
        state = _make_state(ready=False, promoted=False)
        preds = [{"ts": "2026-06-22T14:00:00+00:00", "p50_w": 0.0, "p80_w": 0.0}]
        result = build_predict_payload(state, preds)
        assert result["predictions"] == preds
