from pathlib import Path


def test_config_has_health_watchdog():
    cfg = (Path(__file__).resolve().parent.parent / "addon" / "anker_x1_forecast" / "config.yaml").read_text()
    assert "watchdog:" in cfg
    assert "8099" in cfg and "/health" in cfg


def test_config_has_train_since_option_and_schema():
    cfg = (Path(__file__).resolve().parent.parent / "addon" / "anker_x1_forecast" / "config.yaml").read_text()
    assert 'train_since: ""' in cfg
    assert 'train_since: "str?"' in cfg
