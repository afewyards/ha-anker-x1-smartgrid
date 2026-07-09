from __future__ import annotations

import asyncio
import datetime as _dt
import functools
import logging
import sqlite3
import sys
import threading
from pathlib import Path
from typing import Optional

import sklearn  # noqa: F401 — import at module level: install failure surfaces at container start
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from health import build_health_payload, read_options, run_retrain_loop
import predictor
import trainer
from trainer import TrainState, train_once

_log = logging.getLogger(__name__)

app = FastAPI()

# Hard cap on /predict payload size: a runaway caller (e.g. an integration bug
# requesting a huge horizon) must not be able to force an unbounded prediction
# loop. 96 hours = 4 days, comfortably above the planner's real horizon.
MAX_PREDICT_HOURS = 96

# Module-level STATE: dormant default so /health works before first train.
_scheduler_task: asyncio.Task | None = None
_DB_PATH: str | None = None  # set at startup; enables serve-time lag refresh

STATE: TrainState = TrainState(
    ready=False,
    promoted=False,
    last_trained=None,
    n_rows=0,
    metrics=None,
    model=None,
)

_PREDICT_LOCK = threading.Lock()


async def _scheduler(db_path: str, retrain_hour: int) -> None:
    """Background task: train once at startup, then retrain daily at retrain_hour."""
    loop = asyncio.get_running_loop()

    async def _run_train() -> None:
        global STATE
        try:
            fn = functools.partial(train_once, db_path)
            result = await loop.run_in_executor(None, fn)
            STATE = result
            _log.info(
                "train_once complete: ready=%s promoted=%s n_rows=%s",
                result.ready,
                result.promoted,
                result.n_rows,
            )
        except Exception:
            _log.exception("_scheduler: unexpected error during train_once")

    # Immediate run at startup
    await _run_train()

    # Daily loop, crash-wrapped (see health.run_retrain_loop).
    await run_retrain_loop(
        retrain_hour, _run_train,
        now_fn=lambda: _dt.datetime.now(_dt.timezone.utc),
        sleep_fn=asyncio.sleep,
    )


@app.on_event("startup")
async def startup_event() -> None:
    opts = read_options()
    db_path: str = opts["db_path"]
    retrain_hour: int = int(opts["retrain_hour"])
    _log.info("startup: db_path=%r retrain_hour=%s", db_path, retrain_hour)
    global _scheduler_task, _DB_PATH
    _DB_PATH = db_path
    _scheduler_task = asyncio.create_task(_scheduler(db_path, retrain_hour))


class HourIn(BaseModel):
    ts: str
    temp_forecast: Optional[float] = None
    cloud_cover: Optional[float] = None
    humidity: Optional[float] = None
    wind_speed: Optional[float] = None
    irradiance: Optional[float] = None
    persons_home: Optional[float] = None


class PredictRequest(BaseModel):
    hours: list[HourIn]


def _probe_db_readable(db_path: str | None) -> bool:
    """Real RO probe: open immutable and SELECT 1 FROM samples. Never raises;
    distinguishes 'DB unreadable' (H3) from 'no data yet' (state.ready False)."""
    if not db_path or not Path(db_path).exists():
        return False
    conn = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True, timeout=2.0)
        conn.execute("SELECT 1 FROM samples LIMIT 1").fetchone()
        return True
    except Exception:  # noqa: BLE001 — health probe must never raise
        return False
    finally:
        if conn is not None:
            conn.close()


@app.get("/health")
def health() -> dict:
    """Return current training state. Non-blocking — reads in-memory STATE only."""
    db_ok = _probe_db_readable(_DB_PATH)
    return build_health_payload(STATE, sklearn.__version__, sys.version, db_readable=db_ok)


@app.post("/predict")
def predict(req: PredictRequest) -> dict:
    """Return p50/p80 load forecasts for the requested hours.

    Non-blocking — reads in-memory STATE only, never triggers training.
    Returns an empty predictions list when the model is not ready.
    Rejects payloads over MAX_PREDICT_HOURS entries with HTTP 400.
    """
    if len(req.hours) > MAX_PREDICT_HOURS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"hours: at most {MAX_PREDICT_HOURS} entries allowed, "
                f"got {len(req.hours)}"
            ),
        )
    state = STATE
    if state.model is None or not state.ready:
        return predictor.build_predict_payload(state, [])
    with _PREDICT_LOCK:
        if _DB_PATH:
            trainer.refresh_model_lookups(state.model, _DB_PATH)
    preds = predictor.predict_hours(state.model, [h.model_dump() for h in req.hours])
    return predictor.build_predict_payload(state, preds)
