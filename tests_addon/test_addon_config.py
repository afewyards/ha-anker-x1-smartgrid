from pathlib import Path


def test_config_has_health_watchdog():
    cfg = (Path(__file__).resolve().parent.parent
           / "addon" / "anker_x1_forecast" / "config.yaml").read_text()
    assert "watchdog:" in cfg
    assert "8099" in cfg and "/health" in cfg
