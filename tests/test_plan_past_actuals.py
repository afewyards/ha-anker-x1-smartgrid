from datetime import datetime, timezone

from custom_components.anker_x1_smartgrid.models import Config, ForecastInterval, PriceSlot
from custom_components.anker_x1_smartgrid.plan import build_plan_horizon


def _slot(h, price=0.20):
    return PriceSlot(start=datetime(2026, 6, 29, h, tzinfo=timezone.utc), price=price)


def _cfg():
    # Defaults sufficient for the horizon SoC sim.
    return Config()


def test_past_slot_uses_actuals_and_leaves_forward_soc_unchanged():
    cfg = _cfg()
    now_h = datetime(2026, 6, 29, 10, tzinfo=timezone.utc)
    slots = [_slot(8), _slot(9), _slot(10), _slot(11)]
    # Forward intervals only for now_h onward (mirrors build_display_intervals).
    intervals = [
        ForecastInterval(datetime(2026, 6, 29, 10, tzinfo=timezone.utc), 1000.0, 300.0, 1.0),
        ForecastInterval(datetime(2026, 6, 29, 11, tzinfo=timezone.utc), 1200.0, 300.0, 1.0),
    ]
    past = {
        datetime(2026, 6, 29, 8, tzinfo=timezone.utc): {
            "pv_w": 800.0, "load_w": 250.0, "soc": 30.0,
            "solar_charge_w": 400.0, "grid_charge_w": 0.0, "grid_export_w": 0.0,
        },
        datetime(2026, 6, 29, 9, tzinfo=timezone.utc): {
            "pv_w": 0.0, "load_w": 400.0, "soc": 28.0,
            "solar_charge_w": 0.0, "grid_charge_w": 0.0, "grid_export_w": 1500.0,
        },
    }
    horizon_edge = datetime(2026, 6, 29, 12, tzinfo=timezone.utc)

    with_past = build_plan_horizon(slots, intervals, [], 50.0, horizon_edge, cfg, past_actuals_by_hour=past)
    without = build_plan_horizon(slots, intervals, [], 50.0, horizon_edge, cfg)

    by_start = {e["start"]: e for e in with_past}
    h8 = by_start["2026-06-29T08:00:00+00:00"]
    assert h8["pv_w"] == 800.0 and h8["load_w"] == 250.0 and h8["soc"] == 30.0
    assert h8["solar_charge_w"] == 400.0 and h8["mode"] == "actual"
    h9 = by_start["2026-06-29T09:00:00+00:00"]
    assert h9["grid_export_w"] == 1500.0

    # Forward slots (10,11) must be byte-identical to the no-past output.
    fwd_with = [e for e in with_past if e["start"] >= "2026-06-29T10:00:00+00:00"]
    fwd_without = [e for e in without if e["start"] >= "2026-06-29T10:00:00+00:00"]
    assert fwd_with == fwd_without


def test_default_none_is_byte_identical_legacy():
    cfg = _cfg()
    slots = [_slot(8), _slot(9), _slot(10)]
    intervals = [ForecastInterval(datetime(2026, 6, 29, 10, tzinfo=timezone.utc), 1000.0, 300.0, 1.0)]
    edge = datetime(2026, 6, 29, 11, tzinfo=timezone.utc)
    a = build_plan_horizon(slots, intervals, [], 50.0, edge, cfg)
    b = build_plan_horizon(slots, intervals, [], 50.0, edge, cfg, past_actuals_by_hour=None)
    assert a == b


def test_hour_absent_from_map_stays_legacy_none():
    cfg = _cfg()
    slots = [_slot(8), _slot(10)]
    intervals = [ForecastInterval(datetime(2026, 6, 29, 10, tzinfo=timezone.utc), 1000.0, 300.0, 1.0)]
    edge = datetime(2026, 6, 29, 11, tzinfo=timezone.utc)
    # Empty map -> hour 8 stays the legacy None/flat slot.
    out = build_plan_horizon(slots, intervals, [], 50.0, edge, cfg, past_actuals_by_hour={})
    h8 = [e for e in out if e["start"] == "2026-06-29T08:00:00+00:00"][0]
    assert h8["pv_w"] is None and h8["load_w"] is None


def test_future_slot_kwh_matches_watts_times_dt_h():
    cfg = _cfg()
    now_h = datetime(2026, 6, 29, 10, tzinfo=timezone.utc)
    slots = [_slot(10), _slot(11)]
    intervals = [
        ForecastInterval(now_h, 1000.0, 300.0, 1.0),
        ForecastInterval(datetime(2026, 6, 29, 11, tzinfo=timezone.utc), 1200.0, 300.0, 1.0),
    ]
    edge = datetime(2026, 6, 29, 12, tzinfo=timezone.utc)
    out = build_plan_horizon(slots, intervals, [], 50.0, edge, cfg)
    h10 = out[0]
    assert h10["pv_kwh"] == round(1000.0 * 1.0 / 1000.0, 3)
    assert h10["load_kwh"] == round(300.0 * 1.0 / 1000.0, 3)


