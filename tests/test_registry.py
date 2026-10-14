from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.coselig_modbus_hub import prune_registries
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
