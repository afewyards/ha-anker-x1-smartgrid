"""Controller wiring for the occupancy corrector (Layer B)."""
from datetime import datetime, timezone

from custom_components.anker_x1_smartgrid import occupancy
from custom_components.anker_x1_smartgrid.controller import Controller
from custom_components.anker_x1_smartgrid.load_adapt import PredictionLog
from custom_components.anker_x1_smartgrid.models import Config
from custom_components.anker_x1_smartgrid.occupancy import OccupancyPredictor

NOW = datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc)  # Wed 14:00 CEST → band 2, weekday


class _StubPredictor:
    def __init__(self, base_w=1000.0):
        self.base_w = base_w
        self.calls = []

    def predict(self, when, temp, fallback_w, *, quantile=0.5):
        self.calls.append((when, temp, fallback_w, quantile))
        return self.base_w


def _make_ctl(cfg_overrides: dict) -> Controller:
    """Bare controller via __new__ (adapted from tests/test_controller_load_adapt.py:_make_ctl)."""
    ctl = Controller.__new__(Controller)
    ctl.cfg = Config.from_dict(cfg_overrides)
    ctl.predictor = _StubPredictor()
    ctl.active_model_name = "profile"
    ctl._load_adapt_log = PredictionLog()
    ctl._load_adapt_ratio = None
    ctl._load_adapt_matched = 0
    ctl._occ_table = None
    ctl._persons_home_now = None
    return ctl


def test_default_fraction_returns_base_unchanged():
    ctl = _make_ctl({})  # occ_adapt_fraction defaults to 0.0
    ctl._occ_table = occupancy.OccupancyTable({}, {}, {}, 0)
    ctl._persons_home_now = 2
    pred = ctl._update_load_adapt(NOW, 20.0, {})
    assert not isinstance(pred, OccupancyPredictor)  # parity: no wrapper at default


def test_fraction_on_wraps_base_with_occupancy():
    ctl = _make_ctl({"occ_adapt_fraction": 1.0})
    ctl._occ_table = occupancy.OccupancyTable(
        {(2, False, 0): (300.0, 25), (2, False, 1): (500.0, 25)}, {}, {(2, False): 1}, 2,
    )
    ctl._persons_home_now = 0
    pred = ctl._update_load_adapt(NOW, 20.0, {})
    # away vs climo 1 → 0.6 × base
    base_w = ctl.predictor.predict(NOW, 20.0, 250.0, quantile=0.5)
    assert abs(pred.predict(NOW, 20.0, 250.0) - base_w * 0.6) < 1e-6


def test_remote_tier_guard_skips_occupancy():
    ctl = _make_ctl({"occ_adapt_fraction": 1.0})
    ctl._occ_table = occupancy.OccupancyTable(
        {(2, False, 0): (300.0, 25), (2, False, 1): (500.0, 25)}, {}, {(2, False): 1}, 2,
    )
    ctl._persons_home_now = 0
    ctl.active_model_name = "remote"
    pred = ctl._update_load_adapt(NOW, 20.0, {})
    assert not isinstance(pred, OccupancyPredictor)


def test_load_adapt_log_records_occ_corrected_p50():
    ctl = _make_ctl({"occ_adapt_fraction": 1.0, "load_adapt_fraction": 0.7})
    ctl._occ_table = occupancy.OccupancyTable(
        {(2, False, 0): (300.0, 25), (2, False, 1): (500.0, 25)}, {}, {(2, False): 1}, 2,
    )
    ctl._persons_home_now = 0
    base_w = ctl.predictor.predict(NOW, 20.0, 250.0, quantile=0.5)
    ctl._update_load_adapt(NOW, 20.0, {})
    assert abs(ctl._load_adapt_log.get(NOW) - base_w * 0.6) < 1e-6


def test_status_attrs_exposed():
    ctl = _make_ctl({"occ_adapt_fraction": 1.0})
    ctl._occ_table = occupancy.OccupancyTable(
        {(2, False, 0): (300.0, 25), (2, False, 1): (500.0, 25)}, {}, {(2, False): 1}, 2,
    )
    ctl._persons_home_now = 0
    attrs = ctl._occ_status_attrs(NOW)
    assert attrs["occ_state_now"] == 0
    assert attrs["occ_expected_state"] == 1
    assert abs(attrs["occ_multiplier"] - 0.6) < 1e-6
    assert attrs["occ_cells_ready"] == 2
