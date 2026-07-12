from datetime import datetime, timezone, timedelta, UTC
import pytest
from custom_components.anker_x1_smartgrid.models import Config, ForecastInterval
from custom_components.anker_x1_smartgrid import energy

T0 = datetime(2026, 6, 20, 10, 0, tzinfo=UTC)


def _iv(pv, load, dt=1.0, i=0):
    return ForecastInterval(T0 + timedelta(hours=i), pv, load, dt)


def test_no_solar_no_change():
    cfg = Config()
    assert energy.simulate_soc(50.0, [_iv(0, 0, 1, 0)], cfg) == 50.0


def test_surplus_charges_with_efficiency():
    # 10 kWh pack, 2000 W surplus for 1 h, eta 0.92 => 1.84 kWh => 18.4% SoC rise
    cfg = Config(capacity_kwh=10.0, eta_charge=0.92, soc_target=100.0)
    soc = energy.simulate_soc(50.0, [_iv(2500, 500, 1.0, 0)], cfg)
    assert abs(soc - (50.0 + 1.84 / 10.0 * 100.0)) < 1e-6


def test_load_exceeds_pv_no_charge():
    cfg = Config()
    assert energy.simulate_soc(50.0, [_iv(300, 1000, 1.0, 0)], cfg) == 50.0


def test_charge_power_capped():
    # surplus 10 kW but max_charge 6 kW => only 6 kW * eta lands
    cfg = Config(capacity_kwh=10.0, max_charge_w=6000.0, eta_charge=1.0, soc_target=100.0)
    soc = energy.simulate_soc(0.0, [_iv(12000, 2000, 1.0, 0)], cfg)
    assert abs(soc - 60.0) < 1e-6  # 6 kWh into 10 kWh


def test_soc_capped_at_target():
    cfg = Config(capacity_kwh=10.0, soc_target=97.0, eta_charge=1.0)
    soc = energy.simulate_soc(95.0, [_iv(6000, 0, 5.0, 0)], cfg)
    assert soc == 97.0


# ---------------------------------------------------------------------------
# export_surplus_kwh (B2)
# ---------------------------------------------------------------------------


class TestExportSurplusKwh:
    """Tests for energy.export_surplus_kwh — energy above the reserve."""

    def _cfg(self, **kw) -> Config:
        defaults = dict(
            capacity_kwh=10.0,
            soc_floor=10.0,  # 1 kWh
            soc_target=90.0,
            max_charge_w=3000.0,
            eta_charge=1.0,
        )
        defaults.update(kw)
        return Config(**defaults)

    def test_surplus_above_reserve(self):
        """soc=80%(8 kWh), reserve=3 kWh → surplus = 8 - 3 = 5 kWh."""
        cfg = self._cfg()
        reserve = 3.0
        surplus = energy.export_surplus_kwh(80.0, reserve, cfg)
        assert abs(surplus - 5.0) < 1e-6, f"Expected 5.0 kWh surplus, got {surplus}"

    def test_no_surplus_when_soc_below_reserve(self):
        """soc=30%(3 kWh), reserve=5 kWh → surplus = max(0, 3-5) = 0."""
        cfg = self._cfg()
        surplus = energy.export_surplus_kwh(30.0, 5.0, cfg)
        assert surplus == 0.0, f"Expected 0.0 surplus, got {surplus}"

    def test_sub_floor_soc_returns_zero(self):
        """SoC below soc_floor (5% < 10% floor) → surplus = 0.

        Even if reserve were 0, sub-floor SoC should yield 0 surplus since the
        battery is already below the safety floor.
        """
        cfg = self._cfg()
        # soc=5% → soc_kwh=0.5, which is below floor (1 kWh)
        surplus = energy.export_surplus_kwh(5.0, 0.0, cfg)
        assert surplus == 0.0, f"Sub-floor SoC should give 0 surplus, got {surplus}"

    def test_exact_reserve_equals_soc_gives_zero(self):
        """soc=50%(5 kWh), reserve=5 kWh → surplus = 0, not negative."""
        cfg = self._cfg()
        surplus = energy.export_surplus_kwh(50.0, 5.0, cfg)
        assert surplus == 0.0, f"Expected 0.0 (equal), got {surplus}"

    def test_no_charge_opportunity_reserve_high_surplus_zero(self):
        """When next=None, reserve covers full horizon → surplus should typically be 0.

        soc=60%(6 kWh), reserve=6 kWh (full 12h×500W horizon) → surplus = 0.
        """
        cfg = self._cfg()
        # Reserve was computed as 6 kWh from full-horizon scenario
        surplus = energy.export_surplus_kwh(60.0, 6.0, cfg)
        assert surplus == 0.0, f"Full-horizon reserve should consume available kWh, got {surplus}"


