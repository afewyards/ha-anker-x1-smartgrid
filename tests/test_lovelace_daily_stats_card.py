"""Structural regression tests for the Lovelace daily-stats card.

The card's logic lives in a Jinja template inside YAML, so there is no runtime
to assert against here — these tests pin the column contract against the keys
``daily_stats.merge_days`` actually emits.

Spec: docs/superpowers/specs/2026-08-24-house-wide-energy-accounting-design.md
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import yaml

from custom_components.anker_x1_smartgrid import daily_stats

_ROOT = Path(__file__).resolve().parents[1]
CARD_PATH = _ROOT / "lovelace" / "daily-stats-card.yaml"


def _content() -> str:
    return yaml.safe_load(CARD_PATH.read_text(encoding="utf-8"))["content"]


def test_card_renders_the_battery_basis_columns():
    content = _content()
    for key in ("grid_charge_kwh", "grid_export_kwh", "net_eur"):
        assert key in content


def test_card_renders_the_house_columns():
    content = _content()
    for key in ("house_import_kwh", "house_export_kwh", "house_net_eur"):
        assert key in content


def test_every_referenced_key_is_one_merge_days_emits():
    # Guards against a typo'd d.<key> silently rendering blank in Lovelace.
    emitted = set(daily_stats.merge_days({}, {}, daily_stats.new_day_totals(), date(2026, 7, 20))[0])
    for key in set(re.findall(r"\bd\.([a-z_]+)", _content())):
        assert key in emitted, f"card references d.{key}, which merge_days does not emit"
