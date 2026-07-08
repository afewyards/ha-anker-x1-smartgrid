"""TDD tests for export-aware cost term in optimize_grid (T0.2c).

Verifies the ``feed_in`` parameter added to :func:`optimize_grid`:

- ``feed_in=None`` → **exact parity** with the no-feed_in call.  The T0.1b
  parity invariant (optimize_grid ≡ hindsight_optimal_grid for full 24-h
  windows) must remain green.
- ``feed_in`` provided → solar export surplus credited at the full
  ``feed_in[h]`` EUR/kWh (effective export price, post-fee; no haircut).
  The credit is reflected in the reported ``eur``
  (net cost = import cost − export credit).
- Length mismatch between ``feed_in`` and ``window_len`` → ``ValueError``.
- High ``feed_in`` prices do **not** cause the optimizer to over-buy beyond what
  is needed to reach ``soc_target`` (battery cap is the physical guard).
"""
import pytest

from custom_components.anker_x1_smartgrid.models import Config
from custom_components.anker_x1_smartgrid.optimize import optimize_grid
from custom_components.anker_x1_smartgrid.regret import DayData, hindsight_optimal_grid


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_cfg(**overrides) -> Config:
    """Return a Config with clean export-test defaults (eta=1.0 for exact arithmetic)."""
    defaults = dict(
        capacity_kwh=10.0,
        soc_floor=20.0,   # 2 kWh floor
        soc_target=80.0,  # 8 kWh target
        max_charge_w=3000.0,  # 3 kWh/h
        eta_charge=1.0,   # AC == DC (simplifies test arithmetic)
    )
    defaults.update(overrides)
    return Config(**defaults)


# ---------------------------------------------------------------------------
# 1. feed_in=None → exact parity invariant (additive-off, default-safe)
# ---------------------------------------------------------------------------


class TestFeedInNoneParityInvariant:
    """feed_in=None produces output byte-identical to omitting feed_in entirely.

    This is the HARD parity invariant: the export term must be truly additive —
    absent when feed_in is None, with zero effect on routing or reported values.
    """

    def test_none_identical_to_default(self):
        """Explicit feed_in=None must produce the same result as the default call."""
        cfg = make_cfg()
        pv = [0.0] * 4 + [1.5] * 8 + [0.0] * 12
        load = [0.5] * 24
        price = [0.20] * 24

        result_default = optimize_grid(
            pv, load, price, soc_start=50.0, cfg=cfg,
            window_start_h=0, window_len=24,
        )
        result_none = optimize_grid(
            pv, load, price, soc_start=50.0, cfg=cfg,
            window_start_h=0, window_len=24,
            feed_in=None,
        )

        assert result_default["kwh"] == pytest.approx(result_none["kwh"], abs=1e-9)
        assert result_default["eur"] == pytest.approx(result_none["eur"], abs=1e-9)
        for h in range(24):
            assert result_default["schedule"][h] == pytest.approx(
                result_none["schedule"][h], abs=1e-9
            ), f"Schedule mismatch at h={h}"

    def test_parity_gate_still_green_with_none(self):
        """optimize_grid(feed_in=None) ≡ hindsight_optimal_grid (T0.1b gate holds).

        Runs a representative 24-h scenario through both optimizers and asserts
        exact parity on schedule, kWh, EUR, and infeasible flag.
        """
        cfg = make_cfg()
        pv = [0.0] * 9 + [1.5] * 6 + [0.0] * 9
        load = [0.8] * 24
        price = [0.30, 0.25] + [0.20] * 6 + [0.35] * 16

        day = DayData(
            pv_kwh=tuple(pv),
            load_kwh=tuple(load),
            price=tuple(price),
            soc_start=60.0,
        )
        hind = hindsight_optimal_grid(day, cfg)
        opt = optimize_grid(
            pv, load, price, soc_start=60.0, cfg=cfg,
            window_start_h=0, window_len=24,
            feed_in=None,
        )

        assert opt["kwh"] == pytest.approx(hind["kwh"], abs=1e-6), (
            f"T0.1b parity broken on kWh: opt={opt['kwh']:.8f} hind={hind['kwh']:.8f}"
        )
        assert opt["eur"] == pytest.approx(hind["eur"], abs=1e-6), (
            f"T0.1b parity broken on EUR: opt={opt['eur']:.8f} hind={hind['eur']:.8f}"
        )
        for h in range(24):
            assert opt["schedule"][h] == pytest.approx(hind["schedule"][h], abs=1e-6), (
                f"T0.1b parity broken on schedule[h={h}]"
            )

    def test_partial_window_none_parity(self):
        """Parity holds for a partial-window call with feed_in=None."""
        cfg = make_cfg()
        window_len = 6
        pv = [0.0, 0.0, 1.0, 2.0, 0.5, 0.0]
        load = [0.3] * window_len
        price = [0.20, 0.10, 0.15, 0.25, 0.18, 0.22]

        result_default = optimize_grid(
            pv, load, price, soc_start=60.0, cfg=cfg,
            window_start_h=8, window_len=window_len,
        )
        result_none = optimize_grid(
            pv, load, price, soc_start=60.0, cfg=cfg,
            window_start_h=8, window_len=window_len,
            feed_in=None,
        )

        assert result_default["eur"] == pytest.approx(result_none["eur"], abs=1e-9)
        assert result_default["kwh"] == pytest.approx(result_none["kwh"], abs=1e-9)


