import asyncio
from unittest.mock import Mock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.coselig_modbus_hub.coordinator import Coordinator
from custom_components.coselig_modbus_hub.light import CoseligLight, async_setup_entry
from custom_components.coselig_modbus_hub.models import Device, Model, raw_to_brightness
from custom_components.coselig_modbus_hub.transport import Transport


@pytest.fixture
async def lights(hass, gateway):
    device = Device(1, Model.P404, "cct", "single")
    transport = Transport("127.0.0.1", gateway.port, timeout=0.04)
    coordinator = Coordinator(hass, transport, (device,), 2)
    await coordinator.async_poll_round()
    entities = [
        CoseligLight(coordinator, "entry", device, output) for output in device.outputs
    ]
    for entity in entities:
        entity.hass = hass
        entity.async_write_ha_state = Mock()
    yield coordinator, entities
    await coordinator.stop()
    await transport.close()


async def test_optimistic_before_modbus_completion(gateway, lights):
    coordinator, entities = lights
    light = entities[0]
    assert light.brightness == raw_to_brightness(20)
    gateway.block = True
    gateway.arrived.clear()
    task = asyncio.create_task(light.async_turn_on(brightness=204))
    await gateway.arrived.wait()
    assert not task.done()
    assert light.brightness == 204
    light.async_write_ha_state.assert_called()
    # A poll that started before this operation must not undo it.
    light._handle_coordinator_update()
    assert light.brightness == 204
    gateway.release.set()
    await task
    await coordinator.async_poll_round()
    light._handle_coordinator_update()
    assert light.brightness == 204
    gateway.registers[1][0] = 65
    await coordinator.async_poll_round()
    light._handle_coordinator_update()
    assert light.brightness == raw_to_brightness(65)


async def test_write_failure_keeps_optimistic_then_corrects(gateway, lights):
    coordinator, entities = lights
    light = entities[0]
    gateway.modes[1] = "crc"
    with pytest.raises(HomeAssistantError):
        await light.async_turn_on(brightness=204)
    assert light.brightness == 204
    light._handle_coordinator_update()
    assert light.brightness == 204
    gateway.modes.clear()
    gateway.registers[1][0] = 65
    await coordinator.async_poll_round()
    light._handle_coordinator_update()
    assert light.brightness == raw_to_brightness(65)


async def test_on_off_remembers_brightness_and_cct(gateway, lights):
    coordinator, entities = lights
    light = entities[0]
    await light.async_turn_on(brightness=128, color_temp_kelvin=4000)
    assert light.is_on
    assert abs(light.color_temp_kelvin - 4000) < 30
    assert [r[1] for r in gateway.requests[-2:]] == [6, 6]
    await light.async_turn_off()
    assert not light.is_on
    await light.async_turn_on()
    assert light.is_on
    assert light.brightness == raw_to_brightness(50)
    await coordinator.async_poll_round()
    light._handle_coordinator_update()
    assert abs(light.color_temp_kelvin - 4000) < 30


async def test_unavailable_rejects_without_write(lights):
    coordinator, entities = lights
    coordinator.gateway_lost()
    light = entities[0]
    assert not light.available
    with pytest.raises(HomeAssistantError):
        await light.async_turn_on(brightness=204)
    with pytest.raises(HomeAssistantError):
        await light.async_turn_off()
    light.async_write_ha_state.assert_not_called()


async def test_single_and_entity_metadata(lights):
    coordinator, entities = lights
    light = entities[1]
    assert light.name == "CH3"
    assert light.unique_id == "entry_slave_1_ch3"
    assert light.has_entity_name
    assert light.device_info["manufacturer"] == "Coselig"
    assert light.device_info["model"] == "P404"
    assert light.device_info["identifiers"] == {("coselig_modbus_hub", "entry_slave_1")}
    await light.async_turn_on(brightness=255, color_temp_kelvin=4000)
    assert light.brightness == 255
    await light.async_turn_off()
    assert not light.is_on
    coordinator.scanning = True
    with pytest.raises(HomeAssistantError):
        await light.async_turn_on()


async def test_platform_creates_profiles(hass, entry, lights):
    coordinator, entities = lights
    entry.runtime_data = Mock(coordinator=coordinator)
    add = Mock()
    await async_setup_entry(hass, entry, add)
    assert len(list(add.call_args.args[0])) == 3


async def test_same_tick_on_then_off_latest_wins(gateway, lights):
    coordinator, entities = lights
    light = entities[0]
    gateway.block = True
    gateway.arrived.clear()
    blocker = asyncio.create_task(coordinator.async_poll_round())
    await gateway.arrived.wait()
    on = asyncio.create_task(light.async_turn_on(brightness=204))
    off = asyncio.create_task(light.async_turn_off())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    gateway.release.set()
    await asyncio.gather(blocker, on, off)
    writes = [r[5] for r in gateway.requests if r[1] == 6]
    assert writes == [0]
    assert gateway.registers[1][0] == 0


async def test_fast_cct_brightness_and_temperature_latest_wins(gateway, lights):
    coordinator, entities = lights
    light = entities[0]
    gateway.block = True
    gateway.arrived.clear()
    blocker = asyncio.create_task(coordinator.async_poll_round())
    await gateway.arrived.wait()
    calls = [
        asyncio.create_task(
            light.async_turn_on(brightness=brightness, color_temp_kelvin=kelvin)
        )
        for brightness, kelvin in ((51, 2200), (77, 3000), (102, 4500), (204, 5714))
    ]
    await asyncio.sleep(0)
    gateway.release.set()
    await asyncio.gather(blocker, *calls)
    writes = [r for r in gateway.requests if r[1] == 6]
    assert [(int.from_bytes(r[2:4], "big"), r[5]) for r in writes] == [
        (0x082A, 80),
        (0x082B, 100),
    ]
    assert light.brightness == 204
    assert light.color_temp_kelvin == 5714


async def test_p210_cct_uses_two_register_shared_conversion(gateway, hass):
    device = Device(62, Model.P210, "cct")
    assert [output.key for output in device.outputs] == ["group_a"]
    transport = Transport("127.0.0.1", gateway.port, timeout=0.04)
    coordinator = Coordinator(hass, transport, (device,), 2)
    await coordinator.async_poll_round()
    light = CoseligLight(coordinator, "entry", device, device.outputs[0])
    light.hass = hass
    light.async_write_ha_state = Mock()
    try:
        assert light.brightness == raw_to_brightness(20)
        await light.async_turn_on(brightness=128, color_temp_kelvin=4000)
        assert light.brightness == 128
        assert abs(light.color_temp_kelvin - 4000) < 30
        assert [
            int.from_bytes(request[2:4], "big") for request in gateway.requests[-2:]
        ] == [
            0x082A,
            0x082B,
        ]
    finally:
        await coordinator.stop()
        await transport.close()
