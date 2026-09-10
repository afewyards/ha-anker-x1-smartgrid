"""Periodic full-charge calibration policy — pure decision logic.

Design: docs/superpowers/specs/2026-08-03-battery-calibration-policy-design.md, amended by
docs/superpowers/specs/2026-09-10-calibration-cost-aware-placement-design.md

The pack strands ~3.6 kWh below ~21% SoC (measured 2026-08-03) and has had no
opportunity to top-balance: on 2026-08-02 it reached 99% and then held 0 W for
2.5 h while PV was exported.  This module decides when to drive the pack to the
top of its range and dwell there so the module BMSs get taper current.

No HA imports, no I/O, no clock reads — ``now`` is always a parameter.  A
completed cycle is READ BACK from SoC history rather than stored, so there is
no new table, no Store, and the policy is restart-safe by construction.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise

from . import const, daily_stats
from .models import Config, ControllerState

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

    ``cost_eur`` is what this tick's calibration costs against the plan (see
    ``window_cost``): the accepted window or hold, else the cheapest rejected
    one, a costed fresh hold included. Carried unchanged while a cycle is
    committed; None when nothing was costed.
    """

    phase: str  # "idle" | "scheduled" | "charging" | "holding"
    window_start: datetime | None
    window_end: datetime | None
    cost_eur: float | None = None

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


def plan_peak_soc(soc_pct: float, cal_slots: list[CalibSlot]) -> float:
    """Highest SoC the pack is expected to see: the plan's peak, or live SoC.

    No window may start below ``const.CALIBRATION_MIN_START_SOC``, so a plan
    peaking under it is why an overdue cycle places nothing -- public so the
    controller's overdue warning can say so.

    The live SoC is folded in so an absent plan (startup, a DP exception)
    degrades to "is the pack near the top right now" rather than to a
    fabricated climb.
    """
    return max([soc_pct, *(s.soc_end for s in cal_slots)])


# Tolerance for treating two chronologically-adjacent slots as truly
# contiguous. Guards against float/second rounding in stored timestamps
# without letting a REAL price-curve gap be silently spanned as if it were
# elapsed time — mirrors the gap-awareness MAX_SAMPLE_GAP_MIN already gives
# the SoC-history path above, just for the price-slot path.
_CONTIGUITY_TOLERANCE = timedelta(minutes=1.0)


@dataclass(frozen=True)
class CalibSlot:
    """One future plan slot as the cost model sees it. Energies cover [t0, t1]:
    t0 is ``now`` for the in-progress slot (the plan models only its remaining
    minutes), the slot start otherwise. SoC runs soc_start -> soc_end over it."""

    t0: datetime
    t1: datetime
    import_price: float
    export_price: float | None  # post-fee; None zeroes the revenue leg
    pv_kwh: float
    load_kwh: float
    soc_start: float
    soc_end: float
    plan_import_kwh: float
    plan_export_kwh: float


def build_calib_slots(
    horizon: list[dict],
    now: datetime,
    live_soc: float,
    slot_minutes: int,
    export_price_at: Callable[[datetime, float | None], float | None],
    delivered_at: Callable[[datetime], float] | None = None,
) -> list[CalibSlot]:
    """``CalibSlot``s for the future, uncompleted rows of a plan horizon.

    Skips ``estimated`` rows, past-actual rows (``mode == "actual"``), rows
    missing SoC/price, and rows that have already fully elapsed. The
    in-progress row (if any) is truncated to ``[now, row end]``.
    """
    dur = timedelta(minutes=slot_minutes)
    out: list[CalibSlot] = []
    prev_soc = live_soc
    for row in horizon:
        if row.get("estimated") or row.get("mode") == "actual" or row.get("soc") is None or row.get("price") is None:
            continue
        start = datetime.fromisoformat(row["start"])
        end = start + dur
        if end <= now:
            continue
        t0 = max(start, now)
        # build_plan_horizon scales only the flow columns to the remaining
        # minutes; pv_kwh/load_kwh stay full-slot on the in-progress row.
        frac = (end - t0) / dur
        pv = float(row.get("pv_kwh") or 0.0) * frac
        load = float(row.get("load_kwh") or 0.0) * frac
        charge = float(row.get("grid_charge_kwh") or 0.0)
        if start <= now and delivered_at is not None:
            charge = max(0.0, charge - float(delivered_at(start) or 0.0))
        imp, exp = daily_stats.planned_house_flows({**row, "pv_kwh": pv, "load_kwh": load}, charge)
        price = float(row["price"])
        soc_end = float(row["soc"])
        out.append(CalibSlot(t0, end, price, export_price_at(start, price), pv, load, prev_soc, soc_end, imp, exp))
        prev_soc = soc_end
    return out


def _plan_soc_at(cal_slots: list[CalibSlot], when: datetime) -> float | None:
    """Linearly-interpolated plan SoC at ``when``, or None outside every slot."""
    for s in cal_slots:
        if s.t0 <= when <= s.t1:
            span = (s.t1 - s.t0).total_seconds()
            f = (when - s.t0).total_seconds() / span if span > 0 else 0.0
            return s.soc_start + (s.soc_end - s.soc_start) * f
    return None


