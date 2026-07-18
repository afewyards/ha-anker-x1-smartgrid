import math

from custom_components.anker_x1_smartgrid import const
from custom_components.anker_x1_smartgrid.efficiency import (
    BinStat,
    EfficiencyCurve,
    _bin_midpoint_w,
    _interpolate_gated_bins,
)

_EDGES = const.EFFICIENCY_DC_BIN_EDGES_W  # [400, 800, 1500, 2500, 4000]
_LOS = [0.0, *_EDGES]
_HIS = [*_EDGES, float("inf")]


def _bin(i, eta, *, confident, reason="", measured="auto", direction="discharge"):
    """One BinStat for bin index i with the canonical edges.

    measured defaults to eta for confident bins, None for gated bins; pass an
    explicit value (e.g. the over_unity case) to override.
    """
    if measured == "auto":
        measured = eta if confident else None
    return BinStat(
        _LOS[i],
        _HIS[i],
        direction,
        eta,
        measured,
        99 if confident else 0,
        9.0 if confident else 0.0,
        confident,
        reason,
    )


def test_bin_midpoint_w_uses_lo_for_unbounded_top_bin():
    assert _bin_midpoint_w(_bin(2, 0.9, confident=True)) == 1150.0   # [800,1500)
    assert _bin_midpoint_w(_bin(5, 0.9, confident=True)) == 4000.0   # [4000, inf) -> lo_w


def test_interp_between_two_anchors_discharge():
    # Live 2026-07-18 discharge shape: bin1 & bin5 confident, 0/2/3/4 gated.
    bins = [
        _bin(0, 0.9239, confident=False, reason="no_data"),
        _bin(1, 0.9658, confident=True),
        _bin(2, 0.9239, confident=False, reason="low_confidence", measured=0.945),
        _bin(3, 0.9239, confident=False, reason="low_confidence", measured=0.968),
        _bin(4, 0.9239, confident=False, reason="no_data"),
        _bin(5, 0.9879, confident=True),
    ]
    out = _interpolate_gated_bins(bins)

    def interp(x):  # anchors (600, 0.9658) .. (4000, 0.9879)
        return 0.9658 + (0.9879 - 0.9658) * (x - 600.0) / (4000.0 - 600.0)

    assert math.isclose(out[2].eta, interp(1150.0), rel_tol=1e-12)  # ~0.969
    assert math.isclose(out[3].eta, interp(2000.0), rel_tol=1e-12)  # ~0.975
    assert math.isclose(out[4].eta, interp(3250.0), rel_tol=1e-12)  # ~0.983
    assert out[0].eta == 0.9658  # midpoint 200 < 600 -> flat low anchor
    # confident bins returned untouched (identity); gated non-eta fields intact
    assert out[1] is bins[1] and out[5] is bins[5]
    assert out[2].measured == 0.945 and out[2].fallback_reason == "low_confidence"
    assert out[2].confident is False
    # realism sanity vs spec's rounded expectations
    assert abs(out[2].eta - 0.969) < 0.001
    assert abs(out[3].eta - 0.975) < 0.001
    assert abs(out[4].eta - 0.983) < 0.001


def test_flat_extension_below_and_above_anchor_range():
    bins = [
        _bin(0, 0.5, confident=False, reason="no_data"),
        _bin(1, 0.80, confident=True),
        _bin(2, 0.5, confident=False, reason="no_data"),
        _bin(3, 0.90, confident=True),
        _bin(4, 0.5, confident=False, reason="no_data"),
        _bin(5, 0.5, confident=False, reason="no_data"),
    ]
    out = _interpolate_gated_bins(bins)
    assert out[0].eta == 0.80  # midpoint 200 <= 600 -> flat low anchor
    assert out[4].eta == 0.90  # midpoint 3250 >= 2000 -> flat high anchor
    assert out[5].eta == 0.90  # top-bin x 4000 >= 2000 -> flat high anchor
    mid = 0.80 + (0.90 - 0.80) * (1150.0 - 600.0) / (2000.0 - 600.0)
    assert math.isclose(out[2].eta, mid, rel_tol=1e-12)


def test_zero_confident_anchors_returns_identity_static_prior():
    prior = 0.9239
    bins = [_bin(i, prior, confident=False, reason="no_data") for i in range(6)]
    out = _interpolate_gated_bins(bins)
    assert out is bins  # identity: no rewrite pass runs at all
    assert all(b.eta == prior for b in out)  # every bin still the static prior


def test_over_unity_bin_excluded_as_anchor_but_rewritten():
    bins = [
        _bin(0, 0.92, confident=False, reason="no_data"),
        _bin(1, 0.95, confident=True),
        _bin(2, 0.92, confident=False, reason="over_unity", measured=1.05),
        _bin(3, 0.97, confident=True),
        _bin(4, 0.92, confident=False, reason="no_data"),
        _bin(5, 0.92, confident=False, reason="no_data"),
    ]
    out = _interpolate_gated_bins(bins)
    # bin2 interpolates between the 0.95 and 0.97 anchors ONLY — its own 1.05
    # measured is not confident, so it never enters the anchor set.
    expected = 0.95 + (0.97 - 0.95) * (1150.0 - 600.0) / (2000.0 - 600.0)
    assert math.isclose(out[2].eta, expected, rel_tol=1e-12)
    assert out[2].eta <= 1.0
    assert out[2].fallback_reason == "over_unity"  # reason string preserved
    assert out[2].measured == 1.05                 # raw measured preserved
    assert out[2].confident is False


