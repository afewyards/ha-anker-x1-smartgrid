from pathlib import Path


def test_config_has_health_watchdog():
    cfg = (Path(__file__).resolve().parent.parent / "addon" / "anker_x1_forecast" / "config.yaml").read_text()
    assert "watchdog:" in cfg
    assert "8099" in cfg and "/health" in cfg


def test_config_has_train_since_option_and_schema():
    cfg = (Path(__file__).resolve().parent.parent / "addon" / "anker_x1_forecast" / "config.yaml").read_text()
    assert 'train_since: ""' in cfg
    assert 'train_since: "str?"' in cfg


def test_config_has_recency_and_prior_options_and_schema():
    cfg = (Path(__file__).resolve().parent.parent / "addon" / "anker_x1_forecast" / "config.yaml").read_text()
    assert "  half_life_days: 7\n" in cfg
    assert "  prior_weight: 0.25\n" in cfg
    assert 'half_life_days: "float(0,)"' in cfg
    assert 'prior_weight: "float(0,1)"' in cfg