class TestRideOutReserveKwh:
    """energy.ride_out_reserve_kwh — debit-only, trough-anchored, eta_discharge floor."""

    def _cfg(self, **kw) -> Config:
        # 10 kWh pack, floor 10% (1 kWh), eta_d = min(rte/eta_c, 1) = 1.0 by default.
        d = dict(capacity_kwh=10.0, soc_floor=10.0, eta_charge=1.0, round_trip_eff=1.0, max_charge_w=6000.0)
        d.update(kw)
        return Config(**d)

    def _ivs(self, specs):  # specs: list of (pv_w, load_w)
        return [ForecastInterval(T0 + timedelta(hours=i), pv, load, 1.0) for i, (pv, load) in enumerate(specs)]

    def test_floor_stacked_on_rideout(self):
        # 4 h of 500 W deficit then recovery. ride = 2.0 kWh; reserve = floor(1.0)+2.0.
        ivs = self._ivs([(0.0, 500.0)] * 4 + [(3000.0, 300.0)])
        r = energy.ride_out_reserve_kwh(T0, ivs, self._cfg())
        assert r == pytest.approx(3.0, abs=1e-6)  # NOT max(floor, ride)=2.0 (old bug)

    def test_is_cheap_none_equals_legacy(self):
        ivs = self._ivs([(0.0, 500.0)] * 4 + [(3000.0, 300.0)])
        cfg = self._cfg()
        assert energy.ride_out_reserve_kwh(T0, ivs, cfg, is_cheap=None) == energy.ride_out_reserve_kwh(T0, ivs, cfg)

    def test_is_cheap_early_break_bridges_to_cheap_hour(self):
        # 4h of 500W deficit; hour h+2 flagged cheap → walk stops there.
        # Only h0+h1 debit (1.0 kWh) counts, NOT all 4h. reserve = floor(1.0)+1.0.
        ivs = self._ivs([(0.0, 500.0)] * 4 + [(3000.0, 300.0)])
        cheap = {T0 + timedelta(hours=2): True}
        r = energy.ride_out_reserve_kwh(T0, ivs, self._cfg(), is_cheap=cheap)
        assert r == pytest.approx(2.0, abs=1e-6)  # vs 3.0 legacy (test_floor_stacked_on_rideout)

    def test_is_cheap_never_breaks_at_now_hour(self):
        # Current hour flagged cheap must NOT truncate the ride-out (we ride FROM now).
        ivs = self._ivs([(0.0, 500.0)] * 4 + [(3000.0, 300.0)])
        cheap = {T0: True}
        r = energy.ride_out_reserve_kwh(T0, ivs, self._cfg(), is_cheap=cheap)
        assert r == pytest.approx(3.0, abs=1e-6)  # unchanged from legacy

    def test_double_dip_survives_is_cheap_when_blip_not_cheap(self):
        # dawn PV blip then bigger breakfast deficit; no cheap hour → double-dip intact.
        ivs = self._ivs([(0.0, 500.0), (800.0, 500.0), (300.0, 1500.0), (3000.0, 300.0)])
        r = energy.ride_out_reserve_kwh(T0, ivs, self._cfg(), is_cheap={})
        assert r == pytest.approx(1.0 + 1.7, abs=1e-6)

    def test_monotone_glide(self):
        ivs = self._ivs([(0.0, 500.0)] * 4 + [(3000.0, 300.0)])
        cfg = self._cfg()
        r0 = energy.ride_out_reserve_kwh(T0, ivs, cfg)
        r1 = energy.ride_out_reserve_kwh(T0 + timedelta(hours=1), ivs, cfg)
        assert r0 - r1 == pytest.approx(0.5, abs=1e-6)  # one hour of load drops out

    def test_lands_at_floor_at_recovery(self):
        # Starting AT the recovery hour: no forward drawdown → just the floor.
        ivs = self._ivs([(3000.0, 300.0), (3000.0, 300.0)])
        r = energy.ride_out_reserve_kwh(T0, ivs, self._cfg())
        assert r == pytest.approx(1.0, abs=1e-6)  # floor only

    def test_double_dip_includes_later_deficit(self):
        # deficit, brief PV blip (surplus), bigger deficit, real recovery.
        # Trough is AFTER the blip → reserve covers both deficits, not just the first.
        ivs = self._ivs([(0.0, 500.0), (800.0, 500.0), (300.0, 1500.0), (3000.0, 300.0)])
        r = energy.ride_out_reserve_kwh(T0, ivs, self._cfg())
        # debit = 0.5 (h0) + 1.2 (h2); blip h1 contributes 0. reserve = floor + 1.7.
        assert r == pytest.approx(1.0 + 1.7, abs=1e-6)

    def test_eta_discharge_inflates_drawdown(self):
        # round_trip_eff 0.85, eta_charge 1.0 → eta_d 0.85; draw = load / 0.85.
        ivs = self._ivs([(0.0, 500.0)] * 4 + [(3000.0, 300.0)])
        cfg = self._cfg(round_trip_eff=0.85)
        r = energy.ride_out_reserve_kwh(T0, ivs, cfg)
        assert r == pytest.approx(1.0 + 2.0 / 0.85, abs=1e-6)

    def test_capped_at_capacity(self):
        ivs = self._ivs([(0.0, 2000.0)] * 12 + [(6000.0, 0.0)])  # 24 kWh drawdown
        r = energy.ride_out_reserve_kwh(T0, ivs, self._cfg())
        assert r == pytest.approx(10.0, abs=1e-6)  # min(floor + 24, cap=10)

    def test_no_pv_banking_below_trough(self):
        # A huge surplus fully refills mid-window → trough is the first dip only;
        # debit-only/trough never banks the surplus to ZERO out the reserve, and
        # never sums the post-refill deficit that the battery is full enough to cover.
        ivs = self._ivs([(0.0, 500.0), (6000.0, 0.0), (0.0, 500.0), (3000.0, 300.0)])
        r = energy.ride_out_reserve_kwh(T0, ivs, self._cfg())
        assert r == pytest.approx(1.0 + 0.5, abs=1e-6)  # floor + first-hour dip only

    def test_idle_drain_raises_reserve(self):
        # 4 deficit hours then recovery. idle_drain_w adds a constant DC draw on
        # top of the AC-deficit drawdown for every deficit hour in the walk.
        ivs = self._ivs([(0.0, 500.0)] * 4 + [(3000.0, 300.0)])
        deficit_hours = 4
        r0 = energy.ride_out_reserve_kwh(T0, ivs, self._cfg(idle_drain_w=0.0))
        r130 = energy.ride_out_reserve_kwh(T0, ivs, self._cfg(idle_drain_w=130.0))
        expected_delta = 0.130 * deficit_hours
        assert (r130 - r0) == pytest.approx(expected_delta, abs=1e-6)

    def test_reserve_idle_zero_byte_identical(self):
        # idle_drain_w=0.0 must reproduce the pre-change result byte-identically.
        ivs = self._ivs([(0.0, 500.0)] * 4 + [(3000.0, 300.0)])
        cfg_default = self._cfg()
        cfg_explicit_zero = self._cfg(idle_drain_w=0.0)
        assert energy.ride_out_reserve_kwh(T0, ivs, cfg_default) == energy.ride_out_reserve_kwh(
            T0, ivs, cfg_explicit_zero
        )


