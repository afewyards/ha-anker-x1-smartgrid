"""Occupancy corrector: binning, bands, table build."""
from datetime import datetime, timedelta, timezone

from custom_components.anker_x1_smartgrid import occupancy


def _row(hour_ts, persons, kwh, count=60):
    return {
        "hour_ts": hour_ts, "persons_home_mean": persons,
        "house_load_kwh_sum": kwh, "house_load_count": count,
        "house_load_mean": None,
    }


def _weekday_date(i):
    # i-th weekday (Mon-Fri) of June 2026, which starts on a Monday — avoids
    # landing sample rows on the weekend days scattered through any 20/25-day
    # calendar span (June 2026 has weekends on 6-7, 13-14, 20-21, 27-28).
    week, wd = divmod(i, 5)
    return f"2026-06-{1 + week * 7 + wd:02d}"


def test_state_bin_none_nan_and_negative():
    assert occupancy.state_bin(None) is None
    assert occupancy.state_bin(float("nan")) is None
    assert occupancy.state_bin(-1.0) is None


def test_state_bin_away_rounding_and_cap():
    assert occupancy.state_bin(0.0) == 0
    assert occupancy.state_bin(0.24) == 0          # < _AWAY_EPS → away
    assert occupancy.state_bin(0.3) == 1           # occupied but < 1 rounds up to min 1
    assert occupancy.state_bin(1.4) == 1
    assert occupancy.state_bin(1.6) == 2
    assert occupancy.state_bin(5.0) == 3           # capped at 3+


def test_band_of_local_time_and_weekend():
    # 2026-07-08 is a Wednesday; 05:00 UTC = 07:00 Amsterdam (CEST) → band 1 (morning)
    assert occupancy.band_of(datetime(2026, 7, 8, 5, 0, tzinfo=timezone.utc)) == (1, False)
    # 22:30 UTC Wed = 00:30 Thu local → band 0 (night), still weekday
    assert occupancy.band_of(datetime(2026, 7, 8, 22, 30, tzinfo=timezone.utc)) == (0, False)
    # Saturday 12:00 UTC = 14:00 local → band 2, weekend
    assert occupancy.band_of(datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)) == (2, True)


def test_build_table_cells_and_climo():
    rows = []
    # 25 weekday-afternoon hours at state 2 (2 persons, ~600 W) → trusted count cell
    for d in range(25):
        rows.append(_row(f"{_weekday_date(d % 22)}T12:00:00+00:00", 2.0, 0.6))
    # 25 weekday-afternoon hours at state 0 (~300 W)
    for d in range(25):
        rows.append(_row(f"{_weekday_date(d % 22)}T13:00:00+00:00", 0.0, 0.3))
    t = occupancy.build_table(rows)
    # 12:00/13:00 UTC in June = 14:00/15:00 CEST → band 2, weekday
    mean2, n2 = t.count_cells[(2, False, 2)]
    mean0, n0 = t.count_cells[(2, False, 0)]
    assert n2 == 25 and n0 == 25
    assert abs(mean2 - 600.0) < 1e-6 and abs(mean0 - 300.0) < 1e-6
    # binary cells aggregate 0 vs >=1
    assert t.binary_cells[(2, False, 1)][1] == 25
    assert t.binary_cells[(2, False, 0)][1] == 25
    # climo state for the band: mean persons over all 50 rows = 1.0 → state 1
    assert t.climo_state[(2, False)] == 1
    assert t.cells_ready == 4  # two count cells + two binary cells past floor


def test_build_table_skips_null_and_bad_rows():
    rows = [
        _row("2026-06-01T12:00:00+00:00", None, 0.5),   # no persons → skip
        _row("2026-06-01T13:00:00+00:00", 1.0, None),    # no load → skip
        _row("not-a-date", 1.0, 0.5),                    # bad ts → skip
    ]
    t = occupancy.build_table(rows)
    assert not t.count_cells and not t.binary_cells and t.cells_ready == 0