# ---------------------------------------------------------------------------
# 2. feed_in length validation
# ---------------------------------------------------------------------------


class TestFeedInLengthValidation:
    """feed_in with the wrong length raises ValueError immediately."""

    def test_feed_in_too_short_raises(self):
        """feed_in shorter than window_len → ValueError mentioning 'feed_in'."""
        cfg = make_cfg()
        with pytest.raises(ValueError, match="feed_in"):
            optimize_grid(
                [0.0] * 4, [0.0] * 4, [0.20] * 4,
                soc_start=50.0, cfg=cfg,
                window_start_h=0, window_len=4,
                feed_in=[0.10, 0.10, 0.10],  # length 3 ≠ 4
            )

    def test_feed_in_too_long_raises(self):
        """feed_in longer than window_len → ValueError mentioning 'feed_in'."""
        cfg = make_cfg()
        with pytest.raises(ValueError, match="feed_in"):
            optimize_grid(
                [0.0] * 4, [0.0] * 4, [0.20] * 4,
                soc_start=50.0, cfg=cfg,
                window_start_h=0, window_len=4,
                feed_in=[0.10] * 5,  # length 5 ≠ 4
            )

    def test_feed_in_empty_raises_for_nonzero_window(self):
        """Empty feed_in with window_len > 0 → ValueError."""
        cfg = make_cfg()
        with pytest.raises(ValueError, match="feed_in"):
            optimize_grid(
                [0.0] * 4, [0.0] * 4, [0.20] * 4,
                soc_start=50.0, cfg=cfg,
                window_start_h=0, window_len=4,
                feed_in=[],  # length 0 ≠ 4
            )

    def test_feed_in_correct_length_no_error(self):
        """Correct length → no error."""
        cfg = make_cfg()
        result = optimize_grid(
            [0.0] * 4, [0.0] * 4, [0.20] * 4,
            soc_start=80.0, cfg=cfg,
            window_start_h=0, window_len=4,
            feed_in=[0.10] * 4,
        )
        assert "schedule" in result


# ---------------------------------------------------------------------------
# 3. Solar export credited at discount × feed_in rate
# ---------------------------------------------------------------------------


