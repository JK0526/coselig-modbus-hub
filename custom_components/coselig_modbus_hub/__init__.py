"""Native Home Assistant integration for Coselig RTU-over-TCP devices."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import DEFAULT_POLL_INTERVAL, DOMAIN
from .hub import Hub
from .models import Device, devices_from_options
from .s402 import s402_devices_from_options

type CoseligConfigEntry = ConfigEntry[Hub]

PLATFORMS = [Platform.LIGHT, Platform.SWITCH, Platform.EVENT]


def ensure_parent_device(
    hass: HomeAssistant, entry: CoseligConfigEntry, device: Device
) -> dr.DeviceEntry:
    """Create or retrieve the stable physical Slave parent device."""
    registry = dr.async_get(hass)
    return registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{entry.entry_id}_slave_{device.slave}")},
        name=f"Coselig {device.model.value.upper()} #{device.slave:02d}",
        manufacturer="Coselig",
        model=device.model.value.upper(),
    )


def ensure_s402_parent_device(
    hass: HomeAssistant, entry: CoseligConfigEntry, device_id: int
) -> dr.DeviceEntry:
    """Create the stable parent device for one S402 event input module."""
    registry = dr.async_get(hass)
    return registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{entry.entry_id}_s402_{device_id}")},
        name=f"S402 #{device_id}",
        manufacturer="Coselig",
        model="S402",
    )


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register the optional admin Monitor once for this integration domain."""
    from .monitor import async_register_monitor

    await async_register_monitor(hass)
    return True


def prune_registries(hass: HomeAssistant, entry: CoseligConfigEntry) -> None:
    """Remove only obsolete integration outputs; keep surviving user metadata."""
    devices = devices_from_options(entry.options)
    s402_ids = s402_devices_from_options(dict(entry.options))
    expected = {
        output.unique_id(entry.entry_id, device.slave)
        for device in devices
        for output in device.outputs
    }
    expected.update(
        f"{entry.entry_id}_s402_{device_id}_di{di}"
        for device_id in s402_ids
        for di in range(1, 5)
    )
    entities = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(entities, entry.entry_id):
        if entity.platform == DOMAIN and entity.unique_id not in expected:
            entities.async_remove(entity.entity_id)
    registry = dr.async_get(hass)
    identifiers = {
        (DOMAIN, f"{entry.entry_id}_slave_{device.slave}") for device in devices
    }
    identifiers.update(
        (DOMAIN, f"{entry.entry_id}_s402_{device_id}") for device_id in s402_ids
    )
    expected_identifiers = identifiers | {
        (DOMAIN, output.unique_id(entry.entry_id, device.slave))
        for device in devices
        for output in device.outputs
    }
    registry_devices = [
        *dr.async_child_entries_for_config_entry(registry, entry.entry_id),
        *dr.async_entries_for_config_entry(registry, entry.entry_id),
    ]
    for device in registry_devices:
        if not device.identifiers & expected_identifiers:
            registry.async_remove_device(device.id)
    for device in devices:
        ensure_parent_device(hass, entry, device)
    for device_id in s402_ids:
        ensure_s402_parent_device(hass, entry, device_id)


async def async_setup_entry(hass: HomeAssistant, entry: CoseligConfigEntry) -> bool:
    if entry.version != 2:
        raise ConfigEntryError(
            "v1.0 requires a new config entry; remove the legacy entry and add again"
        )
    hub = Hub.create(
        hass,
        entry.data["host"],
        entry.data["port"],
        devices_from_options(entry.options),
        entry.options.get("poll_interval", DEFAULT_POLL_INTERVAL),
        s402_devices_from_options(dict(entry.options)),
    )
    try:
        await hub.start()
    except (ConnectionError, TimeoutError) as error:
        await hub.stop()
        raise ConfigEntryNotReady("Cannot connect to gateway") from error
    except BaseException:
        await hub.stop()
        raise
    entry.runtime_data = hub

    async def stop_on_shutdown(event: Event) -> None:
        await hub.stop()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop_on_shutdown)
    )
    try:
        prune_registries(hass, entry)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        await hub.stop()
        raise
    return True


async def async_unload_entry(hass: HomeAssistant, entry: CoseligConfigEntry) -> bool:
    if await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.stop()
        return True
    return False


async def async_migrate_entry(hass: HomeAssistant, entry: CoseligConfigEntry) -> bool:
    """Clean break: never reinterpret channels as device configurations."""
    return entry.version == 2
