"""Layer B: serve-time lag-lookup refresh (intraday adaptation of the ML tier)."""

from datetime import datetime, timedelta, timezone, UTC

from custom_components.anker_x1_smartgrid.hgbr import HGBRQuantileModel

T0 = datetime(2026, 7, 4, 0, 0, tzinfo=UTC)


def _rows(n_hours: int, load: float = 500.0) -> list[dict]:
    return [{"hour_ts": (T0 + timedelta(hours=i)).isoformat(), "house_load_mean": load} for i in range(n_hours)]


def test_refresh_populates_lookups_and_returns_true():
    m = HGBRQuantileModel()
    assert m.refresh_lookups(_rows(24)) is True
    assert m._utc_lookup[T0] == 500.0
    assert len(m._utc_lookup) == 24


def test_refresh_replaces_stale_lookups():
    m = HGBRQuantileModel()
    m.refresh_lookups(_rows(24, load=500.0))
    m.refresh_lookups(_rows(30, load=900.0))
    assert m._utc_lookup[T0] == 900.0
    assert len(m._utc_lookup) == 30


def test_refresh_empty_rows_returns_false_keeps_existing():
    m = HGBRQuantileModel()
    m.refresh_lookups(_rows(24))
    assert m.refresh_lookups([]) is False
    assert len(m._utc_lookup) == 24  # untouched


def test_refresh_garbage_rows_never_raises():
    m = HGBRQuantileModel()
    assert m.refresh_lookups([None, 42, "x"]) is False  # type: ignore[list-item]