class TestSolarExportCreditedAtFullEffectivePrice:
    """Solar surplus that can't be stored is credited at the full feed_in price.

    Integration point: after ``_apply_solar_load`` each hour, any PV that
    exceeded the battery's rate/headroom is solar_export_ac = net − delta_soc/eta.
    The credit reduces the reported ``eur`` (net cost = import cost − export credit).
    No haircut is applied: the full effective export price (post-fee) is used.
    """

    def test_battery_full_all_solar_exported_credit_in_eur(self):
        """Battery at target → all PV is exported → eur reflects discounted credit.

        soc_start=80% (= target, 8 kWh).  pv=[2.0]*4.  No load.
        All 2 kWh PV per hour is exported (battery is full, can't absorb more).

        Expected:
          kwh = 0  (no grid import needed)
          export_credit = 4 × 2.0 × 1.0 × 0.10 = 0.80  (full effective price)
          eur = 0 − export_credit  (negative: export revenue exceeds import cost)
        """
        cfg = make_cfg()
        window_len = 4
        pv = [2.0] * window_len
        load = [0.0] * window_len
        price = [0.20] * window_len
        feed_in = [0.10] * window_len

        result = optimize_grid(
            pv, load, price, soc_start=80.0, cfg=cfg,
            window_start_h=0, window_len=window_len,
            feed_in=feed_in,
        )

        # 4h × 2.0 kWh × 1.0 (no haircut) × 0.10 €/kWh = 0.80 €
        # (was 0.7 × 0.80 = 0.56 under the retired 0.7 haircut).
        expected_credit = window_len * 2.0 * 0.10
        assert result["kwh"] == pytest.approx(0.0, abs=1e-6), (
            f"No grid import should be needed, got {result['kwh']:.4f} kWh"
        )
        assert result["eur"] == pytest.approx(-expected_credit, abs=1e-6), (
            f"eur should be -{expected_credit:.4f} (export credit), got {result['eur']:.4f}"
        )

    def test_partial_solar_export_battery_fills_then_exports(self):
        """Battery partially fills at h0 then is full at h1 — exports tracked correctly.

        soc_start=70% (7 kWh).  pv=[2.0, 2.0, 0.0, 0.0].  No load.

        h0: soc=7, dc_solar=min(2,3)*1=2, soc_after=min(9,8)=8.
            Export = 2 − (8−7)/1 = 1.0 kWh.
        h1: soc=8 (full), dc_solar=2, soc_after=8.
            Export = 2 − 0/1 = 2.0 kWh.
        h2–h3: no PV → no export.

        Total export = 3.0 kWh.
        credit = 3.0 × 1.0 × 0.10 = 0.30 (full effective price; was 0.7×=0.21).
        kwh = 0, eur = −credit.
        """
        cfg = make_cfg()
        window_len = 4
        pv = [2.0, 2.0, 0.0, 0.0]
        load = [0.0] * window_len
        price = [0.20] * window_len
        feed_in = [0.10] * window_len

        result = optimize_grid(
            pv, load, price, soc_start=70.0, cfg=cfg,
            window_start_h=0, window_len=window_len,
            feed_in=feed_in,
        )

        # 3.0 kWh × 1.0 (no haircut) × 0.10 €/kWh = 0.30 €
        # (was 3.0 × 0.7 × 0.10 = 0.21 under the retired 0.7 haircut).
        expected_credit = 3.0 * 0.10
        assert result["kwh"] == pytest.approx(0.0, abs=1e-6)
        assert result["eur"] == pytest.approx(-expected_credit, abs=1e-6), (
            f"eur={result['eur']:.6f}, expected={-expected_credit:.6f}"
        )

    def test_no_solar_no_export_credit(self):
        """pv=0 → no solar to export → eur with and without feed_in are identical.

        When there is no PV, the export credit is always zero regardless of the
        feed_in tariff values.  The optimizer must NOT behave differently.
        """
        cfg = make_cfg()
        window_len = 4
        pv = [0.0] * window_len
        load = [0.0] * window_len
        price = [0.20] * window_len

        result_no_fi = optimize_grid(
            pv, load, price, soc_start=50.0, cfg=cfg,
            window_start_h=0, window_len=window_len,
        )
        result_with_fi = optimize_grid(
            pv, load, price, soc_start=50.0, cfg=cfg,
            window_start_h=0, window_len=window_len,
            feed_in=[0.50] * window_len,  # high feed_in, but no PV → zero credit
        )

        assert result_with_fi["kwh"] == pytest.approx(result_no_fi["kwh"], abs=1e-6)
        assert result_with_fi["eur"] == pytest.approx(result_no_fi["eur"], abs=1e-6), (
            f"No PV → export credit should be zero; "
            f"no_fi_eur={result_no_fi['eur']:.4f} with_fi_eur={result_with_fi['eur']:.4f}"
        )

    def test_rate_limited_pv_export_partial_credit(self):
        """PV > max_charge_rate: rate overflow is exported and credited.

        soc_start=50% (5 kWh).  pv=[5.0].  max_charge=3 kWh/h.  eta=1.0.
        dc_solar = min(5.0, 3.0) × 1.0 = 3.0  → soc_after = min(5+3, 8) = 8.0.
        Battery is full after solar; no grid import needed.
        Export = 5.0 − (8.0−5.0)/1.0 = 5.0 − 3.0 = 2.0 kWh (rate overflow).
        credit = 2.0 × 1.0 × feed_in[0] = 0.20 (full price; was 0.7×=0.14).
        """
        cfg = make_cfg()
        window_len = 1
        pv = [5.0]
        load = [0.0]
        price = [0.20]
        feed_in = [0.10]

        result = optimize_grid(
            pv, load, price, soc_start=50.0, cfg=cfg,
            window_start_h=8, window_len=window_len,
            feed_in=feed_in,
        )

        # Solar fills battery to target; no grid import needed
        assert result["kwh"] == pytest.approx(0.0, abs=1e-6), (
            f"Solar fills battery (soc 5→8 kWh); no grid import expected, got {result['kwh']:.4f}"
        )
        # 2.0 kWh × 1.0 (no haircut) × 0.10 €/kWh = 0.20 €
        # (was 2.0 × 0.7 × 0.10 = 0.14 under the retired 0.7 haircut).
        expected_credit = 2.0 * 0.10
        assert result["eur"] == pytest.approx(-expected_credit, abs=1e-6), (
            f"Rate-limited export credit: expected eur={-expected_credit:.6f}, "
            f"got {result['eur']:.6f}"
        )


