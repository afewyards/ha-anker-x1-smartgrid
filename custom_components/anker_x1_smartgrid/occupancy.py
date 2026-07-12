"""Occupancy-deviation corrector for the load forecast (Layer B).

Scales served load predictions by how much the CURRENT persons-home count
deviates from the climatological (typical) occupancy for the same local
time-band.  Base models already encode the usual presence pattern implicitly,
so when occupancy matches climatology the multiplier is 1.0 by construction.
Tier-agnostic predictor wrapper, same pattern as ``load_adapt.py``.
``occ_adapt_fraction=0.0`` disables entirely (wrapper never constructed —
byte-identical planning).  Pure module: no HA imports, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, UTC
from zoneinfo import ZoneInfo

from . import featureset

_TZ_AMS = ZoneInfo("Europe/Amsterdam")

STATE_MAX = 3  # counts >= 3 bin together as "3+"
MIN_CELL_HOURS = 20  # hours before a (band, daytype, state) cell is trusted
MULT_MIN = 0.5
MULT_MAX = 2.0
_AWAY_EPS = 0.25  # hourly persons_home_mean below this bins as away (0)


def state_bin(persons_mean: float | None) -> int | None:
    """Bin an hourly persons-home mean into 0 (away), 1, 2, 3 (=3+)."""
    if persons_mean is None:
        return None
    v = float(persons_mean)
    if v != v or v < 0.0:  # NaN / negative guard
        return None
    if v < _AWAY_EPS:
        return 0
    return min(STATE_MAX, max(1, round(v)))


def band_of(when_utc: datetime) -> tuple[int, bool]:
    """(band, weekend) in Europe/Amsterdam local time.

    band: 0 = night 00-06, 1 = morning 06-12, 2 = afternoon 12-18,
    3 = evening 18-24.  Coarse on purpose: robust at ~1 week of history.
    """
    local = when_utc.astimezone(_TZ_AMS)
    return local.hour // 6, local.weekday() >= 5


@dataclass
class OccupancyTable:
    """Load-by-occupancy lookup built from hourly rollups."""

    count_cells: dict[tuple[int, bool, int], tuple[float, int]]
    binary_cells: dict[tuple[int, bool, int], tuple[float, int]]
    climo_state: dict[tuple[int, bool], int]
    cells_ready: int


def build_table(rows: list[dict]) -> OccupancyTable:
    """Aggregate hourly rows into occupancy cells + per-band climatology.

    rows: ``recorder.read_hourly_rows()`` dicts.  Hours missing either
    ``persons_home_mean`` or a load value are skipped (pre-v8 rows, gaps).
    """
    count_acc: dict[tuple[int, bool, int], list[float]] = {}
    bin_acc: dict[tuple[int, bool, int], list[float]] = {}
    climo_acc: dict[tuple[int, bool], list[float]] = {}
    for r in rows:
        st = state_bin(r.get("persons_home_mean"))
        load_w = featureset.hourly_load_w(r)
        ts_raw = r.get("hour_ts")
        if st is None or load_w is None or ts_raw is None:
            continue
        try:
            when = datetime.fromisoformat(str(ts_raw))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        band, weekend = band_of(when)
        count_acc.setdefault((band, weekend, st), []).append(load_w)
        bin_acc.setdefault((band, weekend, 1 if st > 0 else 0), []).append(load_w)
        climo_acc.setdefault((band, weekend), []).append(float(r["persons_home_mean"]))
    count_cells = {k: (sum(v) / len(v), len(v)) for k, v in count_acc.items()}
    binary_cells = {k: (sum(v) / len(v), len(v)) for k, v in bin_acc.items()}
    climo_state = {k: (state_bin(sum(v) / len(v)) or 0) for k, v in climo_acc.items()}
    ready = sum(1 for _, n in count_cells.values() if n >= MIN_CELL_HOURS) + sum(
        1 for _, n in binary_cells.values() if n >= MIN_CELL_HOURS
    )
    return OccupancyTable(count_cells, binary_cells, climo_state, ready)


def _trusted(cells: dict, key: tuple) -> tuple[float, int] | None:
    c = cells.get(key)
    return c if c is not None and c[1] >= MIN_CELL_HOURS else None


def multiplier(
    table: OccupancyTable | None,
    occ_now: int | None,
    when: datetime,
    now: datetime,
    persistence_h: int,
    fraction: float,
) -> float:
    """Occupancy-deviation multiplier for a prediction at ``when``.

    1.0 whenever: no table / no person entities / fraction off / beyond the
    persistence window / occupancy matches climatology / cells too thin.
    Numerator and denominator always resolve at the SAME hierarchy level
    (count → binary → neutral) so a trusted mean is never divided by an
    untrusted one.
    """
    if table is None or occ_now is None or fraction <= 0.0:
        return 1.0
    if when >= now + timedelta(hours=persistence_h):
        return 1.0
    band, weekend = band_of(when)
    occ_state = min(STATE_MAX, max(0, int(occ_now)))
    climo = table.climo_state.get((band, weekend))
    if climo is None or occ_state == climo:
        return 1.0
    num = _trusted(table.count_cells, (band, weekend, occ_state))
    den = _trusted(table.count_cells, (band, weekend, climo))
    if num is None or den is None:
        num = _trusted(table.binary_cells, (band, weekend, 1 if occ_state > 0 else 0))
        den = _trusted(table.binary_cells, (band, weekend, 1 if climo > 0 else 0))
    if num is None or den is None or den[0] <= 0.0:
        return 1.0
    m_eff = 1.0 + fraction * (num[0] / den[0] - 1.0)
    return min(MULT_MAX, max(MULT_MIN, m_eff))


class OccupancyPredictor:
    """Duck-typed predictor wrapper: base × occupancy-deviation multiplier."""

    def __init__(
        self,
        base,
        table: OccupancyTable | None,
        occ_now: int | None,
        now: datetime,
        persistence_h: int,
        fraction: float,
    ) -> None:
        self._base = base
        self._table = table
        self._occ_now = occ_now
        self._now = now
        self._persistence_h = int(persistence_h)
        self._fraction = float(fraction)

    def predict(
        self,
        when: datetime,
        temp: float | None,
        fallback_w: float,
        *,
        quantile: float = 0.5,
    ) -> float:
        base_w = self._base.predict(when, temp, fallback_w, quantile=quantile)
        return base_w * multiplier(
            self._table,
            self._occ_now,
            when,
            self._now,
            self._persistence_h,
            self._fraction,
        )
