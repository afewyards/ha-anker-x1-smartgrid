"""Tests for custom_components/anker_x1_smartgrid/manifest.json integrity.

NOTE: scikit-learn is intentionally NOT in requirements. There is no cp314/musl/aarch64
wheel and HA's sandbox blocks the source build, which aborts integration setup on the
deploy box. sklearn is lazy-imported in hgbr.py with a BucketedLoadModel/profile fallback.
"""
import json
from pathlib import Path

MANIFEST_PATH = Path(__file__).parent.parent / "custom_components" / "anker_x1_smartgrid" / "manifest.json"


def load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text())


def test_manifest_does_not_require_scikit_learn():
    reqs = load_manifest()["requirements"]
    assert not any("scikit-learn" in r for r in reqs), (
        "scikit-learn must NOT be a manifest requirement — no cp314/musl wheel exists and "
        "HA's sandbox blocks the source build, which aborts setup. "
        "sklearn is lazy-imported with a bucketed/profile fallback instead."
    )


def test_manifest_contains_holidays():
    reqs = load_manifest()["requirements"]
    assert any("holidays" in r for r in reqs), (
        f"holidays not found in requirements: {reqs}"
    )


def test_manifest_does_not_pin_numpy():
    reqs = load_manifest()["requirements"]
    numpy_entries = [r for r in reqs if r.startswith("numpy")]
    assert not numpy_entries, (
        f"numpy must not be pinned in requirements (HA ships its own): {numpy_entries}"
    )


def test_manifest_declares_single_config_entry():
    manifest = load_manifest()
    assert manifest.get("single_config_entry") is True
