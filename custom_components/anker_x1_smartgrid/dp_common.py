"""Shared DP internals for the optimize/regret parity pair.

``optimize.optimize_grid`` (forecast-fed, online) and
``regret.hindsight_optimal_grid`` (hindsight, oracle) run structurally
identical dynamic-program cores.  Blocks that were hand-mirrored between the
two files as byte-identical text — enforced only by a code comment telling
the next editor to keep them in sync — live here instead, so a future edit
that breaks parity is a compile-time single edit site, not two files that
must be remembered to change together.

No Home Assistant imports — pure stdlib (see tests/test_import_boundaries.py).

The SoC grid (``_BIN_KWH``, :func:`grid_floor_kwh`), the eta lookups and the
windowed price references live here; regret.py re-exports the SoC grid and
imports the rest. This module must not import regret.py, which depends on it —
an import in both directions would be a circular import.
"""

from __future__ import annotations

import math
from collections.abc import Callable

from . import const
from .models import Config

# Discretisation resolution for the SoC DP (kWh per bin).
# 0.05 kWh → ≤ 201 states per 10 kWh battery; exact for inputs that are
# multiples of 0.05 kWh (e.g. eta=1.0 with load/PV in 0.5 kWh steps).
_BIN_KWH: float = 0.05


def grid_floor_kwh(floor_kwh: float) -> float:
    """``floor_kwh`` rounded UP onto the DP's SoC grid.

    Every DP state is binned, so a floor that falls between bins re-bins below
    itself each slot and books a phantom import against it; rounding up instead
    of to-nearest keeps the pack from being modelled as usable below the real
    floor.
    """
    return math.ceil(floor_kwh / _BIN_KWH - 1e-9) * _BIN_KWH


def soc_bins(
    cap_kwh: float,
) -> tuple[int, Callable[[float], int], Callable[[int], float]]:
    """SoC-bin count plus ``to_bin``/``from_bin`` closures for a DP over
    ``cap_kwh``.

    Shared by ``optimize.optimize_grid`` and ``regret.hindsight_optimal_grid``
    — the two DP cores must discretise SoC identically for their outputs to
    stay byte-parity (see ``tests/test_optimize_parity.py``).
    """
    bin_kwh = _BIN_KWH
    n_states = round(cap_kwh / bin_kwh) + 1

    def to_bin(soc: float) -> int:
        return max(0, min(n_states - 1, round(soc / bin_kwh)))

    def from_bin(b: int) -> float:
        return b * bin_kwh

    return n_states, to_bin, from_bin


def reserve_wall_kwh(cfg: Config) -> float:
    """The backup reserve in DC kWh, rounded up onto the SoC grid; ``0.0`` when off."""
    reserve = cfg.backup_reserve_kwh
    return grid_floor_kwh(reserve) if reserve > 0.0 else 0.0


def discharge_wall_kwh(soc_kwh: float, firmware_floor_kwh: float, reserve_kwh: float) -> float:
    """Lowest SoC a slot discharges to from *soc_kwh*: the reserve, or where it starts when already under it."""
    return max(firmware_floor_kwh, min(reserve_kwh, soc_kwh))


def wall_ac_per_dc(net_kwh: float, eta_load: float, idle_kwh: float) -> float:
    """AC the meter buys per DC kWh the pack does not deliver at the wall in a slot.

    The pack would have drawn the deficit at *eta_load* plus the standby 1:1; held,
    the meter buys both as AC, as the low-power hold books them. 1.0 on the 1:1 path.
    """
    deficit = max(0.0, -net_kwh)
    dc = deficit / eta_load + idle_kwh
    return (deficit + idle_kwh) / dc if dc > 0.0 else 1.0


def wall_import_kwh(soc_kwh: float, wall_kwh: float, firmware_floor_kwh: float, ac_per_dc: float) -> float:
    """AC the meter buys for the house when the pack stops at *wall_kwh* instead of falling to *soc_kwh*.

    Under the firmware floor the shortfall is booked 1:1; between it and the wall the DC the pack
    did not deliver is converted at *ac_per_dc* (:func:`wall_ac_per_dc`).
    """
    return max(0.0, firmware_floor_kwh - soc_kwh) + max(0.0, wall_kwh - max(soc_kwh, firmware_floor_kwh)) * ac_per_dc


