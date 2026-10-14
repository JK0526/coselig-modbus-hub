from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.coselig_modbus_hub import (
    async_migrate_entry,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.coselig_modbus_hub.const import DOMAIN
from custom_components.coselig_modbus_hub.hub import Hub
from custom_components.coselig_modbus_hub.transport import GatewayError


async def test_initial_offline_retry_and_cleanup(hass, entry):
    hub = Hub.create(hass, "offline", 502, (), 2)
    with (
        patch("custom_components.coselig_modbus_hub.Hub.create", return_value=hub),
        patch.object(hub, "start", new=AsyncMock(side_effect=GatewayError())),
    ):
        with pytest.raises(ConfigEntryNotReady):
            await async_setup_entry(hass, entry)
    assert hub.transport._closed


async def test_setup_error_cleanup(hass, entry):
    hub = Hub.create(hass, "test", 502, (), 2)
    with (
        patch("custom_components.coselig_modbus_hub.Hub.create", return_value=hub),
        patch.object(hub, "start", new=AsyncMock(side_effect=ValueError())),
    ):
        with pytest.raises(ValueError):
            await async_setup_entry(hass, entry)
    assert hub.transport._closed


async def test_forward_platform_failure_cleanup(hass, entry):
    hub = Hub.create(hass, "test", 502, (), 2)
    with (
        patch("custom_components.coselig_modbus_hub.Hub.create", return_value=hub),
        patch.object(hub, "start", new=AsyncMock()),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            new=AsyncMock(side_effect=ValueError()),
        ),
    ):
        with pytest.raises(ValueError):
            await async_setup_entry(hass, entry)
    assert hub.transport._closed


async def test_unload_failure_preserves_runtime(hass, entry):
    entry.runtime_data = Mock(stop=AsyncMock())
    with patch.object(
        hass.config_entries, "async_unload_platforms", new=AsyncMock(return_value=False)
    ):
        assert not await async_unload_entry(hass, entry)
    entry.runtime_data.stop.assert_not_awaited()
    with patch.object(
        hass.config_entries, "async_unload_platforms", new=AsyncMock(return_value=True)
    ):
        assert await async_unload_entry(hass, entry)
    entry.runtime_data.stop.assert_awaited_once()


async def test_clean_break_version(hass, entry):
    assert await async_migrate_entry(hass, entry)
    old = MockConfigEntry(domain=DOMAIN, version=1, data={"host": "test", "port": 502})
    assert not await async_migrate_entry(hass, old)
    with pytest.raises(ConfigEntryError):
        await async_setup_entry(hass, old)


async def test_hub_start_tcp_loss_in_initial_poll(hass):
    hub = Hub.create(hass, "test", 502, (), 2)

    async def poll():
        hub.coordinator.gateway_lost()

    with (
        patch.object(hub.transport, "connect", new=AsyncMock()),
        patch.object(hub.coordinator, "async_poll_round", new=poll),
    ):
        with pytest.raises(ConnectionError):
            await hub.start()
    await hub.stop()


async def test_real_ha_platform_and_registry(hass, entry, gateway):
    hass.config_entries.async_update_entry(
        entry, data={"host": "127.0.0.1", "port": gateway.port}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    entities = er.async_entries_for_config_entry(registry, entry.entry_id)
    assert len(entities) == 6
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert len(devices) == 2  # No gateway device.
    assert {device.model for device in devices} == {"P404", "P210"}
    assert all(hass.states.get(entity.entity_id).state == "on" for entity in entities)
    target = next(
        entity for entity in entities if entity.unique_id.endswith("_slave_1_ch1")
    )
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": target.entity_id, "brightness": 204},
        blocking=True,
    )
    assert hass.states.get(target.entity_id).attributes["brightness"] == 204
    registry.async_update_entity(target.entity_id, name="客廳自訂名稱")
    await hass.async_block_till_done()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get(target.entity_id).name == "客廳自訂名稱"
    hub = entry.runtime_data
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert hub.transport._closed


async def test_shutdown_closes_socket_and_tasks(hass, entry, gateway):
    hass.config_entries.async_update_entry(
        entry, data={"host": "127.0.0.1", "port": gateway.port}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    hub = entry.runtime_data
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert hub.transport._closed
    assert hub.coordinator._task is None
    assert hub.transport._worker is None
    assert not hub.transport.connected
    assert await hass.config_entries.async_unload(entry.entry_id)