# ---------------------------------------------------------------------------
# 4. Bias-to-minimize-reliance: high feed_in does NOT cause over-buy
# ---------------------------------------------------------------------------


class TestHighFeedInNoBias:
    """High feed_in never increases grid import beyond what the target requires.

    The battery cap (max_grid_dc = min(rate, target − soc_after)) physically
    prevents excess import; pricing solar spill at the full effective rate does
    not change this — the DP still cannot over-buy beyond the SoC target.
    This test documents and verifies that invariant.
    """

    def test_high_feed_in_same_kwh_as_low_feed_in(self):
        """Grid import with feed_in=1.0 matches import with feed_in=0.10.

        No PV.  soc_start=50% (5 kWh), deficit=3 kWh.  Price=[0.20]*4.
        Whether feed_in is low or extreme, the import must be identical — the
        optimizer has no mechanism to over-buy beyond the deficit.
        """
        cfg = make_cfg()
        window_len = 4
        pv = [0.0] * window_len
        load = [0.0] * window_len
        price = [0.20] * window_len

        result_low_fi = optimize_grid(
            pv, load, price, soc_start=50.0, cfg=cfg,
            window_start_h=0, window_len=window_len,
            feed_in=[0.10] * window_len,
        )
        result_high_fi = optimize_grid(
            pv, load, price, soc_start=50.0, cfg=cfg,
            window_start_h=0, window_len=window_len,
            feed_in=[1.00] * window_len,
        )

        assert result_high_fi["kwh"] == pytest.approx(result_low_fi["kwh"], abs=1e-6), (
            f"High feed_in must NOT increase grid import: "
            f"low={result_low_fi['kwh']:.3f} high={result_high_fi['kwh']:.3f}"
        )

    def test_very_high_feed_in_never_exceeds_deficit(self):
        """Even feed_in=100 EUR/kWh: grid import == deficit, never more.

        soc_start=50% (5 kWh), target=80% (8 kWh), deficit=3 kWh.
        With an outrageously high feed_in, import still == 3 kWh exactly.
        """
        cfg = make_cfg()
        window_len = 4
        pv = [0.0] * window_len
        load = [0.0] * window_len
        price = [0.20] * window_len

        result = optimize_grid(
            pv, load, price, soc_start=50.0, cfg=cfg,
            window_start_h=0, window_len=window_len,
            feed_in=[100.0] * window_len,
        )

        assert result["kwh"] == pytest.approx(3.0, abs=1e-6), (
            f"Import must equal deficit (3.0 kWh), got {result['kwh']:.4f}"
        )

    def test_solar_high_feed_in_does_not_block_needed_charge(self):
        """High feed_in with solar surplus → still charges grid when battery needs it.

        soc_start=20% (2 kWh), deficit=6 kWh.  pv=[1.0, 0.0, 0.0, 0.0].
        Solar at h0 charges 1 kWh DC → soc=3 kWh.  Still needs 5 more kWh from grid.
        Even with very high feed_in, the optimizer must not suppress grid charging.
        """
        cfg = make_cfg()
        window_len = 4
        pv = [1.0, 0.0, 0.0, 0.0]
        load = [0.0] * window_len
        price = [0.20] * window_len

        result_no_fi = optimize_grid(
            pv, load, price, soc_start=20.0, cfg=cfg,
            window_start_h=0, window_len=window_len,
        )
        result_high_fi = optimize_grid(
            pv, load, price, soc_start=20.0, cfg=cfg,
            window_start_h=0, window_len=window_len,
            feed_in=[50.0] * window_len,
        )

        # Grid import should not INCREASE due to feed_in
        assert result_high_fi["kwh"] <= result_no_fi["kwh"] + 1e-6, (
            f"feed_in must not increase grid import beyond no-feedin case: "
            f"no_fi={result_no_fi['kwh']:.3f} high_fi={result_high_fi['kwh']:.3f}"
        )


