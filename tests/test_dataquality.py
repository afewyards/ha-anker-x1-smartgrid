from datetime import datetime, timezone
from custom_components.anker_x1_smartgrid import dataquality as dq


# ---------------------------------------------------------------------------
# E2 — load_w integrity guard during battery export
# ---------------------------------------------------------------------------


def test_load_w_first_precedence_during_export():
    """load_w from sensor.power_usage is used even when row looks like export."""
    # Battery discharging 3000 W, exporting 2500 W to grid, house uses 500 W.
    # load_w = 500 (true consumption from sensor).  Must NOT fall back to derive.
    row = {
        "load_w": 500.0,
        "p1_w": -2500.0,  # grid export
        "batt_w": 3000.0,  # discharge
        "pv_w": 0.0,
    }
    assert dq.house_load_w(row) == 500.0


def test_load_w_recorded_beats_derive_pv_export():
    """load_w wins over a PV + grid-export scenario where derive would differ."""
    # PV 5000 W, house 1000 W, battery charges -2000 W, exports -2000 W.
    # Derive would yield 1000 W too, but load_w must be trusted unconditionally.
    row = {
        "load_w": 1000.0,
        "p1_w": -2000.0,
        "batt_w": -2000.0,
        "pv_w": 5000.0,
    }
    assert dq.house_load_w(row) == 1000.0


def test_derive_fallback_only_when_load_w_null():
    """Derive fallback is NOT invoked when load_w is present (even if zero)."""
    row = {"load_w": 0.0, "p1_w": None}  # p1 missing; derive would return None
    # load_w = 0.0 present → must return 0.0, not fall to derive
    assert dq.house_load_w(row) == 0.0


def test_derive_fallback_export_row_complete_inputs():
    """Derive fallback on an export row with ALL inputs present is export-safe."""
    # Battery discharging 3000 W, exporting 2500 W, no PV.
    # True house load = p1 + batt + pv = -2500 + 3000 + 0 = 500 W.
    row = {"p1_w": -2500.0, "batt_w": 3000.0, "pv_w": 0.0}
    result = dq.house_load_w(row)
    assert result == 500.0


def test_derive_fallback_export_row_missing_pv_returns_none():
    """Derive fallback on an export row with missing pv_w must return None.

    If pv_w is absent during export we cannot distinguish zero-PV from no-data,
    and silently defaulting to 0 would under-count load (polluting the ML model).
    Returning None causes the row to be dropped by clean_rows, which is safer.
    """
    row = {"p1_w": -2500.0, "batt_w": 3000.0}  # pv_w absent, no load_w
    result = dq.house_load_w(row)
    assert result is None


def test_derive_fallback_non_export_row_missing_pv_defaults_zero():
    """Non-export rows may safely default missing pv_w to 0 (no PV generation)."""
    # Grid import 800 W, battery idle, no PV sensor → typical non-PV house.
    row = {"p1_w": 800.0, "batt_w": 0.0}  # pv_w absent
    assert dq.house_load_w(row) == 800.0


def test_clean_rows_drops_export_row_with_missing_pv():
    """clean_rows discards a derive-fallback export row where pv_w is absent."""
    rows = [
        # Good row: load_w present during export
        {"ts": "2026-06-25T14:00:00+00:00", "load_w": 500.0, "p1_w": -2500.0, "batt_w": 3000.0, "pv_w": 0.0},
        # Bad row: no load_w, grid export, pv_w absent → must be dropped
        {"ts": "2026-06-25T14:05:00+00:00", "p1_w": -2500.0, "batt_w": 3000.0},
        # Good row: non-export derive (pv_w absent is safe)
        {"ts": "2026-06-25T15:00:00+00:00", "p1_w": 800.0, "batt_w": 0.0},
    ]
    out = dq.clean_rows(rows)
    assert len(out) == 2
    assert out[0].load_w == 500.0
    assert out[1].load_w == 800.0


