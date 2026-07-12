"""Direct unit test of server.py's /predict hours-count cap (MAX_PREDICT_HOURS=96).

fastapi is an addon-container-only runtime dependency (see requirements.txt) and is
not installed in the local test venv, so there is no FastAPI TestClient harness
available here (see test_predict_payload.py's docstring — the fastapi-free helper
below `predict()` is what's normally tested; the route itself is validated
on-box). To get real coverage on the new cap logic without adding a fastapi
dependency to the dev venv, this module installs a minimal `fastapi` stub —
just enough for server.py's module-level `from fastapi import FastAPI,
HTTPException` to succeed with `@app.post`/`@app.get`/`@app.on_event` acting as
no-op decorators — then imports server.py for real and calls `server.predict`
directly as a plain function. This exercises the actual handler code (not a
reimplementation of it), with no ASGI/HTTP layer involved.
"""

from __future__ import annotations

import sys
import types

import pytest


def _install_fastapi_stub_if_missing() -> types.ModuleType | None:
    """Install the stub in sys.modules if real fastapi isn't importable.

    Returns the stub module object if one was installed, or None if real
    fastapi was already present (nothing installed, nothing to tear down).
    The return value lets the module-scoped fixture below remove ONLY the
    exact object this function created — never a real fastapi some other
    import may have put there in the meantime.
    """
    try:
        import fastapi

        return None
    except ImportError:
        pass

    class _HTTPException(Exception):
        def __init__(self, status_code: int, detail: str | None = None) -> None:
            self.status_code = status_code
            self.detail = detail
            super().__init__(detail)

    def _identity_decorator_factory(_arg):
        def _decorator(fn):
            return fn

        return _decorator

    class _FastAPI:
        def on_event(self, _name):
            return _identity_decorator_factory(_name)

        def get(self, _path):
            return _identity_decorator_factory(_path)

        def post(self, _path):
            return _identity_decorator_factory(_path)

    stub = types.ModuleType("fastapi")
    stub.FastAPI = _FastAPI
    stub.HTTPException = _HTTPException
    sys.modules["fastapi"] = stub
    return stub


_installed_fastapi_stub = _install_fastapi_stub_if_missing()

import server


@pytest.fixture(scope="module", autouse=True)
def _teardown_fastapi_stub():
    """Keep the stub self-contained to this module: sys.modules is process-wide
    and pytest collects all test modules up front, so a stub left behind here
    would leak into every later-collected module's own `import fastapi` /
    `importorskip("fastapi")` checks (it did — see test_server.py's guard,
    which now checks "fastapi.testclient" specifically as a second line of
    defense). Remove it once this module's tests are done, and only if it is
    still the exact object we installed — never touch a real fastapi."""
    yield
    if _installed_fastapi_stub is not None and sys.modules.get("fastapi") is _installed_fastapi_stub:
        del sys.modules["fastapi"]


def _make_request(n_hours: int) -> server.PredictRequest:
    hours = [server.HourIn(ts=f"2026-01-01T{h % 24:02d}:00:00") for h in range(n_hours)]
    return server.PredictRequest(hours=hours)


def test_96_hours_is_accepted() -> None:
    """Exactly at the cap: no HTTPException, request is processed."""
    req = _make_request(96)
    result = server.predict(req)
    assert result is not None


def test_97_hours_is_rejected_with_400() -> None:
    """One over the cap: HTTPException(400), not a 500 or unhandled error."""
    req = _make_request(97)
    with pytest.raises(Exception) as exc_info:
        server.predict(req)
    assert getattr(exc_info.value, "status_code", None) == 400
