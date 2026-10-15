"""Trace console with admin-only, runtime background polling controls."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import probatio as vol
from homeassistant.components import panel_custom, websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN, VERSION


def _hub(hass: HomeAssistant, entry_id: str) -> Any:
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN or not hasattr(entry, "runtime_data"):
        return None
    return entry.runtime_data


def _snapshot(hub: Any) -> dict[str, Any]:
    devices = [
        {
            "slave": device.slave,
            "model": device.model.value,
            "group_a": device.group_a.value if device.group_a is not None else None,
            "group_b": device.group_b.value if device.group_b is not None else None,
            "available": hub.coordinator.states[device.slave].available,
            "consecutive_failures": hub.coordinator.states[
                device.slave
            ].consecutive_failures,
        }
        for device in hub.coordinator.devices
    ]
    statistics = hub.transport.statistics
    return {
        "connected": hub.transport.connected,
        "poll_interval": hub.coordinator.poll_interval,
        "polling_paused": hub.coordinator.polling_paused,
        "runtime_poll_paused": hub.coordinator.runtime_poll_paused,
        "polling_state": (
            "disabled"
            if hub.coordinator.polling_paused
            else "paused"
            if hub.coordinator.runtime_poll_paused
            else "running"
        ),
        "coordinator": {
            "configured_slaves": len(devices),
            "available_slaves": sum(device["available"] for device in devices),
            "last_poll_round_ms": round(hub.coordinator.last_poll_round_ms, 1),
            "average_poll_round_ms": round(hub.coordinator.average_poll_round_ms, 1),
            "queue_depth": hub.transport.queue_depth,
            "queue_peak": statistics.queue_peak,
        },
        "statistics": {
            "successful_requests": statistics.successful_requests,
            "failed_requests": statistics.failed_requests,
            "timeout_count": statistics.timeout_count,
            "crc_error_count": statistics.crc_error_count,
            "reconnect_count": statistics.reconnect_count,
        },
        "devices": devices,
        "s402_devices": [{"id": device_id} for device_id in hub.s402_ids],
        "entries": hub.transport.trace.snapshot(),
    }


def _empty_snapshot() -> dict[str, Any]:
    return {
        "connected": False,
        "poll_interval": None,
        "polling_paused": True,
        "runtime_poll_paused": False,
        "polling_state": "unloaded",
        "coordinator": {
            "configured_slaves": 0,
            "available_slaves": 0,
            "last_poll_round_ms": 0.0,
            "average_poll_round_ms": 0.0,
            "queue_depth": 0,
            "queue_peak": 0,
        },
        "statistics": {
            "successful_requests": 0,
            "failed_requests": 0,
            "timeout_count": 0,
            "crc_error_count": 0,
            "reconnect_count": 0,
        },
        "devices": [],
        "s402_devices": [],
        "entries": [],
    }


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/monitor/list"})
@websocket_api.require_admin
@callback
def websocket_list(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    connection.send_result(
        msg["id"],
        [
            {"entry_id": entry.entry_id, "title": entry.title}
            for entry in hass.config_entries.async_entries(DOMAIN)
            if hasattr(entry, "runtime_data")
        ],
    )


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/monitor/subscribe", vol.Required("entry_id"): str}
)
@websocket_api.require_admin
@callback
def websocket_subscribe(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    if _hub(hass, msg["entry_id"]) is None:
        connection.send_error(
            msg["id"], "not_loaded", "Gateway integration is not loaded"
        )
        return
    previous: dict[str, Any] | None = None

    @callback
    def publish(_now: Any = None) -> None:
        nonlocal previous
        hub = _hub(hass, msg["entry_id"])
        snapshot = _snapshot(hub) if hub else _empty_snapshot()
        if snapshot != previous:
            connection.send_event(msg["id"], snapshot)
            previous = snapshot

    # Reading the ring buffer sends no Modbus requests. Timer belongs to the WS subscription.
    connection.subscriptions[msg["id"]] = async_track_time_interval(
        hass, publish, timedelta(milliseconds=500)
    )
    connection.send_result(msg["id"])
    publish()


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/monitor/clear", vol.Required("entry_id"): str}
)
@websocket_api.require_admin
@callback
def websocket_clear(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    hub = _hub(hass, msg["entry_id"])
    if hub is None:
        connection.send_error(
            msg["id"], "not_loaded", "Gateway integration is not loaded"
        )
        return
    hub.transport.trace.clear()
    connection.send_result(msg["id"])


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/monitor/poll",
        vol.Required("entry_id"): str,
        vol.Required("paused"): vol.Boolean(),
    }
)
@websocket_api.require_admin
@callback
def websocket_poll(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Change only this loaded Hub's runtime background polling state."""
    hub = _hub(hass, msg["entry_id"])
    if hub is None:
        connection.send_error(
            msg["id"], "not_loaded", "Gateway integration is not loaded"
        )
        return
    coordinator = hub.coordinator
    if not coordinator.set_runtime_poll_paused(msg["paused"]):
        connection.send_error(
            msg["id"],
            "polling_disabled",
            "Background polling is disabled by the configured poll interval",
        )
        return
    connection.send_result(
        msg["id"],
        {
            "runtime_poll_paused": coordinator.runtime_poll_paused,
            "polling_state": "paused" if coordinator.runtime_poll_paused else "running",
        },
    )


async def async_register_monitor(hass: HomeAssistant) -> None:
    for handler in (
        websocket_list,
        websocket_subscribe,
        websocket_clear,
        websocket_poll,
    ):
        websocket_api.async_register_command(hass, handler)
    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(
                f"/{DOMAIN}/monitor-{VERSION}.js",
                str(Path(__file__).with_name("monitor.js")),
                True,
            )
        ]
    )
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path="coselig-modbus-monitor",
        webcomponent_name="coselig-modbus-monitor",
        sidebar_title="Coselig Modbus Monitor",
        sidebar_icon="mdi:console-line",
        module_url=f"/{DOMAIN}/monitor-{VERSION}.js",
        require_admin=True,
    )
