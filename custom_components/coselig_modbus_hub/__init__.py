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
from .models import devices_from_options

type CoseligConfigEntry = ConfigEntry[Hub]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register the optional read-only console once for this integration domain."""
    from .monitor import async_register_monitor

    await async_register_monitor(hass)
    return True


def prune_registries(hass: HomeAssistant, entry: CoseligConfigEntry) -> None:
    """Remove only obsolete integration outputs; keep surviving user metadata."""
    devices = devices_from_options(entry.options)
    expected = {
        output.unique_id(entry.entry_id, device.slave)
        for device in devices
        for output in device.outputs
    }
    entities = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(entities, entry.entry_id):
        if entity.platform == DOMAIN and entity.unique_id not in expected:
            entities.async_remove(entity.entity_id)
    registry = dr.async_get(hass)
    identifiers = {
        (DOMAIN, f"{entry.entry_id}_slave_{device.slave}") for device in devices
    }
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        if not device.identifiers & identifiers:
            registry.async_remove_device(device.id)


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
        await hass.config_entries.async_forward_entry_setups(entry, [Platform.LIGHT])
    except BaseException:
        await hub.stop()
        raise
    return True


async def async_unload_entry(hass: HomeAssistant, entry: CoseligConfigEntry) -> bool:
    if await hass.config_entries.async_unload_platforms(entry, [Platform.LIGHT]):
        await entry.runtime_data.stop()
        return True
    return False


async def async_migrate_entry(hass: HomeAssistant, entry: CoseligConfigEntry) -> bool:
    """Clean break: never reinterpret channels as device configurations."""
    return entry.version == 2
