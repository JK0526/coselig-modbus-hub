import asyncio
from unittest.mock import Mock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.coselig_modbus_hub.coordinator import Coordinator
from custom_components.coselig_modbus_hub.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.coselig_modbus_hub.models import Device, Model
from custom_components.coselig_modbus_hub.switch import CoseligSwitch
from custom_components.coselig_modbus_hub.transport import Transport


@pytest.fixture
async def relays(hass, gateway):
    device = Device(70, Model.R410)
    transport = Transport("127.0.0.1", gateway.port, timeout=0.02)
    coordinator = Coordinator(hass, transport, (device, Device(1, Model.P404)), 0)
    await coordinator.async_poll_round()
    entities = [
        CoseligSwitch(coordinator, "entry", device, o, "parent") for o in device.outputs
    ]
    for entity in entities:
        entity.hass = hass
        entity.async_write_ha_state = Mock()
    yield coordinator, entities
    await coordinator.stop()
    await transport.close()


async def test_optimistic_fc05_on_off_poll_correction(relays, gateway):
    coordinator, entities = relays
    relay = entities[1]
    gateway.block = True
    gateway.arrived.clear()
    task = asyncio.create_task(relay.async_turn_on())
    await gateway.arrived.wait()
    assert relay.is_on and not task.done()
    relay._handle_coordinator_update()
    assert relay.is_on
    gateway.release.set()
    await task
    assert gateway.requests[-1][:6] == bytes.fromhex("46050001ff00")
    await relay.async_turn_off()
    assert gateway.requests[-1][:6] == bytes.fromhex("460500010000")
    assert not relay.is_on
    gateway.coils[70][1] = True
    await coordinator.async_poll_round()
    relay._handle_coordinator_update()
    assert relay.is_on


@pytest.mark.parametrize("mode", ["crc", "timeout", "echo", "disconnect"])
async def test_write_failure_keeps_optimistic_then_corrects(relays, gateway, mode):
    coordinator, entities = relays
    relay = entities[1]
    gateway.modes[70] = mode
    with pytest.raises(HomeAssistantError):
        await relay.async_turn_on()
    assert relay.is_on
    relay._handle_coordinator_update()
    assert relay.is_on
    gateway.modes.clear()
    gateway.coils[70][1] = False
    await coordinator.async_poll_round()
    relay._handle_coordinator_update()
    assert not relay.is_on


async def test_poll_health_gateway_and_control_rejection(relays, gateway):
    coordinator, entities = relays
    gateway.modes[70] = "timeout"
    for failures in range(1, 4):
        await coordinator.async_poll_round()
        assert coordinator.states[70].consecutive_failures == failures
        assert entities[0].available == (failures < 3)
        assert coordinator.states[1].available
    with pytest.raises(HomeAssistantError):
        await entities[0].async_turn_off()
    gateway.modes.clear()
    await coordinator.async_poll_round()
    assert entities[0].available
    coordinator.scanning = True
    with pytest.raises(HomeAssistantError):
        await entities[0].async_turn_on()
    coordinator.scanning = False
    coordinator.gateway_lost()
    assert all(not entity.available for entity in entities)


async def test_queue_latest_wins_independent_and_control_priority(relays, gateway):
    coordinator, entities = relays
    gateway.block = True
    gateway.arrived.clear()
    gateway.requests.clear()
    blocker = asyncio.create_task(coordinator.async_poll_round())
    await gateway.arrived.wait()
    tasks = []
    for entity, on in [
        (entities[0], True),
        (entities[0], False),
        (entities[0], True),
        (entities[1], True),
    ]:
        tasks.append(
            asyncio.create_task(
                entity.async_turn_on() if on else entity.async_turn_off()
            )
        )
    for _ in range(6):
        await asyncio.sleep(0)
    gateway.release.set()
    await asyncio.gather(blocker, *tasks)
    assert [(r[0], r[1], int.from_bytes(r[2:4], "big")) for r in gateway.requests] == [
        (70, 1, 0),
        (70, 5, 0),
        (70, 5, 1),
        (1, 3, 0x082A),
    ]
    assert gateway.coils[70][:2] == [True, True]
    assert coordinator.transport.stateful_capacity == 256
    assert coordinator.transport.action_capacity == 32


