"""Minimum-export-block filter for the Anker X1 SmartGrid export schedule.

Drops contiguous export runs that are too small to be worth executing —
below ``cfg.export_min_block_kwh`` — to prevent the battery from cycling for
negligible revenue.  A run that is currently in progress (contains
``exempt_index``) is always preserved regardless of size.

After dropping whole sub-threshold runs, a per-slot **tail-trim** pass zeroes
leading/trailing sub-threshold slots within each surviving run, anchored to
the run's *core* slots (those whose value ≥ threshold).  Interior sub-threshold
slots between two cores are kept.  ``cfg.export_min_block_kwh`` gates both
the per-run drop and the per-slot tail-trim — set it to 0 to disable both.

Spec: docs/superpowers/specs/2026-06-27-minimum-export-block-design.md §Mechanism.
Tail-trim spec: docs/superpowers/specs/2026-06-28-export-min-block-tail-trim-design.md

Revenue recompute mirrors optimize.py:828-832::

    gross_rev = Σ export_ac[h] · export_price[h]
    net_rev   = gross_rev − Σ (export_ac[h] / eta_d) · cycle_cost

where ``eta_d = optimize.eta_discharge(cfg)``.

Circular-import safety
----------------------
``export_filter`` imports from ``optimize`` (one direction only).  ``optimize``
does not import ``export_filter``, so there is no cycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import Config


def apply_min_export_block(
    export_ac: list[float],
    export_price: list[float] | None,
    cfg: Config,
    exempt_index: int,
    dt_h: float = 1.0,
    *,
    eta_curve=None,
) -> tuple[list[float], float]:
    """Filter export schedule by minimum block size and recompute net revenue.

    Identifies contiguous *runs* (hours with AC kWh > 1e-9; a zero/sub-epsilon
    hour breaks a run), then drops any run whose total kWh is strictly less
    than ``cfg.export_min_block_kwh``.  The run containing ``exempt_index``
    (the current in-progress hour — pass 0 in live use) is always kept.

    No-op paths:

    * ``export_price is None`` → defensive guard; returns a copy of
      *export_ac* and ``0.0`` revenue.  The controller only calls this
      function when an export price is available, so this branch should not
      be reached in normal operation.
    * ``cfg.export_min_block_kwh <= 0.0`` → filtering disabled; returns a
      copy of *export_ac* with revenue recomputed from the full schedule.
      This disables BOTH the per-run drop and the per-slot tail-trim.

    Tail-trim behaviour (applied after the per-run drop):

    For each run that survives the per-run drop, *core* slots are those whose
    value is ≥ the threshold ENERGY-RATE ``export_min_block_kwh * dt_h``
    (using ``>= threshold - 1e-9`` so that a slot exactly at the threshold is
    a core and is kept).  The core anchor is a rate, not an absolute kWh, so
    it is resolution-independent: at ``dt_h=0.25`` a core carries
    ``export_min_block_kwh * 0.25`` kWh — the same average POWER as a
    60-minute core.  The per-run drop above stays on total kWh (already
    resolution-independent, since it sums the whole run regardless of slot
    width).

    * Run has ≥1 core: leading slots (value < threshold) are zeroed inward
      from the run start until the first core; trailing slots are zeroed
      inward from the run end until the last core.  Interior slots between
      two cores are always kept.
    * Run has NO core (but survived per-run on total): kept whole — there is
      no anchor to trim against.
    * The slot at ``exempt_index`` is never zeroed.  If the leading trim scan
      reaches ``exempt_index`` before a core, trimming stops immediately
      (``exempt_index`` and everything between it and the core is preserved).
      The same rule applies to trailing trim.

    Parameters
    ----------
    export_ac : list[float]
        Planned AC export kWh per horizon hour.  Mutated copy is returned;
        the original is never modified.
    export_price : list[float] | None
        Export price (€/kWh AC) per horizon hour; assumed same length as
        *export_ac* (no runtime guard — callers are responsible).  ``None`` triggers the
        defensive no-op path.
    cfg : Config
        System configuration.  Reads ``export_min_block_kwh`` (threshold for
        both per-run drop and per-slot tail-trim), ``cycle_cost_eur_per_kwh``,
        ``round_trip_eff``, and ``eta_charge``.
    exempt_index : int
        Index of the current (in-progress) hour.  The run containing this
        index is never dropped, and this slot is never zeroed by the tail-trim.
        Pass ``0`` in live use (current hour is always index 0 in the rolling
        horizon).
    dt_h : float, default 1.0
        Slot duration in hours.  Scales the tail-trim's core threshold from
        an absolute kWh into a per-slot energy amount (``export_min_block_kwh
        * dt_h``) so a genuine high-power slot still qualifies as a core at
        sub-hour resolutions (e.g. 15-min slots).  The default of ``1.0``
        matches legacy 60-minute slots and keeps every existing caller
        byte-identical.
    eta_curve : optional EfficiencyCurve
        Power-dependent discharge efficiency curve for the net-revenue
        recompute.  ``None`` (default) reproduces today's static
        ``optimize.eta_discharge(cfg)`` scalar byte-identically.  When
        supplied, each hour's ``eta_d`` is looked up at that hour's AC export
        power (``filtered[h] / dt_h * 1000.0`` watts) via
        ``eta_curve.eta_discharge(...)`` instead of the single static value.

    Returns
    -------
    tuple[list[float], float]
        ``(filtered_ac, filtered_net_revenue_eur)`` where:

        * ``filtered_ac`` has the same length as *export_ac*.  Dropped-run
          hours and tail-trimmed slots are set to ``0.0``.
        * ``filtered_net_revenue_eur`` is the net revenue (€) for the
          kept hours (after both per-run drop and tail-trim), subtracting
          per-kWh cycle degradation cost.
    """
    # Lazy import — circular-safe because optimize does not import this module.
    from . import optimize

    # ------------------------------------------------------------------
    # Guard: no export price → can't compute revenue, return unchanged.
    # ------------------------------------------------------------------
    if export_price is None:
        return list(export_ac), 0.0

    filtered = list(export_ac)
    min_kwh = cfg.export_min_block_kwh

    # ------------------------------------------------------------------
    # Main filtering — skipped when threshold is zero or negative.
    # ------------------------------------------------------------------
    if min_kwh > 0.0:
        # Segment filtered into contiguous runs of exporting hours.
        runs: list[list[int]] = []
        current_run: list[int] = []
        for h, ac in enumerate(filtered):
            if ac > 1e-9:
                current_run.append(h)
            else:
                if current_run:
                    runs.append(current_run)
                    current_run = []
        if current_run:
            runs.append(current_run)

        for run in runs:
            # The in-progress run is exempt — never drop it.
            if exempt_index in run:
                continue
            run_total_kwh = sum(filtered[h] for h in run)
            # Strict less-than: a run exactly at the threshold is KEPT.
            if run_total_kwh < min_kwh:
                for h in run:
                    filtered[h] = 0.0

        # ------------------------------------------------------------------
        # Per-slot tail-trim — for each surviving run, zero leading/trailing
        # sub-threshold slots up to the first/last core slot.
        # A core slot has value >= threshold (>= min_kwh - 1e-9 to treat a
        # slot exactly at threshold as a core).
        # The slot at exempt_index is never zeroed; trimming stops if reached.
        # ------------------------------------------------------------------
        for run in runs:
            # Skip runs that were dropped by the per-run pass above.
            if not any(filtered[h] > 1e-9 for h in run):
                continue
            # Find core slots within the (still-live) run.
            # Core = slot at/above the threshold ENERGY-RATE (kWh per hour), so the
            # anchor is resolution-independent: at dt_h=0.25 a core carries
            # min_kwh*0.25 kWh (same average power as a 60-min core).  Per-run drop
            # above stays on total kWh (already resolution-independent).
            # Boundary is >= threshold (slot exactly at threshold IS a core/kept).
            # This intentionally differs from the per-run drop, which uses strict
            # < threshold — the two boundaries are consistent: a slot at exactly
            # the per-slot threshold is always preserved, never dropped or trimmed.
            _core_kwh = min_kwh * dt_h
            cores = [h for h in run if filtered[h] >= _core_kwh - 1e-9]
            if not cores:
                # No core anchor — keep the run whole (survived on total).
                continue
            first_core = cores[0]
            last_core = cores[-1]
            # Trim leading sub-threshold slots inward to the first core.
            for h in run:
                if h == exempt_index:
                    break  # exempt slot and everything toward the core is kept
                if h >= first_core:
                    break  # reached the first core — stop
                filtered[h] = 0.0
            # Trim trailing sub-threshold slots inward to the last core.
            for h in reversed(run):
                if h == exempt_index:
                    break  # exempt slot is protected
                if h <= last_core:
                    break  # reached the last core — stop
                filtered[h] = 0.0

    # ------------------------------------------------------------------
    # Revenue recompute — mirrors optimize.py:828-832.
    # net_rev = Σ ac[h]·price[h]  −  Σ (ac[h]/eta_d)·cycle_cost
    # eta_curve=None keeps the static single-scalar eta_d (parity path);
    # when supplied, each hour looks up eta_d at its own AC export power.
    # ------------------------------------------------------------------
    cycle_cost = cfg.cycle_cost_eur_per_kwh
    if eta_curve is None:
        eta_d = optimize.eta_discharge(cfg)
        net_revenue = sum(
            filtered[h] * export_price[h] - (filtered[h] / eta_d) * cycle_cost for h in range(len(filtered))
        )
    else:
        net_revenue = sum(
            filtered[h] * export_price[h]
            - (filtered[h] / eta_curve.eta_discharge(filtered[h] / dt_h * 1000.0)) * cycle_cost
            for h in range(len(filtered))
        )

    return filtered, net_revenue
