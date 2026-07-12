"""Control loop: gather inputs, decide, actuate, record."""
from __future__ import annotations

import asyncio
import functools
import importlib.util
import json
import logging
import math
from datetime import date, datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from . import backtest as bt
from . import const, coordinator, energy, featureset, forecast as forecast_mod, guard, intra_hour, load_adapt, occupancy, optimize as optimize_mod, past_actuals as past_actuals_mod, plan as plan_mod, pricing_store, regret_job, remote_forecast, resolution, scheduler, soc_drift
from .remote_forecast import RemoteForecastPredictor, build_hours_payload, fetch_forecast
from .actuator import Actuator
from .efficiency import EfficiencyCurve
from .export_filter import apply_min_export_block
from .dataquality import clean_hourly_rows
from .forecast import build_intervals, LoadPredictor
from .hgbr import HGBRQuantileModel
from .ledger import CashLedger
from .loadmodel import BucketedLoadModel
from .models import Config, ControllerState, ExportState, ForecastInterval, PlanState, PlantInputs, PriceSlot
from .parsers import build_pv_curve_from_arrays, build_pv_curve_from_watts, build_two_day_pv_curve, synth_pv_curve
from .recorder import DataRecorder
# Pure planner core (Task C2): re-exported so existing call sites / test
# imports (`from .controller import compute_decision`, `controller._trough_by_hour`,
# etc.) keep working unchanged after the move to decision.py.
from .decision import (
    _DP_EPSILON_SCHEDULE_KWH,
    _apply_price_prior,
    _build_is_cheap_by_hour,
    _build_reserve_by_hour,
    _dp_select_slots,
    _dp_window,
    _next_synthetic_pickup,
    _synthetic_night_rows,
    _trough_by_hour,
    compute_decision,
)

_LOGGER = logging.getLogger(__name__)

# Sentinel: distinguishes "not passed" from "passed as None" in _record_sample.
_UNSET = object()

# Bounded wait for the tick lock during unload/reload so release()/recorder.close
# never interleave with an in-flight tick's engage_*; unblocks if a tick wedges.
_SHUTDOWN_LOCK_TIMEOUT_S = 15.0


def _persist_iso_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


# Table-driven persist/restore for the simple scalar/composite Controller fields
# (Task A9). Each entry is (store_key, attr_name, to_json, from_json, none_guard):
#   to_json(getattr(self, attr_name))  -> value written into the store payload
#   from_json(saved[store_key])        -> value assigned to self.<attr_name>
#   none_guard                         -> True if the original hand-written code
#                                          skipped this field on a stored ``None``
#                                          (``... and saved[key] is not None``)
#                                          instead of passing it to from_json
#
# _PERSIST_GROUPS mirrors the ORIGINAL restore()'s grouped try/except blocks
# exactly (see git show 560e918, Controller.restore): each inner list is one
# try/except group. restore() applies a group's fields sequentially inside a
# single try/except, so a genuine parse error on one field aborts the
# REMAINING fields in that group (fields already assigned earlier in the same
# group stay assigned — this is a "sequential assign-then-abort", not an
# atomic all-or-nothing commit). Fields in different groups are fully
# independent of one another. "plan"/"enabled" are NOT in this table — they
# have legacy back-compat fallback behavior (bare plan dict with no wrapper)
# and stay hand-written.
_PERSIST_GROUPS = [
    # export_state: isolated (its own try/except in the original).
    [
        ("export_state", "export_state", lambda v: v.to_dict(), lambda v: ExportState.from_dict(v), False),
    ],
    # E3: per-day export PnL accumulator — a mid-day HA restart must not zero
    # it. Grouped exactly as the original: a corrupt today_export_pnl_eur also
    # blocks export_pnl_day from restoring in the same pass.
    [
        ("today_export_pnl_eur", "today_export_pnl_eur", lambda v: v, float, False),
        ("export_pnl_day", "_export_pnl_day", lambda v: v, str, True),
    ],
    # Cash ledger: today's figures + the lifetime total must survive a
    # restart; grouped as one try/except in the original.
    [
        ("today_charge_cost_eur", "today_charge_cost_eur", lambda v: v, float, False),
        ("today_export_revenue_eur", "today_export_revenue_eur", lambda v: v, float, False),
        ("total_net_eur", "total_net_eur", lambda v: v, float, False),
    ],
    # SoC drift-hedge accumulator: a restart must resume from the same
    # closed-loop state rather than re-accumulating from scratch; all six
    # fields share one try/except in the original.
    [
        ("soc_drift_kwh", "_soc_drift_kwh", lambda v: v, float, False),
        ("soc_drift_day", "_soc_drift_day", lambda v: v, str, True),
        (
            "soc_drift_last_update", "_soc_drift_last_update",
            _persist_iso_or_none, dt_util.parse_datetime, True,
        ),
        ("soc_drift_last_soc_pct", "_soc_drift_last_soc_pct", lambda v: v, float, True),
        ("soc_drift_engaged", "_soc_drift_engaged", lambda v: v, bool, False),
        ("soc_drift_last_export_kwh_dc", "_soc_drift_last_export_kwh_dc", lambda v: v, float, False),
    ],
]

# Flat view used by _persist() (payload order/content is unaffected by grouping).
_PERSIST_FIELDS = [field for group in _PERSIST_GROUPS for field in group]


