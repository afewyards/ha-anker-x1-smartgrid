from custom_components.anker_x1_smartgrid.models import Config


def test_phase2_config_defaults():
    cfg = Config()
    assert cfg.use_learned_model is True
    assert cfg.retrain_hours == 24
    assert cfg.min_train_samples == 2000
    assert cfg.train_days == 14
    assert cfg.backtest_test_days == 3


def test_phase2_config_override():
    cfg = Config.from_dict({"use_learned_model": False, "min_train_samples": 500})
    assert cfg.use_learned_model is False
    assert cfg.min_train_samples == 500
