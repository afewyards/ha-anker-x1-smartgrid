"""Controller status sensors."""

from __future__ import annotations

from datetime import timedelta
from time import monotonic
from typing import NamedTuple

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval

from . import const, coordinator, daily_stats, usecost
from .const import DOMAIN


HOUSE_LOAD_WRITE_INTERVAL_S = 5


class _Base(SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, controller, entry_id: str, key: str, name: str) -> None:
        self._controller = controller
        self._key = key
        self._attr_name = name
        self._attr_unique_id = f"anker_x1_smartgrid_{key}"

    @property
    def native_value(self):
        return self._controller.last_status.get(self._key)


class _SensorSpec(NamedTuple):
    """Declarative spec for a pure last_status-passthrough sensor.

    ``attrs_keys`` is an optional tuple of (attribute_name, last_status_key)
    pairs; when non-empty, the generic sensor exposes those as
    extra_state_attributes (mirrors the old X1BatteryNetTodaySensor override).
    """

    key: str
    name: str
    unit: str | None = None
    state_class: SensorStateClass | None = None
    attrs_keys: tuple[tuple[str, str], ...] = ()


SENSOR_SPECS: list[_SensorSpec] = [
    _SensorSpec("state", "SmartGrid state"),
    _SensorSpec("solar_charge_kwh", "SmartGrid solar charge", "kWh", SensorStateClass.MEASUREMENT),
    _SensorSpec("setpoint_w", "SmartGrid setpoint", "W", SensorStateClass.MEASUREMENT),
    _SensorSpec("export_setpoint_w", "SmartGrid export setpoint", "W", SensorStateClass.MEASUREMENT),
    _SensorSpec("load_mae", "SmartGrid load forecast MAE", "W", SensorStateClass.MEASUREMENT),
    _SensorSpec("horizon_energy_mae_24h", "SmartGrid 24h horizon energy MAE", "kWh", SensorStateClass.MEASUREMENT),
    _SensorSpec("horizon_energy_mae_12h", "SmartGrid 12h horizon energy MAE", "kWh", SensorStateClass.MEASUREMENT),
    _SensorSpec("pinball_p50", "SmartGrid load forecast pinball p50", "W", SensorStateClass.MEASUREMENT),
    _SensorSpec("pinball_p80", "SmartGrid load forecast pinball p80", "W", SensorStateClass.MEASUREMENT),
    _SensorSpec(
        "active_model",
        "SmartGrid active load model",
        attrs_keys=tuple(
            (k, k)
            for k in (
                "ml_status",
                "addon_configured",
                "addon_reachable",
                "addon_ready",
                "addon_promoted",
                "addon_n_rows",
                "addon_last_trained",
                "addon_improvement_pct",
                "addon_origins_24h",
                "addon_origins_required",
                "addon_model_mae",
                "addon_baseline_mae",
                "addon_h24_mae",
                "addon_baseline_h24_mae",
                "coverage_days",
                "coverage_required",
                "eta_days",
                "last_health_check",
            )
        ),
    ),
    _SensorSpec("regret_eur", "SmartGrid daily regret", "EUR", SensorStateClass.MEASUREMENT),
    # 7-day rolling DP-vs-heuristic regret delta (EUR). Positive = DP was MORE
    # expensive than the heuristic over the past 7 days; negative = DP was
    # CHEAPER; None = insufficient data (< 1 day with both DP and heuristic
    # regret scored). Key deliberately distinct from regret_eur above to
    # prevent HA entity-ID collisions.
    _SensorSpec("dp_regret_7d", "SmartGrid 7d DP-vs-heuristic regret delta", "EUR", SensorStateClass.MEASUREMENT),
    _SensorSpec("over_buy_kwh", "SmartGrid over-buy", "kWh", SensorStateClass.MEASUREMENT),
    _SensorSpec("under_buy_kwh", "SmartGrid under-buy", "kWh", SensorStateClass.MEASUREMENT),
    # Realized battery cash result for the current local day (EUR). Cash
    # basis: export credit minus grid-charge spend, no cycle-cost or
    # opportunity deductions (distinct from the economic export-PnL ledger).
    # MEASUREMENT, not TOTAL: a daily-resetting value under TOTAL without
    # last_reset would make HA book every midnight reset as a negative delta
    # and corrupt the long-term statistics. Per-day history charts come from
    # battery_net_total_eur's statistics deltas instead.
    _SensorSpec(
        "battery_net_today_eur",
        "SmartGrid battery net today",
        "EUR",
        SensorStateClass.MEASUREMENT,
        (
            ("charge_cost_today", "today_charge_cost_eur"),
            ("export_revenue_today", "today_export_revenue_eur"),
        ),
    ),
    # Lifetime battery cash net since deploy (EUR); never resets, may
    # decrease. TOTAL with no last_reset: HA long-term statistics track
    # signed sum deltas, so "profit per day" charts come natively from this
    # sensor. Starts at 0.0 on first deploy (no backfill — spec non-goal).
    _SensorSpec("battery_net_total_eur", "SmartGrid battery net total", "EUR", SensorStateClass.TOTAL),
]


