"""Structural regression tests for the Lovelace plan card.

The card's logic lives in JS strings inside YAML, so there is no runtime to
assert against here. These tests pin the structural invariants that the
2026-08-01 tariff-crop change establishes, and the fixture contract that the
JS filter predicate relies on.

Spec: docs/superpowers/specs/2026-08-01-card-crop-daily-stats-design.md
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]
CARD_PATH = _ROOT / "lovelace" / "apexcharts-plan-card.yaml"
FIXTURE_PATH = _ROOT / "tests" / "fixtures" / "plan-sensor-2026-08-01-postpub.json"


def _card() -> dict:
    return yaml.safe_load(CARD_PATH.read_text(encoding="utf-8"))


def _horizon() -> list[dict]:
    blob = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return blob.get("attributes", blob)["horizon"]


def test_no_estimated_tail_series_remain():
    names = [s["name"] for s in _card()["card"]["series"]]
    assert [n for n in names if "(est)" in n] == [], names


def test_every_series_filters_estimated_rows():
    # The 3 column series (Grid charge / Solar charge / Grid export) used to
    # map the whole horizon while the line series filtered — bars rendered
    # inside the estimated region.
    for series in _card()["card"]["series"]:
        assert "!h.estimated" in series["data_generator"], series["name"]


def test_graph_span_reads_the_filtered_horizon():
    card = _card()
    assert "HR" in card["variables"], card["variables"].keys()
    assert "!h.estimated" in card["variables"]["HR"]
    assert "HR[HR.length - 1]" in card["card"]["graph_span"]
    assert "H[H.length - 1]" not in card["card"]["graph_span"]


def test_apex_fill_and_stroke_arrays_match_series_count():
    # Both arrays are positional per-series; a stale length silently
    # mis-styles every series past the mismatch.
    card = _card()
    n = len(card["card"]["series"])
    assert len(card["card"]["apex_config"]["fill"]["type"]) == n
    assert len(card["card"]["apex_config"]["stroke"]["dashArray"]) == n


def test_fixture_filter_predicate_crops_thirteen_hours():
    # Pins the data contract the JS `!h.estimated` predicate depends on:
    # real tariff ends 21:45Z, the raw horizon runs 13h further.
    horizon = _horizon()
    real = [h for h in horizon if not h["estimated"]]
    assert real[-1]["start"] == "2026-08-02T21:45:00+00:00"
    assert horizon[-1]["start"] == "2026-08-03T10:00:00+00:00"
    assert len(horizon) - len(real) == 13
