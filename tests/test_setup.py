"""Test setup and unload of the Anker X1 SmartGrid integration."""
from unittest.mock import MagicMock

from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.anker_x1_smartgrid as x1_init
from custom_components.anker_x1_smartgrid.const import DOMAIN, DEFAULT_ENTITIES
from custom_components.anker_x1_smartgrid.recorder import DataRecorder
from tests.conftest import ANKER_TEST_ENTITIES


def _make_entry(hass, extra=None, options=None):
    data = {**DEFAULT_ENTITIES, **ANKER_TEST_ENTITIES}
    data.update({"soc_target": 97.0})
    data.update(extra or {})
    entry = MockConfigEntry(domain=DOMAIN, data=data, options=options or {})
    entry.add_to_hass(hass)
    # provide minimal states so a tick doesn't crash
    hass.states.async_set(data["ent_soc"], "50")
    for e in data["ent_phase"]:
        hass.states.async_set(e, "0")
    return entry


async def test_setup_and_unload(hass):
    entry = _make_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert "controller" in hass.data[DOMAIN][entry.entry_id]
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_setup_failure_cleans_up_recorder_and_timer(hass, monkeypatch):
    """A late setup-step failure must not leak the recorder or the tick timer."""
    entry = _make_entry(hass)

    cancel_mock = MagicMock()
    monkeypatch.setattr(x1_init, "async_track_time_interval", lambda *a, **k: cancel_mock)

    closed = []
    orig_close = DataRecorder.close

    def _spy_close(self):
        closed.append(self)
        return orig_close(self)

    monkeypatch.setattr(DataRecorder, "close", _spy_close)

    async def _boom(*_a, **_kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", _boom)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.entry_id not in hass.data.get(DOMAIN, {})
    cancel_mock.assert_called_once()
    assert len(closed) == 1


async def test_setup_forces_device_derived_limits(hass):
    """Stale stored max_charge_w/max_export_w must lose to the nominal consts."""
    entry = _make_entry(
        hass,
        extra={"max_charge_w": 3000.0},
        options={"max_export_w": 2000.0},
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    controller = hass.data[DOMAIN][entry.entry_id]["controller"]
    assert controller.cfg.max_charge_w == 6000.0
    assert controller.cfg.max_export_w == 6000.0
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
