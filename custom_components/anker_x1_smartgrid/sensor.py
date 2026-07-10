"""Controller status sensors."""
from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

from . import const, coordinator
from .const import DOMAIN


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


class X1StateSensor(_Base):
    def __init__(self, c, e):
        super().__init__(c, e, "state", "SmartGrid state")


class X1SolarChargeSensor(_Base):
    _attr_native_unit_of_measurement = "kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, c, e):
        super().__init__(c, e, "solar_charge_kwh", "SmartGrid solar charge")


class X1SetpointSensor(_Base):
    _attr_native_unit_of_measurement = "W"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, c, e):
        super().__init__(c, e, "setpoint_w", "SmartGrid setpoint")


class X1ExportSetpointSensor(_Base):
    _attr_native_unit_of_measurement = "W"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, c, e):
        super().__init__(c, e, "export_setpoint_w", "SmartGrid export setpoint")


class X1HouseLoadSensor(_Base):
    """Live house load (W), event-driven off its source entities.

    Template-sensor semantics: recomputes on every state-change of any
    pv/meter/battery/inverter-loss entity, independent of the 60s controller
    tick. Formula mirrors the controller's tick-time ``_compute_house_load_w``
    (kept tick-synchronous for actuation/recording — this sensor is
    display-only and reads live HA state directly, never last_status):
    pv + meter (+ = import) + batt (+ = discharge, - = charge) -
    inverter_loss, clamped to >= 0.

    ``pv_entities`` accepts either a single legacy entity-id string or a list
    of entity ids (mirroring const.normalize_pv_power_entities) — every PV
    entity in the list is summed together and ALL of them are subscribed, so
    a state change of any one alone triggers a recompute. pv sums to None
    only when every PV entity is unavailable (matching
    coordinator.read_pv_power_w semantics); meter/batt unavailable ->
    native_value None (HA shows unknown). inverter_loss unavailable ->
    treated as 0.0.
    """

    _attr_native_unit_of_measurement = "W"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_should_poll = False

    def __init__(self, c, e, pv_entities, meter_entity: str, batt_entity: str, loss_entity: str):
        super().__init__(c, e, "house_load_w", "SmartGrid house load")
        self._pv_entities = const.normalize_pv_power_entities(pv_entities)
        self._meter_entity = meter_entity
        self._batt_entity = batt_entity
        self._loss_entity = loss_entity
        self._value: float | None = None

    @property
    def native_value(self):
        return self._value

    def _recompute(self) -> None:
        pv = coordinator.read_pv_power_w(
            self.hass, {const.CONF_ENT_PV_POWER: self._pv_entities}
        )
        meter = coordinator.read_float(self.hass, self._meter_entity)
        batt = coordinator.read_float(self.hass, self._batt_entity)
        if pv is None or meter is None or batt is None:
            self._value = None
            return
        loss = coordinator.read_float(self.hass, self._loss_entity)
        self._value = max(0.0, pv + meter + batt - (loss if loss is not None else 0.0))

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Prime the value at startup so it's present before the first
        # source-entity state change fires.
        self._recompute()

        @callback
        def _handle_source_change(event) -> None:
            self._recompute()
            self.async_write_ha_state()

        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                [*self._pv_entities, self._meter_entity, self._batt_entity, self._loss_entity],
                _handle_source_change,
            )
        )


class X1LoadMaeSensor(_Base):
    _attr_native_unit_of_measurement = "W"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, c, e):
        super().__init__(c, e, "load_mae", "SmartGrid load forecast MAE")


class X1HorizonEnergyMae24hSensor(_Base):
    _attr_native_unit_of_measurement = "kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, c, e):
        super().__init__(c, e, "horizon_energy_mae_24h", "SmartGrid 24h horizon energy MAE")


class X1HorizonEnergyMae12hSensor(_Base):
    _attr_native_unit_of_measurement = "kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, c, e):
        super().__init__(c, e, "horizon_energy_mae_12h", "SmartGrid 12h horizon energy MAE")


