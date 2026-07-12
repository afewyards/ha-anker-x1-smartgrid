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
