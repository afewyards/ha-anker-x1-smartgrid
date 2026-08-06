"""Tests for server.py Pydantic models (HourIn, PredictRequest).

Covers:
- HourIn validates all optional fields including persons_home.
- HourIn round-trips through model_dump().

Note: These tests require FastAPI, which is only available in the Docker
container. In dev environment, these tests are skipped; validation happens
on-box during integration testing.
"""

from __future__ import annotations

from datetime import datetime, timezone, UTC

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
    ts = datetime(2024, 2, 1, 14, 0, 0, tzinfo=UTC).isoformat()
    hour = HourIn(ts=ts, persons_home=2.0)
    assert hour.persons_home == 2.0
    assert hour.ts == ts


def test_houin_persons_home_round_trips():
    """HourIn persons_home field round-trips through dict()."""
    ts = datetime(2024, 2, 1, 14, 0, 0, tzinfo=UTC).isoformat()
    hour = HourIn(ts=ts, temp_forecast=10.0, persons_home=3.5)
    dumped = hour.dict()
    assert dumped["persons_home"] == 3.5
    assert dumped["temp_forecast"] == 10.0


def test_houin_persons_home_optional():
    """HourIn persons_home defaults to None when not provided."""
    ts = datetime(2024, 2, 1, 14, 0, 0, tzinfo=UTC).isoformat()
    hour = HourIn(ts=ts)
    assert hour.persons_home is None
    dumped = hour.dict()
    assert dumped["persons_home"] is None


def test_predict_request_with_persons_home():
    """PredictRequest accepts HourIn with persons_home."""
    ts1 = datetime(2024, 2, 1, 14, 0, 0, tzinfo=UTC).isoformat()
    ts2 = datetime(2024, 2, 1, 15, 0, 0, tzinfo=UTC).isoformat()
    request = PredictRequest(
        hours=[
            HourIn(ts=ts1, persons_home=2.0),
            HourIn(ts=ts2, persons_home=1.5),
        ]
    )
    assert len(request.hours) == 2
    assert request.hours[0].persons_home == 2.0
    assert request.hours[1].persons_home == 1.5


def test_probe_db_readable_true_and_false(tmp_path):
    """_probe_db_readable distinguishes a readable DB from missing/corrupt ones,
    never raising (real RO immutable SELECT 1 probe backing /health's db_ok)."""
    from forecast_core.recorder import DataRecorder
    from server import _probe_db_readable

    good = str(tmp_path / "good.db")
    rec = DataRecorder(good)
    rec.append({"ts": "2026-07-08T09:00:00+00:00", "p1_w": 1.0, "batt_w": 0.0, "pv_w": 0.0, "load_w": 1.0})
    rec.wal_checkpoint()
    rec.close()
    assert _probe_db_readable(good) is True
    assert _probe_db_readable(str(tmp_path / "missing.db")) is False
    bad = tmp_path / "bad.db"
    bad.write_text("not a database")
    assert _probe_db_readable(str(bad)) is False


def test_predict_snapshots_state_and_locks_refresh(monkeypatch):
    """B2: /predict snapshots STATE once and serializes refresh_model_lookups
    under _PREDICT_LOCK so concurrent requests cannot race on shared model state."""
    import server
    import threading

    calls = []

    class _FakeModel:
        pass

    fake_model = _FakeModel()
    fake_state = server.TrainState(
        ready=True,
        promoted=True,
        last_trained=None,
        n_rows=100,
        metrics=None,
        model=fake_model,
    )
    monkeypatch.setattr(server, "STATE", fake_state)
    monkeypatch.setattr(server, "_DB_PATH", "/fake/db")

    def _tracking_refresh(model, db_path):
        calls.append(("refresh", model, db_path, threading.current_thread().name))

    monkeypatch.setattr("trainer.refresh_model_lookups", _tracking_refresh)
    monkeypatch.setattr(
        "predictor.predict_hours",
        lambda model, hours: [{"ts": h.get("ts", ""), "p50": 100.0, "p80": 120.0} for h in hours],
    )
    monkeypatch.setattr(
        "predictor.build_predict_payload",
        lambda state, preds: {"predictions": preds, "ready": state.ready},
    )

    from fastapi.testclient import TestClient

    result = TestClient(server.app).post(
        "/predict",
        json={"hours": [{"ts": "2026-07-09T10:00:00+00:00"}]},
    )
    assert result.status_code == 200
    assert len(calls) == 1
    assert calls[0][1] is fake_model


