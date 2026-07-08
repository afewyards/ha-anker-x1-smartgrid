"""TDD tests for windowed_trough_prices (charge-band look-back, mirror of peak)."""
from __future__ import annotations

from custom_components.anker_x1_smartgrid.regret import windowed_trough_prices


def test_lookback_zero_is_suffix_min():
    p = [0.6, 0.2, 0.4, 0.8]
    assert windowed_trough_prices(p, 0) == [0.2, 0.2, 0.4, 0.8]


def test_lookback_remembers_recent_trough():
    p = [0.2, 0.4, 0.6, 0.8]
    # h0 trough 0.2 is remembered 2h forward: h1,h2 see it; h3 falls back to suffix[1]=0.4
    assert windowed_trough_prices(p, 2) == [0.2, 0.2, 0.2, 0.4]


def test_empty():
    assert windowed_trough_prices([], 3) == []


def test_windowed_trough_per_day_does_not_leak_across_days():
    p = [0.30] * 48
    p[3] = 0.10    # day1 trough
    p[27] = 0.05   # day2 (cheaper) trough
    day = [h // 24 for h in range(48)]
    out = windowed_trough_prices(p, 4, day_index=day)
    assert out[0] == 0.10    # day1 hour judged vs day1's own trough, not day2's 0.05
    assert out[3] == 0.10
    assert out[27] == 0.05


def test_windowed_trough_no_day_index_is_legacy_global():
    p = [0.30] * 48
    p[3] = 0.10
    p[27] = 0.05
    out = windowed_trough_prices(p, 4)
    assert out[0] == 0.05    # legacy global suffix-min leaks day2's cheaper trough
