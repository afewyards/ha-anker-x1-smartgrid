"""TDD tests for featureset.py — calendar/time feature encoder (P2-T1).

Public API under test:
    encode_calendar_features(ts) -> dict

Stable output keys:
    hour_sin, hour_cos, doy_sin, doy_cos, day_of_week, is_holiday
"""

from __future__ import annotations

import math
from datetime import datetime, timezone, UTC

from zoneinfo import ZoneInfo

from custom_components.anker_x1_smartgrid.featureset import (
    build_feature_matrix,
    encode_calendar_features,
    encode_lag_features_from_lookups,
    encode_weather_features,
    feature_names,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

UTC = UTC
_TZ_AMS = ZoneInfo("Europe/Amsterdam")


def encode_lag_features(hourly_rows: list[dict], idx: int) -> dict:
    """Test-local port of the removed prod wrapper: build the UTC/local-date
    lookups exactly as build_feature_matrix does, then delegate to the
    canonical encode_lag_features_from_lookups."""
    utc_lookup: dict[datetime, float | None] = {}
    local_date_kwh: dict = {}
    for row in hourly_rows:
        ts_str = row.get("hour_ts")
        if not ts_str:
            continue
        ts = datetime.fromisoformat(str(ts_str))
        utc_lookup[ts] = row.get("house_load_mean")
        kwh = row.get("house_load_kwh_sum")
        if kwh is not None:
            local_d = ts.astimezone(_TZ_AMS).date()
            local_date_kwh[local_d] = local_date_kwh.get(local_d, 0.0) + float(kwh)
    t = datetime.fromisoformat(str(hourly_rows[idx]["hour_ts"]))
    return encode_lag_features_from_lookups(utc_lookup, local_date_kwh, t)


# ---------------------------------------------------------------------------
# 1. Cyclical encoding correctness (hour)
# ---------------------------------------------------------------------------


class TestHourCyclical:
    """Verify sin/cos encoding for synthetic local hours."""

    def _features_for_local_hour(self, local_hour: int) -> dict:
        """Build a UTC timestamp that maps to exactly `local_hour` in Europe/Amsterdam.

        Use a winter date (CET = UTC+1) so offset is predictable.
        local_hour_local = utc_hour + 1  →  utc_hour = local_hour - 1
        """
        utc_hour = local_hour - 1  # CET = UTC+1 on 2026-01-20
        ts = datetime(2026, 1, 20, utc_hour % 24, 0, 0, tzinfo=UTC)
        # Handle wrap-around (local_hour=0 → utc_hour=23 on the previous day)
        if local_hour == 0:
            ts = datetime(2026, 1, 20, 23, 0, 0, tzinfo=UTC)
            # but 2026-01-20 23:00 UTC = 2026-01-21 00:00 CET — still fine for the test
        return encode_calendar_features(ts)

    def test_hour_zero_sin_approx_zero_cos_approx_one(self):
        f = self._features_for_local_hour(0)
        assert abs(f["hour_sin"]) < 1e-9, f"expected hour_sin≈0 at hour 0, got {f['hour_sin']}"
        assert abs(f["hour_cos"] - 1.0) < 1e-9, f"expected hour_cos≈1 at hour 0, got {f['hour_cos']}"

    def test_hour_six_sin_approx_one_cos_approx_zero(self):
        f = self._features_for_local_hour(6)
        assert abs(f["hour_sin"] - 1.0) < 1e-9, f"expected hour_sin≈1 at hour 6, got {f['hour_sin']}"
        assert abs(f["hour_cos"]) < 1e-9, f"expected hour_cos≈0 at hour 6, got {f['hour_cos']}"

    def test_hour_twelve_sin_approx_zero_cos_approx_minus_one(self):
        f = self._features_for_local_hour(12)
        assert abs(f["hour_sin"]) < 1e-9
        assert abs(f["hour_cos"] - (-1.0)) < 1e-9

    def test_hour_eighteen_sin_approx_minus_one_cos_approx_zero(self):
        f = self._features_for_local_hour(18)
        assert abs(f["hour_sin"] - (-1.0)) < 1e-9
        assert abs(f["hour_cos"]) < 1e-9

    def test_cyclical_values_within_unit_circle(self):
        """sin²+cos² == 1 for any hour."""
        for h in range(24):
            f = self._features_for_local_hour(h)
            norm = f["hour_sin"] ** 2 + f["hour_cos"] ** 2
            assert abs(norm - 1.0) < 1e-9, f"hour {h}: sin²+cos²={norm}"


# ---------------------------------------------------------------------------
# 2. DST correctness — the critical test
# ---------------------------------------------------------------------------


class TestDSTConversion:
    """Verify UTC→Europe/Amsterdam conversion handles both CET and CEST."""

    def test_summer_utc_maps_to_cest_local_hour(self):
        """2026-06-20T12:00:00+00:00 → 14:00 local CEST (UTC+2)."""
        ts = datetime(2026, 6, 20, 12, 0, 0, tzinfo=UTC)
        f = encode_calendar_features(ts)
        expected_sin = math.sin(2 * math.pi * 14 / 24)
        expected_cos = math.cos(2 * math.pi * 14 / 24)
        assert abs(f["hour_sin"] - expected_sin) < 1e-9, (
            f"Summer: expected hour_sin for 14:00 local ({expected_sin:.6f}), got {f['hour_sin']:.6f}"
        )
        assert abs(f["hour_cos"] - expected_cos) < 1e-9, (
            f"Summer: expected hour_cos for 14:00 local ({expected_cos:.6f}), got {f['hour_cos']:.6f}"
        )

    def test_winter_utc_maps_to_cet_local_hour(self):
        """2026-01-20T12:00:00+00:00 → 13:00 local CET (UTC+1)."""
        ts = datetime(2026, 1, 20, 12, 0, 0, tzinfo=UTC)
        f = encode_calendar_features(ts)
        expected_sin = math.sin(2 * math.pi * 13 / 24)
        expected_cos = math.cos(2 * math.pi * 13 / 24)
        assert abs(f["hour_sin"] - expected_sin) < 1e-9, (
            f"Winter: expected hour_sin for 13:00 local ({expected_sin:.6f}), got {f['hour_sin']:.6f}"
        )
        assert abs(f["hour_cos"] - expected_cos) < 1e-9, (
            f"Winter: expected hour_cos for 13:00 local ({expected_cos:.6f}), got {f['hour_cos']:.6f}"
        )

    def test_dst_transition_spring_forward(self):
        """2026-03-29T00:00:00+00:00 is just before DST change (still CET = UTC+1).
        So local hour = 1."""
        ts = datetime(2026, 3, 29, 0, 0, 0, tzinfo=UTC)
        f = encode_calendar_features(ts)
        expected_sin = math.sin(2 * math.pi * 1 / 24)
        expected_cos = math.cos(2 * math.pi * 1 / 24)
        assert abs(f["hour_sin"] - expected_sin) < 1e-9
        assert abs(f["hour_cos"] - expected_cos) < 1e-9

    def test_dst_transition_after_spring_forward(self):
        """2026-03-29T01:00:00+00:00 → Europe/Amsterdam clocks spring forward:
        this is 03:00 CEST (UTC+2)."""
        ts = datetime(2026, 3, 29, 1, 0, 0, tzinfo=UTC)
        f = encode_calendar_features(ts)
        expected_sin = math.sin(2 * math.pi * 3 / 24)
        expected_cos = math.cos(2 * math.pi * 3 / 24)
        assert abs(f["hour_sin"] - expected_sin) < 1e-9
        assert abs(f["hour_cos"] - expected_cos) < 1e-9


# ---------------------------------------------------------------------------
# 3. day_of_week
# ---------------------------------------------------------------------------


class TestDayOfWeek:
    def test_known_saturday_local(self):
        """2026-06-20 UTC 12:00 → 2026-06-20 14:00 CEST (still Saturday = 5)."""
        ts = datetime(2026, 6, 20, 12, 0, 0, tzinfo=UTC)
        f = encode_calendar_features(ts)
        assert f["day_of_week"] == 5, f"2026-06-20 should be Saturday (5), got {f['day_of_week']}"

    def test_known_monday_local(self):
        """2026-06-22 is Monday."""
        ts = datetime(2026, 6, 22, 10, 0, 0, tzinfo=UTC)
        f = encode_calendar_features(ts)
        assert f["day_of_week"] == 0, f"2026-06-22 should be Monday (0), got {f['day_of_week']}"

    def test_known_sunday_local(self):
        """2026-06-21 is Sunday."""
        ts = datetime(2026, 6, 21, 10, 0, 0, tzinfo=UTC)
        f = encode_calendar_features(ts)
        assert f["day_of_week"] == 6, f"2026-06-21 should be Sunday (6), got {f['day_of_week']}"

    def test_midnight_utc_may_shift_local_day(self):
        """2026-06-21T23:30:00+00:00 → 2026-06-22T01:30:00 CEST → Monday (0)."""
        ts = datetime(2026, 6, 21, 23, 30, 0, tzinfo=UTC)
        f = encode_calendar_features(ts)
        assert f["day_of_week"] == 0, f"2026-06-21 23:30 UTC should be Monday local (0), got {f['day_of_week']}"


# ---------------------------------------------------------------------------
# 4. is_holiday (NL)
# ---------------------------------------------------------------------------


class TestIsHoliday:
    def test_koningsdag_2026_is_holiday(self):
        """Koningsdag 2026-04-27 is a NL public holiday."""
        ts = datetime(2026, 4, 27, 10, 0, 0, tzinfo=UTC)  # noon in NL local
        f = encode_calendar_features(ts)
        assert f["is_holiday"] == 1, "Koningsdag 2026-04-27 should be is_holiday=1"

    def test_christmas_2025_is_holiday(self):
        """2025-12-25 Christmas Day is a NL public holiday."""
        ts = datetime(2025, 12, 25, 10, 0, 0, tzinfo=UTC)
        f = encode_calendar_features(ts)
        assert f["is_holiday"] == 1, "Christmas 2025-12-25 should be is_holiday=1"

    def test_second_christmas_2025_is_holiday(self):
        """2025-12-26 (Tweede Kerstdag / Boxing Day) is also a NL public holiday."""
        ts = datetime(2025, 12, 26, 10, 0, 0, tzinfo=UTC)
        f = encode_calendar_features(ts)
        assert f["is_holiday"] == 1, "Tweede Kerstdag 2025-12-26 should be is_holiday=1"

    def test_ordinary_weekday_is_not_holiday(self):
        """2026-06-17 (ordinary Wednesday) should NOT be a holiday."""
        ts = datetime(2026, 6, 17, 10, 0, 0, tzinfo=UTC)
        f = encode_calendar_features(ts)
        assert f["is_holiday"] == 0, "2026-06-17 (ordinary Wednesday) should be is_holiday=0"

    def test_new_years_day_2026_is_holiday(self):
        """2026-01-01 (Nieuwjaarsdag) is a NL public holiday."""
        ts = datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)
        f = encode_calendar_features(ts)
        assert f["is_holiday"] == 1, "Nieuwjaarsdag 2026-01-01 should be is_holiday=1"


