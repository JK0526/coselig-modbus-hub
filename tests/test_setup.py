import asyncio
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
from custom_components.coselig_modbus_hub.protocol import frame
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


async def test_s402_only_gateway_event_entities_availability_and_reload_lifecycle(
    hass, entry, gateway
):
    hass.config_entries.async_update_entry(
        entry,
        data={"host": "127.0.0.1", "port": gateway.port},
        options={
            "poll_interval": 0,
            "scan_start": 1,
            "scan_end": 247,
            "devices": [],
            "s402_devices": [{"id": 7}],
        },
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    hub = entry.runtime_data
    assert hub.coordinator.devices == ()
    assert hub.s402_ids == (7,)
    assert hub.transport.connected
    assert hub.transport.statistics.successful_requests == 0

    entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    assert len(entities) == 4
    assert {entity.platform for entity in entities} == {"coselig_modbus_hub"}
    assert {entity.unique_id for entity in entities} == {
        f"{entry.entry_id}_s402_7_di{di}" for di in range(1, 5)
    }
    device_registry = dr.async_get(hass)
    parents = dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    assert len(parents) == 1
    assert parents[0].name == "S402 #7"
    assert parents[0].manufacturer == "Coselig"
    assert parents[0].model == "S402"

    entity_by_di = {
        int(entity.unique_id.rsplit("di", 1)[1]): entity for entity in entities
    }
    states = hass.states
    assert states.get(entity_by_di[1].entity_id).state != "unavailable"
    assert states.get(entity_by_di[1].entity_id).attributes["event_types"] == [
        "press_end"
    ]

    state_changes: list[str] = []
    unsub = hass.bus.async_listen(
        "state_changed",
        lambda event: state_changes.append(event.data["entity_id"]),
    )
    writer = next(writer for writer in gateway.writers if not writer.is_closing())
    writer.write(frame(bytes([1, 6, 1, 0xA0, 7, 4])))
    await writer.drain()
    for _ in range(20):
        await hass.async_block_till_done()
        if entity_by_di[3].entity_id in state_changes:
            break
        await asyncio.sleep(0.01)
    assert state_changes.count(entity_by_di[3].entity_id) == 1
    assert states.get(entity_by_di[3].entity_id).attributes["event_type"] == "press_end"
    assert all(
        states.get(entity_by_di[di].entity_id).state == "unknown" for di in (1, 2, 4)
    )

    writer = next(writer for writer in gateway.writers if not writer.is_closing())
    writer.close()
    await writer.wait_closed()
    for _ in range(100):
        await hass.async_block_till_done()
        if not hub.coordinator.gateway_available:
            break
        await asyncio.sleep(0.01)
    assert not hub.coordinator.gateway_available
    assert all(
        states.get(entity.entity_id).state == "unavailable" for entity in entities
    )
    for _ in range(150):
        await hass.async_block_till_done()
        if hub.coordinator.gateway_available:
            break
        await asyncio.sleep(0.01)
    assert hub.coordinator.gateway_available
    assert all(
        states.get(entity.entity_id).state != "unavailable" for entity in entities
    )

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    state_changes.clear()
    for _ in range(20):
        writers = [writer for writer in gateway.writers if not writer.is_closing()]
        if writers:
            break
        await asyncio.sleep(0.01)
    writer = next(iter(writers))
    writer.write(frame(bytes([1, 6, 1, 0xA0, 7, 4])))
    await writer.drain()
    for _ in range(20):
        await hass.async_block_till_done()
        if any(entity_id.endswith("_s402_7_di3") for entity_id in state_changes):
            break
        await asyncio.sleep(0.01)
    current = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    current_di3 = next(
        entity for entity in current if entity.unique_id.endswith("_di3")
    )
    assert state_changes.count(current_di3.entity_id) == 1
    unsub()
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_multiple_s402_display_names_and_reload_identity(hass, entry, gateway):
    from homeassistant.helpers import area_registry as ar

    hass.config_entries.async_update_entry(
        entry,
        data={"host": "127.0.0.1", "port": gateway.port},
        options={
            "poll_interval": 0,
            "scan_start": 1,
            "scan_end": 247,
            "devices": [],
            "s402_devices": [{"id": 7}, {"id": 12}],
        },
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)

    def current_parents():
        return {
            next(
                value for domain, value in device.identifiers if domain == DOMAIN
            ): device
            for device in dr.async_entries_for_config_entry(
                device_registry, entry.entry_id
            )
        }

    expected_identifiers = {
        f"{entry.entry_id}_s402_7": "S402 #7",
        f"{entry.entry_id}_s402_12": "S402 #12",
    }
    parents = current_parents()
    assert {
        identifier: device.name for identifier, device in parents.items()
    } == expected_identifiers
    assert {
        identifier: device.identifiers for identifier, device in parents.items()
    } == {identifier: {(DOMAIN, identifier)} for identifier in expected_identifiers}

    entities = er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    assert len(entities) == 8
    entity_ids = {entity.unique_id: entity.entity_id for entity in entities}
    assert set(entity_ids) == {
        f"{entry.entry_id}_s402_{device_id}_di{di}"
        for device_id in (7, 12)
        for di in range(1, 5)
    }
    for device_id in (7, 12):
        parent_id = parents[f"{entry.entry_id}_s402_{device_id}"].id
        assert {
            entity.device_id
            for entity in entities
            if entity.unique_id.startswith(f"{entry.entry_id}_s402_{device_id}_")
        } == {parent_id}

    area = ar.async_get(hass).async_create("S402 room")
    custom_entity_id = entity_ids[f"{entry.entry_id}_s402_12_di2"]
    entity_registry.async_update_entity(
        custom_entity_id, name="Entrance DI", area_id=area.id
    )
    custom_device = parents[f"{entry.entry_id}_s402_12"]
    device_registry.async_update_device(custom_device.id, name_by_user="Entry S402")

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    reloaded_entities = er.async_entries_for_config_entry(
        entity_registry, entry.entry_id
    )
    reloaded_parents = current_parents()
    assert len(reloaded_entities) == 8
    assert {
        entity.unique_id: entity.entity_id for entity in reloaded_entities
    } == entity_ids
    assert {
        identifier: device.id for identifier, device in reloaded_parents.items()
    } == {identifier: device.id for identifier, device in parents.items()}
    assert {
        identifier: device.name for identifier, device in reloaded_parents.items()
    } == expected_identifiers
    assert reloaded_parents[f"{entry.entry_id}_s402_12"].name_by_user == "Entry S402"
    custom_entity = entity_registry.async_get(custom_entity_id)
    assert custom_entity is not None
    assert custom_entity.name == "Entrance DI"
    assert custom_entity.area_id == area.id
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_runtime_poll_pause_clears_on_entry_reload(hass, entry, gateway):
    hass.config_entries.async_update_entry(
        entry,
        data={"host": "127.0.0.1", "port": gateway.port},
        options={
            "poll_interval": 2.0,
            "scan_start": 1,
            "scan_end": 247,
            "devices": [],
        },
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    previous_hub = entry.runtime_data
    assert previous_hub.coordinator.set_runtime_poll_paused(True)
    assert previous_hub.coordinator.runtime_poll_paused
    assert entry.options["poll_interval"] == 2.0

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data is not previous_hub
    assert not entry.runtime_data.coordinator.runtime_poll_paused
    assert entry.runtime_data.coordinator.poll_interval == 2.0
    assert entry.options["poll_interval"] == 2.0
    assert await hass.config_entries.async_unload(entry.entry_id)


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
