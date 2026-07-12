"""Tests for the Anker X1 device resolver."""

from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.anker_x1_smartgrid import const
from custom_components.anker_x1_smartgrid.anker_resolver import (
    apply_anker_resolution,
    resolve_anker_config,
)

# suffix -> (platform domain, suggested object id => final entity_id)
_ROLES = {
    "soc": ("sensor", "anker_x1_battery_soc"),
    "battery_power": ("sensor", "anker_x1_battery_power"),
    "battery_setpoint": ("number", "anker_x1_battery_setpoint_charge_discharge"),
    "work_mode_select": ("select", "anker_x1_work_mode"),
    "modbus_control": ("switch", "anker_x1_modbus_control_hand_battery_to_ha_vpp"),
    "meter_total_power": ("sensor", "anker_x1_meter_total_power"),
    "inverter_loss": ("sensor", "anker_x1_inverter_loss"),
    "usable_pv_power": ("sensor", "anker_x1_usable_pv_power"),
}


def _register_anker_device(hass, *, drop=(), capacity_state="10.0"):
    """Register a mock anker_x1 device + entities. Returns (device_id, src_entry)."""
    src = MockConfigEntry(domain=const.ANKER_X1_DOMAIN, title="Anker X1")
    src.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=src.entry_id,
        identifiers={(const.ANKER_X1_DOMAIN, "SER-TEST-1")},
    )
    reg = er.async_get(hass)
    for suffix, (platform, oid) in _ROLES.items():
        if suffix in drop:
            continue
        reg.async_get_or_create(
            platform,
            const.ANKER_X1_DOMAIN,
            f"{src.entry_id}_{suffix}",
            config_entry=src,
            device_id=device.id,
            suggested_object_id=oid,
        )
    if capacity_state is not None:
        cap = reg.async_get_or_create(
            "sensor",
            const.ANKER_X1_DOMAIN,
            f"{src.entry_id}_{const.ANKER_CAPACITY_SUFFIX}",
            config_entry=src,
            device_id=device.id,
            suggested_object_id="anker_x1_battery_nominal_capacity",
        )
        hass.states.async_set(cap.entity_id, capacity_state)
    return device.id, src


async def test_resolve_all_roles_present(hass):
    device_id, _ = _register_anker_device(hass)
    resolved, missing = resolve_anker_config(hass, device_id)
    assert missing == []
    assert resolved[const.CONF_ENT_SOC] == "sensor.anker_x1_battery_soc"
    assert resolved[const.CONF_ENT_BATTERY_POWER] == "sensor.anker_x1_battery_power"
    assert resolved[const.CONF_ENT_SETPOINT] == "number.anker_x1_battery_setpoint_charge_discharge"
    assert resolved[const.CONF_ENT_WORKMODE] == "select.anker_x1_work_mode"
    assert resolved[const.CONF_ENT_ENGAGE] == "switch.anker_x1_modbus_control_hand_battery_to_ha_vpp"
    assert resolved[const.CONF_ENT_METER_POWER] == "sensor.anker_x1_meter_total_power"
    assert resolved[const.CONF_ENT_INVERTER_LOSS] == "sensor.anker_x1_inverter_loss"
    assert resolved[const.CONF_CAPACITY_KWH] == 10.0


async def test_resolve_meter_power_and_inverter_loss_roles(hass):
    """Single-scalar meter power + inverter loss resolve like the hard roles when present."""
    device_id, _ = _register_anker_device(hass)
    resolved, missing = resolve_anker_config(hass, device_id)
    assert missing == []
    assert resolved[const.CONF_ENT_METER_POWER] == "sensor.anker_x1_meter_total_power"
    assert resolved[const.CONF_ENT_INVERTER_LOSS] == "sensor.anker_x1_inverter_loss"


async def test_resolve_missing_meter_power_and_inverter_loss_soft(hass):
    """Meter power / inverter loss are SOFT roles: a miss is omitted from the
    resolved values but must NOT be reported as missing (must not block setup
    on anker_x1 versions without these entities)."""
    device_id, _ = _register_anker_device(hass, drop=("meter_total_power", "inverter_loss"))
    resolved, missing = resolve_anker_config(hass, device_id)
    assert missing == []
    assert const.CONF_ENT_METER_POWER not in resolved
    assert const.CONF_ENT_INVERTER_LOSS not in resolved
    # hard roles still resolve normally
    assert resolved[const.CONF_ENT_SOC] == "sensor.anker_x1_battery_soc"


async def test_resolve_missing_one_soft_role_still_resolves_other(hass):
    device_id, _ = _register_anker_device(hass, drop=("inverter_loss",))
    resolved, missing = resolve_anker_config(hass, device_id)
    assert missing == []
    assert resolved[const.CONF_ENT_METER_POWER] == "sensor.anker_x1_meter_total_power"
    assert const.CONF_ENT_INVERTER_LOSS not in resolved


async def test_resolve_missing_role_reported(hass):
    device_id, _ = _register_anker_device(hass, drop=("soc",))
    resolved, missing = resolve_anker_config(hass, device_id)
    assert const.CONF_ENT_SOC in missing
    assert const.CONF_ENT_BATTERY_POWER not in missing
    assert const.CONF_ENT_SOC not in resolved


async def test_resolve_exact_match_not_endswith(hass):
    device_id, src = _register_anker_device(hass)
    # decoy whose unique_id ENDS WITH "_soc" but is not the real soc role
    er.async_get(hass).async_get_or_create(
        "sensor",
        const.ANKER_X1_DOMAIN,
        f"{src.entry_id}_house_soc",
        config_entry=src,
        device_id=device_id,
        suggested_object_id="anker_x1_house_soc",
    )
    resolved, missing = resolve_anker_config(hass, device_id)
    assert resolved[const.CONF_ENT_SOC] == "sensor.anker_x1_battery_soc"
    assert missing == []


