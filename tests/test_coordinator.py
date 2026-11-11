import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest

from custom_components.coselig_modbus_hub.coordinator import Coordinator
from custom_components.coselig_modbus_hub.hub import Hub
from custom_components.coselig_modbus_hub.models import Device, Model
from custom_components.coselig_modbus_hub.protocol import write_single_register
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


async def test_action_command_requires_confirmed_available_slave(gateway, coordinator):
    await coordinator.async_poll_round()
    gateway.requests.clear()
    request = write_single_register(1, 0x019B, 0x0501)
    await coordinator.async_action_command(1, request)
    assert gateway.requests == [request]

    coordinator.scanning = True
    with pytest.raises(OfflineError):
        await coordinator.async_action_command(1, request)
    coordinator.scanning = False
    coordinator.states[1].available = False
    with pytest.raises(OfflineError):
        await coordinator.async_action_command(1, request)
    coordinator.states[1].available = True
    coordinator.gateway_available = False
    with pytest.raises(OfflineError):
        await coordinator.async_action_command(1, request)
    assert gateway.requests == [request]


@pytest.mark.parametrize("runtime_paused,poll_interval", [(False, 0), (True, 2)])
async def test_action_control_works_when_background_poll_is_disabled_or_paused(
    hass, gateway, runtime_paused, poll_interval
):
    transport = Transport("127.0.0.1", gateway.port, timeout=0.025)
    coordinator = Coordinator(hass, transport, (Device(1, Model.R410),), poll_interval)
    try:
        await coordinator.async_poll_round()
        coordinator.set_runtime_poll_paused(runtime_paused)
        request = write_single_register(1, 0x019B, 0x0503)
        await coordinator.async_action_command(1, request)
        assert gateway.requests[-1] == request
        assert coordinator.states[1].available
    finally:
        await coordinator.stop()
        await transport.close()


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
        assert coordinator._backoff_index == 2
        coordinator._closed = True

    async def sleep(delay):
        delays.append(delay)

    async def connect():
        if len(delays) == 1:
            raise GatewayError
        coordinator.gateway_restored()

    with (
        patch.object(
            coordinator.transport,
            "connect",
            new=AsyncMock(side_effect=connect),
        ),
        patch.object(coordinator, "async_poll_round", new=poll),
        patch(
            "custom_components.coselig_modbus_hub.coordinator.asyncio.sleep", new=sleep
        ),
    ):
        await coordinator._loop()
    assert delays[:2] == [1, 2]
    assert rounds == 1
    assert coordinator.next_backoff() == 4


async def test_reconnect_notifies_listeners_for_empty_s402_only_gateway(hass):
    transport = Transport("test", 502)
    coordinator = Coordinator(hass, transport, (), 0)
    coordinator.gateway_lost()
    updates = Mock()

    async def connect_once():
        coordinator.gateway_restored()
        coordinator._closed = True

    with (
        patch.object(transport, "connect", new=AsyncMock(side_effect=connect_once)),
        patch.object(coordinator, "async_set_updated_data", new=updates),
        patch(
            "custom_components.coselig_modbus_hub.coordinator.asyncio.sleep",
            new=AsyncMock(),
        ),
    ):
        await coordinator._loop()
    assert coordinator.gateway_available
    updates.assert_called_once_with(coordinator.states)


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
        4,
        1,
        4,
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


@pytest.mark.parametrize("mode", ["exception", "invalid_value"])
async def test_all_slave_errors_keep_healthy_tcp(gateway, coordinator, mode):
    await coordinator.async_poll_round()
    for slave, count in ((1, 4), (2, 2)):
        if mode == "exception":
            gateway.modes[slave] = mode
        else:
            gateway.registers[slave] = [101] * count
    for failures in range(1, 4):
        await coordinator.async_poll_round()
        assert coordinator.gateway_available and coordinator.transport.connected
        assert all(
            state.consecutive_failures == failures and state.available == (failures < 3)
            for state in coordinator.states.values()
        )
    assert gateway.connections == 1


@pytest.mark.parametrize("pause_mode", ["disabled", "paused"])
@pytest.mark.parametrize("failure", ["timeout", "eof"])
async def test_idle_scheduler_recovers_tcp_without_polling(
    gateway, coordinator, monkeypatch, pause_mode, failure
):
    await coordinator.async_poll_round()
    transport = coordinator.transport
    reader, worker = transport._reader_task, transport._worker
    if pause_mode == "disabled":
        coordinator.set_poll_interval(0)
    else:
        coordinator.set_runtime_poll_paused(True)
    monkeypatch.setattr(coordinator, "next_backoff", lambda: 0)
    coordinator.start()
    await asyncio.sleep(0)
    gateway.requests.clear()
    request = write(1, 0x082A, 80)
    key = (1, "ch1", "brightness")
    if failure == "timeout":
        gateway.modes[1] = "timeout"
        with pytest.raises(TimeoutError):
            await coordinator.async_command(1, request, key)
        gateway.modes.clear()
        expected = [request]
    else:
        writer = next(iter(gateway.writers))
        writer.close()
        await writer.wait_closed()
        expected = []
    async with asyncio.timeout(2):
        while gateway.connections != 2 or not coordinator.gateway_available:
            await asyncio.sleep(0.001)
    assert gateway.requests == expected
    assert len(gateway.writers) == 1
    assert reader.done()
    assert transport._reader_task is not reader
    assert transport._worker is worker
    await coordinator.async_command(1, request, key)
    assert gateway.requests == [*expected, request]


