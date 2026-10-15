import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.coselig_modbus_hub.protocol import (
    CRCError,
    DeviceException,
    ProtocolError,
    frame,
    read_holding_registers,
    write_single_register,
)
from custom_components.coselig_modbus_hub.s402 import S402Event
from custom_components.coselig_modbus_hub.trace import ProtocolTraceBuffer
from custom_components.coselig_modbus_hub.transport import (
    GatewayError,
    OfflineError,
    Transport,
)
from tests.dimmer_packets import read, write


@pytest.fixture
async def transport(gateway):
    transport = Transport("127.0.0.1", gateway.port, timeout=0.04, capacity=2)
    yield transport
    await transport.close()


async def test_fragmentation_persistent_socket(gateway, transport):
    for request in (read(1, 4), read(2, 2), write(1, 0x082A, 80)):
        assert await transport.submit(request, poll=request[1] == 3)
    assert gateway.connections == 1
    assert transport.statistics.successful_requests == 3


async def test_priority(gateway, transport):
    gateway.block = True
    first = asyncio.create_task(transport.submit(read(1, 4), poll=True))
    await gateway.arrived.wait()
    polls = [
        asyncio.create_task(transport.submit(read(slave, 4), poll=True))
        for slave in (2, 3)
    ]
    control = asyncio.create_task(transport.submit(write(8, 0x082A, 80)))
    await asyncio.sleep(0)
    gateway.release.set()
    await asyncio.gather(first, *polls, control)
    assert [(r[0], r[1]) for r in gateway.requests] == [(1, 3), (8, 6), (2, 3), (3, 3)]


@pytest.mark.parametrize("active", [False, True])
async def test_latest_wins(gateway, transport, active):
    gateway.block = True
    first = asyncio.create_task(
        transport.submit(
            write(1, 0x082A, 20) if active else read(2, 4), poll=not active
        )
    )
    await gateway.arrived.wait()
    pending = [
        asyncio.create_task(transport.submit(write(1, 0x082A, v)))
        for v in ([30, 40, 80] if active else [20, 30, 40, 80])
    ]
    await asyncio.sleep(0)
    gateway.release.set()
    results = await asyncio.gather(first, *pending)
    assert [r[5] for r in gateway.requests if r[1] == 6] == (
        [20, 80] if active else [80]
    )
    assert all(value is None for value in results[1:-1])


async def test_capacity_coalesces_and_rejects(gateway, transport, caplog):
    gateway.block = True
    first = asyncio.create_task(transport.submit(read(1, 4), poll=True))
    await gateway.arrived.wait()
    pending = [
        asyncio.create_task(transport.submit(write(s, 0x082A, 20))) for s in (2, 3)
    ]
    await asyncio.sleep(0)
    replacement = asyncio.create_task(transport.submit(write(2, 0x082A, 80)))
    await asyncio.sleep(0)
    with pytest.raises(asyncio.QueueFull):
        await transport.submit(write(4, 0x082A, 30))
    assert "queue full" in caplog.text
    gateway.release.set()
    await asyncio.gather(first, *pending, replacement)
    assert transport.statistics.queue_peak == 2


@pytest.mark.parametrize(
    "values",
    [
        [0x0501, 0x0501, 0x0501],
        [0x0501, 0x0503, 0x0501],
        [0x0501, 0x0502, 0x0503],
    ],
)
async def test_action_controls_are_fifo_and_never_coalesce(gateway, values):
    transport = Transport("127.0.0.1", gateway.port, timeout=0.04, capacity=4)
    gateway.block = True
    try:
        first = asyncio.create_task(transport.submit(read(1, 4), poll=True))
        await gateway.arrived.wait()
        actions = [
            asyncio.create_task(
                transport.submit_action(write_single_register(1, 0x019B, value))
            )
            for value in values
        ]
        for _ in range(100):
            if transport.queue_depth == len(values):
                break
            await asyncio.sleep(0)
        assert transport.queue_depth == len(values)
        gateway.release.set()
        await asyncio.gather(first, *actions)
        assert [value for _slave, address, value in gateway.register_writes] == values
    finally:
        await transport.close()


