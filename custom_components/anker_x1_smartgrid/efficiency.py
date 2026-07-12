"""Measured battery efficiency curve (pure module).

AC energy from the energy-balance residual ``load_w - p1_w - pv_w``; DC
energy from ``ΔSoC × capacity`` — the BMS coulomb counter, independent of
``batt_w``; ``batt_w`` only segments episodes. Per-bin trailing-window
median with a confidence gate; graceful fallback to the static config
scalars. No Home Assistant imports — stdlib + project models only.

Since the meter/house-load refactor, ``load_w`` is a COMPUTED value
(``pv + p1_w(meter) + batt_w - inverter_loss``, see recorder.py's
``read_efficiency_samples``), so the residual ``load_w - p1_w - pv_w``
algebraically collapses to ``batt_w - inverter_loss``: it is no longer an
independent AC measurement. That no longer disqualifies this pipeline,
though. The independent ground truth this curve actually calibrates
against is ΔSoC — the BMS coulomb counter, which never depends on
``batt_w`` or the residual. What gets measured is the mapping from that
computed-AC quantity to measured ΔSoC, and that computed-AC quantity is
exactly the "load" the planner already schedules charge/export against —
so per-bin efficiency measured this way stays planning-consistent even
though the AC side is no longer metrologically independent. Re-derive this
note if an independent AC house-load sensor becomes available again (it
would let the curve additionally validate the residual itself, rather than
only calibrate against it).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median

from . import const
from .models import Config


@dataclass(frozen=True)
class BinStat:
    lo_w: float
    hi_w: float
    direction: str
    eta: float
    measured: float | None
    n_runs: int
    dc_kwh: float
    confident: bool
    fallback_reason: str

    def as_dict(self) -> dict:
        return {
            "lo_w": self.lo_w,
            "hi_w": None if self.hi_w == float("inf") else self.hi_w,
            "eta": round(self.eta, 4),
            "measured": None if self.measured is None else round(self.measured, 4),
            "n_runs": self.n_runs,
            "dc_kwh": round(self.dc_kwh, 3),
            "confident": self.confident,
            "fallback_reason": self.fallback_reason,
        }


def _bin_edges() -> list[float]:
    return const.EFFICIENCY_DC_BIN_EDGES_W


def bin_index(power_w: float) -> int:
    p = abs(power_w)
    for i, hi in enumerate(_bin_edges()):
        if p < hi:
            return i
    return len(_bin_edges())


def _fallback_bins(direction: str, eta: float, reason: str = "no_data") -> list[BinStat]:
    edges = _bin_edges()
    los = [0.0, *edges]
    his = [*edges, float("inf")]
    return [BinStat(lo, hi, direction, eta, None, 0, 0.0, False, reason) for lo, hi in zip(los, his)]


class EfficiencyCurve:
    def __init__(
        self,
        charge_bins: list[BinStat],
        discharge_bins: list[BinStat],
        fallback_charge: float,
        fallback_discharge: float,
    ) -> None:
        self._charge = charge_bins
        self._discharge = discharge_bins
        self._fc = fallback_charge
        self._fd = fallback_discharge

    def eta_charge(self, dc_power_w: float) -> float:
        return self._charge[bin_index(dc_power_w)].eta

    def eta_discharge(self, dc_power_w: float) -> float:
        return self._discharge[bin_index(dc_power_w)].eta

    def as_attributes(self) -> dict:
        return {
            "charge": [b.as_dict() for b in self._charge],
            "discharge": [b.as_dict() for b in self._discharge],
            "any_over_unity": any(b.fallback_reason == "over_unity" for b in (*self._charge, *self._discharge)),
        }

    @staticmethod
    def _static_scalars(cfg: Config) -> tuple[float, float]:
        eta_c = cfg.eta_charge_safe()
        eta_d = cfg.eta_discharge_static()
        return eta_c, eta_d

    @classmethod
    def static(cls, cfg: Config) -> EfficiencyCurve:
        eta_c, eta_d = cls._static_scalars(cfg)
        return cls(
            _fallback_bins("charge", eta_c),
            _fallback_bins("discharge", eta_d),
            eta_c,
            eta_d,
        )

    @classmethod
    def build(cls, rows: list[dict], cfg: Config, now: datetime) -> EfficiencyCurve:
        """Aggregate per-run etas (from ``rows``) into a per-bin median curve.

        ``rows`` must be chronologically ordered (oldest first) — episode
        segmentation relies on ascending timestamps. Rows older than
        ``EFFICIENCY_WINDOW_DAYS`` are dropped before segmentation.
        """
        eta_c_fb, eta_d_fb = cls._static_scalars(cfg)
        cutoff = (now - timedelta(days=const.EFFICIENCY_WINDOW_DAYS)).isoformat()
        windowed = [r for r in rows if r.get("ts") and r["ts"] >= cutoff]
        n_bins = len(_bin_edges()) + 1
        etas: dict[str, list[list[float]]] = {
            "charge": [[] for _ in range(n_bins)],
            "discharge": [[] for _ in range(n_bins)],
        }
        dc: dict[str, list[float]] = {
            "charge": [0.0] * n_bins,
            "discharge": [0.0] * n_bins,
        }
        for run in segment_episodes(windowed):
            r = run_eta(run, cfg)
            if r is None:
                continue
            i = bin_index(r.dc_power_w)
            etas[r.direction][i].append(r.eta)
            dc[r.direction][i] += r.dc_kwh
        charge_bins = cls._aggregate("charge", etas, dc, eta_c_fb)
        discharge_bins = cls._aggregate("discharge", etas, dc, eta_d_fb)
        return cls(charge_bins, discharge_bins, eta_c_fb, eta_d_fb)

    @staticmethod
    def _aggregate(
        direction: str,
        etas: dict[str, list[list[float]]],
        dc: dict[str, list[float]],
        fallback: float,
    ) -> list[BinStat]:
        """Per-bin median with a confidence gate; low-confidence bins fall back."""
        edges = _bin_edges()
        los = [0.0, *edges]
        his = [*edges, float("inf")]
        lo_env = const.EFFICIENCY_ENVELOPE[0]
        out: list[BinStat] = []
        for i, (lo, hi) in enumerate(zip(los, his)):
            samples = etas[direction][i]
            dc_kwh = dc[direction][i]
            n = len(samples)
            med = median(samples) if samples else None
            if med is None:
                out.append(BinStat(lo, hi, direction, fallback, None, 0, 0.0, False, "no_data"))
                continue
            if med > 1.0:
                out.append(BinStat(lo, hi, direction, fallback, med, n, dc_kwh, False, "over_unity"))
                continue
            confident = n >= const.EFFICIENCY_MIN_RUNS and dc_kwh >= const.EFFICIENCY_MIN_DC_KWH and med >= lo_env
            out.append(
                BinStat(
                    lo,
                    hi,
                    direction,
                    med if confident else fallback,
                    med,
                    n,
                    dc_kwh,
                    confident,
                    "" if confident else "low_confidence",
                )
            )
        return out


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _same_band(prev_w: float, cur_w: float) -> bool:
    """True when |cur_w| stays in |prev_w|'s bin within the hysteresis slack."""
    h = const.EFFICIENCY_HYSTERESIS_W
    i = bin_index(abs(prev_w))
    edges = _bin_edges()
    lo = ([0.0, *edges])[i] - h
    hi = ([*edges, float("inf")])[i]
    hi = hi + h if hi != float("inf") else hi
    return lo - 1e-9 <= abs(cur_w) < hi


