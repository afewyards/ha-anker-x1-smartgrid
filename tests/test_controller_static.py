"""Static-tariff wiring in the controller (export price + recorder tolerance)."""
import pytest

from custom_components.anker_x1_smartgrid import controller, const
from custom_components.anker_x1_smartgrid import optimize as optimize_mod
from tests.test_controller import _StubHass, _make_controller, _seed_valid_inputs, BASE
from tests.test_controller_dp import _call_dp_select_slots, _cfg


def test_resolve_export_price_static_constant():
    ctrl, _ = _make_controller(_StubHass(), data_overrides={
        const.CONF_PRICE_MODE: const.PRICE_MODE_STATIC,
        const.CONF_STATIC_PRICE_EXPORT: 0.12,
    })
    assert ctrl._resolve_export_price() == (0.12, False)


def test_resolve_export_price_static_zero_is_none():
    ctrl, _ = _make_controller(_StubHass(), data_overrides={
        const.CONF_PRICE_MODE: const.PRICE_MODE_STATIC,
        const.CONF_STATIC_PRICE_EXPORT: 0.0,
    })
    assert ctrl._resolve_export_price() == (None, False)


def test_resolve_export_price_sensor_mode_unchanged():
    # Default price_mode=sensor, no export entity configured → (None, False).
    ctrl, _ = _make_controller(_StubHass())
    assert ctrl._resolve_export_price() == (None, False)


@pytest.mark.asyncio
async def test_record_sample_tolerates_absent_price_and_irradiance(monkeypatch):
    monkeypatch.setattr(controller.dt_util, "utcnow", lambda: BASE)
    hass = _StubHass()
    ctrl, _ = _make_controller(hass)
    ctrl.enabled = False
    ctrl._data.pop(const.CONF_ENT_PRICE, None)       # new-install shape (post NL removal)
    ctrl._data.pop(const.CONF_ENT_IRRADIANCE, None)
    _seed_valid_inputs(hass, soc="20.0")
    result = await ctrl.tick()
    assert result["reason"] == "disabled"
    assert ctrl._recorder.rows, "a sample row must have been recorded without KeyError"
    row = ctrl._recorder.rows[-1]
    assert row["import_price"] is None
    assert row["irradiance"] is None


from datetime import timedelta


@pytest.mark.asyncio
async def test_tick_static_mode_zero_price_entities_runs_dp(monkeypatch):
    """Static mode with NO price sensor still ticks (reason ok) and populates a plan."""
    monkeypatch.setattr(controller.dt_util, "utcnow", lambda: BASE)
    hass = _StubHass()
    ctrl, act = _make_controller(hass, data_overrides={
        const.CONF_PRICE_MODE: const.PRICE_MODE_STATIC,
        const.CONF_STATIC_PRICE_IMPORT: 0.30,
        const.CONF_STATIC_PRICE_OFFPEAK: 0.10,
        const.CONF_STATIC_OFFPEAK_HOURS: "01:00-06:00",
        const.CONF_ENT_PRICE: "",          # no dynamic price sensor
        const.CONF_ENT_PV_TODAY: [],
        const.CONF_ENT_PV_TOMORROW: [],
    })
    # Seed plant inputs + sun ONLY — no price forecast entity exists.
    hass.set_state("sensor.soc", "20.0")
    hass.set_state("sensor.meter_power", "0.0")
    hass.set_state("sun.sun", "above_horizon",
                   {"next_setting": (BASE + timedelta(hours=8)).isoformat()})
    hass.set_state("sensor.pv_power", "0.0")
    hass.set_state("sensor.battery_power", "0.0")

    result = await ctrl.tick()

    # NOT failsafe → synth produced slots, all inputs present, DP ran.
    assert result["reason"] == "ok"
    assert ctrl.last_decision, "last_decision must be populated in static mode"
    assert isinstance(ctrl.last_decision["committed_hours"], list)
    # The synthesized horizon carried both tariff levels.
    slots = controller.coordinator.read_price_slots(hass, ctrl._data)
    assert {round(s.price, 2) for s in slots} == {0.30, 0.10}


# ===========================================================================
# Static mode export window must flat-broadcast the constant, never
# ratio-scale by the (HP/HC) import curve's shape.
# ===========================================================================

def _dp_export_window(cfg, prices):
    """Capture the per-hour export-price array _dp_select_slots hands to the DP."""
    from custom_components.anker_x1_smartgrid import controller as ctrl_mod
    captured = {}

    def _fake(*args, **kwargs):
        captured["export_price"] = kwargs.get("export_price")
        wl = kwargs["window_len"]
        return {"schedule": [0.0] * wl, "kwh": 0.0, "eur": 0.0,
                "export_schedule": [0.0] * wl, "export_kwh": 0.0, "export_revenue_eur": 0.0}

    import unittest.mock as mock
    with mock.patch.object(ctrl_mod.optimize_mod, "optimize_grid", side_effect=_fake):
        _call_dp_select_slots(
            cfg, soc=80.0, export_price=cfg.static_price_export,
            export_price_matches_import=False, prices=prices,
        )
    return captured["export_price"]


def test_static_export_window_flat_broadcasts_constant_not_ratio_scaled():
    """HP/HC static import curve (0.30 peak / 0.10 offpeak) + static_price_export=0.12:
    the DP export window must equal effective(0.12) in EVERY hour, at both a
    peak-hour tick and an offpeak-hour tick — never mirror the import shape.
    """
    cfg = _cfg(
        price_mode=const.PRICE_MODE_STATIC,
        static_price_import=0.30,
        static_price_offpeak=0.10,
        static_offpeak_hours="01:00-06:00",
        static_price_export=0.12,
        enable_export=True,
    )
    expected = optimize_mod.effective_export_price(0.12, cfg)

    # Tick at a peak hour (current-hour import price = 0.30).
    peak_tick_prices = [0.30, 0.10, 0.30, 0.10, 0.30, 0.10, 0.30, 0.10, 0.30]
    peak_window = _dp_export_window(cfg, peak_tick_prices)
    assert peak_window is not None
    assert peak_window == pytest.approx([expected] * len(peak_tick_prices)), (
        f"static export window must be a flat {expected} in every hour, got {peak_window}"
    )

    # Tick at an offpeak hour (current-hour import price = 0.10).
    offpeak_tick_prices = [0.10, 0.30, 0.10, 0.30, 0.10, 0.30, 0.10, 0.30, 0.10]
    offpeak_window = _dp_export_window(cfg, offpeak_tick_prices)
    assert offpeak_window is not None
    assert offpeak_window == pytest.approx([expected] * len(offpeak_tick_prices)), (
        f"static export window must be a flat {expected} in every hour, got {offpeak_window}"
    )

    # Identical across tick times too — the constant must not depend on when we tick.
    assert peak_window == pytest.approx(offpeak_window)
