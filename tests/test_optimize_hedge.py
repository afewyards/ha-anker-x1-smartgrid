"""TDD tests for optimize_grid hedge_drain_kwh param (T3 — MAJOR-1).

Tests the new keyword-only ``hedge_drain_kwh`` parameter and verifies that
the backtrack sums the stored per-step floor-import (MAJOR-1) so that
``result["eur"]`` / ``result["kwh"]`` stay consistent with ``best_cost``
even when the hedge drives a state below ``floor_kwh``.

Parity invariant: at hedge=None all results must be byte-identical to the
pre-hedge invocation (test_optimize_parity.py covers the full oracle parity;
here we just spot-check the noop contract).
"""
import pytest

from custom_components.anker_x1_smartgrid.optimize import optimize_grid
from custom_components.anker_x1_smartgrid.regret import _BIN_KWH, _apply_solar_load
from tests.helpers import make_config as make_cfg


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _args(**kw):
    """Call optimize_grid with a simple 6-hour no-export fixture."""
    pv = [0.0] * 6
    load = [0.5] * 6
    price = [0.10, 0.40, 0.40, 0.40, 0.40, 0.40]
    base = dict(window_start_h=0, window_len=6, soc_start=60.0, cfg=make_cfg())
    base.update(kw)
    return optimize_grid(pv, load, price, **base)


def _fixture():
    """Return the parameters for the below-floor consistency test (soc_start=12%)."""
    return dict(
        pv=[0.0] * 6,
        load=[0.5] * 6,
        price=[0.10, 0.40, 0.40, 0.40, 0.40, 0.40],
        soc_start=12.0,
        cfg=make_cfg(),
    )


def _resim(schedule, hedge, pv, load, price, soc_start, cfg):
    """Re-simulate optimize_grid's per-hour SoC transitions to derive expected eur and kwh.

    Replays _apply_solar_load + hedge debit + charge offset + floor-import in the
    same order as the HEDGED forward DP pass (MAJOR-1 fix).  This is the black-box
    consistency check: the backtrack must agree with these forward floor-import values.

    Uses the same bin-quantized SoC progression as the forward pass so that
    ``from_bin(prev_b)`` in the backtrack matches what we compute here.
    """
    cap_kwh = cfg.capacity_kwh
    floor_kwh = cfg.soc_floor / 100.0 * cap_kwh
    eta = cfg.eta_charge if cfg.eta_charge > 1e-9 else 1.0

    n_states = round(cap_kwh / _BIN_KWH) + 1

    def to_bin(soc):
        return max(0, min(n_states - 1, round(soc / _BIN_KWH)))

    def from_bin(b):
        return b * _BIN_KWH

    # Mirror the forward pass: start from the binned initial SoC.
    soc = from_bin(to_bin(soc_start / 100.0 * cap_kwh))

    total_eur = 0.0
    total_kwh = 0.0

    for h in range(len(schedule)):
        net = pv[h] - load[h]
        soc_after = _apply_solar_load(soc, net, cfg)

        # Apply hedge debit exactly as the (post-MAJOR-1) forward pass does.
        if hedge and h < len(hedge) and hedge[h] > 0.0:
            soc_after = max(0.0, soc_after - hedge[h])

        g_ac = schedule[h]
        g_dc = g_ac * eta
        new_soc_pre = soc_after + g_dc
        floor_import_kwh_h = max(0.0, floor_kwh - new_soc_pre)
        floor_import_cost_h = floor_import_kwh_h * price[h]
        new_soc = max(new_soc_pre, floor_kwh)

        total_eur += g_ac * price[h] + floor_import_cost_h
        total_kwh += g_ac + floor_import_kwh_h

        # Advance through the same binned SoC the forward pass uses.
        soc = from_bin(to_bin(new_soc))

    return total_eur, total_kwh


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_none_is_noop_byte_identical():
    """hedge_drain_kwh=None is byte-identical to the no-param call."""
    a = _args()
    b = _args(hedge_drain_kwh=None)
    assert a["schedule"] == b["schedule"]
    assert a["eur"] == b["eur"]
    assert a["kwh"] == b["kwh"]


def test_zero_list_is_noop():
    """All-zero hedge list produces no change (the guard is `> 0.0`)."""
    a = _args()
    b = _args(hedge_drain_kwh=[0.0] * 6)
    assert a["schedule"] == b["schedule"]
    assert a["eur"] == b["eur"]
    assert a["kwh"] == b["kwh"]


def test_hedge_books_more_charge_in_cheap_hour():
    """A debit at h=0 (cheap hour €0.10) should drive the DP to book more charge there."""
    base = _args()
    hed = _args(hedge_drain_kwh=[2.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    assert sum(hed["schedule"]) >= sum(base["schedule"])
    assert hed["schedule"][0] >= base["schedule"][0]


def test_length_mismatch_raises():
    """Wrong-length hedge list raises ValueError."""
    with pytest.raises(ValueError):
        _args(hedge_drain_kwh=[0.0] * 5)


def test_eur_kwh_consistent_below_floor():
    """MAJOR-1: backtrack must agree with the HEDGED forward floor-import.

    A large hedge (3 kWh at h=0) on a low-SoC start (12%) drives the DP state
    well below ``floor_kwh``.  Before MAJOR-1 the backtrack recomputed floor-import
    WITHOUT the hedge → ``result["eur"]`` drifted from ``best_cost``.  After the
    fix (store fi_eur/fi_kwh in the parent tuple) the two must match the
    independent ``_resim`` re-derivation within floating-point tolerance.
    """
    res = _args(soc_start=12.0, hedge_drain_kwh=[3.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    expected_eur, expected_kwh = _resim(
        res["schedule"], hedge=[3.0, 0, 0, 0, 0, 0], **_fixture()
    )
    assert res["eur"] == pytest.approx(expected_eur, abs=1e-6)
    assert res["kwh"] == pytest.approx(expected_kwh, abs=1e-6)
