"""Drift gate for ALL vendored forecast_core/ modules (runs in the main CI suite).

The addon vendors byte-identical copies of 8 integration modules via
addon/anker_x1_forecast/sync_core.sh. Any hand-edit or un-synced source edit fails
here. Supersedes the recorder-only gate (test_recorder_vendored_parity.py).
"""
import hashlib
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SOURCE = _ROOT / "custom_components" / "anker_x1_smartgrid"
_VENDOR = _ROOT / "addon" / "anker_x1_forecast" / "forecast_core"
_MANIFEST = _VENDOR / "SOURCE_SHA256"

MODULES = [
    "backtest", "const", "dataquality", "featureset",
    "hgbr", "loadmodel", "recorder", "rollup",
]


def _manifest_entries() -> dict[str, str]:
    entries: dict[str, str] = {}
    for line in _MANIFEST.read_text().splitlines():
        if line.strip():
            sha, name = line.split("  ", 1)
            entries[name.strip()] = sha
    return entries


@pytest.mark.parametrize("module", MODULES)
def test_vendored_is_byte_identical(module):
    src = (_SOURCE / f"{module}.py").read_bytes()
    ven = (_VENDOR / f"{module}.py").read_bytes()
    assert src == ven, f"{module}.py drifted — run ./addon/anker_x1_forecast/sync_core.sh"


@pytest.mark.parametrize("module", MODULES)
def test_manifest_hash_matches_source(module):
    entries = _manifest_entries()
    assert f"{module}.py" in entries, f"{module}.py missing from SOURCE_SHA256"
    actual = hashlib.sha256((_SOURCE / f"{module}.py").read_bytes()).hexdigest()
    assert entries[f"{module}.py"] == actual, (
        f"SOURCE_SHA256 stale for {module}.py — run ./addon/anker_x1_forecast/sync_core.sh"
    )
