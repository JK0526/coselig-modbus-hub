"""Preserve real v1.2.1 registry identities across the profile architecture change."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.helpers import (
    area_registry as ar,
)
from homeassistant.helpers import (
    device_registry as dr,
)
from homeassistant.helpers import (
    entity_registry as er,
)

from custom_components.coselig_modbus_hub import DOMAIN, prune_registries
from custom_components.coselig_modbus_hub.coordinator import Coordinator
from custom_components.coselig_modbus_hub.hub import Hub
from custom_components.coselig_modbus_hub.models import Device, Model


@pytest.mark.parametrize(
    "group_a,group_b",
    [("single", "single"), ("cct", "single"), ("single", "cct"), ("cct", "cct")],
)
async def test_v121_registry_identity_and_custom_metadata(
    hass, entry, gateway, group_a, group_b
):
    configured = [
        {"slave": 51, "model": "p404", "group_a": group_a, "group_b": group_b},
        {"slave": 62, "model": "p210", "group_a": "cct"},
    ]
    hass.config_entries.async_update_entry(
        entry,
        data={"host": "127.0.0.1", "port": gateway.port},
        options={**entry.options, "poll_interval": 0, "devices": configured},
    )
    device_registry = dr.async_get(hass)
    registry = er.async_get(hass)
    area = ar.async_get(hass).async_create("既有 Area")
    snapshot = {}
    parent_ids = set()
    for config in configured:
        device = Device.from_dict(config)
        parent = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, f"{entry.entry_id}_slave_{device.slave}")},
            name=f"Coselig {device.model.value.upper()} #{device.slave:02d}",
            manufacturer="Coselig",
            model=device.model.value.upper(),
        )
        parent_ids.add(parent.id)
        for output in device.outputs:
            unique_id = output.unique_id(entry.entry_id, device.slave)
            child = device_registry.async_get_or_create_child(
                config_entry_id=entry.entry_id,
                identifiers={(DOMAIN, unique_id)},
                name=f"{device.model.value.upper()} #{device.slave} · {output.name}",
                parent_device_id=parent.id,
            )
            device_registry.async_update_child_device(
                child.id, area_id=area.id, name_by_user=f"自訂裝置 {output.key}"
            )
            entity = registry.async_get_or_create(
                "light",
                DOMAIN,
                unique_id,
                config_entry=entry,
                device_id=child.id,
                has_entity_name=True,
                original_name=None,
            )
            registry.async_update_entity(
                entity.entity_id, name=f"自訂燈光 {output.key}"
            )
            snapshot[entity.entity_id] = (entity.id, unique_id, child.id, parent.id)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    for reload in range(2):
        assert {
            e.entity_id
            for e in er.async_entries_for_config_entry(registry, entry.entry_id)
        } == set(snapshot)
        assert {
            d.id
            for d in dr.async_entries_for_config_entry(device_registry, entry.entry_id)
        } == parent_ids
        for entity_id, (
            registry_id,
            unique_id,
            child_id,
            parent_id,
        ) in snapshot.items():
            entity = registry.async_get(entity_id)
            child = device_registry.async_get(child_id)
            assert (
                entity.id,
                entity.unique_id,
                entity.device_id,
                child.parent_device_id,
            ) == (registry_id, unique_id, child_id, parent_id)
            assert child.area_id == area.id
            assert child.name_by_user.startswith("自訂裝置")
            assert entity.name.startswith("自訂燈光")
        # Existing automation target continues to resolve using the exact old ID.
        await hass.services.async_call(
            "light", "turn_off", {"entity_id": next(iter(snapshot))}, blocking=True
        )
        assert hass.states.get(next(iter(snapshot))).state == "off"
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.version == 2 and entry.minor_version == 1
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_r410_first_poll_and_failures_propagate_to_ha(hass, entry, gateway):
    hass.config_entries.async_update_entry(
        entry,
        data={"host": "127.0.0.1", "port": gateway.port},
        options={
            **entry.options,
            "poll_interval": 0,
            "devices": [{"slave": 70, "model": "r410"}, {"slave": 1, "model": "p404"}],
        },
    )
    with (
        patch.object(Coordinator, "start"),
        patch.object(Coordinator, "async_poll_round", new=AsyncMock()),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    switches = [
        e
        for e in er.async_entries_for_config_entry(registry, entry.entry_id)
        if e.domain == "switch"
    ]
    coordinator = entry.runtime_data.coordinator
    assert all(
        hass.states.get(e.entity_id).state == STATE_UNAVAILABLE for e in switches
    )
    await coordinator.async_poll_round()
    assert all(
        hass.states.get(e.entity_id).state != STATE_UNAVAILABLE for e in switches
    )
    gateway.modes[70] = "exception"
    for _ in range(3):
        await coordinator.async_poll_round()
    assert all(
        hass.states.get(e.entity_id).state == STATE_UNAVAILABLE for e in switches
    )
    assert coordinator.states[1].available
    gateway.modes.clear()
    await coordinator.async_poll_round()
    assert all(
        hass.states.get(e.entity_id).state != STATE_UNAVAILABLE for e in switches
    )
    # A scan that receives unsupported family probes cannot change runtime health.
    gateway.families[70] = "unknown"
    before = (
        coordinator.states[70].consecutive_failures,
        coordinator.states[70].available,
    )
    await entry.runtime_data.scan(70, 70)
    assert (
        coordinator.states[70].consecutive_failures,
        coordinator.states[70].available,
    ) == before
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_remove_parent_cascades_children_without_double_remove(
    hass, entry, gateway
):
    hass.config_entries.async_update_entry(
        entry,
        data={"host": "127.0.0.1", "port": gateway.port},
        options={
            **entry.options,
            "poll_interval": 0,
            "devices": [{"slave": 70, "model": "r410"}],
        },
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "devices": []}
    )
    prune_registries(hass, entry)
    assert not dr.async_child_entries_for_config_entry(
        dr.async_get(hass), entry.entry_id
    )
    assert not dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert not er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)


async def test_six_dimmers_one_relay_seven_requests_per_round(hass, gateway):
    devices = tuple(Device(i, Model.P404) for i in range(1, 7)) + (
        Device(70, Model.R410),
    )
    hub = Hub.create(hass, "127.0.0.1", gateway.port, devices, 0)
    try:
        await hub.coordinator.async_poll_round()
        assert [
            (r[0], r[1], int.from_bytes(r[4:6], "big")) for r in gateway.requests
        ] == [(i, 3, 4) for i in range(1, 7)] + [(70, 1, 4)]
        hub.coordinator.gateway_lost()
        assert all(not state.available for state in hub.coordinator.states.values())
    finally:
        await hub.stop()