async def test_action_controls_count_toward_capacity(gateway, transport):
    gateway.block = True
    first = asyncio.create_task(transport.submit(read(1, 4), poll=True))
    await gateway.arrived.wait()
    actions = [
        asyncio.create_task(
            transport.submit_action(write_single_register(1, 0x019B, value))
        )
        for value in (0x0501, 0x0502)
    ]
    await asyncio.sleep(0)
    assert transport.queue_depth == transport.capacity == 2
    with pytest.raises(asyncio.QueueFull):
        await transport.submit_action(write_single_register(1, 0x019B, 0x0503))
    gateway.release.set()
    await asyncio.gather(first, *actions)


async def test_action_controls_use_default_capacity_32(gateway):
    transport = Transport("127.0.0.1", gateway.port, timeout=0.04)
    gateway.block = True
    try:
        first = asyncio.create_task(transport.submit(read(1, 4), poll=True))
        await gateway.arrived.wait()
        actions = [
            asyncio.create_task(
                transport.submit_action(
                    write_single_register(8, 0x019B, 0x0500 | relay)
                )
            )
            for relay in range(1, 33)
        ]
        for _ in range(100):
            if transport.queue_depth == 32:
                break
            await asyncio.sleep(0)
        assert transport.capacity == transport.queue_depth == 32
        with pytest.raises(asyncio.QueueFull):
            await transport.submit_action(write_single_register(8, 0x019B, 0x0501))
        gateway.release.set()
        await asyncio.gather(first, *actions)
        assert [value for _slave, _address, value in gateway.register_writes] == [
            0x0500 | relay for relay in range(1, 33)
        ]
    finally:
        await transport.close()


async def test_action_controls_keep_control_priority_over_pending_polls(
    gateway, transport
):
    gateway.block = True
    first = asyncio.create_task(transport.submit(read(1, 4), poll=True))
    await gateway.arrived.wait()
    polls = [
        asyncio.create_task(transport.submit(read(slave, 4), poll=True))
        for slave in (2, 3)
    ]
    actions = [
        asyncio.create_task(
            transport.submit_action(write_single_register(8, 0x019B, value))
        )
        for value in (0x0501, 0x0502)
    ]
    await asyncio.sleep(0)
    gateway.release.set()
    await asyncio.gather(first, *polls, *actions)
    assert [(request[0], request[1]) for request in gateway.requests] == [
        (1, 3),
        (8, 6),
        (8, 6),
        (2, 3),
        (3, 3),
    ]
    assert [value for _slave, _address, value in gateway.register_writes] == [
        0x0501,
        0x0502,
    ]


@pytest.mark.parametrize(
    ("mode", "error", "sent_values"),
    [
        ("timeout", TimeoutError, [0x0501]),
        ("disconnect", GatewayError, []),
    ],
)
async def test_action_failure_discards_unsent_actions_without_replay(
    gateway, transport, mode, error, sent_values
):
    gateway.block = True
    gateway.modes[1] = mode
    first = asyncio.create_task(
        transport.submit_action(write_single_register(1, 0x019B, 0x0501))
    )
    await gateway.arrived.wait()
    pending = asyncio.create_task(
        transport.submit_action(write_single_register(1, 0x019B, 0x0502))
    )
    await asyncio.sleep(0)
    gateway.release.set()
    with pytest.raises(error):
        await first
    with pytest.raises(OfflineError):
        await pending
    assert [value for _slave, _address, value in gateway.register_writes] == sent_values
    gateway.modes.clear()


@pytest.mark.parametrize(
    "mode,error",
    [
        ("timeout", TimeoutError),
        ("crc", CRCError),
        ("slave", ProtocolError),
        ("function", ProtocolError),
        ("malformed", ProtocolError),
        ("echo", ProtocolError),
        ("disconnect", GatewayError),
        ("exception", DeviceException),
    ],
)
async def test_failure_reconnect_no_replay(gateway, transport, mode, error):
    gateway.modes[1] = mode
    with pytest.raises(error):
        await transport.submit(write(1, 0x082A, 80))
    # Simulator applies a write before sending corrupt/absent echo.
    assert len(gateway.requests) == 1
    gateway.modes.clear()
    assert await transport.submit(read(1, 4), poll=True)
    assert [r[1] for r in gateway.requests] == [6, 3]
    assert transport.statistics.failed_requests == 1
    assert transport.statistics.timeout_count == int(mode == "timeout")
    assert transport.statistics.crc_error_count == int(mode == "crc")
    assert gateway.connections == (1 if mode == "exception" else 2)


