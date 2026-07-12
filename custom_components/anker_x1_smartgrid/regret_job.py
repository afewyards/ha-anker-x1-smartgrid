"""Daily-regret batch job: nightly scoring + backfill, extracted verbatim
from ``controller.py`` (Task C3, 2026-07-12 refactor).

These are the executor-thread bodies the controller runs off the event
loop (via ``async_add_executor_job``) once per completed local calendar
day. They call ``regret.hindsight_optimal_grid`` / ``optimize.optimize_grid``
/ ``regret.realized_grid_cost`` / ``regret.score_regret`` — the oracle/DP
mirror pair whose output must stay byte-identical across this move
(PARITY-CRITICAL).

``controller.py`` keeps thin wrapper methods (``_run_daily_regret_sync``,
``_backfill_regret_sync``) that pass ``self._recorder`` / ``self.cfg`` /
``self._detected_slot_minutes`` in explicitly and apply the returned
``updates`` dict back onto ``self.last_regret`` / ``self.last_dp_regret_7d``
— see those methods for the exact assignment semantics. Only the keys the
underlying computation actually set are present in ``updates``, so an
early return (no samples, sparse day, no SoC data, or a caught exception)
returns an empty/partial dict and the controller leaves the corresponding
attribute untouched, matching the pre-extraction self-mutating behavior
exactly.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from homeassistant.util import dt as dt_util

from . import const, optimize as optimize_mod, regret as regret_mod
from .dataquality import house_load_w as _house_load_w

_LOGGER = logging.getLogger(__name__)


def run_daily_regret(
    recorder,
    cfg,
    day: str,
    computed_ts: str,
    *,
    slot_minutes: int,
) -> dict:
    """Synchronous: compute and persist the regret score for a completed local calendar day.

    Reads yesterday's samples from the recorder, buckets them by LOCAL hour,
    calls regret.hindsight_optimal_grid / realized_grid_cost / score_regret,
    then upserts the result into daily_regret.
    Never raises — any error is logged and the run is silently skipped.

    Parameters
    ----------
    recorder     : DataRecorder handle (``self._recorder`` at the call site).
    cfg          : Config (``self.cfg`` at the call site).
    day          : YYYY-MM-DD LOCAL calendar day string.
    computed_ts  : ISO-8601 UTC timestamp to store as computed_ts in the row.
    slot_minutes : effective slot resolution (``self._detected_slot_minutes``
                   at the call site).

    Returns
    -------
    dict with keys present only when the corresponding controller attribute
    was actually written by this run: ``"last_regret"`` (self.last_regret)
    and/or ``"last_dp_regret_7d"`` (self.last_dp_regret_7d). An early
    return (no samples / sparse day / no SoC data) or a caught exception
    yields a dict missing one or both keys — the caller must leave the
    matching attribute untouched in that case, exactly as the original
    self-mutating method did.
    """
    updates: dict = {}
    try:
        # Build a UTC read window wide enough to cover the local day in any timezone.
        # We anchor at UTC midnight of the *same YYYY-MM-DD string* (not the actual
        # local midnight, which differs by the TZ offset).  The ±14h / +38h buffer
        # covers every real-world UTC offset (max ±14h), so every tick that local-date
        # belongs to *day* is captured.  Per-sample bucketing (dt_util.as_local below)
        # then ensures only ticks whose local date == *day* are counted — the wide
        # window merely avoids missing anything at the edges.
        # NOTE v1 limitation: triggering is now based on LOCAL midnight
        # (dt_util.as_local), but the read window below is still anchored at UTC
        # midnight of the date string.  A future improvement could anchor the window
        # at the actual local midnight (ref_utc = dt_util.as_local → to_utc) for
        # tighter reads.  The current approach is correct but reads a wider window.
        year, month, dom = int(day[:4]), int(day[5:7]), int(day[8:10])
        ref_utc = datetime(year, month, dom, tzinfo=timezone.utc)
        since_iso = (ref_utc - timedelta(hours=14)).isoformat()
        until_iso = (ref_utc + timedelta(hours=38)).isoformat()

        samples = recorder.read_feature_rows(since_iso=since_iso)
        # Filter to UTC samples that fall inside the over-sized window.
        samples = [s for s in samples if s.get("ts", "") < until_iso]

        if not samples:
            _LOGGER.debug("Daily regret skipped for %s: no samples in window", day)
            return updates

        # Slot resolution for the LOCAL-day buckets below. At 60-min
        # _slot_minutes=60, _spd=24 -> byte-identical to the legacy hourly
        # ledger; at 15-min _spd=96 so each bucket is a quarter-hour.
        _slot_minutes = slot_minutes
        _spd = 24 * 60 // _slot_minutes
        # Known cosmetic limitation: this assumes exactly 24h/day, so DST
        # fold/gap days (~2/yr) blur one bucket at the transition boundary.
        # Hours per slot: 1.0 at 60-min (identity), 0.25 at 15-min. Used to
        # convert average-W buckets to per-slot kWh below, and threaded into
        # every DP-optimal / realized-cost call so the OPTIMAL and REALIZED
        # sides share the same slot width (BC3).
        _dt_h = _slot_minutes / 60.0

        # Aggregate raw per-tick samples into LOCAL per-slot buckets.
        pv_by_hour: dict[int, list[float]] = defaultdict(list)
        load_by_hour: dict[int, list[float]] = defaultdict(list)
        price_by_hour: dict[int, list[float]] = defaultdict(list)
        charge_by_hour: dict[int, list[float]] = defaultdict(list)
        # F3: aggregate actual export (from p1_w < 0) and feed-in price per hour.
        actual_export_by_hour: dict[int, list[float]] = defaultdict(list)
        export_price_by_hour: dict[int, list[float]] = defaultdict(list)
        hours_with_data: set[int] = set()

        # R1: measured v9 per-tick energy-delta buckets (recorder.py's
        # pv_kwh/house_load_kwh/batt_charge_kwh/grid_import_kwh/
        # grid_export_kwh/batt_discharge_kwh columns).  Only ticks with a
        # non-NULL delta for a given quantity contribute — a NULL tick is
        # simply "not usable" for that quantity and is excluded (not
        # treated as zero).  When an hour ends up with zero usable ticks
        # for a quantity, the mean-W×dt_h path below is used instead
        # (pre-v9 rows are byte-identical to today).  See
        # docs/superpowers/specs/2026-07-06-per-tick-energy-accounting-
        # design.md.
        pv_kwh_delta_by_hour: dict[int, list[float]] = defaultdict(list)
        load_kwh_delta_by_hour: dict[int, list[float]] = defaultdict(list)
        # charge_kwh_delta_by_hour holds GRID-SOURCED battery-charge energy
        # only (min(batt_charge_kwh, grid_import_kwh) per usable tick — see
        # below).  batt_charge_kwh alone is TOTAL battery charge (solar +
        # grid); realized_grid_cost bills this as deliberate grid charging
        # while solar charging is already accounted for separately via
        # _apply_solar_load, so using the raw column here would double-
        # count solar-charged energy as phantom grid cost.
        charge_kwh_delta_by_hour: dict[int, list[float]] = defaultdict(list)
        # Battery-sourced export energy analogue of actual_export_w (min of
        # W): min(grid_export_kwh, batt_discharge_kwh) per tick, only when
        # BOTH columns are non-NULL on that tick.
        export_kwh_delta_by_hour: dict[int, list[float]] = defaultdict(list)

        soc_start: float | None = None
        for s in samples:
            ts_str = s.get("ts", "")
            if not ts_str:
                continue
            try:
                ts_dt = datetime.fromisoformat(ts_str)
            except ValueError:
                continue
            # Convert to local time and skip if it doesn't belong to *day*.
            local_dt = dt_util.as_local(ts_dt)
            if local_dt.date().isoformat() != day:
                continue
            h = (local_dt.hour * 60 + local_dt.minute) // _slot_minutes
            hours_with_data.add(h)
            if soc_start is None and s.get("soc") is not None:
                soc_start = float(s["soc"])

            pv_raw = float(s["pv_w"]) if s.get("pv_w") is not None else 0.0
            batt_raw = float(s["batt_w"]) if s.get("batt_w") is not None else 0.0
            p1_raw = float(s["p1_w"]) if s.get("p1_w") is not None else 0.0

            # House load: prefer the recorded load_w column (now computed
            # per-tick from pv+meter+batt−loss by _compute_house_load_w,
            # not read from sensor.power_usage); fall back to AC energy
            # balance p1+batt+pv for older rows that predate that column.
            # Clamped to 0 to avoid negative "load" during export.
            _raw_load = _house_load_w(s)
            load_w = max(0.0, _raw_load) if _raw_load is not None else max(0.0, p1_raw + batt_raw + pv_raw)

            # Grid charge = battery charging current minus the solar surplus
            # that the battery could absorb without pulling from grid.
            battery_charge_w = max(0.0, -batt_raw)       # >0 only when charging
            solar_surplus_w = max(0.0, -(p1_raw + batt_raw))  # surplus to meter
            grid_charge_w = max(0.0, battery_charge_w - solar_surplus_w)

            # F3 (review 2.2): realized export must be BATTERY-ONLY to match
            # the oracle and shadow-DP (battery discharge actions).  Metered
            # −p1_w includes PV spill the oracle can't credit → sunny-day
            # regret went artificially negative.  batt_raw > 0 = discharging.
            actual_export_w = min(max(0.0, -p1_raw), max(0.0, batt_raw))

            pv_by_hour[h].append(pv_raw)
            load_by_hour[h].append(load_w)
            if s.get("import_price") is not None:
                price_by_hour[h].append(float(s["import_price"]))
            charge_by_hour[h].append(grid_charge_w)
            # Actual export and feed-in price (E1-fixed export_price column).
            actual_export_by_hour[h].append(actual_export_w)
            if s.get("export_price") is not None:
                export_price_by_hour[h].append(float(s["export_price"]))

            # R1: measured v9 energy deltas, one usable-tick list per hour.
            # A NULL value for a column on this tick means this tick is not
            # usable for that quantity (not zero) — simply not appended.
            if s.get("pv_kwh") is not None:
                pv_kwh_delta_by_hour[h].append(float(s["pv_kwh"]))
            if s.get("house_load_kwh") is not None:
                load_kwh_delta_by_hour[h].append(float(s["house_load_kwh"]))
            # Grid-sourced battery-charge delta: min-of-energy rule,
            # mirroring grid_charge_w's min-of-power rule above (and the
            # export delta's min-of-energy rule below).  batt_charge_kwh
            # is TOTAL battery charge (solar + grid); battery charge
            # sourced from the grid cannot exceed metered import, so
            # min(batt_charge_kwh, grid_import_kwh) isolates the
            # grid-sourced portion — a solar-only charging tick
            # (grid_import_kwh == 0) contributes 0.0, not the full
            # battery-charge energy.  Both columns must be non-NULL on
            # this tick; else the tick is not usable.
            _batt_charge_kwh = s.get("batt_charge_kwh")
            _grid_import_kwh = s.get("grid_import_kwh")
            if _batt_charge_kwh is not None and _grid_import_kwh is not None:
                charge_kwh_delta_by_hour[h].append(
                    min(max(0.0, float(_batt_charge_kwh)), max(0.0, float(_grid_import_kwh)))
                )
            # Battery-sourced export delta: min-of-energy rule, mirroring
            # actual_export_w's min-of-power rule.  Both columns must be
            # non-NULL on this tick; else the tick is not usable.
            _grid_export_kwh = s.get("grid_export_kwh")
            _batt_discharge_kwh = s.get("batt_discharge_kwh")
            if _grid_export_kwh is not None and _batt_discharge_kwh is not None:
                export_kwh_delta_by_hour[h].append(
                    min(max(0.0, float(_grid_export_kwh)), max(0.0, float(_batt_discharge_kwh)))
                )

        # Sparse-day guard: require at least half the day's slots present.
        if len(hours_with_data) < _spd // 2:
            _LOGGER.debug(
                "Daily regret skipped for %s: only %d slots with data (< %d)",
                day, len(hours_with_data), _spd // 2,
            )
            return updates

        if soc_start is None:
            _LOGGER.debug("Daily regret skipped for %s: no SoC data", day)
            return updates

        def _mean(lst: list[float], fallback: float = 0.0) -> float:
            return sum(lst) / len(lst) if lst else fallback

        def _energy_kwh(deltas: list[float], fallback_kwh: float) -> float:
            """R1: prefer the SUM of this slot's usable measured energy
            deltas; fall back to the mean-W×dt_h estimate ONLY when zero
            deltas were usable (pre-v9 rows, first-tick-after-restart, or a
            sensor blip that nulled every tick in this slot) — that fallback
            is byte-identical to the pre-R1 behaviour.
            """
            return sum(deltas) if deltas else fallback_kwh

        pv_kwh = tuple(
            _energy_kwh(
                pv_kwh_delta_by_hour[h], _mean(pv_by_hour[h]) / 1000.0 * _dt_h,
            )
            for h in range(_spd)
        )
        load_kwh = tuple(
            _energy_kwh(
                load_kwh_delta_by_hour[h],
                _mean(load_by_hour[h], const.DEFAULT_FALLBACK_LOAD_W) / 1000.0 * _dt_h,
            )
            for h in range(_spd)
        )
        price = tuple(_mean(price_by_hour[h], 0.20) for h in range(_spd))
        # Realized GRID-SOURCED charge per slot: prefer the sum of
        # min(batt_charge_kwh, grid_import_kwh) usable-tick deltas (see
        # charge_kwh_delta_by_hour above) so solar-charged energy is
        # excluded; fall back to mean(grid_charge_w)×dt_h per slot, which
        # already applies the equivalent min-of-power rule
        # (grid_charge_w = max(0, battery_charge_w − solar_surplus_w)).
        realized_charge = [
            _energy_kwh(
                charge_kwh_delta_by_hour[h], _mean(charge_by_hour[h]) / 1000.0 * _dt_h,
            )
            for h in range(_spd)
        ]

        # F3/R1: build per-slot actual export (AC kWh) and feed-in price
        # arrays.  Actual export prefers the measured battery-sourced delta
        # sum (min(grid_export_kwh, batt_discharge_kwh) per usable tick);
        # falls back to mean(actual_export_w)×dt_h — derived from metered
        # −p1_w capped at the battery's discharge power (NOT commanded
        # setpoint) — see actual_export_w above.  Only pass to the scoring
        # functions when at least one export slot has a known feed-in price
        # (otherwise no revenue can be computed).
        _realized_export_kwh: list[float] | None = None
        _export_price_tuple: tuple[float, ...] | None = None
        if export_price_by_hour:
            _realized_export_kwh = [
                _energy_kwh(
                    export_kwh_delta_by_hour[h],
                    _mean(actual_export_by_hour[h]) / 1000.0 * _dt_h,
                )
                for h in range(_spd)
            ]
            # Mean feed-in price per slot; 0.0 for slots without export_price data.
            _export_price_tuple = tuple(
                _mean(export_price_by_hour[h]) if export_price_by_hour[h] else 0.0
                for h in range(_spd)
            )

        # Water-value terminal: value end-SoC by the realized day's trough so
        # the oracle shares the live planner's objective (regret stays
        # internally consistent).
        _terminal_mode = "water_value"
        _water_value = optimize_mod.compute_water_value(min(price), cfg)

        # Build fee-adjusted export prices for the nightly regret scorer below.
        # Raw feed-in prices are reduced by cfg.export_fee_eur_per_kwh so that the
        # oracle and DP both see the same effective (net) price that the live planner
        # uses when deciding to export.  None when export is disabled or data absent.
        eff_export: list[float] | None = None
        if _export_price_tuple is not None and cfg.enable_export:
            eff_export = [
                optimize_mod.effective_export_price(p, cfg)
                for p in _export_price_tuple
            ]

        day_data = regret_mod.DayData(
            pv_kwh=pv_kwh,
            load_kwh=load_kwh,
            price=price,
            soc_start=soc_start,
        )
        optimal = regret_mod.hindsight_optimal_grid(
            day_data, cfg,
            terminal_mode=_terminal_mode, water_value=_water_value,
            export_price=eff_export,
            dt_h=_dt_h,
        )

        # Shadow DP regret: compute the DP schedule on realized data (perfect
        # foresight) and score it against the oracle. This inline scorer is the
        # single implementation (the standalone walk_forward_regret test harness
        # was removed in Task 13). Stored alongside the heuristic regret for
        # 7-day comparison.
        # Never raises — failure → dp_regret_eur stays None.
        dp_regret_eur: float | None = None
        if not optimal.get("infeasible", False):
            try:
                _dp_result = optimize_mod.optimize_grid(
                    list(pv_kwh),
                    list(load_kwh),
                    list(price),
                    soc_start,
                    cfg,
                    window_start_h=0,
                    window_len=_spd,
                    slots_per_day=_spd,
                    terminal_mode=_terminal_mode,
                    water_value=_water_value,
                    export_price=eff_export,
                    dt_h=_dt_h,
                )
                _dp_export = _dp_result.get("export_schedule")
                _dp_realized = regret_mod.realized_grid_cost(
                    day_data, _dp_result["schedule"], cfg,
                    realized_export_by_hour=_dp_export,
                    export_price=eff_export,
                    dt_h=_dt_h,
                )
                _dp_score = regret_mod.score_regret(_dp_realized, optimal)
                dp_regret_eur = _dp_score["regret_eur"]
            except Exception:  # noqa: BLE001 — shadow DP failure must not block heuristic regret
                _LOGGER.debug(
                    "Shadow DP regret computation failed for %s", day, exc_info=True
                )

        # INFEASIBLE policy: upsert a marker row but leave metric fields NULL.
        if optimal.get("infeasible", False):
            recorder.upsert_daily_regret(
                day=day,
                regret_eur=None,
                over_buy_kwh=None,
                over_buy_eur=None,
                under_buy_kwh=None,
                cost_regret_eur=None,
                optimal_kwh=None,
                optimal_eur=None,
                realized_kwh=None,
                realized_eur=None,
                infeasible=1,
                computed_ts=computed_ts,
                dp_regret_eur=None,
            )
            updates["last_regret"] = {
                "day": day,
                "regret_eur": None,
                "over_buy_kwh": None,
                "under_buy_kwh": None,
            }
            _LOGGER.info("Daily regret for %s: infeasible day — null metrics", day)
            return updates

        realized = regret_mod.realized_grid_cost(
            day_data, realized_charge, cfg,
            realized_export_by_hour=_realized_export_kwh,
            export_price=eff_export,
            dt_h=_dt_h,
        )
        score = regret_mod.score_regret(realized, optimal)

        recorder.upsert_daily_regret(
            day=day,
            regret_eur=score["regret_eur"],
            over_buy_kwh=score["over_buy_kwh"],
            over_buy_eur=score["over_buy_eur"],
            under_buy_kwh=score["under_buy_kwh"],
            cost_regret_eur=score["cost_regret_eur"],
            optimal_kwh=optimal["kwh"],
            optimal_eur=optimal["eur"],
            realized_kwh=realized["kwh"],
            realized_eur=realized["eur"],
            infeasible=0,
            computed_ts=computed_ts,
            dp_regret_eur=dp_regret_eur,
        )
        updates["last_regret"] = {
            "day": day,
            "regret_eur": score["regret_eur"],
            "over_buy_kwh": score["over_buy_kwh"],
            "under_buy_kwh": score["under_buy_kwh"],
        }
        _LOGGER.info(
            "Daily regret for %s: regret_eur=%.4f over_buy_kwh=%.3f under_buy_kwh=%.3f"
            " dp_regret_eur=%s",
            day, score["regret_eur"], score["over_buy_kwh"], score["under_buy_kwh"],
            f"{dp_regret_eur:.4f}" if dp_regret_eur is not None else "n/a",
        )

        # Compute the 7-day rolling DP-vs-heuristic regret delta.
        # Negative means DP was cheaper over the window; None until ≥1 day.
        # This runs in the executor thread so the DB read is blocking-safe.
        try:
            _since_7d = (date.fromisoformat(day) - timedelta(days=6)).isoformat()
            _rows_7d = recorder.read_daily_regret_range(_since_7d)
            _valid = [
                (r["dp_regret_eur"], r["regret_eur"])
                for r in _rows_7d
                if r.get("dp_regret_eur") is not None
                and r.get("regret_eur") is not None
                and not r.get("infeasible", 0)
            ]
            if _valid:
                updates["last_dp_regret_7d"] = (
                    sum(dp - h for dp, h in _valid) / len(_valid)
                )
        except Exception:  # noqa: BLE001 — 7d delta failure must not block regret logging
            _LOGGER.debug("7d DP-vs-heuristic delta computation failed", exc_info=True)

    except Exception:
        _LOGGER.warning("Daily regret computation failed for %s", day, exc_info=True)

    return updates


def backfill_regret(
    recorder,
    cfg,
    today_str: str,
    computed_ts: str,
    *,
    slot_minutes: int,
) -> dict:
    """Score any regret days missed since the last scored entry (up to 7 days back).

    Called on the first tick after LOCAL midnight (or first tick ever after startup).
    Uses read_latest_daily_regret to find the last scored day, then scores each gap
    day in [from_day, yesterday] that is not already present in daily_regret.

    The upsert-by-day is idempotent, so concurrent or repeated calls are safe.
    Capped at 7 days back to bound compute time on a long outage.

    Parameters mirror ``run_daily_regret``. Internally calls
    ``run_daily_regret`` once per missing day, in the same order the
    original self-mutating loop did, and folds each call's ``updates``
    dict into the return value (later days overwrite earlier ones for a
    given key) — this reproduces the original "last write wins" semantics
    of repeated ``self.last_regret = ...`` / ``self.last_dp_regret_7d =
    ...`` assignments inside the loop.
    """
    updates: dict = {}
    try:
        today_date = date.fromisoformat(today_str)

        latest = recorder.read_latest_daily_regret()
        if latest is None:
            # No scored days yet: backfill up to 7 days.
            from_date = today_date - timedelta(days=7)
        else:
            # Start from the day after the last scored day.
            from_date = date.fromisoformat(latest["day"]) + timedelta(days=1)
            # Never look back more than 7 days regardless of the gap.
            min_date = today_date - timedelta(days=7)
            if from_date < min_date:
                from_date = min_date

        # Fetch already-scored rows in the window so we stay idempotent.
        from_day_str = from_date.isoformat()
        scored = recorder.read_daily_regret_range(from_day_str, today_str)
        scored_set = {r["day"] for r in scored}

        # Score each unscored day from from_date through yesterday.
        yesterday = today_date - timedelta(days=1)
        current = from_date
        while current <= yesterday:
            day_str = current.isoformat()
            if day_str not in scored_set:
                updates.update(
                    run_daily_regret(
                        recorder, cfg, day_str, computed_ts, slot_minutes=slot_minutes,
                    )
                )
            current += timedelta(days=1)
    except Exception:
        _LOGGER.warning("Regret backfill failed", exc_info=True)

    return updates