def start_soc_kwh(soc0: float, cfg: Config, firmware_floor_kwh: float) -> float:
    """Start SoC for the DP: a start at the real firmware floor must not bin below the snapped one."""
    return firmware_floor_kwh if cfg.firmware_floor_kwh <= soc0 < firmware_floor_kwh else soc0


def published_spread(prices: list[float]) -> float:
    """Max minus min of the window's prices, floored at ``BACKUP_RESERVE_MIN_SPREAD``."""
    spread = max(prices) - min(prices) if prices else 0.0
    return max(spread, const.BACKUP_RESERVE_MIN_SPREAD)


def reserve_urgency_by_bin(
    reserve_kwh: float,
    floor_kwh: float,
    spread: float,
    refill_h: float,
    n_states: int,
    from_bin: Callable[[int], float],
    dt_h: float,
) -> list[float] | None:
    """Objective-only rent for ending a slot under the reserve, quadratic in the shortfall; ``None`` when off.

    The marginal kWh at depth fraction d costs ``spread * d / refill_h`` per hour, so the top of the band is
    nearly free and the last kWh only pays for itself if the refill takes under ``refill_h`` hours.
    """
    if reserve_kwh <= 0.0:
        return None
    k = spread * dt_h / refill_h / (2.0 * max(reserve_kwh - floor_kwh, 1e-9))
    return [max(0.0, reserve_kwh - from_bin(b)) ** 2 * k for b in range(n_states)]


# Equal-value end states must not be picked by float rounding: ties go to the higher SoC.
TIE_EPS_EUR = 1e-9
# A kWh bought at exactly its kept value gains nothing, so the kept value sits this far
# below it; one SoC bin of it must clear TIE_EPS_EUR or the tie above buys it.
KEEP_TIE_EUR_PER_KWH = 1e-6


