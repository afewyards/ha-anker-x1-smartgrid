from datetime import datetime, timezone

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.anker_x1_smartgrid.const import DOMAIN, DEFAULT_ENTITIES
from custom_components.anker_x1_smartgrid.models import PlanState
from tests.conftest import ANKER_TEST_ENTITIES

NOW = datetime(2026, 6, 29, 12, 0, tzinfo=timezone.utc)


async def test_fresh_install_starts_disabled(hass):
    """No persisted store → controller.enabled is False after setup."""
    data = {**DEFAULT_ENTITIES, **ANKER_TEST_ENTITIES}
    entry = MockConfigEntry(domain=DOMAIN, data=data)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    controller = hass.data[DOMAIN][entry.entry_id]["controller"]
    assert controller.enabled is False


def _bare_controller():
    from custom_components.anker_x1_smartgrid.controller import Controller
    ctl = Controller.__new__(Controller)
    ctl.enabled = True  # construction default
    return ctl


def test_restore_payload_missing_enabled_defaults_off():
    ctl = _bare_controller()
    ctl.restore({"plan": PlanState.initial(NOW).to_dict()})  # has plan, no "enabled"
    assert ctl.enabled is False


def test_restore_preserves_enabled_true():
    ctl = _bare_controller()
    ctl.restore({"plan": PlanState.initial(NOW).to_dict(), "enabled": True})
    assert ctl.enabled is True
