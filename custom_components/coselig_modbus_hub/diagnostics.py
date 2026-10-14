"""Small, redacted, restart-local diagnostics snapshot."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.core import HomeAssistant

from . import CoseligConfigEntry
from .const import VERSION


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: CoseligConfigEntry
) -> dict[str, Any]:
    hub = entry.runtime_data
    coordinator, transport = hub.coordinator, hub.transport
    return {
        "integration": {"version": VERSION},
        "gateway": {
            "host": "**REDACTED**",
            "port": entry.data["port"],
            "connected": transport.connected,
        },
        "coordinator": {
            "poll_interval": coordinator.poll_interval,
            "queue_depth": transport.queue_depth,
            "queue_peak": transport.statistics.queue_peak,
        },
        "statistics": {
            key: value
            for key, value in asdict(transport.statistics).items()
            if key != "queue_peak"
        },
        "devices": [
            {
                **device.as_dict(),
                "available": coordinator.gateway_available
                and coordinator.states[device.slave].available,
                "consecutive_failures": coordinator.states[
                    device.slave
                ].consecutive_failures,
            }
            for device in coordinator.devices
        ],
    }