class X1StatusSensor(_Base):
    """Generic last_status-passthrough sensor, configured by a _SensorSpec."""

    def __init__(self, controller, entry_id: str, spec: _SensorSpec) -> None:
        super().__init__(controller, entry_id, spec.key, spec.name)
        if spec.unit is not None:
            self._attr_native_unit_of_measurement = spec.unit
        if spec.state_class is not None:
            self._attr_state_class = spec.state_class
        self._attrs_keys = spec.attrs_keys

    @property
    def extra_state_attributes(self):
        if not self._attrs_keys:
            return None
        return {attr_name: self._controller.last_status.get(status_key) for attr_name, status_key in self._attrs_keys}


_SPECS_BY_KEY: dict[str, _SensorSpec] = {spec.key: spec for spec in SENSOR_SPECS}


# --- Backward-compatible per-sensor aliases ---------------------------------
# async_setup_entry (below) instantiates X1StatusSensor directly from
# SENSOR_SPECS. These named subclasses exist only so pre-existing call sites
# (tests constructing a specific sensor directly with a plain
# (controller, entry_id) signature) keep working unchanged — no spec data is
# duplicated, each alias just binds X1StatusSensor to one spec.
class X1StateSensor(X1StatusSensor):
    def __init__(self, c, e):
        super().__init__(c, e, _SPECS_BY_KEY["state"])


class X1SolarChargeSensor(X1StatusSensor):
    def __init__(self, c, e):
        super().__init__(c, e, _SPECS_BY_KEY["solar_charge_kwh"])


class X1SetpointSensor(X1StatusSensor):
    def __init__(self, c, e):
        super().__init__(c, e, _SPECS_BY_KEY["setpoint_w"])


class X1ExportSetpointSensor(X1StatusSensor):
    def __init__(self, c, e):
        super().__init__(c, e, _SPECS_BY_KEY["export_setpoint_w"])


class X1LoadMaeSensor(X1StatusSensor):
    def __init__(self, c, e):
        super().__init__(c, e, _SPECS_BY_KEY["load_mae"])


class X1HorizonEnergyMae24hSensor(X1StatusSensor):
    def __init__(self, c, e):
        super().__init__(c, e, _SPECS_BY_KEY["horizon_energy_mae_24h"])


class X1HorizonEnergyMae12hSensor(X1StatusSensor):
    def __init__(self, c, e):
        super().__init__(c, e, _SPECS_BY_KEY["horizon_energy_mae_12h"])


class X1PinballP50Sensor(X1StatusSensor):
    def __init__(self, c, e):
        super().__init__(c, e, _SPECS_BY_KEY["pinball_p50"])


class X1PinballP80Sensor(X1StatusSensor):
    def __init__(self, c, e):
        super().__init__(c, e, _SPECS_BY_KEY["pinball_p80"])


class X1ActiveModelSensor(X1StatusSensor):
    def __init__(self, c, e):
        super().__init__(c, e, _SPECS_BY_KEY["active_model"])