# ---------------------------------------------------------------------------
# export_net_target_w
# ---------------------------------------------------------------------------

from custom_components.anker_x1_smartgrid.energy import export_net_target_w


def _drain_cfg(**kw):
    base = dict(
        round_trip_eff=0.90, eta_charge=0.95, max_export_w=6000.0, grid_export_limit_w=6000.0, export_drain_window_h=0.0
    )
    base.update(kw)
    return Config(**base)


def test_export_net_target_decisive_caps_at_max():
    # Large surplus, default window (0.0 → one tick) → runs at the export cap.
    assert export_net_target_w(3.0, _drain_cfg(max_export_w=6000.0)) == 6000.0


def test_export_net_target_respects_grid_limit():
    cfg = _drain_cfg(max_export_w=6000.0, grid_export_limit_w=3000.0)
    assert export_net_target_w(3.0, cfg) == 3000.0


def test_export_net_target_tiny_surplus_throttles_below_cap():
    cfg = _drain_cfg()
    eta_d = min(0.90 / 0.95, 1.0)
    expected = 0.05 * eta_d * 1000.0 / (60 / 3600.0)  # TICK_SECONDS = 60
    assert export_net_target_w(0.05, cfg) == pytest.approx(expected)
    assert export_net_target_w(0.05, cfg) < cfg.max_export_w


