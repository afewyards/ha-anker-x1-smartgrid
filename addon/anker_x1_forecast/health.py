"""Pure, fastapi-free helpers for the /health endpoint and daily scheduler.

Imported by server.py and directly by tests (never imports fastapi/uvicorn).
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from trainer import TrainState

_log = logging.getLogger(__name__)

_DEFAULTS: dict = {
    "db_path": "/config/anker_x1_smartgrid.db",
    "retrain_hour": 3,
}


def read_options(path: str = "/data/options.json") -> dict:
    """Load add-on options from *path*, merging over defaults.

    Returns defaults when the file is missing, unreadable, or not valid JSON.
    Never raises.
    """
    opts = dict(_DEFAULTS)
    try:
        raw = Path(path).read_text(encoding="utf-8")
        loaded = json.loads(raw)
        if isinstance(loaded, dict):
            opts.update(loaded)
        else:
            _log.warning("read_options: expected dict in %s, got %s", path, type(loaded))
    except FileNotFoundError:
        _log.debug("read_options: %s not found, using defaults", path)
    except Exception:
        _log.exception("read_options: failed to read/parse %s, using defaults", path)
    try:
        rh = int(opts.get("retrain_hour", _DEFAULTS["retrain_hour"]))
        opts["retrain_hour"] = min(23, max(0, rh))
    except (TypeError, ValueError):
        _log.warning("read_options: invalid retrain_hour %r, using default %s",
                     opts.get("retrain_hour"), _DEFAULTS["retrain_hour"])
        opts["retrain_hour"] = _DEFAULTS["retrain_hour"]
    return opts


def build_health_payload(
    state: "TrainState",
    sklearn_version: str,
    python_version: str,
    db_readable: bool | None = None,
) -> dict:
    """Assemble the /health response body from a TrainState.

    Pure dict assembly — no I/O.  Accepts any object with the TrainState
    attribute set (duck-typed so tests can pass dataclasses or SimpleNamespace).

    db_readable distinguishes "the recorder DB cannot be read" from "no data
    yet" (state.ready=False on a fresh install looks identical to a broken
    read-only mount otherwise). None when the caller has no DB path to probe.

    Returns
    -------
    dict with keys: ready, promoted, last_trained, n_rows, metrics,
    sklearn_version, python_version, db_readable.
    """
    last_trained = state.last_trained
    return {
        "ready": state.ready,
        "promoted": state.promoted,
        "last_trained": last_trained.isoformat() if last_trained is not None else None,
        "n_rows": state.n_rows,
        "metrics": state.metrics if state.metrics is not None else {},
        "sklearn_version": sklearn_version,
        "python_version": python_version,
        "db_readable": db_readable,
    }


def seconds_until_next_run(now: datetime, retrain_hour: int, tz: str = "Europe/Amsterdam") -> float:
    """Return seconds from *now* until the next occurrence of retrain_hour:00 local time.

    If retrain_hour:00 is still ahead today (local), uses today's slot.
    Otherwise uses tomorrow's slot.

    Parameters
    ----------
    now:
        A tz-aware datetime (any timezone).
    retrain_hour:
        Local clock hour (0-23) for the daily retrain.
    tz:
        IANA timezone name for "local" interpretation (default Europe/Amsterdam).

    Returns
    -------
    Positive float: seconds until the next run.
    """
    local_tz = ZoneInfo(tz)
    now_local = now.astimezone(local_tz)

    # Candidate: today at retrain_hour:00:00 local
    candidate = now_local.replace(hour=retrain_hour, minute=0, second=0, microsecond=0)
    if candidate <= now_local:
        # Already past — advance to tomorrow
        candidate += timedelta(days=1)

    delta = candidate - now_local
    return max(1.0, delta.total_seconds())


SCHEDULER_BACKOFF_SECONDS = 60.0
"""Sleep after a crashed retrain iteration to avoid a tight failure loop."""


async def run_retrain_loop(retrain_hour, run_train, *, now_fn, sleep_fn,
                           should_continue=lambda: True):
    """Daily retrain loop, crash-wrapped so one failing iteration cannot stop all
    future retrains (which would permanently freeze the served model).

    run_train / sleep_fn are awaitable-returning callables (injected for tests);
    now_fn returns the current tz-aware datetime; should_continue bounds the loop
    (always True in prod). CancelledError is re-raised for clean task shutdown;
    any other exception is logged and the loop backs off, then continues.
    """
    while should_continue():
        try:
            wait_seconds = seconds_until_next_run(now_fn(), retrain_hour)
            _log.info("run_retrain_loop: next retrain in %.0f s (hour=%s local)",
                      wait_seconds, retrain_hour)
            await sleep_fn(wait_seconds)
            await run_train()
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("run_retrain_loop: iteration failed; backing off %.0fs",
                           SCHEDULER_BACKOFF_SECONDS)
            await sleep_fn(SCHEDULER_BACKOFF_SECONDS)
