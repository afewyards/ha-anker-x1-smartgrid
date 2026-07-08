from datetime import timezone
from custom_components.anker_x1_smartgrid import parsers


def test_parse_price_curve_scales_and_sorts():
    attr = [
        {"datetime": "2026-06-20T13:00:00.000000Z", "electricity_price": 1471074},
        {"datetime": "2026-06-20T12:00:00.000000Z", "electricity_price": 1300000},
    ]
    slots = parsers.parse_price_curve(attr)
    assert len(slots) == 2
    assert slots[0].start.hour == 12  # sorted ascending
    assert abs(slots[1].price - 0.1471074) < 1e-9
    assert slots[0].start.tzinfo is not None


def test_parse_price_curve_skips_malformed():
    attr = [
        {"datetime": "bad", "electricity_price": 1},
        {"electricity_price": 1},
        {"datetime": "2026-06-20T12:00:00.000000Z"},
        {"datetime": "2026-06-20T12:00:00.000000Z", "electricity_price": 1300000},
    ]
    slots = parsers.parse_price_curve(attr)
    assert len(slots) == 1


def test_parse_price_curve_empty():
    assert parsers.parse_price_curve([]) == []
    assert parsers.parse_price_curve(None) == []


def test_parse_price_curve_drops_non_finite_prices():
    attr = [
        {"datetime": "2026-07-02T10:00:00Z", "electricity_price": "NaN"},
        {"datetime": "2026-07-02T11:00:00Z", "electricity_price": "Infinity"},
        {"datetime": "2026-07-02T12:00:00Z", "electricity_price": 2500000},
    ]
    slots = parsers.parse_price_curve(attr)
    assert len(slots) == 1 and slots[0].price == 0.25