async def test_failure_discards_pending(gateway, transport):
    gateway.block = True
    gateway.modes[1] = "timeout"
    first = asyncio.create_task(transport.submit(write(1, 0x082A, 20)))
    await gateway.arrived.wait()
    pending = asyncio.create_task(transport.submit(write(2, 0x082A, 80)))
    await asyncio.sleep(0)
    gateway.release.set()
    with pytest.raises(TimeoutError):
        await first
    with pytest.raises(OfflineError):
        await pending
    assert len(gateway.requests) == 1


async def test_close_cancels_pending(gateway, transport):
    gateway.block = True
    first = asyncio.create_task(transport.submit(read(1, 4), poll=True))
    await gateway.arrived.wait()
    pending = asyncio.create_task(transport.submit(write(2, 0x082A, 80)))
    await asyncio.sleep(0)
    await transport.close()
    results = await asyncio.gather(first, pending, return_exceptions=True)
    assert all(isinstance(r, asyncio.CancelledError) for r in results)
    with pytest.raises(OfflineError):
        await transport.submit(read(1, 4), poll=True)
    with pytest.raises(OfflineError):
        await transport.connect()


async def test_cancelled_pending_never_sent(gateway, transport):
    gateway.block = True
    first = asyncio.create_task(transport.submit(read(1, 4), poll=True))
    await gateway.arrived.wait()
    pending = asyncio.create_task(transport.submit(write(2, 0x082A, 80)))
    await asyncio.sleep(0)
    pending.cancel()
    await asyncio.gather(pending, return_exceptions=True)
    gateway.release.set()
    await first
    async with transport.exclusive():
        assert len(gateway.requests) == 1


@pytest.mark.parametrize("args", [("", 502), ("x", 0), ("x", 502, 0), ("x", 502, 1, 0)])
def test_invalid_configuration(args):
    with pytest.raises(ValueError):
        Transport(*args)


async def test_connect_error(socket_enabled):
    transport = Transport("127.0.0.1", 1)
    with pytest.raises(GatewayError):
        await transport.connect()
    await transport.close()


def _s402_packet(device_id=7, di_bit=1):
    return frame(bytes([1, 6, 1, 0xA0, device_id, di_bit]))


async def _push(gateway, data):
    await gateway.connected_event.wait()
    writer = next(iter(gateway.writers))
    writer.write(data)
    await writer.drain()
    return writer


async def test_idle_s402_event_is_received_without_a_request(gateway):
    events = []
    transport = Transport("127.0.0.1", gateway.port, on_s402_event=events.append)
    try:
        await transport.connect()
        arrived = asyncio.Event()
        transport.on_s402_event = lambda event: (events.append(event), arrived.set())
        await _push(gateway, _s402_packet(7, 8))
        await asyncio.wait_for(arrived.wait(), 1)
        assert events == [S402Event(7, 4)]
        assert transport.statistics.successful_requests == 0
        assert transport.statistics.failed_requests == 0
        assert transport.trace is None
    finally:
        await transport.close()


async def test_s402_event_between_tx_and_response_does_not_steal_response(
    gateway, transport
):
    gateway.block = True
    received = asyncio.Event()
    events = []
    transport.on_s402_event = lambda event: (events.append(event), received.set())
    request = asyncio.create_task(transport.submit(read(51, 2), poll=True))
    await gateway.arrived.wait()
    await _push(gateway, _s402_packet(7, 1))
    await asyncio.wait_for(received.wait(), 1)
    gateway.release.set()
    response = await request
    assert response[0:2] == bytes([51, 3])
    assert events == [S402Event(7, 1)]
    assert transport.statistics.successful_requests == 1


