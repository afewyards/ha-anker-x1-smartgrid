"""Tests for server.py Pydantic models (HourIn, PredictRequest).

Covers:
- HourIn validates all optional fields including persons_home.
- HourIn round-trips through model_dump().

Note: These tests require FastAPI, which is only available in the Docker
container. In dev environment, these tests are skipped; validation happens
on-box during integration testing.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

# Skip all tests in this module if FastAPI is not available. Guard on
# "fastapi.testclient" specifically (not just "fastapi"): a sibling test module
# (test_predict_hours_limit.py) installs a minimal `fastapi` stub into
# sys.modules as a local-dev workaround when the real package is missing, and
# that stub is not a real package. Checking the submodule makes this skip
# correctly even if the bare "fastapi" name is already (falsely) satisfied by
# that stub — real fastapi.testclient only resolves when fastapi is genuinely
# installed (as it is in CI).
pytest.importorskip("fastapi.testclient")

from server import HourIn, PredictRequest


def test_houin_with_persons_home():
    """HourIn accepts and validates persons_home field."""
    ts = datetime(2024, 2, 1, 14, 0, 0, tzinfo=timezone.utc).isoformat()
    hour = HourIn(ts=ts, persons_home=2.0)
    assert hour.persons_home == 2.0
    assert hour.ts == ts


def test_houin_persons_home_round_trips():
    """HourIn persons_home field round-trips through dict()."""
    ts = datetime(2024, 2, 1, 14, 0, 0, tzinfo=timezone.utc).isoformat()
    hour = HourIn(ts=ts, temp_forecast=10.0, persons_home=3.5)
    dumped = hour.dict()
    assert dumped["persons_home"] == 3.5
    assert dumped["temp_forecast"] == 10.0


def test_houin_persons_home_optional():
    """HourIn persons_home defaults to None when not provided."""
    ts = datetime(2024, 2, 1, 14, 0, 0, tzinfo=timezone.utc).isoformat()
    hour = HourIn(ts=ts)
    assert hour.persons_home is None
    dumped = hour.dict()
    assert dumped["persons_home"] is None


def test_predict_request_with_persons_home():
    """PredictRequest accepts HourIn with persons_home."""
    ts1 = datetime(2024, 2, 1, 14, 0, 0, tzinfo=timezone.utc).isoformat()
    ts2 = datetime(2024, 2, 1, 15, 0, 0, tzinfo=timezone.utc).isoformat()
    request = PredictRequest(hours=[
        HourIn(ts=ts1, persons_home=2.0),
        HourIn(ts=ts2, persons_home=1.5),
    ])
    assert len(request.hours) == 2
    assert request.hours[0].persons_home == 2.0
    assert request.hours[1].persons_home == 1.5


def test_health_and_predict_smoke():
    """Exercise the real Pydantic request schema via TestClient (not importorskip'd
    away): /health returns ready flag; /predict validates the hours schema."""
    from fastapi.testclient import TestClient
    from server import app
    with TestClient(app) as client:
        h = client.get("/health")
        assert h.status_code == 200 and "ready" in h.json()
        ok = client.post("/predict", json={"hours": [{"ts": "2026-07-08T10:00:00+00:00"}]})
        assert ok.status_code == 200 and "predictions" in ok.json()
        bad = client.post("/predict", json={"hours": "notalist"})
        assert bad.status_code == 422
