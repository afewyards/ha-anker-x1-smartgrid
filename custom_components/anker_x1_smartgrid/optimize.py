"""Forecast-fed DP grid optimizer — online twin of ``regret.hindsight_optimal_grid``.

This module provides :func:`optimize_grid`, the **forecast-fed** counterpart to
:func:`regret.hindsight_optimal_grid`.  Where the hindsight function operates on
*realized* 24-hour :class:`~regret.DayData`, ``optimize_grid`` accepts an
**arbitrary-length window** ``[window_start_h, window_start_h + window_len)``
of forecast arrays, enabling online use as a model-predictive controller (MPC).

Parity invariant (T0.1b proven)
--------------------------------
When called with a **full realized day** (``window_start_h=0``,
``window_len=24``, realized pv/load/price arrays), ``optimize_grid`` produces
results **provably identical** to ``regret.hindsight_optimal_grid`` on the
matching ``DayData``: same per-hour schedule, same total kWh, same total EUR,
same infeasible flag.  This equivalence is verified by the parity gate in
``tests/test_optimize_parity.py`` (≥3 scenario classes + 60 random-day
property iterations, seeded for determinism).

Only the data source differs between the two call paths (forecast arrays vs.
DayData); the DP algorithm, bin arithmetic, physics functions, and
end-state selection logic are structurally identical.

Shared-physics contract
-----------------------
Battery physics (:func:`~regret._apply_solar_load`, :data:`~regret._BIN_KWH`)
are **imported from regret.py and never duplicated here**.  Any change to those
functions automatically propagates to both hindsight scoring and the online
optimizer, preserving the parity invariant required by the T0.1b gate.

Window contract
---------------
All three input sequences (``window_pv``, ``window_load``, ``price``) must
have exactly ``window_len`` elements.  A :class:`ValueError` is raised if any
sequence length mismatches ``window_len``.  ``window_start_h`` carries the
wall-clock offset of the first element (0–23) and is exposed to callers for
plan alignment; the DP itself is window-position-agnostic (only relative order
within the window matters).

Algorithm
---------
Identical DP to :func:`~regret.hindsight_optimal_grid`, generalised to
``window_len`` steps instead of a hardcoded 24:

1. Discretise SoC into bins of :data:`~regret._BIN_KWH` kWh.
2. Forward pass: for each hour in the window, for every reachable SoC state,
   apply solar/load via :func:`~regret._apply_solar_load`, then enumerate
   feasible DC grid charges ``g_dc ∈ [0, max_grid_dc]``.  Keep the
   minimum-cost predecessor and grid-AC value per (hour, bin) cell.
3. After ``window_len`` steps, pick the cheapest end state with SoC ≥ target.
   If the target is unreachable, fall back to the best achievable end state
   and set ``infeasible=True``.
4. Backtrack parent pointers to reconstruct the per-hour AC schedule.

Pure-Python / stdlib only
-------------------------
No numpy, no sklearn.  Safe to run on the HAOS box (CPython 3.14 / musl).
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from . import const
from .models import Config
from .regret import (
    _BIN_KWH,
    _apply_solar_load,
    _eta_charge_at,
    _eta_discharge_at,
    _max_grid_dc,
    windowed_peak_prices,
)

def compute_water_value(trough_price: float, cfg: Config) -> float:
    """Terminal water value v (€/DC-kWh) for the water-value end selection.

    ``v = (trough_price / eta) * water_value_factor``.  When
    ``clamp_water_value_nonneg`` is True a negative or zero trough price clamps
    v to 0.0, pushing the optimal end SoC down toward the survival floor.
    """
    eta = cfg.eta_charge if cfg.eta_charge > 1e-9 else 1.0
    v = (trough_price / eta) * cfg.water_value_factor
    if cfg.clamp_water_value_nonneg:
        v = max(0.0, v)
    return v


def effective_export_price(raw_export_price: float, cfg: Config) -> float:
    """Net export price after the flat feed-in fee.

    ``effective_export_price = raw_export_price − cfg.export_fee_eur_per_kwh``.

    Not clamped: a negative result simply fails the DP/executor no-op guard
    (``price·η_d − cycle_cost ≤ 0`` ⇒ no export), so a fee above the tariff
    correctly suppresses export rather than paying to feed in.
    """
    return raw_export_price - cfg.export_fee_eur_per_kwh


# ---------------------------------------------------------------------------
# Export value-stack gate (C1)
# ---------------------------------------------------------------------------


def eta_discharge(cfg: Config) -> float:
    """Discharge efficiency derived from round-trip and charge efficiencies.

    ``eta_discharge = min(round_trip_eff / eta_charge, 1.0)``

    All energy terms in this module are per **DC kWh** stored.  The round-trip
    efficiency encodes the full charge→store→discharge cycle; dividing by
    ``eta_charge`` isolates the discharge half.

    The result is clamped to ``≤ 1.0`` because a discharge efficiency above 1.0
    is physically impossible.  Without the clamp a misconfiguration where
    ``eta_charge < round_trip_eff`` would inflate the export-revenue estimate
    above the AC export price itself, biasing the gate toward over-exporting.
    This mirrors the identical clamp in ``plan.py:128``.

    Guards against zero or near-zero ``eta_charge`` to avoid division by zero.
    """
    eta_c = cfg.eta_charge if cfg.eta_charge > 1e-9 else 1.0
    return min(cfg.round_trip_eff / eta_c, 1.0)


def export_pnl_eur(
    export_kwh: float,
    export_price: float,
    keep_value: float,
    cfg: Config,
) -> float:
    """Modeled PnL for one export interval (€).

    Computes the net profit (or loss) from exporting ``export_kwh`` of stored
    energy at the live feed-in tariff ``export_price`` during a single tick.

    .. note::
        ``export_kwh`` is the **modeled** (commanded) energy, derived from the
        inverter setpoint (``setpoint_w / 1000 / 12`` for a 5-min tick).  It is
        *not* measured throughput.  The ledger accumulated across ticks is therefore
        a **modeled PnL** approximation, not a ground-truth meter reading.

    Formula (all per-DC-kWh values scaled by ``export_kwh``)::

        pnl = export_kwh * export_price * eta_discharge(cfg)
              - cfg.cycle_cost_eur_per_kwh * export_kwh
              - keep_value * export_kwh

    Where:

    - ``export_kwh * export_price * eta_discharge`` is the AC revenue converted
      to a DC basis.
    - ``cycle_cost_eur_per_kwh * export_kwh`` is the battery degradation cost
      for the energy discharged.
    - ``keep_value * export_kwh`` is the opportunity cost of dispatching this
      energy now rather than holding it for later import avoidance.

    A negative result indicates that exporting was uneconomic (the gate should
    have prevented this, but it can occur e.g. when ``keep_value`` is based on a
    stale price snapshot, the export price moved intra-interval, or the hysteresis
    dwell keeps the executor engaged for a tick after the hurdle stops clearing).
    The ledger records net PnL including these losses — it is not clamped at zero.

    Parameters
    ----------
    export_kwh :
        Modeled (commanded) energy exported in this tick interval (DC kWh).
    export_price :
        Live feed-in tariff (€/AC kWh) at the time of export.
    keep_value :
        Opportunity cost of dispatching the stored energy now vs. later
        (€/DC kWh), typically from ``compute_water_value``.
    cfg :
        System configuration (provides ``cycle_cost_eur_per_kwh``,
        ``round_trip_eff``, ``eta_charge``).

    Returns
    -------
    float
        Net PnL for this export interval in euros.  Positive = profitable;
        negative = loss (uneconomic dispatch or intra-interval price movement).
    """
    eta_d = eta_discharge(cfg)
    revenue = export_kwh * export_price * eta_d
    cost = cfg.cycle_cost_eur_per_kwh * export_kwh
    opportunity = keep_value * export_kwh
    return revenue - cost - opportunity


def cash_flows_eur(
    meter_w: float,
    batt_w: float,
    import_price: float | None,
    export_price_eff: float | None,
    tick_h: float,
) -> tuple[float, float]:
    """Cash-basis ``(cost, credit)`` € pair for one tick interval.

    Sign conventions: ``meter_w`` positive = grid import, negative = export;
    ``batt_w`` positive = battery discharge, negative = charge.

    - cost   — grid-attributed battery-charge energy × ``import_price``
    - credit — battery-sourced export energy × ``export_price_eff``

    Attribution is ``min()`` of the two signed readings: PV covers the house
    first, PV-spill export is excluded — mirrors the C3 battery-sourced-export
    rule in controller.py.  A leg whose price is ``None`` contributes 0.0
    (caller-level skip).  Prices may be negative (negative day-ahead hours,
    fee above tariff); results are deliberately unclamped — cash is cash.
    NO cycle-cost / eta / opportunity deductions: this is the cash ledger,
    distinct from the economic ``export_pnl_eur``.
    """
    grid_charge_w = min(max(0.0, meter_w), max(0.0, -batt_w))
    batt_export_w = min(max(0.0, -meter_w), max(0.0, batt_w))
    cost = (
        grid_charge_w / 1000.0 * tick_h * import_price
        if import_price is not None
        else 0.0
    )
    credit = (
        batt_export_w / 1000.0 * tick_h * export_price_eff
        if export_price_eff is not None
        else 0.0
    )
    return cost, credit


def solar_reservation_ceiling(
    window_pv: list[float],
    window_load: list[float],
    cfg: Config,
    *,
    cycle_end_idx: list[int] | None = None,
    dt_h: float = 1.0,
) -> list[float]:
    """Per-hour ABSOLUTE SoC ceiling (DC kWh) for grid charging.

    Reserves headroom for the CURRENT solar cycle's remaining forecast solar so
    grid charge never fills capacity that free solar will reach:

        ceiling[h] = clamp(target_kwh - future_solar_dc[h], floor_kwh, target_kwh)
        future_solar_dc[h] = sum_{j=h+1 .. cycle_end_idx[h]-1}
                             min(max(0, pv[j]-load[j]), rate)*eta

    NO survival floor — insufficient solar rides the pack to the firmware floor.
    Cloud-robust: every positive-net hour in the cycle window counts (a midday
    net<=0 dip contributes 0 but does NOT terminate the sum).  cycle_end_idx
    defaults to a single cycle spanning the whole window.  Pure/deterministic so
    both DPs receive an identical array (parity-safe).
    """
    n = len(window_pv)
    cap_kwh = cfg.capacity_kwh
    floor_kwh = cfg.soc_floor / 100.0 * cap_kwh
    target_kwh = cfg.soc_target / 100.0 * cap_kwh
    rate_kwh_h = cfg.max_charge_w / 1000.0 * dt_h
    eta = cfg.eta_charge if cfg.eta_charge > 1e-9 else 1.0
    solar_dc = [
        min(max(0.0, window_pv[j] - window_load[j]), rate_kwh_h) * eta
        for j in range(n)
    ]
    if cycle_end_idx is None:
        cycle_end_idx = [n] * n
    ceiling: list[float] = []
    for h in range(n):
        end = min(cycle_end_idx[h], n)
        future = sum(solar_dc[j] for j in range(h + 1, end))
        ceiling.append(max(floor_kwh, target_kwh - future))
    return ceiling


def solar_cycle_end_idx(
    now_h: datetime,
    window_len: int,
    sun_times: tuple[datetime, datetime, datetime] | None,
    slot_minutes: int = 60,
) -> list[int]:
    """Per-hour exclusive cycle-boundary index (next sunrise) for the ceiling.

    Hours before tomorrow's sunrise reserve only TODAY's remaining solar
    (evening/overnight contribute 0 -> ceiling==target); hours from tomorrow's
    sunrise on reserve tomorrow's solar.  ``sun_times`` is the
    (today_sunset, tomorrow_sunrise, tomorrow_sunset) tuple from compute_decision;
    None or no in-window sunrise -> single-cycle fallback [window_len]*window_len.

    ``slot_minutes`` (default 60, legacy-identical) sizes the returned index in
    the SAME slot units as ``window_len`` — at 15-min resolution the dawn
    boundary is a slot count (e.g. 16 quarter-hour slots for a 4h-ahead
    sunrise), not an hour count, or the ceiling's cycle window would end
    ~4x too early.
    """
    out = [window_len] * window_len
    if sun_times is None:
        return out
    _today_sunset, tomorrow_sunrise, _tomorrow_sunset = sun_times
    sr_idx = int(round((tomorrow_sunrise - now_h).total_seconds() / (slot_minutes * 60)))
    if 0 < sr_idx < window_len:
        for h in range(window_len):
            out[h] = sr_idx if h < sr_idx else window_len
    return out


def build_charge_mask(
    price: list[float],
    ceiling: float | None,
    price_band: float | None = None,
    window_min: float | None = None,
    trough: Sequence[float | None] | None = None,
    price_valid: Sequence[bool] | None = None,
) -> list[bool]:
    """Build a per-hour chargeability mask from a price ceiling and optional trough band.

    Parameters
    ----------
    price :
        All-in electricity price (€/kWh) for each hour of the window.
    ceiling :
        Maximum price at which grid-charging is still economical (€/kWh),
        typically computed by
        :func:`~scheduler.charge_price_ceiling` as
        ``peak * round_trip_eff``.

        Pass ``None`` when the post-deadline peak price is unknown — the
        function then returns **all-False** (fail-closed), so no hour is
        considered chargeable.  This mirrors the fail-closed semantics of
        :func:`~scheduler.peak_price`, which returns ``None`` when no
        post-deadline slot is available.
    price_band :
        Optional trough-proximity band (€/kWh).  When provided, an hour is
        chargeable only if ``price[h] <= trough + price_band`` IN ADDITION to
        the ceiling gate.  The two conditions are ANDed; the trough band
        normally dominates because it is far tighter than the ceiling.

        ``None`` (default) disables the band, preserving the legacy
        ceiling-only behaviour exactly (no change to existing callers).

        Edge cases:
        - Empty ``price`` list → empty mask (no crash).
        - All prices equal → all chargeable (trough == every price).
        - ``ceiling is None`` → fail-closed regardless of ``price_band``.
    window_min :
        Explicit trough price (€/kWh) for the band gate.  When provided,
        ``trough_threshold = window_min + price_band`` is used instead of
        deriving the trough from ``min(price)``.

        **Why this matters**: the DP path in ``_dp_select_slots`` builds
        ``window_price`` by padding hours outside the real price-data window
        with ``0.0``.  If ``window_min`` were computed as ``min(price)``, those
        0.0-padded entries would collapse the trough to 0.0 → threshold ≈ 0.005
        → every real-priced hour fails → mask all-False → grid charging killed.

        Pass the minimum of the *real* slot prices for the window (e.g.
        ``min(s.price for s in slots if now_h <= s.start < deadline_ceil)``).
        ``None`` (default) falls back to ``min(price)`` for backward
        compatibility with callers that don't have 0.0-padding.
    trough :
        Optional PER-HOUR trough reference (€/kWh), ``Sequence[float | None]``,
        length == ``len(price)``.  When provided it OVERRIDES ``window_min``: hour h
        is chargeable only if ``price[h] <= ceiling`` and
        ``price[h] <= trough[h] + price_band``.  A ``None`` entry fails closed.  This
        is the look-back charge band (mirror of the export ``windowed_peak_prices``
        gate).  ``None`` (default) preserves the scalar ``window_min`` behaviour
        exactly.
    price_valid :
        Optional per-hour validity mask (length == ``len(price)``). ``False`` at
        an index fails that hour CLOSED in every path — used to reject 0.0-padded
        phantom-price hours that would otherwise satisfy the trough band. ``None``
        (default) treats every hour as valid (byte parity).

    Returns
    -------
    list[bool]
        ``chargeable[h] = True`` when ``price[h] <= ceiling`` (and, when
        ``price_band`` is not None, also ``price[h] <= trough + price_band``
        where ``trough = window_min if window_min is not None else min(price)``).
        All-False when ``ceiling is None`` (fail-closed on unknown peak).

    Notes
    -----
    This helper is deliberately free of scheduler imports.  The caller
    computes ``ceiling`` via :func:`~scheduler.charge_price_ceiling` and
    passes it in, keeping ``optimize.py`` decoupled from ``scheduler.py``.
    """
    if ceiling is None:
        return [False] * len(price)
    if not price:
        return []
    valid = price_valid if price_valid is not None else [True] * len(price)
    if trough is not None:
        # Per-hour windowed-trough gate (look-back band): each hour is judged
        # against its OWN trough[h] = min over [h - lookback, horizon_edge).  None
        # entries (no real price in range) fail closed.
        band = price_band if price_band is not None else 0.0
        return [
            (v and t is not None and p <= ceiling and p <= t + band)
            for p, t, v in zip(price, trough, valid)
        ]
    if price_band is not None:
        trough_v = window_min if window_min is not None else min(price)
        trough_threshold = trough_v + price_band
        return [v and p <= ceiling and p <= trough_threshold for p, v in zip(price, valid)]
    return [v and p <= ceiling for p, v in zip(price, valid)]


def optimize_grid(
    window_pv: list[float],
    window_load: list[float],
    price: list[float],
    soc_start: float,
    cfg: Config,
    *,
    window_start_h: int,
    window_len: int,
    chargeable: list[bool] | None = None,
    feed_in: list[float] | None = None,
    export_price: list[float] | None = None,
    terminal_mode: str = "reserve",
    water_value: float | None = None,
    reserve_by_hour: list[float] | None = None,
    grid_charge_ceiling: list[float] | None = None,
    hedge_drain_kwh: list[float] | None = None,
    dt_h: float = 1.0,
    slots_per_day: int = 24,
    day_index: list[int] | None = None,
    eta_curve=None,
) -> dict:
    """Minimum-cost grid schedule for a forecast window (DP-exact).

    Parameters
    ----------
    window_pv :
        Forecast PV energy (AC kWh) for each hour of the window.
        Must have exactly ``window_len`` elements.
    window_load :
        Forecast house load (AC kWh) for each hour of the window.
        Must have exactly ``window_len`` elements.
    price :
        All-in electricity price (€/kWh) for each hour of the window.
        Must have exactly ``window_len`` elements.
    soc_start :
        Battery state-of-charge at the start of the window (%, 0–100).
    cfg :
        Battery / system configuration.
    window_start_h :
        Slot index of the first element in the window, counted from local
        midnight in units of the window's own slot width (== wall-clock hour
        at 60-min resolution; == quarter-hour index at 15-min resolution).
        Used only to derive the default per-hour ``day_index`` (see below)
        when ``day_index`` is not supplied — the DP itself is otherwise
        window-position-agnostic.  Also passed through so callers can align
        the returned schedule with wall-clock hours for plan publishing.
    window_len :
        Number of hours in the window (must equal ``len(window_pv)`` etc.).
    slots_per_day :
        Number of slots in one calendar day at the window's resolution
        (default ``24`` = hourly).  Used with ``window_start_h`` to derive the
        default ``day_index`` (``(window_start_h + h) // slots_per_day``)
        when ``day_index`` is ``None``.  At ``slots_per_day=24`` this is
        byte-identical to the legacy hardcoded ``// 24`` arithmetic.
    day_index :
        Optional explicit per-slot calendar-day id (same length as the
        window), passed straight through to :func:`regret.windowed_peak_prices`
        for the export peak-band reference.  When ``None`` (default) it is
        derived from ``window_start_h`` and ``slots_per_day``.
    chargeable :
        Optional per-hour chargeability mask.  ``chargeable[h] = True``
        permits grid charging in hour ``h``; ``False`` forces the grid charge
        for that stage to exactly zero (the transition loop skips all
        ``g_dc > 0`` steps for that hour).

        When ``None`` (default) every hour is fully permissive — this is the
        standard behaviour and **preserves the T0.1b parity invariant exactly**
        (no change to the forward-pass enumeration).

        **Fail-closed on unknown peak**: build via :func:`build_charge_mask`
        which returns all-False when ``ceiling`` is ``None`` (peak unknown),
        mirroring :func:`~scheduler.peak_price` / :func:`~scheduler.charge_price_ceiling`
        fail-closed semantics.  Passing all-False means no grid charge occurs
        that window, and if the reserve target then becomes unreachable the DP
        still surfaces ``infeasible=True`` via the existing infeasible path —
        the mask never silently swallows a floor or reserve breach.
    feed_in :
        Optional per-hour dynamic feed-in tariff (€/kWh).  Must have exactly
        ``window_len`` elements when provided.

        **Parity invariant (T0.1b)**: ``feed_in=None`` (default) → the DP
        objective is identical to the no-feed_in case.  The T0.1b parity gate
        (``tests/test_optimize_parity.py``) must stay exactly green.  The
        export term is **strictly additive and default-off**.

        **Export-credit term**: when provided, solar surplus that cannot be
        stored (PV exceeds battery headroom or inverter rate) is credited at
        the full ``feed_in[h]`` (effective export price, post-fee) per AC kWh
        exported.  This credit is subtracted from the transition cost in the DP
        objective and from the reported ``eur`` (net cost = import cost −
        export credit).

        The export credit is applied *per DP state* at each hour (same credit
        for all grid-charge levels from a given SoC state), so it can shift
        *which SoC trajectory* the DP prefers without changing the within-hour
        charge amount for a given state.  In Phase 0 (charges only to
        ``soc_target``) the effect on routing is expected to be small.

    Returns
    -------
    dict with keys:

    ``schedule``
        ``list[float]`` — ``window_len`` AC kWh charged from grid per hour.
    ``kwh``
        ``float`` — total AC kWh imported over the window.
    ``eur``
        ``float`` — net cost (€).  When ``feed_in`` is ``None``: pure import
        cost (= ``sum(schedule[h] × price[h])``).  When ``feed_in`` is
        provided: import cost minus the discounted export credit (may be
        negative when export revenue exceeds import cost).
    ``export_credit_eur``
        ``float`` — total discounted export credit (€) along the optimal path.
        Present **only** when ``feed_in`` is not ``None``.
    ``infeasible``
        ``bool`` — present (and ``True``) only when the end-reserve constraint
        could not be met even at maximum charge rate.  The best achievable
        schedule is returned in that case.

    Raises
    ------
    ValueError
        If any of ``window_pv``, ``window_load``, or ``price`` has a length
        different from ``window_len``.
        If ``chargeable`` is not ``None`` and its length differs from
        ``window_len``.
        If ``feed_in`` is not ``None`` and its length differs from
        ``window_len``.
    """
    # ------------------------------------------------------------------
    # Input validation
    # ------------------------------------------------------------------
    if len(window_pv) != window_len:
        raise ValueError(
            f"window_pv length {len(window_pv)} != window_len {window_len}"
        )
    if len(window_load) != window_len:
        raise ValueError(
            f"window_load length {len(window_load)} != window_len {window_len}"
        )
    if len(price) != window_len:
        raise ValueError(
            f"price length {len(price)} != window_len {window_len}"
        )
    if window_len < 1:
        raise ValueError(f"window_len must be >= 1, got {window_len}")
    if chargeable is not None and len(chargeable) != window_len:
        raise ValueError(
            f"chargeable length {len(chargeable)} != window_len {window_len}"
        )
    if feed_in is not None and len(feed_in) != window_len:
        raise ValueError(
            f"feed_in length {len(feed_in)} != window_len {window_len}"
        )
    if export_price is not None and len(export_price) != window_len:
        raise ValueError(
            f"export_price length {len(export_price)} != window_len {window_len}"
        )
    if reserve_by_hour is not None and len(reserve_by_hour) != window_len:
        raise ValueError(
            f"reserve_by_hour length {len(reserve_by_hour)} != window_len {window_len}"
        )
    if grid_charge_ceiling is not None and len(grid_charge_ceiling) != window_len:
        raise ValueError(
            f"grid_charge_ceiling length {len(grid_charge_ceiling)} != window_len {window_len}"
        )
    if hedge_drain_kwh is not None and len(hedge_drain_kwh) != window_len:
        raise ValueError(
            f"hedge_drain_kwh length {len(hedge_drain_kwh)} != window_len {window_len}"
        )

    # ------------------------------------------------------------------
    # Derived constants (mirror hindsight_optimal_grid)
    # ------------------------------------------------------------------
    cap_kwh = cfg.capacity_kwh
    floor_kwh = cfg.soc_floor / 100.0 * cap_kwh
    # Firmware hard discharge floor (const.FIRMWARE_SOC_FLOOR, 5%) — the actual
    # physical wall the pack sags to.  cfg.soc_floor is a pure DECISION margin
    # above this (export floor + terminal water-value credit anchor); it no
    # longer forces a fake physical clamp.  Byte-identical to floor_kwh when
    # cfg.soc_floor == const.FIRMWARE_SOC_FLOOR (the default).
    firmware_floor_kwh = const.FIRMWARE_SOC_FLOOR / 100.0 * cap_kwh
    target_kwh = cfg.soc_target / 100.0 * cap_kwh
    eta = cfg.eta_charge if cfg.eta_charge > 1e-9 else 1.0

    # Export leg pre-computations — all off when export_price is None (parity).
    _do_export = export_price is not None
    if _do_export:
        if eta_curve is None:
            eta_d: float = min(cfg.round_trip_eff / eta, 1.0)
        else:
            eta_d = _eta_discharge_at(cfg.max_export_w, cfg, eta_curve)
        cycle_cost: float = cfg.cycle_cost_eur_per_kwh
        _max_export_ac_slot = cfg.max_export_w / 1000.0 * dt_h
        max_export_dc_h: float = (
            _max_export_ac_slot / eta_d if eta_d > 1e-9 else _max_export_ac_slot
        )
        # Combined AC grid export cap (battery + solar spill must not exceed this).
        # Uses the tighter of the inverter export rating and the grid connection limit.
        ac_cap: float = min(cfg.max_export_w, cfg.grid_export_limit_w) / 1000.0 * dt_h
        # C2: peak-only gate. Export is admitted in hour h only when
        # export_price[h] is within export_peak_band_frac of the windowed peak.
        _band = cfg.export_peak_band_frac
        # Look-back-windowed peak: an hour on the DOWN-SLOPE after a peak is
        # judged against that recent peak (export_peak_lookback_h hours) and
        # blocked by the band, instead of the forward-only suffix-max forgetting
        # it.  lookback=0 reproduces the legacy gate.
        # Day boundaries are derived from slots_per_day arithmetic on
        # window_start_h (== 24h arithmetic at the legacy hourly resolution).
        # On a DST-transition day (23h or 25h wall-clock) the boundary can land
        # one hour off true local midnight; this is benign (one hour, twice/year)
        # and moot when the internal clock is UTC (the common HA default).
        _di = (
            day_index if day_index is not None
            else [(window_start_h + h) // slots_per_day for h in range(len(export_price))]
        )
        peak_from = windowed_peak_prices(
            list(export_price),
            round(cfg.export_peak_lookback_h / dt_h),   # wall-clock lookback -> slots (T8)
            day_index=_di,
        )  # type: ignore[arg-type]
    else:
        eta_d = 1.0
        cycle_cost = 0.0
        max_export_dc_h = 0.0
        ac_cap = 0.0
        _band = 0.0
        peak_from = []

    # C3: solar-only baseline spill (AC kWh) per hour — the spill that occurs with
    # NO grid charging.  The curtailed-solar credit is capped at this baseline so
    # grid-charging cannot mint extra feed-in credit by displacing solar in the
    # capacity-capped pack (solar-first accounting).  Computed only when feed_in is
    # provided; otherwise unused (and the credit is 0 anyway -> parity preserved).
    baseline_spill_ac: list[float] = [0.0] * window_len
    if feed_in is not None:
        _soc_b = soc_start / 100.0 * cap_kwh
        for _h in range(window_len):
            _net_b = window_pv[_h] - window_load[_h]
            _soc_after_b = _apply_solar_load(_soc_b, _net_b, cfg, dt_h, eta_curve=eta_curve)
            if _net_b > 0.0:
                _ac_batt_b = (_soc_after_b - _soc_b) / eta
                baseline_spill_ac[_h] = max(0.0, _net_b - _ac_batt_b)
            _soc_b = _soc_after_b

    bin_kwh = _BIN_KWH
    n_states = round(cap_kwh / bin_kwh) + 1

    # Local binning utilities — same formula as the closures in
    # regret.hindsight_optimal_grid; defined here because they capture
    # n_states/bin_kwh from this scope.
    def to_bin(soc: float) -> int:
        return max(0, min(n_states - 1, round(soc / bin_kwh)))

    def from_bin(b: int) -> float:
        return b * bin_kwh

    INF = float("inf")

    # ------------------------------------------------------------------
    # DP initialisation
    # ------------------------------------------------------------------
    # dp[b] = minimum cost to reach SoC bin b at the current hour boundary.
    dp: list[float] = [INF] * n_states
    init_b = to_bin(soc_start / 100.0 * cap_kwh)
    dp[init_b] = 0.0

    # parent[h][b] = (prev_bin, grid_ac_kwh, export_credit_eur, export_dc_kwh,
    #                 floor_import_eur, floor_import_kwh).
    # export_dc_kwh is 0.0 for all grid-charge / idle transitions (no export).
    # floor_import_eur/kwh are stored from the HEDGED forward pass so the backtrack
    # sums them (no unhedged _apply_solar_load recompute needed; parity safe at hedge=None
    # because the stored values equal the old recompute when no hedge is applied).
    parent: list[list[tuple[int, float, float, float, float, float] | None]] = []

    # ------------------------------------------------------------------
    # Forward DP pass — window_len steps (not hardcoded 24)
    # ------------------------------------------------------------------
    for h in range(window_len):
        dp_next: list[float] = [INF] * n_states
        par_h: list[tuple[int, float, float, float, float, float] | None] = [None] * n_states
        price_h = price[h]
        net = window_pv[h] - window_load[h]

        # Hoist the feed_in lookup and discount product out of the per-state
        # loop — both are invariant within a given hour h.
        fi_h = feed_in[h] if feed_in is not None else 0.0

        for b in range(n_states):
            if dp[b] == INF:
                continue
            cost = dp[b]
            soc = from_bin(b)

            # Solar/load step — physics imported from regret.py (shared).
            soc_after = _apply_solar_load(soc, net, cfg, dt_h, eta_curve=eta_curve)

            # SoC drift-hedge: synthetic forward DC-kWh debit that sags the projected
            # trajectory so the DP books the cheapest in-range recovery (or trims export).
            # SoC-accounting ONLY. None / all-zero → byte-identical (parity exact).
            if hedge_drain_kwh is not None and hedge_drain_kwh[h] > 0.0:
                soc_after = max(0.0, soc_after - hedge_drain_kwh[h])

            # Solar spill: AC kWh that cannot be stored (PV overflow to grid).
            # Computed unconditionally so the export block can use it to cap
            # combined AC feed-in regardless of whether feed_in is provided.
            # When net ≤ 0 there is no surplus → solar_export_ac = 0.
            if net > 0.0:
                delta_soc = soc_after - soc  # DC kWh stored from solar
                ac_for_battery = delta_soc / eta  # AC equivalent absorbed
                solar_export_ac = max(0.0, net - ac_for_battery)
            else:
                solar_export_ac = 0.0

            # Export-credit term (T0.2c) — additive, default-off.
            # When feed_in is provided, credit solar surplus that cannot be
            # stored at the full effective feed-in price (no haircut).
            # feed_in=None → fi_h=0.0 → hour_credit=0.0 → objective is
            # IDENTICAL to before (T0.1b parity invariant preserved exactly).
            # Decision-neutral spill credit: a per-hour CONSTANT (independent of
            # this state's SoC).  The old min(solar_export_ac, baseline_spill_ac[h])
            # scaled with SoC, which penalised EXPORT (exporting lowers SoC -> the
            # next sunny day absorbs more solar -> less spill -> smaller credit) and
            # made the DP HOLD the first day's surplus for a higher next-day peak
            # even when next-day solar refills the pack and that surplus is
            # redundant (it just curtails / cheaply-spills tomorrow's free solar).
            # A constant offset is the same for every battery state at hour h, so it
            # cannot change the DP argmin -> charge/export are decided on real
            # economics (forecast-conditional via the solar/load/price/reserve
            # inputs: sell the first-day surplus when solar will refill, HOLD when
            # the next day is low-solar and the higher peak is the only way to
            # capture the energy).  Anti-minting is preserved: the credit is pinned
            # to the no-grid-charge baseline, so grid-charging cannot inflate it.
            # feed_in=None -> fi_h=0.0 -> hour_credit=0.0 (T0.1b byte parity exact).
            # NOTE: solar_export_ac is still computed per-state above — it remains in
            # use for the combined-AC export cap (batt_ac_headroom) below; only this
            # CREDIT term becomes constant.
            hour_credit = baseline_spill_ac[h] * fi_h

            # Feasible DC grid charge range for this hour — computed on the RAW
            # soc_after, BEFORE the floor clamp below.  This ordering is
            # load-bearing for the T0.1b no-op invariant: when the floor never
            # binds (soc_after >= floor) the clamp is a no-op and max_grid_dc /
            # solar_export_ac / hour_credit are byte-identical to the pre-fix
            # path.  In the drain case (net<0) the clamp only RAISES soc_after,
            # which these raw-based quantities deliberately do not see.
            max_grid_dc = _max_grid_dc(soc, soc_after, cfg, dt_h, eta_curve=eta_curve)
            # Action mask: block grid charge when this hour is not chargeable.
            # g_dc=0 (no-charge transition) is still evaluated so the battery
            # can discharge naturally; only g_dc>0 steps are suppressed.
            if chargeable is not None and not chargeable[h]:
                max_grid_dc = 0.0
            if grid_charge_ceiling is not None:
                max_grid_dc = min(max_grid_dc, max(0.0, grid_charge_ceiling[h] - soc_after))

            # Economic-only floor (A1) aligned to regret.realized_grid_cost (A5):
            # the grid charge is applied FIRST (on the raw, possibly below-floor
            # soc_after) and only the REMAINING below-floor house load is served
            # by direct grid->load import (priced 1:1, NO eta and NO fee — matches
            # regret.realized_grid_cost's order of operations).  This REPLACES the
            # old "force a grid charge to hold the floor" prune.  soc_after is NOT
            # pre-clamped: the floor import is computed per-step on the POST-charge
            # SoC (soc_after + g_dc), so the DP optimises the TRUE grid bill
            # (regret ~= 0).  When soc_after >= firmware_floor the import is 0
            # for every step and new_soc == soc_after + g_dc (floor never
            # binds => byte parity preserved).
            n_steps = round(max_grid_dc / bin_kwh)

            for step in range(n_steps + 1):
                g_dc = step * bin_kwh
                if g_dc > max_grid_dc + 1e-9:
                    break  # monotonically increasing — safe to stop

                new_soc_pre = soc_after + g_dc
                # Floor import on the POST-charge SoC: the charge offsets the
                # below-firmware-floor deficit first; only the remainder is a
                # priced grid->load import.  Booked only where it physically
                # occurs — below const.FIRMWARE_SOC_FLOOR — not below the
                # soft cfg.soc_floor margin.  Zero whenever new_soc_pre >=
                # firmware_floor_kwh.
                floor_import_cost = max(0.0, firmware_floor_kwh - new_soc_pre) * price_h
                new_soc = max(new_soc_pre, firmware_floor_kwh)
                new_b = to_bin(new_soc)
                eta_g = (
                    eta if eta_curve is None
                    else _eta_charge_at(g_dc / dt_h * 1000.0, cfg, eta_curve)
                )
                g_ac = g_dc / eta_g
                # Subtract export credit from transition cost; add the below-floor
                # direct-import cost.  When feed_in=None, hour_credit=0.0; when the
                # floor does not bind, floor_import_cost=0.0 -> new_cost unchanged.
                new_cost = (
                    cost + g_ac * price_h - hour_credit + floor_import_cost
                    + g_dc * cfg.charge_margin_eur_per_kwh
                )

                # Strict < mirrors hindsight tie-breaking — do not change to <=
                # (the T0.1b parity gate depends on identical tie resolution).
                if new_cost < dp_next[new_b]:
                    dp_next[new_b] = new_cost
                    # Store hour_credit and floor-import (eur + kwh) in the parent tuple so
                    # backtracking can accumulate the exact values the DP used for routing,
                    # ensuring reported eur == best_cost exactly — even when hedge_drain_kwh
                    # sags a state below floor (MAJOR-1: no unhedged recompute in backtrack).
                    floor_import_kwh_step = max(0.0, firmware_floor_kwh - new_soc_pre)
                    par_h[new_b] = (b, g_ac, hour_credit, 0.0, floor_import_cost, floor_import_kwh_step)

            # Action class B: export discharge (e_dc > 0). Mutually exclusive with
            # grid charge (g_dc=0). Ported from regret.hindsight_optimal_grid:347-377.
            if _do_export:
                ep_h = export_price[h]  # type: ignore[index]
                # Floor import on the (raw) post-load SoC; 0 whenever export is
                # admissible (export requires soc_after > export_floor_h >= floor),
                # so this never changes a real export transition.
                floor_import_export = max(0.0, floor_kwh - soc_after) * price_h
                net_rev_per_dc = ep_h * eta_d - cycle_cost
                # C1: per-hour ride-out floor — voluntary export may not take SoC
                # below the reserve sized to the next solar pickup.  Defaults to the
                # firmware floor when reserve_by_hour is None (byte parity preserved).
                export_floor_h = (
                    floor_kwh if reserve_by_hour is None
                    else max(floor_kwh, reserve_by_hour[h])
                )
                band_floor = peak_from[h] * (1.0 - _band)
                if (
                    net_rev_per_dc > 1e-9
                    and ep_h >= band_floor - 1e-9
                    and soc_after > export_floor_h + bin_kwh - 1e-9
                ):
                    export_headroom_dc = max(0.0, soc_after - export_floor_h)
                    # Cap battery export so combined AC feed-in (solar spill + battery)
                    # does not exceed ac_cap = min(max_export_w, grid_export_limit_w)/1000.
                    # solar_export_ac is state-specific (varies with soc before solar step).
                    batt_ac_headroom = max(0.0, ac_cap - solar_export_ac)
                    batt_dc_cap = batt_ac_headroom / eta_d if eta_d > 1e-9 else batt_ac_headroom
                    max_e_dc = min(export_headroom_dc, max_export_dc_h, batt_dc_cap)
                    n_exp_steps = round(max_e_dc / bin_kwh)
                    for exp_step in range(1, n_exp_steps + 1):  # start at 1: e_dc > 0
                        e_dc = exp_step * bin_kwh
                        if e_dc > max_e_dc + 1e-9:
                            break
                        new_soc = soc_after - e_dc
                        if new_soc < export_floor_h - 1e-9:
                            break  # monotonically discharging — safe to stop
                        new_b = to_bin(new_soc)
                        eta_d_step = (
                            eta_d if eta_curve is None
                            else _eta_discharge_at(e_dc / dt_h * 1000.0, cfg, eta_curve)
                        )
                        e_ac = e_dc * eta_d_step
                        export_revenue = e_ac * ep_h
                        degradation = e_dc * cycle_cost
                        # Cost decreases by net export revenue; hour_credit is 0 when
                        # feed_in is None (preserves byte parity with the oracle).
                        # floor_import_export is computed on the raw soc_after for
                        # this hour; it is 0 whenever export is admissible
                        # (export requires soc_after > export_floor_h >= floor).
                        new_cost = cost - (export_revenue - degradation) - hour_credit + floor_import_export
                        if new_cost < dp_next[new_b]:
                            dp_next[new_b] = new_cost
                            # Export requires soc_after > export_floor_h >= floor, so
                            # floor-import is always 0 for export transitions.
                            par_h[new_b] = (b, 0.0, hour_credit, e_dc, 0.0, 0.0)

        parent.append(par_h)
        dp = dp_next

    # ------------------------------------------------------------------
    # Select best end state
    # ------------------------------------------------------------------
    if terminal_mode == "water_value":
        # Price end-SoC by the trough water value instead of forcing the reserve.
        # The per-transition floor constraint already guarantees every reachable
        # end state has SoC >= floor, so survival-floor unreachability is NOT
        # flagged here (it is handled in the shield path, Task C2a). Only the
        # all-pruned pathological case below sets infeasible.
        v = water_value if water_value is not None else 0.0
        # Scan from the firmware floor (widened): sub-soft-floor end states are
        # now reachable (transition clamp sags to firmware_floor_kwh, not
        # floor_kwh).  The credit anchor stays at floor_kwh (soft margin) below
        # so those sub-margin states simply earn zero credit, not a penalty.
        floor_b = to_bin(firmware_floor_kwh)
        target_b = to_bin(target_kwh)
        best_score = INF
        best_end_b = -1
        for end_b in range(floor_b, target_b + 1):
            if dp[end_b] == INF:
                continue
            credit = max(0.0, from_bin(end_b) - floor_kwh) * v
            score = dp[end_b] - credit
            if score < best_score:
                best_score = score
                best_end_b = end_b
        # Fallback: soc_start > soc_target means [floor_b, target_b] states are
        # unreachable via charging. Scan the full [floor_b, n_states) range to
        # find the lowest net-cost reachable state (still keeping infeasible=False,
        # because the survival floor is satisfied — battery is above target).
        if best_end_b == -1:
            best_score = INF
            for end_b in range(floor_b, n_states):
                if dp[end_b] == INF:
                    continue
                credit = max(0.0, from_bin(end_b) - floor_kwh) * v
                score = dp[end_b] - credit
                if score < best_score:
                    best_score = score
                    best_end_b = end_b
        best_cost = dp[best_end_b] if best_end_b != -1 else INF
        infeasible = False
    else:
        # Reserve mode (default) — UNCHANGED from the original implementation.
        reserve_b = to_bin(target_kwh)
        best_cost = INF
        best_end_b = -1
        for end_b in range(reserve_b, n_states):
            if dp[end_b] < best_cost:
                best_cost = dp[end_b]
                best_end_b = end_b

        infeasible = best_end_b == -1
        if infeasible:
            # Reserve unreachable: return best achievable end state.
            for end_b in range(n_states - 1, -1, -1):
                if dp[end_b] < INF:
                    best_end_b = end_b
                    best_cost = dp[end_b]
                    break

    if best_end_b == -1:
        # All paths blocked by floor (extremely pathological input).
        result: dict = {
            "schedule": [0.0] * window_len,
            "kwh": 0.0,
            "eur": 0.0,
            "infeasible": True,
        }
        if feed_in is not None:
            result["export_credit_eur"] = 0.0
        if _do_export:
            result["export_schedule"] = [0.0] * window_len
            result["export_kwh"] = 0.0
            result["export_revenue_eur"] = 0.0
        return result

    # ------------------------------------------------------------------
    # Backtrack parent pointers → per-hour AC schedule + export credit
    # ------------------------------------------------------------------
    # The parent tuple is (prev_bin, g_ac, hour_credit, export_dc_kwh,
    #                      floor_import_eur, floor_import_kwh).
    # floor_import_eur/kwh are stored from the HEDGED forward pass — summing them
    # keeps reported eur == best_cost exactly even when hedge_drain_kwh sags a state
    # below floor (MAJOR-1). At hedge=None they equal the old unhedged recompute
    # (forward soc_after == backtrack soc_after_h when no hedge is applied).
    schedule_ac: list[float] = [0.0] * window_len
    export_dc_sched: list[float] = [0.0] * window_len
    export_credit = 0.0
    floor_import_eur = 0.0
    # M1: track the below-floor direct-import VOLUME alongside its cost so the
    # returned total_kwh folds it in — mirroring regret.realized_grid_cost, whose
    # kwh already includes its forced floor imports.  Without this the oracle kwh
    # under-reports by the floor-import volume and regret.score_regret over-reports
    # a phantom over_buy on every drain-to-floor day.  Zero whenever the floor
    # never binds (byte parity with the pre-M1 total_kwh preserved exactly).
    floor_import_kwh = 0.0
    cur_b = best_end_b
    for h in range(window_len - 1, -1, -1):
        info = parent[h][cur_b]
        if info is None:
            break  # should not occur for a reachable end state
        prev_b, g_ac, step_credit, e_dc, fi_eur, fi_kwh = info
        schedule_ac[h] = g_ac
        export_dc_sched[h] = e_dc
        export_credit += step_credit
        floor_import_eur += fi_eur     # stored from the HEDGED forward pass (was: unhedged recompute)
        floor_import_kwh += fi_kwh
        cur_b = prev_b

    total_kwh = sum(schedule_ac) + floor_import_kwh
    import_eur = sum(schedule_ac[h] * price[h] for h in range(window_len))

    if _do_export:
        export_schedule_ac = [
            e_dc * (
                eta_d if eta_curve is None
                else _eta_discharge_at(e_dc / dt_h * 1000.0, cfg, eta_curve)
            )
            for e_dc in export_dc_sched
        ]
        total_export_kwh = sum(export_schedule_ac)
        gross_export_rev = sum(export_schedule_ac[h] * export_price[h] for h in range(window_len))  # type: ignore[index]
        net_export_rev = gross_export_rev - sum(export_dc_sched) * cycle_cost
    else:
        export_schedule_ac = [0.0] * window_len
        total_export_kwh = 0.0
        net_export_rev = 0.0

    # charge_margin_eur_per_kwh is a ROUTING-ONLY hurdle: it steers the DP argmin
    # (added to the per-step transition cost above) but is deliberately NOT billed
    # into the reported eur.  eur must stay actual grid cash so it does not bias the
    # regret score / HGBR promotion gate (realized_grid_cost carries no synthetic
    # margin).  So eur == best_cost - Σ g_dc*margin, and forward/oracle eur parity
    # still holds byte-exactly (both reconstruct the same margin-free cash).
    total_eur = import_eur - export_credit - net_export_rev + floor_import_eur
    result = {"schedule": schedule_ac, "kwh": total_kwh, "eur": total_eur}
    if feed_in is not None:
        result["export_credit_eur"] = export_credit
    if _do_export:
        result["export_schedule"] = export_schedule_ac
        result["export_kwh"] = total_export_kwh
        result["export_revenue_eur"] = net_export_rev
    if infeasible:
        result["infeasible"] = True
    return result
