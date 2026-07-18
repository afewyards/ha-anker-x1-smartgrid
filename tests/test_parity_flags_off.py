"""All new flags at defaults → predictor chain identical to pre-feature main."""

from datetime import datetime, timezone, UTC

from custom_components.anker_x1_smartgrid.controller import Controller
from custom_components.anker_x1_smartgrid.intra_hour import CurrentHourBlendPredictor, HourAccumulator
from custom_components.anker_x1_smartgrid.load_adapt import PredictionLog
from custom_components.anker_x1_smartgrid.models import Config
from custom_components.anker_x1_smartgrid.occupancy import OccupancyPredictor

NOW = datetime(2026, 6, 3, 12, 0, tzinfo=UTC)


class _StubPredictor:
    def __init__(self, base_w=1000.0):
        self.base_w = base_w
        self.calls = []

    def predict(self, when, temp, fallback_w, *, quantile=0.5):
        self.calls.append((when, temp, fallback_w, quantile))
        return self.base_w


def _make_ctl(cfg_overrides: dict) -> Controller:
    """Bare controller via __new__ (adapted from tests/test_controller_blend.py:_make_ctl)."""
    ctl = Controller.__new__(Controller)
    ctl.cfg = Config.from_dict(cfg_overrides)
    ctl.predictor = _StubPredictor()
    ctl.active_model_name = "profile"
    ctl._load_adapt_log = PredictionLog()
    ctl._load_adapt_ratio = None
    ctl._load_adapt_matched = 0
    ctl._occ_table = None
    ctl._persons_home_now = None
    ctl._hour_acc = HourAccumulator()
    return ctl


def test_default_config_predictor_chain_unchanged():
    ctl = _make_ctl({})  # every new key at its default
    ctl._persons_home_now = 2
    pred = ctl._update_load_adapt(NOW, 20.0, {})
    assert not isinstance(pred, (OccupancyPredictor, CurrentHourBlendPredictor))
    # with no matched hours the method returns the base tier itself
    assert pred is ctl.predictor


def test_default_config_fields_are_off():
    # Introspect dataclass defaults (robust even if Config gains required fields)
    f = Config.__dataclass_fields__
    assert f["occ_adapt_fraction"].default == 0.0
    assert f["occ_persistence_h"].default == 4
    assert f["current_hour_blend"].default is False
    assert f["load_adapt_partial_hour"].default is False
    # Acknowledged exception: terminal_overnight_credit is the first default-ON
    # flag (two-segment terminal water value corrects DP behavior; there is no
    # byte-identical "off" state to default to). See const.DEFAULT_TERMINAL_OVERNIGHT_CREDIT.
    assert f["terminal_overnight_credit"].default is True
