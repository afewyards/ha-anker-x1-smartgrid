"""TDD: build_charge_mask per-hour trough gate (S1 fix)."""
from __future__ import annotations

from custom_components.anker_x1_smartgrid.optimize import build_charge_mask


def test_per_hour_trough_gates_each_hour_independently():
    price = [0.30, 0.20, 0.10, 0.15]
    ceiling = 0.40
    # h0/h1 see a 0.10 trough in their look-back; h3's window no longer includes it.
    trough = [0.10, 0.10, 0.10, 0.15]
    mask = build_charge_mask(price, ceiling, price_band=0.005, trough=trough)
    # h0:0.30<=0.105?N  h1:0.20<=0.105?N  h2:0.10<=0.105?Y  h3:0.15<=0.155?Y
    assert mask == [False, False, True, True]


def test_per_hour_trough_none_entry_fails_closed():
    mask = build_charge_mask([0.10, 0.10], 0.40, price_band=0.005, trough=[0.10, None])
    assert mask == [True, False]


def test_trough_none_preserves_scalar_window_min_behaviour():
    # trough omitted → legacy scalar path unchanged.
    price = [0.20, 0.13, 0.30]
    mask = build_charge_mask(price, 0.40, price_band=0.005, window_min=0.13)
    assert mask == [False, True, False]
