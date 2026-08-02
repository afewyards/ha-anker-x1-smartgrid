from datetime import UTC, datetime

import pytest

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


def test_parse_price_curve_non_list_attribute_returns_empty():
    """A truthy non-list attribute (e.g. `prices: 5`) must not raise — it's not a curve."""
    assert parsers.parse_price_curve(5) == []
    assert parsers.parse_price_curve("prices") == []
    assert parsers.parse_price_curve({"from": "x", "price": 0.1}) == []


def test_parse_price_curve_drops_non_finite_prices():
    attr = [
        {"datetime": "2026-07-02T10:00:00Z", "electricity_price": "NaN"},
        {"datetime": "2026-07-02T11:00:00Z", "electricity_price": "Infinity"},
        {"datetime": "2026-07-02T12:00:00Z", "electricity_price": 2500000},
    ]
    slots = parsers.parse_price_curve(attr)
    assert len(slots) == 1 and slots[0].price == 0.25


def test_parse_price_curve_zonneplan_regression_durations_and_scale():
    """Zonneplan decode is byte-identical: ÷PRICE_SCALE, 60-min derived durations."""
    attr = [{"datetime": f"2026-06-20T{h:02d}:00:00.000000Z", "electricity_price": 1000000 + h} for h in range(6)]
    slots = parsers.parse_price_curve(attr)
    assert [s.duration_min for s in slots] == [60.0] * 6
    assert slots[3].price == pytest.approx(1000003 / 1e7)


def test_parse_price_curve_frank_shape():
    """Frank Energie: {from, till, price} — tz-aware ISO, plain EUR/kWh, no scaling."""
    attr = [
        {"from": "2026-07-31T10:00:00+02:00", "till": "2026-07-31T10:15:00+02:00", "price": 0.2461},
        {"from": "2026-07-31T10:15:00+02:00", "till": "2026-07-31T10:30:00+02:00", "price": 0.2312},
    ]
    slots = parsers.parse_price_curve(attr)
    assert len(slots) == 2
    assert slots[0].price == pytest.approx(0.2461)
    assert slots[1].price == pytest.approx(0.2312)
    assert slots[0].start == datetime(2026, 7, 31, 8, 0, tzinfo=UTC)
    assert [s.duration_min for s in slots] == [15.0, 15.0]


def test_parse_price_curve_frank_dedupes_doubled_entries():
    """The integration publishes every slot exactly twice; durations must stay 15."""
    raw = [
        {"from": "2026-07-31T10:00:00+02:00", "price": 0.20},
        {"from": "2026-07-31T10:15:00+02:00", "price": 0.21},
        {"from": "2026-07-31T10:30:00+02:00", "price": 0.22},
    ]
    slots = parsers.parse_price_curve(raw + raw)
    assert [s.price for s in slots] == pytest.approx([0.20, 0.21, 0.22])
    assert [s.duration_min for s in slots] == [15.0, 15.0, 15.0]


def test_parse_price_curve_dedupes_zonneplan_duplicates():
    """Dedupe is generic — it protects the Zonneplan path too."""
    attr = [
        {"datetime": "2026-06-20T12:00:00Z", "electricity_price": 1300000},
        {"datetime": "2026-06-20T12:00:00Z", "electricity_price": 1300000},
        {"datetime": "2026-06-20T13:00:00Z", "electricity_price": 1400000},
    ]
    slots = parsers.parse_price_curve(attr)
    assert len(slots) == 2
    assert [s.duration_min for s in slots] == [60.0, 60.0]


def test_parse_price_curve_frank_skips_malformed_and_non_finite():
    attr = [
        {"from": "bad", "price": 0.2},
        {"from": "2026-07-31T10:00:00+02:00", "price": "NaN"},
        {"from": "2026-07-31T11:00:00+02:00", "price": "Infinity"},
        {"from": "2026-07-31T12:00:00+02:00"},
        {"price": 0.3},
        "not-a-dict",
        {"from": "2026-07-31T13:00:00+02:00", "price": 0.25},
    ]
    slots = parsers.parse_price_curve(attr)
    assert len(slots) == 1 and slots[0].price == pytest.approx(0.25)


def test_parse_price_curve_mixed_shapes_both_decoded():
    """A junk-mixed list keeps every entry it can decode, regardless of shape."""
    attr = [
        {"datetime": "2026-07-31T08:00:00Z", "electricity_price": 2000000},
        {"from": "2026-07-31T09:00:00Z", "price": 0.30},
        {"nonsense": 1},
    ]
    slots = parsers.parse_price_curve(attr)
    assert [s.price for s in slots] == pytest.approx([0.20, 0.30])


def test_parse_price_curve_quarter_hourly_shape():
    """Zonneplan quarter-hourly: {start_date, price_tax_included:{amount}} — scaled, sorted."""
    attr = [
        {
            "start_date": "2026-07-31T07:15:00+00:00",
            "end_date": "2026-07-31T07:30:00+00:00",
            "price_tax_included": {"amount": 2136119},
            "price_tax_excluded": {"amount": 1500000},
            "sustainability_score": {"permille": 400},
        },
        {
            "start_date": "2026-07-31T07:00:00+00:00",
            "end_date": "2026-07-31T07:15:00+00:00",
            "price_tax_included": {"amount": 3244600},
            "price_tax_excluded": {"amount": 2136119},
            "sustainability_score": {"permille": 396},
        },
        {
            "start_date": "2026-07-31T07:30:00+00:00",
            "end_date": "2026-07-31T07:45:00+00:00",
            "price_tax_included": {"amount": 2000000},
            "price_tax_excluded": {"amount": 1400000},
            "sustainability_score": {"permille": 410},
        },
    ]
    slots = parsers.parse_price_curve(attr)
    assert len(slots) == 3
    assert slots[0].start == datetime(2026, 7, 31, 7, 0, tzinfo=UTC)  # sorted ascending
    assert slots[0].price == pytest.approx(0.32446)
    assert slots[1].price == pytest.approx(0.2136119)
    assert slots[2].price == pytest.approx(0.20)
    assert [s.duration_min for s in slots] == [15.0, 15.0, 15.0]


def test_parse_price_curve_quarter_hourly_skips_malformed_nested_price():
    """A malformed/missing/non-dict/non-finite nested price is skipped; valid siblings survive."""
    attr = [
        {"start_date": "2026-07-31T07:00:00+00:00"},  # price_tax_included missing entirely
        {"start_date": "2026-07-31T07:15:00+00:00", "price_tax_included": {}},  # no amount
        {"start_date": "2026-07-31T07:30:00+00:00", "price_tax_included": "3244600"},  # non-dict
        {"start_date": "2026-07-31T07:45:00+00:00", "price_tax_included": {"amount": "NaN"}},  # non-finite
        {"start_date": "2026-07-31T08:00:00+00:00", "price_tax_included": {"amount": 2500000}},  # valid
    ]
    slots = parsers.parse_price_curve(attr)
    assert len(slots) == 1
    assert slots[0].price == pytest.approx(0.25)


def test_parse_price_curve_mixed_all_three_shapes_decoded():
    """A list mixing legacy Zonneplan, Frank, and quarter-hourly entries decodes all three."""
    attr = [
        {"datetime": "2026-07-31T08:00:00Z", "electricity_price": 2000000},
        {"from": "2026-07-31T09:00:00Z", "price": 0.30},
        {"start_date": "2026-07-31T10:00:00Z", "price_tax_included": {"amount": 3200000}},
    ]
    slots = parsers.parse_price_curve(attr)
    assert [s.price for s in slots] == pytest.approx([0.20, 0.30, 0.32])
