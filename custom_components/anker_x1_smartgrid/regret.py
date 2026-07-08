"""Pure regret scoring for completed grid-charge days.

No Home Assistant imports — only stdlib and the project's own models.
All functions are pure (no side effects, no I/O).

Discharge-model note  (INTENTIONAL divergence from energy.py)
-------------------------------------------------------------
energy.py's simulate_soc is a *charge-only* model: it processes solar
surplus only and has no discharge term.  regret.py models the FULL day:
solar surplus charges (same eta_charge and rate conventions as energy.py),
and load deficit discharges the battery 1 DC kWh per 1 AC kWh consumed
(no discharge efficiency factor).  This is a *deliberate* hindsight-
modelling choice — the regret module needs to trace the battery level
through the whole day, including load-driven discharge, to find where
the battery would have breached the floor.  Callers (A3b and beyond)
must supply AC-side pv/load values consistent with this
no-discharge-eta assumption.

Public API
----------
DayData                  — frozen dataclass carrying per-hour day observations
hindsight_optimal_grid   — minimum-cost grid schedule in hindsight (DP-exact)
realized_grid_cost       — battery sim of realized schedule + forced floor imports
score_regret             — regret decomposition vs optimal
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import Config

# Discretisation resolution for the SoC DP (kWh per bin).
# 0.05 kWh → ≤ 201 states per 10 kWh battery; exact for inputs that are
# multiples of 0.05 kWh (e.g. eta=1.0 with load/PV in 0.5 kWh steps).
_BIN_KWH: float = 0.05


# ---------------------------------------------------------------------------
# Input type
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DayData:
    """Per-slot observations for a completed day (24 at 60-min, 96 at 15-min).

    Attributes
    ----------
    pv_kwh     : PV energy produced per hour (kWh, AC side).
    load_kwh   : House load per hour (kWh, AC side).
    price      : All-in electricity price per hour (€/kWh).
    soc_start  : Battery SoC at the very start of the day (%).
    """

    pv_kwh: tuple[float, ...]    # length 24 (60-min) / 96 (15-min)
    load_kwh: tuple[float, ...]  # length 24 (60-min) / 96 (15-min)
    price: tuple[float, ...]     # length 24 (60-min) / 96 (15-min)
    soc_start: float             # %


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _validate_day_len(seq: tuple | list, name: str, expected: int) -> None:
    """Raise ValueError if *seq* does not have exactly *expected* elements."""
    if len(seq) != expected:
        raise ValueError(
            f"{name} must have exactly {expected} elements, got {len(seq)}"
        )


def _eta_charge_at(dc_power_w: float, cfg: Config, eta_curve) -> float:
    """Charge-side eta at *dc_power_w* — measured curve if provided, else the
    static config scalar (eta_curve=None keeps today's behaviour)."""
    if eta_curve is not None:
        return eta_curve.eta_charge(dc_power_w)
    return cfg.eta_charge if cfg.eta_charge > 1e-9 else 1.0


def _eta_discharge_at(dc_power_w: float, cfg: Config, eta_curve) -> float:
    """Discharge-side eta at *dc_power_w* — measured curve if provided, else
    the static round-trip-derived scalar (eta_curve=None keeps today's
    behaviour)."""
    if eta_curve is not None:
        return eta_curve.eta_discharge(dc_power_w)
    eta_c = cfg.eta_charge if cfg.eta_charge > 1e-9 else 1.0
    return min(cfg.round_trip_eff / eta_c, 1.0)


def _apply_solar_load(
    soc_kwh: float,
    net_kwh: float,
    cfg: Config,
    dt_h: float = 1.0,
    *,
    eta_curve=None,
) -> float:
    """Apply one hour of solar surplus or load discharge to battery SoC.

    Shared by hindsight_optimal_grid and realized_grid_cost so the two sims
    stay in sync.

    net_kwh > 0  — solar surplus charges battery:
                   DC = min(net, rate_kwh_h) × eta, capped at soc_target.
    net_kwh ≤ 0  — load discharges battery. When eta_curve is None (default)
                   this is 1:1 (no discharge eta) — the byte-identical parity
                   path. When eta_curve is supplied, load-driven discharge
                   bears eta_d: DC draw = AC deficit / eta_discharge(power).

    Parameters
    ----------
    soc_kwh   : current SoC in kWh (DC side).
    net_kwh   : pv_kwh[h] − load_kwh[h] for the current hour.
    cfg       : battery / system parameters.
    eta_curve : optional EfficiencyCurve — None preserves parity exactly.

    Returns
    -------
    New SoC in kWh after the solar/load step (before any grid charge).
    May be below soc_floor (callers must handle the floor constraint).
    """
    target_kwh = cfg.soc_target / 100.0 * cfg.capacity_kwh
    rate_kwh_slot = cfg.max_charge_w / 1000.0 * dt_h
    if net_kwh > 0.0:
        eta = _eta_charge_at(net_kwh / dt_h * 1000.0, cfg, eta_curve)
        dc_solar = min(net_kwh, rate_kwh_slot) * eta
        return min(soc_kwh + dc_solar, target_kwh)
    if eta_curve is None:
        return soc_kwh + net_kwh  # discharge 1:1, no efficiency factor — PARITY PATH, do not change
    ac_deficit = -net_kwh
    eta_d = _eta_discharge_at(ac_deficit / dt_h * 1000.0, cfg, eta_curve)
    dc_draw = ac_deficit / eta_d
    return soc_kwh - dc_draw


def _max_grid_dc(
    soc_kwh: float,
    soc_after_kwh: float,
    cfg: Config,
    dt_h: float = 1.0,
    *,
    eta_curve=None,
) -> float:
    """Max DC grid charge for one hour under the SHARED inverter rate cap.

    The inverter setpoint is a single TOTAL battery-charge target: solar is
    consumed first, grid imports only the remainder.  So the grid ceiling is
    the AC charge rate left after this hour's solar charging, converted to DC,
    and further bounded by headroom to soc_target.

    soc_kwh / soc_after_kwh are DC kWh before / after the solar-load step
    (i.e. soc_after_kwh = _apply_solar_load(soc_kwh, net, cfg)).

    Grid charge is always full-rate, so eta is looked up at cfg.max_charge_w.
    """
    rate_kwh_slot = cfg.max_charge_w / 1000.0 * dt_h
    eta = _eta_charge_at(cfg.max_charge_w, cfg, eta_curve)
    target_kwh = cfg.soc_target / 100.0 * cfg.capacity_kwh
    solar_ac_used = max(0.0, soc_after_kwh - soc_kwh) / eta  # AC absorbed by solar
    remaining_ac = max(0.0, rate_kwh_slot - solar_ac_used)
    return max(0.0, min(remaining_ac * eta, target_kwh - soc_after_kwh))


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------

def windowed_peak_prices(
    prices: list[float],
    lookback: int,
    day_index: list[int] | None = None,
) -> list[float]:
    """Per-hour peak reference for the export band.

    Returns ``peak[h] = max(prices[max(0, h - lookback):])`` (when
    ``day_index`` is None) — the forward suffix-max extended ``lookback``
    hours into the recent past, so a DOWN-SLOPE hour after a peak is measured
    against that peak (and blocked by the band) instead of forgetting it.
    ``lookback=0`` reproduces the legacy forward-only suffix-max byte-for-byte.

    When ``day_index`` is given (same length as ``prices``, non-decreasing day
    ids), the suffix is computed WITHIN each day only and the look-back never
    reaches into a prior day, so a higher peak on a later day does not suppress
    an earlier day's band.  ``None`` = single day = legacy global behavior.
    """
    n = len(prices)
    if n == 0:
        return []
    if day_index is None:
        day_index = [0] * n
    suffix = [0.0] * n
    run = float("-inf")
    for h in range(n - 1, -1, -1):
        if h == n - 1 or day_index[h] != day_index[h + 1]:
            run = prices[h]                      # reset suffix at the day boundary
        else:
            run = max(run, prices[h])
        suffix[h] = run
    day_start = [0] * n
    for h in range(n):
        day_start[h] = h if (h == 0 or day_index[h] != day_index[h - 1]) else day_start[h - 1]
    return [suffix[max(day_start[h], h - lookback)] for h in range(n)]


def windowed_trough_prices(
    prices: list[float],
    lookback: int,
    day_index: list[int] | None = None,
) -> list[float]:
    """Per-hour trough reference for the charge band.

    Returns ``trough[h] = min(prices[max(0, h - lookback):])`` (when
    ``day_index`` is None) — the forward suffix-min extended ``lookback``
    hours into the recent past, so an UP-SLOPE hour after a trough is measured
    against that trough (and blocked by the band) instead of forgetting it.
    ``lookback=0`` reproduces the forward-only suffix-min.  Mirror of
    :func:`windowed_peak_prices` (suffix-MAX).

    When ``day_index`` is given (same length as ``prices``, non-decreasing day
    ids), the suffix is computed WITHIN each day only and the look-back never
    reaches into a prior day, so a cheaper trough on a later day does not
    suppress an earlier day's band.  ``None`` = single day = legacy global.
    """
    n = len(prices)
    if n == 0:
        return []
    if day_index is None:
        day_index = [0] * n
    suffix = [0.0] * n
    run = float("inf")
    for h in range(n - 1, -1, -1):
        if h == n - 1 or day_index[h] != day_index[h + 1]:
            run = prices[h]                      # reset suffix at the day boundary
        else:
            run = min(run, prices[h])
        suffix[h] = run
    day_start = [0] * n
    for h in range(n):
        day_start[h] = h if (h == 0 or day_index[h] != day_index[h - 1]) else day_start[h - 1]
    return [suffix[max(day_start[h], h - lookback)] for h in range(n)]


def hindsight_optimal_grid(
    day: DayData,
    cfg: Config,
    *,
    terminal_mode: str = "reserve",
    water_value: float | None = None,
    export_price: tuple[float, ...] | list[float] | None = None,
    reserve_by_hour: list[float] | tuple[float, ...] | None = None,
    grid_charge_ceiling: list[float] | None = None,
    dt_h: float = 1.0,
    eta_curve=None,
) -> dict:
    """Minimum-cost grid charging schedule for a completed day (DP-exact).

    Preconditions
    -------------
    All three DayData arrays (pv_kwh, load_kwh, price) must have equal length
    n ≥ 1.  ValueError is raised otherwise.  (Previously required exactly 24
    elements; now length-agnostic so sub-day windows can be scored.)

    Parameters
    ----------
    terminal_mode : "reserve" (default) or "water_value".
        "reserve"     — select minimum-cost end state with SoC ≥ soc_target.
                        Keeps existing 24h callers byte-for-byte.
        "water_value" — select end state that minimises (cost − credit) where
                        credit = max(0, soc_above_floor) × water_value.
                        Mirrors optimize_grid's water-value branch (M2 fix
                        applied: falls back to lowest-net-cost reachable state
                        when soc_start > soc_target).
    water_value : float | None — value of stored energy (€/kWh AC).  Used
        only when terminal_mode="water_value"; treated as 0.0 if None.

    Parameters
    ----------
    export_price : tuple or list of length n, or None (default).
        Per-hour feed-in tariff (€/AC kWh).  When provided and the per-hour
        export hurdle clears (``export_price[h] × eta_discharge(cfg) −
        cfg.cycle_cost_eur_per_kwh > 0``), the oracle may discharge stored
        energy to the grid.  Export is modelled as a separate action class
        (mutually exclusive with grid charge in the same hour): SoC decreases
        by the DC kWh exported, and ``eur`` is reduced by the AC export
        revenue.

        **Parity invariant**: ``export_price=None`` (default) → the DP is
        byte-identical to the no-export path.  Existing charge-only callers
        are completely unaffected.

        **No-op guard**: export is skipped for any hour where the net export
        revenue per DC kWh (``price_h × eta_d − cycle_cost``) is ≤ 0.

        **Floor safety**: export is bounded by ``soc_after − soc_floor`` so
        the battery never drops below the survival floor during a discharge.

        **Inverter rate**: export is also bounded by ``max_export_w / 1000 ×
        eta_discharge(cfg)`` DC kWh per hour.

    Returns
    -------
    dict with keys:
        schedule           : list[float] — n AC kWh charged from grid per hour.
        kwh                : float — total AC kWh imported.
        eur                : float — net cost at the day's actual prices
                             (import cost minus export revenue; may be negative
                             when export revenue exceeds import cost).
        export_kwh         : float — total AC kWh exported (0.0 when
                             ``export_price`` is None or hurdle never clears).
        export_revenue_eur : float — total export revenue (€) along the
                             optimal path (0.0 when no export).
        export_schedule    : list[float] — n AC kWh exported per hour.
        infeasible  : bool (present only when True) — reserve constraint could
                      not be met even at maximum charge rate; best achievable
                      schedule is returned instead.

    Algorithm — forward SoC dynamic programming
    --------------------------------------------
    State = battery SoC (kWh), discretised into bins of _BIN_KWH width.
    At each of the n hourly steps, for every reachable SoC state:

      1. Apply solar/load contribution (matching energy.py conventions):
           if net > 0:  dc_solar = min(net, rate) × eta;  SoC capped at target.
           if net < 0:  discharge 1 kWh AC → 1 kWh DC (no discharge eta).

      2a. Enumerate feasible grid charges g_dc in [0, max_grid_dc] in steps
          of _BIN_KWH, where max_grid_dc = min((rate − solar_ac) × eta, target − soc_after).
          Transition cost = (g_dc / eta) × price[h] − export_credit_h.
          (g_dc = 0 covers the idle action and is always included.)

      2b. When export_price is provided and per-hour net revenue > 0:
          Enumerate export DC kWh e_dc in [_BIN_KWH, max_export_dc] in steps
          of _BIN_KWH, where max_export_dc = min(soc_after − soc_floor,
          max_export_w/1000 × eta_discharge(cfg)).  Export and grid charge are
          MUTUALLY EXCLUSIVE per hour (g_dc = 0 for all export transitions).
          Transition cost = −(e_dc × export_price_h × eta_discharge(cfg) −
          e_dc × cycle_cost_eur_per_kwh).

      3. Feasibility per transition:
           * new_soc ≥ soc_floor  (floor enforced every boundary)
           * new_soc ≤ soc_target (guaranteed by max_grid_dc formula)

    After all n hours, select the minimum-cost end state per terminal_mode.
    Backtrack parent pointers for schedule.

    Feasibility guarantee
    ---------------------
    Every returned schedule is self-consistent: per-hour AC grid ≤ rate,
    SoC stays in [floor, cap] at every boundary, end SoC ≥ reserve (unless
    infeasible=True).  No energy that cannot be stored is ever assigned.

    Discharge model (see module-level note)
    ----------------------------------------
    Load discharges the battery 1:1 (no discharge eta), intentionally
    diverging from energy.py which is charge-only.  See module docstring.

    Assumptions
    -----------
    * Grid and solar SHARE the inverter rate cap: grid charge per hour is
      bounded by (max_charge_w − solar absorbed) — see _max_grid_dc.
    * eta_charge applied on AC-to-DC charge path only (matches energy.py).
    * Export and grid-charge are mutually exclusive per hour (action B
      forces g_dc=0; action A enumerates g_dc≥0 with no export).
    * Timing: energy charged in hour h available from end of hour h onward.
    * Discretisation error ≤ _BIN_KWH per boundary (negligible for inputs
      that are multiples of _BIN_KWH, such as test cases with eta=1.0).
    """
    n = len(day.pv_kwh)
    if len(day.load_kwh) != n or len(day.price) != n:
        raise ValueError(
            "day.pv_kwh, day.load_kwh and day.price must have equal length; "
            f"got {n}, {len(day.load_kwh)}, {len(day.price)}"
        )
    if n < 1:
        raise ValueError(f"DayData must have at least 1 hour, got {n}")
    if export_price is not None and len(export_price) != n:
        raise ValueError(
            f"export_price length {len(export_price)} != day length {n}"
        )
    if reserve_by_hour is not None and len(reserve_by_hour) != n:
        raise ValueError(
            f"reserve_by_hour length {len(reserve_by_hour)} != day length {n}"
        )
    if grid_charge_ceiling is not None and len(grid_charge_ceiling) != n:
        raise ValueError(
            f"grid_charge_ceiling length {len(grid_charge_ceiling)} != day length {n}"
        )

    cap_kwh = cfg.capacity_kwh
    floor_kwh = cfg.soc_floor / 100.0 * cap_kwh
    target_kwh = cfg.soc_target / 100.0 * cap_kwh
    eta = cfg.eta_charge if cfg.eta_charge > 1e-9 else 1.0

    # Export leg pre-computations (F1).  All zero / off when export_price is None.
    _do_export = export_price is not None
    if _do_export:
        # Discharge efficiency: eta_d = min(round_trip_eff / eta_charge, 1.0)
        # on the None path (reuses `eta`, already computed above with the same
        # zero-guard); looked up on the curve at the max export rate otherwise.
        if eta_curve is None:
            eta_d: float = min(cfg.round_trip_eff / eta, 1.0)
        else:
            eta_d = _eta_discharge_at(cfg.max_export_w, cfg, eta_curve)
        cycle_cost: float = cfg.cycle_cost_eur_per_kwh
        # Max DC kWh per hour that can be exported through the inverter.
        # max_export_w is the AC-side export rate cap; DC discharged = AC / eta_d.
        max_export_ac_h: float = cfg.max_export_w / 1000.0 * dt_h
        max_export_dc_h: float = max_export_ac_h / eta_d if eta_d > 1e-9 else max_export_ac_h
        # Combined AC grid export cap (battery + solar spill must not exceed this).
        # Mirrors optimize.optimize_grid:510 — tighter of inverter rating + grid limit.
        ac_cap: float = min(cfg.max_export_w, cfg.grid_export_limit_w) / 1000.0 * dt_h
        _band = cfg.export_peak_band_frac
        peak_from = windowed_peak_prices(list(export_price), round(cfg.export_peak_lookback_h / dt_h))  # type: ignore[arg-type]
    else:
        eta_d = 1.0
        cycle_cost = 0.0
        max_export_dc_h = 0.0
        ac_cap = 0.0
        _band = 0.0
        peak_from = []

    bin_kwh = _BIN_KWH
    n_states = round(cap_kwh / bin_kwh) + 1

    def to_bin(soc: float) -> int:
        return max(0, min(n_states - 1, round(soc / bin_kwh)))

    def from_bin(b: int) -> float:
        return b * bin_kwh

    INF = float("inf")

    # dp[b] = minimum cost to reach SoC bin b at current hour boundary.
    dp: list[float] = [INF] * n_states
    init_b = to_bin(day.soc_start / 100.0 * cap_kwh)
    dp[init_b] = 0.0

    # parent[h][b] = (prev_bin, grid_ac_kwh, export_dc_kwh) for path reconstruction.
    # export_dc_kwh is 0.0 for all grid-charge / idle transitions (no export).
    parent: list[list[tuple[int, float, float] | None]] = []

    for h in range(n):
        dp_next: list[float] = [INF] * n_states
        par_h: list[tuple[int, float, float] | None] = [None] * n_states
        price_h = day.price[h]
        net = day.pv_kwh[h] - day.load_kwh[h]

        for b in range(n_states):
            if dp[b] == INF:
                continue
            cost = dp[b]
            soc = from_bin(b)

            # Solar/load contribution — shared conventions via _apply_solar_load.
            # eta_curve=None (default) is the byte-identical parity path.
            soc_after = _apply_solar_load(soc, net, cfg, dt_h, eta_curve=eta_curve)

            # Solar spill (AC kWh) that cannot be stored — mirror of
            # optimize.optimize_grid:603-608.  Used by the export leg's
            # combined-AC cap below.  net <= 0 → no surplus → 0.0.
            if net > 0.0:
                delta_soc = soc_after - soc            # DC kWh stored from solar
                ac_for_battery = delta_soc / eta       # AC equivalent absorbed
                solar_export_ac = max(0.0, net - ac_for_battery)
            else:
                solar_export_ac = 0.0

            # ----------------------------------------------------------------
            # Action class A: grid charge (g_dc ≥ 0) — identical to pre-F1.
            # g_dc=0 covers the idle action.  Export = 0 for all these steps.
            # ----------------------------------------------------------------
            max_grid_dc = _max_grid_dc(soc, soc_after, cfg, dt_h, eta_curve=eta_curve)
            if grid_charge_ceiling is not None:
                max_grid_dc = min(max_grid_dc, max(0.0, grid_charge_ceiling[h] - soc_after))

            # Economic-only floor (A1) — MUST mirror optimize.optimize_grid
            # byte-for-byte so the T0.1b parity gate stays exact.  Aligned to
            # regret.realized_grid_cost (A5): the grid charge is applied FIRST
            # (on the raw, possibly below-floor soc_after) and only the REMAINING
            # below-floor load is served by direct grid->load import (priced 1:1,
            # no eta, no fee).  soc_after is NOT pre-clamped; the floor import is
            # computed per-step on the POST-charge SoC (soc_after + g_dc).  This
            # mirrors realized_grid_cost's order of operations exactly so the DP
            # optimises the TRUE grid bill (regret ~= 0).  When soc_after >= floor
            # the import is 0 for every step and new_soc == soc_after + g_dc
            # (floor never binds => byte parity preserved).
            #
            # L1: this economic model assumes cfg.soc_floor == the firmware hard
            # discharge floor (5%).  Raising soc_floor above 5 would over-price
            # load in the [5%, soc_floor] band, because nothing force-charges to
            # hold that band anymore — the DP would bill it as direct grid->load
            # import that never actually occurs at the firmware level.
            n_steps = round(max_grid_dc / bin_kwh)

            for step in range(n_steps + 1):
                g_dc = step * bin_kwh
                if g_dc > max_grid_dc + 1e-9:
                    break  # g_dc is monotonically increasing; safe to stop

                new_soc_pre = soc_after + g_dc
                # Floor import on the POST-charge SoC: the charge offsets the
                # below-floor deficit first; only the remainder is a priced
                # grid->load import.  Zero whenever new_soc_pre >= floor.
                floor_import_cost = max(0.0, floor_kwh - new_soc_pre) * price_h
                new_soc = max(new_soc_pre, floor_kwh)
                new_b = to_bin(new_soc)
                eta_g = (
                    eta if eta_curve is None
                    else _eta_charge_at(g_dc / dt_h * 1000.0, cfg, eta_curve)
                )
                g_ac = g_dc / eta_g
                new_cost = (
                    cost + g_ac * price_h + floor_import_cost
                    + g_dc * cfg.charge_margin_eur_per_kwh
                )

                if new_cost < dp_next[new_b]:
                    dp_next[new_b] = new_cost
                    par_h[new_b] = (b, g_ac, 0.0)  # export_dc=0 for charge/idle

            # ----------------------------------------------------------------
            # Action class B: export discharge (e_dc > 0) — F1 addition.
            # Mutually exclusive with grid charge (no import while exporting).
            # Only enumerated when export_price is provided and hurdle clears.
            # ----------------------------------------------------------------
            if _do_export:
                ep_h = export_price[h]  # type: ignore[index]
                # Floor import on the (raw) post-load SoC; 0 whenever export is
                # admissible (export requires soc_after > export_floor_h >= floor),
                # so this never changes a real export transition.
                floor_import_export = max(0.0, floor_kwh - soc_after) * price_h
                # Net export revenue per DC kWh: must be strictly positive to export.
                net_rev_per_dc = ep_h * eta_d - cycle_cost
                # C1 mirror: per-hour ride-out floor; defaults to firmware floor.
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
                    # Cap battery export so combined AC feed-in (solar spill +
                    # battery) does not exceed ac_cap = min(max_export_w,
                    # grid_export_limit_w)/1000.  Mirror of optimize.py:722-727.
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
                            break  # monotonically discharging; safe to stop

                        new_b = to_bin(new_soc)
                        eta_d_step = (
                            eta_d if eta_curve is None
                            else _eta_discharge_at(e_dc / dt_h * 1000.0, cfg, eta_curve)
                        )
                        e_ac = e_dc * eta_d_step  # DC → AC: 1 DC kWh → eta_d AC kWh
                        export_revenue = e_ac * ep_h
                        degradation = e_dc * cycle_cost
                        # + floor_import_export mirrors optimize.optimize_grid; it
                        # is 0 whenever export is admissible (soc_after >
                        # export_floor_h >= floor), so this never changes a real
                        # export transition.
                        new_cost = cost - (export_revenue - degradation) + floor_import_export

                        if new_cost < dp_next[new_b]:
                            dp_next[new_b] = new_cost
                            par_h[new_b] = (b, 0.0, e_dc)  # g_ac=0, export_dc=e_dc

        parent.append(par_h)
        dp = dp_next

    # Select minimum-cost end state per terminal_mode.
    if terminal_mode == "water_value":
        v = water_value if water_value is not None else 0.0
        floor_b = to_bin(floor_kwh)
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
        # M2 fix: if no state in [floor_b, target_b] is reachable (soc_start > target),
        # fall back to the lowest net-cost reachable state in [floor_b, n_states);
        # survival floor still guaranteed, so NOT infeasible.
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
        # reserve mode (default) — select minimum-cost end state with SoC >= soc_target.
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
        return {
            "schedule": [0.0] * n,
            "kwh": 0.0,
            "eur": 0.0,
            "export_kwh": 0.0,
            "export_revenue_eur": 0.0,
            "export_schedule": [0.0] * n,
            "infeasible": True,
        }

    # Reconstruct per-hour schedules by backtracking parent pointers.
    # parent tuple: (prev_bin, grid_ac_kwh, export_dc_kwh)
    schedule_ac: list[float] = [0.0] * n
    export_dc_sched: list[float] = [0.0] * n
    floor_import_eur = 0.0
    # M1 mirror of optimize.optimize_grid: fold the below-floor direct-import
    # VOLUME into total_kwh so the oracle kwh matches realized_grid_cost (whose
    # kwh already includes its forced_imports).  Without this score_regret
    # over-reports a phantom over_buy equal to the floor-import volume on every
    # drain-to-floor day.  Zero whenever the floor never binds (byte parity with
    # the pre-M1 total_kwh — and with optimize_grid — preserved exactly).
    floor_import_kwh = 0.0
    cur_b = best_end_b
    for h in range(n - 1, -1, -1):
        info = parent[h][cur_b]
        if info is None:
            break  # should not occur for a properly reachable end state
        prev_b, g_ac, e_dc = info
        schedule_ac[h] = g_ac
        export_dc_sched[h] = e_dc
        # Below-floor direct-import cost — recompute on the POST-charge SoC
        # exactly as the forward pass did (A5; mirrors optimize_grid), so
        # eur == best_cost and parity stays byte-exact.  g_dc = g_ac * eta
        # reconstructs the charge that offset the deficit; for export
        # transitions g_ac=0 (and soc_after_h > floor anyway).  Zero when the
        # floor never binds.
        soc_after_h = _apply_solar_load(
            from_bin(prev_b), day.pv_kwh[h] - day.load_kwh[h], cfg, dt_h,
            eta_curve=eta_curve,
        )
        new_soc_pre_h = soc_after_h + g_ac * eta
        floor_import_step = max(0.0, floor_kwh - new_soc_pre_h)
        floor_import_eur += floor_import_step * day.price[h]
        floor_import_kwh += floor_import_step
        cur_b = prev_b

    total_kwh = sum(schedule_ac) + floor_import_kwh
    import_cost_eur = sum(schedule_ac[h] * day.price[h] for h in range(n))

    # Export summary (F1): convert DC export to AC using eta_d (per-step when an
    # eta_curve is supplied, mirroring the forward-pass routing above so the
    # reported schedule matches the objective the DP actually optimised).
    if _do_export:
        export_schedule_ac: list[float] = [
            e_dc * (
                eta_d if eta_curve is None
                else _eta_discharge_at(e_dc / dt_h * 1000.0, cfg, eta_curve)
            )
            for e_dc in export_dc_sched
        ]
        total_export_kwh = sum(export_schedule_ac)
        gross_export_rev = sum(
            export_schedule_ac[h] * export_price[h]  # type: ignore[index]
            for h in range(n)
        )
        # net export_revenue_eur = gross revenue − cycle degradation cost.
        total_degradation = sum(export_dc_sched) * cycle_cost
        net_export_rev = gross_export_rev - total_degradation
    else:
        export_schedule_ac = [0.0] * n
        total_export_kwh = 0.0
        net_export_rev = 0.0

    # eur = import cost minus net export revenue (may be negative on profitable arb
    # days) plus below-floor direct-import cost (A1).  The charge_margin_eur_per_kwh
    # hurdle is ROUTING-ONLY (steers the transition-cost argmin above) and is
    # deliberately NOT billed here: reported eur must stay actual grid cash so
    # score_regret (the surviving inline DP-regret scorer) is not biased by the
    # synthetic margin (realized_grid_cost carries none).  eur == best_cost -
    # Σ g_dc*margin; forward/oracle eur parity still holds (both reconstruct the
    # same margin-free cash).
    total_eur = import_cost_eur - net_export_rev + floor_import_eur

    result: dict = {
        "schedule": schedule_ac,
        "kwh": total_kwh,
        "eur": total_eur,
        "export_kwh": total_export_kwh,
        "export_revenue_eur": net_export_rev,
        "export_schedule": export_schedule_ac,
    }
    if infeasible:
        result["infeasible"] = True
    return result


def realized_grid_cost(
    day: DayData,
    realized_charge_by_hour: list[float],
    cfg: Config,
    *,
    realized_export_by_hour: list[float] | None = None,
    export_price: list[float] | tuple[float, ...] | None = None,
    dt_h: float = 1.0,
    eta_curve=None,
) -> dict:
    """Simulate realized charging + forced floor-hit imports for a completed day.

    Runs the same hour-by-hour battery physics as hindsight_optimal_grid so that
    the *realized* cost can be compared fairly against the optimal.

    Preconditions
    -------------
    *realized_charge_by_hour* must have exactly ``len(day.pv_kwh)`` elements
    (24 at 60-min, 96 at 15-min); ValueError is raised if it has a different
    length.  When *realized_export_by_hour* is supplied it must also match
    that length.

    Parameters
    ----------
    day : per-slot day observations.
    realized_charge_by_hour : per-slot list (``len(day.pv_kwh)`` elements) —
        AC kWh drawn per slot for deliberate grid charging (e.g. the scheduled
        pre-charge).  Excess beyond battery headroom is PAID but not stored
        (waste).
    cfg : battery / system parameters.
    realized_export_by_hour : optional per-slot list — METERED/derived AC kWh
        actually exported to the grid per slot.  Must be derived from the actual
        energy balance, capped at the battery's discharge power (e.g.
        min(max(0, −p1_w), max(0, batt_w)) from recorder samples — excludes PV
        spill the oracle can't credit), NOT from the commanded export_setpoint_w.
        When supplied together with *export_price*, the gross export revenue
        minus cycle degradation is subtracted from *eur* so the realized cost
        mirrors the oracle's accounting sign.
    export_price : optional per-slot list or tuple — per-slot feed-in tariff
        (€/AC kWh).  Ignored when *realized_export_by_hour* is None.

    Returns
    -------
    dict with keys:
        kwh               : float — total AC kWh drawn from grid
                            (deliberate charges + forced floor-hit imports).
                            Compatible with score_regret(realized_grid_cost(...), opt).
        grid_kwh          : float — alias for kwh (backwards-compat convenience).
        eur               : float — net cost € (grid import cost minus export
                            revenue net of cycle degradation; mirrors oracle eur).
        charge_kwh        : float — DC kWh actually stored from deliberate charges
                            (≤ sum(realized_charge_by_hour) × eta × rate cap, due
                            to headroom and rate-parity cap).
        forced_import_kwh : list[float] — per-hour AC kWh of forced floor-hit
                            imports (24 elements; mostly zeros on well-scheduled days).
        export_revenue_eur : float — net export revenue (€) = gross revenue −
                            cycle degradation.  0.0 when no export is supplied.

    Simulation conventions (identical to hindsight_optimal_grid via _apply_solar_load)
    ------------------------------------------------------------------------------------
    1. Solar/load each hour — via shared _apply_solar_load():
         net > 0:  dc_solar = min(net, rate_kwh_h) × eta, capped at target.
         net ≤ 0:  battery discharges 1:1 (no discharge eta).
    2. Realized deliberate charge — pay for all AC, store only what fits:
         g_dc = min(g_ac × eta, rate_kwh_h × eta, headroom)
         Full AC cost paid regardless (excess = waste).  Rate cap mirrors DP.
    3. Forced floor-hit import — grid serves unmet load DIRECTLY (grid→load, 1:1):
         If SoC < soc_floor after steps 1+2:
           forced_import_ac[h] = floor − soc  (1:1; NOT divided by eta).
           SoC clamps to soc_floor.
         Physics: the battery stopped at floor; the grid directly serves the
         remaining load without going through the battery and its eta loss.
    4. Cost accumulation:
         eur += realized_charge_by_hour[h] × price[h]
               + forced_import_ac[h] × price[h]
               − net_export_revenue_h  (when export is supplied)

    Export revenue accounting (mirrors hindsight_optimal_grid F1 convention)
    --------------------------------------------------------------------------
    When *realized_export_by_hour* and *export_price* are both supplied:
      e_ac_h = realized_export_by_hour[h]   (metered AC kWh, derived from −p1_w,
                                              capped at battery-discharge power)
      eta_d  = min(round_trip_eff / eta_charge, 1.0)   (same formula as oracle)
      e_dc_h = e_ac_h / eta_d               (DC kWh discharged from battery)
      gross_revenue_h = e_ac_h × export_price[h]
      degradation_h   = e_dc_h × cycle_cost_eur_per_kwh
      net_revenue_h   = gross_revenue_h − degradation_h
      eur            -= net_revenue_h        (revenue REDUCES net cost)

    Discharge model diverges from energy.py (intentional; see module docstring).
    """
    # Canonical reference is the DAY's own length (independent of the array
    # being validated) so a wrong-length realized_charge_by_hour is correctly
    # blamed by name rather than trivially matching itself.
    _expected = len(day.pv_kwh)
    _validate_day_len(realized_charge_by_hour, "realized_charge_by_hour", _expected)
    if realized_export_by_hour is not None:
        _validate_day_len(realized_export_by_hour, "realized_export_by_hour", _expected)

    cap_kwh = cfg.capacity_kwh
    floor_kwh = cfg.soc_floor / 100.0 * cap_kwh
    target_kwh = cfg.soc_target / 100.0 * cap_kwh
    rate_kwh_h = cfg.max_charge_w / 1000.0 * dt_h
    eta = cfg.eta_charge if cfg.eta_charge > 1e-9 else 1.0
    rate_dc = rate_kwh_h * eta  # max DC per hour from grid (parity with DP)

    # Export leg pre-computations (F3 — mirrors hindsight_optimal_grid F1 convention).
    _do_export = realized_export_by_hour is not None and export_price is not None
    if _do_export:
        # Discharge efficiency: same formula as oracle (F1).
        eta_d: float = min(cfg.round_trip_eff / eta, 1.0)
        cycle_cost: float = cfg.cycle_cost_eur_per_kwh
    else:
        eta_d = 1.0
        cycle_cost = 0.0

    soc = day.soc_start / 100.0 * cap_kwh
    forced_imports: list[float] = [0.0] * _expected
    total_charge_dc = 0.0

    for h in range(_expected):
        net = day.pv_kwh[h] - day.load_kwh[h]

        # Step 1: Solar/load — shared physics with hindsight_optimal_grid.
        soc = _apply_solar_load(soc, net, cfg, dt_h, eta_curve=eta_curve)

        # Step 2: Deliberate charge — pay full AC, store min(g_ac×eta, rate_dc, headroom).
        g_ac = realized_charge_by_hour[h]
        if g_ac > 0.0:
            headroom = max(0.0, target_kwh - soc)
            g_dc = min(g_ac * eta, rate_dc, headroom)
            soc = soc + g_dc
            total_charge_dc += g_dc

        # Step 2.5: Export SoC drain — AC export → DC discharged from battery.
        # Mirrors oracle: e_dc = e_ac / eta_d (inverse of oracle's e_ac = e_dc × eta_d).
        # Must run BEFORE the floor-hit check so low-SoC export can trigger a forced import.
        if _do_export:
            e_ac_exp = realized_export_by_hour[h]  # type: ignore[index]
            if e_ac_exp > 0.0:
                e_dc_exp = e_ac_exp / eta_d if eta_d > 1e-9 else e_ac_exp
                soc = soc - e_dc_exp

        # Step 3: Forced floor-hit import — grid→load direct, 1:1 (no eta loss).
        if soc < floor_kwh - 1e-9:
            shortfall_dc = floor_kwh - soc
            forced_ac = shortfall_dc          # 1:1: grid serves load directly
            forced_imports[h] = forced_ac
            soc = floor_kwh

    total_kwh = sum(realized_charge_by_hour) + sum(forced_imports)
    eur = (
        sum(realized_charge_by_hour[h] * day.price[h] for h in range(_expected))
        + sum(forced_imports[h] * day.price[h] for h in range(_expected))
    )

    # Step 4 (F3): Subtract actual export revenue to mirror oracle accounting.
    # Metered actual export (AC kWh from −p1_w) → revenue net of cycle degradation.
    net_export_revenue = 0.0
    if _do_export:
        for h in range(_expected):
            e_ac = realized_export_by_hour[h]  # type: ignore[index]
            if e_ac <= 0.0:
                continue
            ep_h = export_price[h]  # type: ignore[index]
            # DC discharged = AC / eta_d (inverse of oracle's e_ac = e_dc × eta_d).
            e_dc = e_ac / eta_d if eta_d > 1e-9 else e_ac
            gross_rev = e_ac * ep_h
            degradation = e_dc * cycle_cost
            net_export_revenue += gross_rev - degradation
        eur -= net_export_revenue

    return {
        "kwh": total_kwh,           # primary key — score_regret reads realized["kwh"]
        "grid_kwh": total_kwh,      # alias for backwards compat
        "eur": eur,
        "charge_kwh": total_charge_dc,
        "forced_import_kwh": forced_imports,
        "export_revenue_eur": net_export_revenue,
    }


def score_regret(realized: dict, optimal: dict) -> dict:
    """Score a completed day's grid-charge decisions against the hindsight optimal.

    Parameters
    ----------
    realized : dict with 'kwh' and 'eur' keys (e.g. from realized_grid_cost).
    optimal  : output of hindsight_optimal_grid() — must contain 'kwh' and 'eur'.

    Returns
    -------
    dict with keys:
        regret_eur      : realized_eur - optimal_eur.  Positive = overspent vs
                          optimal.  THE authoritative single-number regret metric.
        over_buy_kwh    : max(0, realized_kwh - optimal_kwh).  Non-zero when we
                          charged more kWh than hindsight-optimal (solar or prior
                          charge made the grid purchase unnecessary).
        over_buy_eur    : over_buy_kwh × weighted-average realized price — cost of
                          the excess purchases.
        under_buy_kwh   : max(0, optimal_kwh - realized_kwh).  kWh the battery
                          was short of the optimal (floor breaches, unmet reserve).
                          Standalone volume metric; see cost_regret_eur below.
        cost_regret_eur : max(0, regret_eur - over_buy_eur).  Residual regret after
                          subtracting the over-buy component.  Captures TIMING
                          penalty (buying at peak instead of cheap pre-charge)
                          AND/OR volume shortfall cost.
                          When under_buy_kwh == 0 but regret_eur > 0 (same total
                          volume, wrong/expensive timing), cost_regret_eur captures
                          the full timing penalty while under_buy_kwh stays 0.

    Decomposition note
    ------------------
    The buckets (over_buy_eur + cost_regret_eur) are NOT a strict partition of
    regret_eur.  They are an approximate decomposition useful for diagnosis.
    regret_eur is always the authoritative number.  A4 sensors surface
    regret_eur + over_buy_kwh + under_buy_kwh.
    """
    r_kwh: float = realized["kwh"]
    r_eur: float = realized["eur"]
    o_kwh: float = optimal["kwh"]
    o_eur: float = optimal["eur"]

    regret_eur = r_eur - o_eur

    over_buy_kwh = max(0.0, r_kwh - o_kwh)
    under_buy_kwh = max(0.0, o_kwh - r_kwh)

    avg_realized_price = r_eur / r_kwh if r_kwh > 1e-9 else 0.0
    over_buy_eur = over_buy_kwh * avg_realized_price
    cost_regret_eur = max(0.0, regret_eur - over_buy_eur)

    return {
        "regret_eur": regret_eur,
        "over_buy_kwh": over_buy_kwh,
        "over_buy_eur": over_buy_eur,
        "under_buy_kwh": under_buy_kwh,
        "cost_regret_eur": cost_regret_eur,
    }
