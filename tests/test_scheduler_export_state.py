"""TDD tests for C2: export engage dwell / hysteresis state machine.

Tests:
- ExportState dataclass fields (engaged, state_since)
- decide_export_state: engage above eps_hi with hurdle+dwell
- decide_export_state: no engage in [eps_lo, eps_hi] band (hysteresis dead zone)
- decide_export_state: stay engaged when surplus still above eps_lo (dwell honored)
- decide_export_state: disengage when surplus drops below eps_lo after dwell
- decide_export_state: dwell prevents premature disengage even below eps_lo
- decide_export_state: hurdle dropping mid-engage releases state
- decide_export_state: surplus in dead zone with no prior engagement stays disengaged
"""
from datetime import datetime, timezone, timedelta


from custom_components.anker_x1_smartgrid.models import Config, ExportState
from custom_components.anker_x1_smartgrid import scheduler

T = datetime(2026, 6, 25, 10, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def disengaged(since=T) -> ExportState:
    return ExportState(engaged=False, state_since=since)


def engaged(since=T) -> ExportState:
    return ExportState(engaged=True, state_since=since)


def cfg(**overrides) -> Config:
    defaults = dict(export_eps_lo_kwh=0.2, export_eps_hi_kwh=0.4, export_dwell_min=15)
    defaults.update(overrides)
    return Config(**defaults)


# ---------------------------------------------------------------------------
# ExportState dataclass
# ---------------------------------------------------------------------------


def test_export_state_fields():
    """ExportState has engaged:bool and state_since:datetime."""
    es = ExportState(engaged=True, state_since=T)
    assert es.engaged is True
    assert es.state_since == T


def test_export_state_defaults_false():
    """ExportState default engaged=False."""
    es = ExportState(engaged=False, state_since=T)
    assert not es.engaged


# ---------------------------------------------------------------------------
# Engage: above eps_hi + hurdle + dwell
# ---------------------------------------------------------------------------


def test_engages_when_surplus_above_hi_hurdle_clears_dwell_elapsed():
    """Surplus > eps_hi AND hurdle_clears AND dwell elapsed → engage."""
    c = cfg()
    prev = disengaged(since=T - timedelta(minutes=20))  # dwell elapsed
    result = scheduler.decide_export_state(
        prev,
        surplus_kwh=0.5,  # > eps_hi (0.4)
        hurdle_clears=True,
        now=T,
        cfg=c,
    )
    assert result.engaged is True
    assert result.state_since == T


def test_stays_disengaged_when_surplus_below_hi():
    """Surplus ≤ eps_hi → no engage even with hurdle + dwell."""
    c = cfg()
    prev = disengaged(since=T - timedelta(minutes=20))
    result = scheduler.decide_export_state(
        prev,
        surplus_kwh=0.4,  # == eps_hi, not strictly above
        hurdle_clears=True,
        now=T,
        cfg=c,
    )
    assert result.engaged is False


def test_stays_disengaged_when_surplus_in_dead_zone():
    """Surplus in [eps_lo, eps_hi] → stays disengaged (hysteresis dead zone)."""
    c = cfg()
    prev = disengaged(since=T - timedelta(minutes=20))
    result = scheduler.decide_export_state(
        prev,
        surplus_kwh=0.3,  # between eps_lo (0.2) and eps_hi (0.4)
        hurdle_clears=True,
        now=T,
        cfg=c,
    )
    assert result.engaged is False


def test_stays_disengaged_when_hurdle_fails():
    """Surplus > eps_hi but hurdle_clears=False → no engage."""
    c = cfg()
    prev = disengaged(since=T - timedelta(minutes=20))
    result = scheduler.decide_export_state(
        prev,
        surplus_kwh=0.5,
        hurdle_clears=False,
        now=T,
        cfg=c,
    )
    assert result.engaged is False


def test_stays_disengaged_during_dwell():
    """Surplus > eps_hi + hurdle + dwell NOT elapsed → no engage yet."""
    c = cfg()
    prev = disengaged(since=T - timedelta(minutes=5))  # dwell not elapsed (need 15)
    result = scheduler.decide_export_state(
        prev,
        surplus_kwh=0.5,
        hurdle_clears=True,
        now=T,
        cfg=c,
    )
    assert result.engaged is False


# ---------------------------------------------------------------------------
# Stay engaged: surplus above eps_lo
# ---------------------------------------------------------------------------


def test_stays_engaged_when_surplus_above_lo_dwell_elapsed():
    """Once engaged, surplus > eps_lo after dwell → stays engaged."""
    c = cfg()
    prev = engaged(since=T - timedelta(minutes=20))
    result = scheduler.decide_export_state(
        prev,
        surplus_kwh=0.3,  # above eps_lo (0.2), in dead zone → stay engaged
        hurdle_clears=True,
        now=T,
        cfg=c,
    )
    assert result.engaged is True


def test_stays_engaged_during_dwell_even_below_lo():
    """Once engaged, surplus dropped below eps_lo but dwell not elapsed → stay."""
    c = cfg()
    prev = engaged(since=T - timedelta(minutes=5))  # dwell not elapsed
    result = scheduler.decide_export_state(
        prev,
        surplus_kwh=0.1,  # below eps_lo
        hurdle_clears=True,
        now=T,
        cfg=c,
    )
    assert result.engaged is True  # dwell blocks disengage


# ---------------------------------------------------------------------------
# Disengage: surplus drops below eps_lo after dwell
# ---------------------------------------------------------------------------


def test_disengages_when_surplus_below_lo_dwell_elapsed():
    """Once engaged, surplus < eps_lo after dwell → disengage."""
    c = cfg()
    prev = engaged(since=T - timedelta(minutes=20))
    result = scheduler.decide_export_state(
        prev,
        surplus_kwh=0.1,  # below eps_lo (0.2)
        hurdle_clears=True,
        now=T,
        cfg=c,
    )
    assert result.engaged is False
    assert result.state_since == T


def test_disengages_exactly_at_eps_lo_boundary():
    """Surplus == eps_lo boundary: not strictly below, so stay engaged."""
    c = cfg()
    prev = engaged(since=T - timedelta(minutes=20))
    result = scheduler.decide_export_state(
        prev,
        surplus_kwh=0.2,  # == eps_lo exactly
        hurdle_clears=True,
        now=T,
        cfg=c,
    )
    # surplus is NOT strictly less than eps_lo → stays engaged
    assert result.engaged is True


# ---------------------------------------------------------------------------
# Hurdle drops mid-engage → release
# ---------------------------------------------------------------------------


def test_hurdle_dropping_mid_engage_releases():
    """Once engaged, hurdle_clears=False after dwell → disengage."""
    c = cfg()
    prev = engaged(since=T - timedelta(minutes=20))  # dwell elapsed
    result = scheduler.decide_export_state(
        prev,
        surplus_kwh=0.5,  # high surplus, but hurdle dropped
        hurdle_clears=False,
        now=T,
        cfg=c,
    )
    assert result.engaged is False
    assert result.state_since == T


def test_hurdle_dropping_mid_engage_within_dwell_keeps_engaged():
    """Hurdle drops mid-engage but dwell not yet elapsed → stay engaged."""
    c = cfg()
    prev = engaged(since=T - timedelta(minutes=5))  # dwell not elapsed
    result = scheduler.decide_export_state(
        prev,
        surplus_kwh=0.5,
        hurdle_clears=False,
        now=T,
        cfg=c,
    )
    assert result.engaged is True  # dwell shields against hurdle drop too


# ---------------------------------------------------------------------------
# Straddling: no flap scenario
# ---------------------------------------------------------------------------


def test_no_engage_flap_straddling_eps_band():
    """Surplus oscillates in [eps_lo, eps_hi] → disengaged stays disengaged."""
    c = cfg()
    prev = disengaged(since=T - timedelta(minutes=20))

    # Surplus at 0.3 (dead zone) — multiple calls must not engage
    for surplus in [0.25, 0.30, 0.35, 0.38]:
        result = scheduler.decide_export_state(
            prev,
            surplus_kwh=surplus,
            hurdle_clears=True,
            now=T,
            cfg=c,
        )
        assert result.engaged is False, (
            f"Surplus {surplus} in dead zone [{c.export_eps_lo_kwh}, "
            f"{c.export_eps_hi_kwh}] should not engage, got engaged=True"
        )


def test_no_disengage_flap_above_eps_lo():
    """Once engaged, surplus oscillates above eps_lo → stays engaged."""
    c = cfg()
    prev = engaged(since=T - timedelta(minutes=20))

    for surplus in [0.22, 0.28, 0.35, 0.41]:
        result = scheduler.decide_export_state(
            prev,
            surplus_kwh=surplus,
            hurdle_clears=True,
            now=T,
            cfg=c,
        )
        assert result.engaged is True, (
            f"Surplus {surplus} above eps_lo ({c.export_eps_lo_kwh}) while engaged "
            f"should stay engaged, got engaged=False"
        )
