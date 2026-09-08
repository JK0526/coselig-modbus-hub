"""Admin-only WebSocket API used by the Coselig management panel."""

from __future__ import annotations

from collections.abc import Iterable

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant

from .const import (
    CONF_CHANNELS,
    CONF_POLL_INTERVAL,
    CONF_POLLING_ENABLED,
    DOMAIN,
)
from .devices import channel_from_mapping, validate_channels


def _entry(hass: HomeAssistant, entry_id: str):
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.entry_id == entry_id:
            return entry
    raise ValueError("Coselig Hub entry not found")


def _normalise_channel(value: dict) -> dict[str, object]:
    """Validate panel input and return JSON-safe persisted data."""
    channel = channel_from_mapping(value)
    return {
        "model": channel.model,
        "slave": channel.slave,
        "channel": channel.channel,
        "kind": channel.kind,
        "name": channel.name,
        "minimum": channel.minimum,
        "mired_min": channel.mired_min,
        "mired_max": channel.mired_max,
    }


def _channels(entry) -> list[dict[str, object]]:
    return [dict(channel) for channel in entry.options.get(CONF_CHANNELS, [])]


async def _clear_discovery(hass: HomeAssistant, entry, old: Iterable[dict], new: Iterable[dict]) -> None:
    """Clear configs that are removed or changed before a reload."""
    old_by_key = {(int(item["slave"]), str(item["channel"])): item for item in old}
    new_keys = {(int(item["slave"]), str(item["channel"])) for item in new}
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if runtime is None or runtime.mqtt_bridge is None:
        return
    for key, item in old_by_key.items():
        replacement = next(
            (candidate for candidate in new if (int(candidate["slave"]), str(candidate["channel"])) == key),
            None,
        )
        if key not in new_keys or replacement != item:
            await runtime.mqtt_bridge.async_remove_channel(item)


def async_register_websocket_commands(hass: HomeAssistant) -> None:
    """Register panel commands once during domain setup."""
    websocket_api.async_register_command(hass, websocket_list)
    websocket_api.async_register_command(hass, websocket_save_channel)
    websocket_api.async_register_command(hass, websocket_remove_channel)
    websocket_api.async_register_command(hass, websocket_set_polling)


@websocket_api.require_admin
@websocket_api.async_response
@websocket_api.websocket_command(
    {vol.Required("type"): "coselig_modbus_hub/list"}
)
async def websocket_list(hass: HomeAssistant, connection, msg: dict) -> None:
    """Return all configured hubs and their current runtime state."""
    entries = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        state = getattr(runtime, "state", None)
        entries.append(
            {
                "entry_id": entry.entry_id,
                "title": entry.title,
                "host": entry.data.get("host"),
                "port": entry.data.get("port"),
                "options": {
                    "polling_enabled": entry.options.get(CONF_POLLING_ENABLED, True),
                    "poll_interval": entry.options.get(CONF_POLL_INTERVAL, 3.0),
                },
                "channels": _channels(entry),
                "state": {
                    "connected": bool(getattr(state, "connected", False)),
                    "queue_depth": int(getattr(state, "queue_depth", 0)),
                    "last_error": getattr(state, "last_error", None),
                    "success_count": int(getattr(state, "success_count", 0)),
                    "timeout_count": int(getattr(state, "timeout_count", 0)),
                    "crc_error_count": int(getattr(state, "crc_error_count", 0)),
                },
            }
        )
    connection.send_result(msg["id"], {"entries": entries})


@websocket_api.require_admin
@websocket_api.async_response
@websocket_api.websocket_command(
    {
        vol.Required("type"): "coselig_modbus_hub/save_channel",
        vol.Required("entry_id"): str,
        vol.Required("channel"): dict,
    }
)
async def websocket_save_channel(hass: HomeAssistant, connection, msg: dict) -> None:
    """Create or update a channel, then reload the Hub runtime."""
    try:
        entry = _entry(hass, msg["entry_id"])
        channel = _normalise_channel(msg["channel"])
        channels = _channels(entry)
        key = (channel["slave"], channel["channel"])
        channels = [item for item in channels if (int(item["slave"]), str(item["channel"])) != key]
        new_channels = channels + [channel]
        validate_channels([channel_from_mapping(item) for item in new_channels])
        await _clear_discovery(hass, entry, _channels(entry), new_channels)
        options = dict(entry.options)
        options[CONF_CHANNELS] = new_channels
        hass.config_entries.async_update_entry(entry, options=options)
        await hass.config_entries.async_reload(entry.entry_id)
    except (KeyError, TypeError, ValueError, vol.Invalid) as error:
        connection.send_error(msg["id"], "invalid_channel", str(error))
        return
    connection.send_result(msg["id"], {"saved": channel})


@websocket_api.require_admin
@websocket_api.async_response
@websocket_api.websocket_command(
    {
        vol.Required("type"): "coselig_modbus_hub/remove_channel",
        vol.Required("entry_id"): str,
        vol.Required("slave"): vol.Coerce(int),
        vol.Required("channel"): str,
    }
)
async def websocket_remove_channel(hass: HomeAssistant, connection, msg: dict) -> None:
    """Remove one channel and clear its retained Discovery config."""
    try:
        entry = _entry(hass, msg["entry_id"])
        old_channels = _channels(entry)
        new_channels = [
            item
            for item in old_channels
            if (int(item["slave"]), str(item["channel"]))
            != (msg["slave"], msg["channel"])
        ]
        await _clear_discovery(hass, entry, old_channels, new_channels)
        options = dict(entry.options)
        options[CONF_CHANNELS] = new_channels
        hass.config_entries.async_update_entry(entry, options=options)
        await hass.config_entries.async_reload(entry.entry_id)
    except (KeyError, TypeError, ValueError) as error:
        connection.send_error(msg["id"], "invalid_channel", str(error))
        return
    connection.send_result(msg["id"], {"removed": True})


@websocket_api.require_admin
@websocket_api.async_response
@websocket_api.websocket_command(
    {
        vol.Required("type"): "coselig_modbus_hub/set_polling",
        vol.Required("entry_id"): str,
        vol.Required("enabled"): bool,
        vol.Optional("interval"): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=3600.0)),
    }
)
async def websocket_set_polling(hass: HomeAssistant, connection, msg: dict) -> None:
    """Change polling without changing the TCP identity."""
    try:
        entry = _entry(hass, msg["entry_id"])
        options = dict(entry.options)
        options[CONF_POLLING_ENABLED] = msg["enabled"]
        if "interval" in msg:
            options[CONF_POLL_INTERVAL] = msg["interval"]
        hass.config_entries.async_update_entry(entry, options=options)
    except (KeyError, TypeError, ValueError, vol.Invalid) as error:
        connection.send_error(msg["id"], "invalid_options", str(error))
        return
    connection.send_result(msg["id"], {"updated": True})
