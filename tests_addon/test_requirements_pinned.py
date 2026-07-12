"""A1: guard that the addon's requirements.txt pins stay exact.

CI (.github/workflows/tests.yml) installs scikit-learn/holidays for the addon
test step with explicit versions; this test pins the invariant so a drift in
addon/anker_x1_forecast/requirements.txt is caught instead of silently
diverging from what CI actually installs.
"""

from __future__ import annotations


def test_addon_requirements_pinned_exact():
    from pathlib import Path

    req = (Path(__file__).resolve().parent.parent / "addon" / "anker_x1_forecast" / "requirements.txt").read_text()
    assert "scikit-learn==1.5.2" in req
    assert "holidays==0.99" in req
