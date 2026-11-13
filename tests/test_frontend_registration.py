"""The power card is served by the integration and added to every frontend page."""

from urllib.parse import urlsplit

from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL
from homeassistant.setup import async_setup_component

from custom_components.anker_x1_smartgrid import CARD_URL, card_module_url
from custom_components.anker_x1_smartgrid.const import DOMAIN


async def test_card_module_is_registered_and_served(hass):
    # The test venv has no hass_frontend package, so stand in for the frontend's URL manager.
    hass.config.components.add("frontend")
    hass.data[DATA_EXTRA_MODULE_URL] = set()

    assert await async_setup_component(hass, DOMAIN, {})

    (url,) = hass.data[DATA_EXTRA_MODULE_URL]
    assert urlsplit(url).path == CARD_URL
    # hass_client is not used: its aiohttp session leaves a pycares shutdown thread the leak check rejects.
    assert any(r.method == "GET" and r.resource.canonical == CARD_URL for r in hass.http.app.router.routes())


async def test_setup_without_frontend_skips_the_module_url(hass):
    assert await async_setup_component(hass, DOMAIN, {})
    assert DATA_EXTRA_MODULE_URL not in hass.data


def test_module_url_changes_with_file_content():
    a, b = card_module_url(b"one"), card_module_url(b"two")
    assert a != b
    assert a == card_module_url(b"one")
    assert urlsplit(a).query.startswith("v=")


async def test_setup_survives_a_missing_card_file(hass, monkeypatch, caplog):
    import custom_components.anker_x1_smartgrid as integration

    monkeypatch.setattr(integration, "_CARD_PATH", integration._CARD_PATH.with_name("missing.js"))
    hass.config.components.add("frontend")
    hass.data[DATA_EXTRA_MODULE_URL] = set()

    assert await async_setup_component(hass, DOMAIN, {})
    assert hass.data[DATA_EXTRA_MODULE_URL] == set()
    assert "power card" in caplog.text