class Controller:
    def __init__(
        self,
        hass: HomeAssistant,
        data: dict,
        recorder: DataRecorder,
        actuator: Actuator,
        store,
        price_store=None,
    ) -> None:
        self._hass = hass
        self._data = data
        self._recorder = recorder
        self._actuator = actuator
        self._store = store
        self._price_store = price_store        # PriceHistoryStore | None (Plan B)
        # Re-entrancy guard (review 1.2): serializes tick() so a slow tick (e.g. an
        # ML retrain) cannot overlap the next timer fire and race the actuator.
        self._tick_lock = asyncio.Lock()
        self._price_history_day: str | None = None
        self.cfg = Config.from_dict(data)
        if self.cfg.soc_floor > const.FIRMWARE_SOC_FLOOR + 1e-9:
            _LOGGER.info(
                "soc_floor=%.1f%%: export margin only; passive drain modeled "
                "to firmware floor (%.0f%%)",
                self.cfg.soc_floor, const.FIRMWARE_SOC_FLOOR,
            )
        self.plan = PlanState.initial(dt_util.utcnow())
        self.enabled = True
        self.profile: dict = {}
        self._profile_predictor: LoadPredictor = LoadPredictor.from_profile(self.profile)
        self.last_status: dict = {}
        self._res_latch: tuple[int, "date"] | None = None
        self._detected_slot_minutes: int = 60
        # Last LATCHED slot_minutes used to build self.plan's committed state
        # (committed_slots / committed_charge_kwh).  Compared each live tick;
        # a change clears committed state so hour-keyed state from the old
        # resolution cannot mis-align with the new quarter-slot keys and
        # mis-fire the hysteresis/anti-fight guards.  Init 60 == the initial
        # resolution, so a stable-60 deployment never sees a mismatch (parity).
        self._committed_slot_minutes: int = 60
        self._last_purge_hour = -1
        self._last_rollup_hour = -1
        self._last_wal_checkpoint_hour = -1
        self._last_weather_hour = -1
        self._weather_forecast: list[dict] = []
        self._first_tick_after_start = True
        self._learned_model_warned = False
        self._last_remote_forecast_hour = -1
        self._remote_forecast_map: dict | None = None
        self._last_profile_refresh: datetime | None = None
        self.predictor = self._profile_predictor
        self.backtest_result: dict | None = None
        self.active_model_name: str = "profile"
        self._last_retrain: datetime | None = None
        self.last_decision: dict = {}
        self._last_regret_day: str | None = None
        self.last_regret: dict | None = None
        # 7-day rolling mean of (dp_regret_eur - heuristic_regret_eur).
        # Negative = DP was cheaper; set by _run_daily_regret_sync after each day.
        self.last_dp_regret_7d: float | None = None
        # Edge-trigger flag for the low-SoC infeasible WARNING (Acceptance §7).
        # True while the infeasible-at-floor condition is sustained; cleared when
        # the condition ends so the next episode logs again.
        self._infeasible_at_floor_warned: bool = False
        # C3 — export dwell/hysteresis state (persisted across ticks).
        # Initial state: disengaged, state_since = construction time.
        self.export_state: ExportState = ExportState.initial(dt_util.utcnow())
        # E3 — realized-arbitrage PnL ledger.
        # Accumulated export PnL for the current local day (euros).
        # Reset to 0.0 on local-day rollover; G2 reads this for the sensor attribute.
        # Cash ledger (spec 2026-07-10-battery-cash-ledger, Task C4): realized
        # battery €-flows — the cash-basis fields plus E3's economic PnL,
        # which share one day-stamp (see ledger.CashLedger.rollover). Exposed
        # on self via properties below so _PERSIST_GROUPS's getattr/setattr
        # keeps working unchanged.
        self._ledger = CashLedger()
        # C4: planned export revenue (€) from the current DP horizon — refreshed
        # every tick the DP runs.  Drives the card's arbitrage_pnl attribute so it
        # shows the plan, not just realized ticks (which stay 0.0 until export fires).
        self.planned_export_revenue_eur: float = 0.0
        # Past-actuals cache: per-clock-hour measured values for the display horizon.
        # Refreshed at most once per clock-hour (past hours never change).
        self._past_actuals_cache: dict | None = None
        self._past_actuals_hour: datetime | None = None
        # N2: last known COMPUTED house load (W) — fallback cache used whenever
        # pv/batt sensors are unavailable (skips the compute for that tick).
        # NOT persisted.  Recorder load_w / metered-net PnL / last_status all use
        # this cache-fallback value regardless of freshness.  The actuation
        # gross-setpoint compensation is the one consumer that must NOT act on a
        # stale cache hit — it gates on `self._house_load_fresh` instead (0.0
        # when not fresh; under-export is the safe direction there).
        self._last_house_load_w: float = 0.0
        # True only when the most recent _compute_house_load_w call did a live
        # compute (pv AND batt both available this tick); False when it fell back
        # to the cache above.  Set on every call; read by the export executor.
        self._house_load_fresh: bool = False
        # ── SoC drift-hedge accumulator state ─────────────────────────────────────────
        # Whole block gated on cfg.soc_hedge_fraction > 0 (default 0.0 = OFF / parity-safe).
        self._soc_drift_kwh: float = 0.0
        self._soc_drift_day: str | None = None
        # ── Layer A intraday residual corrector state (load_adapt.py) ─────────────────
        self._load_adapt_log = load_adapt.PredictionLog()
        self._load_adapt_ratio: float | None = None
        self._load_adapt_matched: int = 0
        # ── Layer B occupancy corrector state (occupancy.py) ─────────────────────────
        self._occ_table: occupancy.OccupancyTable | None = None
        self._persons_home_now: int | None = None
        # ── Current-hour kWh accumulator (intra_hour.py; blend gated by cfg.current_hour_blend) ──
        self._hour_acc = intra_hour.HourAccumulator()
        self._soc_drift_last_update: datetime | None = None
        self._soc_drift_last_soc_pct: float | None = None
        self._soc_drift_engaged: bool = False
        self._soc_drift_last_export_kwh_dc: float = 0.0
        # Previous tick's P50 intervals (for forecast_rate_at on the NEXT tick's accumulator step).
        # Intervals are built inside compute_decision, so we cache the result to use next tick.
        # Not persisted — rebuilt from DP output every successful tick.
        self._soc_drift_last_intervals: list | None = None
        # ── Measured efficiency curve (gated by cfg.use_measured_eta, default OFF) ────
        # Built from the static fallback until the first successful recorder read;
        # refreshed at most once per EFFICIENCY_CACHE_SECONDS (see _refresh_efficiency_curve).
        self._eta_curve: EfficiencyCurve = EfficiencyCurve.static(self.cfg)
        self._eta_curve_built_at: datetime | None = None

    # ── Cash ledger delegating properties (Task C4) ────────────────────────────
    # Preserve the pre-extraction attribute surface (direct get/set, and
    # _PERSIST_GROUPS's generic getattr/setattr) while the state itself now
    # lives on self._ledger (ledger.CashLedger).
    @property
    def today_export_pnl_eur(self) -> float:
        return self._ledger.today_export_pnl_eur

    @today_export_pnl_eur.setter
    def today_export_pnl_eur(self, value: float) -> None:
        self._ledger.today_export_pnl_eur = value

    @property
    def _export_pnl_day(self) -> str | None:
        return self._ledger.day

    @_export_pnl_day.setter
    def _export_pnl_day(self, value: str | None) -> None:
        self._ledger.day = value

    @property
    def today_charge_cost_eur(self) -> float:
        return self._ledger.today_charge_cost_eur

    @today_charge_cost_eur.setter
    def today_charge_cost_eur(self, value: float) -> None:
        self._ledger.today_charge_cost_eur = value

    @property
    def today_export_revenue_eur(self) -> float:
        return self._ledger.today_export_revenue_eur

    @today_export_revenue_eur.setter
    def today_export_revenue_eur(self, value: float) -> None:
        self._ledger.today_export_revenue_eur = value

    @property
    def total_net_eur(self) -> float:
        return self._ledger.total_net_eur

    @total_net_eur.setter
    def total_net_eur(self, value: float) -> None:
        self._ledger.total_net_eur = value

    async def _refresh_efficiency_curve(self, now: datetime) -> None:
        """Rebuild the measured efficiency curve from recent recorder samples.

        Skipped entirely when ``use_measured_eta`` is off (the default): the
        planner uses the static scalar curve via ``_planner_curve()`` returning
        None, so no read is needed. When on, cached for
        ``EFFICIENCY_CACHE_SECONDS`` and the SQLite read runs off-loop.
        """
        if not self.cfg.use_measured_eta:
            return
        if (
            self._eta_curve_built_at is not None
            and (now - self._eta_curve_built_at).total_seconds() < const.EFFICIENCY_CACHE_SECONDS
        ):
            return
        try:
            since = (now - timedelta(days=const.EFFICIENCY_WINDOW_DAYS)).isoformat()
            rows = await self._hass.async_add_executor_job(
                self._recorder.read_efficiency_samples, since
            )
            self._eta_curve = EfficiencyCurve.build(rows, self.cfg, now)
        except Exception:
            _LOGGER.warning("efficiency curve build failed; using static fallback", exc_info=True)
            self._eta_curve = EfficiencyCurve.static(self.cfg)
        self._eta_curve_built_at = now

    def _planner_curve(self) -> EfficiencyCurve | None:
        """The measured curve to pass to the DP planner/reserve, gated by cfg.

        ``None`` when ``use_measured_eta`` is off (default) — every downstream
        eta_curve consumer treats ``None`` as "use the static scalar eta",
        which is the byte-identical parity path.
        """
        return self._eta_curve if self.cfg.use_measured_eta else None

    def _eta_d_at(self, power_w: float) -> float:
        """Discharge efficiency at ``power_w``, gated by ``cfg.use_measured_eta``.

        Mirrors ``_planner_curve()``'s gate: the static scalar
        ``optimize.eta_discharge(cfg)`` when the flag is off (byte-identical
        parity), the measured curve's power-dependent value when on.
        """
        c = self._planner_curve()
        return optimize_mod.eta_discharge(self.cfg) if c is None else c.eta_discharge(power_w)

    async def _get_past_actuals(self, now) -> dict:
        """Measured actuals per past clock-hour for the display horizon.

        Built once per clock-hour from the recorder (completed past hours never
        change) and filtered to hours strictly before now_h so the forward
        projection is untouched. Returns {} on error (past slots stay empty).
        """
        now_h = resolution.hour_floor(now)
        if self._past_actuals_hour == now_h and self._past_actuals_cache is not None:
            return self._past_actuals_cache
        try:
            since_iso = (now - timedelta(hours=48)).isoformat()
            rows = await self._hass.async_add_executor_job(
                self._recorder.read_feature_rows, since_iso
            )
            actuals = past_actuals_mod.aggregate_past_actuals(rows)
            actuals = {h: v for h, v in actuals.items() if h < now_h}
            self._past_actuals_cache = actuals
            self._past_actuals_hour = now_h
            return actuals
        except Exception:
            _LOGGER.warning("past-actuals build failed; horizon past slots stay empty", exc_info=True)
            return {}

    def _update_load_adapt(self, now, cur_temp, past_actuals):
        """Update the base-prediction log + residual ratio; return the predictor
        for the LIVE plan (base tier unless a correction applies).

        Never raises; any failure returns the unwrapped base predictor.
        Shadow/fictive/disabled paths keep ``self.predictor`` — this wrapper is
        live-only (same pattern as estimated_tomorrow).
        """
        base = self.predictor
        # Layer B: occupancy-deviation wrapper (OFF at fraction 0.0; skipped on the
        # remote tier — the addon already conditions on per-hour projected occupancy).
        if (
            self.cfg.occ_adapt_fraction > 0.0
            and self._occ_table is not None
            and self.active_model_name != "remote"
        ):
            base = occupancy.OccupancyPredictor(
                base, self._occ_table, self._persons_home_now, now,
                self.cfg.occ_persistence_h, self.cfg.occ_adapt_fraction,
            )
        now_h = resolution.hour_floor(now)
        try:
            base_p50 = base.predict(
                now_h, cur_temp, const.DEFAULT_FALLBACK_LOAD_W, quantile=0.5,
            )
            self._load_adapt_log.record(now_h, base_p50)
        except Exception:  # noqa: BLE001 — never block the tick on logging
            pass
        try:
            _partial = None
            if (
                self.cfg.load_adapt_partial_hour
                and self._hour_acc.hour == now_h
                and self._hour_acc.covered_s > 0.0
            ):
                _rate_w = self._hour_acc.kwh * 3_600_000.0 / self._hour_acc.covered_s
                _partial = (_rate_w, self._hour_acc.covered_s / 3600.0)
            ratio, matched = load_adapt.compute_ratio(
                self._load_adapt_log, past_actuals or {}, now_h,
                self.cfg.load_adapt_window_h, partial=_partial,
            )
        except Exception:  # noqa: BLE001
            ratio, matched = None, 0
        self._load_adapt_ratio = ratio
        self._load_adapt_matched = matched
        pred = base
        if self.cfg.load_adapt_fraction > 0.0 and ratio is not None:
            pred = load_adapt.AdaptivePredictor(
                base, ratio, now, self.cfg.load_adapt_fade_h,
                self.cfg.load_adapt_fraction,
            )
        if self.cfg.current_hour_blend:
            pred = intra_hour.CurrentHourBlendPredictor(pred, self._hour_acc, now_h)
        return pred

    async def refresh_profile(self) -> None:
        """Read hourly energy rows from recorder and update the rolling load profile.

        Tier-3 (profile) fallback is now fed hourly-energy tuples — one per
        completed clock-hour, derived via ``featureset.hourly_load_w`` (kwh_sum
        x1000, coverage-rescaled by house_load_count, house_load_mean fallback)
        — instead of raw per-tick W samples. One tuple per hour removes the
        implicit count-weighting bias that per-tick sampling had (hours with
        more/fewer live ticks no longer over/under-influence the mean), and the
        profile's empirical quantiles become hourly-energy quantiles, consistent
        with the bucketed (Tier 2) model.

        Called on first tick and roughly hourly.  On error, keeps the existing
        profile and logs — never raises into tick().
        """
        try:
            now = dt_util.utcnow()
            since_iso = (now - timedelta(days=self.cfg.lookback_days)).isoformat()
            rows = await self._hass.async_add_executor_job(
                self._recorder.read_hourly_rows, since_iso
            )
            samples = [
                (str(r["hour_ts"]), load)
                for r in rows
                if (load := featureset.hourly_load_w(r)) is not None
            ]
            self.profile = forecast_mod.rolling_load_profile(
                samples, self.cfg.lookback_days, now
            )
            # Build a quantile-aware predictor from the raw samples so the profile tier
            # CAN return empirical quantiles above P50 if ever requested; live control
            # currently requests only P50 (see the P80-scaffolding note in _retrain_sync).
            self._profile_predictor = LoadPredictor.from_profile_samples(
                samples, self.cfg.lookback_days, now
            )
            self._occ_table = occupancy.build_table(rows)
            self._last_profile_refresh = now
        except Exception:
            _LOGGER.warning("refresh_profile failed; keeping existing profile", exc_info=True)

    async def _snapshot_prices_on_rollover(self, now, slots) -> None:
        """Plan B (B1): on local-day rollover, snapshot the just-finished day's
        realized prices.  Date-keyed write ⇒ restart-idempotent.  `slots` carries
        ~24 h of elapsed hours, so yesterday is fully present."""
        if self._price_store is None:
            return
        today = dt_util.as_local(now).date()
        if self._price_history_day == today.isoformat():
            return
        self._price_history_day = today.isoformat()
        yday = today - timedelta(days=1)
        await self._price_store.async_snapshot(
            yday.isoformat(), pricing_store.extract_realized_day(slots, yday)
        )

    def _retrain_sync(self, since_iso: str) -> None:
        """Synchronous body of retrain — safe to run in an executor thread.

        Four-tier fallback chain:

        0. **Remote** (Tier-0) — when ``addon_enabled`` is True and a non-empty
           forecast map has been fetched this clock-hour, use it and return
           immediately.  The existing HGBR/bucketed/profile chain is skipped
           entirely.
        1. **HGBR** — tried first when the coverage gate (``is_ready``) and
           quality gate (``should_promote``) both pass.  Falls through on any
           failure so the next tier always gets a chance.
        2. **Bucketed** — BucketedLoadModel, now trained on hourly energy
           rollups (``samples_hourly`` → ``clean_hourly_rows``) instead of
           per-tick W samples; gated on ``DEFAULT_MIN_TRAIN_HOURS``.
        3. **Profile** — rolling profile fallback when all else fails.
        """
        # ------------------------------------------------------------------
        # Tier 0: Remote ML add-on (when add-on is enabled + map available)
        # ------------------------------------------------------------------
        if self.cfg.addon_enabled and self._remote_forecast_map:
            self.predictor = RemoteForecastPredictor(self._remote_forecast_map)
            self.active_model_name = "remote"
            return

        hourly_rows = self._recorder.read_hourly_rows(since_iso=since_iso)
        clean_h = clean_hourly_rows(hourly_rows)
        if self.cfg.use_learned_model:
            # ------------------------------------------------------------------
            # Tier 1: HistGBR (coverage + quality gated)
            # ------------------------------------------------------------------
            try:
                hourly = self._recorder.read_hourly_rows()
                hgbr = HGBRQuantileModel()
                if hgbr.is_ready(hourly):
                    metrics = bt.walk_forward_hgbr(
                        hourly,
                        train_days=self.cfg.train_days,
                        test_days=self.cfg.backtest_test_days,
                        fallback_w=const.DEFAULT_FALLBACK_LOAD_W,
                    )
                    if bt.should_promote(metrics):
                        # Live control consumes only P50 (review: P80 scaffolding);
                        # fitting the second quantile doubled retrain cost for no reader.
                        hgbr.fit(hourly, quantiles=(0.5,))
                        if hgbr._fitted:
                            self.predictor = LoadPredictor.from_model(hgbr)
                            self.active_model_name = "hgbr"
                            self.backtest_result = metrics
                            return
            except Exception:  # noqa: BLE001 — bad HGBR path must not crash
                _LOGGER.warning(
                    "HGBR retrain path failed; falling through to bucketed",
                    exc_info=True,
                )
            # ------------------------------------------------------------------
            # Tier 2: BucketedLoadModel — trained on hourly energy rollups
            # (samples_hourly), one FeatureRow per hour; gated on
            # DEFAULT_MIN_TRAIN_HOURS rather than the old per-tick sample count.
            # ------------------------------------------------------------------
            if len(clean_h) >= const.DEFAULT_MIN_TRAIN_HOURS:
                model = BucketedLoadModel.fit(clean_h)
                self.backtest_result = bt.walk_forward(
                    clean_h,
                    train_days=self.cfg.train_days,
                    test_days=self.cfg.backtest_test_days,
                    fallback_w=const.DEFAULT_FALLBACK_LOAD_W,
                )
                self.predictor = LoadPredictor.from_model(model)
                self.active_model_name = "bucketed"
                return
        # ------------------------------------------------------------------
        # Tier 3: rolling profile fallback (unchanged behaviour)
        # ------------------------------------------------------------------
        self.predictor = self._profile_predictor
        self.active_model_name = "profile"

    async def retrain(self, now: datetime | None = None) -> None:
        """Fit or refresh the load predictor from recorded feature rows.

        Runs a walk-forward backtest and upgrades to the learned model when
        ``use_learned_model`` is enabled and enough samples are available.
        Never raises — any error keeps the previous predictor.
        """
        try:
            if now is None:
                now = dt_util.utcnow()
            window_days = self.cfg.train_days + self.cfg.backtest_test_days * 2
            since_iso = (now - timedelta(days=window_days)).isoformat()
            if self._hass is not None:
                await self._hass.async_add_executor_job(self._retrain_sync, since_iso)
            else:
                self._retrain_sync(since_iso)
        except Exception:  # noqa: BLE001 - never break the loop on training error
            pass

    async def _safe_release(
        self,
        now: datetime,
        context: str = "",
        *,
        release: bool = True,
        reset_export: bool = True,
        reset_before_release: bool = False,
    ) -> None:
        """Best-effort inverter release + export dwell-state reset.

        Consolidates the try/``release_to_self()``/log-error +
        ``ExportState(engaged=False, state_since=now)`` reset pattern repeated
        across the tick/failsafe/export-executor paths. ``context`` becomes the
        error-log message on a release failure, so each call site keeps its
        original diagnostic text.

        ``release``/``reset_export`` let a call site express a release-only or
        reset-only variant (some sites reset export state without ever having
        engaged the actuator; others release without touching export state
        directly — e.g. a local ``_new_export_state`` var is unified into
        ``self.export_state`` later by the caller). The export-state reset is
        itself gated on ``self.export_state.engaged`` (mirrors every original
        call site) so a no-op reset never bumps ``state_since``.

        ``reset_before_release`` mirrors the one call site (export-disabled
        path) whose original code reset ``self.export_state`` BEFORE
        attempting the release rather than after — order matters there since
        ``release_to_self()`` awaits, and a concurrent reader (e.g. a sensor)
        could observe ``export_state`` mid-release.
        """
        if reset_export and reset_before_release and self.export_state.engaged:
            self.export_state = ExportState(engaged=False, state_since=now)
        if release:
            try:
                await self._actuator.release_to_self()
            except Exception:  # noqa: BLE001 — best-effort release must never raise
                _LOGGER.error(context, exc_info=True)
        if reset_export and not reset_before_release and self.export_state.engaged:
            self.export_state = ExportState(engaged=False, state_since=now)

    async def tick(self) -> dict:
        # Re-entrancy guard (review 1.2): the 60s timer fires regardless of the
        # previous tick; a slow retrain tick must not overlap and race the actuator.
        if self._tick_lock.locked():
            _LOGGER.warning("tick overlap: previous tick still running; skipping")
            return self.last_status
        async with self._tick_lock:
            try:
                return await self._tick_impl()
            except Exception:  # noqa: BLE001 — whole-tick failsafe (review 1.1)
                _LOGGER.exception("tick failed; releasing to self-consumption")
                now = dt_util.utcnow()
                await self._safe_release(now, "release_to_self failed in tick failsafe")
                self.plan = PlanState(ControllerState.PASSIVE, now, ())
                status = self._status(now, 0.0, None, "failsafe")
                status["state"] = "failsafe"
                return status

    def _apply_drift_hedge(self, now, inputs, slots, sunset) -> dict | None:
        # ── SoC drift-hedge (LIVE/enabled path; whole block gated OFF unless fraction>0) ──
        # Default None = byte-identical to pre-hedge (parity preserved at soc_hedge_fraction=0.0).
        hedge_drain_by_hour: dict[datetime, float] | None = None
        if self.cfg.soc_hedge_fraction > 0.0:
            _today_key = dt_util.as_local(now).date().isoformat()
            _prev_day = self._soc_drift_day
            self._soc_drift_kwh, self._soc_drift_day = soc_drift.reset_if_new_day(
                self._soc_drift_kwh, self._soc_drift_day, _today_key,
            )
            _new_day = self._soc_drift_day != _prev_day
            # "Real rollover" = day changed AND we had a previous day (not first-ever tick).
            # On first-ever start _prev_day is None; no step to gate, anchor should be written.
            _real_rollover = _new_day and _prev_day is not None
            if _real_rollover:
                # No step spans the day reset — clear the SoC anchor.
                self._soc_drift_last_soc_pct = None
            _dt_h = (
                (now - self._soc_drift_last_update).total_seconds() / 3600.0
                if self._soc_drift_last_update is not None else 0.0
            )
            _soc_now = inputs.soc
            _gated = (
                not (0.0 < _dt_h <= soc_drift.MAX_DRIFT_STEP_H)
                or self._soc_drift_last_soc_pct is None
                or self._soc_drift_last_intervals is None
                or _soc_now >= self.cfg.soc_target - 1.0
                or _soc_now <= self.cfg.soc_floor + 1.0
            )
            if not _gated:
                # _gated guards _soc_drift_last_soc_pct is None and _soc_drift_last_intervals is None;
                # assert for Pyright narrowing (runtime: impossible to fail here).
                assert self._soc_drift_last_soc_pct is not None
                assert self._soc_drift_last_intervals is not None
                # Use the P50 intervals cached from the PREVIOUS tick's DP run.
                # Intervals change at most hourly; stale-by-one-tick is functionally identical.
                _fc_pv_w, _fc_load_w = soc_drift.forecast_rate_at(
                    self._soc_drift_last_intervals, now
                )
                # Curve-derived discharge eta at the forecast deficit power (only used
                # by expected_soc_delta_kwh on the deficit branch); static scalar when
                # the flag is off (_eta_d_at's own gate) — byte-identical parity.
                _eta_d = self._eta_d_at(max(0.0, _fc_load_w - _fc_pv_w))
                _expected_dc = soc_drift.expected_soc_delta_kwh(
                    _fc_pv_w, _fc_load_w, _dt_h, self.cfg.eta_charge, _eta_d,
                    idle_drain_w=self.cfg.idle_drain_w,
                )
                _measured_dc = soc_drift.measured_soc_delta_kwh(
                    _soc_now, self._soc_drift_last_soc_pct, self.cfg.capacity_kwh,
                )
                _tick_h = const.TICK_SECONDS / 3600.0
                # Duration-scale the export add-back: _last_export_kwh_dc is sized over
                # TICK_SECONDS but this step integrates _dt_h (may differ on missed ticks).
                _export_dc_step = (
                    self._soc_drift_last_export_kwh_dc * _dt_h / _tick_h
                    if _tick_h > 0 else 0.0
                )
                self._soc_drift_kwh = soc_drift.accumulate(
                    self._soc_drift_kwh,
                    soc_drift.per_step_drift_kwh(_expected_dc, _measured_dc, _export_dc_step),
                    dt_h=_dt_h, halflife_h=self.cfg.soc_drift_decay_halflife_h,
                )
                self._soc_drift_kwh = soc_drift.cap_accumulator(
                    self._soc_drift_kwh, self.cfg.capacity_kwh,
                )
            # Consume the export field; C3 re-sets it if THIS tick fires an export.
            self._soc_drift_last_export_kwh_dc = 0.0
            # On a REAL rollover (prev day known → today) leave anchor None so the very
            # next step cannot span midnight.  On fresh start or normal ticks, record now.
            if not _real_rollover:
                self._soc_drift_last_soc_pct = _soc_now
            self._soc_drift_last_update = now
            # State is flushed by the single end-of-tick _persist() call (line ~1738).
            _drift, self._soc_drift_engaged = soc_drift.drift_kwh(
                self._soc_drift_kwh, self.cfg.soc_drift_deadband_kwh,
                0.5 * self.cfg.soc_drift_deadband_kwh, self._soc_drift_engaged,
            )
            _hedge = self.cfg.soc_hedge_fraction * _drift
            if _hedge > 0.0:
                # Front-load the debit to the cheapest forward clock-hour (the trough)
                # so any over-buy lands at the cheapest tariff.
                _now_h = resolution.hour_floor(now)
                _hedge_deadline = scheduler.compute_deadline(now, sunset, slots, self.cfg)
                _fwd = [
                    s for s in slots
                    if resolution.hour_floor(s.start) >= _now_h
                    and s.start <= _hedge_deadline
                ]
                _trough_h = (
                    resolution.hour_floor(min(_fwd, key=lambda s: s.price).start)
                    if _fwd else _now_h
                )
                hedge_drain_by_hour = {_trough_h: _hedge}
        return hedge_drain_by_hour

    async def _tick_impl(self) -> dict:
        now = dt_util.utcnow()
        _first_tick = self._first_tick_after_start
        self._first_tick_after_start = False
        # Refresh the measured efficiency curve off-loop (cached; cheap no-op most
        # ticks). Skipped entirely when use_measured_eta is off (default) — the
        # planner uses the static scalar; the curve rebuilds on the first tick after
        # the flag is flipped on.
        await self._refresh_efficiency_curve(now)
        # Hour-gate: the hourly forecast changes at most hourly, and an unbounded
        # await here (a hung weather integration) would otherwise wedge every 60 s
        # tick with the inverter parked. Fetch once per clock-hour; keep the last
        # good forecast if a refresh returns [] (transient failure).
        if now.hour != self._last_weather_hour:
            self._last_weather_hour = now.hour
            _fetched = await coordinator.read_hourly_weather_forecast(self._hass, self._data)
            if _fetched:
                self._weather_forecast = _fetched
        _wf_list = self._weather_forecast
        _now_hour = resolution.hour_floor(now)
        _weather_entry = coordinator.get_forecast_for_hour(_wf_list, _now_hour)
        # Home-presence count for this tick (on-loop state reads).
        _persons_home_now = coordinator.count_persons_home(self._hass, self._data)
        self._persons_home_now = _persons_home_now
        # Per-hour temperature map derived from the hourly weather forecast.
        # Keys are hour-start UTC datetimes; values are temp_forecast (float | None).
        # Passed to compute_decision so each forecast interval uses its own hourly
        # temperature rather than the flat current-temperature scalar.
        _temp_by_hour: dict[datetime, float | None] = {
            resolution.hour_floor(e["datetime"]): e.get("temp_forecast")
            for e in (_wf_list or [])
            if e.get("datetime") is not None
        }

        # Hourly rollup: aggregate completed clock-hours into samples_hourly once per
        # clock-hour, regardless of enabled/disabled state.  Guard against a missing
        # recorder (partial initialisation or tests without one — the backfill sync path
        # uses try/except, but here we guard explicitly to keep the tick clean).
        if self._recorder is not None and now.hour != self._last_rollup_hour:
            self._last_rollup_hour = now.hour
            _hourly_cutoff = (
                now - timedelta(days=self.cfg.retention_hourly_days)
            ).isoformat()
            await self._hass.async_add_executor_job(
                self._rollup_hourly_sync, now.isoformat(), _hourly_cutoff
            )

        # H3a: periodic WAL checkpoint so a read-only immutable reader (the addon,
        # mounted config:ro) sees recent rows. Once per clock-hour, off-loop.
        if self._recorder is not None and now.hour != self._last_wal_checkpoint_hour:
            self._last_wal_checkpoint_hour = now.hour
            await self._hass.async_add_executor_job(self._recorder.wal_checkpoint)

        # A4: DEFAULT_USE_LEARNED_MODEL is True, but sklearn is NOT an integration
        # requirement (musl is why the addon exists) and the addon defaults off — a
        # stock install then silently falls back to the bucketed model forever.
        if (
            not self._learned_model_warned
            and self.cfg.use_learned_model
            and not self.cfg.addon_enabled
            and importlib.util.find_spec("sklearn") is None
        ):
            self._learned_model_warned = True
            _LOGGER.warning(
                "use_learned_model is on but scikit-learn is unavailable in the "
                "integration and the forecast add-on is disabled — falling back to "
                "the bucketed load model. Enable the Anker X1 Forecast add-on to "
                "use the learned model."
            )

        # Remote forecast fetch: once per clock-hour when the add-on is enabled.
        # Uses the weather forecast already fetched above as the feature payload.
        # A fetch failure (network error, add-on dormant, non-200, bad JSON) silently
        # returns None — the map is then left unchanged so the next successful fetch
        # will update it.  This never raises; any exception is swallowed here as a
        # final backstop even though fetch_forecast already guarantees non-raising.
        if self.cfg.addon_enabled and now.hour != self._last_remote_forecast_hour:
            self._last_remote_forecast_hour = now.hour
            try:
                _persons_by_ts = None
                if self._recorder is not None:
                    _ph_since = (
                        now - timedelta(days=remote_forecast.PERSONS_HOW_LOOKBACK_DAYS)
                    ).isoformat()
                    _ph_samples = await self._hass.async_add_executor_job(
                        self._recorder.read_persons_home_samples, _ph_since
                    )
                    _ph_means = remote_forecast.persons_home_hour_of_week_means(_ph_samples)
                    _ph_hour_starts = [
                        e["datetime"] for e in (_wf_list or []) if e.get("datetime") is not None
                    ]
                    _persons_by_ts = remote_forecast.project_persons_home(
                        now, _persons_home_now, _ph_means, _ph_hour_starts,
                        persistence_hours=self.cfg.occ_persistence_h,
                    )
                _payload = build_hours_payload(_wf_list, _persons_by_ts)
                _fetched_map = await fetch_forecast(
                    async_get_clientsession(self._hass),
                    self.cfg.addon_url,
                    self.cfg.addon_timeout,
                    _payload,
                )
                if _fetched_map is not None:
                    self._remote_forecast_map = _fetched_map
            except Exception:  # noqa: BLE001 — belt-and-suspenders; fetch_forecast never raises
                _LOGGER.debug("remote_forecast fetch raised unexpectedly", exc_info=True)

        if not self.enabled:
            # Hand control back to the X1 ONCE if we were actively engaged, then stay
            # hands-off — re-asserting self-consumption every tick would clobber a
            # user-set manual/modbus mode while disabled.
            # Derive "was engaged" from PERSISTED state on the first tick after a
            # (re)start — actuator.engaged is in-memory only and resets to False on
            # restart, so a crash while exporting/FORCING would otherwise leave the
            # inverter executing its last VPP command forever. Fire ONE release; on
            # every later disabled tick fall back to the live actuator flag so we do
            # not clobber a user-set manual/modbus mode.
            _was_engaged = self._actuator.engaged or (
                _first_tick
                and (self.plan.state is ControllerState.FORCING or self.export_state.engaged)
            )
            # Reset export dwell state so a later re-enable starts clean (mirror FORCING/C3).
            await self._safe_release(
                now, "Actuator release_to_self failed (disabled path)", release=_was_engaged,
            )
            # Save the previous plan for state-machine continuity in the shadow compute,
            # then reset to PASSIVE so no committed slots are carried forward.
            _prev_plan = self.plan
            self.plan = PlanState(ControllerState.PASSIVE, now, ())
            # Persist the disengaged/PASSIVE state: the disabled branch otherwise
            # never writes the store, so a mid-disable restart would re-derive
            # "was engaged" from stale persisted export_state and re-release,
            # clobbering a user-set manual mode. Guarded on _first_tick so we do it
            # once per (re)start, not every disabled tick.
            if _first_tick:
                await self._persist()
            inputs = coordinator.read_plant_inputs(self._hass, self._data)

            # Read all forecast/schedule data needed for shadow compute and display horizon.
            slots = coordinator.read_price_slots(self._hass, self._data)
            _slot_minutes = self._resolve_slot_minutes(slots)
            pv_remaining = coordinator.read_pv_remaining_kwh(self._hass, self._data)
            tomorrow_total = coordinator.read_pv_tomorrow_kwh(self._hass, self._data)
            sun_times = coordinator.read_sun_times(self._hass, self._data)
            today_arrays = coordinator.read_pv_today_arrays(self._hass, self._data)
            tomorrow_arrays = coordinator.read_pv_tomorrow_arrays(self._hass, self._data)
            today_watts, tomorrow_watts = self._read_forecast_bundle()
            sunset = coordinator.read_sunset(self._hass, self._data)
            _temp_ent = self._data.get(const.CONF_ENT_TEMP)
            cur_temp = (
                coordinator.read_attr(self._hass, _temp_ent, "temperature")
                if _temp_ent is not None
                else None
            )

            # Read live feed-in tariff (same logic as the enabled path below).
            _shadow_export_price, _shadow_export_matches_import = self._resolve_export_price()

            # Shadow compute: run the real decision logic but NEVER actuate.
            # Use _prev_plan so the dwell / state-machine history is preserved.
            # _shadow_dp_out receives DP artefacts (dp_selected, intervals) when
            # the DP succeeds in shadow mode — used to publish fictive_plan below.
            shadow_deadline: datetime | None = None
            shadow_plan = self.plan
            _shadow_hm = "single-day"
            _shadow_dp_out: dict = {}
            if inputs is not None and slots and sunset is not None and pv_remaining is not None:
                try:
                    shadow_plan, _, shadow_deadline, _, _shadow_hm, _ = await self._hass.async_add_executor_job(
                        functools.partial(
                            compute_decision,
                            _prev_plan, inputs, slots, pv_remaining, sunset,
                            self.predictor, cur_temp, self.cfg,
                            tomorrow_total, sun_times, today_arrays, tomorrow_arrays,
                            today_watts=today_watts,
                            tomorrow_watts=tomorrow_watts,
                            export_price=_shadow_export_price,
                            _out=_shadow_dp_out,
                            _shadow_dp=True,
                            export_price_matches_import=_shadow_export_matches_import,
                            temp_by_hour=_temp_by_hour,
                            slot_minutes=_slot_minutes,
                            eta_curve=self._planner_curve(),
                        )
                    )
                except Exception:
                    _LOGGER.warning("Shadow compute_decision failed (disabled path)", exc_info=True)

            if inputs is not None:
                await self._record_sample(
                    now, inputs, setpoint=0.0, state="disabled",
                    weather_entry=_weather_entry, persons_home=_persons_home_now,
                )

            # Keep the predictor warming while disabled so the SoC/load curve
            # sharpens as collected data accumulates (same cadence as enabled).
            if (
                self._last_profile_refresh is None
                or (now - self._last_profile_refresh) >= timedelta(hours=1)
            ):
                await self.refresh_profile()
            if self._last_retrain is None or (now - self._last_retrain) >= timedelta(
                hours=self.cfg.retrain_hours
            ):
                await self.retrain(now)
                self._last_retrain = now

            # Stash decision snapshot for persistence by the recorder writer (A3).
            if inputs is not None:
                _price_window = [
                    (s.start.isoformat(), s.price)
                    for s in slots
                    if shadow_deadline is not None and now <= s.start < shadow_deadline
                ]
                self.last_decision = self._build_decision_snapshot(
                    now=now,
                    active=False,
                    soc=inputs.soc,
                    deadline=shadow_deadline,
                    committed_slots=shadow_plan.committed_slots,
                    pv_remaining=pv_remaining,
                    tomorrow_total=tomorrow_total,
                    price_window=_price_window,
                    setpoint=0.0,
                    state="disabled",
                    horizon_mode=_shadow_hm,
                )

            # A3b: persist decision snapshot to decisions table.
            await self._persist_decision_snapshot()

            # A3b: daily regret job — run on first tick after LOCAL midnight, and
            # also on first tick after restart (_last_regret_day is None).
            # _backfill_regret_sync handles both: scores yesterday + any missed days.
            await self._backfill_regret(now)

            # Shadow decision is recorded to samples + decision log for learning,
            # but live sensors stay 0 while disabled.
            # Build status AFTER the daily regret job so regret keys are fresh.
            status = self._status(now, 0.0, None, "disabled")

            # Publish a self-consumption display horizon (no grid charging) so the
            # card still renders PV + load + projected SoC while disabled.
            if inputs is not None and slots and pv_remaining is not None and sun_times is not None:
                horizon = plan_mod.build_display_horizon(
                    slots, now, today_arrays, tomorrow_arrays, sun_times,
                    self.predictor, cur_temp, const.DEFAULT_FALLBACK_LOAD_W,
                    inputs.soc, [], now, self.cfg,
                    today_watts=today_watts,
                    tomorrow_watts=tomorrow_watts,
                    temp_by_hour=_temp_by_hour,
                    eta_curve=self._planner_curve(),
                )
                if horizon:
                    self.last_status["plan"] = {
                        "horizon": horizon,
                        "deadline": now.isoformat(),
                        "planned_grid_hours": 0,
                    }

            # Publish the DP's proposed horizon as a fictive plan so the
            # dashboard shows DP intentions during the shadow period (T0.5c).
            # The DP ran purely for observation — no setpoint was ever issued.
            # Mirrors the enabled-path publication (T0.6a) with identical schema.
            if (
                _shadow_dp_out.get("dp_selected") is not None
                and shadow_deadline is not None
                and inputs is not None
            ):
                _fictive_h = plan_mod.build_plan_horizon(
                    slots,
                    _shadow_dp_out["intervals"],
                    _shadow_dp_out["dp_selected"],
                    inputs.soc,
                    shadow_deadline,
                    self.cfg,
                    grid_request_by_hour=_shadow_dp_out.get("grid_request"),
                    eta_curve=self._planner_curve(),
                )
                self.last_status["fictive_plan"] = {
                    "horizon": _fictive_h,
                    "deadline": shadow_deadline.isoformat(),
                    "planned_grid_hours": sum(1 for e in _fictive_h if e["mode"] == "grid"),
                }
            else:
                # DP did not run or failed — remove any stale fictive_plan key.
                self.last_status.pop("fictive_plan", None)

            return status

        inputs = coordinator.read_plant_inputs(self._hass, self._data)
        slots = coordinator.read_price_slots(self._hass, self._data)
        sunset = coordinator.read_sunset(self._hass, self._data)
        pv_remaining = coordinator.read_pv_remaining_kwh(self._hass, self._data)
        tomorrow_total = coordinator.read_pv_tomorrow_kwh(self._hass, self._data)
        sun_times = coordinator.read_sun_times(self._hass, self._data)

        # FIX M4 — treat all-PV-unavailable as failsafe (pv_remaining is None).
        if inputs is None or not slots or sunset is None or pv_remaining is None:
            await self._safe_release(now, "Actuator release_to_self failed (failsafe path)")
            self.plan = PlanState(ControllerState.PASSIVE, now, ())
            return self._status(now, 0.0, None, "failsafe")

        _slot_minutes = self._resolve_slot_minutes(slots)

        # Committed-state clear on LATCHED resolution change (live/persisted path
        # only — the disabled/shadow branch above already resets self.plan to
        # PASSIVE/() every tick and never persists committed state).  Hour-keyed
        # committed_slots/committed_charge_kwh from the old resolution cannot be
        # allowed to mis-align with quarter-slot keys under the new resolution
        # (would mis-fire the hysteresis/anti-fight guards).  Modeled on the
        # existing new-day-style resets (e.g. PlanState(..., now, ()) above).
        # At a stable resolution (always 60 today) _slot_minutes never differs
        # from the init value, so this never fires — parity-safe.
        if _slot_minutes != self._committed_slot_minutes:
            self.plan = PlanState(self.plan.state, self.plan.state_since, ())
            self.plan.committed_charge_kwh = 0.0
            self.plan.committed_charge_slot = None
            self._committed_slot_minutes = _slot_minutes

        await self._snapshot_prices_on_rollover(now, slots)

        # FIX C1 — refresh load profile on first tick and roughly hourly.
        _refresh_needed = (
            self._last_profile_refresh is None
            or (now - self._last_profile_refresh) >= timedelta(hours=1)
        )
        if _refresh_needed:
            await self.refresh_profile()

        # periodic retrain
        if self._last_retrain is None or (now - self._last_retrain) >= timedelta(hours=self.cfg.retrain_hours):
            await self.retrain(now)
            self._last_retrain = now

        # Read temp the same way the recorder does (attribute, not state text).
        _temp_ent = self._data.get(const.CONF_ENT_TEMP)
        cur_temp = (
            coordinator.read_attr(self._hass, _temp_ent, "temperature")
            if _temp_ent is not None
            else None
        )

        today_arrays = coordinator.read_pv_today_arrays(self._hass, self._data)
        tomorrow_arrays = coordinator.read_pv_tomorrow_arrays(self._hass, self._data)
        today_watts, tomorrow_watts = self._read_forecast_bundle()

        # Read live feed-in tariff for the export-credit term in the DP optimizer.
        # Empty ent_export_price → None → export credit disabled (default behaviour).
        _export_price, _export_matches_import = self._resolve_export_price()

        # _dp_out receives DP artefacts when the DP succeeds:
        # {"dp_selected": [...], "intervals": [...]}.  Left empty on DP failure
        # — used below to publish / clear fictive_plan.
        _dp_out: dict = {}
        # Plan B: compute the blended tomorrow price estimate for the reserve prior.
        # Passed ONLY to the live compute_decision call; shadow + recompute keep None
        # so parity/telemetry paths are byte-identical.
        _estimated_tomorrow = None
        if self._price_store is not None:
            _tom = (dt_util.as_local(now) + timedelta(days=1)).date()
            _estimated_tomorrow = pricing_store.blend_price_prior(
                self._price_store.history, _tom,
                weight_today=self.cfg.price_blend_weight_today,
            )
        past_actuals = await self._get_past_actuals(now)

        # Layer A: residual-corrected predictor for the LIVE plan only (shadow,
        # fictive and disabled paths keep the base tier — parity preserved).
        _plan_predictor = self._update_load_adapt(now, cur_temp, past_actuals)

        # ── SoC drift-hedge (LIVE/enabled path; whole block gated OFF unless fraction>0) ──
        # Default None = byte-identical to pre-hedge (parity preserved at soc_hedge_fraction=0.0).
        hedge_drain_by_hour = self._apply_drift_hedge(now, inputs, slots, sunset)

        new_plan, _, deadline, horizon, _horizon_mode_e, _ivs_reserve = await self._hass.async_add_executor_job(
            functools.partial(
                compute_decision,
                self.plan, inputs, slots, pv_remaining, sunset,
                _plan_predictor, cur_temp, self.cfg,
                tomorrow_total, sun_times, today_arrays, tomorrow_arrays,
                today_watts=today_watts,
                tomorrow_watts=tomorrow_watts,
                export_price=_export_price,
                _out=_dp_out,
                export_price_matches_import=_export_matches_import,
                estimated_tomorrow=_estimated_tomorrow,
                temp_by_hour=_temp_by_hour,
                past_actuals_by_hour=past_actuals,
                hedge_drain_by_hour=hedge_drain_by_hour,
                slot_minutes=_slot_minutes,
                eta_curve=self._planner_curve(),
            )
        )
        # Cache P50 intervals for next tick's drift accumulator step (only when hedging
        # is enabled; off→on toggle leaves it None so the H1 gate fires on the first
        # hedging tick, then sets the cache — correct first-step behaviour).
        if self.cfg.soc_hedge_fraction > 0.0:
            self._soc_drift_last_intervals = _dp_out.get("intervals")
        # C4: capture the DP's planned export revenue so the card always reflects
        # the plan (not just realized ticks which stay 0.0 until export fires).
        self.planned_export_revenue_eur = float(_dp_out.get("export_revenue_eur", 0.0))

        # Edge-triggered WARNING: fire ONCE when the battery first drains to the
        # firmware floor; re-arm when SoC recovers above the floor so the next
        # episode (e.g., the following night) logs again.
        # Only on the live/enabled path — the shadow/disabled path never reaches here.
        _at_floor = inputs.soc <= self.cfg.soc_floor + 1.0
        if _at_floor and not self._infeasible_at_floor_warned:
            self._infeasible_at_floor_warned = True
            _LOGGER.warning(
                "Battery drained to firmware floor (soc=%.1f%% <= floor %.1f%%): "
                "short on carryover charge from yesterday. Holding the reserve; "
                "will recharge at the next price-worthy slot "
                "(economic-only, no force-charge).",
                inputs.soc,
                self.cfg.soc_floor,
            )
        elif not _at_floor and self._infeasible_at_floor_warned:
            # Re-arm: SoC has recovered above floor, so next drain episode logs again.
            self._infeasible_at_floor_warned = False

        # ── C3 export executor variables (populated below when export fires) ──
        _export_setpoint_w: float | None = None
        _export_kwh: float | None = None
        _reserve_kwh_val: float | None = None
        _surplus_kwh_val: float | None = None

        # Live house load for export compensation (computed once; reused by C3 and
        # _record_sample to avoid actuation/log divergence across awaits).
        # house_load_w = pv + meter_w (signed net grid, + = import) + batt
        # (+ = discharge, − = charge) − inverter_loss, clamped to ≥ 0.  pv/batt
        # unavailable → skip the compute and fall back to the cached last-known
        # value (N2); self._house_load_fresh is set False in that case so the
        # export gross-setpoint compensation below knows not to act on it.
        # NB: distinct name from the module-level `house_load_w as _house_load_w`
        # import (a function) to avoid shadowing it within tick().
        _house_load_now_w = self._compute_house_load_w(inputs)
        self._hour_acc.add(now, _house_load_now_w)

        # E3 + cash ledger: reset ALL per-day € accumulators on local-day
        # rollover, BEFORE accumulating this tick.
        self._rollover_daily_ledgers(now)

        # Cash ledger: realized battery €-flows, accumulated on every enabled
        # tick past the failsafe guard — independent of the executor branches
        # below, so it also catches exports the executor never commanded
        # (the 2026-07-06 invisible decisive-drain class).  Disabled-path and
        # failsafe ticks return before this point: accepted spec limitation.
        self._accumulate_cash_ledger(now, inputs, slots, _slot_minutes, _export_price)

        _engage_failed = False
        if new_plan.state is ControllerState.FORCING:
            setpoint = guard.command_setpoint(
                self.cfg.max_charge_w, self._actuator.last_setpoint_w, self.cfg,
            )
            try:
                await self._actuator.engage_and_charge(setpoint)
            except Exception:
                # Publish truth for THIS tick without a hardware release or plan
                # reset: the inverter never engaged, so setpoint 0 + PASSIVE is
                # honest; self.plan stays FORCING so the next tick retries.
                _LOGGER.error("Actuator engage_and_charge failed (FORCING path); publishing passive/0", exc_info=True)
                _engage_failed = True
                setpoint = 0.0
            # Mutual exclusion: export executor is skipped entirely while force-charging.
            # Release export state so we transition cleanly after force-charge ends.
            await self._safe_release(now, release=False)
        else:
            setpoint = 0.0
            if self.plan.state is ControllerState.FORCING:
                await self._safe_release(
                    now, "Actuator release_to_self failed (FORCING→PASSIVE transition)",
                    reset_export=False,
                )

            # ── C3: live export executor ──────────────────────────────────────
            # Only fires when export is enabled and an export price is available.
            # A1 = NET-EXPORT: setpoint is export_rate directly (inverter serves
            # house load first, exports the remainder); no house_load_now term.
            if self.cfg.enable_export and _export_price is not None and _export_price > 0.0:
                # Compute ride-out reserve and battery surplus above it.
                # _ivs_reserve is the TWO-DAY reserve interval list from compute_decision
                # (6th return element).  Trough-anchored: ride_out_reserve_kwh walks
                # forward to the deepest signed-trajectory point, matching the DP floor.
                # rev-2: under the trough anchor, hour-align now + thread the SAME
                # cheap-relief map as the plan so the live export floor matches the
                # planned floor at this hour. Legacy anchor keeps the raw `now` and
                # no map → byte-identical rollback behavior (unchanged from pre-rev-2).
                if self.cfg.reserve_anchor == const.RESERVE_ANCHOR_TROUGH:
                    _cur_h_reserve = resolution.floor_to_slot(now, _slot_minutes)
                    _reserve_is_cheap = _build_is_cheap_by_hour(slots, self.cfg, _slot_minutes)
                else:
                    _cur_h_reserve = now
                    _reserve_is_cheap = None
                _reserve = energy.ride_out_reserve_kwh(
                    _cur_h_reserve, _ivs_reserve, self.cfg, is_cheap=_reserve_is_cheap,
                    slot_minutes=_slot_minutes, eta_curve=self._planner_curve(),
                )
                _surplus = energy.export_surplus_kwh(inputs.soc, _reserve, self.cfg)

                # Economic hurdle: does exporting now beat holding for later use?
                _keep_value = optimize_mod.compute_water_value(
                    # Use trough price as keep_value proxy (reuse existing helper).
                    # find_next_trough returns (dt, price); price is in €/kWh.
                    scheduler.find_next_trough(now, slots, self.cfg)[1],
                    self.cfg,
                )
                # Economic decision (which hours, how much) = the DP's committed plan.
                # Read the committed export RATE (W) for the current clock-hour; plan
                # membership is the hurdle gate.  No committed rate ⇒ no export (strictly
                # safer than the old ungated surplus-dump).  Real-time adaptation = the
                # live surplus clamp below + inverter net-export (house served first).
                # export_request is keyed on the slot grid (see _dp_select_slots);
                # slot-floor `now` so the lookup names the actual current slot.
                _cur_h = resolution.floor_to_slot(now, _slot_minutes)
                _committed_export = _dp_out.get("export_request") or {}
                _hurdle = _cur_h in _committed_export

                # Decide next export dwell/hysteresis state.
                _new_export_state = scheduler.decide_export_state(
                    self.export_state,
                    surplus_kwh=_surplus,
                    hurdle_clears=_hurdle,
                    now=now,
                    cfg=self.cfg,
                )

                if _new_export_state.engaged:
                    # NET target: drain the live surplus-above-reserve decisively
                    # over cfg.export_drain_window_h (default 0.0 → one tick → at the
                    # export cap, stopping at the live reserve on the final tick).
                    # _hurdle gates WHETHER to export (DP plan membership); committed
                    # rate no longer throttles HOW FAST.
                    _net_target_w = energy.export_net_target_w(
                        _surplus, self.cfg, eta_curve=self._planner_curve(),
                    )
                    # GROSS setpoint must cover house load (firmware serves house
                    # first, exports the remainder).  Bounded only by SETPOINT_MAX_W
                    # via discharge_cap_w (max_export_w already capped net_target).
                    # Only compensate with a FRESH read (A: fix for a safety
                    # regression) — a stale cached value (pv/batt sensor blip this
                    # tick, soc+meter still live so no failsafe) must not inflate
                    # the gross setpoint beyond the reserve-aware target;
                    # under-compensating (0.0) is the safe direction here.
                    _load_comp_w = (
                        self.cfg.export_load_comp_factor * _house_load_now_w
                        if self._house_load_fresh else 0.0
                    )
                    _gross_w = _net_target_w + _load_comp_w
                    _export_sp = guard.command_setpoint(
                        -_gross_w,
                        self._actuator.last_setpoint_w,
                        self.cfg,
                        discharge_cap_w=const.SETPOINT_MAX_W,
                    )
                    # command_setpoint returns positive value for discharge; engage_export
                    # validates > 0, so a sign error here fails loudly (safety-net).
                    if _export_sp > 0:
                        try:
                            await self._actuator.engage_export(_export_sp)
                            _export_setpoint_w = _export_sp
                            # R1: MEASURED export, not the commanded setpoint.
                            # _metered_export_w is the battery-sourced portion of
                            # the live grid export — min(meter export, battery
                            # discharge) — read directly from the meter + battery
                            # power sensors this tick.  Mirrors the daily-regret
                            # battery-sourced export rule (F3/actual_export_w
                            # above): PV-spill export is out of scope, only the
                            # energy actually drawn from the battery counts.
                            # Drives PnL + record; independent of the gross
                            # setpoint (which may be inflated by load_comp/
                            # quantization and does not reflect what was
                            # actually metered).
                            _batt_w_now = coordinator.read_float(
                                self._hass, self._data[const.CONF_ENT_BATTERY_POWER]
                            )
                            _metered_export_w = min(
                                max(0.0, -inputs.meter_w),
                                max(0.0, _batt_w_now if _batt_w_now is not None else 0.0),
                            )
                            _export_kwh = (
                                _metered_export_w / 1000.0 * (const.TICK_SECONDS / 3600.0)
                            )
                            _reserve_kwh_val = _reserve
                            _surplus_kwh_val = _surplus
                            # E3: accumulate realized PnL for this export interval.
                            # Price at the effective (post-fee) rate so PnL matches
                            # the DP's objective (gross − export_fee).
                            # PnL uses the DC-stored basis export_pnl_eur expects:
                            # convert AC metered export to DC drawn (AC / eta_discharge)
                            # so revenue = AC * price (the helper's eta_discharge cancels
                            # — no spurious second factor); cost/opportunity scale on DC
                            # energy actually dispatched. _export_kwh (recorded AC) is
                            # now the measured value above, not a setpoint estimate.
                            _eta_d = self._eta_d_at(_metered_export_w)
                            _export_kwh_dc = (
                                _export_kwh / _eta_d if _eta_d > 1e-9 else _export_kwh
                            )
                            # Retain for the NEXT tick's drift add-back (duration-scaled).
                            # The drift step re-zeros this field at its start; C3 re-sets
                            # it here only when an export actually fired this tick.
                            # Restart-gap caveat: if the process restarts between C3 and the
                            # next tick's end-of-tick _persist(), this value is lost and the
                            # add-back for that export window is skipped — self-correcting
                            # (one missed add-back → slight over-count in accumulator for
                            # one step). Do NOT add an extra _persist() here; that risks
                            # double-counting if C3 fires multiple times per tick.
                            self._soc_drift_last_export_kwh_dc = _export_kwh_dc
                            _eff_export_price = optimize_mod.effective_export_price(
                                _export_price, self.cfg
                            )
                            _tick_pnl = optimize_mod.export_pnl_eur(
                                _export_kwh_dc, _eff_export_price, _keep_value, self.cfg
                            )
                            self.today_export_pnl_eur += _tick_pnl
                        except Exception:
                            _LOGGER.error("Actuator engage_export failed (C3 path)", exc_info=True)
                            # Engage failed → do NOT report engaged. Force a clean
                            # disengaged state and best-effort release so the next
                            # tick starts from self-consumption (mirror FORCING L1409).
                            _new_export_state = ExportState(engaged=False, state_since=now)
                            await self._safe_release(
                                now,
                                "Actuator release_to_self failed (engage_export except)",
                                reset_export=False,
                            )
                    else:
                        # Surplus too small to quantize to a valid step — release.
                        _new_export_state = ExportState(engaged=False, state_since=now)
                        if self.export_state.engaged:
                            await self._safe_release(
                                now,
                                "Actuator release_to_self failed (C3 zero-rate path)",
                                reset_export=False,
                            )
                else:
                    # Gate fail or surplus below lo-eps: release if currently engaged.
                    if self.export_state.engaged:
                        await self._safe_release(
                            now,
                            "Actuator release_to_self failed (C3 disengage path)",
                            reset_export=False,
                        )

                self.export_state = _new_export_state
            else:
                # Export disabled or no export price: release if engaged.
                if self.export_state.engaged:
                    await self._safe_release(
                        now,
                        "Actuator release_to_self failed (export disabled path)",
                        reset_before_release=True,
                    )

        self.plan = new_plan
        await self._persist()
        await self._record_sample(
            now, inputs,
            setpoint=setpoint,
            state="passive" if _engage_failed else new_plan.state.value,
            weather_entry=_weather_entry,
            export_setpoint_w=_export_setpoint_w,
            export_kwh=_export_kwh,
            reserve_kwh=_reserve_kwh_val,
            surplus_kwh=_surplus_kwh_val,
            house_load_w=_house_load_now_w,
            persons_home=_persons_home_now,
        )

        # Stash decision snapshot for persistence by the recorder writer (A3).
        _price_window_e = [
            (s.start.isoformat(), s.price)
            for s in slots
            if deadline is not None and now <= s.start < deadline
        ]
        self.last_decision = self._build_decision_snapshot(
            now=now,
            active=new_plan.state is ControllerState.FORCING and not _engage_failed,
            soc=inputs.soc,
            deadline=deadline,
            committed_slots=new_plan.committed_slots,
            pv_remaining=pv_remaining,
            tomorrow_total=tomorrow_total,
            price_window=_price_window_e,
            setpoint=setpoint,
            state="passive" if _engage_failed else new_plan.state.value,
            horizon_mode=_horizon_mode_e,
        )

        # A3b: persist decision snapshot to decisions table.
        await self._persist_decision_snapshot()

        # A3b: daily regret job — run on first tick after LOCAL midnight, and
        # also on first tick after restart (_last_regret_day is None).
        await self._backfill_regret(now)

        if now.hour % 6 == 0 and now.hour != self._last_purge_hour:
            self._last_purge_hour = now.hour
            await self._hass.async_add_executor_job(
                self._recorder.purge_older_than, now.isoformat(), self.cfg.retention_days
            )
            # Purge stale decision rows on the same 6-hour schedule; else the
            # decisions table grows at ~1440 rows/day indefinitely.
            _cutoff = (now - timedelta(days=self.cfg.retention_days)).isoformat()
            await self._hass.async_add_executor_job(
                self._recorder.purge_decisions_older_than, _cutoff
            )
        required_kwh = max(0.0, (self.cfg.soc_target - inputs.soc) / 100.0 * self.cfg.capacity_kwh)
        # Full kWh required to reach soc_target from current SoC. Used as the
        # solar_charge_kwh status key so the dashboard shows the charge-to-target gap.
        solar_charge = required_kwh
        result = self._status(now, setpoint, deadline, "ok", solar_charge=solar_charge)
        if _engage_failed:
            # Override the published state only (self.plan intentionally left FORCING).
            result["state"] = "passive"
        # E2: surface the live export setpoint for observability only.
        # _status publishes 0.0/"passive" because export runs in the non-FORCING
        # branch with setpoint=0.0 — state is intentionally left untouched.
        # The recorder's smartcharge_state column (_record_sample, called above
        # at line 2201) already always records the plan state ("passive" during
        # export), so leaving last_status["state"] alone matches the recorded
        # history instead of diverging from it. A dedicated sensor reads the key.
        self.last_status["export_setpoint_w"] = _export_setpoint_w
        # T16: surface the live per-tick house load for observability. Mirrors
        # export_setpoint_w above — this line only runs in the enabled/"ok" tick
        # path. last_status is a persistent dict mutated in place (same as
        # export_setpoint_w), so a disabled/failsafe tick does NOT remove this
        # key — it simply leaves whatever value the last enabled tick wrote.
        # A dedicated sensor reads the key.
        self.last_status["house_load_w"] = _house_load_now_w
        self.last_status["plan"] = {
            "horizon": horizon,
            "deadline": deadline.isoformat() if deadline else None,
            "planned_grid_hours": sum(1 for e in horizon if e["mode"] == "grid"),
        }
        # Publish the DP optimizer's proposed horizon as a second (fictive) plan so
        # shadow mode is legible on the dashboard (T0.6a).  The fictive horizon is
        # always built via build_plan_horizon — identical per-entry schema to "plan".
        if _dp_out.get("dp_selected") is not None:
            # DP ran successfully — publish live DP's plan (T0.6a).
            # Published only when the DP succeeded; absent (key removed) otherwise so
            # consumers never see stale data.
            _fictive_h = plan_mod.build_plan_horizon(
                slots,
                _dp_out["intervals"],
                _dp_out["dp_selected"],
                inputs.soc,
                deadline,
                self.cfg,
                grid_request_by_hour=_dp_out.get("grid_request"),
                eta_curve=self._planner_curve(),
            )
            self.last_status["fictive_plan"] = {
                "horizon": _fictive_h,
                "deadline": deadline.isoformat() if deadline else None,
                "planned_grid_hours": sum(1 for e in _fictive_h if e["mode"] == "grid"),
            }
        else:
            # DP failed — remove stale key to prevent consumers from reading an
            # outdated fictive plan from a previous tick.
            self.last_status.pop("fictive_plan", None)
        return result

    def _write_decision_sync(self, snapshot: dict) -> None:
        """Synchronous: persist one decision snapshot to the decisions table.

        Safe to run in an executor thread.  The caller guarantees snapshot is
        a complete 12-key dict matching append_decision's kwargs signature.
        """
        self._recorder.append_decision(**snapshot)

    def _rollup_hourly_sync(self, now_iso: str, cutoff_iso: str) -> None:
        """Synchronous: roll up completed clock-hours into samples_hourly and purge old rows.

        Called once per clock-hour via async_add_executor_job (blocking sqlite calls).
        ``now_iso``    — current UTC time as ISO string (upper bound for rollup).
        ``cutoff_iso`` — delete samples_hourly rows whose hour_ts < this value.
        """
        self._recorder.rollup_hours(now_iso)
        self._recorder.purge_hourly_older_than(cutoff_iso)

    def _run_daily_regret_sync(self, day: str, computed_ts: str) -> None:
        """Synchronous: compute and persist the regret score for a completed local calendar day.

        Thin wrapper (Task C3) around ``regret_job.run_daily_regret`` — the
        actual computation (reads yesterday's samples from the recorder,
        buckets them by LOCAL hour, calls regret.hindsight_optimal_grid /
        realized_grid_cost / score_regret, upserts into daily_regret) moved
        verbatim to ``regret_job.py``. Never raises — any error is logged
        and the run is silently skipped (module function catches
        internally).

        Parameters
        ----------
        day          : YYYY-MM-DD LOCAL calendar day string.
        computed_ts  : ISO-8601 UTC timestamp to store as computed_ts in the row.
        """
        updates = regret_job.run_daily_regret(
            self._recorder, self.cfg, day, computed_ts,
            slot_minutes=self._detected_slot_minutes,
        )
        # Only apply keys the module function actually set — an early
        # return / caught exception there must leave these untouched here,
        # matching the pre-extraction self-mutating method's behavior.
        if "last_regret" in updates:
            self.last_regret = updates["last_regret"]
        if "last_dp_regret_7d" in updates:
            self.last_dp_regret_7d = updates["last_dp_regret_7d"]

    def _backfill_regret_sync(self, today_str: str, computed_ts: str) -> None:
        """Score any regret days missed since the last scored entry (up to 7 days back).

        Thin wrapper (Task C3) around ``regret_job.backfill_regret`` — see
        that function's docstring for the day-window / idempotency
        semantics. Called on the first tick after LOCAL midnight (or first
        tick ever after startup).
        """
        updates = regret_job.backfill_regret(
            self._recorder, self.cfg, today_str, computed_ts,
            slot_minutes=self._detected_slot_minutes,
        )
        if "last_regret" in updates:
            self.last_regret = updates["last_regret"]
        if "last_dp_regret_7d" in updates:
            self.last_dp_regret_7d = updates["last_dp_regret_7d"]

    def _build_decision_snapshot(
        self,
        *,
        now: datetime,
        active: bool,
        soc: float,
        deadline: datetime | None,
        committed_slots: tuple,
        pv_remaining: float | None,
        tomorrow_total: float | None,
        price_window: list,
        setpoint: float,
        state: str,
        horizon_mode: str,
    ) -> dict:
        """Build the self.last_decision dict with identical 12-key schema for both paths.

        Called from the disabled (shadow) path and the enabled path so the keys/types
        can never silently diverge — A3 calls append_decision(**self.last_decision).
        """
        return {
            "ts": now.isoformat(),
            "active": active,
            "start_soc": float(soc),
            "deadline": deadline.isoformat() if deadline else None,
            "committed_hours": [h.isoformat() for h in committed_slots],
            "horizon_mode": horizon_mode,
            "pv_today_forecast_kwh": float(pv_remaining) if pv_remaining is not None else None,
            "pv_tomorrow_forecast_kwh": float(tomorrow_total) if tomorrow_total is not None else None,
            "predicted_load_json": None,
            "price_window_json": json.dumps(price_window) if price_window else None,
            "setpoint_w": float(setpoint),
            "state": state,
        }

    def _occ_status_attrs(self, now: datetime) -> dict:
        """Occupancy-corrector observability attrs (Layer B)."""
        return {
            # Clamped to the same state bin multiplier() uses (0..STATE_MAX), so
            # occ_state_now and occ_expected_state are directly comparable.
            "occ_state_now": (
                min(occupancy.STATE_MAX, max(0, int(self._persons_home_now)))
                if self._persons_home_now is not None else None
            ),
            "occ_expected_state": (
                self._occ_table.climo_state.get(occupancy.band_of(now))
                if self._occ_table is not None else None
            ),
            "occ_multiplier": round(
                occupancy.multiplier(
                    self._occ_table, self._persons_home_now, now, now,
                    self.cfg.occ_persistence_h, self.cfg.occ_adapt_fraction,
                ), 3,
            ),
            "occ_cells_ready": (
                self._occ_table.cells_ready if self._occ_table is not None else 0
            ),
        }

    def _status(self, now, setpoint, deadline, reason, solar_charge: float = 0.0) -> dict:
        _regret = self.last_regret or {}
        _bt = self.backtest_result or {}
        self.last_status = {
            "state": self.plan.state.value,
            "solar_charge_kwh": round(solar_charge, 3),
            "setpoint_w": setpoint,
            "deadline": deadline.isoformat() if deadline else None,
            "reason": reason,
            "load_mae": _bt.get("model_mae"),
            "horizon_energy_mae_24h": _bt.get("horizon_energy_mae_24h"),
            "horizon_energy_mae_12h": _bt.get("horizon_energy_mae_12h"),
            "pinball_p50": _bt.get("pinball_p50"),
            "pinball_p80": _bt.get("pinball_p80"),
            "active_model": self.active_model_name,
            "load_adapt_ratio": (
                round(self._load_adapt_ratio, 3)
                if self._load_adapt_ratio is not None else None
            ),
            "load_adapt_matched_hours": self._load_adapt_matched,
            **self._occ_status_attrs(now),
            "regret_eur": _regret.get("regret_eur"),
            "over_buy_kwh": _regret.get("over_buy_kwh"),
            "under_buy_kwh": _regret.get("under_buy_kwh"),
            # 7-day rolling DP-vs-heuristic regret delta (T0.5c).
            # Negative = DP was cheaper over past 7 days; None until first day scored.
            "dp_regret_7d": self.last_dp_regret_7d,
            # E3: realized export PnL for the current local day (€).
            # Accumulated per tick when the C3 export executor fires.
            # Resets to 0.0 on local-day rollover.  G2 reads this key.
            "today_export_pnl_eur": round(self.today_export_pnl_eur, 6),
            # Cash ledger (spec 2026-07-10): realized battery cash flows.
            # battery_net_today/total drive the two €-sensors; components are
            # exposed for the today-sensor's attributes.
            "today_charge_cost_eur": round(self.today_charge_cost_eur, 6),
            "today_export_revenue_eur": round(self.today_export_revenue_eur, 6),
            "battery_net_today_eur": round(
                self.today_export_revenue_eur - self.today_charge_cost_eur, 6
            ),
            "battery_net_total_eur": round(self.total_net_eur, 6),
            # C4: the DP's PLANNED export revenue (€) for the current horizon. Drives
            # the card's arbitrage_pnl so it reflects the plan, not just realized ticks.
            "planned_export_revenue_eur": round(self.planned_export_revenue_eur, 6),
            "slot_minutes": self._detected_slot_minutes,
            # T18: measured efficiency curve bin table, for observability only
            # (does not drive behaviour — that's gated by use_measured_eta below).
            "efficiency_curve": self._eta_curve.as_attributes(),
            "use_measured_eta": self.cfg.use_measured_eta,
        }
        return self.last_status

    def _rollover_daily_ledgers(self, now: datetime) -> None:
        """Reset ALL per-day € ledgers on local-day rollover. See ledger.CashLedger.rollover."""
        self._ledger.rollover(now)

    def _accumulate_cash_ledger(
        self,
        now: datetime,
        inputs: PlantInputs,
        slots: list[PriceSlot],
        slot_minutes: int,
        raw_export_price: float | None,
    ) -> None:
        """Accumulate realized battery cash flows for this tick. See ledger.CashLedger.accumulate."""
        self._ledger.accumulate(
            self._hass, self._data, self.cfg,
            now, inputs, slots, slot_minutes, raw_export_price,
        )

    async def release(self) -> None:
        """Release actuator control back to self (best-effort; never raises).

        Waited behind the tick lock (bounded by _SHUTDOWN_LOCK_TIMEOUT_S) so an
        unload/reload cannot interleave the release with an in-flight tick's
        engage_* calls, and so __init__.async_unload_entry's recorder.close runs
        only after the in-flight tick's awaited recorder writes have drained.
        """
        acquired = False
        try:
            await asyncio.wait_for(
                self._tick_lock.acquire(), timeout=_SHUTDOWN_LOCK_TIMEOUT_S
            )
            acquired = True
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001 — never block teardown
            _LOGGER.warning(
                "release: tick lock not acquired within %ss; releasing anyway",
                _SHUTDOWN_LOCK_TIMEOUT_S,
            )
        try:
            await self._actuator.release_to_self()
        except Exception:
            _LOGGER.warning("release_to_self failed during controller release", exc_info=True)
        finally:
            if acquired:
                self._tick_lock.release()

    async def _persist(self) -> None:
        payload = {
            "plan": self.plan.to_dict(),
            "enabled": self.enabled,
        }
        for store_key, attr_name, to_json, _from_json, _none_guard in _PERSIST_FIELDS:
            payload[store_key] = to_json(getattr(self, attr_name))
        await self._store.async_save(payload)

    async def set_enabled(self, value: bool) -> None:
        """Set the master enable flag and persist it immediately."""
        self.enabled = bool(value)
        await self._persist()

    def restore(self, saved: dict) -> None:
        """Restore plan + enabled + export_state from a saved store payload (back-compatible).

        New format is {"plan": <dict>, "enabled": <bool>, "export_state": <dict>};
        a legacy bare plan dict (no "plan" key) restores the plan only and leaves
        enabled and export_state at their defaults.
        Missing "export_state" key is silently ignored — preserves the existing
        initial (disengaged) state so upgrades from pre-C3 stores are safe.
        """
        try:
            if "plan" in saved:
                self.plan = PlanState.from_dict(saved["plan"])
                self.enabled = bool(saved.get("enabled", False))
            else:
                self.plan = PlanState.from_dict(saved)
        except (KeyError, ValueError, TypeError):
            pass
        # Table-driven restore (Task A9), grouped exactly as the original
        # hand-written try/except blocks (see _PERSIST_GROUPS): a genuine
        # parse error on one field aborts the remaining fields in its group
        # (fields already assigned earlier in the same group stay assigned),
        # while a stored ``None`` on a none_guard field is skipped without
        # aborting the group. Groups are independent of one another.
        for group in _PERSIST_GROUPS:
            try:
                for store_key, attr_name, _to_json, from_json, none_guard in group:
                    if store_key not in saved:
                        continue
                    value = saved[store_key]
                    if none_guard and value is None:
                        continue
                    setattr(self, attr_name, from_json(value))
            except (KeyError, ValueError, TypeError):
                pass

    async def _record_sample(
        self,
        now,
        inputs,
        *,
        setpoint: float,
        state: str,
        weather_entry: dict | None = None,
        export_setpoint_w: float | None = None,
        export_kwh: float | None = None,
        reserve_kwh: float | None = None,
        surplus_kwh: float | None = None,
        house_load_w=_UNSET,
        persons_home: int | None = None,
    ) -> None:
        """Read the live physical sample and append one recorder row.

        Used by both the active path (state=plan.state.value) and the disabled
        path (state="disabled", setpoint 0).

        ``weather_entry`` is the hourly forecast dict for the current clock-hour
        (from coordinator.get_forecast_for_hour).  None → all 4 weather columns
        stored as NULL.

        C3 export signal columns: populated on export ticks; None on non-export ticks.
        ``export_setpoint_w`` — the positive inverter setpoint sent to engage_export.
        ``export_kwh``        — MEASURED battery-sourced export kWh for this tick
                                 (R1: min(meter export, battery discharge) × TICK_SECONDS
                                 /3600, not derived from the commanded setpoint).
        ``reserve_kwh``       — DC kWh the battery must retain (ride-out reserve).
        ``surplus_kwh``       — DC kWh available above the reserve (available to export).
        """
        pv_w = coordinator.read_pv_power_w(self._hass, self._data)
        batt_w = coordinator.read_float(self._hass, self._data[const.CONF_ENT_BATTERY_POWER])
        import_price = coordinator.read_float(self._hass, self._data.get(const.CONF_ENT_PRICE, ""))
        irradiance = coordinator.read_float(self._hass, self._data.get(const.CONF_ENT_IRRADIANCE, ""))
        temp_ent = self._data.get(const.CONF_ENT_TEMP)
        temp = (
            coordinator.read_attr(self._hass, temp_ent, "temperature")
            if temp_ent is not None
            else None
        )
        # House load: use the value threaded from the active tick if provided
        # (single compute, consistent with actuation); otherwise compute it here
        # (disabled path, which does not pass house_load_w).
        if house_load_w is _UNSET:
            house_load = self._compute_house_load_w(inputs)
        else:
            house_load = house_load_w
        # Read the live feed-in tariff from the configured export-price entity.
        # Fallback: None (stored as NULL) when entity is absent or empty string.
        # Rationale: recording NULL is safer than mirroring the import price, which
        # would over-credit export by the full energy-tax component of the import rate.
        # Post-hoc analysis of NULL rows simply skips export-revenue attribution.
        _rec_export_price_ent = self._data.get(const.CONF_ENT_EXPORT_PRICE, "")
        rec_export_price = (
            coordinator.read_float(self._hass, _rec_export_price_ent)
            if _rec_export_price_ent
            else None
        )
        row = {
            "ts": now.isoformat(),
            "hour": now.hour,
            "weekday": now.weekday(),
            "soc": inputs.soc,
            # Legacy 3-phase columns retired (the Anker X1 meter reports one signed
            # scalar, not per-phase import); schema unchanged, so these stay NULL.
            "p1_l1": None,
            "p1_l2": None,
            "p1_l3": None,
            "p1_w": inputs.meter_w,
            "state": state,
            "setpoint_w": setpoint,
            "pv_w": pv_w,
            "batt_w": batt_w,
            "import_price": import_price,
            # Real feed-in tariff from CONF_ENT_EXPORT_PRICE entity (v7 fix).
            # NULL when entity is unconfigured; never mirrors import price.
            "export_price": rec_export_price,
            "irradiance": irradiance,
            "temp": temp,
            # Weather-forecast columns (all None when forecast unavailable).
            "temp_forecast": weather_entry.get("temp_forecast") if weather_entry else None,
            "cloud_cover": weather_entry.get("cloud_cover") if weather_entry else None,
            "humidity": weather_entry.get("humidity") if weather_entry else None,
            "wind_speed": weather_entry.get("wind_speed") if weather_entry else None,
            # Ground-truth house load, computed per-tick by _compute_house_load_w
            # (pv + meter_w + batt − inverter_loss), which clamps to ≥ 0 itself.
            # Never NULL in practice from the two tick() call sites — cache
            # fallback (N2) covers a pv/batt sensor blip.
            "load_w": house_load,
            # v7 export-arbitrage signal columns.
            # Populated by the C3 export executor on export ticks; None otherwise.
            "export_setpoint_w": export_setpoint_w,
            "export_kwh": export_kwh,
            "reserve_kwh": reserve_kwh,
            "surplus_kwh": surplus_kwh,
            "persons_home": persons_home,
        }
        # Row dict built on the loop thread (HA state reads above must stay on-loop).
        # Only the blocking SQLite write moves off-loop, mirroring _write_decision_sync.
        await self._hass.async_add_executor_job(self._recorder.append, row)

    # ── tick() extraction helpers ─────────────────────────────────────────────
    # Shared read/persist logic extracted from the enabled and disabled tick()
    # branches.  Behaviour-identical: callers assign the return values to their
    # own local names (e.g. _shadow_export_price vs _export_price) so the
    # distinct naming in each branch is preserved.

    def _compute_house_load_w(self, inputs) -> float:
        """Live house load (W): pv + meter_w (signed net grid, + = import) +
        batt (+ = discharge, − = charge) − inverter_loss, clamped to ≥ 0 (house
        load cannot physically be negative; cross-read skew between the pv/
        meter/batt sensors on a given tick can otherwise yield a small negative
        value).

        inverter_loss reads 0.0 when its sensor is unavailable (it genuinely
        reads 0 while charging/idle and may drop out).  pv is read via
        coordinator.read_pv_power_w, which sums every entity in the
        normalized CONF_ENT_PV_POWER list (legacy single-string config sums
        just that one entity); it is None only when every resolved PV entity
        is unavailable.  pv or batt unavailable → skip the compute for this
        tick entirely and fall back to the cached last-known value (N2)
        rather than publish a number built from a stale mixture of reads.
        On a successful compute, refresh the cache so later unavailable
        ticks fall back to this fresher value.

        Also sets ``self._house_load_fresh`` (True on a live compute, False on
        a cache-fallback) so callers that must not act on stale data — the
        export gross-setpoint compensation — can tell the two apart.
        """
        pv_w = coordinator.read_pv_power_w(self._hass, self._data)
        batt_w = coordinator.read_float(self._hass, self._data[const.CONF_ENT_BATTERY_POWER])
        if pv_w is None or batt_w is None:
            self._house_load_fresh = False
            return self._last_house_load_w
        loss_w = coordinator.read_float(
            self._hass,
            self._data.get(
                const.CONF_ENT_INVERTER_LOSS,
                const.DEFAULT_ENTITIES[const.CONF_ENT_INVERTER_LOSS],
            ),
        )
        house_load_w = max(
            0.0, pv_w + inputs.meter_w + batt_w - (loss_w if loss_w is not None else 0.0)
        )
        self._last_house_load_w = house_load_w
        self._house_load_fresh = True
        return house_load_w

    def _read_forecast_bundle(self) -> tuple[list | None, list | None]:
        """Per-day PV watts arrays; warn when exactly one day is available."""
        today_watts = coordinator.read_pv_today_watts(self._hass, self._data)
        tomorrow_watts = coordinator.read_pv_tomorrow_watts(self._hass, self._data)
        if (today_watts is None) != (tomorrow_watts is None):
            _LOGGER.warning(
                "PV watts available for only one day (today=%s, tomorrow=%s); "
                "that day's PV will be absent from the plan",
                today_watts is not None,
                tomorrow_watts is not None,
            )
        return today_watts, tomorrow_watts

    def _resolve_export_price(self) -> tuple[float | None, bool]:
        """Live feed-in tariff + whether it points at the same entity as import.

        Static tariff mode bypasses the sensor path entirely: it returns the
        configured constant ``static_price_export`` (None when <= 0, i.e. no
        export credit) and never mirrors the import price.
        """
        if self.cfg.price_mode == const.PRICE_MODE_STATIC:
            px = self.cfg.static_price_export
            return (px if px > 0.0 else None), False
        export_ent = self._data.get(const.CONF_ENT_EXPORT_PRICE, "")
        import_ent = self._data.get(const.CONF_ENT_PRICE, "")
        price = coordinator.read_float(self._hass, export_ent) if export_ent else None
        matches = bool(export_ent and export_ent == import_ent)
        return price, matches

    def _resolve_slot_minutes(self, slots) -> int:
        """Per-refresh detected slot length, latched to the finest seen this UTC day.

        `slot_resolution` override hard-pins (no latch).  Stores the effective value
        on `self._detected_slot_minutes` for the diagnostic.  At 60-min detection is
        a stable 60 → no latch change → parity-safe.
        """
        detected = resolution.resolve_slot_minutes(slots, self.cfg.slot_resolution)
        if self.cfg.slot_resolution != const.SLOT_RESOLUTION_AUTO:
            self._detected_slot_minutes = detected
            return detected
        now_utc = dt_util.utcnow()
        effective, self._res_latch = resolution.latch_finest(detected, now_utc, self._res_latch)
        self._detected_slot_minutes = effective
        return effective

    async def _persist_decision_snapshot(self) -> None:
        """Persist self.last_decision to the decisions table (off-loop) if it has a ts."""
        if self.last_decision.get("ts"):
            await self._hass.async_add_executor_job(
                self._write_decision_sync, self.last_decision
            )

    async def _backfill_regret(self, now) -> None:
        """Run the daily-regret backfill on the first tick of a new local day / after restart."""
        today = dt_util.as_local(now).date().isoformat()
        if today != self._last_regret_day:
            await self._hass.async_add_executor_job(
                self._backfill_regret_sync, today, now.isoformat()
            )
        self._last_regret_day = today
