import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.coselig_modbus_hub.coordinator import Coordinator
from custom_components.coselig_modbus_hub.hub import Hub
from custom_components.coselig_modbus_hub.models import Device, Model
from custom_components.coselig_modbus_hub.transport import (
    GatewayError,
    OfflineError,
    Transport,
)
from tests.dimmer_packets import write


@pytest.fixture
async def coordinator(hass, gateway):
    transport = Transport("127.0.0.1", gateway.port, timeout=0.025)
    coordinator = Coordinator(
        hass, transport, (Device(1, Model.P404), Device(2, Model.P210)), 2
    )
    yield coordinator
    await coordinator.stop()
    await transport.close()


async def test_round_shapes_and_slave_isolation(gateway, coordinator, caplog):
    await coordinator.async_poll_round()
    assert [int.from_bytes(r[4:6], "big") for r in gateway.requests] == [4, 2]
    gateway.modes[1] = "timeout"
    for index in range(1, 4):
        await coordinator.async_poll_round()
        assert coordinator.states[1].consecutive_failures == index
        assert coordinator.states[1].available == (index < 3)
        assert coordinator.states[2].available
        assert coordinator.gateway_available
    await coordinator.async_poll_round()
    assert caplog.text.count("Slave #1 unavailable") == 1
    gateway.modes.clear()
    await coordinator.async_poll_round()
    assert coordinator.states[1].available
    assert coordinator.states[1].consecutive_failures == 0


@pytest.mark.parametrize("mode", ["single", "cct"])
async def test_p210_poll_count_stays_two_for_all_output_modes(hass, gateway, mode):
    device = Device(62, Model.P210, mode)
    transport = Transport("127.0.0.1", gateway.port, timeout=0.025)
    coordinator = Coordinator(hass, transport, (device,), 2)
    try:
        await coordinator.async_poll_round()
        assert len(gateway.requests) == 1
        assert gateway.requests[0][0] == 62
        assert gateway.requests[0][1] == 3
        assert int.from_bytes(gateway.requests[0][4:6], "big") == 2
        assert coordinator.states[62].available
    finally:
        await coordinator.stop()
        await transport.close()


async def test_gateway_loss_everyone_unavailable(gateway, coordinator, caplog):
    await coordinator.async_poll_round()
    gateway.modes[1] = "disconnect"
    await coordinator.async_poll_round()
    assert not coordinator.gateway_available
    assert not any(s.available for s in coordinator.states.values())
    coordinator.gateway_lost()
    assert caplog.text.count("Gateway lost") == 1
    with pytest.raises(OfflineError):
        await coordinator.async_command(
            1, write(1, 0x082A, 80), (1, "ch1", "brightness")
        )


async def test_all_failed_round_rebuilds(gateway, coordinator):
    await coordinator.async_poll_round()
    gateway.modes.update({1: "timeout", 2: "timeout"})
    await coordinator.async_poll_round()
    assert not coordinator.transport.connected
    assert coordinator.gateway_available
    assert coordinator.states[1].available
    gateway.modes.clear()
    await coordinator.async_poll_round()
    assert all(s.available for s in coordinator.states.values())


async def test_invalid_register_values(gateway, coordinator):
    gateway.registers[1] = [101, 20, 30, 40]
    await coordinator.async_poll_round()
    assert coordinator.states[1].consecutive_failures == 1
    assert coordinator.states[2].available


async def test_no_extra_read_write_failure(gateway, coordinator):
    await coordinator.async_poll_round()
    gateway.requests.clear()
    await coordinator.async_command(1, write(1, 0x082A, 80), (1, "ch1", "brightness"))
    assert [r[1] for r in gateway.requests] == [6]
    gateway.modes[1] = "timeout"
    with pytest.raises(TimeoutError):
        await coordinator.async_command(
            1, write(1, 0x082A, 65), (1, "ch1", "brightness")
        )
    assert [r[1] for r in gateway.requests] == [6, 6]
    assert coordinator.states[1].available


async def test_write_tcp_failure(gateway, coordinator):
    await coordinator.async_poll_round()
    gateway.modes[1] = "disconnect"
    with pytest.raises(GatewayError):
        await coordinator.async_command(
            1, write(1, 0x082A, 80), (1, "ch1", "brightness")
        )
    assert not coordinator.gateway_available


def test_backoff_reset(hass):
    coordinator = Coordinator(hass, Transport("test", 502), (), 2)
    assert [coordinator.next_backoff() for _ in range(8)] == [
        1,
        2,
        4,
        8,
        15,
        30,
        30,
        30,
    ]


