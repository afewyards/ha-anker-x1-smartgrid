"""Task 5 — overnight terminal credit live wiring in ``compute_decision``.

The DP horizon always ends at ``horizon_edge`` (last priced slot + 1h), so an
unpriced post-horizon night exists on essentially every plan.  When
``cfg.terminal_overnight_credit`` is ON, ``compute_decision`` builds one
``(dc_kwh, value)`` piecewise segment per PRICED gap hour
``[horizon_edge, next solar pickup)`` from the persistence price estimate
(spec rev-3, :func:`optimize.overnight_terminal_segments`) and threads them
through ``_dp_select_slots -> optimize_grid -> select_end_state`` so
end-of-horizon energy that serves the overnight load earns the richer
per-hour credit.  The same flag also widens the reserve ``is_cheap`` map
(``decision._build_is_cheap_by_hour``) with the estimated tail so a
truncated real price horizon's last hour no longer reads "cheap" purely
because there is no later real data to compare against (design doc point 4,
"is_cheap tail collapse").

Scenario design
---------------
``now = 14:00`` gives a price horizon that crosses midnight (ends ~03:00).  The
degraded-data synthetic reserve extension is then suppressed (the real horizon
already runs past midnight), so the ride-out reserve stays low and does NOT
mask the soft terminal credit — the credit becomes the binding lever, exactly
the pre-publication regime the feature targets.  Behavioural scenarios read the
DP's chosen terminal SoC directly via a ``select_end_state`` spy, which bypasses
display-SoC clamping and grid-charge tie-break noise.

Scenarios:
  * threading pin — the real per-hour segments reach the builder, ``optimize_grid`` + ``_out``
  * est hour above the hold-vs-burst hurdle → that hour's load slice is held
  * est hour below the hurdle               → the tall evening peak still bursts
  * truncated real horizon ("stub") + a genuine estimated trough deep in the
    gap → the widened is_cheap map stops the reserve from collapsing at the
    stub's edge
  * is_cheap map is widened only when the flag is ON
  * flag explicitly False → terminal_segments=None → byte-identical legacy
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.util import dt as dt_util

from custom_components.anker_x1_smartgrid import decision
from custom_components.anker_x1_smartgrid import optimize as optimize_mod
from custom_components.anker_x1_smartgrid import pricing_store
from custom_components.anker_x1_smartgrid.decision import _next_synthetic_pickup, compute_decision
from custom_components.anker_x1_smartgrid.forecast import LoadPredictor
from custom_components.anker_x1_smartgrid.models import (
    Config,
    ControllerState,
    PlantInputs,
    PlanState,
    PriceSlot,
)

BASE = datetime(2026, 7, 18, 14, 0, tzinfo=UTC)  # 14:00 UTC → horizon crosses midnight
_PREDICTOR = LoadPredictor.from_profile({})

# 13 hourly slots (14:00..02:00 next day): shoulders, an evening export staircase
# with a tall 22:00 peak (the best in-window export = v_hi clamp), then a cheap
# after-midnight tail (sets a low v_lo).
#   idx  0    1    2    3   | 4    5    6    7    8   | 9    10   11   12
#   hr   14   15   16   17  | 18   19   20   21   22  | 23   00   01   02
_PRICES = [0.20, 0.20, 0.20, 0.20, 0.28, 0.30, 0.32, 0.40, 0.45, 0.20, 0.18, 0.15, 0.13]
_PEAK_HOUR = BASE + timedelta(hours=8)  # 22:00, the 0.45 slot
_HORIZON_EDGE = BASE + timedelta(hours=13)  # 03:00 next day (last slot 02:00 + 1h)

# Gap UTC hours = [03:00, 08:00) (horizon_edge → synthetic pickup at 08:00 UTC).
_GAP_UTC_HOURS = (3, 4, 5, 6, 7)


def _estimate(gap_prices: list[float], fallback: float = 0.05) -> list[float]:
    """24-entry hour-of-LOCAL-day estimate: one entry per ``_GAP_UTC_HOURS``
    index (in the given order), ``fallback`` everywhere else.

    ``build_estimated_slots`` indexes the estimate by ``as_local(h).hour``, so
    the mapping is keyed to the LOCAL hour each gap UTC hour maps to under the
    harness default timezone (built at call time, not import time).
    """
    est = [fallback] * 24
    for utc_h, price in zip(_GAP_UTC_HOURS, gap_prices):
        local_h = dt_util.as_local(datetime(2026, 7, 19, utc_h, 0, tzinfo=UTC)).hour
        est[local_h] = price
    return est


def _expensive_estimate() -> list[float]:
    """24-entry hour-of-LOCAL-day estimate: expensive + gently sloped over the gap
    hours, cheap elsewhere.

    The downward slope keeps every gap hour genuinely priced (no flat-band
    coincidences) so the segments builder always has real per-hour data to
    chew on.
    """
    return _estimate([0.40, 0.40, 0.40, 0.40, 0.30])


def _hold_hurdle(cfg: Config, prices: list[float]) -> float:
    """Hold-vs-burst hurdle price for a gap hour: ``ep_peak_eff - cycle_cost/eta_d``.

    A gap-hour estimate priced above this makes the DP prefer crediting that
    hour's load-serving slice via the terminal segment over exporting it at
    the best in-window peak; below it, exporting still wins (the F1 over-hold
    guard).
    """
    eta_d = cfg.eta_discharge_static()
    ep_peak_eff = optimize_mod.effective_export_price(max(prices), cfg)
    return ep_peak_eff - cfg.cycle_cost_eur_per_kwh / eta_d


def _cfg(**overrides) -> Config:
    return Config.from_dict(
        {
            "capacity_kwh": 10.0,
            "soc_target": 97.0,
            "soc_floor": 5.0,  # floor_kwh == firmware_floor_kwh (0.5) → cheap-est parity
            "eta_charge": 0.92,
            "round_trip_eff": 0.85,
            "min_dwell_min": 0,
            "max_charge_w": 6000.0,
            "enable_export": True,
            "export_fee_eur_per_kwh": 0.02,
            "export_min_block_kwh": 0.0,  # disable the sub-block filter (deterministic)
            "cycle_cost_eur_per_kwh": 0.10,
            **overrides,
        }
    )


def _slots(prices: list[float]) -> list[PriceSlot]:
    return [PriceSlot(BASE + timedelta(hours=i), p) for i, p in enumerate(prices)]


def _plan() -> PlanState:
    return PlanState(ControllerState.PASSIVE, BASE - timedelta(hours=2), ())


def _call(
    cfg: Config,
    *,
    soc: float,
    prices: list[float],
    estimated_tomorrow=None,
    out=None,
    export_price=0.30,
    sun_times=None,
):
    inputs = PlantInputs(soc=soc, meter_w=0.0, now=BASE)
    sunset = BASE + timedelta(hours=len(prices))
    return compute_decision(
        _plan(),
        inputs,
        _slots(prices),
        0.0,  # pv_remaining → no solar pickup → synthetic overnight gap
        sunset,
        _PREDICTOR,
        None,
        cfg,
        export_price=export_price,
        export_price_matches_import=True,
        estimated_tomorrow=estimated_tomorrow,
        _out=out,
        sun_times=sun_times,
    )


def _end_state_kwh(cfg: Config, *, soc: float, prices: list[float], estimated_tomorrow=None) -> tuple[float, dict]:
    """Run compute_decision and return the DP's chosen terminal SoC (DC kWh).

    Spies on ``optimize.select_end_state`` — invoked once by the live DP — to read
    ``from_bin(best_end_b)`` directly, bypassing the reconstructed/clamped display
    horizon.  Returns ``(end_kwh, _out)``.
    """
    real = optimize_mod.select_end_state
    captured: list[float] = []

    def _spy(*args, **kwargs):
        result = real(*args, **kwargs)
        captured.append(kwargs["from_bin"](result[0]))
        return result

    optimize_mod.select_end_state = _spy
    try:
        out: dict = {}
        _call(cfg, soc=soc, prices=prices, estimated_tomorrow=estimated_tomorrow, out=out)
    finally:
        optimize_mod.select_end_state = real
    assert captured, "the live DP must call select_end_state exactly once"
    return captured[-1], out


def _row_start(row: dict):
    return datetime.fromisoformat(row["start"])


# ---------------------------------------------------------------------------
# 1. Threading pin: the real per-hour segments reach the builder, optimize_grid
#    + _out.
# ---------------------------------------------------------------------------


def test_wiring_threads_params_to_optimize_grid_and_out(monkeypatch):
    """The segments built in the wv block flow to optimize_grid and _out.

    Wraps the REAL ``overnight_terminal_segments`` builder (records its
    return value) and the REAL ``optimize_grid`` (records the pass-through
    kwargs), so the test pins the wiring end-to-end against the actual DP
    economics rather than a sentinel.

    Task 4 removed optimize_grid's water_value_hi/overnight_need_kwh params
    in favor of terminal_segments; decision.py's own forwarding into
    optimize_grid was silenced (not yet re-wired to terminal_segments — that
    landed in Task 5). This test now asserts the REAL segments threading.
    """
    captured_builder_returns: list = []
    captured_dp: dict = {}

    real_builder = optimize_mod.overnight_terminal_segments

    def _spy_builder(*args, **kwargs):
        result = real_builder(*args, **kwargs)
        captured_builder_returns.append(result)
        return result

    real_dp = optimize_mod.optimize_grid

    def _spy_dp(*args, **kwargs):
        captured_dp.clear()
        captured_dp.update(kwargs)
        return real_dp(*args, **kwargs)

    monkeypatch.setattr(optimize_mod, "overnight_terminal_segments", _spy_builder)
    monkeypatch.setattr(optimize_mod, "optimize_grid", _spy_dp)

    cfg = _cfg()
    est = _expensive_estimate()
    out: dict = {}
    _call(cfg, soc=80.0, prices=_PRICES, estimated_tomorrow=est, out=out)

    assert captured_builder_returns, "the live DP path must call overnight_terminal_segments"
    segments, need, v_hi_mean = captured_builder_returns[-1]
    assert segments, "gap must be priced by the estimate (non-empty segments)"

    # ...the REAL segments reach optimize_grid verbatim...
    assert captured_dp.get("terminal_segments") == segments
    # ...and the old (Task 4-removed) kwarg names never reach it.
    assert "water_value_hi" not in captured_dp
    assert "overnight_need_kwh" not in captured_dp

    # ..._out stashes match the builder's own return values.
    assert out["terminal_v_hi"] == pytest.approx(v_hi_mean)
    assert out["terminal_need_kwh"] == pytest.approx(need)
    assert out["terminal_segments"] == segments


# ---------------------------------------------------------------------------
# 2. Est hour above the hold-vs-burst hurdle → the DP holds that hour's slice
# ---------------------------------------------------------------------------


def test_est_hour_above_hurdle_held():
    """A gap-hour estimate priced above the hold-vs-burst hurdle
    (``ep_peak_eff - cycle_cost/eta_d``) makes the DP retain that hour's
    load-serving slice at the end of the horizon, instead of liquidating
    everything through the evening-peak export like the cheap baseline.
    """
    cfg = _cfg()
    hurdle = _hold_hurdle(cfg, _PRICES)
    # Only the FIRST gap hour is priced rich (well above the hurdle); the
    # rest stay cheap so the "held" and "sold" runs differ by exactly one
    # segment's worth of credit.
    est_held = _estimate([hurdle + 0.40, 0.05, 0.05, 0.05, 0.05])
    est_sold = _estimate([0.05] * 5)

    end_held, out_held = _end_state_kwh(cfg, soc=90.0, prices=_PRICES, estimated_tomorrow=est_held)
    end_sold, out_sold = _end_state_kwh(cfg, soc=90.0, prices=_PRICES, estimated_tomorrow=est_sold)

    held_segments = out_held["terminal_segments"]
    assert held_segments, "the rich gap hour must produce a segment"
    held_slice_kwh = out_held["terminal_need_kwh"]
    assert held_slice_kwh > 0.0, "the rich hour's slice must count toward the overnight need"
    # The cheap baseline's segments are all floored at v_lo -> no genuine need.
    assert out_sold["terminal_need_kwh"] == 0.0

    # The rich estimate retains materially more energy than the cheap
    # baseline -- well clear of a single DP bin (~0.05 kWh) so this is not
    # rounding noise.
    assert end_held > end_sold + 0.1
    # Sanity: the held run's terminal SoC is consistent with retaining at
    # least the rich slice above the hard firmware floor.
    assert end_held >= cfg.firmware_floor_kwh + held_slice_kwh - 1e-6


# ---------------------------------------------------------------------------
# 3. Est hour below the hurdle → the tall evening peak still bursts (F1 guard)
# ---------------------------------------------------------------------------


def test_est_below_hurdle_sold():
    """A gap-hour estimate priced below the hold-vs-burst hurdle must not
    suppress the tall evening-peak export -- the burst still fires.

    Guards the F1 over-hold band: a merely decent (but sub-hurdle) overnight
    estimate must not out-value a genuinely profitable peak-hour export.
    """
    cfg = _cfg()
    hurdle = _hold_hurdle(cfg, _PRICES)
    est = _estimate([hurdle - 0.10] * 5)  # clearly below the hurdle, with margin

    out: dict = {}
    _call(cfg, soc=90.0, prices=_PRICES, estimated_tomorrow=est, out=out)

    assert out.get("terminal_segments"), "gap must be priced (mechanism engaged) for this to be a real check"
    assert _PEAK_HOUR in out.get("export_request", {}), (
        f"tall 0.45 peak must still export; got {sorted(out.get('export_request', {}))}"
    )


# ---------------------------------------------------------------------------
# 4. Truncated real horizon ("stub") → widened is_cheap prevents the reserve
#    from collapsing at the stub's edge (design doc point 4).
# ---------------------------------------------------------------------------

# A short real price stub: only 3 hours (14:00, 15:00, 16:00 -> horizon_edge
# 17:00). Its own last hour (0.13) is trivially "cheap" relative to itself
# under a real-only is_cheap map -- there is no later real data to compare
# against -- even though it is far from the genuine overnight relief.
_STUB_PRICES = [0.20, 0.20, 0.13]


def test_truncated_reserve_no_collapse():
    """With only a short real price stub, the real-only is_cheap map's
    forward-min window has nothing past the stub to compare against, so the
    stub's last hour reads "cheap" trivially and the ride-out walk breaks
    there -- collapsing the reserve to a few hours instead of the true
    overnight need. Widening the map with the estimated tail (flag ON) fixes
    this: a genuine estimated trough deep in the gap (near the solar pickup)
    pulls the forward-min down so the stub's tail no longer reads cheap, and
    the walk correctly carries the reserve through to the real relief point.
    """
    cfg_on = _cfg()  # terminal_overnight_credit defaults ON
    cfg_off = _cfg(terminal_overnight_credit=False)
    # Expensive gap except right before the synthetic pickup (08:00 UTC) --
    # the genuine relief sits deep in the gap, not at the real-price edge.
    est = _estimate([0.30, 0.30, 0.30, 0.30, 0.02])

    _, _, _, horizon_on, _, _ = _call(cfg_on, soc=80.0, prices=_STUB_PRICES, estimated_tomorrow=est)
    _, _, _, horizon_off, _, _ = _call(cfg_off, soc=80.0, prices=_STUB_PRICES, estimated_tomorrow=est)

    row_on = next(r for r in horizon_on if _row_start(r) == BASE)
    row_off = next(r for r in horizon_off if _row_start(r) == BASE)

    # OFF: real-only is_cheap trivially flags the stub's last hour cheap --
    # the reserve collapses to (near) the short stub's own ride-out.
    assert row_off["reserve_soc"] < 20.0
    # ON: widened is_cheap sees past the stub into the genuine overnight need
    # -- reserve at this (burst) hour reaches materially further than the
    # collapsed OFF figure.
    assert row_on["reserve_soc"] > 50.0
    assert row_on["reserve_soc"] > row_off["reserve_soc"] + 20.0


# ---------------------------------------------------------------------------
# 5. The is_cheap map is widened with the estimated tail ONLY when the flag
#    is ON.
# ---------------------------------------------------------------------------


def test_is_cheap_real_only_when_flag_off(monkeypatch):
    """``_build_is_cheap_by_hour`` receives ``slots + est slots`` only when
    ``terminal_overnight_credit`` is ON; flag OFF keeps today's real-only map
    (same slot count as the real price horizon)."""
    real_build = decision._build_is_cheap_by_hour
    captured_lens: list[int] = []

    def _spy(slots, cfg, slot_minutes=60):
        captured_lens.append(len(slots))
        return real_build(slots, cfg, slot_minutes)

    monkeypatch.setattr(decision, "_build_is_cheap_by_hour", _spy)

    est = _expensive_estimate()
    pickup = _next_synthetic_pickup(_HORIZON_EDGE)
    n_est_slots = len(pricing_store.build_estimated_slots(est, _HORIZON_EDGE, pickup))
    assert n_est_slots > 0, "the gap must be estimate-priced for this to be a real check"

    cfg_on = _cfg()  # terminal_overnight_credit defaults ON
    captured_lens.clear()
    _call(cfg_on, soc=80.0, prices=_PRICES, estimated_tomorrow=est)
    assert captured_lens, "is_cheap map must be built (reserve_anchor defaults to trough)"
    assert captured_lens[-1] == len(_PRICES) + n_est_slots

    cfg_off = _cfg(terminal_overnight_credit=False)
    captured_lens.clear()
    _call(cfg_off, soc=80.0, prices=_PRICES, estimated_tomorrow=est)
    assert captured_lens, "is_cheap map must still be built when the credit flag is off"
    assert captured_lens[-1] == len(_PRICES)


# ---------------------------------------------------------------------------
# 6. Flag explicitly False → byte-identical legacy terminal
# ---------------------------------------------------------------------------


def test_flag_off_byte_identical(monkeypatch):
    """Flag False → terminal_segments=None, builder never called, legacy schedule."""
    calls: list = []
    real_builder = optimize_mod.overnight_terminal_segments

    def _tracking_builder(*a, **k):
        calls.append(1)
        return real_builder(*a, **k)

    monkeypatch.setattr(optimize_mod, "overnight_terminal_segments", _tracking_builder)

    cfg = _cfg(terminal_overnight_credit=False)
    out_est: dict = {}
    out_none: dict = {}
    _call(cfg, soc=90.0, prices=_PRICES, estimated_tomorrow=_expensive_estimate(), out=out_est)
    _call(cfg, soc=90.0, prices=_PRICES, estimated_tomorrow=None, out=out_none)

    assert calls == [], "builder must not run when the flag is OFF"
    assert out_est["terminal_v_hi"] is None
    assert out_est["terminal_need_kwh"] == 0.0
    assert out_est["terminal_segments"] is None
    # The estimate must not leak into the DP result when the flag is OFF.
    assert out_est["export_request"] == out_none["export_request"]
    assert out_est["grid_request"] == out_none["grid_request"]
    assert out_est["dp_selected"] == out_none["dp_selected"]


# ---------------------------------------------------------------------------
# 7. Task 3 — the estimated tomorrow tail reaches the live DISPLAY horizon
#
# ``compute_decision`` only threads the tail through ``build_display_horizon``
# (the ``sun_times is not None`` branch); the degraded ``sun_times=None``
# fallback (``build_plan_horizon`` called directly) is untouched. ``sun_times``
# below is a bare non-None tuple with no today/tomorrow PV arrays, so the
# two-day curve is empty and the gap/pickup math is identical to the other
# tests in this file (still the [03:00, 08:00) UTC synthetic gap) — only the
# horizon-assembly branch taken inside ``compute_decision`` changes.
# ---------------------------------------------------------------------------

_SUN_TIMES = (
    BASE + timedelta(hours=9),  # today_sunset (23:00) — unused: no PV arrays
    BASE + timedelta(hours=18),  # tomorrow_sunrise (08:00 next day)
    BASE + timedelta(hours=30),  # tomorrow_sunset (20:00 next day)
)


def test_live_horizon_grows_est_tail():
    """sun_times present + estimate + credit ON → horizon rows extend past
    horizon_edge, every tail row is ``estimated=True``, and tail prices equal
    the estimated-slot prices built for the gap (no display munging)."""
    cfg = _cfg()  # terminal_overnight_credit defaults ON
    est = _expensive_estimate()
    _, _, _, horizon, _, _ = _call(cfg, soc=80.0, prices=_PRICES, estimated_tomorrow=est, sun_times=_SUN_TIMES)

    assert horizon, "expected a non-empty horizon"
    assert max(_row_start(r) for r in horizon) > _HORIZON_EDGE

    real_rows = [r for r in horizon if _row_start(r) < _HORIZON_EDGE]
    tail_rows = [r for r in horizon if _row_start(r) >= _HORIZON_EDGE]
    assert tail_rows, "expected estimated tail rows appended past horizon_edge"
    assert all(r["estimated"] is True for r in tail_rows)
    assert all(r["estimated"] is False for r in real_rows)

    pickup = _next_synthetic_pickup(_HORIZON_EDGE)
    expected_prices = {s.start: s.price for s in pricing_store.build_estimated_slots(est, _HORIZON_EDGE, pickup)}
    assert {_row_start(r) for r in tail_rows} == set(expected_prices)
    for row in tail_rows:
        assert row["price"] == pytest.approx(expected_prices[_row_start(row)])
    # Deferred minor: the DP never plans charge/export against estimated rows —
    # they only ever hold or drain (see test_tail_soc_continues_from_last_real_row).
    for row in tail_rows:
        assert row["grid_charge_kwh"] == 0.0
        assert row["grid_export_kwh"] == 0.0


def test_dp_never_sees_estimated_slots(monkeypatch):
    """Core wave invariant: with the estimated tail ACTIVE (flag on + estimate +
    sun_times set, the same scenario as ``test_live_horizon_grows_est_tail``),
    ``decision._dp_select_slots`` must be invoked with ONLY the real price
    slots — the estimated tail must never reach the DP, no matter that the
    display horizon appends it afterwards."""
    cfg = _cfg()  # terminal_overnight_credit defaults ON
    est = _expensive_estimate()

    real_dp_select_slots = decision._dp_select_slots
    captured: list[list[PriceSlot]] = []

    def _spy(*args, **kwargs):
        captured.append(kwargs["slots"])
        return real_dp_select_slots(*args, **kwargs)

    monkeypatch.setattr(decision, "_dp_select_slots", _spy)
    _call(cfg, soc=80.0, prices=_PRICES, estimated_tomorrow=est, sun_times=_SUN_TIMES)

    assert captured, "the live DP path must call _dp_select_slots exactly once"
    dp_slots = captured[-1]

    pickup = _next_synthetic_pickup(_HORIZON_EDGE)
    est_starts = {s.start for s in pricing_store.build_estimated_slots(est, _HORIZON_EDGE, pickup)}
    assert est_starts, "the gap must be estimate-priced (non-empty) for this to be a real check"

    real_starts = {s.start for s in _slots(_PRICES)}
    dp_starts = {s.start for s in dp_slots}
    assert dp_starts == real_starts
    assert len(dp_slots) == len(_PRICES)
    assert not (dp_starts & est_starts), "no estimated slot start may reach the DP"
    assert max(dp_starts) < min(est_starts)


def test_no_estimate_no_tail():
    """estimated_tomorrow=None → no est slots are built → last row unchanged."""
    cfg = _cfg()
    _, _, _, horizon, _, _ = _call(cfg, soc=80.0, prices=_PRICES, estimated_tomorrow=None, sun_times=_SUN_TIMES)

    assert horizon
    assert max(_row_start(r) for r in horizon) == BASE + timedelta(hours=len(_PRICES) - 1)
    assert all(r["estimated"] is False for r in horizon)


def test_flag_off_no_tail():
    """terminal_overnight_credit=False → _est_slot_list never built → no tail
    even though an estimate is supplied."""
    cfg = _cfg(terminal_overnight_credit=False)
    est = _expensive_estimate()
    _, _, _, horizon, _, _ = _call(cfg, soc=80.0, prices=_PRICES, estimated_tomorrow=est, sun_times=_SUN_TIMES)

    assert horizon
    assert max(_row_start(r) for r in horizon) == BASE + timedelta(hours=len(_PRICES) - 1)
    assert all(r["estimated"] is False for r in horizon)


def test_tail_soc_continues_from_last_real_row():
    """The SoC walk is continuous across the real→tail boundary: the first
    estimated row's soc picks up from (does not reset above) the last real
    row's soc — no charge/export is planned in the tail, so it can only hold
    or drain."""
    cfg = _cfg()
    est = _expensive_estimate()
    _, _, _, horizon, _, _ = _call(cfg, soc=80.0, prices=_PRICES, estimated_tomorrow=est, sun_times=_SUN_TIMES)

    real_rows = sorted((r for r in horizon if not r["estimated"]), key=_row_start)
    tail_rows = sorted((r for r in horizon if r["estimated"]), key=_row_start)
    assert real_rows and tail_rows
    assert tail_rows[0]["soc"] <= real_rows[-1]["soc"]