def select_end_state(
    dp: list[float],
    *,
    terminal_mode: str,
    water_value: float | None,
    firmware_floor_kwh: float,
    floor_kwh: float,
    target_kwh: float,
    to_bin: Callable[[float], int],
    from_bin: Callable[[int], float],
    n_states: int,
    terminal_segments: list[tuple[float, float]] | None = None,
    band_top_kwh: float = 0.0,
    reserve_kwh: float = 0.0,
    reserve_deadband_kwh: float = 0.0,
) -> tuple[int, float, bool]:
    """Select the DP terminal end state: ``(best_end_b, best_cost, infeasible)``.

    Shared by ``optimize.optimize_grid`` and ``regret.hindsight_optimal_grid``
    — MUST stay byte-identical between the two DP cores (parity gate; see
    ``TestExportOnParity.test_two_peaks_water_value_mode`` in
    ``tests/test_optimize_parity.py``).  ``dp`` is the cost array after the
    DP's final hour transition; ``to_bin``/``from_bin``/``n_states`` come
    from :func:`soc_bins`.

    ``terminal_mode="water_value"``: price end-SoC by the trough water value
    instead of forcing the reserve.  The per-transition floor constraint
    already guarantees every reachable end state has SoC >= floor, so
    survival-floor unreachability is NOT flagged here (it is handled
    upstream by the caller's shield path).  Only the all-pruned pathological
    case (``best_end_b`` left at -1) signals a problem, and handling that is
    left to the caller.

    When ``terminal_segments`` is set, the credit is piecewise (spec
    rev-3 of the overnight terminal value): ``terminal_segments`` is a list
    of ``(dc_kwh, value_eur_per_dc_kwh)`` pairs, sorted by value descending
    defensively (once per call, not per bin — the caller's ordering is not
    trusted). Energy available above the HARD ``firmware_floor_kwh`` is
    walked through the sorted segments richest-first, each segment consuming
    up to its own ``dc_kwh`` at its own rate; any surplus beyond all
    segments is credited at the original ``water_value`` (``v``). This also
    shifts the credit anchor itself from the SOFT ``floor_kwh`` down to
    ``firmware_floor_kwh`` — the (firmware_floor_kwh, floor_kwh] sub-margin
    band earns credit too, where the legacy single-rate formula gave it
    none. When ``terminal_segments`` is None (legacy/default), the credit is
    the original single-rate formula, anchored at ``floor_kwh``,
    byte-identical to the pre-piecewise-credit behaviour.

    Either way, energy left below ``band_top_kwh`` (the derated bottom band)
    is credited at ``const.DISCHARGE_DERATE_5_10`` of its rate; the band is
    the first energy the walk credits, so end states above it all shift by
    the same amount. ``0.0`` disables this.

    An end state more than ``reserve_deadband_kwh`` under ``reserve_kwh`` (the
    backup reserve) scores ``const.BACKUP_SHORTFALL_EUR_PER_KWH`` per kWh beyond
    it, so the plan refills it but a reading a point under is not topped up;
    the penalty is selection-only and is never in the returned cost.

    ``terminal_mode="reserve"`` (default): select the minimum-cost end state
    with SoC >= soc_target; if unreachable, fall back to the best achievable
    end state and mark ``infeasible``.
    """
    INF = float("inf")
    if terminal_mode == "water_value":
        v = water_value if water_value is not None else 0.0
        _segs = sorted(terminal_segments, key=lambda s: -s[1]) if terminal_segments is not None else None
        band_loss = 1.0 - const.DISCHARGE_DERATE_5_10

        def _walk(avail: float) -> float:
            credit = 0.0
            for seg_kwh, seg_v in _segs or ():
                take = min(avail, seg_kwh)
                credit += take * seg_v
                avail -= take
                if avail <= 0.0:
                    break
            return credit + max(0.0, avail) * v

        def _credit(end_b: int) -> float:
            x = from_bin(end_b)
            if _segs is not None:
                return _walk(
                    max(0.0, x - firmware_floor_kwh) - band_loss * max(0.0, min(x, band_top_kwh) - firmware_floor_kwh)
                )
            return (max(0.0, x - floor_kwh) - band_loss * max(0.0, min(x, band_top_kwh) - floor_kwh)) * v

        # Scan from the firmware floor (widened): sub-soft-floor end states
        # are reachable (transition clamp sags to firmware_floor_kwh, not
        # floor_kwh).  The credit anchor stays at floor_kwh (soft margin)
        # below so those sub-margin states simply earn zero credit, not a
        # penalty -- unless water_value_hi shifts the anchor (see _credit).
        floor_b = to_bin(firmware_floor_kwh)
        target_b = to_bin(target_kwh)
        short_from = reserve_kwh - reserve_deadband_kwh

        def _pick(hi: int) -> int:
            scores = {
                b: dp[b] - _credit(b) + max(0.0, short_from - from_bin(b)) * const.BACKUP_SHORTFALL_EUR_PER_KWH
                for b in range(floor_b, hi)
                if dp[b] != INF
            }
            if not scores:
                return -1
            best = min(scores.values())
            return max(b for b, sc in scores.items() if sc <= best + TIE_EPS_EUR)

        best_end_b = _pick(target_b + 1)
        # Fallback (M2 fix): soc_start > soc_target means [floor_b, target_b]
        # states are unreachable via charging.  Scan the full [floor_b,
        # n_states) range for the lowest net-cost reachable state (still
        # infeasible=False — the survival floor is satisfied).
        if best_end_b == -1:
            best_end_b = _pick(n_states)
        best_cost = dp[best_end_b] if best_end_b != -1 else INF
        infeasible = False
    else:
        # Reserve mode (default) — select minimum-cost end state with
        # SoC >= soc_target.
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

    return best_end_b, best_cost, infeasible


