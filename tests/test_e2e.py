"""End-to-end dry-run integration tests: seed live HA states, call tick(), assert decision."""
from datetime import timedelta

from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.anker_x1_smartgrid.const import DOMAIN, DEFAULT_ENTITIES
from tests.conftest import ANKER_TEST_ENTITIES


def _seed_states(hass, data, soc, pv_remaining, cheap_now=True):
    # Inject an explicit 2-element PV-today list so e2e tests don't depend on
    # DEFAULT_ENTITIES cardinality (became 1-element in commit 8d3936c).
    # Mirrors the pattern used in test_coordinator after that commit.
    data["ent_pv_today"] = ["sensor.pv_e2e_today_0", "sensor.pv_e2e_today_1"]
    hass.states.async_set(data["ent_soc"], str(soc))
    for e in data["ent_phase"]:
        hass.states.async_set(e, "0")
    hass.states.async_set(data["ent_pv_today"][0], str(pv_remaining / 2))
    hass.states.async_set(data["ent_pv_today"][1], str(pv_remaining / 2))
    now = dt_util.utcnow()
    # Truncate to the current hour so slot[0] is definitively <= any tick() `now`
    # within the same hour — mirrors real Zonneplan data that uses round-hour starts.
    hour0 = now.replace(minute=0, second=0, microsecond=0)
    price = 0.05 if cheap_now else 0.40
    forecast = [
        {"datetime": (hour0 + timedelta(hours=i)).isoformat(),
         "electricity_price": int((price if i == 0 else 0.40) * 1e7)}
        for i in range(6)
    ]
    hass.states.async_set(data["ent_price"], str(price), {"forecast": forecast})
    hass.states.async_set(
        data["ent_sun"], "above_horizon",
        {"next_setting": (now + timedelta(hours=5)).isoformat()},
    )


async def test_e2e_low_soc_charges(hass):
    """Economic-only charging at firmware floor: cheap slot → FORCING.

    With the economic-only redesign (2026-06-25) all survival-override / force-charge
    paths were removed.  Charging decisions are driven purely by the DP's price
    comparison: the controller charges when it is economically beneficial, not to
    satisfy a software-enforced survival reserve.

    At soc=5.0 == soc_floor=5.0 with cheap_now=True (0.05 €/kWh now, 0.40 later):
    - ceiling = peak × round_trip_eff = 0.40 × 0.85 = 0.34 > 0.05 → current slot
      is below the ceiling gate (chargeable).
    - The DP selects the cheap current slot economically (large deficit, clear
      price spread) → state FORCING, setpoint_w < 0 (charging).

    This encodes the economic-only contract: the controller charges at soc=floor
    when the price is good, not because of any survival obligation.
    """
    data = {**DEFAULT_ENTITIES, **ANKER_TEST_ENTITIES}
    data.update({"soc_target": 100.0, "eta_charge": 1.0, "min_dwell_min": 0})
    entry = MockConfigEntry(domain=DOMAIN, data=data)
    entry.add_to_hass(hass)
    # soc=5.0 == soc_floor=5.0 (DEFAULT_SOC_FLOOR lowered 10→5 in economic-only
    # redesign).  cheap_now=True: current slot 0.05 €/kWh, rest 0.40 €/kWh.
    # DP economically selects cheap slot → FORCING.
    _seed_states(hass, data, soc=5.0, pv_remaining=0.0, cheap_now=True)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    controller = hass.data[DOMAIN][entry.entry_id]["controller"]
    controller.enabled = True  # fresh install starts disabled (M5); e2e needs enabled
    status = await controller.tick()
    assert status["reason"] == "ok"
    # Economic-only: cheap slot (0.05) below ceiling (0.34) → DP selects it → FORCING.
    assert status["state"] == "forcing"
    assert status["setpoint_w"] < 0.0  # negative = charging


async def test_e2e_high_soc_releases(hass):
    data = {**DEFAULT_ENTITIES, **ANKER_TEST_ENTITIES}
    data.update({"soc_target": 97.0, "min_dwell_min": 0})
    entry = MockConfigEntry(domain=DOMAIN, data=data)
    entry.add_to_hass(hass)
    _seed_states(hass, data, soc=96.0, pv_remaining=0.0, cheap_now=True)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    controller = hass.data[DOMAIN][entry.entry_id]["controller"]
    status = await controller.tick()
    assert status["state"] == "passive"
    assert status["setpoint_w"] == 0.0