class X1RegretEurSensor(X1StatusSensor):
    def __init__(self, c, e):
        super().__init__(c, e, _SPECS_BY_KEY["regret_eur"])


class X1DpRegretSensor(X1StatusSensor):
    def __init__(self, c, e):
        super().__init__(c, e, _SPECS_BY_KEY["dp_regret_7d"])


class X1OverBuyKwhSensor(X1StatusSensor):
    def __init__(self, c, e):
        super().__init__(c, e, _SPECS_BY_KEY["over_buy_kwh"])


class X1UnderBuyKwhSensor(X1StatusSensor):
    def __init__(self, c, e):
        super().__init__(c, e, _SPECS_BY_KEY["under_buy_kwh"])


class X1BatteryNetTodaySensor(X1StatusSensor):
    def __init__(self, c, e):
        super().__init__(c, e, _SPECS_BY_KEY["battery_net_today_eur"])


class X1BatteryNetTotalSensor(X1StatusSensor):
    def __init__(self, c, e):
        super().__init__(c, e, _SPECS_BY_KEY["battery_net_total_eur"])


class _Mean:
    """Time-weighted mean of one source; time while it is unavailable does not count."""

    def __init__(self) -> None:
        self.cur: float | None = None
        self._t = 0.0
        self._sum = 0.0
        self._s = 0.0

    def set(self, value: float | None, now: float) -> None:
        if self.cur is not None:
            self._sum += self.cur * (now - self._t)
            self._s += now - self._t
        self._t = now
        self.cur = value

    def take(self, now: float) -> float | None:
        cur = self.cur
        self.set(cur, now)
        mean = None if cur is None else self._sum / self._s if self._s > 0 else cur
        self._sum = self._s = 0.0
        return mean


class _LiveFeed:
    """Averages the live power sources and writes every attached sensor from one timer.

    Source state changes only update the instantaneous readings; every
    HOUSE_LOAD_WRITE_INTERVAL_S each source's time-weighted mean is published
    to all sensors at once, so the shown values share one interval.

    ``pv_entities`` is a list of entity ids (see const.normalize_pv_power_entities);
    all are subscribed and summed, None only when every one is unavailable
    (coordinator.read_pv_power_w semantics).
    """

    def __init__(self, pv_entities, meter_entity: str, batt_entity: str, loss_entity: str) -> None:
        self._pv_entities = const.normalize_pv_power_entities(pv_entities)
        self._meter_entity = meter_entity
        self._batt_entity = batt_entity
        self._loss_entity = loss_entity
        self._means = {k: _Mean() for k in ("pv", "meter", "batt", "loss")}
        self._sensors: list[X1LiveSensor] = []

    def _read(self, hass: HomeAssistant) -> None:
        now = monotonic()
        values = {
            "pv": coordinator.read_pv_power_w(hass, {const.CONF_ENT_PV_POWER: self._pv_entities}),
            "meter": coordinator.read_float(hass, self._meter_entity),
            "batt": coordinator.read_float(hass, self._batt_entity),
            "loss": coordinator.read_float(hass, self._loss_entity),
        }
        for key, mean in self._means.items():
            mean.set(values[key], now)

    def attach(self, sensor: X1LiveSensor) -> None:
        hass = sensor.hass
        if not self._sensors:
            self._read(hass)

            @callback
            def _write(now) -> None:
                t = monotonic()
                values = {k: m.take(t) for k, m in self._means.items()}
                for s in self._sensors:
                    s.show(values)
                    s.async_write_ha_state()

            sensor.async_on_remove(
                async_track_state_change_event(
                    hass,
                    [*self._pv_entities, self._meter_entity, self._batt_entity, self._loss_entity],
                    lambda event: self._read(hass),
                )
            )
            sensor.async_on_remove(
                async_track_time_interval(hass, _write, timedelta(seconds=HOUSE_LOAD_WRITE_INTERVAL_S))
            )
        self._sensors.append(sensor)
        sensor.show({k: m.cur for k, m in self._means.items()})
        sensor.async_on_remove(lambda: self._sensors.remove(sensor))