def window_cost(
    cal_slots: list[CalibSlot],
    start: datetime,
    end: datetime,
    *,
    cfg: Config,
    water_value: float | None,
) -> float | None:
    """EUR a calibration over [start, end] costs relative to the plan, or None
    when the plan does not cover the window contiguously.

    Calibration charges at max rate to the top (solar first), then holds: the
    battery never discharges, so load minus PV meets the grid. The energy it
    leaves in the pack above the plan is credited at the DP's water value.
    """
    covering = [s for s in cal_slots if s.t1 > start and s.t0 < end]
    if not covering or covering[0].t0 > start or covering[-1].t1 < end:
        return None
    for a, b in pairwise(covering):
        if abs(b.t0 - a.t1) > _CONTIGUITY_TOLERANCE:
            return None
    soc0, soc1 = _plan_soc_at(cal_slots, start), _plan_soc_at(cal_slots, end)
    if soc0 is None or soc1 is None:
        return None
    e, e_top = cfg.pct_to_kwh(soc0), cfg.pct_to_kwh(cfg.calibration_top_soc)
    eta = cfg.eta_charge_safe()
    delta = 0.0
    for s in covering:
        m_h = (min(s.t1, end) - max(s.t0, start)).total_seconds() / 3600.0
        span_h = (s.t1 - s.t0).total_seconds() / 3600.0
        f = m_h / span_h if span_h > 0 else 0.0
        charge_ac = 0.0
        if e < e_top:
            charge_ac = min(cfg.max_charge_w / 1000.0 * m_h, (e_top - e) / eta)
            e += charge_ac * eta
        net = (s.load_kwh - s.pv_kwh) * f + charge_ac
        exp_p = s.export_price or 0.0
        cal_eur = max(0.0, net) * s.import_price - max(0.0, -net) * exp_p
        plan_eur = (s.plan_import_kwh * s.import_price - s.plan_export_kwh * exp_p) * f
        delta += cal_eur - plan_eur
    return delta - (water_value or 0.0) * max(0.0, e - cfg.pct_to_kwh(soc1))


@dataclass(frozen=True)
class WindowPick:
    """``select_window``'s answer. Unaccepted, it is the cheapest candidate, so
    a due cycle left idle can still report what calibrating would cost."""

    start: datetime
    end: datetime
    cost_eur: float
    accepted: bool


def _acceptable(cost: float, need_kwh: float, *, bar: float | None, force: bool) -> bool:
    """Normal: the top-up bought at the price bar, plus an allowance for what
    the window displaces (no bar: the allowance alone). Overdue: a flat cap."""
    if force:
        return cost <= const.CALIBRATION_OVERDUE_COST_CAP_EUR
    return cost <= need_kwh * (bar or 0.0) + const.CALIBRATION_COST_ALLOWANCE_EUR


def select_window(
    now: datetime,
    cal_slots: list[CalibSlot],
    *,
    cfg: Config,
    bar: float | None,
    force: bool,
    water_value: float | None,
) -> WindowPick | None:
    """Where to calibrate, priced against the DP's own plan; None when no
    candidate exists.

    One candidate per unfinished plan slot whose projected SoC at its start
    clears ``const.CALIBRATION_MIN_START_SOC``, sized from that SoC: the
    top-up at max rate, then the hold. ``window_cost`` prices what the window
    displaces -- planned export, battery-served load -- so the pre-peak slot
    the DP fills only to export it is dear and a midday PV spill is free. A
    window the plan does not cover contiguously has no cost and is no
    candidate.

    The earliest UTC day with an acceptable candidate wins, and within it the
    cheapest acceptable one. Taking the EARLIEST rather than the globally
    cheapest is what stops the 13:00 publication of tomorrow's prices from
    pulling a cycle off a today window that already qualified. With nothing
    acceptable, the cheapest candidate comes back unaccepted.

    Re-run every tick against the latest plan, so a scheduled window can
    still move; only a running cycle is committed (see ``calibration_plan``).
    """
    cands: list[tuple[float, float, datetime, datetime]] = []
    for s in cal_slots:
        if s.t1 <= now or s.soc_start < const.CALIBRATION_MIN_START_SOC:
            continue
        end = s.t0 + timedelta(hours=_charge_h(s.soc_start, cfg) + cfg.calibration_dwell_h)
        cost = window_cost(cal_slots, s.t0, end, cfg=cfg, water_value=water_value)
        if cost is not None:
            cands.append((cost, _charge_kwh(s.soc_start, cfg), s.t0, end))
    if not cands:
        return None
    # Filter BEFORE taking each day's cheapest: the normal bar scales with
    # need_kwh, so a day's cheapest candidate can fail while a dearer one passes.
    per_day: dict[object, tuple[float, float, datetime, datetime]] = {}
    for c in cands:
        if _acceptable(c[0], c[1], bar=bar, force=force):
            key = c[2].date()  # UTC date; see the 2026-08-03 spec
            if key not in per_day or c[0] < per_day[key][0]:
                per_day[key] = c
    if per_day:
        cost, _need, start, end = per_day[min(per_day)]  # earliest acceptable day wins
        return WindowPick(start, end, cost, True)
    cost, _need, start, end = min(cands, key=lambda c: (c[0], c[2]))
    return WindowPick(start, end, cost, False)


