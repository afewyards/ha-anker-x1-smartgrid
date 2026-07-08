"""Unit tests for controller status sensors."""
from __future__ import annotations


def test_export_setpoint_sensor_reads_key():
    from custom_components.anker_x1_smartgrid.sensor import X1ExportSetpointSensor

    class _C:
        last_status = {"export_setpoint_w": 1500.0}

    s = X1ExportSetpointSensor(_C(), "e")
    assert s.native_value == 1500.0
