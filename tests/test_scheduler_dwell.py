"""TDD tests for N1 (HIGH, control-plane): dwell short-circuits must not bypass
the per-slot price gate when the configured dwell >= the slot length.

Both ``decide_state`` (charge/FORCING) and ``decide_export_state`` (export)
gate their "within dwell -> stay" short-circuit on ``now - state_since``. When
the configured dwell (``cfg.min_dwell_min`` / ``cfg.export_dwell_min``) is >=
``slot_minutes``, that short-circuit can span an entire unselected slot
without ever re-checking ``now_selected`` / ``hurdle_clears`` — holding
FORCING/export engaged straight through slots the DP never selected.

The fix clamps the EFFECTIVE dwell strictly below ``slot_minutes`` at both
call sites, so within one slot boundary crossing the price gate always gets a
chance to re-evaluate. At slot_minutes=60 with the 15-min default this is a
no-op (15 < 59) -> byte-identical behaviour (pinned below).
"""

from datetime import timedelta, datetime, UTC

import pytest

from custom_components.anker_x1_smartgrid.models import Config, PlanState, ControllerState, ExportState
from custom_components.anker_x1_smartgrid import scheduler

T = datetime(2026, 6, 20, 10, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _reset_dwell_clamp_log_cache():
    """The one-time-clamp-log dedup cache is module-level and process-lifetime
    by design (avoids spamming every tick in production). Reset it around each
    test so the "logs once" assertions are independent of test execution order
    (multiple tests below intentionally reuse the same (min_dwell_min=30,
    slot_minutes=15) combo).
    """
    scheduler._DWELL_CLAMP_LOGGED.clear()
    yield
    scheduler._DWELL_CLAMP_LOGGED.clear()


def forcing(since, slots=(T,)):
    return PlanState(ControllerState.FORCING, since, slots)


# ---------------------------------------------------------------------------
# decide_state: dwell must not outlive the slot at 15-min resolution
# ---------------------------------------------------------------------------


def test_dwell_ge_slot_length_does_not_survive_unselected_slot_boundary():
    """Mirrors the reviewer's repro: slot_minutes=15, dwell=30. DP selects only
    the 10:15 quarter. FORCING entered mid-slot at 10:15:30. By the 10:35 tick
    (past the unselected 10:30 slot boundary) the state must be PASSIVE, not
    still FORCING from the oversized dwell short-circuit.
    """
    cfg = Config(min_dwell_min=30)
    entered_at = T + timedelta(minutes=15, seconds=30)  # 10:15:30
    tick = T + timedelta(minutes=35)  # 10:35:00

    ps = scheduler.decide_state(
        forcing(since=entered_at, slots=(T + timedelta(minutes=15),)),
        soc=60.0,
        now=tick,
        selected_slots=[T + timedelta(minutes=15)],  # only 10:15 was ever selected
        cfg=cfg,
        slot_minutes=15,
    )
    assert ps.state is ControllerState.PASSIVE


def test_dwell_clamped_still_holds_within_the_same_selected_slot():
    """Sanity: within the SAME (still-selected) slot the clamp must not force
    an early exit — clamping is about not outliving the slot boundary, not
    about shrinking dwell below what's needed within a slot.
    """
    cfg = Config(min_dwell_min=30)
    entered_at = T + timedelta(minutes=15, seconds=30)  # 10:15:30
    tick = T + timedelta(minutes=15, seconds=45)  # still inside the 10:15 slot

    ps = scheduler.decide_state(
        forcing(since=entered_at, slots=(T + timedelta(minutes=15),)),
        soc=60.0,
        now=tick,
        selected_slots=[T + timedelta(minutes=15)],
        cfg=cfg,
        slot_minutes=15,
    )
    assert ps.state is ControllerState.FORCING


def test_60min_slot_default_dwell_is_byte_identical_noop():
    """At slot_minutes=60 (the default) with the 15-min default dwell, the
    clamp ceiling (59) never binds -> the 15-min dwell boundary is pinned
    exactly as before the fix.
    """
    cfg = Config(min_dwell_min=15)
    since = T

    # Just under 15 min: dwell not elapsed -> stays FORCING even though the
    # current hour is no longer selected.
    ps_before = scheduler.decide_state(
        forcing(since=since, slots=(T,)),
        soc=60.0,
        now=since + timedelta(minutes=14, seconds=59),
        selected_slots=[],
        cfg=cfg,
    )
    assert ps_before.state is ControllerState.FORCING

    # Exactly at 15 min: dwell elapsed, current hour not selected -> PASSIVE.
    ps_after = scheduler.decide_state(
        forcing(since=since, slots=(T,)),
        soc=60.0,
        now=since + timedelta(minutes=15),
        selected_slots=[],
        cfg=cfg,
    )
    assert ps_after.state is ControllerState.PASSIVE


def test_60min_slot_default_dwell_does_not_log_clamp_warning(caplog):
    cfg = Config(min_dwell_min=15)
    with caplog.at_level("WARNING"):
        scheduler.decide_state(
            forcing(since=T, slots=(T,)),
            soc=60.0,
            now=T + timedelta(minutes=20),
            selected_slots=[],
            cfg=cfg,
            slot_minutes=60,
        )
    assert "clamp" not in caplog.text.lower()


def test_dwell_clamp_logs_once_when_it_binds(caplog):
    cfg = Config(min_dwell_min=30)
    with caplog.at_level("WARNING"):
        for _ in range(3):
            scheduler.decide_state(
                forcing(since=T, slots=(T,)),
                soc=60.0,
                now=T + timedelta(minutes=1),
                selected_slots=[T],
                cfg=cfg,
                slot_minutes=15,
            )
    assert "clamp" in caplog.text.lower()
    assert caplog.text.lower().count("clamp") == 1


# ---------------------------------------------------------------------------
# decide_export_state: same shape
# ---------------------------------------------------------------------------


def engaged(since):
    return ExportState(engaged=True, state_since=since)


def export_cfg(**overrides):
    defaults = dict(export_eps_lo_kwh=0.2, export_eps_hi_kwh=0.4, export_dwell_min=30)
    defaults.update(overrides)
    return Config(**defaults)


def test_export_dwell_ge_slot_length_does_not_survive_unselected_slot_boundary():
    """Mirrors the charge-side repro for export: engaged mid the 10:15 slot,
    hurdle only clears for that slot. By the 10:35 tick (past the unselected
    10:30 boundary, hurdle no longer clears) export must disengage even though
    the raw 30-min dwell has not elapsed since 10:15:30.
    """
    c = export_cfg()
    entered_at = T + timedelta(minutes=15, seconds=30)  # 10:15:30
    tick = T + timedelta(minutes=35)  # 10:35:00

    result = scheduler.decide_export_state(
        engaged(since=entered_at),
        surplus_kwh=0.5,  # well above eps_lo -- surplus alone would not disengage
        hurdle_clears=False,  # DP never committed export for the 10:30 slot
        now=tick,
        cfg=c,
        slot_minutes=15,
    )
    assert result.engaged is False


def test_export_dwell_clamped_still_holds_within_the_same_slot():
    c = export_cfg()
    entered_at = T + timedelta(minutes=15, seconds=30)
    tick = T + timedelta(minutes=15, seconds=45)  # still inside the 10:15 slot

    result = scheduler.decide_export_state(
        engaged(since=entered_at),
        surplus_kwh=0.5,
        hurdle_clears=True,
        now=tick,
        cfg=c,
        slot_minutes=15,
    )
    assert result.engaged is True


def test_export_60min_slot_default_dwell_is_byte_identical_noop():
    c = export_cfg(export_dwell_min=15)
    since = T

    # Just under 15 min: dwell not elapsed -> stays engaged despite hurdle drop.
    result_before = scheduler.decide_export_state(
        engaged(since=since),
        surplus_kwh=0.5,
        hurdle_clears=False,
        now=since + timedelta(minutes=14, seconds=59),
        cfg=c,
    )
    assert result_before.engaged is True

    # Exactly at 15 min: dwell elapsed, hurdle dropped -> disengage.
    result_after = scheduler.decide_export_state(
        engaged(since=since),
        surplus_kwh=0.5,
        hurdle_clears=False,
        now=since + timedelta(minutes=15),
        cfg=c,
    )
    assert result_after.engaged is False