def _house_load(v: dict) -> float | None:
    if v["pv"] is None or v["meter"] is None or v["batt"] is None:
        return None
    return max(0.0, v["pv"] + v["meter"] + v["batt"] - (v["loss"] or 0.0))


class X1LiveSensor(_Base):
    """Display-only power read straight from the source entities, independent of the controller tick.

    House load mirrors the controller's ``_compute_house_load_w``: pv + meter
    (+ = import) + batt (+ = discharge) - inverter_loss, clamped to >= 0 after
    averaging; missing inverter_loss counts as 0. The other sensors keep their
    source's sign and are None when it is unavailable.
    """

    _attr_native_unit_of_measurement = "W"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_should_poll = False

    def __init__(self, c, e, feed: _LiveFeed, key: str, name: str, value) -> None:
        super().__init__(c, e, key, name)
        self._feed = feed
        self._value_of = value
        self._value: float | None = None

    @property
    def native_value(self):
        return self._value

    def show(self, values: dict) -> None:
        self._value = self._value_of(values)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._feed.attach(self)


class X1TrackedDevicesSensor(_Base):
    _attr_native_unit_of_measurement = "W"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _unrecorded_attributes = frozenset({"devices"})

    def __init__(self, c, e):
        super().__init__(c, e, "tracked_devices_w", "SmartGrid tracked devices")

    @property
    def extra_state_attributes(self):
        return {"devices": self._controller.last_status.get("tracked_devices")}


class X1PlanSensor(_Base):
    _attr_native_unit_of_measurement = "h"
    # ~150 slots at 15-min blow HA's 16KB recorded-attribute cap and bloat the
    # recorder DB; the card reads live state, so recording the blob buys nothing.
    # T18: efficiency_curve is also excluded — it's a bin table for
    # observability, not something worth recording every tick.
    _unrecorded_attributes = frozenset({"horizon", "efficiency_curve"})

    def __init__(self, c, e):
        super().__init__(c, e, "plan", "SmartGrid plan")

    @property
    def native_value(self):
        plan = self._controller.last_status.get("plan")
        return plan.get("planned_grid_hours") if plan else None

    @property
    def extra_state_attributes(self):
        plan = self._controller.last_status.get("plan")
        if not plan:
            return {}
        return {
            "horizon": plan.get("horizon", []),
            "deadline": plan.get("deadline"),
            "arbitrage_pnl": self._controller.last_status.get("planned_export_revenue_eur"),
            # Terminal water-value artefacts from the DP optimizer's final slot,
            # for observability only. Both None on DP failure. When the flag is
            # off and the DP succeeds, terminal_need_kwh reads 0.0 (not None);
            # only terminal_v_hi stays None then.
            "terminal_v_hi": self._controller.last_status.get("terminal_v_hi"),
            "terminal_need_kwh": self._controller.last_status.get("terminal_need_kwh"),
            "slot_minutes": self._controller.last_status.get("slot_minutes"),
            # Export-curve engagement diagnostics (decision._dp_select_slots'
            # _out side-channel) — makes the "per-slot export curve visible in
            # plan" rollout step actually observable: None until the DP has run
            # with an export curve at least once; False means a curve was
            # supplied but failed all-or-nothing coverage this tick (see the
            # matching INFO log in decision.py).
            "export_curve_covered": self._controller.last_status.get("export_curve_covered"),
            "export_curve_slots": self._controller.last_status.get("export_curve_slots"),
            # Calibration policy (spec 2026-08-03): "idle" | "charging" | "holding".
            "calibration_state": self._controller.last_status.get("calibration_state", "idle"),
            "calibration_window_start": self._controller.last_status.get("calibration_window_start"),
            "calibration_window_end": self._controller.last_status.get("calibration_window_end"),
            "calibration_last_success": self._controller.last_status.get("calibration_last_success"),
            "calibration_days_since": self._controller.last_status.get("calibration_days_since"),
            "calibration_target_soc": self._controller.last_status.get("calibration_target_soc"),
            "calibration_hold_soc": self._controller.last_status.get("calibration_hold_soc"),
            "calibration_cost_eur": self._controller.last_status.get("calibration_cost_eur"),
            "load_adapt_ratio": self._controller.last_status.get("load_adapt_ratio"),
            "load_adapt_matched_hours": self._controller.last_status.get("load_adapt_matched_hours"),
            "load_adapt_ratio_raw": self._controller.last_status.get("load_adapt_ratio_raw"),
            "load_adapt_pinned_h": self._controller.last_status.get("load_adapt_pinned_h"),
            # Layer B occupancy corrector observability (controller._occ_status_attrs).
            "occ_state_now": self._controller.last_status.get("occ_state_now"),
            "occ_expected_state": self._controller.last_status.get("occ_expected_state"),
            "occ_multiplier": self._controller.last_status.get("occ_multiplier"),
            "occ_cells_ready": self._controller.last_status.get("occ_cells_ready"),
            # T18: measured efficiency curve bin table, for
            # dashboard/diagnostic observability only.
            "efficiency_curve": self._controller.last_status.get("efficiency_curve"),
            # Low-power hold: discharge value is None whenever a hold isn't allowed this tick.
            "low_power_hold_active": self._controller.last_status.get("low_power_hold_active"),
            "hold_discharge_value_eur_per_kwh": self._controller.last_status.get("hold_discharge_value_eur_per_kwh"),
            "hold_stored_value_eur_per_kwh": self._controller.last_status.get("hold_stored_value_eur_per_kwh"),
        }