async def test_multiple_s402_events_before_pending_response_keep_order(
    gateway, transport
):
    gateway.block = True
    events = []
    received = asyncio.Event()

    def on_event(event):
        events.append(event)
        if len(events) == 4:
            received.set()

    transport.on_s402_event = on_event
    request = asyncio.create_task(transport.submit(read(51, 2), poll=True))
    await gateway.arrived.wait()
    await _push(gateway, b"".join(_s402_packet(7, bit) for bit in (1, 2, 4, 8)))
    await asyncio.wait_for(received.wait(), 1)
    gateway.release.set()
    await request
    assert events == [S402Event(7, di) for di in range(1, 5)]


async def test_s402_event_after_response_is_still_received(gateway, transport):
    events = []
    arrived = asyncio.Event()
    transport.on_s402_event = lambda event: (events.append(event), arrived.set())
    assert await transport.submit(read(51, 2), poll=True)
    await _push(gateway, _s402_packet(7, 2))
    await asyncio.wait_for(arrived.wait(), 1)
    assert events == [S402Event(7, 2)]


@pytest.mark.parametrize("invalid", ["crc", "di"])
async def test_invalid_s402_candidate_is_consumed_without_request_counters(
    gateway, transport, invalid
):
    gateway.block = True
    packet = bytearray(_s402_packet(7, 1))
    if invalid == "crc":
        packet[-1] ^= 0xFF
    else:
        packet[5] = 0x10
        packet[-2:] = frame(bytes(packet[:-2]))[-2:]
    events = []
    transport.on_s402_event = events.append
    request = asyncio.create_task(transport.submit(read(51, 2), poll=True))
    await gateway.arrived.wait()
    await _push(gateway, bytes(packet))
    await asyncio.sleep(0)
    gateway.release.set()
    assert await request
    assert not events
    assert transport.statistics.successful_requests == 1
    assert transport.statistics.failed_requests == 0
    assert transport.statistics.crc_error_count == 0


async def test_s402_frame_can_be_fragmented_across_tcp_chunks(gateway, transport):
    events = []
    arrived = asyncio.Event()
    transport.on_s402_event = lambda event: (events.append(event), arrived.set())
    await transport.connect()
    writer = await _push(gateway, _s402_packet(255, 8)[:3])
    await asyncio.sleep(0.01)
    writer.write(_s402_packet(255, 8)[3:])
    await writer.drain()
    await asyncio.wait_for(arrived.wait(), 1)
    assert events == [S402Event(255, 4)]


async def test_event_and_normal_response_coalesced_in_one_tcp_write(socket_enabled):
    events = []
    connected = asyncio.Event()

    async def handler(reader, writer):
        connected.set()
        request = await reader.readexactly(8)
        writer.write(_s402_packet(12, 8) + frame(bytes([request[0], 3, 2, 0, 42])))
        await writer.drain()
        await reader.read()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    transport = Transport("127.0.0.1", port, on_s402_event=events.append)
    try:
        request = asyncio.create_task(
            transport.submit(read_holding_registers(51, 0x082A, 1), poll=True)
        )
        await connected.wait()
        response = await request
        assert response[0:2] == bytes([51, 3])
        assert events == [S402Event(12, 4)]
    finally:
        await transport.close()
        server.close()
        await server.wait_closed()


async def test_s402_events_do_not_extend_original_request_timeout(gateway):
    transport = Transport("127.0.0.1", gateway.port, timeout=0.08)
    gateway.block = True
    event_received = asyncio.Event()
    transport.on_s402_event = lambda _event: event_received.set()
    try:
        started = asyncio.get_running_loop().time()
        request = asyncio.create_task(transport.submit(read(51, 2), poll=True))
        await gateway.arrived.wait()
        await asyncio.sleep(0.05)
        await _push(gateway, _s402_packet())
        await asyncio.wait_for(event_received.wait(), 1)
        with pytest.raises(TimeoutError):
            await request
        elapsed = asyncio.get_running_loop().time() - started
        assert 0.07 <= elapsed < 0.12
        assert transport.statistics.timeout_count == 1
    finally:
        await transport.close()


