"""Pure static-tariff synthesis (tariff.py)."""
import pytest

from custom_components.anker_x1_smartgrid import tariff


def test_parse_offpeak_ranges_empty():
    assert tariff.parse_offpeak_ranges("") == []
    assert tariff.parse_offpeak_ranges(None) == []
    assert tariff.parse_offpeak_ranges("  ") == []


def test_parse_offpeak_ranges_single():
    assert tariff.parse_offpeak_ranges("01:30-07:30") == [(90, 450)]


def test_parse_offpeak_ranges_multi_and_midnight():
    assert tariff.parse_offpeak_ranges("22:00-06:00, 12:30-14:30") == [(1320, 360), (750, 870)]


@pytest.mark.parametrize("bad", [
    "7-8", "25:00-01:00", "01:60-02:00", "0100-0200",
    "01:00_02:00", "01:00-", "01:00-02:00-03:00", "aa:bb-cc:dd",
])
def test_parse_offpeak_ranges_invalid_raises(bad):
    with pytest.raises(ValueError):
        tariff.parse_offpeak_ranges(bad)


def test_in_offpeak_normal_half_open():
    r = [(90, 450)]  # 01:30-07:30
    assert tariff._in_offpeak(90, r) is True
    assert tariff._in_offpeak(449, r) is True
    assert tariff._in_offpeak(450, r) is False   # end exclusive
    assert tariff._in_offpeak(89, r) is False


def test_in_offpeak_midnight_span():
    r = [(1320, 360)]  # 22:00-06:00
    assert tariff._in_offpeak(1350, r) is True   # 22:30
    assert tariff._in_offpeak(0, r) is True       # 00:00
    assert tariff._in_offpeak(359, r) is True     # 05:59
    assert tariff._in_offpeak(360, r) is False    # 06:00
    assert tariff._in_offpeak(700, r) is False


def test_resolution_minutes_flat_is_60():
    assert tariff._resolution_minutes([]) == 60


def test_resolution_minutes_on_hour_is_60():
    assert tariff._resolution_minutes([(60, 420)]) == 60   # 01:00-07:00


def test_resolution_minutes_half_hour_is_30():
    assert tariff._resolution_minutes([(90, 450)]) == 30   # 01:30-07:30


def test_resolution_minutes_quarter_is_15():
    assert tariff._resolution_minutes([(75, 435)]) == 15   # 01:15-07:15


def test_resolution_minutes_floored_at_15():
    assert tariff._resolution_minutes([(65, 125)]) == 15   # :05 → gcd 5 → floor 15
