import json

from custom_components.coselig_modbus_hub.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.coselig_modbus_hub.hub import Hub
from custom_components.coselig_modbus_hub.models import devices_from_options


async def test_redaction_and_runtime_counters(hass, entry):
    entry.runtime_data = Hub.create(
        hass,
        entry.data["host"],
        entry.data["port"],
        devices_from_options(entry.options),
        2,
    )
    hub = entry.runtime_data
    hub.transport.statistics.timeout_count = 3
    hub.coordinator.states[1].consecutive_failures = 3
    result = await async_get_config_entry_diagnostics(hass, entry)
    assert entry.data["host"] not in json.dumps(result)
    assert result["gateway"]["host"] == "**REDACTED**"
    assert result["statistics"]["timeout_count"] == 3
    assert result["coordinator"]["queue_depth"] == 0
    assert result["devices"][0]["group_a"] == "single"
    assert "group_a" not in result["devices"][1]
    assert result["devices"][0]["consecutive_failures"] == 3
    second = Hub.create(hass, "new", 502, (), 2)
    assert second.transport.statistics.timeout_count == 0
    await hub.stop()
    await second.stop()


async def test_poll_capacity_diagnostics_and_no_trace_history(hass, entry):
    entry.runtime_data = Hub.create(
        hass, entry.data["host"], 502, devices_from_options(entry.options), 0
    )
    hub = entry.runtime_data
    hub.coordinator.states[1].available = True
    hub.coordinator._round_times.extend([230, 450])
    hub.transport.trace.record("TX", b"private packet", source="poll")
    result = await async_get_config_entry_diagnostics(hass, entry)
    assert result["coordinator"]["configured_slaves"] == 2
    assert result["coordinator"]["available_slaves"] == 1
    assert result["coordinator"]["polling_paused"]
    assert result["coordinator"]["poll_interval"] == 0
    assert result["coordinator"]["last_poll_round_ms"] == 450
    assert result["coordinator"]["average_poll_round_ms"] == 340
    assert entry.data["host"] not in json.dumps(result)
    assert "private packet" not in json.dumps(result)
    assert "trace" not in result
    await hub.stop()
