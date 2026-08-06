"""Structural regression tests for the Lovelace plan card.

The card's logic lives in JS strings inside YAML, so there is no runtime to
assert against here. These tests pin the structural invariants that the
2026-08-01 tariff-crop change establishes, and the fixture contract that the
JS filter predicate relies on.

Spec: docs/superpowers/specs/2026-08-01-card-crop-daily-stats-design.md
"""

from __future__ import annotations

import json
import re
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


def test_positional_style_arrays_match_series_count():
    """apex_config.fill.type and stroke.dashArray are POSITIONAL — one entry
    per series, in order. A series added without extending both silently
    shifts every style after the insertion point onto the wrong series."""
    card = _card()["card"]
    n = len(card["series"])
    assert len(card["apex_config"]["fill"]["type"]) == n
    assert len(card["apex_config"]["stroke"]["dashArray"]) == n


def test_apex_config_declares_annotations_exactly_once():
    """YAML last-key-wins silently discards an earlier duplicate, which is how
    the calibration window shading first shipped as a no-op."""
    text = CARD_PATH.read_text(encoding="utf-8")
    assert len(re.findall(r"^    annotations:", text, re.M)) == 1


def test_no_estimated_tail_series_remain():
    names = [s["name"] for s in _card()["card"]["series"]]
    assert [n for n in names if "(est)" in n] == [], names


def test_every_horizon_series_filters_estimated_rows():
    # The 3 column series (Grid charge / Solar charge / Grid export) used to
    # map the whole horizon while the line series filtered — bars rendered
    # inside the estimated region.
    #
    # Scoped to series that actually READ the horizon. The calibration band
    # edges are drawn from the calibration_* attributes (two points, window
    # start and end) and never touch a horizon row, so the filter is not just
    # unnecessary there but impossible to express.
    for series in _card()["card"]["series"]:
        gen = series["data_generator"]
        if "attributes.horizon" not in gen:
            continue
        assert "!h.estimated" in gen, series["name"]


def test_calibration_series_take_values_from_attributes_not_horizon_fields():
    """Calibration is quarantined outside the DP, so no horizon row carries
    it. The band's VALUES must come from the calibration_* attributes; if one
    ever read an h.<field> value it would mean the override had leaked into
    the plan the DP produces."""
    cal = [s for s in _card()["card"]["series"] if s["name"].startswith("Calib")]
    assert len(cal) == 2, [s["name"] for s in _card()["card"]["series"]]
    for series in cal:
        gen = series["data_generator"]
        assert "calibration_window_start" in gen, series["name"]
        assert "a.calibration_target_soc" in gen or "a.calibration_hold_soc" in gen, series["name"]


def test_calibration_series_span_the_full_horizon_x_array():
    """They must map the whole (non-estimated) horizon and null out the rows
    outside the window, NOT emit two points at the window edges. This is a
    mixed column+line chart: a series whose x-array differs from the others
    does not render against its own timestamps — the two-point version drew
    the band stretching past the window to the right edge of the chart."""
    for series in [s for s in _card()["card"]["series"] if s["name"].startswith("Calib")]:
        gen = series["data_generator"]
        assert "attributes.horizon" in gen or "a.horizon" in gen, series["name"]
        assert ".map(" in gen, series["name"]
        assert "null" in gen, f"{series['name']} must null out rows outside the window"


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
