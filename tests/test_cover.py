"""R410 Cover actions and entity/registry lifecycle regressions."""

from unittest.mock import Mock
from uuid import uuid4

import pytest
from homeassistant.components.cover import CoverEntityFeature
from homeassistant.exceptions import ConfigEntryError, HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.coselig_modbus_hub import async_setup_entry
from custom_components.coselig_modbus_hub.const import DOMAIN
from custom_components.coselig_modbus_hub.coordinator import Coordinator
from custom_components.coselig_modbus_hub.cover import CoseligR410Cover
from custom_components.coselig_modbus_hub.models import Device, Model
from custom_components.coselig_modbus_hub.r410_cover import parse_r410_cover
from custom_components.coselig_modbus_hub.transport import Transport


def cover_config(**changes):
    return parse_r410_cover(
        {
            "id": uuid4().hex,
            "slave": 63,
            "name": "Garage",
            "device_class": "garage",
            "mode": "two_relay",
            "open_relay": 4,
            "close_relay": 1,
            "pulse_tenths": 5,
            **changes,
        }
    )


@pytest.fixture
async def cover_runtime(hass, gateway):
    gateway.families[63] = "relay"
    transport = Transport("127.0.0.1", gateway.port, timeout=0.025)
    coordinator = Coordinator(hass, transport, (Device(63, Model.R410),), 0)
    await coordinator.async_poll_round()
    config = cover_config()
    cover = CoseligR410Cover(coordinator, "entry", config)
    cover.hass = hass
    cover.async_write_ha_state = Mock()
    yield coordinator, cover, gateway
    await coordinator.stop()
    await transport.close()


async def test_cover_actions_use_019b_and_keep_state_unknown(cover_runtime):
    coordinator, cover, gateway = cover_runtime
    assert cover.available
    assert cover.supported_features == (
        CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP
    )
    assert cover.current_cover_position is None
    assert cover.is_closed is None
    assert cover.is_opening is None
    assert cover.is_closing is None
    assert cover.device_info["identifiers"] == {(DOMAIN, "entry_slave_63")}

    gateway.requests.clear()
    await cover.async_open_cover()
    await cover.async_stop_cover()
    await cover.async_close_cover()
    await cover.async_open_cover()
    assert [(address, value) for _slave, address, value in gateway.register_writes] == [
        (0x019B, 0x0508),
        (0x019B, 0x0509),
        (0x019B, 0x0501),
        (0x019B, 0x0508),
    ]
    assert [request[1] for request in gateway.requests] == [6, 6, 6, 6]
    assert cover.current_cover_position is None
    assert cover.is_closed is None
    assert cover.is_opening is None
    assert cover.is_closing is None
    assert coordinator.poll_interval == 0


async def test_cover_control_survives_runtime_poll_pause(cover_runtime):
    coordinator, cover, gateway = cover_runtime
    coordinator.set_poll_interval(2)
    assert coordinator.set_runtime_poll_paused(True)
    assert coordinator.runtime_poll_paused
    gateway.requests.clear()
    await cover.async_open_cover()
    assert [request[1] for request in gateway.requests] == [6]
    assert gateway.register_writes[-1] == (63, 0x019B, 0x0508)


async def test_three_relay_cover_stop_uses_only_mapped_stop_relay(cover_runtime):
    _coordinator, cover, gateway = cover_runtime
    cover.config = cover_config(
        mode="three_relay", open_relay=3, stop_relay=1, close_relay=4
    )
    gateway.requests.clear()
    gateway.register_writes.clear()
    await cover.async_stop_cover()
    assert gateway.requests[0][1] == 6
    assert gateway.register_writes == [(63, 0x019B, 0x0501)]


async def test_unavailable_scanning_and_uncertain_timeout_fail_without_replay(
    cover_runtime,
):
    coordinator, cover, gateway = cover_runtime
    gateway.requests.clear()
    coordinator.scanning = True
    with pytest.raises(HomeAssistantError):
        await cover.async_open_cover()
    assert not gateway.requests

    coordinator.scanning = False
    coordinator.states[63].available = False
    assert not cover.available
    with pytest.raises(HomeAssistantError):
        await cover.async_close_cover()
    assert not gateway.requests

    coordinator.states[63].available = True
    coordinator.gateway_lost()
    assert not cover.available
    with pytest.raises(HomeAssistantError):
        await cover.async_stop_cover()
    assert not gateway.requests


async def test_timeout_returns_homeassistant_error_without_retry(cover_runtime):
    coordinator, cover, gateway = cover_runtime
    gateway.requests.clear()
    gateway.register_writes.clear()
    gateway.modes[63] = "timeout"
    with pytest.raises(HomeAssistantError, match="no automatic retry"):
        await cover.async_open_cover()
    assert len(gateway.requests) == 1
    assert gateway.register_writes == [(63, 0x019B, 0x0508)]
    assert cover.current_cover_position is None
    assert cover.is_closed is None
    assert cover.is_opening is None
    assert cover.is_closing is None
    assert coordinator.states[63].available


async def test_gateway_disconnect_marks_cover_unavailable(cover_runtime):
    coordinator, cover, gateway = cover_runtime
    gateway.requests.clear()
    gateway.modes[63] = "disconnect"
    with pytest.raises(HomeAssistantError):
        await cover.async_open_cover()
    assert len(gateway.requests) == 1
    assert not coordinator.gateway_available
    assert not coordinator.states[63].available
    assert not cover.available


