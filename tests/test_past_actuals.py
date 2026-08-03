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


# --- Slot-grid bucketing (2026-08-03 history-gap fix) -----------------------
#
# aggregate_past_actuals buckets by clock hour by default. At sub-hour display
# resolution the caller passes slot_minutes so each display slot gets its OWN
# measurement instead of the hour mean repeated across its quarters -- and, more
# importantly, so the elapsed quarters of the CURRENT hour become available as
# actuals (the hour bucket is only complete, and only released, after the hour
# ends, which left those quarters with neither an actual nor a forecast).


def test_slot_bucketing_splits_the_hour_into_its_own_slots():
    # One tick per quarter, each with a distinct pv/load: at slot_minutes=15 the
    # hour must yield 4 buckets carrying their own values, not one hour mean.
    rows = [
        {"ts": _ts(10, m), "pv_w": pv, "load_w": 200.0, "batt_w": 0.0, "p1_w": 0.0, "soc": 50.0}
        for m, pv in ((0, 400.0), (15, 800.0), (30, 1200.0), (45, 1600.0))
    ]
    out = aggregate_past_actuals(rows, slot_minutes=15)
    assert len(out) == 4
    for m, pv in ((0, 400.0), (15, 800.0), (30, 1200.0), (45, 1600.0)):
        assert out[datetime(2026, 6, 29, 10, m, tzinfo=UTC)]["pv_w"] == pv


def test_slot_bucketing_default_is_byte_identical_to_hour_bucketing():
    rows = [
        {"ts": _ts(10, m), "pv_w": 1000.0, "load_w": 500.0, "batt_w": -600.0, "p1_w": -200.0, "soc": 50.0}
        for m in range(60)
    ]
    assert aggregate_past_actuals(rows, slot_minutes=60) == aggregate_past_actuals(rows)


def test_slot_bucket_energy_is_per_slot_not_per_hour():
    # v9 delta path: each slot sums only ITS OWN ticks, so the four quarters add
    # back up to exactly the hour total (no double-count, no quartering).
    rows = [
        {
            "ts": _ts(10, m),
            "pv_w": 1000.0,
            "pv_kwh": 0.02,
            "load_w": 500.0,
            "house_load_kwh": 0.01,
            "batt_w": 0.0,
            "p1_w": 0.0,
            "soc": 50.0,
        }
        for m in range(60)
    ]
    per_slot = aggregate_past_actuals(rows, slot_minutes=15)
    hourly = aggregate_past_actuals(rows)[datetime(2026, 6, 29, 10, tzinfo=UTC)]
    assert len(per_slot) == 4
    assert all(round(r["pv_kwh"], 3) == round(0.02 * 15, 3) for r in per_slot.values())
    assert round(sum(r["pv_kwh"] for r in per_slot.values()), 3) == hourly["pv_kwh"]
    assert round(sum(r["load_kwh"] for r in per_slot.values()), 3) == hourly["load_kwh"]


def test_mean_w_fallback_coverage_denominator_follows_slot_width():
    # Pre-v9 rows (no delta columns) fall back to mean-W x slot_h x coverage.
    # A FULL quarter is 15 ticks, not 60: with the hour's denominator this
    # 15-tick quarter would have read as 25% covered and under-reported 4x.
    rows = [
        {"ts": _ts(10, m), "pv_w": 1000.0, "load_w": 500.0, "batt_w": 0.0, "p1_w": 0.0, "soc": 50.0} for m in range(15)
    ]
    rec = aggregate_past_actuals(rows, slot_minutes=15)[datetime(2026, 6, 29, 10, tzinfo=UTC)]
    assert rec["pv_kwh"] == 0.25  # 1000 W over a full 15-min slot
    assert rec["load_kwh"] == 0.125


def test_mean_w_fallback_partial_slot_still_scaled_down():
    # 5 of 15 ticks in the quarter -> a third of the slot's worth of energy.
    rows = [
        {"ts": _ts(10, m), "pv_w": 1000.0, "load_w": 500.0, "batt_w": 0.0, "p1_w": 0.0, "soc": 50.0} for m in range(5)
    ]
    rec = aggregate_past_actuals(rows, slot_minutes=15)[datetime(2026, 6, 29, 10, tzinfo=UTC)]
    assert rec["pv_kwh"] == round(1.0 * 0.25 * 5 / 15, 3)
