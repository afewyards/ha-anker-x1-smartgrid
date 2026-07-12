from datetime import datetime, timezone, UTC

from custom_components.anker_x1_smartgrid.past_actuals import aggregate_past_actuals


def _ts(h, m=0):
    return datetime(2026, 6, 29, h, m, tzinfo=UTC).isoformat()


def test_solar_first_split_when_pv_surplus_covers_charge():
    # PV 2000, load 200 -> surplus 1800; charging 1000 (batt_w -1000) -> all solar.
    rows = [{"ts": _ts(10), "pv_w": 2000.0, "load_w": 200.0, "batt_w": -1000.0, "p1_w": 50.0, "soc": 40.0}]
    out = aggregate_past_actuals(rows)
    hour = datetime(2026, 6, 29, 10, tzinfo=UTC)
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
    rec = aggregate_past_actuals(rows)[datetime(2026, 6, 29, 9, tzinfo=UTC)]
    assert rec["solar_charge_w"] == 300.0
    assert rec["grid_charge_w"] == 700.0


def test_night_hour_pv_zero_no_solar_charge():
    # pv_w NULL (night), discharging (batt_w +300), importing.
    rows = [{"ts": _ts(2), "pv_w": None, "load_w": 300.0, "batt_w": 300.0, "p1_w": 0.0, "soc": 30.0}]
    rec = aggregate_past_actuals(rows)[datetime(2026, 6, 29, 2, tzinfo=UTC)]
    assert rec["pv_w"] == 0.0
    assert rec["solar_charge_w"] == 0.0
    assert rec["grid_charge_w"] == 0.0  # batt positive = discharge, no charge


def test_export_hour_sets_grid_export():
    # p1_w negative = export.
    rows = [{"ts": _ts(19), "pv_w": 0.0, "load_w": 400.0, "batt_w": 2000.0, "p1_w": -1500.0, "soc": 60.0}]
    rec = aggregate_past_actuals(rows)[datetime(2026, 6, 29, 19, tzinfo=UTC)]
    assert rec["grid_export_w"] == 1500.0


def test_means_over_multiple_rows_in_an_hour():
    rows = [
        {"ts": _ts(11, 0), "pv_w": 1000.0, "load_w": 100.0, "batt_w": 0.0, "p1_w": 0.0, "soc": 50.0},
        {"ts": _ts(11, 30), "pv_w": 2000.0, "load_w": 300.0, "batt_w": 0.0, "p1_w": 0.0, "soc": 54.0},
    ]
    rec = aggregate_past_actuals(rows)[datetime(2026, 6, 29, 11, tzinfo=UTC)]
    assert rec["pv_w"] == 1500.0
    assert rec["load_w"] == 200.0
    assert rec["soc"] == 52.0


def test_load_uses_derive_fallback_when_load_w_null():
    # load_w NULL -> house_load_w derives p1 + batt + pv = 400 + 0 + 0 = 400.
    rows = [{"ts": _ts(3), "pv_w": 0.0, "load_w": None, "batt_w": 0.0, "p1_w": 400.0, "soc": 25.0}]
    rec = aggregate_past_actuals(rows)[datetime(2026, 6, 29, 3, tzinfo=UTC)]
    assert rec["load_w"] == 400.0


def test_empty_and_bad_rows():
    assert aggregate_past_actuals([]) == {}
    assert aggregate_past_actuals([{"ts": "", "pv_w": 1.0}]) == {}
    assert aggregate_past_actuals([{"pv_w": 1.0}]) == {}  # no ts key


def test_kwh_keys_sum_deltas():
    # 3 rows in hour 10, each carrying v9 per-tick kWh deltas.
    row = {
        "pv_w": 1000.0,
        "pv_kwh": 0.02,
        "load_w": 500.0,
        "house_load_kwh": 0.01,
        "batt_w": -600.0,
        "batt_charge_kwh": 0.01,
        "p1_w": -200.0,
        "grid_export_kwh": 0.005,
        "soc": 50.0,
    }
    rows = [{**row, "ts": _ts(10, m)} for m in (0, 20, 40)]
    rec = aggregate_past_actuals(rows)[datetime(2026, 6, 29, 10, tzinfo=UTC)]
    assert rec["pv_kwh"] == 0.06
    assert rec["load_kwh"] == 0.03
    assert rec["grid_export_kwh"] == 0.015
    # energy-level solar-first split: surplus = pv_kwh - load_kwh = 0.03
    assert rec["solar_charge_kwh"] == 0.03
    assert rec["grid_charge_kwh"] == 0.0


