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
from custom_components.coselig_modbus_hub.models import Device, Model
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
    registry_devices = dr.async_get(hass)
    parents = dr.async_entries_for_config_entry(registry_devices, entry.entry_id)
    children = dr.async_child_entries_for_config_entry(registry_devices, entry.entry_id)
    devices = [*parents, *children]
    assert len(devices) == 8  # Two parents and six logical output children.
    assert {device.model for device in parents} == {"P404", "P210"}
    assert len(children) == 6
    assert {identifier for child in children for identifier in child.identifiers} == {
        (DOMAIN, entity.unique_id) for entity in entities
    }
    assert {child.parent_device_id for child in children} == {
        parent.id for parent in parents
    }
    child_names = {next(iter(child.identifiers)): child.name for child in children}
    assert child_names == {
        (DOMAIN, entity.unique_id): (
            "P404 #1 · " + entity.unique_id.rsplit("_", 1)[-1].upper()
            if "_slave_1_" in entity.unique_id
            else "P210 #3 · " + entity.unique_id.rsplit("_", 1)[-1].upper()
        )
        for entity in entities
    }
    assert {
        hass.states.get(entity.entity_id).attributes["friendly_name"]
        for entity in entities
    } == set(child_names.values())
    assert all(
        not hasattr(child, "manufacturer") and not hasattr(child, "model")
        for child in children
    )
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