async def test_resolve_capacity_unavailable_omitted(hass):
    device_id, _ = _register_anker_device(hass, capacity_state="unavailable")
    resolved, missing = resolve_anker_config(hass, device_id)
    assert const.CONF_CAPACITY_KWH not in resolved
    assert missing == []  # capacity is soft


async def test_resolve_capacity_non_numeric_omitted(hass):
    device_id, _ = _register_anker_device(hass, capacity_state="N/A")
    resolved, _ = resolve_anker_config(hass, device_id)
    assert const.CONF_CAPACITY_KWH not in resolved


async def test_resolve_capacity_sensor_absent_omitted(hass):
    device_id, _ = _register_anker_device(hass, capacity_state=None)
    resolved, missing = resolve_anker_config(hass, device_id)
    assert const.CONF_CAPACITY_KWH not in resolved
    assert missing == []


async def test_resolve_unknown_device_all_missing(hass):
    resolved, missing = resolve_anker_config(hass, "no_such_device")
    assert resolved == {}
    assert set(missing) == set(const.ANKER_ROLE_SUFFIXES)


async def test_apply_resolution_refreshes_in_memory(hass):
    device_id, _ = _register_anker_device(hass, capacity_state="15.0")
    data = {const.CONF_ANKER_DEVICE: device_id, const.CONF_ENT_SOC: "sensor.stale_soc"}
    apply_anker_resolution(hass, data)
    assert data[const.CONF_ENT_SOC] == "sensor.anker_x1_battery_soc"
    assert data[const.CONF_CAPACITY_KWH] == 15.0


async def test_apply_resolution_no_device_is_noop(hass):
    data = {const.CONF_ENT_SOC: "sensor.stale_soc"}
    apply_anker_resolution(hass, data)
    assert data[const.CONF_ENT_SOC] == "sensor.stale_soc"


async def test_apply_resolution_missing_role_keeps_stored(hass):
    device_id, _ = _register_anker_device(hass, drop=("soc",))
    data = {const.CONF_ANKER_DEVICE: device_id, const.CONF_ENT_SOC: "sensor.stale_soc"}
    apply_anker_resolution(hass, data)
    assert data[const.CONF_ENT_SOC] == "sensor.stale_soc"  # unresolved → kept
    assert data[const.CONF_ENT_BATTERY_POWER] == "sensor.anker_x1_battery_power"  # resolved


async def test_resolve_usable_pv_power_soft_role(hass):
    device_id, _ = _register_anker_device(hass)
    resolved, missing = resolve_anker_config(hass, device_id)
    assert missing == []
    assert resolved[const.CONF_ENT_PV_POWER] == "sensor.anker_x1_usable_pv_power"


async def test_resolve_missing_usable_pv_power_is_soft(hass):
    device_id, _ = _register_anker_device(hass, drop=("usable_pv_power",))
    resolved, missing = resolve_anker_config(hass, device_id)
    assert const.CONF_ENT_PV_POWER not in resolved  # miss omitted
    assert const.CONF_ENT_PV_POWER not in missing  # soft: never blocks setup


async def test_apply_resolution_preserves_configured_pv_list(hass):
    """A user-configured (non-empty) ent_pv_power list must survive reload
    unchanged, even though the device exposes its own usable_pv_power sensor
    (AC-coupled sites: Anker-native PV reads ~0 W and must never clobber a
    real PV source). Other roles still refresh from the device as normal."""
    device_id, _ = _register_anker_device(hass)
    data = {
        const.CONF_ANKER_DEVICE: device_id,
        const.CONF_ENT_PV_POWER: ["sensor.envoy_x"],
        const.CONF_ENT_SOC: "sensor.stale_soc",
    }
    apply_anker_resolution(hass, data)
    assert data[const.CONF_ENT_PV_POWER] == ["sensor.envoy_x"]
    assert data[const.CONF_ENT_SOC] == "sensor.anker_x1_battery_soc"


async def test_apply_resolution_preserves_configured_pv_legacy_string(hass):
    """Same as above, but the stored ent_pv_power is a legacy single string."""
    device_id, _ = _register_anker_device(hass)
    data = {
        const.CONF_ANKER_DEVICE: device_id,
        const.CONF_ENT_PV_POWER: "sensor.envoy_x",
    }
    apply_anker_resolution(hass, data)
    assert data[const.CONF_ENT_PV_POWER] == "sensor.envoy_x"


async def test_apply_resolution_fills_pv_when_absent(hass):
    """When ent_pv_power is absent entirely, the device's usable_pv_power IS
    applied as the default (first-time setup / no user PV config yet)."""
    device_id, _ = _register_anker_device(hass)
    data = {const.CONF_ANKER_DEVICE: device_id}
    apply_anker_resolution(hass, data)
    assert data[const.CONF_ENT_PV_POWER] == "sensor.anker_x1_usable_pv_power"


async def test_apply_resolution_fills_pv_when_empty_list(hass):
    """When ent_pv_power is present but an empty list, the device's
    usable_pv_power IS applied — same treatment as absent."""
    device_id, _ = _register_anker_device(hass)
    data = {const.CONF_ANKER_DEVICE: device_id, const.CONF_ENT_PV_POWER: []}
    apply_anker_resolution(hass, data)
    assert data[const.CONF_ENT_PV_POWER] == "sensor.anker_x1_usable_pv_power"
