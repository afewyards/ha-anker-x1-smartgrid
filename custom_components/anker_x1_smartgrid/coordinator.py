"""Read live HA state into pure-model inputs."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from . import const
from .models import Config, PlantInputs, PriceSlot
from .parsers import parse_price_curve, _parse_dt
from .tariff import synth_static_price_slots

_BAD = {"unknown", "unavailable", "none", ""}


def read_float(hass: HomeAssistant, entity_id: str) -> float | None:
    state = hass.states.get(entity_id)
    if state is None or str(state.state).lower() in _BAD:
        return None
    try:
        return float(state.state)
    except (ValueError, TypeError):
        return None


def read_attr(hass: HomeAssistant, entity_id: str, attr: str) -> float | None:
    """Return a state attribute as float, or None if missing/non-numeric."""
    state = hass.states.get(entity_id)
    if state is None:
        return None
    val = state.attributes.get(attr)
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def read_plant_inputs(hass: HomeAssistant, data: dict) -> PlantInputs | None:
    soc = read_float(hass, data[const.CONF_ENT_SOC])
    if soc is None:
        return None
    meter_w = read_float(
        hass,
        data.get(const.CONF_ENT_METER_POWER, const.DEFAULT_ENTITIES[const.CONF_ENT_METER_POWER]),
    )
    if meter_w is None:
        return None
    return PlantInputs(soc, meter_w, dt_util.utcnow())


def read_price_slots(hass: HomeAssistant, data: dict) -> list[PriceSlot]:
    # Static tariff mode: synthesize slots from config, ignore any price sensor.
    if data.get(const.CONF_PRICE_MODE, const.DEFAULT_PRICE_MODE) == const.PRICE_MODE_STATIC:
        return synth_static_price_slots(
            dt_util.utcnow(), Config.from_dict(data), dt_util.DEFAULT_TIME_ZONE
        )
    # Sensor mode (default): read the dynamic price sensor's forecast attribute.
    ent = data.get(const.CONF_ENT_PRICE)
    if not ent:
        return []
    state = hass.states.get(ent)
    if state is None:
        return []
    return parse_price_curve(state.attributes.get("forecast"))


def count_persons_home(hass: HomeAssistant, data: dict) -> int | None:
    """Count configured person.* entities currently in state 'home'.

    Returns None when no person entities are configured (feature disabled →
    persons_home recorded as NULL). Returns an int (0 = all away) otherwise.
    """
    entities = data.get(const.CONF_PERSON_ENTITIES) or []
    if not entities:
        return None
    count = 0
    for ent in entities:
        state = hass.states.get(ent)
        if state is not None and str(state.state) == "home":
            count += 1
    return count


def read_pv_remaining_kwh(hass: HomeAssistant, data: dict) -> float | None:
    """Sum PV-today entities.

    Returns None if EVERY configured entity is unavailable (fail-safe: unknown
    solar state should not be treated as zero).  Returns the numeric sum
    (possibly 0.0) if at least one entity reads a real number.
    """
    total = 0.0
    any_available = False
    for ent in data.get(const.CONF_ENT_PV_TODAY, []):
        v = read_float(hass, ent)
        if v is not None:
            total += v
            any_available = True
    if not any_available and data.get(const.CONF_ENT_PV_TODAY, []):
        return None
    return total


def read_pv_tomorrow_kwh(hass: HomeAssistant, data: dict) -> float | None:
    """Sum PV-tomorrow entities.

    Mirrors read_pv_remaining_kwh: returns None iff EVERY configured entity is
    unavailable (fail-safe), 0.0 for an empty list, else the numeric sum.
    """
    total = 0.0
    any_available = False
    for ent in data.get(const.CONF_ENT_PV_TOMORROW, []):
        v = read_float(hass, ent)
        if v is not None:
            total += v
            any_available = True
    if not any_available and data.get(const.CONF_ENT_PV_TOMORROW, []):
        return None
    return total


def read_sun_times(
    hass: HomeAssistant, data: dict
) -> tuple[datetime | None, datetime, datetime] | None:
    """Return (today_sunset, tomorrow_sunrise, tomorrow_sunset) from the sun entity.

    When the sun is above the horizon (daytime): next_setting > next_rising, so
    today_sunset = next_setting, tomorrow_sunrise = next_rising,
    tomorrow_sunset = next_setting + 24h.

    When the sun is below the horizon (night): next_rising < next_setting, so
    today_sunset = None (today's sunset already passed), tomorrow_sunrise = next_rising,
    tomorrow_sunset = next_setting.

    Returns None if the entity or either attribute is missing/unparseable.
    """
    state = hass.states.get(data[const.CONF_ENT_SUN])
    if state is None:
        return None

    def _parse(attr: str) -> datetime | None:
        raw = state.attributes.get(attr)
        if raw is None:
            return None
        try:
            return datetime.fromisoformat(
                str(raw).replace("Z", "+00:00")
            ).astimezone(timezone.utc)
        except (ValueError, TypeError):
            return None

    next_setting = _parse("next_setting")
    next_rising = _parse("next_rising")
    if next_setting is None or next_rising is None:
        return None

    if next_rising < next_setting:
        # Sun is below horizon (night): today's sunset already passed
        return None, next_rising, next_setting
    else:
        # Sun is above horizon (daytime): today's sunset is next_setting
        return next_setting, next_rising, next_setting + timedelta(hours=24)


def read_pv_today_watts(
    hass: HomeAssistant, data: dict
) -> list[list[tuple[datetime, float]]] | None:
    """Return per-source sub-hourly (datetime_utc, watts) sample arrays for
    today's PV forecast.

    Resolves the watts dict from each configured entity (or its ``_remaining``-stripped
    sibling).  Each source's samples are returned as a SEPARATE array (no
    cross-source pooling); resample+sum happens in ``build_pv_curve_from_watts``.

    Returns None iff the entity list is non-empty and no array yielded any watts.
    Returns [] for an empty entity list.
    """
    return _read_pv_watts(hass, data, const.CONF_ENT_PV_TODAY)


def read_pv_tomorrow_watts(
    hass: HomeAssistant, data: dict
) -> list[list[tuple[datetime, float]]] | None:
    """Return per-source sub-hourly (datetime_utc, watts) sample arrays for
    tomorrow's PV forecast.

    Same semantics as ``read_pv_today_watts``.
    """
    return _read_pv_watts(hass, data, const.CONF_ENT_PV_TOMORROW)


def _read_pv_watts(
    hass: HomeAssistant,
    data: dict,
    kwh_key: str,
) -> list[list[tuple[datetime, float]]] | None:
    """Shared helper for read_pv_today_watts / read_pv_tomorrow_watts.

    For each entity id in the configured list:
    - If the entity has a non-empty ``watts`` attribute dict, use it directly.
    - Else, if the entity id ends with ``_remaining``, strip that suffix and look
      for the sibling entity's ``watts`` attribute.
    - Otherwise, skip (no watts for this array).

    Timestamps are parsed via ``datetime.fromisoformat`` and converted to UTC.
    Each source's samples are returned as a SEPARATE array (no cross-source
    pooling); resample+sum happens in build_pv_curve_from_watts.
    """
    kwh_list: list[str] = data.get(kwh_key, const.DEFAULT_ENTITIES.get(kwh_key, []))
    if not kwh_list:
        return []

    per_source: list[list[tuple[datetime, float]]] = []
    any_array_yielded = False

    for entity_id in kwh_list:
        # Step 1: does this entity directly have a non-empty watts attribute?
        state = hass.states.get(entity_id)
        watts_dict = None
        if state is not None:
            candidate = state.attributes.get("watts")
            if candidate:  # non-empty dict
                watts_dict = candidate

        # Step 2: sibling lookup — strip the "_remaining" suffix
        if watts_dict is None and entity_id.endswith("_remaining"):
            sibling_id = entity_id[: -len("_remaining")]
            sibling_state = hass.states.get(sibling_id)
            if sibling_state is not None:
                candidate = sibling_state.attributes.get("watts")
                if candidate:
                    watts_dict = candidate

        if watts_dict is None:
            continue  # no watts found for this array

        # Parse timestamps into this source's own array (kept separate — see
        # build_pv_curve_from_watts for the resample-then-sum logic).
        # Use _parse_dt which treats naive keys (no tz suffix) as UTC, not system-local.
        any_array_yielded = True
        samples: list[tuple[datetime, float]] = []
        for k, v in watts_dict.items():
            dt_utc = _parse_dt(str(k))
            if dt_utc is None:
                continue
            try:
                w = float(v)
            except (ValueError, TypeError):
                continue
            samples.append((dt_utc, w))
        per_source.append(sorted(samples))

    if not any_array_yielded:
        # Non-empty list but no array had watts — caller should treat as unavailable
        return None

    return per_source


def read_pv_today_arrays(
    hass: HomeAssistant, data: dict
) -> list[tuple[float, datetime | None]] | None:
    """Return per-array (kwh, peak_dt) tuples for today's PV forecast.

    Returns None iff the kWh list is non-empty and every array is unavailable.
    Returns [] for an empty kWh list.
    """
    return _read_pv_arrays(
        hass, data, const.CONF_ENT_PV_TODAY, const.CONF_ENT_PV_PEAK_TODAY
    )


def read_pv_tomorrow_arrays(
    hass: HomeAssistant, data: dict
) -> list[tuple[float, datetime | None]] | None:
    """Return per-array (kwh, peak_dt) tuples for tomorrow's PV forecast.

    Returns None iff the kWh list is non-empty and every array is unavailable.
    Returns [] for an empty kWh list.
    """
    return _read_pv_arrays(
        hass, data, const.CONF_ENT_PV_TOMORROW, const.CONF_ENT_PV_PEAK_TOMORROW
    )


def _read_pv_arrays(
    hass: HomeAssistant,
    data: dict,
    kwh_key: str,
    peak_key: str,
) -> list[tuple[float, datetime | None]] | None:
    """Shared helper for read_pv_today_arrays / read_pv_tomorrow_arrays."""
    kwh_list = data.get(kwh_key, [])
    peak_list = data.get(peak_key, [])
    arrays: list[tuple[float, datetime | None]] = []
    any_available = False
    for i, kwh_ent in enumerate(kwh_list):
        kwh = read_float(hass, kwh_ent)
        if kwh is None:
            continue  # array unavailable → skip (partial PV)
        any_available = True
        peak_entity = peak_list[i] if i < len(peak_list) else None
        peak_dt: datetime | None = None
        if peak_entity is not None:
            state = hass.states.get(peak_entity)
            if state is not None:
                peak_dt = _parse_dt(state.state)  # bad/unknown/unavailable → None
        arrays.append((kwh, peak_dt))
    if not any_available and kwh_list:  # non-empty list, every array unavailable
        return None
    return arrays  # empty kwh_list → [] (differs from read_pv_remaining_kwh's 0.0)


def read_sunset(hass: HomeAssistant, data: dict) -> datetime | None:
    state = hass.states.get(data[const.CONF_ENT_SUN])
    if state is None:
        return None
    raw = state.attributes.get("next_setting")
    if raw is None:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt.astimezone(timezone.utc)


def _forecast_float(item: dict, key: str) -> float | None:
    """Return item[key] as float, or None if absent/non-numeric."""
    v = item.get(key)
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


async def read_hourly_weather_forecast(
    hass: HomeAssistant, data: dict
) -> list[dict]:
    """Fetch the hourly weather forecast via the ``weather.get_forecasts`` service.

    Each returned dict has the keys:

    ==================  ==========  ========================
    Key                 Type        HA forecast field
    ==================  ==========  ========================
    ``datetime``        datetime    ``datetime`` (UTC-aware)
    ``temp_forecast``   float|None  ``temperature``
    ``cloud_cover``     float|None  ``cloud_coverage``
    ``humidity``        float|None  ``humidity``
    ``wind_speed``      float|None  ``wind_speed``
    ==================  ==========  ========================

    Each numeric field is independently None when the service omits it.
    Returns ``[]`` (never raises) when the entity is missing/unavailable,
    the service raises, the response is empty/malformed, or the forecast
    list is empty.

    Wire-here note (P1-T6): call once per clock-hour from
    ``controller.tick``; pass the matching entry (via
    ``get_forecast_for_hour``) into ``recorder._record_sample`` as the 4
    weather columns.
    """
    entity_id = data.get(
        const.CONF_ENT_WEATHER_FORECAST,
        const.DEFAULT_ENT_WEATHER_FORECAST,
    )
    try:
        resp = await hass.services.async_call(
            "weather",
            "get_forecasts",
            {"entity_id": entity_id, "type": "hourly"},
            blocking=True,
            return_response=True,
        )
    except Exception:  # noqa: BLE001
        return []

    if not resp or entity_id not in resp:
        return []

    raw_items = resp[entity_id].get("forecast")
    if not raw_items:
        return []

    result: list[dict] = []
    for item in raw_items:
        dt_str = item.get("datetime")
        if not dt_str:
            continue
        try:
            dt = datetime.fromisoformat(
                str(dt_str).replace("Z", "+00:00")
            ).astimezone(timezone.utc)
        except (ValueError, TypeError):
            continue
        result.append({
            "datetime": dt,
            "temp_forecast": _forecast_float(item, "temperature"),
            "cloud_cover": _forecast_float(item, "cloud_coverage"),
            "humidity": _forecast_float(item, "humidity"),
            "wind_speed": _forecast_float(item, "wind_speed"),
        })

    return result


def get_forecast_for_hour(
    forecast: list[dict], target_dt: datetime
) -> dict | None:
    """Return the entry in *forecast* whose ``datetime`` is nearest to *target_dt*.

    Returns ``None`` when *forecast* is empty.

    Typical caller pattern (P1-T6)::

        now_hour = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
        entry = get_forecast_for_hour(forecast, now_hour)
        if entry:
            temp_forecast = entry["temp_forecast"]
            ...
    """
    if not forecast:
        return None
    target_ts = target_dt.timestamp()
    return min(forecast, key=lambda e: abs(e["datetime"].timestamp() - target_ts))