# ---------------------------------------------------------------------------
# 5. Input forms — both datetime and ISO string
# ---------------------------------------------------------------------------


class TestInputForms:
    """Both UTC-aware datetime and ISO-8601 string must be accepted."""

    def test_datetime_and_iso_string_produce_equal_results(self):
        ts_dt = datetime(2026, 6, 20, 12, 0, 0, tzinfo=UTC)
        ts_str = "2026-06-20T12:00:00+00:00"
        f_dt = encode_calendar_features(ts_dt)
        f_str = encode_calendar_features(ts_str)
        assert f_dt == f_str, f"datetime and ISO-string inputs should produce identical results:\n{f_dt}\nvs\n{f_str}"

    def test_iso_string_with_zulu_suffix(self):
        """Accept 'Z' suffix (Python 3.11+ fromisoformat supports it)."""
        ts_dt = datetime(2026, 1, 20, 12, 0, 0, tzinfo=UTC)
        ts_z = "2026-01-20T12:00:00Z"
        f_dt = encode_calendar_features(ts_dt)
        f_z = encode_calendar_features(ts_z)
        assert f_dt == f_z

    def test_iso_string_hour_ts_format(self):
        """Rollup table stores hour_ts as e.g. '2026-06-20T14:00:00+00:00'."""
        ts_str = "2026-06-20T14:00:00+00:00"
        f = encode_calendar_features(ts_str)
        # 14:00 UTC in summer = 16:00 CEST
        expected_sin = math.sin(2 * math.pi * 16 / 24)
        expected_cos = math.cos(2 * math.pi * 16 / 24)
        assert abs(f["hour_sin"] - expected_sin) < 1e-9
        assert abs(f["hour_cos"] - expected_cos) < 1e-9


