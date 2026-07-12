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