class X1FictivePlanSensor(_Base):
    """Exposes the DP-proposed fictive plan so the dashboard card can render it.

    Reads ``last_status["fictive_plan"]`` published by the controller's tick()
    when the DP optimizer runs (T0.6a).  Entity-id: sensor.smartgrid_fictive_plan.
    """

    _attr_native_unit_of_measurement = "h"
    # ~150 slots at 15-min blow HA's 16KB recorded-attribute cap and bloat the
    # recorder DB; the card reads live state, so recording the blob buys nothing.
    _unrecorded_attributes = frozenset({"horizon"})

    def __init__(self, c, e):
        super().__init__(c, e, "fictive_plan", "SmartGrid fictive plan")

    @property
    def native_value(self):
        plan = self._controller.last_status.get("fictive_plan")
        return plan.get("planned_grid_hours") if plan else None

    @property
    def extra_state_attributes(self):
        plan = self._controller.last_status.get("fictive_plan")
        if not plan:
            return {}
        return {"horizon": plan.get("horizon", []), "deadline": plan.get("deadline")}


class X1DailyStatsSensor(_Base):
    """Per-day grid-charge / grid-export kWh + net € table (spec 2026-08-01).

    Reads ``last_status["daily_stats"]`` published by the controller's
    ``_publish_daily_stats``.  Entity-id: sensor.smartgrid_daily_stats.

    State is the ROW COUNT, deliberately not today's € — that number already
    has an entity (sensor.smartgrid_battery_net_today) and two entities
    publishing the same figure would eventually be seen to disagree.
    """

    # A ~15-row list that changes every tick bloats the recorder DB for no
    # gain; the card reads live state. Same reasoning as X1PlanSensor.horizon.
    _unrecorded_attributes = frozenset({"days"})

    def __init__(self, c, e):
        super().__init__(c, e, "daily_stats", "SmartGrid daily stats")

    @property
    def native_value(self):
        return len(self._controller.last_status.get("daily_stats") or [])

    @property
    def extra_state_attributes(self):
        m30 = self._controller.last_status.get("battery_margin_30d") or (None, None)
        s30 = self._controller.last_status.get("battery_solar_gain_30d") or (None, None)
        return {
            "days": self._controller.last_status.get("daily_stats") or [],
            "window_days": daily_stats.WINDOW_DAYS,
            "battery_margin_30d_eur": m30[0],
            "battery_margin_30d_days": m30[1],
            "battery_solar_gain_30d_eur": s30[0],
        }