# ---------------------------------------------------------------------------
# 5. Export credit is reflected in eur (net cost accounting)
# ---------------------------------------------------------------------------


class TestExportCreditReflectedInEur:
    """eur with feed_in = import_cost − export_credit (net cost)."""

    def test_export_credit_reduces_eur_vs_no_feedin(self):
        """When solar export occurs, eur with feed_in < eur without feed_in.

        soc_start=80% (full).  pv=[1.0]*4.  No load.  No grid needed.
        Without feed_in: eur = 0.0.
        With feed_in: eur < 0 (export revenue credited).
        """
        cfg = make_cfg()
        window_len = 4
        pv = [1.0] * window_len
        load = [0.0] * window_len
        price = [0.20] * window_len

        result_no_fi = optimize_grid(
            pv, load, price, soc_start=80.0, cfg=cfg,
            window_start_h=0, window_len=window_len,
        )
        result_with_fi = optimize_grid(
            pv, load, price, soc_start=80.0, cfg=cfg,
            window_start_h=0, window_len=window_len,
            feed_in=[0.10] * window_len,
        )

        assert result_no_fi["eur"] == pytest.approx(0.0, abs=1e-6)
        assert result_with_fi["eur"] < result_no_fi["eur"] - 1e-6, (
            f"With feed_in, eur ({result_with_fi['eur']:.4f}) must be less than "
            f"without ({result_no_fi['eur']:.4f})"
        )

    def test_doubling_feed_in_doubles_credit(self):
        """Doubling the feed_in price doubles the export credit magnitude.

        soc_start=80% (full).  pv=[1.0]*4.  All solar exported.
        eur(feed_in=0.20) ≈ 2 × eur(feed_in=0.10) in magnitude (both negative).
        """
        cfg = make_cfg()
        window_len = 4
        pv = [1.0] * window_len
        load = [0.0] * window_len
        price = [0.20] * window_len

        result_fi_low = optimize_grid(
            pv, load, price, soc_start=80.0, cfg=cfg,
            window_start_h=0, window_len=window_len,
            feed_in=[0.10] * window_len,
        )
        result_fi_high = optimize_grid(
            pv, load, price, soc_start=80.0, cfg=cfg,
            window_start_h=0, window_len=window_len,
            feed_in=[0.20] * window_len,
        )

        credit_low = -result_fi_low["eur"]    # positive (export revenue)
        credit_high = -result_fi_high["eur"]  # should be exactly double

        assert credit_low > 1e-6, "Low feed_in should produce non-zero credit"
        assert credit_high == pytest.approx(2 * credit_low, abs=1e-6), (
            f"Doubled feed_in should double credit: "
            f"credit_low={credit_low:.6f} credit_high={credit_high:.6f}"
        )

    def test_export_credit_eur_key_present_when_feed_in_provided(self):
        """Result contains 'export_credit_eur' key when feed_in is provided."""
        cfg = make_cfg()
        window_len = 4
        pv = [2.0] * window_len
        load = [0.0] * window_len
        price = [0.20] * window_len

        result = optimize_grid(
            pv, load, price, soc_start=80.0, cfg=cfg,
            window_start_h=0, window_len=window_len,
            feed_in=[0.10] * window_len,
        )

        assert "export_credit_eur" in result, (
            "result should contain 'export_credit_eur' when feed_in is provided"
        )
        assert result["export_credit_eur"] > 0.0, (
            f"export_credit_eur should be positive (solar was exported), "
            f"got {result.get('export_credit_eur')}"
        )

    def test_export_credit_eur_not_present_without_feed_in(self):
        """Result does NOT contain 'export_credit_eur' when feed_in is None."""
        cfg = make_cfg()
        window_len = 4
        pv = [2.0] * window_len
        load = [0.0] * window_len
        price = [0.20] * window_len

        result = optimize_grid(
            pv, load, price, soc_start=80.0, cfg=cfg,
            window_start_h=0, window_len=window_len,
        )

        assert "export_credit_eur" not in result, (
            "result must NOT contain 'export_credit_eur' when feed_in is None "
            "(parity invariant: default-off, additive)"
        )