# ---------------------------------------------------------------------------
# 6. doy cyclical encoding
# ---------------------------------------------------------------------------


class TestDOYCyclical:
    def test_jan_1_doy_is_1(self):
        """Day-of-year 1 → sin(2π/365.25), cos(2π/365.25)."""
        ts = datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)  # 11:00 CET (still Jan 1)
        f = encode_calendar_features(ts)
        expected_sin = math.sin(2 * math.pi * 1 / 365.25)
        expected_cos = math.cos(2 * math.pi * 1 / 365.25)
        assert abs(f["doy_sin"] - expected_sin) < 1e-9
        assert abs(f["doy_cos"] - expected_cos) < 1e-9

    def test_doy_sin_cos_unit_circle(self):
        """sin²+cos² == 1 for any day."""
        for month in (1, 3, 6, 9, 12):
            ts = datetime(2026, month, 15, 10, 0, 0, tzinfo=UTC)
            f = encode_calendar_features(ts)
            norm = f["doy_sin"] ** 2 + f["doy_cos"] ** 2
            assert abs(norm - 1.0) < 1e-9, f"month {month}: sin²+cos²={norm}"


# ---------------------------------------------------------------------------
# 7. Return keys are stable and complete
# ---------------------------------------------------------------------------


EXPECTED_KEYS = {"hour_sin", "hour_cos", "doy_sin", "doy_cos", "day_of_week", "is_holiday"}


class TestReturnShape:
    def test_all_expected_keys_present(self):
        ts = datetime(2026, 6, 20, 12, 0, 0, tzinfo=UTC)
        f = encode_calendar_features(ts)
        missing = EXPECTED_KEYS - set(f.keys())
        assert not missing, f"Missing keys: {missing}"

    def test_no_extra_calendar_keys(self):
        """Verify no extra keys slip in — P2-T4 builds a fixed-column matrix."""
        ts = datetime(2026, 6, 20, 12, 0, 0, tzinfo=UTC)
        f = encode_calendar_features(ts)
        extra = set(f.keys()) - EXPECTED_KEYS
        assert not extra, f"Unexpected extra keys: {extra}"

    def test_numeric_types(self):
        ts = datetime(2026, 6, 20, 12, 0, 0, tzinfo=UTC)
        f = encode_calendar_features(ts)
        for k in ("hour_sin", "hour_cos", "doy_sin", "doy_cos"):
            assert isinstance(f[k], float), f"{k} should be float, got {type(f[k])}"
        for k in ("day_of_week", "is_holiday"):
            assert isinstance(f[k], int), f"{k} should be int, got {type(f[k])}"