async def test_idle_eof_notifies_loss_once_and_disconnect_is_intentional(gateway):
    losses = []
    transport = Transport(
        "127.0.0.1", gateway.port, on_connection_lost=lambda: losses.append(True)
    )
    await transport.connect()
    await gateway.connected_event.wait()
    writer = next(iter(gateway.writers))
    writer.close()
    await writer.wait_closed()
    for _ in range(20):
        if losses:
            break
        await asyncio.sleep(0)
    assert losses == [True]
    await transport.disconnect()
    assert losses == [True]
    await transport.close()


async def test_reconnect_creates_one_reader_and_disconnect_cleans_it(gateway):
    losses = []
    transport = Transport(
        "127.0.0.1", gateway.port, on_connection_lost=lambda: losses.append(True)
    )
    await transport.connect()
    first = transport._reader_task
    assert first is not None and not first.done()
    await transport.disconnect()
    assert first.done()
    assert not losses
    await transport.connect()
    second = transport._reader_task
    assert second is not None and second is not first and not second.done()
    assert (
        sum(
            task.get_name() == "coselig-rx" and not task.done()
            for task in asyncio.all_tasks()
        )
        == 1
    )
    await transport.close()
    assert second.done()


async def test_close_cancels_reader_worker_and_pending_future(gateway):
    gateway.block = True
    transport = Transport("127.0.0.1", gateway.port, timeout=1)
    request = asyncio.create_task(transport.submit(read(51, 2), poll=True))
    await gateway.arrived.wait()
    reader_task = transport._reader_task
    worker_task = transport._worker
    pending_future = transport._pending_response
    await transport.close()
    result = await asyncio.gather(request, return_exceptions=True)
    assert isinstance(result[0], asyncio.CancelledError)
    assert reader_task is not None and reader_task.done()
    assert worker_task is not None and worker_task.done()
    assert pending_future is not None and pending_future.cancelled()
    assert transport._reader is None and transport._writer is None


@pytest.mark.parametrize("close_error", [OSError("closed"), TimeoutError()])
async def test_disconnect_completes_pending_and_tolerates_writer_close_error(
    close_error,
):
    transport = Transport("gateway", 502)
    writer = Mock()
    writer.close = Mock()
    writer.wait_closed = AsyncMock(side_effect=close_error)
    transport._writer = writer
    pending = asyncio.get_running_loop().create_future()
    transport._pending_response = pending

    await transport.disconnect()

    with pytest.raises(GatewayError, match="disconnected"):
        await pending
    writer.close.assert_called_once()
    assert transport._writer is None
    assert not transport._disconnecting


async def test_idle_normal_frames_and_frames_after_pending_completion_are_consumed(
    gateway,
):
    trace = ProtocolTraceBuffer()
    transport = Transport("127.0.0.1", gateway.port, trace=trace)
    try:
        await transport.connect()
        response = frame(bytes([51, 3, 2, 0, 1]))
        await _push(gateway, response)
        await asyncio.sleep(0.01)
        completed = asyncio.get_running_loop().create_future()
        completed.set_result(b"already handled")
        transport._pending_response = completed
        await _push(gateway, response)
        await asyncio.sleep(0.01)
        assert trace.snapshot() == []
        assert transport.statistics.successful_requests == 0
        assert completed.result() == b"already handled"
    finally:
        transport._pending_response = None
        await transport.close()


@pytest.mark.parametrize("callback_mode", ["none", "raises"])
async def test_s402_callback_is_optional_and_listener_errors_are_isolated(
    gateway, callback_mode
):
    arrived = asyncio.Event()
    transport = Transport("127.0.0.1", gateway.port)
    if callback_mode == "raises":

        def fail(_event):
            arrived.set()
            raise RuntimeError("listener error")

        transport.on_s402_event = fail
    try:
        await transport.connect()
        await _push(gateway, _s402_packet(4, 1))
        if callback_mode == "raises":
            await asyncio.wait_for(arrived.wait(), 1)
        else:
            await asyncio.sleep(0.01)
        assert transport.connected
        assert transport.statistics.successful_requests == 0
        assert transport.statistics.failed_requests == 0
    finally:
        await transport.close()