# ---------------------------------------------------------------------------
# 6. Full-price solar-spill credit (0.7 haircut retired)
# ---------------------------------------------------------------------------


def test_solar_spill_credited_at_full_effective_price():
    """Un-storable solar surplus is credited at the full (effective) feed-in price — no 0.7 haircut.

    soc_start=80% (= target, 8 kWh, no headroom).
    pv=[0]*9 + [5.0] + [0]*14.  All 5 kWh spills (battery full, cannot absorb).
    Expected credit: 5 kWh × 0.30 €/kWh = 1.50 €
    (was 0.7 × 1.50 = 1.05 € under the retired 0.7 haircut).
    """
    cfg = make_cfg()
    # soc_start=80% = target → no headroom → all 5 kWh spills.
    pv = [0.0] * 9 + [5.0] + [0.0] * 14
    load = [0.0] * 24
    price = [0.20] * 24
    feed_in = [0.30] * 24
    res = optimize_grid(pv, load, price, soc_start=80.0, cfg=cfg,
                        window_start_h=0, window_len=24, feed_in=feed_in)
    # 5 kWh spilled × 1.0 (no haircut) × 0.30 = 1.50 € credit
    # (was 0.7 × 1.50 = 1.05 € under the retired 0.7 haircut).
    assert res["export_credit_eur"] == pytest.approx(1.50, abs=1e-6)


# ---------------------------------------------------------------------------
# 7. Combined solar-spill + battery export capped at grid connection limit
# ---------------------------------------------------------------------------


def _make_export_cfg_for_cap_test(**overrides) -> Config:
    """Config for combined-export-cap tests: unit etas, explicit export limits."""
    defaults = dict(
        capacity_kwh=10.0,
        soc_floor=20.0,       # 2 kWh floor
        soc_target=80.0,      # 8 kWh target
        max_charge_w=3000.0,  # 3 kWh/h
        eta_charge=1.0,       # AC == DC on charge side
        round_trip_eff=1.0,   # eta_d = 1.0 (simplifies arithmetic)
        cycle_cost_eur_per_kwh=0.04,
        max_export_w=3000.0,
        grid_export_limit_w=3000.0,
    )
    defaults.update(overrides)
    return Config(**defaults)