def test_future_grid_charge_and_export_kwh():
    cfg = Config(capacity_kwh=10.0, soc_target=100.0, max_charge_w=3000.0, eta_charge=1.0)
    h10 = datetime(2026, 6, 29, 10, tzinfo=timezone.utc)
    h11 = datetime(2026, 6, 29, 11, tzinfo=timezone.utc)
    slots = [_slot(10), _slot(11)]
    intervals = [
        ForecastInterval(h10, 0.0, 300.0, 1.0),
        ForecastInterval(h11, 0.0, 300.0, 1.0),
    ]
    edge = datetime(2026, 6, 29, 12, tzinfo=timezone.utc)
    out = build_plan_horizon(
        slots, intervals, [h10], 50.0, edge, cfg,
        export_request_by_hour={h11: 1500.0},
    )
    grid_hour = out[0]
    export_hour = out[1]
    assert grid_hour["mode"] == "grid"
    assert grid_hour["grid_charge_kwh"] == round(grid_hour["grid_charge_w"] / 1000.0, 3)
    assert export_hour["grid_export_w"] == 1500.0
    assert export_hour["grid_export_kwh"] == round(export_hour["grid_export_w"] / 1000.0, 3)


def test_future_slot_kwh_at_quarter_hour_dt():
    # dt_h=0.25 (15-min cutover): kwh must use the ACTUAL interval dt_h, not
    # an assumed 1-hour slot.
    cfg = _cfg()
    h10 = datetime(2026, 6, 29, 10, tzinfo=timezone.utc)
    slots = [_slot(10)]
    intervals = [ForecastInterval(h10, 800.0, 300.0, 0.25)]
    edge = datetime(2026, 6, 29, 11, tzinfo=timezone.utc)
    out = build_plan_horizon(slots, intervals, [], 50.0, edge, cfg)
    h = out[0]
    assert h["load_kwh"] == round(300.0 * 0.25 / 1000.0, 3)
    assert h["pv_kwh"] == round(800.0 * 0.25 / 1000.0, 3)


def test_past_slot_kwh_passthrough_from_actuals():
    cfg = _cfg()
    now_h = datetime(2026, 6, 29, 10, tzinfo=timezone.utc)
    slots = [_slot(8), _slot(10)]
    intervals = [ForecastInterval(now_h, 1000.0, 300.0, 1.0)]
    past = {
        datetime(2026, 6, 29, 8, tzinfo=timezone.utc): {
            "pv_w": 800.0, "load_w": 250.0, "soc": 30.0,
            "solar_charge_w": 400.0, "grid_charge_w": 0.0, "grid_export_w": 0.0,
            "pv_kwh": 0.9, "load_kwh": 0.25, "solar_charge_kwh": 0.4,
            "grid_charge_kwh": 0.0, "grid_export_kwh": 0.0,
        },
    }
    edge = datetime(2026, 6, 29, 11, tzinfo=timezone.utc)
    out = build_plan_horizon(slots, intervals, [], 50.0, edge, cfg, past_actuals_by_hour=past)
    h8 = [e for e in out if e["start"] == "2026-06-29T08:00:00+00:00"][0]
    assert h8["pv_kwh"] == 0.9
    assert h8["load_kwh"] == 0.25
    assert h8["solar_charge_kwh"] == 0.4
    assert h8["grid_charge_kwh"] == 0.0
    assert h8["grid_export_kwh"] == 0.0


def test_past_slot_kwh_none_when_actual_predates_new_keys():
    # Stale cached actuals recorded before the kwh keys existed must not KeyError;
    # the slot should carry the five kwh keys with None values (None-safe passthrough).
    cfg = _cfg()
    slots = [_slot(8), _slot(10)]
    intervals = [ForecastInterval(datetime(2026, 6, 29, 10, tzinfo=timezone.utc), 1000.0, 300.0, 1.0)]
    past = {
        datetime(2026, 6, 29, 8, tzinfo=timezone.utc): {
            "pv_w": 800.0, "load_w": 250.0, "soc": 30.0,
            "solar_charge_w": 400.0, "grid_charge_w": 0.0, "grid_export_w": 0.0,
        },
    }
    edge = datetime(2026, 6, 29, 11, tzinfo=timezone.utc)
    out = build_plan_horizon(slots, intervals, [], 50.0, edge, cfg, past_actuals_by_hour=past)
    h8 = [e for e in out if e["start"] == "2026-06-29T08:00:00+00:00"][0]
    assert h8["pv_kwh"] is None
    assert h8["load_kwh"] is None
    assert h8["solar_charge_kwh"] is None
    assert h8["grid_charge_kwh"] is None
    assert h8["grid_export_kwh"] is None