def export_leg_precompute(
    export_price: list[float] | None,
    cfg: Config,
    eta: float,
    eta_curve,
    dt_h: float,
    *,
    day_index: list[int] | None = None,
) -> tuple[float, float, float, float, float, list[float]]:
    """Export-leg pre-computations: ``(eta_d, cycle_cost, max_export_dc_h,
    ac_cap, band, peak_from)``.  All zero/off when ``export_price`` is None.

    Shared by ``optimize.optimize_grid`` and ``regret.hindsight_optimal_grid``.
    ``day_index`` is the ONE real difference between the two callers:
    ``optimize.optimize_grid`` threads a per-hour day index (derived from its
    ``window_start_h``/``slots_per_day``, or a caller-supplied override) into
    :func:`windowed_peak_prices` so the look-back peak never reaches
    across a day boundary in a multi-day rolling window.
    ``regret.hindsight_optimal_grid`` operates on a single realized day and
    always calls with ``day_index=None`` (global, single-day suffix-max —
    see ``windowed_peak_prices``'s docstring). Callers resolve their own
    ``day_index`` value BEFORE calling this function; this function only
    forwards it.
    """
    if export_price is not None:
        # Discharge efficiency: eta_d = min(round_trip_eff / eta_charge, 1.0)
        # on the None path (reuses `eta`, already computed by the caller with
        # the same zero-guard); looked up on the curve at the max export rate
        # otherwise.
        if eta_curve is None:
            eta_d: float = min(cfg.round_trip_eff / eta, 1.0)
        else:
            eta_d = eta_discharge_at(cfg.max_export_w, cfg, eta_curve)
        cycle_cost: float = cfg.cycle_cost_eur_per_kwh
        # Max DC kWh per hour that can be exported through the inverter.
        # max_export_w is the AC-side export rate cap; DC discharged = AC / eta_d.
        max_export_ac_h = cfg.max_export_w / 1000.0 * dt_h
        max_export_dc_h: float = max_export_ac_h / eta_d if eta_d > 1e-9 else max_export_ac_h
        # Combined AC grid export cap (battery + solar spill must not exceed
        # this) — the tighter of the inverter export rating and the grid
        # connection limit.
        ac_cap: float = min(cfg.max_export_w, cfg.grid_import_limit_w) / 1000.0 * dt_h
        # C2: peak-only gate. Export is admitted in hour h only when
        # export_price[h] is within export_peak_band_frac of the windowed
        # peak (look-back-windowed — see windowed_peak_prices).
        band = cfg.export_peak_band_frac
        peak_from = windowed_peak_prices(
            list(export_price),
            round(cfg.export_peak_lookback_h / dt_h),  # wall-clock lookback -> slots (T8)
            day_index=day_index,
        )
    else:
        eta_d = 1.0
        cycle_cost = 0.0
        max_export_dc_h = 0.0
        ac_cap = 0.0
        band = 0.0
        peak_from = []

    return eta_d, cycle_cost, max_export_dc_h, ac_cap, band, peak_from


def eta_charge_at(dc_power_w: float, cfg: Config, eta_curve) -> float:
    """Charge-side eta at *dc_power_w* — measured curve if provided, else the
    static config scalar (eta_curve=None keeps today's behaviour)."""
    if eta_curve is not None:
        return eta_curve.eta_charge(dc_power_w)
    return cfg.eta_charge_safe()


def eta_discharge_at(dc_power_w: float, cfg: Config, eta_curve) -> float:
    """Discharge-side eta at *dc_power_w* — measured curve if provided, else
    the static round-trip-derived scalar (eta_curve=None keeps today's
    behaviour)."""
    if eta_curve is not None:
        return eta_curve.eta_discharge(dc_power_w)
    return cfg.eta_discharge_static()


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
            run = prices[h]  # reset suffix at the day boundary
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
            run = prices[h]  # reset suffix at the day boundary
        else:
            run = min(run, prices[h])
        suffix[h] = run
    day_start = [0] * n
    for h in range(n):
        day_start[h] = h if (h == 0 or day_index[h] != day_index[h - 1]) else day_start[h - 1]
    return [suffix[max(day_start[h], h - lookback)] for h in range(n)]