# ===========================================================================
# P2-T2: Lag features
# ===========================================================================

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row(hour_ts: str, house_load_mean, **extra) -> dict:
    """Build a minimal hourly rollup row for lag/matrix tests."""
    d: dict = {"hour_ts": hour_ts, "house_load_mean": house_load_mean}
    d.update(extra)
    return d


# ---------------------------------------------------------------------------
# 8. encode_lag_features — point lags
# ---------------------------------------------------------------------------


class TestLagFeaturesPointLags:
    """Verify t-1h, t-24h, t-168h pulls and gap→NaN behaviour."""

    # hand-built sequence with all three lag rows present
    _T168H = "2026-06-15T12:00:00+00:00"
    _T24H = "2026-06-21T12:00:00+00:00"
    _T1H = "2026-06-22T11:00:00+00:00"
    _T = "2026-06-22T12:00:00+00:00"

    @property
    def _rows(self) -> list[dict]:
        return [
            _row(self._T168H, 300.0),
            _row(self._T24H, 200.0),
            _row(self._T1H, 100.0),
            _row(self._T, 50.0),  # target at idx 3
        ]

    def test_lag_1h(self):
        f = encode_lag_features(self._rows, 3)
        assert f["load_lag_1h"] == 100.0

    def test_lag_24h(self):
        f = encode_lag_features(self._rows, 3)
        assert f["load_lag_24h"] == 200.0

    def test_lag_168h(self):
        f = encode_lag_features(self._rows, 3)
        assert f["load_lag_168h"] == 300.0

    def test_missing_lags_are_nan(self):
        """Only t-1h present → t-24h and t-168h become NaN."""
        rows = [
            _row(self._T1H, 100.0),
            _row(self._T, 50.0),
        ]
        f = encode_lag_features(rows, 1)
        assert f["load_lag_1h"] == 100.0
        assert math.isnan(f["load_lag_24h"])
        assert math.isnan(f["load_lag_168h"])

    def test_none_load_in_lag_row_gives_nan(self):
        """Row exists at t-1h but house_load_mean is None → NaN."""
        rows = [
            _row(self._T1H, None),
            _row(self._T, 50.0),
        ]
        f = encode_lag_features(rows, 1)
        assert math.isnan(f["load_lag_1h"])

    def test_only_target_row(self):
        """Single row (no lags) → all three point lags are NaN."""
        rows = [_row(self._T, 50.0)]
        f = encode_lag_features(rows, 0)
        assert math.isnan(f["load_lag_1h"])
        assert math.isnan(f["load_lag_24h"])
        assert math.isnan(f["load_lag_168h"])

    def test_return_keys_complete(self):
        rows = [_row(self._T, 50.0)]
        f = encode_lag_features(rows, 0)
        expected = {"load_lag_1h", "load_lag_24h", "load_lag_168h", "rolling_mean_24h", "prev_day_total_kwh"}
        assert set(f.keys()) == expected


# ---------------------------------------------------------------------------
# 9. encode_lag_features — rolling_mean_24h
# ---------------------------------------------------------------------------


class TestRollingMean24h:
    _T = "2026-06-22T12:00:00+00:00"

    def test_partial_window_correct_mean(self):
        """Only 3 of 24 window hours present; mean over those 3."""
        # t-3h, t-12h, t-24h are all inside (t-24h .. t-1h)
        rows = [
            _row("2026-06-21T12:00:00+00:00", 120.0),  # t-24h (boundary, included)
            _row("2026-06-22T00:00:00+00:00", 240.0),  # t-12h
            _row("2026-06-22T09:00:00+00:00", 360.0),  # t-3h
            _row(self._T, 50.0),
        ]
        f = encode_lag_features(rows, 3)
        expected = (120.0 + 240.0 + 360.0) / 3
        assert abs(f["rolling_mean_24h"] - expected) < 1e-9

    def test_no_window_rows_is_nan(self):
        """No rows in the 24-hour trailing window → NaN."""
        rows = [_row(self._T, 50.0)]
        f = encode_lag_features(rows, 0)
        assert math.isnan(f["rolling_mean_24h"])

    def test_window_excludes_target_itself(self):
        """The rolling mean window is (t-24h..t-1h) — does NOT include t."""
        rows = [
            _row("2026-06-22T11:00:00+00:00", 100.0),  # t-1h (in window)
            _row(self._T, 999.0),  # t (NOT in window)
        ]
        f = encode_lag_features(rows, 1)
        assert abs(f["rolling_mean_24h"] - 100.0) < 1e-9

    def test_window_includes_t_minus_24h_boundary(self):
        """t-24h is INCLUDED in the rolling window (boundary test)."""
        rows = [
            _row("2026-06-21T12:00:00+00:00", 120.0),  # exactly t-24h
            _row(self._T, 50.0),
        ]
        f = encode_lag_features(rows, 1)
        assert abs(f["rolling_mean_24h"] - 120.0) < 1e-9

    def test_window_excludes_beyond_t_minus_24h(self):
        """t-25h is NOT in the window; must not contribute."""
        rows = [
            _row("2026-06-21T11:00:00+00:00", 999.0),  # t-25h (outside)
            _row("2026-06-22T11:00:00+00:00", 100.0),  # t-1h (inside)
            _row(self._T, 50.0),
        ]
        f = encode_lag_features(rows, 2)
        # Only t-1h=100 in window
        assert abs(f["rolling_mean_24h"] - 100.0) < 1e-9

    def test_none_load_rows_excluded_from_window(self):
        """None house_load_mean rows don't contribute to the rolling mean."""
        rows = [
            _row("2026-06-22T10:00:00+00:00", None),  # t-2h, None → skip
            _row("2026-06-22T11:00:00+00:00", 200.0),  # t-1h, valid
            _row(self._T, 50.0),
        ]
        f = encode_lag_features(rows, 2)
        assert abs(f["rolling_mean_24h"] - 200.0) < 1e-9