# ---------------------------------------------------------------------------
# Existing tests (unchanged)
# ---------------------------------------------------------------------------


def test_derive_house_load_grid_only():
    # importing 800 W, battery idle, no PV -> 800 W load
    row = {"p1_w": 800.0, "batt_w": 0.0, "pv_w": 0.0}
    assert dq.derive_house_load_w(row) == 800.0


def test_derive_house_load_from_battery_discharge():
    # exporting nothing, battery discharging 1000 W covers load, no grid, no PV
    row = {"p1_w": 0.0, "batt_w": 1000.0, "pv_w": 0.0}
    assert dq.derive_house_load_w(row) == 1000.0


def test_derive_house_load_pv_self_consumption():
    # PV 2000 W, charging battery 500 W (batt_w=-500), grid 0 -> pv_to_house=1500, load=1500
    row = {"p1_w": 0.0, "batt_w": -500.0, "pv_w": 2000.0}
    assert dq.derive_house_load_w(row) == 1500.0


def test_derive_house_load_missing_p1_none():
    assert dq.derive_house_load_w({"batt_w": 0.0}) is None


def test_derive_house_load_export_while_charging():
    # GoodWe 5000 W: house uses 1000, battery charges 2000 (batt_w=-2000),
    # 2000 exported (p1_w=-2000). True house load = 1000 W.
    # Old DC-node formula returned 3000 here; the AC balance returns 1000.
    row = {"p1_w": -2000.0, "batt_w": -2000.0, "pv_w": 5000.0}
    assert dq.derive_house_load_w(row) == 1000.0


def test_derive_house_load_missing_batt_and_pv_default_zero():
    row = {"p1_w": 400.0}
    assert dq.derive_house_load_w(row) == 400.0


def test_clean_rows_filters_and_parses():
    rows = [
        {"ts": "2026-06-20T08:00:00+00:00", "p1_w": 500.0, "batt_w": 0.0, "pv_w": 0.0, "temp": 12.0},
        {"ts": None, "p1_w": 500.0},                 # dropped: no ts
        {"ts": "2026-06-21T08:00:00+00:00"},          # dropped: no load
        {"ts": "2026-06-20T09:00:00+00:00", "p1_w": 99999.0, "batt_w": 0.0, "pv_w": 0.0, "temp": 12.0},  # clamped
    ]
    out = dq.clean_rows(rows)
    assert len(out) == 2
    assert out[0].load_w == 500.0
    assert out[0].is_weekend is True  # 2026-06-20 is Saturday
    assert out[1].load_w == 25000.0  # clamped


# ---------------------------------------------------------------------------
# clean_hourly_rows — hourly energy-rollup FeatureRows for the bucketed tier
# ---------------------------------------------------------------------------


def test_clean_hourly_prefers_kwh_sum():
    rows = [{"hour_ts": "2026-07-09T10:00:00+00:00", "house_load_kwh_sum": 0.5,
             "house_load_mean": 700.0, "temp_mean": 18.5}]
    out = dq.clean_hourly_rows(rows)
    assert out[0].load_w == 500.0 and out[0].temp == 18.5 and out[0].hour == 10


def test_clean_hourly_fallback_and_skips():
    rows = [{"hour_ts": "2026-07-09T10:00:00+00:00", "house_load_mean": 700.0},
            {"hour_ts": "2026-07-09T11:00:00+00:00"},
            {"house_load_kwh_sum": 1.0}]
    out = dq.clean_hourly_rows(rows)
    assert len(out) == 1 and out[0].load_w == 700.0


def test_clean_hourly_clamps_to_load_max():
    # _LOAD_MAX_W is 25000.0 in dataquality.py (not 15000.0).
    rows = [{"hour_ts": "2026-07-09T10:00:00+00:00", "house_load_kwh_sum": 99.0}]
    assert dq.clean_hourly_rows(rows)[0].load_w == 25000.0
