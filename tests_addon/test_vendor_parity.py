"""
test_vendor_parity.py

Drift guard for vendored forecast_core/ modules.

Checks:
  1. Byte-identical parity between vendored copies and source originals.
  2. SOURCE_SHA256 manifest matches source file hashes.
  3. None of the vendored files contain `import homeassistant` / `from homeassistant`.
  4. Import smoke-test: each vendored module imports cleanly in this venv.
"""
from __future__ import annotations

import hashlib
import importlib
import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SOURCE_DIR = _REPO_ROOT / "custom_components" / "anker_x1_smartgrid"
_VENDOR_DIR = _REPO_ROOT / "addon" / "anker_x1_forecast" / "forecast_core"
_MANIFEST = _VENDOR_DIR / "SOURCE_SHA256"

MODULES = [
    "const",
    "dataquality",
    "rollup",
    "loadmodel",
    "featureset",
    "recorder",
    "hgbr",
    "backtest",
]


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------
def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# 1. Byte-identical parity
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("module", MODULES)
def test_vendored_file_is_byte_identical_to_source(module: str) -> None:
    source = _SOURCE_DIR / f"{module}.py"
    vendored = _VENDOR_DIR / f"{module}.py"

    assert source.exists(), f"Source file missing: {source}"
    assert vendored.exists(), f"Vendored file missing — run sync_core.sh: {vendored}"

    source_bytes = source.read_bytes()
    vendored_bytes = vendored.read_bytes()

    assert source_bytes == vendored_bytes, (
        f"{module}.py has drifted from source. Run ./sync_core.sh to re-sync."
    )


# ---------------------------------------------------------------------------
# 2. SOURCE_SHA256 manifest matches source hashes
# ---------------------------------------------------------------------------
def test_manifest_exists() -> None:
    assert _MANIFEST.exists(), (
        f"SOURCE_SHA256 manifest missing — run sync_core.sh: {_MANIFEST}"
    )


def test_manifest_covers_all_modules() -> None:
    assert _MANIFEST.exists(), "SOURCE_SHA256 manifest missing"
    recorded: dict[str, str] = {}
    for line in _MANIFEST.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        sha, filename = line.split("  ", 1)
        recorded[filename] = sha
    for module in MODULES:
        assert f"{module}.py" in recorded, (
            f"{module}.py not found in SOURCE_SHA256 manifest"
        )


@pytest.mark.parametrize("module", MODULES)
def test_manifest_hash_matches_source(module: str) -> None:
    assert _MANIFEST.exists(), "SOURCE_SHA256 manifest missing"

    recorded: dict[str, str] = {}
    for line in _MANIFEST.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        sha, filename = line.split("  ", 1)
        recorded[filename] = sha

    filename = f"{module}.py"
    assert filename in recorded, f"{filename} not in SOURCE_SHA256 manifest"

    source = _SOURCE_DIR / filename
    assert source.exists(), f"Source file missing: {source}"

    actual = _sha256(source)
    assert recorded[filename] == actual, (
        f"SOURCE_SHA256 entry for {filename} is stale. "
        f"Expected {actual}, got {recorded[filename]}. Run ./sync_core.sh."
    )


# ---------------------------------------------------------------------------
# 3. No homeassistant imports in vendored files
# ---------------------------------------------------------------------------
_HA_IMPORT_PATTERN = re.compile(
    r"^(import homeassistant|from homeassistant)", re.MULTILINE
)


@pytest.mark.parametrize("module", MODULES)
def test_vendored_file_has_no_homeassistant_import(module: str) -> None:
    vendored = _VENDOR_DIR / f"{module}.py"
    assert vendored.exists(), f"Vendored file missing: {vendored}"

    text = vendored.read_text(encoding="utf-8")
    matches = _HA_IMPORT_PATTERN.findall(text)
    assert not matches, (
        f"{module}.py contains homeassistant imports: {matches}"
    )


# ---------------------------------------------------------------------------
# 4. Import smoke-test (confirms import closure resolves in .venv)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("module", MODULES)
def test_vendored_module_imports(module: str) -> None:
    """Each vendored module must import cleanly."""
    importlib.import_module(f"forecast_core.{module}")
