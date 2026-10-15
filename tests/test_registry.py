from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.coselig_modbus_hub import (
    ensure_s402_parent_device,
    prune_registries,
)
from custom_components.coselig_modbus_hub.const import DOMAIN
from custom_components.coselig_modbus_hub.models import Device, Model


async def populate(hass, entry):
    registry = er.async_get(hass)
    devices = dr.async_get(hass)
    device = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{entry.entry_id}_slave_1")},
        manufacturer="Coselig",
        model="P404",
        name="Coselig P404 #01",
    )
    for output in Device(1, Model.P404).outputs:
        entity = registry.async_get_or_create(
            "light",
            DOMAIN,
            output.unique_id(entry.entry_id, 1),
            config_entry=entry,
            device_id=device.id,
            suggested_object_id=output.key,
        )
        registry.async_update_entity(entity.entity_id, name=f"使用者 {output.name}")
    return registry, devices, device


@pytest.mark.parametrize("group", ["a", "b"])
async def test_group_mode_lifecycle_only_target_group(hass, entry, group):
    registry, devices, device = await populate(hass, entry)
    before = {
        entity.unique_id: entity
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    target = {
        "slave": 1,
        "model": "p404",
        "group_a": "cct" if group == "a" else "single",
        "group_b": "cct" if group == "b" else "single",
    }
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "devices": [target]}
    )
    prune_registries(hass, entry)
    surviving = (3, 4) if group == "a" else (1, 2)
    for index in surviving:
        unique_id = f"{entry.entry_id}_slave_1_ch{index}"
        assert registry.async_get(before[unique_id].entity_id) == before[unique_id]
        assert (
            registry.async_get(before[unique_id].entity_id).name == f"使用者 CH{index}"
        )
    for index in {1, 2, 3, 4} - set(surviving):
        assert (
            registry.async_get(before[f"{entry.entry_id}_slave_1_ch{index}"].entity_id)
            is None
        )
    cct = registry.async_get_or_create(
        "light",
        DOMAIN,
        f"{entry.entry_id}_slave_1_group_{group}",
        config_entry=entry,
        device_id=device.id,
    )
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "devices": [{"slave": 1, "model": "p404"}]}
    )
    prune_registries(hass, entry)
    assert registry.async_get(cct.entity_id) is None
    assert devices.async_get(device.id).manufacturer == "Coselig"
    assert devices.async_get(device.id).model == "P404"
    for index in surviving:
        assert (
            registry.async_get(before[f"{entry.entry_id}_slave_1_ch{index}"].entity_id)
            == before[f"{entry.entry_id}_slave_1_ch{index}"]
        )


async def test_p210_group_a_mode_lifecycle_and_area(hass, entry):
    registry = er.async_get(hass)
    devices = dr.async_get(hass)
    device = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{entry.entry_id}_slave_62")},
        manufacturer="Coselig",
        model="P210",
        name="Coselig P210 #62",
    )
    for output in Device(62, Model.P210).outputs:
        registry.async_get_or_create(
            "light",
            DOMAIN,
            output.unique_id(entry.entry_id, 62),
            config_entry=entry,
            device_id=device.id,
        )
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            "devices": [{"slave": 62, "model": "p210", "group_a": "cct"}],
        },
    )
    prune_registries(hass, entry)
    assert registry.async_get(f"light.{entry.entry_id}_slave_62_ch1") is None
    assert registry.async_get(f"light.{entry.entry_id}_slave_62_ch2") is None
    group = registry.async_get_or_create(
        "light",
        DOMAIN,
        f"{entry.entry_id}_slave_62_group_a",
        config_entry=entry,
        device_id=device.id,
    )
    from homeassistant.helpers import area_registry as ar

    area = ar.async_get(hass).async_create("P210 CCT")
    registry.async_update_entity(group.entity_id, area_id=area.id, name="保留 CCT")
    prune_registries(hass, entry)
    saved = registry.async_get(group.entity_id)
    assert saved.area_id == area.id
    assert saved.name == "保留 CCT"


async def test_device_removal_is_explicit(hass, entry):
    registry, devices, device = await populate(hass, entry)
    hass.config_entries.async_update_entry(
        entry, data={"host": "another-ip", "port": 503}
    )
    prune_registries(hass, entry)
    assert devices.async_get(device.id) is not None
    assert len(er.async_entries_for_config_entry(registry, entry.entry_id)) == 4
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "devices": []}
    )
    prune_registries(hass, entry)
    assert devices.async_get(device.id) is None
    assert not er.async_entries_for_config_entry(registry, entry.entry_id)


