def test_addon_version_matches_integration():
    import json, re
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    proj_ver = re.search(r'^version = "([^"]+)"', (root / "pyproject.toml").read_text(), re.M).group(1)
    manifest = json.loads((root / "custom_components" / "anker_x1_smartgrid" / "manifest.json").read_text())
    addon_ver = re.search(r'^version:\s*"([^"]+)"',
                          (root / "addon" / "anker_x1_forecast" / "config.yaml").read_text(), re.M).group(1)
    assert manifest["version"] == proj_ver == addon_ver