class X1PowerCardSensor(_Base):
    """What the three-day power card draws; state is when days and hours were built."""

    _unrecorded_attributes = frozenset({"battery", "live", "days", "hours", "pack"})

    def __init__(self, c, e):
        super().__init__(c, e, "power_card", "SmartGrid power card")

    @property
    def native_value(self):
        return (self._controller.power_card or {}).get("built_at")

    @property
    def extra_state_attributes(self):
        card = self._controller.power_card or {}
        attrs = {key: card.get(key) for key in ("battery", "live", "days", "hours")}
        attrs["pack"] = None
        if attrs["hours"]:
            attrs["hours"], attrs["pack"] = usecost.pack_rows(usecost.trim_rows(attrs["hours"]))
        return attrs


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    controller = hass.data[DOMAIN][entry.entry_id]["controller"]
    # Merged entry data (post Anker-device resolution) — same dict the
    # controller reads from at tick time.  BATTERY_POWER is guaranteed
    # present (a hard Anker role with no DEFAULT_ENTITIES fallback),
    # matching controller._compute_house_load_w's direct indexing.
    # PV_POWER may be a legacy single entity-id string or a list of entity
    # ids summed together; const.resolve_pv_power_entities normalizes both
    # shapes (and falls back to the DEFAULT_ENTITIES soft-role default when
    # empty/absent), matching coordinator.read_pv_power_w.
    # METER_POWER/INVERTER_LOSS are soft roles, so they fall back to
    # DEFAULT_ENTITIES like every other soft-role read in this integration.
    entry_data = controller._data
    pv_entities = const.resolve_pv_power_entities(entry_data)
    meter_entity = entry_data.get(const.CONF_ENT_METER_POWER, const.DEFAULT_ENTITIES[const.CONF_ENT_METER_POWER])
    batt_entity = entry_data[const.CONF_ENT_BATTERY_POWER]
    loss_entity = entry_data.get(const.CONF_ENT_INVERTER_LOSS, const.DEFAULT_ENTITIES[const.CONF_ENT_INVERTER_LOSS])
    # Keyed by spec.key so the entities list below can interleave spec-driven
    # sensors with the real (non-boilerplate) classes in their original order.
    feed = _LiveFeed(pv_entities, meter_entity, batt_entity, loss_entity)
    live_sensors = [
        X1LiveSensor(controller, entry.entry_id, feed, key, name, value)
        for key, name, value in (
            ("house_load_w", "SmartGrid house load", _house_load),
            ("grid_power_w", "SmartGrid grid power", lambda v: v["meter"]),
            ("battery_power_w", "SmartGrid battery power", lambda v: v["batt"]),
            ("solar_power_w", "SmartGrid solar power", lambda v: v["pv"]),
        )
    ]
    spec_sensors = {spec.key: X1StatusSensor(controller, entry.entry_id, spec) for spec in SENSOR_SPECS}
    async_add_entities(
        [
            spec_sensors["state"],
            spec_sensors["solar_charge_kwh"],
            spec_sensors["setpoint_w"],
            spec_sensors["export_setpoint_w"],
            *live_sensors,
            spec_sensors["load_mae"],
            spec_sensors["horizon_energy_mae_24h"],
            spec_sensors["horizon_energy_mae_12h"],
            spec_sensors["pinball_p50"],
            spec_sensors["pinball_p80"],
            spec_sensors["active_model"],
            X1PlanSensor(controller, entry.entry_id),
            X1TrackedDevicesSensor(controller, entry.entry_id),
            X1FictivePlanSensor(controller, entry.entry_id),
            X1DailyStatsSensor(controller, entry.entry_id),
            X1PowerCardSensor(controller, entry.entry_id),
            spec_sensors["regret_eur"],
            spec_sensors["dp_regret_7d"],
            spec_sensors["over_buy_kwh"],
            spec_sensors["under_buy_kwh"],
            spec_sensors["battery_net_today_eur"],
            spec_sensors["battery_net_total_eur"],
        ]
    )