async def test_s402_registry_pruning_removes_only_selected_s402(hass, entry):
    from homeassistant.helpers import area_registry as ar

    options = {
        **entry.options,
        "devices": [{"slave": 51, "model": "p404"}],
        "s402_devices": [{"id": 7}, {"id": 12}],
    }
    hass.config_entries.async_update_entry(entry, options=options)
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    p404 = Device(51, Model.P404)
    parent = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{entry.entry_id}_slave_51")},
        name="Coselig P404 #51",
        manufacturer="Coselig",
        model="P404",
    )
    area = ar.async_get(hass).async_create("P404 room")
    outputs = {}
    for output in p404.outputs:
        unique_id = output.unique_id(entry.entry_id, 51)
        entity = entity_registry.async_get_or_create(
            "light", DOMAIN, unique_id, config_entry=entry, device_id=parent.id
        )
        entity_registry.async_update_entity(
            entity.entity_id, name=f"Custom {output.name}", area_id=area.id
        )
        outputs[unique_id] = (entity.entity_id, f"Custom {output.name}")
    s402_entities = {}
    s402_devices = {}
    for device_id in (7, 12):
        device = ensure_s402_parent_device(hass, entry, device_id)
        s402_devices[device_id] = device.id
        for di in range(1, 5):
            unique_id = f"{entry.entry_id}_s402_{device_id}_di{di}"
            entity = entity_registry.async_get_or_create(
                "event",
                DOMAIN,
                unique_id,
                config_entry=entry,
                device_id=device.id,
                has_entity_name=True,
                original_name=f"DI{di}",
            )
            s402_entities[(device_id, di)] = entity.entity_id

    prune_registries(hass, entry)
    assert all(
        entity_registry.async_get(entity_id) for entity_id in s402_entities.values()
    )
    hass.config_entries.async_update_entry(
        entry,
        options={**options, "s402_devices": [{"id": 12}]},
    )
    prune_registries(hass, entry)
    assert all(
        entity_registry.async_get(s402_entities[(7, di)]) is None for di in range(1, 5)
    )
    assert all(
        entity_registry.async_get(s402_entities[(12, di)]) is not None
        for di in range(1, 5)
    )
    assert device_registry.async_get(s402_devices[7]) is None
    assert device_registry.async_get(s402_devices[12]) is not None
    for unique_id, (entity_id, name) in outputs.items():
        saved = entity_registry.async_get(entity_id)
        assert saved is not None
        assert saved.unique_id == unique_id
        assert saved.name == name
        assert saved.area_id == area.id


async def test_reconfigure_registry_user_name_preserved(hass, entry):
    registry, devices, device = await populate(hass, entry)
    before = list(er.async_entries_for_config_entry(registry, entry.entry_id))
    with (
        patch(
            "custom_components.coselig_modbus_hub.config_flow.validate_gateway",
            new=AsyncMock(return_value={"host": "new.local", "port": 502}),
        ),
        patch.object(hass.config_entries, "async_schedule_reload"),
    ):
        await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "reconfigure", "entry_id": entry.entry_id},
            data={"host": "new.local", "port": 502},
        )
    prune_registries(hass, entry)
    assert list(er.async_entries_for_config_entry(registry, entry.entry_id)) == before
    assert devices.async_get(device.id) == device


async def test_native_entity_areas_survive_reload_rescan_and_options(
    hass, entry, gateway
):
    from homeassistant.helpers import area_registry as ar

    hass.config_entries.async_update_entry(
        entry, data={"host": "127.0.0.1", "port": gateway.port}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    entities = er.async_entries_for_config_entry(registry, entry.entry_id)
    affected = [entity for entity in entities if "_slave_1_" in entity.unique_id]
    rooms = ar.async_get(hass)
    areas = [rooms.async_create(name) for name in ["客廳", "餐廳", "玄關", "廚房"]]
    for entity, area in zip(affected, areas, strict=True):
        registry.async_update_entity(
            entity.entity_id, area_id=area.id, name="自訂 " + area.name
        )
    device = dr.async_get(hass).async_get(affected[0].device_id)
    assert device.area_id is None
    await hass.async_block_till_done()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await entry.runtime_data.scan(1, 3)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "communication"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "polling"}
    )
    await hass.config_entries.options.async_configure(
        result["flow_id"], {"poll_interval": 0}
    )
    await hass.async_block_till_done()
    for entity, area in zip(affected, areas, strict=True):
        saved = registry.async_get(entity.entity_id)
        assert saved.area_id == area.id
        assert saved.name == "自訂 " + area.name
    assert await hass.config_entries.async_unload(entry.entry_id)
