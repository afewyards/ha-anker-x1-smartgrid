import re
from pathlib import Path

import pytest

from custom_components.anker_x1_smartgrid import money

PKG = Path(money.__file__).parent
EUR_MODULES = {"money", "margin", "usecost", "carddebug", "daily_stats", "why", "ledger"}


def test_grid_eur_bought_only():
    assert money.grid_eur((2.0, 0.0), 0.25, None) == 0.5


def test_grid_eur_sold_without_export_price_is_none():
    assert money.grid_eur((0.0, 1.0), 0.25, None) is None


def test_grid_eur_no_kwh_is_none():
    assert money.grid_eur(None, 0.25, 0.1) is None


def test_grid_eur_bought_less_sold():
    assert money.grid_eur((1.0, 2.0), 0.30, 0.10) == pytest.approx(0.10)


def test_no_cross_module_private_imports():
    found = []
    for name in EUR_MODULES:
        for m in re.finditer(r"^from \.(\w+) import (\([^)]*\)|.*)$", (PKG / f"{name}.py").read_text(), re.M):
            if m.group(1) in EUR_MODULES and re.search(r"\b_[A-Za-z]", m.group(2)):
                found.append(f"{name}: {m.group(0)}")
    assert not found, found
