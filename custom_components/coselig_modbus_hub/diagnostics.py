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
            "polling_paused": coordinator.polling_paused,
            "configured_slaves": len(coordinator.devices),
            "available_slaves": sum(
                coordinator.gateway_available and state.available
                for state in coordinator.states.values()
            ),
            "last_poll_round_ms": round(coordinator.last_poll_round_ms, 3),
            "average_poll_round_ms": round(coordinator.average_poll_round_ms, 3),
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
        "s402_devices": [{"id": device_id} for device_id in hub.s402_ids],
    }