async def test_protocol_framing_error_is_delivered_to_pending_request(gateway):
    transport = Transport("127.0.0.1", gateway.port)
    try:
        await transport.connect()
        pending = asyncio.get_running_loop().create_future()
        transport._pending_response = pending
        await _push(gateway, b"\x01\x07")
        with pytest.raises(ProtocolError, match="Unsupported response"):
            await pending
        assert transport._reader_task is None
        assert transport.connected
    finally:
        await transport.close()


async def test_idle_protocol_framing_error_uses_connection_loss_callback(gateway):
    lost = asyncio.Event()
    transport = Transport("127.0.0.1", gateway.port, on_connection_lost=lost.set)
    await transport.connect()
    await _push(gateway, b"\x01\x07")
    await asyncio.wait_for(lost.wait(), 1)
    assert not transport.connected
    await transport.close()


async def test_stale_reader_error_does_not_mark_active_connection_lost():
    transport = Transport("gateway", 502)
    losses = []
    transport.on_connection_lost = lambda: losses.append(True)

    class SwitchingReader:
        async def readexactly(self, _count):
            transport._reader = None
            raise OSError("stale reader")

    reader = SwitchingReader()
    transport._reader = reader
    task = asyncio.create_task(transport._read_loop(reader))
    transport._reader_task = task
    await task
    assert not losses
    assert transport._reader_task is None

    other = asyncio.create_task(asyncio.sleep(60))
    transport._reader_task = other
    await transport._read_loop(object())
    assert transport._reader_task is other
    other.cancel()
    await asyncio.gather(other, return_exceptions=True)


@pytest.mark.parametrize("disconnecting,reported", [(True, False), (False, True)])
async def test_connection_loss_report_is_suppressed_during_teardown_or_duplicate(
    disconnecting, reported
):
    losses = []
    transport = Transport(
        "gateway", 502, on_connection_lost=lambda: losses.append(True)
    )
    transport._disconnecting = disconnecting
    transport._loss_reported = reported
    transport._mark_connection_lost(OSError("gone"))
    assert not losses


async def test_connection_loss_callback_errors_do_not_escape(caplog):
    def fail():
        raise RuntimeError("loss listener error")

    transport = Transport("gateway", 502, on_connection_lost=fail)
    transport._mark_connection_lost(OSError("gone"))
    assert "Gateway loss listener failed" in caplog.text


async def test_completed_controls_are_cleared_and_latest_wins_replaces_done_item():
    transport = Transport("gateway", 502)
    hold = asyncio.Event()
    transport._worker = asyncio.create_task(hold.wait())
    key = (1, "082a", "brightness")
    first = transport._enqueue(write(1, 0x082A, 20), False, key, "control")
    first.set_result(None)
    replacement = transport._enqueue(write(1, 0x082A, 80), False, key, "control")
    assert transport._controls[key].future is replacement
    replacement.set_result(None)
    transport.discard_controls()
    assert not transport._controls
    await transport.close()


async def test_exchange_checks_connection_and_cancels_unawaited_response_future():
    transport = Transport("gateway", 502)
    transport.connect = AsyncMock()
    with pytest.raises(GatewayError, match="not connected"):
        await transport._exchange(read(1, 2), "poll")

    class BrokenWriter:
        def write(self, _request):
            raise OSError("write failed")

        async def drain(self):
            return None

    transport._reader = object()
    transport._writer = BrokenWriter()
    with pytest.raises(GatewayError, match="connection lost"):
        await transport._exchange(read(1, 2), "poll")
    assert transport._pending_response is None


async def test_cancelled_active_request_does_not_rewrite_its_completed_future(
    gateway, transport
):
    gateway.block = True
    request = asyncio.create_task(transport.submit(read(1, 4), poll=True))
    await gateway.arrived.wait()
    request.cancel()
    await asyncio.gather(request, return_exceptions=True)
    gateway.modes[1] = "disconnect"
    gateway.release.set()
    for _ in range(100):
        if transport.statistics.failed_requests:
            break
        await asyncio.sleep(0.001)
    assert transport.statistics.failed_requests == 1
    await transport.close()