def test_charge_side_gated_pulled_down_below_prior():
    prior = 0.92  # static charge prior sits ABOVE every confident anchor
    bins = [
        _bin(0, prior, confident=False, reason="no_data", direction="charge"),
        _bin(1, 0.716, confident=True, direction="charge"),
        _bin(2, 0.8228, confident=True, direction="charge"),
        _bin(3, prior, confident=False, reason="low_confidence", measured=0.787, direction="charge"),
        _bin(4, prior, confident=False, reason="no_data", direction="charge"),
        _bin(5, 0.8801, confident=True, direction="charge"),
    ]
    out = _interpolate_gated_bins(bins)

    def interp(x):  # anchors (1150, 0.8228) .. (4000, 0.8801)
        return 0.8228 + (0.8801 - 0.8228) * (x - 1150.0) / (4000.0 - 1150.0)

    assert math.isclose(out[3].eta, interp(2000.0), rel_tol=1e-12)  # ~0.840
    assert math.isclose(out[4].eta, interp(3250.0), rel_tol=1e-12)  # ~0.865
    assert out[0].eta == 0.716  # midpoint 200 < 600 -> flat low anchor, NOT 0.92
    # the whole point: every gated bin is pulled BELOW the prior it used to use
    assert out[0].eta < prior and out[3].eta < prior and out[4].eta < prior
    assert abs(out[3].eta - 0.840) < 0.001
    assert abs(out[4].eta - 0.865) < 0.001


def test_eta_clamped_at_one():
    # Defensive: interpolation between <=1.0 anchors can't exceed 1.0, but the
    # min(., 1.0) clamp must hold the invariant even at an anchor of exactly 1.0.
    bins = [
        _bin(0, 0.92, confident=False, reason="no_data"),
        _bin(1, 1.0, confident=True),
        _bin(2, 0.92, confident=False, reason="no_data"),
        _bin(3, 0.92, confident=False, reason="no_data"),
        _bin(4, 0.92, confident=False, reason="no_data"),
        _bin(5, 0.98, confident=True),
    ]
    out = _interpolate_gated_bins(bins)
    assert out[0].eta == 1.0  # flat extension of the 1.0 anchor, still <= 1.0
    assert all(b.eta <= 1.0 for b in out)


def test_interpolation_idempotent():
    bins = [
        _bin(0, 0.92, confident=False, reason="no_data", direction="charge"),
        _bin(1, 0.716, confident=True, direction="charge"),
        _bin(2, 0.8228, confident=True, direction="charge"),
        _bin(3, 0.92, confident=False, reason="low_confidence", measured=0.787, direction="charge"),
        _bin(4, 0.92, confident=False, reason="no_data", direction="charge"),
        _bin(5, 0.8801, confident=True, direction="charge"),
    ]
    once = _interpolate_gated_bins(bins)
    twice = _interpolate_gated_bins(once)
    assert [b.eta for b in once] == [b.eta for b in twice]
    assert once == twice  # frozen-dataclass equality over ALL fields


def test_aggregate_interpolates_gated_bins_through_the_gate():
    # Full _aggregate path: two confident discharge anchors (bin1, bin5) + one
    # low-confidence gated bin (bin3). The gated bin must come back at the
    # interpolated eta, NOT the static prior it was assigned at finalization.
    etas = {"charge": [[] for _ in range(6)], "discharge": [[] for _ in range(6)]}
    dc = {"charge": [0.0] * 6, "discharge": [0.0] * 6}
    etas["discharge"][1] = [(0.9658, 0.5)] * 10  # n=10, 5.0 kWh -> confident
    dc["discharge"][1] = 5.0
    etas["discharge"][5] = [(0.9879, 0.5)] * 10
    dc["discharge"][5] = 5.0
    etas["discharge"][3] = [(0.90, 0.5)] * 3      # n=3 -> low_confidence
    dc["discharge"][3] = 1.5
    out = EfficiencyCurve._aggregate("discharge", etas, dc, 0.9239)
    assert out[1].confident is True and out[5].confident is True
    assert out[3].confident is False and out[3].fallback_reason == "low_confidence"
    expected = 0.9658 + (0.9879 - 0.9658) * (2000.0 - 600.0) / (4000.0 - 600.0)
    assert math.isclose(out[3].eta, expected, rel_tol=1e-12)
    assert out[3].measured == 0.90  # median preserved; only eta rewritten
    assert out[0].eta == 0.9658     # no_data bin0 flat-extended, not 0.9239