def segment_episodes(rows: list[dict]) -> list[list[dict]]:
    """Split rows into contiguous same-sign, band-stable, gap-free runs (len >= 2)."""
    max_gap_s = 2 * const.TICK_SECONDS
    runs: list[list[dict]] = []
    cur: list[dict] = []
    for row in rows:
        b = row["batt_w"]
        if abs(b) < 1e-9:
            # idle tick ends any run (no charge/discharge episode here)
            if len(cur) >= 2:
                runs.append(cur)
            cur = []
            continue
        if not cur:
            cur = [row]
            continue
        prev = cur[-1]
        gap = (_parse_ts(row["ts"]) - _parse_ts(prev["ts"])).total_seconds()
        same_sign = (b > 0) == (prev["batt_w"] > 0)
        if gap > max_gap_s or not same_sign or not _same_band(prev["batt_w"], b):
            if len(cur) >= 2:
                runs.append(cur)
            cur = [row]
        else:
            cur.append(row)
    if len(cur) >= 2:
        runs.append(cur)
    return runs


@dataclass(frozen=True)
class RunEta:
    direction: str
    dc_power_w: float
    eta: float
    dc_kwh: float


def run_eta(run: list[dict], cfg: Config) -> RunEta | None:
    """Per-run efficiency from ΔSoC (DC, BMS coulomb counter) vs. the
    energy-balance residual (AC).

    The DC side (``ΔSoC × capacity``) is independent of ``batt_w`` and thus
    of the residual. The AC side now equals ``batt_w - inverter_loss``
    (the battery-served share of house load) since the meter/house-load
    refactor made ``load_w`` a computed quantity (see the module
    docstring). That AC estimate shares its measurement basis with the
    computed load the planner schedules against, so the fitted curve maps
    planner-space AC to BMS-space DC consistently; independent AC
    metrology isn't required for that purpose. Delegates to
    ``_run_eta_impl`` for the actual gate/envelope/computation.
    """
    return _run_eta_impl(run, cfg)


