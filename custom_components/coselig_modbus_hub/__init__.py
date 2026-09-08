"""Coselig Modbus Hub Home Assistant integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

from .const import (
    CONF_HOST,
    CONF_CHANNELS,
    CONF_POLL_INTERVAL,
    CONF_POLLING_ENABLED,
    CONF_PORT,
    CONF_TIMEOUT,
    DOMAIN,
    PLATFORMS,
)
from .runtime import HubRuntime


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the domain; entries own their runtimes."""
    hass.data.setdefault(DOMAIN, {})
    from .panel import async_register_panel
    from .websocket import async_register_websocket_commands

    async_register_websocket_commands(hass)
    await async_register_panel(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Start a configured gateway runtime."""
    runtime = HubRuntime(
        host=entry.data[CONF_HOST],
        port=entry.data[CONF_PORT],
        timeout=entry.data[CONF_TIMEOUT],
        polling_enabled=entry.options.get(CONF_POLLING_ENABLED, True),
        poll_interval=entry.options.get(CONF_POLL_INTERVAL, 3.0),
        channels=list(entry.options.get(CONF_CHANNELS, [])),
        hass=hass,
        hub_id=entry.entry_id,
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime
    async def _async_update_listener(_hass: HomeAssistant, updated_entry: ConfigEntry) -> None:
        """Apply options without replacing the transport connection."""
        await runtime.async_set_poll_interval(
            updated_entry.options.get(CONF_POLL_INTERVAL, 3.0)
        )
        await runtime.async_set_polling(
            updated_entry.options.get(CONF_POLLING_ENABLED, True)
        )

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    try:
        await runtime.async_start()
    except Exception:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        await runtime.async_stop()
        raise
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Stop and remove a gateway runtime."""
    runtime = hass.data[DOMAIN].pop(entry.entry_id)
    await runtime.async_stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