# ---------------------------------------------------------------------------
# 10. encode_lag_features — prev_day_total_kwh
# ---------------------------------------------------------------------------


class TestPrevDayTotalKwh:
    """Verify local-calendar-day grouping and house_load_kwh_sum summation.

    ``prev_day_total_kwh`` is summed directly from each row's
    ``house_load_kwh_sum`` column (the rollup's honest per-tick energy
    integration, with a mean/1000 tier-2 fallback already baked in for
    pre-v10 hours).  These rows set ``house_load_kwh_sum`` explicitly via
    the ``_row(..., house_load_kwh_sum=...)`` extra kwarg so the numbers
    stay independent of ``house_load_mean`` (used only for load_lag_*).
    """

    # In summer CEST (UTC+2):
    # Local 2026-06-21 starts at UTC 2026-06-20T22:00 and ends at UTC 2026-06-21T21:59.
    # Target 2026-06-22T12:00 UTC = 2026-06-22 14:00 CEST → prev_local_date = 2026-06-21.

    _T = "2026-06-22T12:00:00+00:00"

    def test_basic_w_to_kwh_conversion_and_local_grouping(self):
        """Two rows on local 2026-06-21; energy = 1.0 + 2.0 = 3.0 kWh."""
        rows = [
            _row("2026-06-20T22:00:00+00:00", 1000.0, house_load_kwh_sum=1.0),  # UTC 22h = CEST 00h → local 2026-06-21
            _row("2026-06-21T00:00:00+00:00", 2000.0, house_load_kwh_sum=2.0),  # UTC 00h = CEST 02h → local 2026-06-21
            _row(self._T, 50.0),
        ]
        f = encode_lag_features(rows, 2)
        assert abs(f["prev_day_total_kwh"] - 3.0) < 1e-9

    def test_absent_prev_day_is_nan(self):
        """No rows for the previous local day → NaN."""
        rows = [_row(self._T, 50.0)]
        f = encode_lag_features(rows, 0)
        assert math.isnan(f["prev_day_total_kwh"])

    def test_all_none_loads_for_prev_day_is_nan(self):
        """Prev day has a row but house_load_kwh_sum=None → NaN (no energy data)."""
        rows = [
            _row("2026-06-20T22:00:00+00:00", None),  # local 2026-06-21, no load, no kwh_sum
            _row(self._T, 50.0),
        ]
        f = encode_lag_features(rows, 1)
        assert math.isnan(f["prev_day_total_kwh"])

    def test_utc_midnight_belongs_to_correct_local_date(self):
        """In CET (UTC+1): 2026-01-15T00:00Z = 2026-01-15 01:00 CET → local 2026-01-15.
        Target 2026-01-16T10:00Z → prev_local_date = 2026-01-15."""
        rows = [
            _row("2026-01-15T00:00:00+00:00", 500.0, house_load_kwh_sum=0.5),  # CET 01:00 → local 2026-01-15
            _row("2026-01-16T10:00:00+00:00", 50.0),  # target
        ]
        f = encode_lag_features(rows, 1)
        assert abs(f["prev_day_total_kwh"] - 0.5) < 1e-9

    def test_prev_day_excludes_today_rows(self):
        """Rows from local today do NOT count toward prev_day_total_kwh."""
        rows = [
            _row("2026-06-21T22:00:00+00:00", 5000.0, house_load_kwh_sum=5.0),  # CEST 00:00 = local 2026-06-22 (today!)
            _row(
                "2026-06-20T22:00:00+00:00", 1000.0, house_load_kwh_sum=1.0
            ),  # CEST 00:00 = local 2026-06-21 (yesterday)
            _row(self._T, 50.0),
        ]
        f = encode_lag_features(rows, 2)
        # Only the 2026-06-21 row counts: 1.0 kWh
        assert abs(f["prev_day_total_kwh"] - 1.0) < 1e-9

    def test_single_row_prev_day(self):
        """Single row on prev local day → correct single-hour energy."""
        rows = [
            _row("2026-06-21T08:00:00+00:00", 800.0, house_load_kwh_sum=0.8),  # CEST 10:00 → local 2026-06-21
            _row(self._T, 50.0),
        ]
        f = encode_lag_features(rows, 1)
        assert abs(f["prev_day_total_kwh"] - 0.8) < 1e-9

    def test_prev_day_kwh_uses_kwh_sum(self):
        """house_load_kwh_sum is used directly, NOT derived from house_load_mean.

        house_load_mean=4000W would naively imply 4.0 kWh (mean/1000), but the
        honest per-tick kwh_sum (accounting for gaps) is only 0.6 kWh. The
        lower, accurate value must win.
        """
        rows = [
            _row("2026-06-20T22:00:00+00:00", 4000.0, house_load_kwh_sum=0.6),
            _row(self._T, 50.0),
        ]
        f = encode_lag_features(rows, 1)
        assert abs(f["prev_day_total_kwh"] - 0.6) < 1e-9

    def test_prev_day_kwh_skips_none(self):
        """Row with house_load_kwh_sum=None (pre-v10 row) contributes nothing,
        even when house_load_mean is present — no mean/1000 fallback here
        (that fallback lives in the rollup, not in this lag-feature layer)."""
        rows = [
            _row("2026-06-20T22:00:00+00:00", 1000.0),  # no house_load_kwh_sum kwarg → None
            _row("2026-06-21T00:00:00+00:00", 2000.0, house_load_kwh_sum=2.0),
            _row(self._T, 50.0),
        ]
        f = encode_lag_features(rows, 2)
        # Only the second row's kwh_sum counts; the first (None) contributes nothing.
        assert abs(f["prev_day_total_kwh"] - 2.0) < 1e-9


