"""Tick input refresh: device limits, efficiency curve, weather, presence and the hourly recorder upkeep."""

from __future__ import annotations

import dataclasses
import importlib.util
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from . import coordinator, resolution, tick_remote

if TYPE_CHECKING:
    from .controller import Controller

_LOGGER = logging.getLogger(__name__)

# Same-hour retries of an EMPTY weather read (integration not populated yet
# after boot), bounded for the same reason when the entity never fills.
_MAX_WEATHER_READ_RETRIES = 5


@dataclasses.dataclass(frozen=True)
class TickInputs:
    """Weather and presence values the rest of the tick reads."""

    wf_list: list[dict]
    weather_entry: dict | None
    persons_home_now: int | None
    temp_by_hour: dict[datetime, float | None]


async def _record_devices(controller: Controller, now: datetime) -> None:
    """Read tracked devices on the loop and store one row each off it; never raises into the tick."""
    try:
        readings = coordinator.read_device_watts(controller._hass, controller._data)
    except Exception:
        controller._log_failure("device usage read")
        return
    controller._failed_once.discard("device usage read")
    controller._device_watts_now = {ent: watts for ent, (watts, _state) in readings.items()}
    if not readings or controller._recorder is None:
        return
    try:
        await controller._hass.async_add_executor_job(
            controller._recorder.append_device_samples, now.isoformat(), readings
        )
        controller._failed_once.discard("device usage write")
    except Exception:
        controller._log_failure("device usage write")


async def gather(controller: Controller, now: datetime) -> TickInputs:
    """Refresh the tick's inputs and run the once-per-hour recorder upkeep."""
    # RACE 1: retry device-derived capacity/limit resolution while it
    # hasn't genuinely landed yet (self-heals a HA-restart race where the
    # anker_x1 entities weren't populated at config-entry setup). Runs
    # regardless of self.enabled so a re-enable always sees correct cfg.
    controller._maybe_reresolve_anker()
    # Refresh the measured efficiency curve off-loop (cached; cheap no-op most
    # ticks).
    await controller._refresh_efficiency_curve(now)
    # Hour-gate: the hourly forecast changes at most hourly, and an unbounded
    # await here (a hung weather integration) would otherwise wedge every 60 s
    # tick with the inverter parked. Read once per clock-hour, plus up to
    # _MAX_WEATHER_READ_RETRIES same-hour retries while the read comes back []
    # (integration not populated yet after boot); keep the last good forecast
    # on [].
    _read_weather = now.hour != controller._last_weather_hour
    if _read_weather:
        controller._last_weather_hour = now.hour
        controller._weather_retries_left = _MAX_WEATHER_READ_RETRIES
    elif controller._weather_retries_left > 0 and not controller._weather_forecast:
        controller._weather_retries_left -= 1
        _read_weather = True
    if _read_weather:
        _fetched = await coordinator.read_hourly_weather_forecast(controller._hass, controller._data)
        if _fetched:
            controller._weather_forecast = _fetched
            controller._weather_retries_left = 0
    _wf_list = controller._weather_forecast
    _now_hour = resolution.hour_floor(now)
    _weather_entry = coordinator.get_forecast_for_hour(_wf_list, _now_hour)
    # Home-presence count for this tick (on-loop state reads).
    _persons_home_now = coordinator.count_persons_home(controller._hass, controller._data)
    controller._persons_home_now = _persons_home_now
    await _record_devices(controller, now)
    # Per-hour temperature map derived from the hourly weather forecast.
    # Keys are hour-start UTC datetimes; values are temp_forecast (float | None).
    # Passed to compute_decision so each forecast interval uses its own hourly
    # temperature rather than the flat current-temperature scalar.
    _temp_by_hour: dict[datetime, float | None] = {
        resolution.hour_floor(e["datetime"]): e.get("temp_forecast")
        for e in (_wf_list or [])
        if e.get("datetime") is not None
    }

    # Hourly rollup: aggregate completed clock-hours into samples_hourly once per
    # clock-hour, regardless of enabled/disabled state.  Guard against a missing
    # recorder (partial initialisation or tests without one — the backfill sync path
    # uses try/except, but here we guard explicitly to keep the tick clean).
    if controller._recorder is not None and now.hour != controller._last_rollup_hour:
        controller._last_rollup_hour = now.hour
        _hourly_cutoff = (now - timedelta(days=controller.cfg.retention_hourly_days)).isoformat()
        _raw_cutoff = (now - timedelta(days=controller.cfg.retention_days)).isoformat()
        await controller._hass.async_add_executor_job(
            controller._rollup_hourly_sync, now.isoformat(), _hourly_cutoff, _raw_cutoff
        )

    # H3a: periodic WAL checkpoint so a read-only immutable reader (the addon,
    # mounted config:ro) sees recent rows. Once per clock-hour, off-loop.
    if controller._recorder is not None and now.hour != controller._last_wal_checkpoint_hour:
        controller._last_wal_checkpoint_hour = now.hour
        await controller._hass.async_add_executor_job(controller._recorder.wal_checkpoint)

    # A4: the learned model is always on, but sklearn is NOT an integration
    # requirement (musl is why the addon exists) — without the add-on a stock
    # install silently falls back to the bucketed model forever. This runs before
    # the probe, so a set address only counts as missing once its retries are spent.
    if (
        not controller._learned_model_warned
        and not controller._addon_seen
        and (
            not controller.cfg.addon_url
            or controller._addon_health_retry_count >= tick_remote._MAX_ADDON_HEALTH_RETRIES
        )
        and importlib.util.find_spec("sklearn") is None
    ):
        controller._learned_model_warned = True
        _LOGGER.warning(
            "The learned load model needs scikit-learn, which is unavailable in the "
            "integration, and no forecast add-on answered — falling back to "
            "the bucketed load model. Install the Anker X1 Forecast add-on to "
            "use the learned model."
        )

    return TickInputs(
        wf_list=_wf_list,
        weather_entry=_weather_entry,
        persons_home_now=_persons_home_now,
        temp_by_hour=_temp_by_hour,
    )
