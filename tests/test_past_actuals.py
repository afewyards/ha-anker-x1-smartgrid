from datetime import datetime, timezone

from custom_components.anker_x1_smartgrid.past_actuals import aggregate_past_actuals


def _ts(h, m=0):
    return datetime(2026, 6, 29, h, m, tzinfo=timezone.utc).isoformat()


def test_solar_first_split_when_pv_surplus_covers_charge():
    # PV 2000, load 200 -> surplus 1800; charging 1000 (batt_w -1000) -> all solar.
    rows = [{"ts": _ts(10), "pv_w": 2000.0, "load_w": 200.0, "batt_w": -1000.0, "p1_w": 50.0, "soc": 40.0}]
    out = aggregate_past_actuals(rows)
    hour = datetime(2026, 6, 29, 10, tzinfo=timezone.utc)
    rec = out[hour]
    assert rec["solar_charge_w"] == 1000.0
    assert rec["grid_charge_w"] == 0.0
    assert rec["pv_w"] == 2000.0
    assert rec["load_w"] == 200.0
    assert rec["soc"] == 40.0
    assert rec["grid_export_w"] == 0.0  # p1_w positive = import


def test_charge_exceeding_surplus_spills_to_grid():
    # PV 500, load 200 -> surplus 300; charging 1000 -> 300 solar + 700 grid.
    rows = [{"ts": _ts(9), "pv_w": 500.0, "load_w": 200.0, "batt_w": -1000.0, "p1_w": 700.0, "soc": 20.0}]
    rec = aggregate_past_actuals(rows)[datetime(2026, 6, 29, 9, tzinfo=timezone.utc)]
    assert rec["solar_charge_w"] == 300.0
    assert rec["grid_charge_w"] == 700.0


def test_night_hour_pv_zero_no_solar_charge():
    # pv_w NULL (night), discharging (batt_w +300), importing.
    rows = [{"ts": _ts(2), "pv_w": None, "load_w": 300.0, "batt_w": 300.0, "p1_w": 0.0, "soc": 30.0}]
    rec = aggregate_past_actuals(rows)[datetime(2026, 6, 29, 2, tzinfo=timezone.utc)]
    assert rec["pv_w"] == 0.0
    assert rec["solar_charge_w"] == 0.0
    assert rec["grid_charge_w"] == 0.0  # batt positive = discharge, no charge


def test_export_hour_sets_grid_export():
    # p1_w negative = export.
    rows = [{"ts": _ts(19), "pv_w": 0.0, "load_w": 400.0, "batt_w": 2000.0, "p1_w": -1500.0, "soc": 60.0}]
    rec = aggregate_past_actuals(rows)[datetime(2026, 6, 29, 19, tzinfo=timezone.utc)]
    assert rec["grid_export_w"] == 1500.0


def test_means_over_multiple_rows_in_an_hour():
    rows = [
        {"ts": _ts(11, 0), "pv_w": 1000.0, "load_w": 100.0, "batt_w": 0.0, "p1_w": 0.0, "soc": 50.0},
        {"ts": _ts(11, 30), "pv_w": 2000.0, "load_w": 300.0, "batt_w": 0.0, "p1_w": 0.0, "soc": 54.0},
    ]
    rec = aggregate_past_actuals(rows)[datetime(2026, 6, 29, 11, tzinfo=timezone.utc)]
    assert rec["pv_w"] == 1500.0
    assert rec["load_w"] == 200.0
    assert rec["soc"] == 52.0


def test_load_uses_derive_fallback_when_load_w_null():
    # load_w NULL -> house_load_w derives p1 + batt + pv = 400 + 0 + 0 = 400.
    rows = [{"ts": _ts(3), "pv_w": 0.0, "load_w": None, "batt_w": 0.0, "p1_w": 400.0, "soc": 25.0}]
    rec = aggregate_past_actuals(rows)[datetime(2026, 6, 29, 3, tzinfo=timezone.utc)]
    assert rec["load_w"] == 400.0


def test_empty_and_bad_rows():
    assert aggregate_past_actuals([]) == {}
    assert aggregate_past_actuals([{"ts": "", "pv_w": 1.0}]) == {}
    assert aggregate_past_actuals([{"pv_w": 1.0}]) == {}  # no ts key