# ===========================================================================
# P2-T3: Weather features
# ===========================================================================


class TestWeatherFeatures:
    """Verify HDD/CDD math, threshold boundary cases, and NaN propagation."""

    # ---- HDD / CDD ----

    def test_hdd_temp_well_below_base(self):
        """temp=5.0 → HDD=15.5-5.0=10.5, CDD=0."""
        f = encode_weather_features({"temp_forecast_mean": 5.0})
        assert abs(f["temp_forecast"] - 5.0) < 1e-9
        assert abs(f["hdd"] - 10.5) < 1e-9
        assert f["cdd"] == 0.0

    def test_cdd_temp_well_above_base(self):
        """temp=30.0 → HDD=0, CDD=30-22=8.0."""
        f = encode_weather_features({"temp_forecast_mean": 30.0})
        assert f["hdd"] == 0.0
        assert abs(f["cdd"] - 8.0) < 1e-9

    def test_temp_between_bases_no_degree_days(self):
        """15.5 ≤ temp ≤ 22.0 → HDD=0, CDD=0."""
        f = encode_weather_features({"temp_forecast_mean": 18.0})
        assert f["hdd"] == 0.0
        assert f["cdd"] == 0.0

    def test_temp_exactly_at_hdd_base(self):
        """temp=15.5 → HDD=max(0, 0)=0."""
        f = encode_weather_features({"temp_forecast_mean": 15.5})
        assert f["hdd"] == 0.0
        assert f["cdd"] == 0.0

    def test_temp_exactly_at_cdd_base(self):
        """temp=22.0 → CDD=max(0, 0)=0."""
        f = encode_weather_features({"temp_forecast_mean": 22.0})
        assert f["hdd"] == 0.0
        assert f["cdd"] == 0.0

    def test_negative_temp_hdd(self):
        """temp=-5.0 → HDD=15.5-(-5)=20.5."""
        f = encode_weather_features({"temp_forecast_mean": -5.0})
        assert abs(f["hdd"] - 20.5) < 1e-9
        assert f["cdd"] == 0.0

    # ---- NaN propagation ----

    def test_none_temp_forecast_all_nan(self):
        """None temp_forecast_mean → temp_forecast, hdd, cdd all NaN."""
        f = encode_weather_features({"temp_forecast_mean": None})
        assert math.isnan(f["temp_forecast"])
        assert math.isnan(f["hdd"])
        assert math.isnan(f["cdd"])

    def test_missing_temp_key_all_nan(self):
        """Completely absent temp_forecast_mean key → NaN trio."""
        f = encode_weather_features({})
        assert math.isnan(f["temp_forecast"])
        assert math.isnan(f["hdd"])
        assert math.isnan(f["cdd"])

    def test_none_weather_columns_become_nan(self):
        """None values for cloud/humidity/wind → NaN."""
        row = {
            "temp_forecast_mean": 15.0,
            "cloud_cover_mean": None,
            "humidity_mean": None,
            "wind_speed_mean": None,
        }
        f = encode_weather_features(row)
        assert math.isnan(f["cloud_cover"])
        assert math.isnan(f["humidity"])
        assert math.isnan(f["wind_speed"])

    # ---- Passthrough ----

    def test_all_weather_values_passed_through(self):
        """All weather columns present → correct values in output dict."""
        row = {
            "temp_forecast_mean": 10.0,
            "cloud_cover_mean": 75.0,
            "humidity_mean": 80.0,
            "wind_speed_mean": 5.5,
        }
        f = encode_weather_features(row)
        assert f["temp_forecast"] == 10.0
        assert f["cloud_cover"] == 75.0
        assert f["humidity"] == 80.0
        assert f["wind_speed"] == 5.5

    def test_weather_keys_always_complete(self):
        """All 6 weather feature keys are always present in the output dict."""
        f = encode_weather_features({})
        expected = {"temp_forecast", "hdd", "cdd", "cloud_cover", "humidity", "wind_speed"}
        assert set(f.keys()) == expected


