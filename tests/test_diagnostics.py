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