async def test_loop_reconnect_reset_and_immediate_poll(coordinator):
    coordinator.gateway_lost()
    rounds = 0
    delays = []

    async def poll(*, background=False):
        nonlocal rounds
        rounds += 1
        assert coordinator.gateway_available
        assert coordinator._backoff_index == 0
        coordinator._closed = True

    async def sleep(delay):
        delays.append(delay)

    with (
        patch.object(
            coordinator.transport,
            "connect",
            new=AsyncMock(side_effect=[GatewayError(), None]),
        ),
        patch.object(coordinator, "async_poll_round", new=poll),
        patch(
            "custom_components.coselig_modbus_hub.coordinator.asyncio.sleep", new=sleep
        ),
    ):
        await coordinator._loop()
    assert delays[:2] == [1, 2]
    assert rounds == 1
    coordinator._closed = False
    coordinator.gateway_lost()
    assert coordinator.next_backoff() == 1


async def test_long_round_no_backlog(coordinator):
    # Exact scheduling semantics with a virtual clock: 2s interval, 3s round.
    now = 100.0
    rounds = 0
    delays = []
    real_loop = asyncio.get_running_loop()

    async def poll(*, background=False):
        nonlocal now, rounds
        rounds += 1
        now += 3
        if rounds == 3:
            coordinator._closed = True

    async def sleep(delay):
        delays.append(delay)

    with (
        patch.object(real_loop, "time", side_effect=lambda: now),
        patch.object(coordinator, "async_poll_round", new=poll),
        patch(
            "custom_components.coselig_modbus_hub.coordinator.asyncio.sleep", new=sleep
        ),
    ):
        await coordinator._loop()
    assert rounds == 3
    assert delays == [0, 0, 0]
    assert coordinator.transport.queue_depth == 0


async def test_loop_gateway_failure_no_poll_sleep(coordinator):
    async def poll(*, background=False):
        coordinator.gateway_lost()
        coordinator._closed = True

    with (
        patch.object(coordinator, "async_poll_round", new=poll),
        patch(
            "custom_components.coselig_modbus_hub.coordinator.asyncio.sleep",
            new=AsyncMock(),
        ) as sleep,
    ):
        await coordinator._loop()
    sleep.assert_not_awaited()


async def test_scan_pauses_round_and_controls(gateway, coordinator):
    await coordinator.async_poll_round()
    gateway.requests.clear()
    gateway.block = True
    gateway.arrived.clear()
    hub = Hub(coordinator.transport, coordinator)
    scan_task = asyncio.create_task(hub.scan(1, 2))
    await gateway.arrived.wait()
    assert coordinator.scanning
    poll = asyncio.create_task(coordinator.async_poll_round())
    with pytest.raises(OfflineError):
        await coordinator.async_command(
            1, write(1, 0x082A, 80), (1, "ch1", "brightness")
        )
    await asyncio.sleep(0)
    assert len(gateway.requests) == 1
    gateway.release.set()
    assert [r.slave for r in await scan_task] == [1, 2]
    await poll
    assert [int.from_bytes(r[4:6], "big") for r in gateway.requests] == [
        1,
        2,
        1,
        2,
        4,
        2,
    ]
    assert not coordinator.scanning


async def test_scan_cancel_resumes(gateway, coordinator):
    gateway.block = True
    task = asyncio.create_task(Hub(coordinator.transport, coordinator).scan(1, 2))
    await gateway.arrived.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert not coordinator.scanning
    gateway.release.set()
    await coordinator.async_poll_round()
    assert coordinator.states[2].available


async def test_start_stop_loop(coordinator):
    coordinator.start()
    await asyncio.sleep(0)
    await coordinator.stop()
    assert coordinator._task is None


def test_invalid_interval(hass):
    with pytest.raises(ValueError):
        Coordinator(hass, Transport("test", 502), (), 0.4)


async def test_scan_tcp_failure_marks_gateway_offline(gateway, coordinator):
    await coordinator.async_poll_round()
    gateway.modes[1] = "disconnect"
    with pytest.raises(GatewayError):
        await Hub(coordinator.transport, coordinator).scan(1, 2)
    assert not coordinator.gateway_available
    assert not any(state.available for state in coordinator.states.values())
    assert not coordinator.scanning


async def test_native_coordinator_manual_refresh(coordinator, gateway):
    await coordinator.async_refresh()
    assert coordinator.data is coordinator.states
    assert coordinator.last_update_success
    assert all(state.available for state in coordinator.states.values())
    assert [r[0] for r in gateway.requests] == [1, 2]
