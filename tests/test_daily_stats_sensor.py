"""Sensor exposure of the per-day statistics table.

Spec: docs/superpowers/specs/2026-08-01-card-crop-daily-stats-design.md
"""

from __future__ import annotations

import pytest

from custom_components.anker_x1_smartgrid import daily_stats
from custom_components.anker_x1_smartgrid.sensor import X1DailyStatsSensor


class _StubController:
    def __init__(self, status):
        self.last_status = status


def _rows():
    return [
        {"date": "2026-07-31", "grid_charge_kwh": 4.0, "grid_export_kwh": 2.0, "net_eur": -0.6, "source": "actual"},
        {"date": "2026-08-01", "grid_charge_kwh": 3.0, "grid_export_kwh": 5.0, "net_eur": 0.95, "source": "mixed"},
    ]


def test_state_is_the_row_count():
    sensor = X1DailyStatsSensor(_StubController({"daily_stats": _rows()}), "entry")
    assert sensor.native_value == 2


def test_attributes_carry_the_table_and_the_window():
    sensor = X1DailyStatsSensor(_StubController({"daily_stats": _rows()}), "entry")
    attrs = sensor.extra_state_attributes
    assert attrs["days"] == _rows()
    assert attrs["window_days"] == daily_stats.WINDOW_DAYS


def test_missing_table_is_zero_rows_not_a_crash():
    # First tick after a restart, before _publish_daily_stats has run.
    sensor = X1DailyStatsSensor(_StubController({}), "entry")
    assert sensor.native_value == 0
    assert sensor.extra_state_attributes["days"] == []


def test_days_blob_is_not_recorded():
    assert "days" in X1DailyStatsSensor._unrecorded_attributes