def test_export_net_target_window_one_hour_is_legacy():
    cfg = _drain_cfg(export_drain_window_h=1.0)
    eta_d = min(0.90 / 0.95, 1.0)
    assert export_net_target_w(1.0, cfg) == pytest.approx(1.0 * eta_d * 1000.0)


def test_export_net_target_zero_surplus_is_zero():
    assert export_net_target_w(0.0, _drain_cfg()) == 0.0


def test_export_net_target_decisive_converges_vs_legacy():
    """Iterating the drain recurrence: the decisive window (0.0) descends at the
    cap and reaches the reserve, while the legacy 1-hour window stalls well above
    it (the exponential tail). Also pins bounded overshoot below reserve."""
    reserve_kwh = 4.0
    tick_h = 60 / 3600.0
    eta_d = min(0.90 / 0.95, 1.0)

    def _simulate(window_h, ticks):
        cfg = _drain_cfg(export_drain_window_h=window_h, max_export_w=6000.0, grid_export_limit_w=6000.0)
        soc_kwh = 9.0
        lowest = soc_kwh
        for _ in range(ticks):
            surplus = max(0.0, soc_kwh - reserve_kwh)
            net_w = export_net_target_w(surplus, cfg)  # AC W
            soc_kwh -= net_w / eta_d / 1000.0 * tick_h  # DC drawn this tick
            lowest = min(lowest, soc_kwh)
        return soc_kwh, lowest

    decisive_soc, decisive_low = _simulate(0.0, 60)  # 60 one-minute ticks
    legacy_soc, _ = _simulate(1.0, 60)

    one_tick_dc = 6000.0 / eta_d / 1000.0 * tick_h  # ~0.105 kWh
    assert decisive_soc == pytest.approx(reserve_kwh, abs=one_tick_dc + 1e-9)
    assert decisive_low >= reserve_kwh - one_tick_dc - 1e-9  # bounded overshoot below reserve
    assert legacy_soc > reserve_kwh + 1.0  # legacy stalls well above reserve


# ---------------------------------------------------------------------------
# eta_curve threading (Task 12) — optional curve param, None branch byte-identical
# ---------------------------------------------------------------------------


def test_reserve_none_is_byte_identical():
    from datetime import datetime
    from custom_components.anker_x1_smartgrid.models import Config, ForecastInterval
    from custom_components.anker_x1_smartgrid.energy import ride_out_reserve_kwh

    cfg = Config(eta_charge=0.92, round_trip_eff=0.85, capacity_kwh=10.0)
    now = datetime(2026, 7, 1, 22, 0, 0)
    ivs = [ForecastInterval(datetime(2026, 7, 1, 22 + i % 2, 0, 0), 0.0, 800.0, 1.0) for i in range(6)]
    assert ride_out_reserve_kwh(now, ivs, cfg) == ride_out_reserve_kwh(now, ivs, cfg, eta_curve=None)


def test_reserve_curve_raises_for_lower_low_power_eta():
    from datetime import datetime
    from custom_components.anker_x1_smartgrid.models import Config, ForecastInterval
    from custom_components.anker_x1_smartgrid.efficiency import EfficiencyCurve, BinStat
    from custom_components.anker_x1_smartgrid.energy import ride_out_reserve_kwh

    cfg = Config(eta_charge=0.92, round_trip_eff=0.85, capacity_kwh=10.0)
    base = EfficiencyCurve.static(cfg)
    disch = list(base._discharge)
    disch[1] = BinStat(disch[1].lo_w, disch[1].hi_w, "discharge", 0.80, 0.80, 99, 9.0, True, "")
    curve = EfficiencyCurve(list(base._charge), disch, base._fc, base._fd)
    now = datetime(2026, 7, 1, 22, 0, 0)
    ivs = [ForecastInterval(datetime(2026, 7, 1, 22, 0, 0), 0.0, 600.0, 4.0)]
    assert ride_out_reserve_kwh(now, ivs, cfg, eta_curve=curve) > ride_out_reserve_kwh(now, ivs, cfg)