class X1PinballP50Sensor(_Base):
    _attr_native_unit_of_measurement = "W"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, c, e):
        super().__init__(c, e, "pinball_p50", "SmartGrid load forecast pinball p50")


class X1PinballP80Sensor(_Base):
    _attr_native_unit_of_measurement = "W"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, c, e):
        super().__init__(c, e, "pinball_p80", "SmartGrid load forecast pinball p80")


class X1ActiveModelSensor(_Base):
    def __init__(self, c, e):
        super().__init__(c, e, "active_model", "SmartGrid active load model")


class X1RegretEurSensor(_Base):
    _attr_native_unit_of_measurement = "EUR"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, c, e):
        super().__init__(c, e, "regret_eur", "SmartGrid daily regret")


class X1DpRegretSensor(_Base):
    """7-day rolling DP-vs-heuristic regret delta (EUR).

    Positive  — DP was MORE expensive than the heuristic over the past 7 days.
    Negative  — DP was CHEAPER (DP saved money vs heuristic).
    None      — insufficient data (< 1 day with both DP and heuristic regret scored).

    Key is ``dp_regret_7d`` — deliberately distinct from X1RegretEurSensor's
    ``regret_eur`` key to prevent HA entity-ID collisions.
    """
    _attr_native_unit_of_measurement = "EUR"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, c, e):
        super().__init__(c, e, "dp_regret_7d", "SmartGrid 7d DP-vs-heuristic regret delta")


class X1OverBuyKwhSensor(_Base):
    _attr_native_unit_of_measurement = "kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, c, e):
        super().__init__(c, e, "over_buy_kwh", "SmartGrid over-buy")


class X1UnderBuyKwhSensor(_Base):
    _attr_native_unit_of_measurement = "kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, c, e):
        super().__init__(c, e, "under_buy_kwh", "SmartGrid under-buy")


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
            "slot_minutes": self._controller.last_status.get("slot_minutes"),
            "load_adapt_ratio": self._controller.last_status.get("load_adapt_ratio"),
            "load_adapt_matched_hours": self._controller.last_status.get("load_adapt_matched_hours"),
            # T18: measured efficiency curve bin table + gate flag, for
            # dashboard/diagnostic observability only.
            "efficiency_curve": self._controller.last_status.get("efficiency_curve"),
            "use_measured_eta": self._controller.last_status.get("use_measured_eta"),
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


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
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
    meter_entity = entry_data.get(
        const.CONF_ENT_METER_POWER, const.DEFAULT_ENTITIES[const.CONF_ENT_METER_POWER]
    )
    batt_entity = entry_data[const.CONF_ENT_BATTERY_POWER]
    loss_entity = entry_data.get(
        const.CONF_ENT_INVERTER_LOSS, const.DEFAULT_ENTITIES[const.CONF_ENT_INVERTER_LOSS]
    )
    async_add_entities(
        [
            X1StateSensor(controller, entry.entry_id),
            X1SolarChargeSensor(controller, entry.entry_id),
            X1SetpointSensor(controller, entry.entry_id),
            X1ExportSetpointSensor(controller, entry.entry_id),
            X1HouseLoadSensor(controller, entry.entry_id, pv_entities, meter_entity, batt_entity, loss_entity),
            X1LoadMaeSensor(controller, entry.entry_id),
            X1HorizonEnergyMae24hSensor(controller, entry.entry_id),
            X1HorizonEnergyMae12hSensor(controller, entry.entry_id),
            X1PinballP50Sensor(controller, entry.entry_id),
            X1PinballP80Sensor(controller, entry.entry_id),
            X1ActiveModelSensor(controller, entry.entry_id),
            X1PlanSensor(controller, entry.entry_id),
            X1FictivePlanSensor(controller, entry.entry_id),
            X1RegretEurSensor(controller, entry.entry_id),
            X1DpRegretSensor(controller, entry.entry_id),
            X1OverBuyKwhSensor(controller, entry.entry_id),
            X1UnderBuyKwhSensor(controller, entry.entry_id),
        ]
    )