async def test_corrupt_cover_options_fail_setup_as_config_entry_error(hass, entry):
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            "devices": [{"slave": 63, "model": "r410"}],
            "r410_covers": [{"id": "bad"}],
        },
    )
    with pytest.raises(ConfigEntryError, match="Stored device or R410 Cover"):
        await async_setup_entry(hass, entry)


async def test_occupied_relay_registry_identity_and_metadata_survive_cover_reload(
    hass, entry, gateway
):
    gateway.families[63] = "relay"
    cover = {
        "id": uuid4().hex,
        "slave": 63,
        "name": "Garage door",
        "device_class": "garage",
        "mode": "two_relay",
        "open_relay": 1,
        "close_relay": 2,
        "pulse_tenths": 5,
    }
    raw_devices = Device(63, Model.R410)
    registry = er.async_get(hass)
    area = ar.async_get(hass).async_create("Garage")
    relay_entities = {}
    for output in raw_devices.outputs:
        entity = registry.async_get_or_create(
            "switch",
            DOMAIN,
            output.unique_id(entry.entry_id, 63),
            config_entry=entry,
            suggested_object_id=f"r410_63_relay_{output.registers[0] + 1}",
        )
        if output.registers[0] == 0:
            registry.async_update_entity(
                entity.entity_id, name="Custom garage relay", area_id=area.id
            )
        relay_entities[output.registers[0] + 1] = entity

    hass.config_entries.async_update_entry(
        entry,
        data={"host": "127.0.0.1", "port": gateway.port},
        options={
            **entry.options,
            "poll_interval": 0,
            "devices": [{"slave": 63, "model": "r410"}],
            "r410_covers": [cover],
        },
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    devices = dr.async_get(hass)
    parent = next(
        device
        for device in dr.async_entries_for_config_entry(devices, entry.entry_id)
        if device.model == "R410"
    )
    entries = er.async_entries_for_config_entry(registry, entry.entry_id)
    cover_entry = next(entity for entity in entries if entity.domain == "cover")
    assert cover_entry.unique_id == (f"{entry.entry_id}_slave_63_cover_{cover['id']}")
    assert cover_entry.device_id == parent.id
    assert hass.states.get(cover_entry.entity_id).state == "unknown"
    registry.async_update_entity(
        cover_entry.entity_id, name="Custom garage Cover", area_id=area.id
    )
    assert registry.async_get(relay_entities[1].entity_id).name == "Custom garage relay"
    assert registry.async_get(relay_entities[1].entity_id).area_id == area.id

    active_switch_states = {
        entity.entity_id: hass.states.get(entity.entity_id)
        for entity in entries
        if entity.domain == "switch"
    }
    assert active_switch_states[relay_entities[1].entity_id] is None
    assert active_switch_states[relay_entities[2].entity_id] is None
    assert active_switch_states[relay_entities[3].entity_id] is not None
    assert active_switch_states[relay_entities[4].entity_id] is not None
    assert (
        len([entity for entity in entries if entity.unique_id.endswith("relay1")]) == 1
    )
    assert not any(
        "_cover_" in identifier[1]
        for device in dr.async_entries_for_config_entry(devices, entry.entry_id)
        for identifier in device.identifiers
    )

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    reloaded_entries = er.async_entries_for_config_entry(registry, entry.entry_id)
    reloaded_cover = next(
        entity for entity in reloaded_entries if entity.domain == "cover"
    )
    assert reloaded_cover.entity_id == cover_entry.entity_id
    assert reloaded_cover.unique_id == cover_entry.unique_id
    assert reloaded_cover.name == "Custom garage Cover"
    assert reloaded_cover.area_id == area.id
    assert hass.states.get(reloaded_cover.entity_id).state == "unknown"
    assert len([entity for entity in reloaded_entries if entity.domain == "cover"]) == 1

    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            "r410_covers": [],
        },
    )
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    restored = er.async_get(hass).async_get(relay_entities[1].entity_id)
    assert restored.unique_id == relay_entities[1].unique_id
    assert restored.name == "Custom garage relay"
    assert restored.area_id == area.id
    assert hass.states.get(restored.entity_id) is not None
    assert not [
        entity
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id)
        if entity.domain == "cover"
    ]
    await hass.config_entries.async_unload(entry.entry_id)


async def test_two_covers_occupy_all_r410_relays_without_active_switches(
    hass, entry, gateway
):
    gateway.families[63] = "relay"
    covers = [
        {
            "id": uuid4().hex,
            "slave": 63,
            "name": name,
            "device_class": None,
            "mode": "two_relay",
            "open_relay": open_relay,
            "close_relay": close_relay,
            "pulse_tenths": 5,
        }
        for name, open_relay, close_relay in (
            ("Cover A", 1, 2),
            ("Cover B", 3, 4),
        )
    ]
    hass.config_entries.async_update_entry(
        entry,
        data={"host": "127.0.0.1", "port": gateway.port},
        options={
            **entry.options,
            "poll_interval": 0,
            "devices": [{"slave": 63, "model": "r410"}],
            "r410_covers": covers,
        },
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entries = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    assert {entity.domain for entity in entries} == {"cover"}
    assert len(entries) == 2
    assert all(
        hass.states.get(entity.entity_id).state == "unknown" for entity in entries
    )
    await hass.config_entries.async_unload(entry.entry_id)
