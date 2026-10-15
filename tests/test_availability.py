"""Availability propagation through real HA entities and its state machine."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.light import DATA_COMPONENT
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.helpers import entity_registry as er

from custom_components.coselig_modbus_hub.coordinator import Coordinator, DeviceState
from custom_components.coselig_modbus_hub.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.coselig_modbus_hub.light import CoseligLight


@pytest.mark.parametrize(
    "group_a,group_b,entity_count",
    [
        ("single", "single", 6),
        ("cct", "single", 5),
        ("single", "cct", 5),
        ("cct", "cct", 4),
    ],
)
async def test_initial_unavailable_successful_poll_recovers_without_reload(
    hass, entry, gateway, group_a, group_b, entity_count
):
    hass.config_entries.async_update_entry(
        entry, data={"host": "127.0.0.1", "port": gateway.port}
    )
    options = {
        **entry.options,
        "devices": [
            {"slave": 1, "model": "p404", "group_a": group_a, "group_b": group_b},
            {"slave": 3, "model": "p210"},
        ],
    }
    hass.config_entries.async_update_entry(entry, options=options)
    # Keep TCP online but defer the first poll until all entities are registered.
    with (
        patch.object(Coordinator, "start"),
        patch.object(Coordinator, "async_poll_round", new=AsyncMock()),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data.coordinator
    entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    assert len(entities) == entity_count
    live = [
        hass.data[DATA_COMPONENT].get_entity(entity.entity_id) for entity in entities
    ]
    assert all(
        isinstance(entity, CoseligLight) and entity.coordinator is coordinator
        for entity in live
    )
    assert all(not entity.available for entity in live)
    assert all(
        hass.states.get(entity.entity_id).state == STATE_UNAVAILABLE
        for entity in entities
    )
    coordinator.last_update_success = False
    await coordinator.async_poll_round()
    await hass.async_block_till_done()
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert all(entity.available for entity in live)
    assert diagnostics["gateway"]["connected"]
    assert diagnostics["statistics"]["failed_requests"] == 0
    assert diagnostics["statistics"]["timeout_count"] == 0
    assert diagnostics["statistics"]["crc_error_count"] == 0
    assert all(device["consecutive_failures"] == 0 for device in diagnostics["devices"])
    assert all(device["available"] for device in diagnostics["devices"])
    assert coordinator.last_update_success
    assert all(hass.states.get(entity.entity_id).state == "on" for entity in entities)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_three_failures_then_success_update_ha_state_without_reload(
    hass, entry, gateway
):
    hass.config_entries.async_update_entry(
        entry, data={"host": "127.0.0.1", "port": gateway.port}
    )
    with patch.object(Coordinator, "start"):
        assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data.coordinator
    entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    affected = [entity for entity in entities if "_slave_1_" in entity.unique_id]
    other = [entity for entity in entities if "_slave_3_" in entity.unique_id]
    gateway.modes[1] = "timeout"
    for failures in range(1, 4):
        await coordinator.async_poll_round()
        await hass.async_block_till_done()
        assert coordinator.states[1].consecutive_failures == failures
        assert all(
            hass.states.get(entity.entity_id).state
            == (STATE_UNAVAILABLE if failures == 3 else "on")
            for entity in affected
        )
        assert all(hass.states.get(entity.entity_id).state == "on" for entity in other)
    gateway.modes.clear()
    await coordinator.async_poll_round()
    await hass.async_block_till_done()
    assert coordinator.states[1].available
    assert coordinator.states[1].consecutive_failures == 0
    assert all(hass.states.get(entity.entity_id).state == "on" for entity in entities)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_replacing_runtime_state_does_not_leave_stale_entity_copy(
    hass, entry, gateway
):
    hass.config_entries.async_update_entry(
        entry, data={"host": "127.0.0.1", "port": gateway.port}
    )
    with patch.object(Coordinator, "start"):
        assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data.coordinator
    entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    coordinator.states[1] = DeviceState(available=False, consecutive_failures=3)
    coordinator.async_set_updated_data(coordinator.states)
    affected = [entity for entity in entities if "_slave_1_" in entity.unique_id]
    assert all(
        hass.states.get(entity.entity_id).state == STATE_UNAVAILABLE
        for entity in affected
    )
    coordinator.states[1] = DeviceState(
        holding_registers={0x082A: 0, 0x082B: 50, 0x082C: 0, 0x082D: 50},
        available=True,
        poll_started=hass.loop.time(),
    )
    coordinator.async_set_updated_data(coordinator.states)
    assert all(
        hass.states.get(entity.entity_id).state != STATE_UNAVAILABLE
        for entity in affected
    )
    assert await hass.config_entries.async_unload(entry.entry_id)
