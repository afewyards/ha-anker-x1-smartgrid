"""Shared DP internals for the optimize/regret parity pair.

``optimize.optimize_grid`` (forecast-fed, online) and
``regret.hindsight_optimal_grid`` (hindsight, oracle) run structurally
identical dynamic-program cores.  Blocks that were hand-mirrored between the
two files as byte-identical text — enforced only by a code comment telling
the next editor to keep them in sync — live here instead, so a future edit
that breaks parity is a compile-time single edit site, not two files that
must be remembered to change together.

No Home Assistant imports — pure stdlib (see tests/test_import_boundaries.py).

Physics constants/helpers referenced by the functions below (``_BIN_KWH``,
``_eta_discharge_at``, ``windowed_peak_prices``) stay owned by regret.py and
are imported locally inside each function rather than at module scope. That
keeps this module free of a module-level dependency on regret.py, which now
depends on this module — a top-level import in both directions would be a
circular import.
"""

from __future__ import annotations

from collections.abc import Callable

from .models import Config


def soc_bins(
    cap_kwh: float,
) -> tuple[int, Callable[[float], int], Callable[[int], float]]:
    """SoC-bin count plus ``to_bin``/``from_bin`` closures for a DP over
    ``cap_kwh``.

    Shared by ``optimize.optimize_grid`` and ``regret.hindsight_optimal_grid``
    — the two DP cores must discretise SoC identically for their outputs to
    stay byte-parity (see ``tests/test_optimize_parity.py``).
    """
    from .regret import _BIN_KWH  # local import: avoids a module-level cycle

    bin_kwh = _BIN_KWH
    n_states = round(cap_kwh / bin_kwh) + 1

    def to_bin(soc: float) -> int:
        return max(0, min(n_states - 1, round(soc / bin_kwh)))

    def from_bin(b: int) -> float:
        return b * bin_kwh

    return n_states, to_bin, from_bin


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

    ``terminal_mode="reserve"`` (default): select the minimum-cost end state
    with SoC >= soc_target; if unreachable, fall back to the best achievable
    end state and mark ``infeasible``.
    """
    INF = float("inf")
    if terminal_mode == "water_value":
        v = water_value if water_value is not None else 0.0
        # Scan from the firmware floor (widened): sub-soft-floor end states
        # are reachable (transition clamp sags to firmware_floor_kwh, not
        # floor_kwh).  The credit anchor stays at floor_kwh (soft margin)
        # below so those sub-margin states simply earn zero credit, not a
        # penalty.
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
        # Fallback (M2 fix): soc_start > soc_target means [floor_b, target_b]
        # states are unreachable via charging.  Scan the full [floor_b,
        # n_states) range for the lowest net-cost reachable state (still
        # infeasible=False — the survival floor is satisfied).
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
    :func:`regret.windowed_peak_prices` so the look-back peak never reaches
    across a day boundary in a multi-day rolling window.
    ``regret.hindsight_optimal_grid`` operates on a single realized day and
    always calls with ``day_index=None`` (global, single-day suffix-max —
    see ``windowed_peak_prices``'s docstring). Callers resolve their own
    ``day_index`` value BEFORE calling this function; this function only
    forwards it.
    """
    from .regret import _eta_discharge_at, windowed_peak_prices  # local: avoids a module-level cycle

    if export_price is not None:
        # Discharge efficiency: eta_d = min(round_trip_eff / eta_charge, 1.0)
        # on the None path (reuses `eta`, already computed by the caller with
        # the same zero-guard); looked up on the curve at the max export rate
        # otherwise.
        if eta_curve is None:
            eta_d: float = min(cfg.round_trip_eff / eta, 1.0)
        else:
            eta_d = _eta_discharge_at(cfg.max_export_w, cfg, eta_curve)
        cycle_cost: float = cfg.cycle_cost_eur_per_kwh
        # Max DC kWh per hour that can be exported through the inverter.
        # max_export_w is the AC-side export rate cap; DC discharged = AC / eta_d.
        max_export_ac_h = cfg.max_export_w / 1000.0 * dt_h
        max_export_dc_h: float = max_export_ac_h / eta_d if eta_d > 1e-9 else max_export_ac_h
        # Combined AC grid export cap (battery + solar spill must not exceed
        # this) — the tighter of the inverter export rating and the grid
        # connection limit.
        ac_cap: float = min(cfg.max_export_w, cfg.grid_export_limit_w) / 1000.0 * dt_h
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