def test_startup_logs_addon_url(caplog, monkeypatch):
    """Startup logs the reachable base URL: store installs get a repo-hash
    hostname users cannot guess, and it is what the integration's 'Add-on URL'
    option needs."""
    import asyncio
    import logging
    import server

    monkeypatch.setattr(server, "read_options", lambda: {"db_path": "/nonexistent.db", "retrain_hour": 3})
    monkeypatch.setattr(server, "service_url", lambda: "http://2b933eb0-anker-x1-forecast:8099")

    async def _noop_scheduler(db_path, retrain_hour, train_kwargs=None):
        return None

    monkeypatch.setattr(server, "_scheduler", _noop_scheduler)

    async def _run():
        with caplog.at_level(logging.INFO):
            await server.startup_event()
        if server._scheduler_task is not None:
            await server._scheduler_task

    asyncio.run(_run())
    assert "http://2b933eb0-anker-x1-forecast:8099" in caplog.text
    assert "Add-on URL" in caplog.text


def test_startup_always_calls_scheduler_with_three_args(monkeypatch):
    """M4: startup_event must call _scheduler unconditionally with train_kwargs
    as the third argument, even when it is empty — no conditional fork shaped
    around a stub's arity. A stub requiring all three positional args (no
    default for train_kwargs) proves the call site itself always supplies
    three: under the old conditional fork, the empty-train_kwargs path called
    _scheduler with only 2 args and this stub would raise TypeError."""
    import asyncio
    import server

    monkeypatch.setattr(
        server,
        "read_options",
        lambda: {"db_path": "/nonexistent.db", "retrain_hour": 3, "train_since": ""},
    )
    monkeypatch.setattr(server, "service_url", lambda: "http://2b933eb0-anker-x1-forecast:8099")

    received = {}

    async def _strict_scheduler(db_path, retrain_hour, train_kwargs):
        received["train_kwargs"] = train_kwargs
        return None

    monkeypatch.setattr(server, "_scheduler", _strict_scheduler)

    async def _run():
        await server.startup_event()
        if server._scheduler_task is not None:
            await server._scheduler_task

    asyncio.run(_run())
    assert received["train_kwargs"] == {}


def test_startup_threads_train_since_to_scheduler(monkeypatch):
    """M4: startup_event must always call _scheduler unconditionally with
    train_kwargs — when options carry a train_since floor, _scheduler must
    receive it as {"since_iso": <value>}. Before the fix, this path (options
    -> train_kwargs -> train_once partial) had zero end-to-end coverage: the
    3-arg call was only reachable via a conditional fork shaped around a
    stale 2-arg test stub."""
    import asyncio
    import server

    monkeypatch.setattr(
        server,
        "read_options",
        lambda: {"db_path": "/nonexistent.db", "retrain_hour": 3, "train_since": "2026-07-10"},
    )
    monkeypatch.setattr(server, "service_url", lambda: "http://2b933eb0-anker-x1-forecast:8099")

    received = {}

    async def _capturing_scheduler(db_path, retrain_hour, train_kwargs=None):
        received["db_path"] = db_path
        received["retrain_hour"] = retrain_hour
        received["train_kwargs"] = train_kwargs
        return None

    monkeypatch.setattr(server, "_scheduler", _capturing_scheduler)

    async def _run():
        await server.startup_event()
        if server._scheduler_task is not None:
            await server._scheduler_task

    asyncio.run(_run())
    assert received["train_kwargs"] == {"since_iso": "2026-07-10"}


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
