"""Controller status sensors."""
from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

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
    async_add_entities(
        [
            X1StateSensor(controller, entry.entry_id),
            X1SolarChargeSensor(controller, entry.entry_id),
            X1SetpointSensor(controller, entry.entry_id),
            X1ExportSetpointSensor(controller, entry.entry_id),
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
