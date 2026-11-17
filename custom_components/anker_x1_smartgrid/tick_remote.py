"""Hourly add-on health poll and remote load-forecast fetch for the controller tick."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .controller import Controller

_LOGGER = logging.getLogger(__name__)

# ── RACE 2 (2026-07-29 add-battery-module restart) ──────────────────────────
# Bounded same-hour retry count for the cheap add-on /health poll (see the
# elif branch in _tick_impl and Controller._poll_addon_health). Ticks are
# ~60s apart, so this buys ~_MAX_ADDON_HEALTH_RETRIES extra minutes of
# catch-up after a failed hourly poll without hammering a genuinely-dead
# add-on every tick for the rest of the clock-hour.
_MAX_ADDON_HEALTH_RETRIES = 5


async def refresh(controller: Controller, now: datetime, _wf_list: list[dict], _persons_home_now: int | None) -> None:
    """Run the hourly add-on health poll and remote forecast fetch, plus their same-hour catch-ups."""
    # Health poll + remote forecast fetch: once per clock-hour when the add-on
    # is enabled. Uses the weather forecast already fetched above as the
    # feature payload (no POST while it is empty; the fetch is then owed to a
    # later same-hour tick). A failed fetch (network error, timeout, non-200,
    # bad JSON) returns None and leaves the map unchanged; a {} map, or a
    # health reading of not ready/promoted, replaces it so the tier falls back
    # (see _refresh_remote_forecast). This never raises; any exception is
    # swallowed here as a final backstop even though fetch_forecast and
    # fetch_health already guarantee non-raising.
    _addon_hour_ran = controller.cfg.addon_enabled and now.hour != controller._last_remote_forecast_hour
    if _addon_hour_ran:
        controller._last_remote_forecast_hour = now.hour
        # RACE 2 bookkeeping: a fresh hour resets the bounded same-hour
        # health-retry budget (see the elif branch below).
        controller._addon_health_retry_count = 0
        try:
            # Health FIRST: a failure anywhere in the persons/predict path below
            # must not blind reachability reporting — that is exactly the window
            # in which a dead addon_url would otherwise hide.
            await controller._poll_addon_health(now)

            await controller._refresh_remote_forecast(now, _wf_list, _persons_home_now)
        except Exception:
            _LOGGER.debug("remote_forecast fetch raised unexpectedly", exc_info=True)
    elif (
        controller.cfg.addon_enabled
        and controller._addon_health is None
        and controller._addon_health_retry_count < _MAX_ADDON_HEALTH_RETRIES
    ):
        # RACE 2 catch-up (2026-07-29 add-battery-module restart): Supervisor
        # started the add-on container 17s AFTER this integration's hourly
        # health poll already ran and failed, latching "unreachable" on the
        # ml-status card for up to an hour even though the add-on was healthy
        # for almost all of that time. Gating the CHEAP /health call (5s
        # timeout cap) to strictly once-per-clock-hour hides a real recovery
        # that happens moments later.
        #
        # Retry ONLY the health call on later ticks within the same hour,
        # capped at _MAX_ADDON_HEALTH_RETRIES attempts — ticks are ~60s
        # apart, so this buys a few extra minutes of catch-up without
        # hammering a genuinely-dead add-on every tick for the rest of the
        # hour. This branch is skipped once the poll succeeds (self._addon_health
        # is no longer None) or the cap is hit; either way it reverts to the
        # normal once-per-hour cadence. Only /health is retried; its first
        # success owes the hour's forecast fetch once more, since the hourly
        # one most likely failed against the same not-yet-started container.
        controller._addon_health_retry_count += 1
        try:
            await controller._poll_addon_health(now)
        except Exception:
            _LOGGER.debug("remote_forecast: health retry raised unexpectedly", exc_info=True)
        if controller._addon_health is not None:
            controller._remote_forecast_pending = True

    if controller.cfg.addon_enabled and controller._remote_forecast_pending and not _addon_hour_ran:
        try:
            await controller._refresh_remote_forecast(now, _wf_list, _persons_home_now)
        except Exception:
            _LOGGER.debug("remote_forecast: owed same-hour fetch raised unexpectedly", exc_info=True)
