"""Tests for coordinator.py — reading live HA state into pure inputs."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import ServiceRegistry

from custom_components.anker_x1_smartgrid import const, coordinator
from tests.conftest import ANKER_TEST_ENTITIES


def _data():
    d = {**const.DEFAULT_ENTITIES, **ANKER_TEST_ENTITIES}
    return d


async def test_read_float_handles_unavailable(hass):
    hass.states.async_set("sensor.x", "unavailable")
    assert coordinator.read_float(hass, "sensor.x") is None
    hass.states.async_set("sensor.x", "12.5")
    assert coordinator.read_float(hass, "sensor.x") == 12.5
    assert coordinator.read_float(hass, "sensor.missing") is None


async def test_read_plant_inputs(hass):
    d = _data()
    hass.states.async_set(d[const.CONF_ENT_SOC], "42")
    for i, ent in enumerate(d[const.CONF_ENT_PHASE]):
        hass.states.async_set(ent, str(100 * (i + 1)))
    pi = coordinator.read_plant_inputs(hass, d)
    assert pi.soc == 42.0
    assert pi.phase_import_w == (100.0, 200.0, 300.0)


async def test_read_plant_inputs_none_when_soc_missing(hass):
    d = _data()
    for ent in d[const.CONF_ENT_PHASE]:
        hass.states.async_set(ent, "100")
    assert coordinator.read_plant_inputs(hass, d) is None


async def test_read_price_slots(hass):
    d = _data()
    hass.states.async_set(
        d[const.CONF_ENT_PRICE], "0.13",
        {"forecast": [{"datetime": "2026-06-20T12:00:00Z", "electricity_price": 1300000}]},
    )
    slots = coordinator.read_price_slots(hass, d)
    assert len(slots) == 1
    assert abs(slots[0].price - 0.13) < 1e-6


async def test_count_persons_home_counts_home_states(hass):
    hass.states.async_set("person.alice", "home")
    hass.states.async_set("person.bob", "not_home")
    hass.states.async_set("person.carol", "home")
    data = {const.CONF_PERSON_ENTITIES: ["person.alice", "person.bob", "person.carol"]}
    assert coordinator.count_persons_home(hass, data) == 2


async def test_count_persons_home_none_when_unconfigured(hass):
    assert coordinator.count_persons_home(hass, {}) is None
    assert coordinator.count_persons_home(hass, {const.CONF_PERSON_ENTITIES: []}) is None


async def test_count_persons_home_missing_entity_not_counted(hass):
    data = {const.CONF_PERSON_ENTITIES: ["person.ghost"]}
    assert coordinator.count_persons_home(hass, data) == 0


async def test_read_attr_returns_attribute_as_float(hass):
    hass.states.async_set("weather.knmi_home", "cloudy", {"temperature": 18.5})
    assert coordinator.read_attr(hass, "weather.knmi_home", "temperature") == 18.5


async def test_read_attr_returns_none_when_missing(hass):
    # entity absent
    assert coordinator.read_attr(hass, "weather.missing", "temperature") is None
    # entity present but attribute absent
    hass.states.async_set("weather.knmi_home", "sunny")
    assert coordinator.read_attr(hass, "weather.knmi_home", "temperature") is None


async def test_read_pv_remaining_sums(hass):
    d = _data()
    d[const.CONF_ENT_PV_TODAY] = [_KWH_TODAY_0, _KWH_TODAY_1]
    hass.states.async_set(d[const.CONF_ENT_PV_TODAY][0], "3.1")
    hass.states.async_set(d[const.CONF_ENT_PV_TODAY][1], "4.3")
    assert abs(coordinator.read_pv_remaining_kwh(hass, d) - 7.4) < 1e-6


# ---------------------------------------------------------------------------
# FIX M4 — all-PV-unavailable must return None (fail-safe)
# ---------------------------------------------------------------------------

async def test_read_pv_remaining_all_unavailable_returns_none(hass):
    """When every PV-today entity is unavailable, return None (not 0.0)."""
    d = _data()
    d[const.CONF_ENT_PV_TODAY] = [_KWH_TODAY_0, _KWH_TODAY_1]
    hass.states.async_set(d[const.CONF_ENT_PV_TODAY][0], "unavailable")
    hass.states.async_set(d[const.CONF_ENT_PV_TODAY][1], "unavailable")
    result = coordinator.read_pv_remaining_kwh(hass, d)
    assert result is None, f"Expected None when all PV unavailable, got {result}"


async def test_read_pv_remaining_partial_unavailable_sums_available(hass):
    """When one PV entity is available, return the sum (not None)."""
    d = _data()
    d[const.CONF_ENT_PV_TODAY] = [_KWH_TODAY_0, _KWH_TODAY_1]
    hass.states.async_set(d[const.CONF_ENT_PV_TODAY][0], "unavailable")
    hass.states.async_set(d[const.CONF_ENT_PV_TODAY][1], "2.5")
    result = coordinator.read_pv_remaining_kwh(hass, d)
    assert result is not None
    assert abs(result - 2.5) < 1e-6


async def test_read_pv_remaining_genuine_zero_not_none(hass):
    """0.0 from a sensor is a real night reading — must NOT be treated as unavailable."""
    d = _data()
    d[const.CONF_ENT_PV_TODAY] = [_KWH_TODAY_0, _KWH_TODAY_1]
    hass.states.async_set(d[const.CONF_ENT_PV_TODAY][0], "0.0")
    hass.states.async_set(d[const.CONF_ENT_PV_TODAY][1], "0.0")
    result = coordinator.read_pv_remaining_kwh(hass, d)
    assert result is not None
    assert result == 0.0


async def test_read_pv_remaining_empty_list_returns_zero(hass):
    """No PV-today entities configured → returns 0.0 (not None)."""
    d = _data()
    d[const.CONF_ENT_PV_TODAY] = []
    result = coordinator.read_pv_remaining_kwh(hass, d)
    assert result == 0.0


async def test_read_pv_tomorrow_sums(hass):
    d = _data()
    d[const.CONF_ENT_PV_TOMORROW] = [_KWH_TOMORROW_0, _KWH_TOMORROW_1]
    hass.states.async_set(d[const.CONF_ENT_PV_TOMORROW][0], "4.5")
    hass.states.async_set(d[const.CONF_ENT_PV_TOMORROW][1], "4.0")
    assert abs(coordinator.read_pv_tomorrow_kwh(hass, d) - 8.5) < 1e-6


async def test_read_pv_tomorrow_all_unavailable_none(hass):
    d = _data()
    d[const.CONF_ENT_PV_TOMORROW] = [_KWH_TOMORROW_0, _KWH_TOMORROW_1]
    hass.states.async_set(d[const.CONF_ENT_PV_TOMORROW][0], "unavailable")
    hass.states.async_set(d[const.CONF_ENT_PV_TOMORROW][1], "unavailable")
    assert coordinator.read_pv_tomorrow_kwh(hass, d) is None


async def test_read_sun_times_parses_and_derives(hass):
    d = _data()
    hass.states.async_set(
        d[const.CONF_ENT_SUN],
        "above_horizon",
        {
            "next_setting": "2026-06-20T20:00:00+00:00",
            "next_rising": "2026-06-21T03:00:00+00:00",
        },
    )
    today_sunset, tom_sunrise, tom_sunset = coordinator.read_sun_times(hass, d)
    assert today_sunset == datetime(2026, 6, 20, 20, 0, tzinfo=timezone.utc)
    assert tom_sunrise == datetime(2026, 6, 21, 3, 0, tzinfo=timezone.utc)
    assert tom_sunset == datetime(2026, 6, 21, 20, 0, tzinfo=timezone.utc)  # +24h


async def test_read_sun_times_none_when_attr_missing(hass):
    d = _data()
    hass.states.async_set(d[const.CONF_ENT_SUN], "above_horizon", {"next_setting": "2026-06-20T20:00:00+00:00"})
    assert coordinator.read_sun_times(hass, d) is None


async def test_read_sun_times_night_branch(hass):
    """When sun is below horizon, next_rising < next_setting, so today_sunset is None."""
    d = _data()
    hass.states.async_set(
        d[const.CONF_ENT_SUN],
        "below_horizon",
        {
            "next_rising": "2026-06-21T03:18:00+00:00",
            "next_setting": "2026-06-21T20:06:00+00:00",
        },
    )

    result = coordinator.read_sun_times(hass, d)

    assert result is not None
    today_sunset, tom_sunrise, tom_sunset = result
    assert today_sunset is None
    assert tom_sunrise == datetime(2026, 6, 21, 3, 18, 0, tzinfo=timezone.utc)
    assert tom_sunset == datetime(2026, 6, 21, 20, 6, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# read_pv_today_arrays / read_pv_tomorrow_arrays
# ---------------------------------------------------------------------------

_PEAK_TODAY_0 = "sensor.power_highest_peak_time_today_2"
_PEAK_TODAY_1 = "sensor.power_highest_peak_time_today_3"
_PEAK_TOMORROW_0 = "sensor.power_highest_peak_time_tomorrow_2"
_PEAK_TOMORROW_1 = "sensor.power_highest_peak_time_tomorrow_3"

_KWH_TODAY_0 = "sensor.pv_today_test_0"
_KWH_TODAY_1 = "sensor.pv_today_test_1"
_KWH_TOMORROW_0 = "sensor.pv_tomorrow_test_0"
_KWH_TOMORROW_1 = "sensor.pv_tomorrow_test_1"

_PEAK_TS_0 = "2026-06-21T09:00:00+00:00"
_PEAK_TS_1 = "2026-06-21T17:00:00+00:00"
_PEAK_DT_0 = datetime(2026, 6, 21, 9, 0, tzinfo=timezone.utc)
_PEAK_DT_1 = datetime(2026, 6, 21, 17, 0, tzinfo=timezone.utc)


async def test_read_pv_today_arrays_pairs_kwh_with_peak(hass):
    """Each available kWh entity is paired with its parsed peak datetime."""
    d = _data()
    d[const.CONF_ENT_PV_TODAY] = [_KWH_TODAY_0, _KWH_TODAY_1]
    d[const.CONF_ENT_PV_PEAK_TODAY] = [_PEAK_TODAY_0, _PEAK_TODAY_1]
    hass.states.async_set(d[const.CONF_ENT_PV_TODAY][0], "3.1")
    hass.states.async_set(d[const.CONF_ENT_PV_TODAY][1], "4.3")
    hass.states.async_set(_PEAK_TODAY_0, _PEAK_TS_0)
    hass.states.async_set(_PEAK_TODAY_1, _PEAK_TS_1)

    result = coordinator.read_pv_today_arrays(hass, d)

    assert result is not None
    assert len(result) == 2
    kwh0, peak0 = result[0]
    kwh1, peak1 = result[1]
    assert abs(kwh0 - 3.1) < 1e-6
    assert abs(kwh1 - 4.3) < 1e-6
    assert peak0 == _PEAK_DT_0
    assert peak1 == _PEAK_DT_1


async def test_read_pv_today_arrays_missing_peak_entity_gives_none(hass):
    """When hass.states.get(peak_entity) returns None, peak_dt is None."""
    d = _data()
    d[const.CONF_ENT_PV_TODAY] = [_KWH_TODAY_0, _KWH_TODAY_1]
    d[const.CONF_ENT_PV_PEAK_TODAY] = [_PEAK_TODAY_0, _PEAK_TODAY_1]
    hass.states.async_set(d[const.CONF_ENT_PV_TODAY][0], "3.1")
    hass.states.async_set(d[const.CONF_ENT_PV_TODAY][1], "4.3")
    # Peak entities NOT set in hass.states → hass.states.get returns None

    result = coordinator.read_pv_today_arrays(hass, d)

    assert result is not None
    assert len(result) == 2
    assert result[0] == (pytest.approx(3.1), None)
    assert result[1] == (pytest.approx(4.3), None)


async def test_read_pv_today_arrays_peak_list_shorter_than_kwh_list(hass):
    """If peak_list has fewer entries than kwh_list, extra arrays get peak_dt=None."""
    d = _data()
    d[const.CONF_ENT_PV_TODAY] = [_KWH_TODAY_0, _KWH_TODAY_1]
    # Only one peak entity in the list
    d[const.CONF_ENT_PV_PEAK_TODAY] = [_PEAK_TODAY_0]
    hass.states.async_set(d[const.CONF_ENT_PV_TODAY][0], "3.1")
    hass.states.async_set(d[const.CONF_ENT_PV_TODAY][1], "4.3")
    hass.states.async_set(_PEAK_TODAY_0, _PEAK_TS_0)

    result = coordinator.read_pv_today_arrays(hass, d)

    assert result is not None
    assert len(result) == 2
    assert result[0][1] == _PEAK_DT_0   # first peak present
    assert result[1][1] is None          # second peak missing → None


async def test_read_pv_today_arrays_bad_peak_state_gives_none(hass):
    """Unknown/unavailable/garbage peak states → peak_dt=None."""
    d = _data()
    d[const.CONF_ENT_PV_TODAY] = [_KWH_TODAY_0, _KWH_TODAY_1]
    d[const.CONF_ENT_PV_PEAK_TODAY] = [_PEAK_TODAY_0, _PEAK_TODAY_1]
    hass.states.async_set(d[const.CONF_ENT_PV_TODAY][0], "3.1")
    hass.states.async_set(d[const.CONF_ENT_PV_TODAY][1], "4.3")
    hass.states.async_set(_PEAK_TODAY_0, "unknown")
    hass.states.async_set(_PEAK_TODAY_1, "not-a-timestamp")

    result = coordinator.read_pv_today_arrays(hass, d)

    assert result is not None
    assert len(result) == 2
    assert result[0][1] is None
    assert result[1][1] is None


async def test_read_pv_today_arrays_unavailable_kwh_skipped(hass):
    """An unavailable kWh array is skipped; the other arrays are still returned."""
    d = _data()
    d[const.CONF_ENT_PV_TODAY] = [_KWH_TODAY_0, _KWH_TODAY_1]
    d[const.CONF_ENT_PV_PEAK_TODAY] = [_PEAK_TODAY_0, _PEAK_TODAY_1]
    hass.states.async_set(d[const.CONF_ENT_PV_TODAY][0], "unavailable")
    hass.states.async_set(d[const.CONF_ENT_PV_TODAY][1], "4.3")
    hass.states.async_set(_PEAK_TODAY_1, _PEAK_TS_1)

    result = coordinator.read_pv_today_arrays(hass, d)

    assert result is not None
    assert len(result) == 1
    assert abs(result[0][0] - 4.3) < 1e-6
    assert result[0][1] == _PEAK_DT_1


async def test_read_pv_today_arrays_all_unavailable_returns_none(hass):
    """When every kWh array is unavailable, return None (failsafe parity)."""
    d = _data()
    d[const.CONF_ENT_PV_TODAY] = [_KWH_TODAY_0, _KWH_TODAY_1]
    hass.states.async_set(d[const.CONF_ENT_PV_TODAY][0], "unavailable")
    hass.states.async_set(d[const.CONF_ENT_PV_TODAY][1], "unavailable")

    result = coordinator.read_pv_today_arrays(hass, d)

    assert result is None


async def test_read_pv_today_arrays_empty_kwh_list_returns_empty(hass):
    """Empty kWh list → [] (not 0.0, not None)."""
    d = _data()
    d[const.CONF_ENT_PV_TODAY] = []

    result = coordinator.read_pv_today_arrays(hass, d)

    assert result == []


async def test_read_pv_today_arrays_get_fallback_no_keyerror(hass):
    """Entry without CONF_ENT_PV_PEAK_TODAY falls back to DEFAULT_ENTITIES via .get."""
    # Seed data as if it came from an old entry that lacks the new peak keys
    d = {k: v for k, v in const.DEFAULT_ENTITIES.items()
         if k != const.CONF_ENT_PV_PEAK_TODAY}
    d[const.CONF_ENT_PV_TODAY] = [_KWH_TODAY_0, _KWH_TODAY_1]
    hass.states.async_set(d[const.CONF_ENT_PV_TODAY][0], "3.1")
    hass.states.async_set(d[const.CONF_ENT_PV_TODAY][1], "4.3")
    # Peak entities not set → None peak_dt is fine

    # Must not raise KeyError
    result = coordinator.read_pv_today_arrays(hass, d)

    assert result is not None
    assert len(result) == 2


async def test_read_pv_today_arrays_sum_parity_all_available(hass):
    """sum(kwh for kwh, _ in read_pv_today_arrays) == read_pv_remaining_kwh."""
    d = _data()
    d[const.CONF_ENT_PV_TODAY] = [_KWH_TODAY_0, _KWH_TODAY_1]
    hass.states.async_set(d[const.CONF_ENT_PV_TODAY][0], "3.1")
    hass.states.async_set(d[const.CONF_ENT_PV_TODAY][1], "4.3")

    arrays = coordinator.read_pv_today_arrays(hass, d)
    scalar = coordinator.read_pv_remaining_kwh(hass, d)

    assert arrays is not None
    assert scalar is not None
    assert abs(sum(kwh for kwh, _ in arrays) - scalar) < 1e-6


async def test_read_pv_today_arrays_sum_parity_one_unavailable(hass):
    """Sum parity holds when one array is unavailable (skipped in both readers)."""
    d = _data()
    d[const.CONF_ENT_PV_TODAY] = [_KWH_TODAY_0, _KWH_TODAY_1]
    hass.states.async_set(d[const.CONF_ENT_PV_TODAY][0], "unavailable")
    hass.states.async_set(d[const.CONF_ENT_PV_TODAY][1], "4.3")

    arrays = coordinator.read_pv_today_arrays(hass, d)
    scalar = coordinator.read_pv_remaining_kwh(hass, d)

    assert arrays is not None
    assert scalar is not None
    assert abs(sum(kwh for kwh, _ in arrays) - scalar) < 1e-6


# --- tomorrow mirrors ---

async def test_read_pv_tomorrow_arrays_pairs_kwh_with_peak(hass):
    """read_pv_tomorrow_arrays pairs kWh with parsed peak datetimes."""
    d = _data()
    d[const.CONF_ENT_PV_TOMORROW] = [_KWH_TOMORROW_0, _KWH_TOMORROW_1]
    d[const.CONF_ENT_PV_PEAK_TOMORROW] = [_PEAK_TOMORROW_0, _PEAK_TOMORROW_1]
    hass.states.async_set(d[const.CONF_ENT_PV_TOMORROW][0], "4.5")
    hass.states.async_set(d[const.CONF_ENT_PV_TOMORROW][1], "3.0")
    hass.states.async_set(_PEAK_TOMORROW_0, _PEAK_TS_0)
    hass.states.async_set(_PEAK_TOMORROW_1, _PEAK_TS_1)

    result = coordinator.read_pv_tomorrow_arrays(hass, d)

    assert result is not None
    assert len(result) == 2
    assert abs(result[0][0] - 4.5) < 1e-6
    assert abs(result[1][0] - 3.0) < 1e-6
    assert result[0][1] == _PEAK_DT_0
    assert result[1][1] == _PEAK_DT_1


async def test_read_pv_tomorrow_arrays_all_unavailable_returns_none(hass):
    """When every tomorrow kWh array is unavailable, return None."""
    d = _data()
    d[const.CONF_ENT_PV_TOMORROW] = [_KWH_TOMORROW_0, _KWH_TOMORROW_1]
    hass.states.async_set(d[const.CONF_ENT_PV_TOMORROW][0], "unavailable")
    hass.states.async_set(d[const.CONF_ENT_PV_TOMORROW][1], "unavailable")

    result = coordinator.read_pv_tomorrow_arrays(hass, d)

    assert result is None


async def test_read_pv_tomorrow_arrays_get_fallback_no_keyerror(hass):
    """Entry without CONF_ENT_PV_PEAK_TOMORROW falls back to DEFAULT_ENTITIES via .get."""
    d = {k: v for k, v in const.DEFAULT_ENTITIES.items()
         if k != const.CONF_ENT_PV_PEAK_TOMORROW}
    d[const.CONF_ENT_PV_TOMORROW] = [_KWH_TOMORROW_0, _KWH_TOMORROW_1]
    hass.states.async_set(d[const.CONF_ENT_PV_TOMORROW][0], "4.5")
    hass.states.async_set(d[const.CONF_ENT_PV_TOMORROW][1], "3.0")

    result = coordinator.read_pv_tomorrow_arrays(hass, d)

    assert result is not None
    assert len(result) == 2


# ---------------------------------------------------------------------------
# read_hourly_weather_forecast + get_forecast_for_hour (P1-T4)
# ---------------------------------------------------------------------------

_ENTITY_WX = const.DEFAULT_ENT_WEATHER_FORECAST  # "weather.knmi_home"

_FORECAST_PAYLOAD = {
    _ENTITY_WX: {
        "forecast": [
            {
                "datetime": "2026-06-22T09:00:00+00:00",
                "temperature": 17.0,
                "cloud_coverage": 20.0,
                "humidity": 60.0,
                "wind_speed": 8.0,
            },
            {
                "datetime": "2026-06-22T10:00:00+00:00",
                "temperature": 18.5,
                "cloud_coverage": 30.0,
                "humidity": 65.0,
                "wind_speed": 12.3,
            },
            {
                "datetime": "2026-06-22T11:00:00+00:00",
                "temperature": 19.0,
                "cloud_coverage": 25.0,
                "humidity": 62.0,
                "wind_speed": 10.0,
            },
        ]
    }
}


async def test_read_hourly_weather_forecast_maps_fields_and_parses_datetime(hass):
    """Maps HA keys temperature/cloud_coverage/humidity/wind_speed to the 4 column names;
    datetime is parsed to a UTC-aware datetime object."""
    d = _data()
    with patch.object(ServiceRegistry, "async_call", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _FORECAST_PAYLOAD

        result = await coordinator.read_hourly_weather_forecast(hass, d)

    assert len(result) == 3

    h0 = result[0]
    assert h0["datetime"] == datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
    assert h0["temp_forecast"] == pytest.approx(17.0)
    assert h0["cloud_cover"] == pytest.approx(20.0)
    assert h0["humidity"] == pytest.approx(60.0)
    assert h0["wind_speed"] == pytest.approx(8.0)

    h1 = result[1]
    assert h1["datetime"] == datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc)
    assert h1["temp_forecast"] == pytest.approx(18.5)
    assert h1["cloud_cover"] == pytest.approx(30.0)
    assert h1["humidity"] == pytest.approx(65.0)
    assert h1["wind_speed"] == pytest.approx(12.3)


async def test_read_hourly_weather_forecast_service_called_with_correct_args(hass):
    """Reader calls weather.get_forecasts with entity_id and type=hourly."""
    d = _data()
    with patch.object(ServiceRegistry, "async_call", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _FORECAST_PAYLOAD

        await coordinator.read_hourly_weather_forecast(hass, d)

        mock_call.assert_called_once_with(
            "weather",
            "get_forecasts",
            {"entity_id": _ENTITY_WX, "type": "hourly"},
            blocking=True,
            return_response=True,
        )


async def test_read_hourly_weather_forecast_service_raises_returns_empty(hass):
    """When the service raises any exception, the reader returns [] without propagating."""
    d = _data()
    with patch.object(ServiceRegistry, "async_call", new_callable=AsyncMock) as mock_call:
        mock_call.side_effect = Exception("entity unavailable")

        result = await coordinator.read_hourly_weather_forecast(hass, d)

    assert result == []


async def test_read_hourly_weather_forecast_empty_response_dict_returns_empty(hass):
    """Service returns {} → reader returns []."""
    d = _data()
    with patch.object(ServiceRegistry, "async_call", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = {}

        result = await coordinator.read_hourly_weather_forecast(hass, d)

    assert result == []


async def test_read_hourly_weather_forecast_entity_absent_from_response_returns_empty(hass):
    """Response omits the configured entity key → reader returns []."""
    d = _data()
    with patch.object(ServiceRegistry, "async_call", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = {"weather.some_other_entity": {"forecast": []}}

        result = await coordinator.read_hourly_weather_forecast(hass, d)

    assert result == []


async def test_read_hourly_weather_forecast_empty_forecast_list_returns_empty(hass):
    """Service returns an empty forecast list → reader returns []."""
    d = _data()
    with patch.object(ServiceRegistry, "async_call", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = {_ENTITY_WX: {"forecast": []}}

        result = await coordinator.read_hourly_weather_forecast(hass, d)

    assert result == []


async def test_read_hourly_weather_forecast_missing_fields_are_none_independently(hass):
    """Fields absent from a forecast item are returned as None; present fields are numeric.
    Each of the 4 fields is independently optional — no all-or-nothing failure."""
    d = _data()
    with patch.object(ServiceRegistry, "async_call", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = {
            _ENTITY_WX: {
                "forecast": [
                    {
                        "datetime": "2026-06-22T10:00:00+00:00",
                        "temperature": 18.5,
                        # cloud_coverage, humidity, wind_speed intentionally absent
                    },
                ]
            }
        }

        result = await coordinator.read_hourly_weather_forecast(hass, d)

    assert len(result) == 1
    h = result[0]
    assert h["temp_forecast"] == pytest.approx(18.5)
    assert h["cloud_cover"] is None
    assert h["humidity"] is None
    assert h["wind_speed"] is None


async def test_read_hourly_weather_forecast_all_fields_missing_returns_none_fields(hass):
    """An item with only datetime (no numeric fields) → entry with all-None values."""
    d = _data()
    with patch.object(ServiceRegistry, "async_call", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = {
            _ENTITY_WX: {
                "forecast": [
                    {"datetime": "2026-06-22T10:00:00+00:00"},
                ]
            }
        }

        result = await coordinator.read_hourly_weather_forecast(hass, d)

    assert len(result) == 1
    h = result[0]
    assert h["temp_forecast"] is None
    assert h["cloud_cover"] is None
    assert h["humidity"] is None
    assert h["wind_speed"] is None


async def test_read_hourly_weather_forecast_uses_default_entity_when_key_absent(hass):
    """data dict without CONF_ENT_WEATHER_FORECAST falls back to DEFAULT_ENT_WEATHER_FORECAST."""
    d = {k: v for k, v in _data().items() if k != const.CONF_ENT_WEATHER_FORECAST}
    with patch.object(ServiceRegistry, "async_call", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = _FORECAST_PAYLOAD

        result = await coordinator.read_hourly_weather_forecast(hass, d)

    # Should still succeed — default entity == _ENTITY_WX == key in _FORECAST_PAYLOAD
    assert len(result) == 3


async def test_read_hourly_weather_forecast_skips_items_without_datetime(hass):
    """Items missing the datetime key are silently skipped."""
    d = _data()
    with patch.object(ServiceRegistry, "async_call", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = {
            _ENTITY_WX: {
                "forecast": [
                    {"temperature": 18.5},          # no datetime → skip
                    {
                        "datetime": "2026-06-22T11:00:00+00:00",
                        "temperature": 19.0,
                    },
                ]
            }
        }

        result = await coordinator.read_hourly_weather_forecast(hass, d)

    assert len(result) == 1
    assert result[0]["datetime"] == datetime(2026, 6, 22, 11, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# get_forecast_for_hour
# ---------------------------------------------------------------------------

def _make_entries(*hours_utc: int) -> list[dict]:
    """Build minimal forecast entries for the given UTC hours on 2026-06-22."""
    return [
        {
            "datetime": datetime(2026, 6, 22, h, 0, tzinfo=timezone.utc),
            "temp_forecast": float(h),
            "cloud_cover": None,
            "humidity": None,
            "wind_speed": None,
        }
        for h in hours_utc
    ]


def test_get_forecast_for_hour_returns_nearest_entry():
    """Returns the entry whose datetime is closest to target_dt."""
    entries = _make_entries(9, 10, 11)
    # 10:15 is 15 min from 10:00, 45 min from 11:00 → nearest is 10:00
    target = datetime(2026, 6, 22, 10, 15, tzinfo=timezone.utc)

    result = coordinator.get_forecast_for_hour(entries, target)

    assert result is not None
    assert result["temp_forecast"] == pytest.approx(10.0)


def test_get_forecast_for_hour_exact_match():
    """Returns the entry when target matches exactly."""
    entries = _make_entries(9, 10, 11)
    target = datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc)

    result = coordinator.get_forecast_for_hour(entries, target)

    assert result is not None
    assert result["temp_forecast"] == pytest.approx(10.0)


def test_get_forecast_for_hour_empty_returns_none():
    """Empty forecast list → None."""
    result = coordinator.get_forecast_for_hour([], datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc))
    assert result is None
