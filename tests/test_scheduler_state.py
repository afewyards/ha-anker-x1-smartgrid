from datetime import datetime, timezone, timedelta, UTC
from custom_components.anker_x1_smartgrid.models import Config, PlanState, ControllerState
from custom_components.anker_x1_smartgrid import scheduler

T = datetime(2026, 6, 20, 11, 0, tzinfo=UTC)


def passive(since=T):
    return PlanState(ControllerState.PASSIVE, since, ())


def forcing(since=T, slots=(T,)):
    return PlanState(ControllerState.FORCING, since, slots)


def test_decide_state_signature_has_no_deficit_kwh():
    import inspect
    from custom_components.anker_x1_smartgrid import scheduler

    assert "deficit_kwh" not in inspect.signature(scheduler.decide_state).parameters


def test_enters_forcing_when_now_selected_from_passive():
    cfg = Config()
    ps = scheduler.decide_state(
        passive(since=T - timedelta(hours=1)),
        soc=50.0,
        now=T,
        selected_slots=[T],
        cfg=cfg,
    )
    assert ps.state is ControllerState.FORCING
    assert ps.committed_slots == (T,)


def test_stays_passive_when_now_not_selected():
    cfg = Config()
    ps = scheduler.decide_state(
        passive(since=T - timedelta(hours=1)),
        soc=50.0,
        now=T,
        selected_slots=[T + timedelta(hours=2)],
        cfg=cfg,
    )
    assert ps.state is ControllerState.PASSIVE


def test_high_soc_forces_passive_even_during_dwell():
    cfg = Config(soc_target=97.0, min_dwell_min=15)
    ps = scheduler.decide_state(
        forcing(since=T),  # just entered, within dwell
        soc=96.5,
        now=T + timedelta(minutes=2),
        selected_slots=[T],
        cfg=cfg,
    )
    assert ps.state is ControllerState.PASSIVE


def test_min_dwell_keeps_forcing_before_dwell_elapsed():
    cfg = Config(min_dwell_min=15)
    ps = scheduler.decide_state(
        forcing(since=T),
        soc=60.0,
        now=T + timedelta(minutes=5),
        selected_slots=[T],
        cfg=cfg,
    )
    assert ps.state is ControllerState.FORCING  # dwell not elapsed


def test_stays_forcing_after_dwell_when_now_selected():
    # Economic-only redesign: deficit no longer gates the exit. When the current
    # hour is still selected the plan stays FORCING even if deficit < eps_lo.
    # The old test asserted PASSIVE here (eps_lo exit); that exit was removed.
    cfg = Config(min_dwell_min=15)
    ps = scheduler.decide_state(
        forcing(since=T),
        soc=60.0,
        now=T + timedelta(minutes=20),
        selected_slots=[T],
        cfg=cfg,
    )
    assert ps.state is ControllerState.FORCING


def test_commitment_keeps_forcing_when_still_in_committed_slot():
    cfg = Config(min_dwell_min=15)
    # deficit between eps_lo and eps_hi (hysteresis dead zone), dwell elapsed,
    # still inside committed slot -> stay FORCING
    ps = scheduler.decide_state(
        forcing(since=T, slots=(T,)),
        soc=70.0,
        now=T + timedelta(minutes=30),
        selected_slots=[T],
        cfg=cfg,
    )
    assert ps.state is ControllerState.FORCING


def test_forcing_exits_when_current_hour_no_longer_selected():
    # Regression: once FORCING, the price gate must keep applying. With dwell
    # elapsed, a deficit in the dead zone (eps_lo<d<eps_hi), and the current
    # hour NOT in the (price-gated) selected list -> stop, accept partial fill.
    cfg = Config(min_dwell_min=15)
    ps = scheduler.decide_state(
        forcing(since=T - timedelta(hours=1)),  # dwell elapsed
        soc=91.0,
        now=T,
        selected_slots=[],
        cfg=cfg,
    )
    assert ps.state is ControllerState.PASSIVE


def test_forcing_continues_while_current_hour_still_selected():
    cfg = Config(min_dwell_min=15)
    ps = scheduler.decide_state(
        forcing(since=T - timedelta(hours=1)),
        soc=60.0,
        now=T,
        selected_slots=[T],
        cfg=cfg,
    )
    assert ps.state is ControllerState.FORCING


# --- Economic-only charging: new tests (TDD, must fail before impl) ---


def test_enters_forcing_when_now_selected_economic_only():
    """Economic-only: FORCING fires on the DP schedule even with near-zero deficit.

    Before the change this fails: entry required deficit > eps_hi (0.4), so a
    pure economic charge with deficit=0.026 would never actuate.
    """
    cfg = Config(min_dwell_min=15)
    ps = scheduler.decide_state(
        passive(since=T - timedelta(hours=1)),
        soc=50.0,
        now=T,
        selected_slots=[T],
        cfg=cfg,
    )
    assert ps.state is ControllerState.FORCING
    assert ps.committed_slots == (T,)


def test_stays_forcing_when_now_selected_after_dwell_elapsed():
    """Economic-only: FORCING persists while now_selected even when deficit is tiny.

    Before the change this fails: the eps_lo exit (deficit < 0.2 → PASSIVE)
    fired before the now_selected check.
    """
    cfg = Config(min_dwell_min=15)
    ps = scheduler.decide_state(
        forcing(since=T - timedelta(hours=1)),
        soc=60.0,
        now=T,
        selected_slots=[T],
        cfg=cfg,
    )
    assert ps.state is ControllerState.FORCING


def test_solar_ceiling_guard_stops_charging_at_ceiling_soc():
    """Solar-reservation ceiling guard still terminates FORCING independently of deficit."""
    cfg = Config(min_dwell_min=15, soc_target=97.0)
    ps = scheduler.decide_state(
        forcing(since=T - timedelta(hours=1)),
        soc=84.0,
        now=T,
        selected_slots=[T],
        cfg=cfg,
        charge_ceiling_soc=84.0,
    )
    assert ps.state is ControllerState.PASSIVE


def test_detect_evening_peak_clamps_negative_median():
    from custom_components.anker_x1_smartgrid.models import PriceSlot

    base = datetime(2026, 6, 20, 11, 0, tzinfo=UTC)
    cfg = Config(peak_k=1.0, peak_after_hour=0)
    # Mostly-negative prices (median ≈ -0.065) then a clear positive evening peak.
    prices = [-0.10, -0.09, -0.08, -0.07, -0.06, -0.05, 0.20, 0.30]
    slots = [PriceSlot(base + timedelta(hours=i), p) for i, p in enumerate(prices)]
    peak = scheduler.detect_evening_peak(base, slots, cfg)
    # Old (threshold = median<0): returns the -0.06 slot at h=4 (a negative price!).
    # Fixed (threshold = max(median,0)=0): first rising slot with price >= 0 → h=6.
    assert peak == base + timedelta(hours=6)
