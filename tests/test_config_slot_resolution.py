from datetime import datetime, timedelta, timezone, UTC

from custom_components.anker_x1_smartgrid import const, resolution
from custom_components.anker_x1_smartgrid.models import Config, PriceSlot


def test_default_slot_resolution_is_auto():
    assert Config().slot_resolution == "auto"
    assert const.DEFAULT_SLOT_RESOLUTION == "auto"


def test_config_from_dict_reads_override():
    cfg = Config.from_dict({const.CONF_SLOT_RESOLUTION: "15"})
    assert cfg.slot_resolution == "15"


def test_auto_resolves_from_live_slots():
    base = datetime(2026, 8, 1, tzinfo=UTC)
    q = [PriceSlot(base + timedelta(minutes=15 * i), 0.2) for i in range(96)]
    assert resolution.resolve_slot_minutes(q, Config().slot_resolution) == 15