# ===========================================================================
# P2-T4: feature_names() and build_feature_matrix()
# ===========================================================================

# ---------------------------------------------------------------------------
# 11. feature_names() — column order contract
# ---------------------------------------------------------------------------

# The LOCKED column order that HistGBR depends on.
# 6 calendar + 5 lags + 6 weather = 17 features.
# NOTE: live ambient temp (temp_mean) is intentionally absent — train/serve skew risk.
_EXPECTED_FEATURE_NAMES = [
    "hour_sin",
    "hour_cos",
    "doy_sin",
    "doy_cos",
    "day_of_week",
    "is_holiday",
    "load_lag_1h",
    "load_lag_24h",
    "load_lag_168h",
    "rolling_mean_24h",
    "prev_day_total_kwh",
    "temp_forecast",
    "hdd",
    "cdd",
    "cloud_cover",
    "humidity",
    "wind_speed",
    "persons_home",
]


class TestFeatureNames:
    def test_length_is_18(self):
        assert len(feature_names()) == 18

    def test_exact_order_locked(self):
        """HistGBR is positional — column order is a hard contract."""
        assert feature_names() == _EXPECTED_FEATURE_NAMES

    def test_live_ambient_temp_not_in_feature_names(self):
        """Regression: live ambient temp (temp_mean) must NOT appear.
        It is unavailable at forecast-time for future hours (train/serve skew)
        and is redundant with temp_forecast.  This assertion locks the deliberate
        omission so it cannot slip back in accidentally."""
        assert "temp" not in feature_names()

    def test_returns_copy_not_mutable_constant(self):
        """Mutating the returned list must not affect the module constant."""
        names = feature_names()
        names[0] = "TAMPERED"
        assert feature_names()[0] == "hour_sin"


# ---------------------------------------------------------------------------
# Helpers for matrix tests
# ---------------------------------------------------------------------------


def _full_row(hour_ts: str, house_load_mean, **extra) -> dict:
    """Create a row with all typical weather columns (defaults for unset ones)."""
    d: dict = {
        "hour_ts": hour_ts,
        "house_load_mean": house_load_mean,
        "temp_forecast_mean": extra.pop("temp_forecast_mean", 15.0),
        "cloud_cover_mean": extra.pop("cloud_cover_mean", 50.0),
        "humidity_mean": extra.pop("humidity_mean", 70.0),
        "wind_speed_mean": extra.pop("wind_speed_mean", 3.0),
        "temp_mean": extra.pop("temp_mean", 14.5),
        "persons_home_mean": extra.pop("persons_home_mean", 2.0),
    }
    d.update(extra)
    return d


# ---------------------------------------------------------------------------
# 12. build_feature_matrix()
# ---------------------------------------------------------------------------