async def test_v112_registry_migrates_parent_entities_to_children(hass, entry, gateway):
    """Existing v1.1.2 entities keep IDs and metadata while gaining child devices."""
    from homeassistant.helpers import area_registry as ar

    hass.config_entries.async_update_entry(
        entry, data={"host": "127.0.0.1", "port": gateway.port}
    )
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    rooms = ar.async_get(hass)
    areas = [rooms.async_create("舊區域 A"), rooms.async_create("舊區域 B")]
    parent_ids: dict[int, str] = {}
    old_entities: dict[str, tuple[str, str, str, str]] = {}
    for configured, area in zip(
        (Device(1, Model.P404), Device(3, Model.P210)), areas, strict=True
    ):
        parent = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, f"{entry.entry_id}_slave_{configured.slave}")},
            name=f"Coselig {configured.model.value.upper()} #{configured.slave:02d}",
            manufacturer="Coselig",
            model=configured.model.value.upper(),
        )
        parent_ids[configured.slave] = parent.id
        for output in configured.outputs:
            unique_id = output.unique_id(entry.entry_id, configured.slave)
            entity = entity_registry.async_get_or_create(
                "light",
                DOMAIN,
                unique_id,
                config_entry=entry,
                device_id=parent.id,
            )
            custom_name = f"舊版 {output.name}"
            entity_registry.async_update_entity(
                entity.entity_id, name=custom_name, area_id=area.id
            )
            old_entities[unique_id] = (
                entity.entity_id,
                custom_name,
                area.id,
                parent.id,
            )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    for unique_id, (entity_id, custom_name, area_id, parent_id) in old_entities.items():
        current = entity_registry.async_get(entity_id)
        assert current is not None
        assert current.unique_id == unique_id
        assert current.name == custom_name
        assert current.area_id == area_id
        assert current.device_id != parent_id
        child = device_registry.async_get(current.device_id)
        assert child is not None
        assert child.parent_device_id == parent_id

    assert {
        device.id
        for device in dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    } == set(parent_ids.values())
    children = dr.async_child_entries_for_config_entry(device_registry, entry.entry_id)
    assert {next(iter(child.identifiers)) for child in children} == {
        (DOMAIN, unique_id) for unique_id in old_entities
    }
    assert {child.parent_device_id for child in children} == set(parent_ids.values())
    old_entity_ids = {entity_id for entity_id, *_ in old_entities.values()}
    old_child_ids = {next(iter(child.identifiers)): child.id for child in children}
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert {
        entity.entity_id
        for entity in er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    } == old_entity_ids
    reloaded_children = dr.async_child_entries_for_config_entry(
        device_registry, entry.entry_id
    )
    assert {
        next(iter(child.identifiers)): child.id for child in reloaded_children
    } == old_child_ids
    for unique_id, (
        entity_id,
        custom_name,
        area_id,
        _parent_id,
    ) in old_entities.items():
        current = entity_registry.async_get(entity_id)
        assert current is not None
        assert current.unique_id == unique_id
        assert current.name == custom_name
        assert current.area_id == area_id
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_v120_default_names_migrate_without_duplicate_entities(
    hass, entry, gateway
):
    """v1.2.0 default names become prefixed without changing registry IDs."""
    hass.config_entries.async_update_entry(
        entry, data={"host": "127.0.0.1", "port": gateway.port}
    )
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    old_entity_ids: dict[str, str] = {}
    for configured in (Device(1, Model.P404), Device(3, Model.P210)):
        parent = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, f"{entry.entry_id}_slave_{configured.slave}")},
            name=f"Coselig {configured.model.value.upper()} #{configured.slave:02d}",
            manufacturer="Coselig",
            model=configured.model.value.upper(),
        )
        for output in configured.outputs:
            unique_id = output.unique_id(entry.entry_id, configured.slave)
            child = device_registry.async_get_or_create_child(
                config_entry_id=entry.entry_id,
                identifiers={(DOMAIN, unique_id)},
                name=output.name,
                parent_device_id=parent.id,
            )
            entity = entity_registry.async_get_or_create(
                "light",
                DOMAIN,
                unique_id,
                config_entry=entry,
                device_id=child.id,
                has_entity_name=True,
                original_name=output.name,
            )
            old_entity_ids[unique_id] = entity.entity_id

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entities = er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    assert len(entities) == len(old_entity_ids)
    assert {entity.unique_id: entity.entity_id for entity in entities} == old_entity_ids
    children = dr.async_child_entries_for_config_entry(device_registry, entry.entry_id)
    assert {next(iter(child.identifiers)): child.name for child in children} == {
        (DOMAIN, unique_id): (
            "P404 #1 · " + unique_id.rsplit("_", 1)[-1].upper()
            if "_slave_1_" in unique_id
            else "P210 #3 · " + unique_id.rsplit("_", 1)[-1].upper()
        )
        for unique_id in old_entity_ids
    }
    assert len({entity.unique_id for entity in entities}) == len(old_entity_ids)
    assert all(entity.name is None for entity in entities)
    assert {
        hass.states.get(entity.entity_id).attributes["friendly_name"]
        for entity in entities
    } == {child.name for child in children}
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_cct_outputs_register_as_group_children(hass, entry, gateway):
    hass.config_entries.async_update_entry(
        entry,
        data={"host": "127.0.0.1", "port": gateway.port},
        options={
            **entry.options,
            "devices": [
                {"slave": 1, "model": "p404", "group_a": "cct", "group_b": "cct"},
                {"slave": 3, "model": "p210", "group_a": "cct"},
            ],
        },
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    assert {entity.unique_id for entity in entities} == {
        f"{entry.entry_id}_slave_1_group_a",
        f"{entry.entry_id}_slave_1_group_b",
        f"{entry.entry_id}_slave_3_group_a",
    }
    children = dr.async_child_entries_for_config_entry(
        dr.async_get(hass), entry.entry_id
    )
    assert {identifier for child in children for identifier in child.identifiers} == {
        (DOMAIN, entity.unique_id) for entity in entities
    }
    assert {next(iter(child.identifiers)): child.name for child in children} == {
        (DOMAIN, f"{entry.entry_id}_slave_1_group_a"): "P404 #1 · 雙色溫 A",
        (DOMAIN, f"{entry.entry_id}_slave_1_group_b"): "P404 #1 · 雙色溫 B",
        (DOMAIN, f"{entry.entry_id}_slave_3_group_a"): "P210 #3 · 雙色溫 A",
    }
    assert await hass.config_entries.async_unload(entry.entry_id)


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