def test_build_table_coverage_rescale():
    # 30-tick hour (half coverage) at 0.25 kWh → hourly_load_w rescales to ~500 W
    rows = [_row(f"{_weekday_date(d)}T12:00:00+00:00", 1.0, 0.25, count=30) for d in range(20)]
    t = occupancy.build_table(rows)
    mean, n = t.count_cells[(2, False, 1)]
    assert n == 20
    assert abs(mean - 500.0) < 1.0


NOW = datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc)  # Wed 14:00 CEST → band 2, weekday


def _table(count=None, binary=None, climo=None, ready=0):
    return occupancy.OccupancyTable(count or {}, binary or {}, climo or {}, ready)


def _full_table():
    # band 2 weekday: away 300 W, 1p 500 W, 2p 600 W, all trusted; climo state 1
    return _table(
        count={(2, False, 0): (300.0, 25), (2, False, 1): (500.0, 25), (2, False, 2): (600.0, 25)},
        binary={(2, False, 0): (300.0, 25), (2, False, 1): (550.0, 50)},
        climo={(2, False): 1},
    )


def test_multiplier_neutral_cases():
    t = _full_table()
    assert occupancy.multiplier(None, 2, NOW, NOW, 4, 1.0) == 1.0        # no table
    assert occupancy.multiplier(t, None, NOW, NOW, 4, 1.0) == 1.0        # no person entities
    assert occupancy.multiplier(t, 2, NOW, NOW, 4, 0.0) == 1.0           # fraction off
    assert occupancy.multiplier(t, 1, NOW, NOW, 4, 1.0) == 1.0           # matches climo


def test_multiplier_deviation_count_level():
    t = _full_table()
    # 2 home vs climo 1 → 600/500 = 1.2 at fraction 1.0
    assert abs(occupancy.multiplier(t, 2, NOW, NOW, 4, 1.0) - 1.2) < 1e-9
    # fraction 0.5 → 1.1
    assert abs(occupancy.multiplier(t, 2, NOW, NOW, 4, 0.5) - 1.1) < 1e-9
    # away vs climo 1 → 300/500 = 0.6
    assert abs(occupancy.multiplier(t, 0, NOW, NOW, 4, 1.0) - 0.6) < 1e-9


def test_multiplier_persistence_cutoff():
    t = _full_table()
    within = NOW + timedelta(hours=3, minutes=59)
    beyond = NOW + timedelta(hours=4)
    assert occupancy.multiplier(t, 0, within, NOW, 4, 1.0) != 1.0
    assert occupancy.multiplier(t, 0, beyond, NOW, 4, 1.0) == 1.0


def test_multiplier_same_level_hierarchy_falls_back_to_binary():
    # count cell for state 2 too thin (n=5) → BOTH sides resolve at binary level
    t = _table(
        count={(2, False, 2): (600.0, 5), (2, False, 1): (500.0, 25)},
        binary={(2, False, 0): (300.0, 25), (2, False, 1): (550.0, 50)},
        climo={(2, False): 1},
    )
    # 2 home vs climo 1: binary(1)/binary(1) = 1.0 — same binary side → neutral
    assert occupancy.multiplier(t, 2, NOW, NOW, 4, 1.0) == 1.0
    # away vs climo 1: binary 300/550
    assert abs(occupancy.multiplier(t, 0, NOW, NOW, 4, 1.0) - 300.0 / 550.0) < 1e-9


def test_multiplier_clamps():
    t = _table(
        count={(2, False, 0): (100.0, 25), (2, False, 1): (1000.0, 25)},
        climo={(2, False): 1},
    )
    assert occupancy.multiplier(t, 0, NOW, NOW, 4, 1.0) == occupancy.MULT_MIN


class _Base:
    def predict(self, when, temp, fallback_w, *, quantile=0.5):
        return 1000.0


def test_occupancy_predictor_wraps_base():
    p = occupancy.OccupancyPredictor(_Base(), _full_table(), 2, NOW, 4, 1.0)
    assert abs(p.predict(NOW, 20.0, 250.0) - 1200.0) < 1e-6
    assert abs(p.predict(NOW + timedelta(hours=5), 20.0, 250.0) - 1000.0) < 1e-6