def calibration_plan(
    now: datetime,
    soc_pct: float,
    cal_slots: list[CalibSlot],
    soc_samples: list[tuple[str, float, str | None]],
    price_history: dict[str, dict[str, float]],
    cfg: Config,
    *,
    prev: CalibPlan | None = None,
    water_value: float | None = None,
) -> CalibPlan:
    """The cycle's full state this tick, including a window that has been
    accepted but has not started yet (``scheduled``).

    Fail-closed: absent or too-short history yields ``idle`` rather than
    "never calibrated, charge now".

    ``prev`` -- the PREVIOUS tick's plan; this module is pure and keeps no
    state of its own, so the caller must supply it. A cycle it shows running
    is committed: ``charging`` keeps its window until the window ends, a top-out
    straight after a ``charging`` tick starts the hold uncosted even at or past
    that end, and ``holding`` continues down to ``continue_soc`` -- a deadband
    so SoC quantisation/load-spike noise at the boundary cannot toggle
    holding/charging/idle every tick and churn the inverter mode (F1).

    ``water_value`` -- the DP's own EUR/kWh refill value, crediting energy a
    window leaves in the pack (see ``window_cost``); None credits nothing,
    which overstates cost and so fails closed.

    With nothing committed, a top-out the pack reached on its own is held only
    if the hold is itself acceptable; otherwise ``select_window`` places a
    window, and one containing ``now`` starts ``charging`` only once the LIVE
    pack clears ``const.CALIBRATION_MIN_START_SOC`` -- until then it is
    ``scheduled``. With no plan (startup, a DP failure) nothing new is placed
    or held.

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

    dwell = timedelta(hours=cfg.calibration_dwell_h)

    def _hold(cost: float | None) -> CalibPlan:
        run_start = _open_run_start(soc_samples, target_soc=cfg.calibration_top_soc, continue_soc=cont_soc) or now
        return CalibPlan("holding", run_start, run_start + dwell, cost)

    # A running cycle is never re-costed or re-gated: abandoning it mid-way
    # buys the charge without the balancing it was for. The hold still ends by
    # itself: once the run reaches dwell_h, last_success_end finds it and the
    # not-due check above goes idle.
    if prev is not None and prev.phase == "holding" and soc_pct >= cont_soc:
        return _hold(prev.cost_eur)
    if prev is not None and prev.phase == "charging":
        # Ahead of the window guard: the pack can top out on the first tick
        # at or past window_end, and that hold was priced with the window.
        if soc_pct >= cfg.calibration_top_soc:
            return _hold(prev.cost_eur)
        if prev.window_end is not None and now < prev.window_end:
            return CalibPlan("charging", prev.window_start, prev.window_end, prev.cost_eur)

    force = days_since >= cfg.calibration_interval_days + const.CALIBRATION_GRACE_DAYS
    bar = price_percentile(price_history, const.CALIBRATION_PRICE_PERCENTILE)

    # A self top-out (solar, or the DP's own charge) is held only if the hold
    # itself is cheap -- the DP often fills the pack precisely to export it.
    hold_cost: float | None = None
    if soc_pct >= cfg.calibration_top_soc:
        hold_cost = window_cost(cal_slots, now, now + dwell, cfg=cfg, water_value=water_value)
        if hold_cost is not None and _acceptable(hold_cost, 0.0, bar=bar, force=force):
            return _hold(hold_cost)

    pick = select_window(now, cal_slots, cfg=cfg, bar=bar, force=force, water_value=water_value)
    if pick is None:
        return CalibPlan("idle", None, None, hold_cost)
    if not pick.accepted:
        return CalibPlan("idle", None, None, pick.cost_eur if hold_cost is None else min(hold_cost, pick.cost_eur))
    if pick.start <= now < pick.end and soc_pct >= const.CALIBRATION_MIN_START_SOC:
        return CalibPlan("charging", pick.start, pick.end, pick.cost_eur)
    return CalibPlan("scheduled", pick.start, pick.end, pick.cost_eur)


def calibration_action(
    now: datetime,
    soc_pct: float,
    cal_slots: list[CalibSlot],
    soc_samples: list[tuple[str, float, str | None]],
    price_history: dict[str, dict[str, float]],
    cfg: Config,
    *,
    prev: CalibPlan | None = None,
    water_value: float | None = None,
) -> CalibAction | None:
    """Whether a calibration cycle is ACTUATING in the slot containing ``now``.

    Derived from ``calibration_plan`` rather than computed separately, so the
    displayed state and the actuated one cannot drift apart -- the same
    single-source-of-truth reason ``compute_days_since`` is shared (F3).
    """
    return calibration_plan(
        now,
        soc_pct,
        cal_slots,
        soc_samples,
        price_history,
        cfg,
        prev=prev,
        water_value=water_value,
    ).action
