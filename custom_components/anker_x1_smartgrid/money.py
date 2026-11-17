"""Shared base of the realised-€ modules: sample parsing, slot geometry, grid cost. No HA imports."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta, tzinfo
from itertools import pairwise

from . import resolution
from .dataquality import soc_glitch
from .models import PriceSlot

MAX_DT_S = 300.0
EMPTY_KWH = 0.01


def parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


def points(rows: list[dict]) -> list[tuple[datetime, dict]]:
    """Rows with ``ts``, ``soc`` and ``import_price`` in ``ts`` order, false SoC-0 readings dropped."""
    pts = sorted(
        (
            (ts, row)
            for row in rows
            if (ts := parse_ts(row.get("ts"))) is not None
            and row.get("soc") is not None
            and row.get("import_price") is not None
        ),
        key=lambda p: p[0],
    )
    bad = glitches(pts)
    return [p for i, p in enumerate(pts) if i not in bad]


def glitches(pts: list[tuple[datetime, dict]]) -> set[int]:
    """Indices of the false SoC-0 readings in ``ts``-ordered ``pts``; rows without a SoC are skipped."""
    out: set[int] = set()
    prev: tuple[datetime, float] | None = None
    latched = False
    for i, (ts, row) in enumerate(pts):
        if row.get("soc") is None:
            continue
        soc = float(row["soc"])
        latched = prev is not None and soc_glitch(
            soc, row.get("batt_w"), prev[1], (ts - prev[0]).total_seconds(), latched
        )
        if latched:
            out.add(i)
        else:
            prev = (ts, soc)
    return out


def slots(horizon: list[dict] | None) -> list[tuple[datetime, dict, float]]:
    """Horizon rows sorted by start, each with its length in hours (from the next start)."""
    rows = sorted(
        ((start, row) for row in horizon or [] if (start := parse_ts(row.get("start"))) is not None),
        key=lambda p: p[0],
    )
    hours = [min((b[0] - a[0]).total_seconds() / 3600.0, 1.0) for a, b in pairwise(rows)]
    hours.append(hours[-1] if hours else 1.0)
    return [(start, row, h) for (start, row), h in zip(rows, hours, strict=False)]


def ahead(horizon: list[dict] | None, now: datetime) -> Iterator[tuple[datetime, dict, float, float]]:
    """Plan rows not over at ``now``, with their hours and the fraction of them still ahead."""
    for start, row, h in slots(horizon):
        end = start + timedelta(hours=h)
        if end > now and row.get("mode") != "actual":
            yield start, row, h, 1.0 if start >= now else (end - now) / (end - start)


def hour_key(ts: datetime, tz: tzinfo, slot_min: int = 60) -> datetime:
    """The start in UTC of the local ``slot_min``-minute clock slot holding ``ts``."""
    loc = ts.astimezone(tz)
    return ts.astimezone(UTC) - timedelta(
        minutes=loc.minute % slot_min, seconds=loc.second, microseconds=loc.microsecond
    )


def grid_eur(kwh: tuple[float, float] | None, price: float | None, export_price: float | None) -> float | None:
    if kwh is None or price is None or (kwh[1] > 1e-6 and export_price is None):
        return None
    return kwh[0] * price - kwh[1] * (export_price or 0.0)


def export_price_fn(
    now: datetime,
    horizon: list[dict],
    export_price: float | None,
    export_slots: list[PriceSlot] | None,
    slot_minutes: int,
    export_matches_import: bool,
    *,
    static: bool,
    effective: Callable[[float], float],
    current_import_price: Callable[[list[dict] | None, datetime], float | None],
) -> Callable[[datetime, float | None], float | None]:
    """Build the export-price resolver ``daily_stats`` and ``calibration``
    use to value a planned kWh.

    Export valuation MIRRORS ``decision.py``'s four-branch ladder, because
    the caller's job is to report what the DP planned — value the same kWh
    differently and the row stops being the plan's net:

    1. static tariff mode → the configured constant, broadcast flat (never
       ratio-scaled: a fixed credit must not swing with an HP/HC import
       schedule);
    2. an explicit per-slot export curve → that slot's price;
    3. ``export_matches_import`` (one entity for both, i.e. salderen) →
       THAT ROW's import price.  ``_resolve_export_slots`` returns [] for
       this config by design, so branch 2 never fires and the old flat
       fallback booked every future slot at the current spot — live on
       2026-08-02 that valued a 0.36 €/kWh evening peak at 0.2319 and
       under-reported today's net by €1.36;
    4. otherwise → the import curve ratio-scaled by
       ``export_price / current import``.

    Every branch goes through ``effective_export_price`` so the feed-in fee
    is subtracted exactly once.  A ``None`` export price zeroes the revenue
    leg only, matching the DP's own "no export credit" branch.
    """
    _curve = resolution.resample_price_map(export_slots, slot_minutes) if export_slots else {}
    _flat = effective(export_price) if export_price is not None else None
    _static = static
    _cur_import = current_import_price(horizon, now)

    def _export_price_at(start: datetime, import_price: float | None) -> float | None:
        _raw = _curve.get(start)
        if _raw is not None:
            return effective(_raw)
        if _static or export_price is None or import_price is None:
            return _flat
        if export_matches_import:
            return effective(import_price)
        if _cur_import is not None and _cur_import > 1e-9:
            _scaled = import_price * (export_price / _cur_import)
            return effective(_scaled)
        return _flat

    return _export_price_at
