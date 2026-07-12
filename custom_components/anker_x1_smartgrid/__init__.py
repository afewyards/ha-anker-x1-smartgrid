"""Anker X1 SmartGrid integration setup."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from .actuator import Actuator
from .anker_resolver import apply_anker_resolution
from .const import (
    CONF_MAX_CHARGE_W,
    CONF_MAX_EXPORT_W,
    CONF_PRICE_HISTORY_DAYS,
    DEFAULT_MAX_CHARGE_W,
    DEFAULT_MAX_EXPORT_W,
    DEFAULT_PRICE_HISTORY_DAYS,
    DOMAIN,
    PLATFORMS,
    TICK_SECONDS,
)
from .controller import Controller
from .pricing_store import PriceHistoryStore
from .recorder import DataRecorder

_STORE_VERSION = 1


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})

    # --- OPTIONS MERGE (entry.options fix) ---
    # entry.data holds the initial setup values; entry.options holds values
    # updated via the options flow (e.g. forecast service, export price).
    # Without the merge, options-flow fields are read from entry.data (which
    # keeps the original default) and remain INERT at runtime — the shadow
    # toggle would never activate even if set in the UI.
    # Options take precedence: the options-flow value overrides the original
    # setup default when the same key appears in both.
    data = {**entry.data, **entry.options}

    # --- DEVICE-DERIVED LIMITS ---
    # Nominal X1 charge/discharge capability is hardware, not user preference.
    # Both keys used to be form fields, so older entries carry stale stored
    # values — force the consts unconditionally so they can never win.
    data[CONF_MAX_CHARGE_W] = DEFAULT_MAX_CHARGE_W
    data[CONF_MAX_EXPORT_W] = DEFAULT_MAX_EXPORT_W

    # Re-resolve the configured Anker device in-memory so renamed entities and
    # added battery modules self-heal on every reload.  Must stay in-memory:
    # calling async_update_entry here would fire the update listener below and
    # reload-loop.
    apply_anker_resolution(hass, data)

    db_path = hass.config.path(f"{DOMAIN}.db")
    recorder = await hass.async_add_executor_job(DataRecorder, db_path)

    # Everything below opens no further OS resources on its own, but a
    # failure partway through (bad stored state, controller construction,
    # platform setup, ...) must not leave the recorder connection or the
    # tick timer dangling with no entry left in hass.data to release them.
    cancel = None
    try:
        actuator = Actuator(hass, data)
        store = Store(hass, _STORE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        price_store = PriceHistoryStore(
            Store(hass, _STORE_VERSION, f"{DOMAIN}_price_history"),
            max_days=int(data.get(CONF_PRICE_HISTORY_DAYS, DEFAULT_PRICE_HISTORY_DAYS)),
        )
        await price_store.async_load()

        controller = Controller(hass, data, recorder, actuator, store, price_store=price_store)
        saved = await store.async_load()
        if saved:
            controller.restore(saved)
        else:
            # Fresh install: start disabled so a new box never actuates before the
            # owner reviews the plan.  A live box always has a persisted store (enabled
            # is written every tick), so this only affects first-time setups.
            controller.enabled = False

        async def _tick(_now) -> None:
            await controller.tick()

        cancel = async_track_time_interval(hass, _tick, timedelta(seconds=TICK_SECONDS))

        hass.data[DOMAIN][entry.entry_id] = {
            "controller": controller,
            "recorder": recorder,
            "cancel": cancel,
        }

        # --- OPTIONS UPDATE LISTENER ---
        # Register a listener so that options-flow changes (e.g. toggling
        # changing options in the UI) trigger an entry reload.  The reload
        # re-runs async_setup_entry, which re-applies the options merge above
        # so the controller picks up the new Config without a restart.
        entry.async_on_unload(entry.add_update_listener(_async_reload_on_options_change))

        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        return True
    except Exception:
        if cancel is not None:
            cancel()
        hass.data[DOMAIN].pop(entry.entry_id, None)
        await hass.async_add_executor_job(recorder.close)
        raise


async def _async_reload_on_options_change(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the config entry when options change.

    Triggered by ``entry.add_update_listener`` whenever the options flow
    saves new values (e.g. options changed in the UI).  Reloading
    re-runs ``async_setup_entry``, which re-applies the ``entry.data +
    entry.options`` merge so the running controller receives the updated Config
    without requiring a full Home Assistant restart.
    """
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        stored = hass.data[DOMAIN].pop(entry.entry_id, None)
        if stored:
            stored["cancel"]()
            await stored["controller"].release()
            await hass.async_add_executor_job(stored["recorder"].close)
    return unload_ok
