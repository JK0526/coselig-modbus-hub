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
        CoseligLight(coordinator, "entry", device, output, "parent")
        for output in device.outputs
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


async def test_zero_brightness_turn_on_does_not_replace_remembered_level(
    gateway, lights
):
    _coordinator, entities = lights
    light = entities[0]
    remembered = light._last_brightness

    await light.async_turn_on(brightness=0)

    assert not light.is_on
    assert light._last_brightness == remembered


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
    assert light.name is None
    assert light.unique_id == "entry_slave_1_ch3"
    assert light.has_entity_name
    assert light.device_info["name"] == "P404 #1 · CH3"
    assert light.device_info["identifiers"] == {
        ("coselig_modbus_hub", "entry_slave_1_ch3")
    }
    assert light.device_info["parent_device_id"] == "parent"
    assert "manufacturer" not in light.device_info
    assert "model" not in light.device_info
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
    light = CoseligLight(coordinator, "entry", device, device.outputs[0], "parent")
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


@pytest.mark.parametrize("turn_on", [True, False])
async def test_light_queue_full_leaves_state_unchanged_with_polling_disabled(
    gateway, lights, turn_on
):
    coordinator, entities = lights
    light = entities[0]
    coordinator.set_poll_interval(0)
    coordinator.transport.stateful_capacity = 1
    gateway.block = True
    gateway.arrived.clear()
    blocker = asyncio.create_task(coordinator.async_poll_round())
    await gateway.arrived.wait()
    filler = asyncio.create_task(entities[1].async_turn_on(brightness=60))
    await asyncio.sleep(0)
    before = (
        light.is_on,
        light.brightness,
        light.color_temp_kelvin,
        light._last_brightness,
    )
    try:
        with pytest.raises(HomeAssistantError) as rejected:
            if turn_on:
                await light.async_turn_on(brightness=190)
            else:
                await light.async_turn_off()
        assert isinstance(rejected.value.__cause__, asyncio.QueueFull)
        assert (
            light.is_on,
            light.brightness,
            light.color_temp_kelvin,
            light._last_brightness,
        ) == before
        light.async_write_ha_state.assert_not_called()
    finally:
        gateway.release.set()
        await asyncio.gather(blocker, filler)


async def test_cct_queue_rejection_does_not_replace_accepted_brightness(
    gateway, lights
):
    coordinator, entities = lights
    light = entities[0]
    coordinator.set_runtime_poll_paused(True)
    coordinator.transport.stateful_capacity = 1
    gateway.block = True
    gateway.arrived.clear()
    blocker = asyncio.create_task(coordinator.async_poll_round())
    await gateway.arrived.wait()
    old = asyncio.create_task(light.async_turn_on(brightness=102))
    await asyncio.sleep(0)
    before = light.color_temp_kelvin
    try:
        with pytest.raises(HomeAssistantError) as rejected:
            await light.async_turn_on(brightness=204, color_temp_kelvin=4000)
        assert isinstance(rejected.value.__cause__, asyncio.QueueFull)
        assert light.brightness == 102
        assert light.color_temp_kelvin == before
    finally:
        gateway.release.set()
        await asyncio.gather(blocker, old)
    assert [(r[3], r[5]) for r in gateway.requests if r[1] == 6] == [(0x2A, 40)]


async def test_discarded_newer_write_rolls_back_to_sent_uncertain_value(
    gateway, lights
):
    coordinator, entities = lights
    light = entities[0]
    coordinator.set_poll_interval(0)
    gateway.modes[1] = "timeout"
    gateway.arrived.clear()
    older = asyncio.create_task(light.async_turn_on(brightness=102))
    await gateway.arrived.wait()
    newer = asyncio.create_task(light.async_turn_on(brightness=204))
    await asyncio.sleep(0)
    assert light.brightness == 204
    results = await asyncio.gather(older, newer, return_exceptions=True)
    assert all(isinstance(result, HomeAssistantError) for result in results)
    assert isinstance(results[0].__cause__, TimeoutError)
    assert light.brightness == 102
    assert [r[5] for r in gateway.requests if r[1] == 6] == [40]


async def test_superseded_and_cancelled_writes_do_not_leave_optimistic_state(
    gateway, lights
):
    coordinator, entities = lights
    light = entities[0]
    coordinator.set_poll_interval(0)
    gateway.block = True
    gateway.arrived.clear()
    blocker = asyncio.create_task(coordinator.async_poll_round())
    await gateway.arrived.wait()
    initial = light.brightness
    older = asyncio.create_task(light.async_turn_on(brightness=102))
    newer = asyncio.create_task(light.async_turn_on(brightness=204))
    await older
    assert light.brightness == 204
    newer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await newer
    assert light.brightness == initial
    gateway.release.set()
    await blocker
    assert not [r for r in gateway.requests if r[1] == 6]


async def test_partial_wire_cct_failure_only_rolls_back_unsent_temperature(
    gateway, lights
):
    coordinator, entities = lights
    light = entities[0]
    coordinator.set_poll_interval(0)
    before = light.color_temp_kelvin
    gateway.modes[1] = "timeout"
    with pytest.raises(HomeAssistantError):
        await light.async_turn_on(brightness=204, color_temp_kelvin=4000)
    assert light.brightness == 204
    assert light.color_temp_kelvin == before
    assert [(r[3], r[5]) for r in gateway.requests if r[1] == 6] == [(0x2A, 80)]


async def test_older_unsent_failure_does_not_override_newer_control(gateway, lights):
    coordinator, entities = lights
    light = entities[0]
    gateway.block = True
    gateway.arrived.clear()
    blocker = asyncio.create_task(coordinator.async_poll_round())
    await gateway.arrived.wait()
    older = asyncio.create_task(light.async_turn_on(brightness=102), eager_start=True)
    coordinator.transport.discard_controls()
    newer = asyncio.create_task(light.async_turn_on(brightness=204), eager_start=True)
    try:
        with pytest.raises(HomeAssistantError):
            await older
        assert light.brightness == 204
        assert not newer.done()
    finally:
        gateway.release.set()
        await asyncio.gather(blocker, newer)
    assert light.brightness == 204
    assert [r[5] for r in gateway.requests if r[1] == 6] == [80]


async def test_cancelled_brightness_does_not_replace_remembered_level(gateway, lights):
    coordinator, entities = lights
    light = entities[0]
    initial = light.brightness
    gateway.block = True
    gateway.arrived.clear()
    blocker = asyncio.create_task(coordinator.async_poll_round())
    await gateway.arrived.wait()
    older = asyncio.create_task(light.async_turn_on(brightness=204), eager_start=True)
    off = asyncio.create_task(light.async_turn_off(), eager_start=True)
    await older  # Superseded before send.
    gateway.release.set()
    await asyncio.gather(blocker, off)
    await light.async_turn_on()
    assert light.brightness == initial
    assert [r[5] for r in gateway.requests if r[1] == 6] == [0, 20]