def test_kwh_fallback_from_means_when_deltas_null():
    # No *_kwh columns present, single tick (1/60 of an hour) -> fall back to
    # mean-W * 1h * coverage, not the full mean-W * 1h (coverage-scaled fix).
    rows = [{"ts": _ts(10), "pv_w": 1000.0, "load_w": 500.0, "batt_w": 0.0, "p1_w": 0.0, "soc": 50.0}]
    rec = aggregate_past_actuals(rows)[datetime(2026, 6, 29, 10, tzinfo=UTC)]
    assert rec["pv_kwh"] == round(1.0 * 1 / 60, 3)
    assert rec["load_kwh"] == round(0.5 * 1 / 60, 3)
    assert rec["solar_charge_kwh"] == 0.0
    assert rec["grid_charge_kwh"] == 0.0
    assert rec["grid_export_kwh"] == 0.0


def test_kwh_fallback_scaled_by_partial_hour_coverage():
    # 20 of 60 ticks present (e.g. 20 minutes after a restart) -> mean-W
    # fallback scaled down to 20/60 of a full hour, not the full hour.
    rows = [
        {"ts": _ts(10, m), "pv_w": 1000.0, "load_w": 500.0, "batt_w": 0.0, "p1_w": 0.0, "soc": 50.0} for m in range(20)
    ]
    rec = aggregate_past_actuals(rows)[datetime(2026, 6, 29, 10, tzinfo=UTC)]
    assert rec["pv_kwh"] == round(1.0 * 20 / 60, 3) == 0.333
    assert rec["load_kwh"] == round(0.5 * 20 / 60, 3)


def test_kwh_fallback_full_hour_coverage_unscaled():
    # 60 ticks -> full-hour coverage; fallback equals plain mean-W * 1h.
    rows = [
        {"ts": _ts(10, m), "pv_w": 1000.0, "load_w": 500.0, "batt_w": 0.0, "p1_w": 0.0, "soc": 50.0} for m in range(60)
    ]
    rec = aggregate_past_actuals(rows)[datetime(2026, 6, 29, 10, tzinfo=UTC)]
    assert rec["pv_kwh"] == 1.0
    assert rec["load_kwh"] == 0.5


def test_kwh_delta_sum_not_scaled_by_coverage():
    # Even with few rows relative to a full hour, when v9 kWh deltas are
    # present the sum path is used verbatim -- no coverage scaling applied.
    row = {
        "pv_w": 1000.0,
        "pv_kwh": 0.02,
        "load_w": 500.0,
        "house_load_kwh": 0.01,
        "batt_w": 0.0,
        "p1_w": 0.0,
        "soc": 50.0,
    }
    rows = [{**row, "ts": _ts(10, m)} for m in (0, 20, 40)]
    rec = aggregate_past_actuals(rows)[datetime(2026, 6, 29, 10, tzinfo=UTC)]
    assert rec["pv_kwh"] == 0.06
    assert rec["load_kwh"] == 0.03


def test_w_keys_unchanged():
    # Same rows as test_kwh_keys_sum_deltas: mean-W outputs must be byte-identical
    # to pre-change behaviour (naive means over pv_w/load_w/soc/batt_w/p1_w).
    row = {
        "pv_w": 1000.0,
        "pv_kwh": 0.02,
        "load_w": 500.0,
        "house_load_kwh": 0.01,
        "batt_w": -600.0,
        "batt_charge_kwh": 0.01,
        "p1_w": -200.0,
        "grid_export_kwh": 0.005,
        "soc": 50.0,
    }
    rows = [{**row, "ts": _ts(10, m)} for m in (0, 20, 40)]
    rec = aggregate_past_actuals(rows)[datetime(2026, 6, 29, 10, tzinfo=UTC)]
    assert rec["pv_w"] == 1000.0
    assert rec["load_w"] == 500.0
    assert rec["soc"] == 50.0
    # charge_w = mean(max(0, -batt_w)) = 600; surplus = max(0, pv_w - load_w) = 500
    assert rec["solar_charge_w"] == 500.0
    assert rec["grid_charge_w"] == 100.0
    # grid_export_w = mean(max(0, -p1_w)) = 200
    assert rec["grid_export_w"] == 200.0