class TestBuildFeatureMatrix:
    def test_empty_input_returns_empty(self):
        X, y, index = build_feature_matrix([])
        assert X == [] and y == [] and index == []

    def test_nan_target_excluded_from_all_outputs(self):
        """Rows with None house_load_mean are excluded from X, y, and index."""
        rows = [
            _full_row("2026-06-22T10:00:00+00:00", None),  # excluded
            _full_row("2026-06-22T11:00:00+00:00", 500.0),  # kept
            _full_row("2026-06-22T12:00:00+00:00", None),  # excluded
        ]
        X, y, index = build_feature_matrix(rows)
        assert len(X) == 1
        assert y == [500.0]
        assert index == ["2026-06-22T11:00:00+00:00"]

    def test_nan_feature_rows_are_kept(self):
        """Rows with NaN *feature* values (e.g. missing weather) are kept."""
        rows = [
            _full_row("2026-06-22T12:00:00+00:00", 750.0, temp_forecast_mean=None),
        ]
        X, y, index = build_feature_matrix(rows)
        assert len(X) == 1
        assert y[0] == 750.0
        # temp_forecast feature should be NaN
        tf_idx = feature_names().index("temp_forecast")
        assert math.isnan(X[0][tf_idx])

    def test_feature_vector_length_equals_feature_names(self):
        """Each row in X has exactly len(feature_names()) = 17 entries."""
        rows = [_full_row("2026-06-22T12:00:00+00:00", 1000.0)]
        X, y, index = build_feature_matrix(rows)
        assert len(X[0]) == len(feature_names())

    def test_x_columns_align_to_feature_names(self):
        """X[i][j] corresponds to feature_names()[j]."""
        rows = [
            _full_row("2026-06-22T12:00:00+00:00", 500.0, cloud_cover_mean=88.0),
        ]
        X, y, index = build_feature_matrix(rows)
        names = feature_names()
        cc_idx = names.index("cloud_cover")
        assert X[0][cc_idx] == 88.0

    def test_index_matches_kept_hour_ts(self):
        """index contains hour_ts strings for the rows that passed the target filter."""
        rows = [
            _full_row("2026-06-22T10:00:00+00:00", None),  # excluded
            _full_row("2026-06-22T11:00:00+00:00", 100.0),
            _full_row("2026-06-22T12:00:00+00:00", 200.0),
        ]
        X, y, index = build_feature_matrix(rows)
        assert index == ["2026-06-22T11:00:00+00:00", "2026-06-22T12:00:00+00:00"]

    def test_y_and_index_lengths_match_x(self):
        """X, y, index have the same length and ordering."""
        rows = [
            _full_row("2026-06-22T10:00:00+00:00", 100.0),
            _full_row("2026-06-22T11:00:00+00:00", None),  # excluded
            _full_row("2026-06-22T12:00:00+00:00", 300.0),
        ]
        X, y, index = build_feature_matrix(rows)
        assert len(X) == len(y) == len(index) == 2
        assert y == [100.0, 300.0]

    def test_all_none_targets_gives_empty(self):
        """All rows have None target → all three outputs empty."""
        rows = [
            _full_row("2026-06-22T10:00:00+00:00", None),
            _full_row("2026-06-22T11:00:00+00:00", None),
        ]
        X, y, index = build_feature_matrix(rows)
        assert X == [] and y == [] and index == []

    def test_lag_1h_populated_in_matrix(self):
        """load_lag_1h in X matches the previous row's house_load_mean."""
        rows = [
            _full_row("2026-06-22T11:00:00+00:00", 100.0),  # t-1h
            _full_row("2026-06-22T12:00:00+00:00", 200.0),  # target
        ]
        X, y, index = build_feature_matrix(rows)
        names = feature_names()
        lag_idx = names.index("load_lag_1h")
        # Second kept row (hour 12) should have lag_1h = 100.0
        assert X[1][lag_idx] == 100.0

    def test_lag_missing_is_nan_in_matrix(self):
        """Single row with no lag data → lag columns are NaN."""
        rows = [_full_row("2026-06-22T12:00:00+00:00", 500.0)]
        X, y, index = build_feature_matrix(rows)
        names = feature_names()
        for lag_key in ("load_lag_1h", "load_lag_24h", "load_lag_168h", "rolling_mean_24h"):
            idx = names.index(lag_key)
            assert math.isnan(X[0][idx]), f"{lag_key} should be NaN for single row"

    def test_hdd_cdd_computed_in_matrix(self):
        """HDD/CDD are derived from temp_forecast inside the matrix."""
        rows = [
            _full_row("2026-06-22T12:00:00+00:00", 500.0, temp_forecast_mean=5.0),
        ]
        X, y, index = build_feature_matrix(rows)
        names = feature_names()
        assert abs(X[0][names.index("temp_forecast")] - 5.0) < 1e-9
        assert abs(X[0][names.index("hdd")] - 10.5) < 1e-9  # 15.5 - 5.0
        assert X[0][names.index("cdd")] == 0.0

    def test_calendar_features_correct_in_matrix(self):
        """Calendar features reflect local Amsterdam time, not UTC."""
        # 2026-06-22T12:00:00+00:00 = 14:00 CEST (Monday)
        rows = [_full_row("2026-06-22T12:00:00+00:00", 500.0)]
        X, y, index = build_feature_matrix(rows)
        names = feature_names()
        dow_idx = names.index("day_of_week")
        assert X[0][dow_idx] == 0  # Monday = 0

    def test_prev_day_kwh_in_matrix(self):
        """prev_day_total_kwh is correctly assembled inside the matrix."""
        # Target: 2026-06-22T12:00 UTC (14:00 CEST) → prev local day = 2026-06-21
        # 2026-06-20T22:00 UTC = 2026-06-21 00:00 CEST: house_load_kwh_sum=1.0
        rows = [
            _full_row("2026-06-20T22:00:00+00:00", 1000.0, house_load_kwh_sum=1.0),  # prev local day
            _full_row("2026-06-22T12:00:00+00:00", 200.0),  # target
        ]
        X, y, index = build_feature_matrix(rows)
        names = feature_names()
        pdk_idx = names.index("prev_day_total_kwh")
        # First kept row (hour 2026-06-20T22) has no prev-day data → NaN
        assert math.isnan(X[0][pdk_idx])
        # Second kept row (target) has prev local day 2026-06-21 with 1.0 kWh
        assert abs(X[1][pdk_idx] - 1.0) < 1e-9


def test_persons_home_feature_in_matrix():
    rows = [_full_row("2026-06-22T12:00:00+00:00", 500.0, persons_home_mean=3.0)]
    X, y, index = build_feature_matrix(rows)
    names = feature_names()
    assert X[0][names.index("persons_home")] == 3.0


def test_persons_home_missing_is_nan_in_matrix():
    rows = [_full_row("2026-06-22T12:00:00+00:00", 500.0, persons_home_mean=None)]
    X, y, index = build_feature_matrix(rows)
    names = feature_names()
    assert math.isnan(X[0][names.index("persons_home")])
