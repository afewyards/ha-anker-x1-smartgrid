"""async_setup_entry re-resolves the Anker device in-memory (self-heal)."""
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.anker_x1_smartgrid import const
from tests.test_anker_resolver import _register_anker_device


async def test_setup_applies_anker_resolution_without_persisting(hass):
    device_id, _ = _register_anker_device(hass, capacity_state="15.0")
    entry = MockConfigEntry(
        domain=const.DOMAIN,
        data={
            **const.DEFAULT_ENTITIES,
            const.CONF_ANKER_DEVICE: device_id,
            const.CONF_CAPACITY_KWH: 10.0,        # stale; device reports 15.0
            const.CONF_ENT_SOC: "sensor.stale_soc",
        },
    )
    entry.add_to_hass(hass)
    data_before = dict(entry.data)
    options_before = dict(entry.options)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    controller = hass.data[const.DOMAIN][entry.entry_id]["controller"]
    # capacity flowed through Config.from_dict(data) → fresh device value
    assert controller.cfg.capacity_kwh == 15.0
    # persisted stores untouched → in-memory only, no async_update_entry, no loop
    assert dict(entry.data) == data_before
    assert dict(entry.options) == options_before

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
