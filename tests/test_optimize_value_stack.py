"""Tests for export value-stack gate functions (C1).

Worked example (from plan §C1):
  round_trip_eff=0.85, eta_charge=0.95
  => eta_discharge = 0.85 / 0.95 ≈ 0.8947...

  trough_price=0.15 => keep_value = compute_water_value(0.15, cfg)
                     = (0.15 / 0.95) * 1.0 ≈ 0.15789...

  cycle_cost=0.04
"""

import pytest

from custom_components.anker_x1_smartgrid.models import Config
from custom_components.anker_x1_smartgrid.optimize import (
    eta_discharge,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cfg_worked() -> Config:
    """Config matching the §C1 worked example."""
    return Config(
        round_trip_eff=0.85,
        eta_charge=0.95,
        cycle_cost_eur_per_kwh=0.04,
    )


# ---------------------------------------------------------------------------
# eta_discharge
# ---------------------------------------------------------------------------


class TestEtaDischarge:
    def test_basic_worked_example(self, cfg_worked):
        """round_trip 0.85 / eta_charge 0.95 ≈ 0.8947."""
        result = eta_discharge(cfg_worked)
        assert result == pytest.approx(0.85 / 0.95, rel=1e-9)

    def test_symmetric_eff(self):
        """round_trip=0.81, eta_charge=0.9 => eta_discharge=0.9."""
        cfg = Config(round_trip_eff=0.81, eta_charge=0.9)
        assert eta_discharge(cfg) == pytest.approx(0.9, rel=1e-9)

    def test_eta_charge_zero_guard(self):
        """eta_charge=0 → guard substitutes 1.0, result = round_trip_eff = 0.85."""
        cfg = Config(round_trip_eff=0.85, eta_charge=0.0)
        result = eta_discharge(cfg)
        assert result == pytest.approx(0.85)

    def test_eta_discharge_clamped_to_one(self):
        """eta_charge < round_trip_eff is a misconfiguration; result is clamped to 1.0."""
        cfg = Config(round_trip_eff=0.85, eta_charge=0.5)  # would give 1.7 without clamp
        assert eta_discharge(cfg) == pytest.approx(1.0)

    def test_returns_float(self, cfg_worked):
        assert isinstance(eta_discharge(cfg_worked), float)
