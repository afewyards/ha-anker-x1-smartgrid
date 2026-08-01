"""Per-day grid-charge / grid-export kWh + cash-€ statistics (display only).

Spec: docs/superpowers/specs/2026-08-01-card-crop-daily-stats-design.md

Pure module — NO Home Assistant imports (the local timezone arrives as an
explicit ``tzinfo`` parameter).  Two independent producers feed one merge:

- :func:`aggregate_actual_days` replays recorded per-tick samples into
  measured per-day totals.  Its attribution is *definitionally* the same as
  the live ``ledger.CashLedger``: within one tick every v9 kWh delta column
  is ``W x the same clamped dt`` (see ``recorder.append``), so ``min`` of two
  energies equals ``min`` of the two powers x dt — exactly
  ``optimize.cash_energy_kwh``.
- :func:`aggregate_planned_days` sums the forward plan horizon.

:func:`merge_days` stitches past / today / future into one ordered list.
Never affects control.
"""

from __future__ import annotations

from datetime import date, datetime, tzinfo

# Rolling history depth of the merged table, in days back from today.
WINDOW_DAYS = 14

_ZERO: dict = {
    "grid_charge_kwh": 0.0,
    "grid_export_kwh": 0.0,
    "cost_eur": 0.0,
    "revenue_eur": 0.0,
    "coverage_ticks": 0,
    "null_ticks": 0,
}

# The four v9 per-tick delta columns the attribution needs. A row missing any
# one of them cannot be attributed at all (see aggregate_actual_days).
_DELTA_COLUMNS = ("grid_import_kwh", "grid_export_kwh", "batt_charge_kwh", "batt_discharge_kwh")


def new_day_totals() -> dict:
    """Fresh zeroed ``DayTotals`` dict (plain dict — HA-attribute friendly)."""
    return dict(_ZERO)


def _parse(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


def aggregate_actual_days(
    rows: list[dict],
    export_fee_eur_per_kwh: float,
    tz: tzinfo,
) -> dict[date, dict]:
    """Group recorder sample rows into measured per-local-day totals.

    ``rows`` are column-keyed dicts from ``DataRecorder.read_feature_rows``;
    ``ts`` is an aware-UTC ISO string.  ``export_price`` in the recorder is
    the RAW feed-in tariff, so the fee is subtracted here (mirrors
    ``optimize.effective_export_price``).

    A row missing any of the four v9 delta columns cannot be attributed; it
    increments ``null_ticks`` and contributes nothing, so a gappy day reads as
    gappy rather than silently small.  A NULL price zeroes only its own €
    leg — the kWh is still counted (France runs with no export-price entity).
    """
    out: dict[date, dict] = {}
    for row in rows:
        ts = _parse(row.get("ts"))
        if ts is None:
            continue
        rec = out.setdefault(ts.astimezone(tz).date(), new_day_totals())
        if any(row.get(col) is None for col in _DELTA_COLUMNS):
            rec["null_ticks"] += 1
            continue
        rec["coverage_ticks"] += 1
        grid_charge_kwh = min(float(row["grid_import_kwh"]), float(row["batt_charge_kwh"]))
        batt_export_kwh = min(float(row["grid_export_kwh"]), float(row["batt_discharge_kwh"]))
        rec["grid_charge_kwh"] += grid_charge_kwh
        rec["grid_export_kwh"] += batt_export_kwh
        import_price = row.get("import_price")
        if import_price is not None:
            rec["cost_eur"] += grid_charge_kwh * float(import_price)
        export_price = row.get("export_price")
        if export_price is not None:
            rec["revenue_eur"] += batt_export_kwh * (float(export_price) - export_fee_eur_per_kwh)
    return out


def aggregate_planned_days(
    horizon: list[dict] | None,
    export_price_at,
    tz: tzinfo,
) -> dict[date, dict]:
    """Group forward plan-horizon rows into planned per-local-day totals.

    Skips two row classes:

    - ``estimated`` rows — the estimated-tomorrow tail past the real price
      edge.  Same rule the card applies (spec 2026-08-01-card-crop): the
      table reports only what real tariff supports.
    - ``mode == "actual"`` rows — past slots back-filled from measurements by
      ``past_actuals``.  Counting them here would double-count against the
      ledger figures ``merge_days`` uses for today.

    ``export_price_at(start) -> float | None`` is caller-supplied (keeps this
    module HA-free) and MUST already be post-fee — see
    ``optimize.effective_export_price``.  ``None`` zeroes the revenue leg only.
    """
    out: dict[date, dict] = {}
    for row in horizon or []:
        if row.get("estimated") or row.get("mode") == "actual":
            continue
        start = _parse(row.get("start"))
        if start is None:
            continue
        rec = out.setdefault(start.astimezone(tz).date(), new_day_totals())
        charge_kwh = float(row.get("grid_charge_kwh") or 0.0)
        export_kwh = float(row.get("grid_export_kwh") or 0.0)
        rec["grid_charge_kwh"] += charge_kwh
        rec["grid_export_kwh"] += export_kwh
        price = row.get("price")
        if price is not None:
            rec["cost_eur"] += charge_kwh * float(price)
        export_price = export_price_at(start)
        if export_price is not None:
            rec["revenue_eur"] += export_kwh * float(export_price)
    return out