class TestCombinedSolarBatteryExportCap:
    """Battery export is bounded so solar_export_ac + e_ac ≤ min(max_export_w, grid_export_limit_w)/1000.

    Physics recap:
    - solar_export_ac: AC kWh of un-storable PV surplus that spills to grid (computed from
      the solar/load step physics; NOT commanded — it's a physical overflow).
    - e_ac = e_dc * eta_d: battery AC kWh commanded to export.
    Both flow on the same AC connection, so their sum must stay within the connection cap.

    The bug (pre-fix): the DP enumerated battery export up to max_export_dc_h derived from
    max_export_w alone, without subtracting solar_export_ac already using that headroom.
    Combined export could therefore far exceed the physical AC connection limit.
    """

    def test_solar_exceeds_cap_battery_export_is_zero(self):
        """When solar spill ≥ ac_cap, battery export must be exactly 0.

        Setup (1-hour window):
          soc_start=80% (8 kWh = target, battery is full).
          pv=5.0 kWh — all 5 kWh spills (battery at target, _apply_solar_load absorbs 0).
          max_export_w=grid_export_limit_w=3000 W → ac_cap=3.0 kWh/h.
          export_price=[0.50] (very profitable — DP would export if it could).

        solar_export_ac=5 already exceeds ac_cap=3, so battery headroom=max(0,3−5)=0.
        Expected: battery export = 0 kWh (the AC bus is already at/above cap from solar).

        Pre-fix: battery ignores solar and tries to export up to 3 kWh → combined 8 kWh >> cap.
        Post-fix: batt_dc_cap=0 → no export steps enumerated → battery export = 0 kWh.
        """
        cfg = _make_export_cfg_for_cap_test()
        window_len = 1
        pv = [5.0]      # all 5 kWh spills (battery full, can't absorb)
        load = [0.0]
        price = [0.20]
        export_price = [0.50]  # profitable — DP would eagerly export without the cap

        result = optimize_grid(
            pv, load, price, soc_start=80.0, cfg=cfg,
            window_start_h=10, window_len=window_len,
            export_price=export_price,
            terminal_mode="water_value", water_value=0.0,
        )

        # solar_export_ac = 5.0 kWh (verified from physics: battery full, all PV spills)
        # ac_cap = min(3000, 3000)/1000 = 3.0 kWh; batt_headroom = max(0, 3−5) = 0
        solar_export_ac = 5.0
        ac_cap = min(cfg.max_export_w, cfg.grid_export_limit_w) / 1000.0
        expected_max_batt_export = max(0.0, ac_cap - solar_export_ac)  # = 0.0

        battery_export_ac = result["export_schedule"][0]
        assert battery_export_ac <= expected_max_batt_export + 1e-6, (
            f"Battery export {battery_export_ac:.4f} kWh exceeds allowed headroom "
            f"{expected_max_batt_export:.4f} kWh "
            f"(solar_export_ac={solar_export_ac:.1f}, ac_cap={ac_cap:.1f})"
        )

    def test_solar_partial_battery_gets_remaining_headroom(self):
        """When solar spill < ac_cap, battery can export the remaining headroom.

        Setup (1-hour window):
          soc_start=60% (6 kWh).
          pv=4.0 kWh: _apply_solar_load absorbs min(4, rate=3)*eta=3 DC kWh,
            soc_after=min(6+3, 8)=8 kWh (hits target cap).
            delta_soc=2 kWh → ac_for_battery=2 → solar_export_ac=max(0,4−2)=2.0 kWh.
          max_export_w=grid_export_limit_w=3000 W → ac_cap=3.0 kWh.
          Battery AC headroom = max(0, 3−2) = 1.0 kWh → batt_dc_cap = 1.0 kWh.

        Expected: battery export ≤ 1.0 kWh (DP maximises profit → exactly 1.0 kWh).
        Combined: 2.0 (solar) + 1.0 (battery) = 3.0 = ac_cap ✓.

        Pre-fix: battery ignores solar and exports up to min(headroom=6, max=3)=3 kWh
          → combined 2+3=5 kWh >> 3 kWh cap.
        Post-fix: max_e_dc = min(6, 3, 1) = 1.0 kWh → battery exports 1.0 kWh.
        """
        cfg = _make_export_cfg_for_cap_test()
        window_len = 1
        pv = [4.0]      # 2 kWh absorbed by battery, 2 kWh spills
        load = [0.0]
        price = [0.20]
        export_price = [0.50]

        result = optimize_grid(
            pv, load, price, soc_start=60.0, cfg=cfg,
            window_start_h=10, window_len=window_len,
            export_price=export_price,
            terminal_mode="water_value", water_value=0.0,
        )

        # solar_export_ac = 2.0 kWh (2 kWh absorbed, 2 kWh spills from 4 kWh PV)
        # ac_cap = 3.0 kWh; batt_headroom = max(0, 3−2) = 1.0 kWh
        solar_export_ac = 2.0
        ac_cap = min(cfg.max_export_w, cfg.grid_export_limit_w) / 1000.0
        expected_max_batt_export = max(0.0, ac_cap - solar_export_ac)  # = 1.0 kWh

        battery_export_ac = result["export_schedule"][0]
        assert battery_export_ac <= expected_max_batt_export + 1e-6, (
            f"Battery export {battery_export_ac:.4f} kWh exceeds allowed headroom "
            f"{expected_max_batt_export:.4f} kWh "
            f"(solar_export_ac={solar_export_ac:.1f}, ac_cap={ac_cap:.1f})"
        )
        # DP maximises profit → should use the full 1.0 kWh of headroom
        assert battery_export_ac == pytest.approx(expected_max_batt_export, abs=1e-6), (
            f"DP should export exactly {expected_max_batt_export:.3f} kWh (max profitable), "
            f"got {battery_export_ac:.4f} kWh"
        )
