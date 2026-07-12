"""Controller status sensors."""
from __future__ import annotations

from typing import NamedTuple

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
    _SensorSpec(
        "horizon_energy_mae_24h", "SmartGrid 24h horizon energy MAE", "kWh", SensorStateClass.MEASUREMENT
    ),
    _SensorSpec(
        "horizon_energy_mae_12h", "SmartGrid 12h horizon energy MAE", "kWh", SensorStateClass.MEASUREMENT
    ),
    _SensorSpec("pinball_p50", "SmartGrid load forecast pinball p50", "W", SensorStateClass.MEASUREMENT),
    _SensorSpec("pinball_p80", "SmartGrid load forecast pinball p80", "W", SensorStateClass.MEASUREMENT),
    _SensorSpec("active_model", "SmartGrid active load model"),
    _SensorSpec("regret_eur", "SmartGrid daily regret", "EUR", SensorStateClass.MEASUREMENT),
    # 7-day rolling DP-vs-heuristic regret delta (EUR). Positive = DP was MORE
    # expensive than the heuristic over the past 7 days; negative = DP was
    # CHEAPER; None = insufficient data (< 1 day with both DP and heuristic
    # regret scored). Key deliberately distinct from regret_eur above to
    # prevent HA entity-ID collisions.
    _SensorSpec(
        "dp_regret_7d", "SmartGrid 7d DP-vs-heuristic regret delta", "EUR", SensorStateClass.MEASUREMENT
    ),
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
        return {
            attr_name: self._controller.last_status.get(status_key)
            for attr_name, status_key in self._attrs_keys
        }


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
            # Layer B occupancy corrector observability (controller._occ_status_attrs).
            "occ_state_now": self._controller.last_status.get("occ_state_now"),
            "occ_expected_state": self._controller.last_status.get("occ_expected_state"),
            "occ_multiplier": self._controller.last_status.get("occ_multiplier"),
            "occ_cells_ready": self._controller.last_status.get("occ_cells_ready"),
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
    # Keyed by spec.key so the entities list below can interleave spec-driven
    # sensors with the real (non-boilerplate) classes in their original order.
    spec_sensors = {
        spec.key: X1StatusSensor(controller, entry.entry_id, spec) for spec in SENSOR_SPECS
    }
    async_add_entities(
        [
            spec_sensors["state"],
            spec_sensors["solar_charge_kwh"],
            spec_sensors["setpoint_w"],
            spec_sensors["export_setpoint_w"],
            X1HouseLoadSensor(controller, entry.entry_id, pv_entities, meter_entity, batt_entity, loss_entity),
            spec_sensors["load_mae"],
            spec_sensors["horizon_energy_mae_24h"],
            spec_sensors["horizon_energy_mae_12h"],
            spec_sensors["pinball_p50"],
            spec_sensors["pinball_p80"],
            spec_sensors["active_model"],
            X1PlanSensor(controller, entry.entry_id),
            X1FictivePlanSensor(controller, entry.entry_id),
            spec_sensors["regret_eur"],
            spec_sensors["dp_regret_7d"],
            spec_sensors["over_buy_kwh"],
            spec_sensors["under_buy_kwh"],
            spec_sensors["battery_net_today_eur"],
            spec_sensors["battery_net_total_eur"],
        ]
    )