async def test_r410_native_registry_poll_zero_reload_options_rescan(
    hass, entry, gateway
):
    gateway.families[70] = "relay"
    hass.config_entries.async_update_entry(
        entry,
        data={"host": "127.0.0.1", "port": gateway.port},
        options={
            **entry.options,
            "poll_interval": 0,
            "devices": [
                {"slave": 70, "model": "r410"},
                {"slave": 1, "model": "p404"},
                {"slave": 3, "model": "p210"},
            ],
        },
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data.coordinator
    registry = er.async_get(hass)
    devices = dr.async_get(hass)
    entities = er.async_entries_for_config_entry(registry, entry.entry_id)
    switches = [e for e in entities if e.domain == "switch"]
    assert len(switches) == 4
    assert len([e for e in entities if e.domain == "light"]) == 6
    assert {e.domain for e in entities} == {"light", "switch"}
    assert {e.unique_id for e in switches} == {
        f"{entry.entry_id}_slave_70_relay{i}" for i in range(1, 5)
    }
    assert len({e.device_id for e in switches}) == 4
    parents = dr.async_entries_for_config_entry(devices, entry.entry_id)
    parent = next(d for d in parents if d.model == "R410")
    assert parent.name == "Coselig R410 #70"
    children = dr.async_entries_for_parent_device(devices, parent.id)
    assert {c.name for c in children} == {f"R410 #70 · Relay {i}" for i in range(1, 5)}
    for i, e in enumerate(switches):
        area = ar.async_get(hass).async_create(f"Relay area {i}")
        devices.async_update_child_device(
            e.device_id, area_id=area.id, name_by_user=f"自訂 relay {i}"
        )
        registry.async_update_entity(e.entity_id, name=f"使用者 {i}")
    snapshot = {
        e.entity_id: (
            e.unique_id,
            e.device_id,
            devices.async_get(e.device_id).area_id,
            devices.async_get(e.device_id).name_by_user,
        )
        for e in switches
    }
    gateway.requests.clear()
    await asyncio.sleep(0.03)
    assert not gateway.requests
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": switches[1].entity_id}, blocking=True
    )
    assert len(gateway.requests) == 1 and gateway.requests[0][1] == 5
    await entry.runtime_data.scan(70, 70)
    assert all(s.available for s in coordinator.states.values())
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["coordinator"]["configured_slaves"] == 3
    assert diagnostics["gateway"]["host"] == "**REDACTED**"
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
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    # Scanning only reports the existing R410; Add cannot remove or reconfigure it.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    flow_id = result["flow_id"]
    await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": "device_management"}
    )
    await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": "add_device"}
    )
    await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": "search_modbus"}
    )
    await hass.config_entries.options.async_configure(
        flow_id, {"scan_start": 70, "scan_end": 70}
    )
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_configure(flow_id)
    assert result["step_id"] == "scan_no_new"
    assert len(entry.options["devices"]) == 3
    result = await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": "back_to_add_device"}
    )
    result = await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": "back_to_device_management"}
    )
    result = await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": "manage_devices"}
    )
    result = await hass.config_entries.options.async_configure(
        flow_id, {"device": "modbus:70"}
    )
    assert result["menu_options"] == [
        "r410_output_configuration",
        "remove_managed_device",
        "back_to_device_list",
    ]
    result = await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": "r410_output_configuration"}
    )
    assert result["menu_options"] == ["add_r410_cover", "back_to_device_menu"]
    await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": "back_to_device_menu"}
    )
    assert entry.options["devices"][0]["model"] == "r410"
    assert {
        e.entity_id for e in er.async_entries_for_config_entry(registry, entry.entry_id)
    } == {e.entity_id for e in entities}
    for entity_id, (
        unique_id,
        child_id,
        area_id,
        custom_device_name,
    ) in snapshot.items():
        e = registry.async_get(entity_id)
        child = devices.async_get(child_id)
        assert (e.unique_id, e.device_id, child.area_id, child.name_by_user) == (
            unique_id,
            child_id,
            area_id,
            custom_device_name,
        )
        assert child.parent_device_id == parent.id
        assert e.name.startswith("使用者")
    gateway.requests.clear()
    gateway.arrived.clear()
    entry.runtime_data.coordinator.set_poll_interval(2)
    await asyncio.wait_for(gateway.arrived.wait(), 1)
    async with entry.runtime_data.coordinator.pause():
        pass
    assert sorted(
        (r[0], r[1], int.from_bytes(r[4:6], "big")) for r in gateway.requests
    ) == [(1, 3, 4), (3, 3, 2), (70, 1, 4)]
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_relay_scan_and_controls_share_existing_trace(relays, gateway):
    from custom_components.coselig_modbus_hub.hub import Hub
    from custom_components.coselig_modbus_hub.trace import ProtocolTraceBuffer
    from tests.dimmer_packets import write

    coordinator, entities = relays
    gateway.families[70] = "relay"
    coordinator.transport.trace = ProtocolTraceBuffer()
    await coordinator.async_poll_round()
    await entities[0].async_turn_off()
    await coordinator.transport.submit(write(1, 0x082A, 49))
    await Hub(coordinator.transport, coordinator).scan(70, 70)
    entries = coordinator.transport.trace.snapshot()
    functions = {
        bytes.fromhex(e["raw_hex"])[1] for e in entries if e["direction"] == "TX"
    }
    assert functions == {1, 2, 3, 5, 6}
    assert {"TX", "RX", "OK", "ERROR"} <= {e["direction"] for e in entries}
    assert all(e["source"] == "scan" for e in entries[-9:])


async def test_paused_relay_reconnect_preserves_last_known_false_coils(relays, gateway):
    coordinator, entities = relays
    gateway.coils[70] = [False] * 4
    await coordinator.async_poll_round()
    coordinator.gateway_lost()
    gateway.requests.clear()

    async def sleep(_delay):
        pass

    async def wait(_timeout=None):
        coordinator._closed = True

    with (
        patch(
            "custom_components.coselig_modbus_hub.coordinator.asyncio.sleep", new=sleep
        ),
        patch.object(coordinator, "_wait_for_schedule", new=wait),
    ):
        await coordinator._loop()
    assert coordinator.states[70].available
    assert coordinator.states[70].coils == {0: False, 1: False, 2: False, 3: False}
    assert gateway.requests == []
