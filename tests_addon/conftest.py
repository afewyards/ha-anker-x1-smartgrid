"""
conftest.py for tests_addon/

Inserts addon/anker_x1_forecast onto sys.path so `import forecast_core` resolves.
Intentionally does NOT import pytest_homeassistant_custom_component — that plugin
corrupts sys.modules for late sklearn imports, which is why these tests live in
tests_addon/ rather than tests/.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Repo root is two levels up from this file (tests_addon/conftest.py → repo root)
_REPO_ROOT = Path(__file__).resolve().parent.parent
_ADDON_PATH = str(_REPO_ROOT / "addon" / "anker_x1_forecast")

if _ADDON_PATH not in sys.path:
    sys.path.insert(0, _ADDON_PATH)