async def test_poll_disconnect_rotates_and_quarantines_offending_slave(
    gateway, coordinator, monkeypatch
):
    await coordinator.async_poll_round()
    gateway.requests.clear()
    gateway.modes[1] = "disconnect"
    coordinator.set_poll_interval(0.5)
    delays = []
    next_backoff = coordinator.next_backoff

    def record_backoff():
        delays.append(next_backoff())
        return 0

    monkeypatch.setattr(coordinator, "next_backoff", record_backoff)
    coordinator.start()
    async with asyncio.timeout(4):
        while not (
            coordinator.states[1].next_probe_at and coordinator.states[2].available
        ):
            await asyncio.sleep(0.001)
    assert delays == [1, 1, 1]
    assert [request[0] for request in gateway.requests[:6]] == [1, 2, 1, 2, 1, 2]
    assert not coordinator.states[1].available
    assert coordinator.gateway_available
    assert gateway.connections == 4
    assert len(gateway.writers) == 1


@pytest.mark.parametrize(
    ("mode", "reconnects"), [("p210_signature", 3), ("p210_exception", 0)]
)
async def test_misconfigured_slave_isolated_from_other_18(
    hass, gateway, mode, reconnects
):
    healthy = tuple(range(10, 28))
    devices = (
        Device(62, Model.P404),
        *(Device(slave, Model.P404) for slave in healthy),
    )
    hub = Hub.create(hass, "127.0.0.1", gateway.port, devices, 2)
    coordinator, transport = hub.coordinator, hub.transport
    try:
        await coordinator.async_poll_round(background=True)
        worker = transport._worker
        gateway.modes[62] = mode
        for _ in range(3):
            await coordinator.async_poll_round(background=True)
        assert transport.statistics.reconnect_count == reconnects
        assert transport.statistics.crc_error_count == reconnects
        state = coordinator.states[62]
        assert state.consecutive_failures == 3
        assert not state.available
        assert all(coordinator.states[slave].available for slave in healthy)
        assert coordinator.devices[0].model == Model.P404
        for delay in (30, 60, 120, 240, 300, 300):
            assert (
                state.next_probe_at - asyncio.get_running_loop().time()
                == pytest.approx(delay, abs=1)
            )
            if delay != 30:
                assert state.consecutive_failures >= 4
            before = len(gateway.requests)
            await coordinator.async_poll_round(background=True)
            assert [r[0] for r in gateway.requests[before:]] == list(healthy)
            if state.consecutive_failures == 8:
                break
            state.next_probe_at = asyncio.get_running_loop().time() - 1
            await coordinator.async_poll_round(background=True)

        gateway.modes.clear()
        state.next_probe_at = asyncio.get_running_loop().time() - 1
        await coordinator.async_poll_round(background=True)
        assert state.available and state.consecutive_failures == 0
        assert state.next_probe_at == 0
        request = write(10, 0x082A, 72)
        await coordinator.async_command(10, request, (10, "ch1", "brightness"))
        assert gateway.requests.count(request) == 1
        assert transport._worker is worker
        assert sum(t.get_name() == "coselig-rx" for t in asyncio.all_tasks()) == 1
        assert len(gateway.writers) == 1
    finally:
        await hub.stop()
    assert transport._worker is None and transport._reader_task is None


async def test_soft_reset_escalates_to_offline_if_tcp_cannot_reconnect(
    coordinator, gateway, monkeypatch
):
    await coordinator.async_poll_round()
    coordinator.set_poll_interval(0)
    coordinator.start()
    request = write(1, 0x082A, 72)
    gateway.modes[1] = "timeout"

    new_connect = AsyncMock(side_effect=OSError("refused"))
    monkeypatch.setattr(asyncio, "open_connection", new_connect)
    with pytest.raises(TimeoutError):
        await coordinator.async_command(1, request, (1, "ch1", "brightness"))
    async with asyncio.timeout(2):
        while coordinator.gateway_available:
            await asyncio.sleep(0.001)
    new_connect.assert_awaited_once()
    assert coordinator.gateway_failed
    assert not any(state.available for state in coordinator.states.values())
    assert all(state.consecutive_failures == 0 for state in coordinator.states.values())
    assert gateway.requests.count(request) == 1