def _run_eta_impl(run: list[dict], cfg: Config) -> RunEta | None:
    """Gated on a minimum |ΔSoC| (noise floor) and clamped to a physically
    plausible envelope; returns None when the run can't yield a
    trustworthy sample rather than a garbage value.

    DISCHARGE runs subtract the modeled constant standby drain
    (``cfg.idle_drain_w * duration_h``) from the DC side before computing
    eta: a discharge run's raw ΔSoC bundles both conversion loss and idle
    drain, and the planner already models idle drain separately via
    ``idle_drain_w``, so leaving it in the eta fit would double-count it
    (once via eta, once via ``idle_drain_w``). Returns None if the modeled
    idle energy would consume the run's entire ΔSoC. CHARGE runs are
    unaffected. Binning (``dc_power_w``) always uses the GROSS,
    non-debiased DC energy — only the eta ratio's denominator changes.
    """
    if len(run) < 2:
        return None
    dsoc_pct = run[-1]["soc"] - run[0]["soc"]
    if abs(dsoc_pct) < const.EFFICIENCY_DSOC_GATE_PCT:
        return None
    t0 = _parse_ts(run[0]["ts"])
    duration_h = (_parse_ts(run[-1]["ts"]) - t0).total_seconds() / 3600.0
    if duration_h <= 0.0:
        return None
    dc_kwh = cfg.pct_to_kwh(abs(dsoc_pct))
    if dc_kwh <= 0.0:
        return None
    dc_power_w = dc_kwh * 1000.0 / duration_h
    ac_wh = 0.0
    for a, b in itertools.pairwise(run):
        dt_h = (_parse_ts(b["ts"]) - _parse_ts(a["ts"])).total_seconds() / 3600.0
        ac_wh += 0.5 * (a["residual_w"] + b["residual_w"]) * dt_h
    ac_kwh = ac_wh / 1000.0
    lo, hi = const.EFFICIENCY_ENVELOPE
    if dsoc_pct > 0.0:
        ac_absorbed = -ac_kwh
        if ac_absorbed <= 0.0:
            return None
        eta = dc_kwh / ac_absorbed
        direction = "charge"
    else:
        ac_delivered = ac_kwh
        if ac_delivered <= 0.0:
            return None
        idle_kwh = cfg.idle_drain_w * duration_h / 1000.0
        dc_kwh_eff = dc_kwh - idle_kwh
        if dc_kwh_eff <= 0.0:
            return None
        eta = ac_delivered / dc_kwh_eff
        direction = "discharge"
    if not (lo <= eta <= hi):
        return None
    return RunEta(direction, dc_power_w, eta, dc_kwh)
