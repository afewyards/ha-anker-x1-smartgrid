from datetime import datetime, timezone, UTC
from custom_components.anker_x1_smartgrid import soc_drift
from custom_components.anker_x1_smartgrid.models import ForecastInterval

UTC = UTC


# ── expected (η-aware, DC) ──
def test_expected_charge_applies_eta_charge():
    v = soc_drift.expected_soc_delta_kwh(1000.0, 0.0, 0.1, 0.92, 0.92)
    assert round(v, 6) == round(0.1 * 0.92, 6)


def test_expected_discharge_applies_eta_discharge():
    # deficit -1000W over 0.1h = -0.1 kWh AC → ÷0.92 DC (more negative); reality-anchored
    v = soc_drift.expected_soc_delta_kwh(0.0, 1000.0, 0.1, 0.92, 0.92)
    assert round(v, 6) == round(-0.1 / 0.92, 6)


def test_expected_zero_on_anomalous_dt():
    assert soc_drift.expected_soc_delta_kwh(1000.0, 0.0, 0.0, 0.92, 0.92) == 0.0
    assert soc_drift.expected_soc_delta_kwh(1000.0, 0.0, 5.0, 0.92, 0.92) == 0.0  # > MAX


def test_expected_delta_subtracts_idle_on_deficit():
    # deficit -1000W over 0.1h = -0.1 kWh AC → ÷0.92 DC, minus idle drain (130W over 0.1h)
    v = soc_drift.expected_soc_delta_kwh(0.0, 1000.0, 0.1, 0.92, 0.92, idle_drain_w=130.0)
    assert round(v, 6) == round(-0.1 / 0.92 - 130.0 * 0.1 / 1000.0, 6)


def test_expected_delta_idle_default_zero_parity():
    # Omitting idle_drain_w must reproduce the pre-idle-drain values exactly (byte-identical).
    v_no_kwarg = soc_drift.expected_soc_delta_kwh(0.0, 1000.0, 0.1, 0.92, 0.92)
    v_explicit_zero = soc_drift.expected_soc_delta_kwh(0.0, 1000.0, 0.1, 0.92, 0.92, idle_drain_w=0.0)
    assert v_no_kwarg == v_explicit_zero == -0.1 / 0.92


def test_expected_delta_surplus_branch_unaffected_by_idle():
    # Surplus (charge) branch must ignore idle_drain_w entirely.
    v_with_idle = soc_drift.expected_soc_delta_kwh(1000.0, 0.0, 0.1, 0.92, 0.92, idle_drain_w=130.0)
    v_without_idle = soc_drift.expected_soc_delta_kwh(1000.0, 0.0, 0.1, 0.92, 0.92)
    assert v_with_idle == v_without_idle
    assert round(v_with_idle, 6) == round(0.1 * 0.92, 6)


# ── measured (DC from % SoC) ──
def test_measured_delta_from_soc_pct():
    assert soc_drift.measured_soc_delta_kwh(60.0, 55.0, 10.0) == 0.5
    assert soc_drift.measured_soc_delta_kwh(55.0, 60.0, 10.0) == -0.5


# ── per-step drift (expected − measured), export add-back ──
def test_per_step_positive_when_reality_under_delivers():
    assert round(soc_drift.per_step_drift_kwh(0.092, 0.0), 6) == 0.092


def test_per_step_negative_when_grid_charge_recovers():
    assert soc_drift.per_step_drift_kwh(0.0, 0.5) == -0.5


def test_per_step_export_corrected_to_neutral():
    assert soc_drift.per_step_drift_kwh(0.0, -0.5, 0.5) == 0.0


def test_per_step_export_correction_preserves_real_shortfall():
    assert round(soc_drift.per_step_drift_kwh(0.3, -0.5, 0.5), 6) == 0.3


# ── accumulator / decay / cap / reset ──
def test_decay_off_by_default():
    assert soc_drift.decay(2.0, 0.5, 0.0) == 2.0


def test_decay_halflife():
    assert soc_drift.decay(2.0, 1.0, 1.0) == 1.0


def test_accumulate_adds_after_decay():
    assert soc_drift.accumulate(1.0, 0.5, dt_h=1.0, halflife_h=1.0) == 1.0


def test_accumulate_no_decay_default():
    assert soc_drift.accumulate(1.0, 0.5) == 1.5


def test_cap_accumulator_absolute_bound():
    assert soc_drift.cap_accumulator(12.0, 10.0) == 10.0  # runaway sanity bound
    assert soc_drift.cap_accumulator(3.0, 10.0) == 3.0
    assert soc_drift.cap_accumulator(-5.0, 10.0) == -5.0  # negatives pass (behind-only handled later)


def test_reset_on_new_day():
    assert soc_drift.reset_if_new_day(3.0, "2026-06-28", "2026-06-29") == (0.0, "2026-06-29")
    assert soc_drift.reset_if_new_day(3.0, "2026-06-29", "2026-06-29") == (3.0, "2026-06-29")
    assert soc_drift.reset_if_new_day(3.0, None, "2026-06-29") == (0.0, "2026-06-29")


# ── drift: behind-only + two-level hysteresis ──
def test_drift_behind_only():
    assert soc_drift.drift_kwh(-1.0, 0.3, 0.15, False) == (0.0, False)


def test_drift_engages_above_deadband():
    assert soc_drift.drift_kwh(0.9, 0.3, 0.15, False) == (0.9, True)


def test_drift_below_engage_stays_off():
    assert soc_drift.drift_kwh(0.2, 0.3, 0.15, False) == (0.0, False)


def test_drift_hysteresis_holds_between_release_and_engage():
    # once engaged, stays on down to the release band (0.2 is < engage 0.3 but >= release 0.15)
    assert soc_drift.drift_kwh(0.2, 0.3, 0.15, True) == (0.2, True)


def test_drift_releases_below_release_band():
    assert soc_drift.drift_kwh(0.1, 0.3, 0.15, True) == (0.0, False)


# ── forecast_rate_at: same source as the DP window ──
def test_forecast_rate_at_picks_covering_interval():
    ivs = [
        ForecastInterval(datetime(2026, 6, 29, 10, tzinfo=UTC), 2000.0, 400.0, 1.0),
        ForecastInterval(datetime(2026, 6, 29, 11, tzinfo=UTC), 3000.0, 500.0, 1.0),
    ]
    assert soc_drift.forecast_rate_at(ivs, datetime(2026, 6, 29, 10, 30, tzinfo=UTC)) == (2000.0, 400.0)
    assert soc_drift.forecast_rate_at(ivs, datetime(2026, 6, 29, 11, 5, tzinfo=UTC)) == (3000.0, 500.0)


def test_forecast_rate_at_no_cover_returns_zero():
    assert soc_drift.forecast_rate_at([], datetime(2026, 6, 29, 10, tzinfo=UTC)) == (0.0, 0.0)
    assert soc_drift.forecast_rate_at(None, datetime(2026, 6, 29, 10, tzinfo=UTC)) == (0.0, 0.0)
