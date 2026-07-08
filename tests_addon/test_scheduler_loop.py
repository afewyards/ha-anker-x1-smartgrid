"""Tests for the crash-wrapped retrain scheduler loop (health.run_retrain_loop).

server.py imports fastapi which is not in .venv_test, so the loop logic is
extracted into health.py (fastapi-free) and tested here.
"""
import asyncio
from datetime import datetime, timezone
import pytest
from health import run_retrain_loop, SCHEDULER_BACKOFF_SECONDS


def _fixed_now():
    return datetime(2024, 1, 1, 1, 0, tzinfo=timezone.utc)


async def test_loop_survives_iteration_exception():
    calls = {"train": 0, "sleeps": []}

    async def sleep_fn(s):
        calls["sleeps"].append(s)

    async def run_train():
        calls["train"] += 1
        if calls["train"] == 1:
            raise RuntimeError("boom in first retrain")

    it = {"n": 0}

    def should_continue():
        it["n"] += 1
        return it["n"] <= 2

    await run_retrain_loop(3, run_train, now_fn=_fixed_now,
                           sleep_fn=sleep_fn, should_continue=should_continue)
    assert calls["train"] == 2, "loop must keep retraining after a failed iteration"
    assert SCHEDULER_BACKOFF_SECONDS in calls["sleeps"], "must back off after a crash"


async def test_loop_propagates_cancellation():
    async def sleep_fn(s):
        raise asyncio.CancelledError

    async def run_train():
        pass

    with pytest.raises(asyncio.CancelledError):
        await run_retrain_loop(3, run_train, now_fn=_fixed_now, sleep_fn=sleep_fn)
