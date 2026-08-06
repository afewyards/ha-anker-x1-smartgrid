"""Periodic full-charge calibration policy — pure decision logic.

Design: docs/superpowers/specs/2026-08-03-battery-calibration-policy-design.md

The pack strands ~3.6 kWh below ~21% SoC (measured 2026-08-03) and has had no
opportunity to top-balance: on 2026-08-02 it reached 99% and then held 0 W for
2.5 h while PV was exported.  This module decides when to drive the pack to the
top of its range and dwell there so the module BMSs get taper current.

No HA imports, no I/O, no clock reads — ``now`` is always a parameter.  A
completed cycle is READ BACK from SoC history rather than stored, so there is
no new table, no Store, and the policy is restart-safe by construction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from . import const
from .models import Config, ControllerState, PriceSlot

# Two adjacent samples further apart than this do not belong to the same run —
# otherwise an HA outage spanning a high-SoC period fakes a completed dwell.
MAX_SAMPLE_GAP_MIN: float = 15.0


def continue_soc(cfg: Config) -> float:
    """SoC at/above which an ALREADY-STARTED dwell keeps counting.

    A continuation allowance, never an entry discount — the dwell only starts
    at ``calibration_top_soc`` itself. The distinction is the whole point:
    balancing happens in the taper, and the taper is the last point or two of
    the charge curve. Measured on this pack 2026-07-30, charge was still
    -3.9 kW at 98% and -5.1 kW at 99% (both bulk), dropping to -540 W only at
    the very top. An hour spent at 98 or 99 is therefore an hour of ordinary
    charging, and balances nothing.

    The allowance is needed because at a true 100% the inverter cuts charge
    dead and the pack self-discharges into house load (+270 W measured on the
    same run). Without it a real dwell would break on that drift alone, even
    though FORCING is still commanded and will top the pack straight back up.
    """
    return cfg.calibration_top_soc - const.CALIBRATION_HOLD_TOLERANCE


@dataclass(frozen=True)
class CalibAction:
    """An active calibration slot.  ``phase`` is for reporting only —
    both phases actuate identically (FORCING at max rate; the BMS taper
    turns that into a hold once the pack is full)."""

    phase: str  # "charging" | "holding"
    window_start: datetime
    window_end: datetime


# Phases that actuate. "scheduled" is deliberately absent: a window may be
# accepted hours before it starts, and forcing then would charge at whatever
# price happens to be live rather than the cheap one that was selected.
_ACTIVE_PHASES = frozenset({"charging", "holding"})


@dataclass(frozen=True)
class CalibPlan:
    """The cycle's state this tick, for display AND actuation.

    Strictly wider than ``CalibAction``: it also carries ``scheduled`` (a
    window accepted but not yet started) and ``idle``, neither of which may
    actuate. ``action`` is the narrowing — the ONLY way to get an actuatable
    value out — so the plan sensor can draw a coming window without any risk
    of the controller engaging on it.
    """

    phase: str  # "idle" | "scheduled" | "charging" | "holding"
    window_start: datetime | None
    window_end: datetime | None

    @property
    def action(self) -> CalibAction | None:
        if self.phase not in _ACTIVE_PHASES:
            return None
        # Both active phases always carry a window (set at every construction
        # site below), so the asserts are for the type checker, not runtime.
        assert self.window_start is not None and self.window_end is not None
        return CalibAction(phase=self.phase, window_start=self.window_start, window_end=self.window_end)


_IDLE = CalibPlan(phase="idle", window_start=None, window_end=None)


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def history_span_days(soc_samples: list[tuple[str, float, str | None]]) -> float:
    """Wall-clock days covered by the sample series (0.0 when < 2 rows).

    Precondition: ``soc_samples`` is ascending by timestamp, as guaranteed by
    ``recorder.read_soc_samples``'s ``ORDER BY ts ASC``. This computes
    ``samples[-1] - samples[0]``, so an out-of-order series would silently
    yield a wrong (even negative) span.
    """
    if len(soc_samples) < 2:
        return 0.0
    return (_parse(soc_samples[-1][0]) - _parse(soc_samples[0][0])).total_seconds() / 86400.0


def _forced(state: str | None) -> bool:
    """Was the controller commanding the charge on this sample?

    Without this, any incidental plateau counts: a sunny afternoon parks the
    pack at the top for hours with the controller passive and 0 W commanded,
    which delivers no taper current and balances nothing, yet would close the
    cycle and reset the clock. Measured on the live pack, that made the policy
    self-suppressing — 19 "successes" over 42 days, all passive, so the 5-day
    interval never once elapsed and the override never fired.
    """
    return state == ControllerState.FORCING


def last_success_end(
    soc_samples: list[tuple[str, float, str | None]],
    *,
    target_soc: float,
    continue_soc: float,
    dwell_h: float,
) -> datetime | None:
    """End timestamp of the most recent completed calibration dwell.

    A dwell is a maximal block of consecutive FORCING samples that STARTS at
    or above ``target_soc`` and CONTINUES while at or above ``continue_soc``,
    with no adjacent gap over ``MAX_SAMPLE_GAP_MIN``, spanning at least
    ``dwell_h``.  Returns the block's LAST timestamp, so an in-progress hold
    keeps the clock at ~now and the policy goes idle as soon as it qualifies.

    The asymmetric entry/continuation bars are deliberate — see
    ``continue_soc``. Entry at the target is what makes the hour an hour at
    the TOP rather than an hour of bulk charging a point or two below it.

    A non-forcing sample, or one below ``continue_soc``, breaks the run: the
    current stopped or the pack left the top, so the halves either side are
    separate dwells and may not be summed.

    Returns None when no block qualifies — including an empty series.

    Precondition: ``soc_samples`` is ascending by timestamp, as guaranteed by
    ``recorder.read_soc_samples``'s ``ORDER BY ts ASC``. Duplicate timestamps
    are benign (zero delta, run continues). An out-of-order series is not
    guarded against here: a ``ts`` preceding the open run's ``run_end`` makes
    ``ts - run_end`` negative, which is always ``<= max_gap``, so the run
    would silently keep extending backward and corrupt both the run's
    duration and the most-recent-wins result.
    """
    best: datetime | None = None
    run_start: datetime | None = None
    run_end: datetime | None = None
    max_gap = timedelta(minutes=MAX_SAMPLE_GAP_MIN)
    need = timedelta(hours=dwell_h)

    for ts_s, soc, state in soc_samples:
        ts = _parse(ts_s)
        forced = _forced(state)
        if forced and soc >= continue_soc and run_end is not None and ts - run_end <= max_gap:
            run_end = ts
            continue
        # Close the open run (if any) before starting a new one.
        if run_start is not None and run_end is not None and run_end - run_start >= need:
            best = run_end
        if forced and soc >= target_soc:
            run_start, run_end = ts, ts
        else:
            run_start, run_end = None, None

    if run_start is not None and run_end is not None and run_end - run_start >= need:
        best = run_end
    return best


def _open_run_start(
    soc_samples: list[tuple[str, float, str | None]],
    *,
    target_soc: float,
    continue_soc: float,
) -> datetime | None:
    """Start of the dwell run still open at the end of ``soc_samples`` (i.e.
    containing the LAST row), or None if the last row doesn't qualify
    (including an empty series).

    Same entry/continuation rule and gap tolerance as ``last_success_end`` but
    reports the start of the trailing run regardless of whether it has reached
    ``dwell_h`` yet -- used only to report an in-progress hold's real start
    (F4), never for success detection.
    """
    run_start: datetime | None = None
    run_end: datetime | None = None
    max_gap = timedelta(minutes=MAX_SAMPLE_GAP_MIN)
    for ts_s, soc, state in soc_samples:
        ts = _parse(ts_s)
        forced = _forced(state)
        if forced and soc >= continue_soc and run_end is not None and ts - run_end <= max_gap:
            run_end = ts
            continue
        if forced and soc >= target_soc:
            run_start, run_end = ts, ts
        else:
            run_start, run_end = None, None
    return run_start


def compute_days_since(
    last_success: datetime | None,
    span_days: float,
    now: datetime,
    cfg: Config,
) -> float | None:
    """Days since the last qualifying calibration dwell.

    Falls back to ``span_days`` (the read-window's wall-clock span) when no
    dwell has ever qualified but the history is long enough to call the
    policy overdue. None when neither holds (fresh install). Shared by
    ``calibration_action`` and the controller's own status computation so the
    two cannot drift out of sync (F3).
    """
    if last_success is not None:
        return (now - last_success).total_seconds() / 86400.0
    if span_days >= cfg.calibration_interval_days:
        return span_days
    return None


def price_percentile(price_history: dict[str, dict[str, float]], pct: float) -> float | None:
    """Linear-interpolated percentile of every slot price in the history ring.

    Returns None for an empty history — the caller must then refuse the
    percentile path and let only the deadline path fire.
    """
    values = sorted(v for day in price_history.values() for v in day.values())
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    pos = (pct / 100.0) * (len(values) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (pos - lo)


def _charge_kwh(soc_pct: float, cfg: Config) -> float:
    """Grid energy needed to lift SoC from ``soc_pct`` to the calibration top."""
    return max(0.0, (cfg.calibration_top_soc - soc_pct) / 100.0 * cfg.capacity_kwh)


def _charge_h(soc_pct: float, cfg: Config) -> float:
    """Hours to lift SoC from ``soc_pct`` to the calibration top, at max rate."""
    rate_kw = cfg.max_charge_w / 1000.0 * cfg.eta_charge_safe()
    if rate_kw <= 0.0:
        return 0.0
    return _charge_kwh(soc_pct, cfg) / rate_kw


def _soc_at(
    when: datetime,
    live_soc: float,
    soc_forecast: list[tuple[datetime, float]] | None,
) -> float:
    """Projected SoC at ``when`` from the DP's own plan horizon.

    Falls back to ``live_soc`` when there is no projection covering ``when`` —
    an absent horizon (startup, a failed DP run) or a candidate starting before
    the horizon does. Borrowing the first row's value instead would read a
    future solar peak back onto the present and size the window as a free
    top-up that the pack cannot actually do.

    Precondition: ``soc_forecast`` is ascending by timestamp (as built from
    ``plan.build_plan_horizon``, which emits slots in order).
    """
    if not soc_forecast:
        return live_soc
    found = live_soc
    for ts, soc in soc_forecast:
        if ts > when:
            break
        found = soc
    return found


# Tolerance for treating two chronologically-adjacent slots as truly
# contiguous. Guards against float/second rounding in stored timestamps
# without letting a REAL price-curve gap be silently spanned as if it were
# elapsed time — mirrors the gap-awareness MAX_SAMPLE_GAP_MIN already gives
# the SoC-history path above, just for the price-slot path.
_CONTIGUITY_TOLERANCE = timedelta(minutes=1.0)


def _slot_duration_min(slot: PriceSlot) -> float:
    """This slot's own duration in minutes.

    Falls back to 60.0 for a missing (``None``), zero, or invalid (negative)
    duration — ``duration_min or 60.0`` alone would NOT catch a negative
    value (a negative number is truthy), so the sign is checked explicitly.
    """
    dur = slot.duration_min
    return dur if dur and dur > 0.0 else 60.0


def select_window(
    now: datetime,
    soc_pct: float,
    slots: list[PriceSlot],
    *,
    cfg: Config,
    bar: float | None,
    force: bool,
    soc_forecast: list[tuple[datetime, float]] | None = None,
) -> tuple[datetime, datetime] | None:
    """Least-cost acceptable contiguous window, or None.

    Two things make "least cost" different from "cheapest per kWh":

    Each candidate is sized from the SoC the pack is PROJECTED to have when
    that candidate starts, not the SoC it has now. Sizing every candidate from
    the live SoC costs a window opening at the solar peak as though it still
    had to charge from this morning's empty pack.

    Candidates are then ranked by what they actually cost -- grid kWh times
    mean price -- not by mean price alone. Live lab 2026-08-06: a 0.127 EUR/kWh
    midday block needing 8.56 kWh (1.10 EUR, and 5.32 kWh of planned solar
    displaced with nowhere to go) beat a 0.24 EUR/kWh window at the natural
    solar top needing 0.15 kWh (0.04 EUR). Ranking on price alone cannot see
    that the expensive window is 30x cheaper, because the whole point is that
    solar has already done the climb for free.

    Deterministic in (now, slots, soc, cfg, bar, force, soc_forecast):
    published prices do not change within a day, so re-running each tick yields
    the same answer and no commitment needs storing.
    """
    if not slots:
        return None
    ordered = sorted(slots, key=lambda s: s.start)

    # Build candidates: variable-length runs of REAL, chronologically-adjacent
    # slots — each contributing its OWN duration_min, never a single width
    # sampled once and extrapolated — whose summed duration covers `need_min`
    # and that have not fully elapsed. A real gap between two slots forecloses
    # spanning it (see _CONTIGUITY_TOLERANCE above): the price curve can mix
    # cadences (e.g. hourly slots followed by 15-min slots, or a genuine
    # missing-data hole), and neither may be papered over with arithmetic.
    candidates: list[tuple[float, float, float, datetime, datetime]] = []
    for i in range(len(ordered)):
        # Sized per candidate, from the SoC projected at ITS OWN start.
        need_kwh = _charge_kwh(_soc_at(ordered[i].start, soc_pct, soc_forecast), cfg)
        need_min = (_charge_h(_soc_at(ordered[i].start, soc_pct, soc_forecast), cfg) + cfg.calibration_dwell_h) * 60.0
        acc_min = 0.0
        weighted_price = 0.0
        prev_end: datetime | None = None
        end: datetime | None = None
        for slot in ordered[i:]:
            if prev_end is not None and abs(slot.start - prev_end) > _CONTIGUITY_TOLERANCE:
                break  # real discontinuity in the price curve — do not span it
            dur_min = _slot_duration_min(slot)
            acc_min += dur_min
            weighted_price += slot.price * dur_min
            prev_end = slot.start + timedelta(minutes=dur_min)
            if acc_min >= need_min:
                end = prev_end
                break
        if end is None:
            continue  # ran out of slots, or hit a gap, before covering need_min
        start = ordered[i].start
        if end <= now:
            continue
        mean_price = weighted_price / acc_min
        candidates.append((need_kwh * mean_price, mean_price, need_kwh, start, end))
    if not candidates:
        return None

    # One candidate per UTC start-date (the cheapest) -- `cand[1]` (PriceSlot.start)
    # is UTC-normalised by parsers.py, and this module never threads a timezone
    # in, so the boundary is 00:00 UTC (02:00 local in CEST), not local
    # midnight: up to 2 attempts per local day, not 1.
    per_day: dict[object, tuple[float, float, float, datetime, datetime]] = {}
    for cand in candidates:
        key = cand[3].date()
        # Strict `<` keeps the EARLIEST of equally-costed candidates, which is
        # what a pack already at the top produces (every need_kwh is 0.0).
        if key not in per_day or cand[0] < per_day[key][0]:
            per_day[key] = cand

    # Earliest date with an acceptable window wins.  Taking the EARLIEST rather
    # than the globally cheapest is what stops the 13:00 publication of
    # tomorrow's prices from pulling a cycle off a today window that already
    # qualified — the "never abandon a started window" rule, expressed without
    # storing any commitment.
    for key in sorted(per_day):
        mean_price, need_kwh, start, end = per_day[key][1:]
        # The bar exists to stop calibrating at an expensive TIME. A window that
        # barely needs the grid has no expensive time to speak of, and gating it
        # on price would strand the cycle at exactly the placement the cost
        # ranking just picked as best — waiting out the grace period only to
        # deadline-force a worse one.
        if force or need_kwh <= const.CALIBRATION_FREE_TOPUP_KWH or (bar is not None and mean_price <= bar):
            return (start, end)
    return None


def calibration_plan(
    now: datetime,
    soc_pct: float,
    slots: list,
    soc_samples: list[tuple[str, float, str | None]],
    price_history: dict[str, dict[str, float]],
    cfg,
    *,
    already_holding: bool = False,
    soc_forecast: list[tuple[datetime, float]] | None = None,
) -> CalibPlan:
    """The cycle's full state this tick, including a window that has been
    accepted but has not started yet (``scheduled``).

    Fail-closed: absent or too-short history yields ``idle`` rather than
    "never calibrated, charge now".

    ``already_holding`` -- true iff the PREVIOUS tick's action was already
    "holding". Softens the hold re-entry bar to ``continue_soc`` (F1) so SoC
    quantisation/load-spike noise at the boundary cannot toggle
    holding/charging/idle every tick and churn the inverter mode. This module
    is pure and keeps no state of its own, so the caller must supply it.

    ``soc_forecast`` -- (slot start, projected SoC) from the DP's own plan
    horizon, ascending. Placement only; the DP never sees calibration back, so
    the quarantine still holds in the direction that matters. Absent, window
    sizing falls back to the live SoC (see ``_soc_at``).

    ``scheduled`` exists ONLY so the plan sensor and card can draw the coming
    window; ``CalibPlan.action`` withholds it from actuation. Callers deciding
    whether to force MUST go through ``calibration_action``.
    """
    if not cfg.calibration_enabled:
        return _IDLE

    cont_soc = continue_soc(cfg)
    last = last_success_end(
        soc_samples,
        target_soc=cfg.calibration_top_soc,
        continue_soc=cont_soc,
        dwell_h=cfg.calibration_dwell_h,
    )
    span = history_span_days(soc_samples)
    days_since = compute_days_since(last, span, now, cfg)
    if days_since is None or days_since < cfg.calibration_interval_days:
        return _IDLE

    # Hold-through: once the pack is AT (or, mid-hold, within 1 point of) the
    # top and a cycle is due, keep holding until the dwell completes,
    # independent of the window.  The price curve's back-horizon is not
    # guaranteed deep enough to keep re-selecting a window that started hours
    # ago (coordinator.read_price_slots passes the sensor's curve through
    # verbatim), and a stranded half-dwell buys the charge without the
    # balancing it was for.  Ends by itself: the moment the run reaches
    # dwell_h, last_success_end returns and days_since drops to ~0.
    hold_bar = cont_soc if already_holding else cfg.calibration_top_soc
    if soc_pct >= hold_bar:
        run_start = _open_run_start(soc_samples, target_soc=cfg.calibration_top_soc, continue_soc=cont_soc) or now
        return CalibPlan(
            phase="holding",
            window_start=run_start,
            window_end=run_start + timedelta(hours=cfg.calibration_dwell_h),
        )

    force = days_since >= cfg.calibration_interval_days + const.CALIBRATION_GRACE_DAYS
    bar = price_percentile(price_history, const.CALIBRATION_PRICE_PERCENTILE)
    win = select_window(now, soc_pct, slots, cfg=cfg, bar=bar, force=force, soc_forecast=soc_forecast)
    if win is None:
        return _IDLE

    start, end = win
    if not (start <= now < end):
        # Accepted a future window: report it so the plan can draw it, but
        # `.action` withholds it so nothing actuates early.
        return CalibPlan(phase="scheduled", window_start=start, window_end=end)

    # Always "charging": the hold-through branch above already returned
    # whenever soc_pct >= hold_bar, and nothing between here and there mutates
    # soc_pct or cfg (Config is frozen) -- so this point is only ever reached
    # with soc_pct < hold_bar, i.e. genuinely still climbing.
    return CalibPlan(phase="charging", window_start=start, window_end=end)


def calibration_action(
    now: datetime,
    soc_pct: float,
    slots: list,
    soc_samples: list[tuple[str, float, str | None]],
    price_history: dict[str, dict[str, float]],
    cfg,
    *,
    already_holding: bool = False,
    soc_forecast: list[tuple[datetime, float]] | None = None,
) -> CalibAction | None:
    """Whether a calibration cycle is ACTUATING in the slot containing ``now``.

    Derived from ``calibration_plan`` rather than computed separately, so the
    displayed state and the actuated one cannot drift apart -- the same
    single-source-of-truth reason ``compute_days_since`` is shared (F3).
    """
    return calibration_plan(
        now,
        soc_pct,
        slots,
        soc_samples,
        price_history,
        cfg,
        already_holding=already_holding,
        soc_forecast=soc_forecast,
    ).action
