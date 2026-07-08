from datetime import datetime, timezone
from custom_components.anker_x1_smartgrid.models import (
    Config, ControllerState, PriceSlot, ForecastInterval, PlantInputs, PlanState,
)


def test_config_from_dict_uses_defaults():
    cfg = Config.from_dict({})
    assert cfg.soc_target == 97.0
    assert cfg.eta_charge == 0.92


def test_config_from_dict_overrides():
    cfg = Config.from_dict({"soc_target": 90.0})
    assert cfg.soc_target == 90.0


def test_planstate_roundtrip():
    t = datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc)
    ps = PlanState(ControllerState.FORCING, t, (t,))
    d = ps.to_dict()
    ps2 = PlanState.from_dict(d)
    assert ps2.state is ControllerState.FORCING
    assert ps2.state_since == t
    assert ps2.committed_slots == (t,)


def test_price_slot_and_interval():
    t = datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc)
    assert PriceSlot(t, 0.12).price == 0.12
    fi = ForecastInterval(t, 3000.0, 500.0, 1.0)
    assert fi.pv_w == 3000.0 and fi.dt_h == 1.0
    pi = PlantInputs(50.0, (100.0, 200.0, -50.0), t)
    assert pi.phase_import_w[2] == -50.0


def test_planstate_roundtrips_committed_charge_kwh():
    from datetime import datetime, timezone
    from custom_components.anker_x1_smartgrid.models import ControllerState, PlanState
    now = datetime(2026, 6, 23, 18, 0, tzinfo=timezone.utc)
    ps = PlanState(ControllerState.FORCING, now, (now,), committed_charge_kwh=1.5)
    restored = PlanState.from_dict(ps.to_dict())
    assert restored.committed_charge_kwh == 1.5


def test_planstate_from_dict_defaults_committed_charge_kwh():
    from datetime import datetime, timezone
    from custom_components.anker_x1_smartgrid.models import PlanState
    now = datetime(2026, 6, 23, 18, 0, tzinfo=timezone.utc)
    # Legacy payload without the new key restores to 0.0.
    legacy = {"state": "passive", "state_since": now.isoformat(), "committed_slots": []}
    assert PlanState.from_dict(legacy).committed_charge_kwh == 0.0


# ── A2: Config export fields ──────────────────────────────────────────────────

def test_config_export_defaults():
    """Config() defaults match DEFAULT_* export constants (A2)."""
    from custom_components.anker_x1_smartgrid import const
    cfg = Config()
    assert cfg.enable_export is const.DEFAULT_ENABLE_EXPORT
    assert cfg.max_export_w == const.DEFAULT_MAX_EXPORT_W
    assert cfg.grid_export_limit_w == const.DEFAULT_GRID_EXPORT_LIMIT_W
    assert cfg.cycle_cost_eur_per_kwh == const.DEFAULT_CYCLE_COST_EUR_PER_KWH
    assert cfg.export_eps_lo_kwh == const.DEFAULT_EXPORT_EPS_LO_KWH
    assert cfg.export_eps_hi_kwh == const.DEFAULT_EXPORT_EPS_HI_KWH
    assert cfg.export_dwell_min == const.DEFAULT_EXPORT_DWELL_MIN


def test_config_from_dict_max_export_w_round_trip():
    """from_dict picks up max_export_w by field name; others stay at default."""
    from custom_components.anker_x1_smartgrid import const
    cfg = Config.from_dict({const.CONF_MAX_EXPORT_W: 3000.0})
    assert cfg.max_export_w == 3000.0
    assert cfg.enable_export is const.DEFAULT_ENABLE_EXPORT
    assert cfg.grid_export_limit_w == const.DEFAULT_GRID_EXPORT_LIMIT_W


def test_config_from_dict_export_eps_dwell_round_trip():
    """from_dict round-trips export hysteresis band + dwell values."""
    from custom_components.anker_x1_smartgrid import const
    cfg = Config.from_dict({
        const.CONF_EXPORT_EPS_LO_KWH: 0.1,
        const.CONF_EXPORT_EPS_HI_KWH: 0.3,
        const.CONF_EXPORT_DWELL_MIN: 10,
        const.CONF_CYCLE_COST_EUR_PER_KWH: 0.05,
    })
    assert cfg.export_eps_lo_kwh == 0.1
    assert cfg.export_eps_hi_kwh == 0.3
    assert cfg.export_dwell_min == 10
    assert cfg.cycle_cost_eur_per_kwh == 0.05
