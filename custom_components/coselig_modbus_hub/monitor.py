"""Read-only trace console: three admin WS commands and one static panel."""

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
    return {
        "connected": hub.transport.connected,
        "poll_interval": hub.coordinator.poll_interval,
        "polling_paused": hub.coordinator.polling_paused,
        "entries": hub.transport.trace.snapshot(),
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
        snapshot = (
            _snapshot(hub)
            if hub
            else {
                "connected": False,
                "poll_interval": None,
                "polling_paused": True,
                "entries": [],
            }
        )
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


async def async_register_monitor(hass: HomeAssistant) -> None:
    for handler in (websocket_list, websocket_subscribe, websocket_clear):
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
