import asyncio
from datetime import UTC, datetime
from unittest.mock import Mock, patch

import pytest

from custom_components.coselig_modbus_hub.protocol import (
    CRCError,
    DeviceException,
    ProtocolError,
    frame,
)
from custom_components.coselig_modbus_hub.scanner import scan
from custom_components.coselig_modbus_hub.trace import ProtocolTraceBuffer
from custom_components.coselig_modbus_hub.transport import GatewayError, Transport
from tests.dimmer_packets import read, write


async def test_tx_rx_ok_full_hex_latency(gateway):
    trace = ProtocolTraceBuffer()
    transport = Transport("127.0.0.1", gateway.port, trace=trace)
    try:
        request = read(1, 4)
        response = await transport.submit(request, poll=True)
        entries = trace.snapshot()
        assert [entry["direction"] for entry in entries] == ["TX", "RX", "OK"]
        assert entries[0]["raw_hex"] == request.hex(" ").upper()
        assert entries[1]["raw_hex"] == response.hex(" ").upper()
        assert all(entry["timestamp"].endswith("+00:00") for entry in entries)
        assert all(entry["source"] == "poll" for entry in entries)
        assert entries[2]["latency_ms"] >= 0
        await transport.submit(write(1, 0x082A, 80))
        assert trace.snapshot()[-1]["source"] == "control"
        await scan(transport, 1, 1)
        assert trace.snapshot()[-1]["source"] == "scan"
    finally:
        await transport.close()


@pytest.mark.parametrize(
    "mode,error,direction",
    [
        ("crc", CRCError, "CRC ERROR"),
        ("timeout", TimeoutError, "TIMEOUT"),
        ("disconnect", GatewayError, "ERROR"),
        ("echo", ProtocolError, "ERROR"),
        ("exception", DeviceException, "ERROR"),
        ("malformed", ProtocolError, "ERROR"),
    ],
)
async def test_failure_trace_is_observation(gateway, mode, error, direction):
    trace = ProtocolTraceBuffer()
    transport = Transport("127.0.0.1", gateway.port, timeout=0.02, trace=trace)
    gateway.modes[1] = mode
    try:
        with pytest.raises(error):
            await transport.submit(write(1, 0x082A, 80))
        entries = trace.snapshot()
        assert entries[0]["direction"] == "TX"
        assert entries[-1]["direction"] == direction
        assert entries[-1]["latency_ms"] >= 0
        if mode in ("crc", "echo", "exception"):
            assert entries[1]["direction"] == "RX"
        assert len(gateway.requests) == 1
        assert transport.statistics.failed_requests == 1
    finally:
        await transport.close()


async def test_trace_enabled_disabled_and_broken_identical_transactions(gateway):
    request = write(1, 0x082A, 80)
    for trace in (
        None,
        ProtocolTraceBuffer(),
        Mock(record=Mock(side_effect=RuntimeError("observer failed"))),
    ):
        transport = Transport("127.0.0.1", gateway.port, trace=trace)
        try:
            assert await transport.submit(request) == request
            assert transport.statistics.successful_requests == 1
            assert transport.statistics.failed_requests == 0
        finally:
            await transport.close()
    assert gateway.requests == [request] * 3


async def test_s402_event_has_standalone_trace_without_request_statistics(gateway):
    trace = ProtocolTraceBuffer()
    events = asyncio.Event()
    transport = Transport(
        "127.0.0.1",
        gateway.port,
        trace=trace,
        on_s402_event=lambda _event: events.set(),
    )
    try:
        await transport.connect()
        await gateway.connected_event.wait()
        writer = next(iter(gateway.writers))
        packet = frame(bytes([1, 6, 1, 0xA0, 7, 8]))
        writer.write(packet)
        await writer.drain()
        await asyncio.wait_for(events.wait(), 1)
        entry = trace.snapshot()[0]
        assert entry["direction"] == "EVENT"
        assert entry["raw_hex"] == packet.hex(" ").upper()
        assert entry["source"] == "s402_event"
        assert transport.statistics.successful_requests == 0
        assert transport.statistics.failed_requests == 0
        assert transport.statistics.timeout_count == 0
        assert transport.statistics.crc_error_count == 0
    finally:
        await transport.close()


def test_ring_clear_and_restart():
    trace = ProtocolTraceBuffer()
    for index in range(250):
        trace.record("TX", bytes([index % 256]), source="poll")
    snapshot = trace.snapshot()
    assert len(snapshot) == 200
    assert snapshot[0]["raw_hex"] == "32"
    snapshot.clear()
    assert len(trace.snapshot()) == 200
    trace.clear()
    assert trace.snapshot() == []
    assert ProtocolTraceBuffer().snapshot() == []


def test_identical_rapid_events_receive_unique_stable_timestamps():
    instant = datetime(2026, 10, 15, tzinfo=UTC)
    trace = ProtocolTraceBuffer()
    with patch("custom_components.coselig_modbus_hub.trace.datetime") as clock:
        clock.now.return_value = instant
        trace.record("EVENT", b"\x01\x06\x01\xa0\x07\x01", source="s402_event")
        trace.record("EVENT", b"\x01\x06\x01\xa0\x07\x01", source="s402_event")
    entries = trace.snapshot()
    assert entries[0]["raw_hex"] == entries[1]["raw_hex"]
    assert entries[0]["timestamp"] != entries[1]["timestamp"]
    assert datetime.fromisoformat(entries[1]["timestamp"]) > datetime.fromisoformat(
        entries[0]["timestamp"]
    )


@pytest.mark.parametrize("capacity", [0, 501])
def test_invalid_capacity(capacity):
    with pytest.raises(ValueError):
        ProtocolTraceBuffer(capacity)


async def test_trace_keeps_priority_and_latest_wins(gateway):
    trace = ProtocolTraceBuffer()
    transport = Transport("127.0.0.1", gateway.port, trace=trace)
    gateway.block = True
    first = asyncio.create_task(transport.submit(read(1, 4), poll=True))
    await gateway.arrived.wait()
    poll = asyncio.create_task(transport.submit(read(2, 4), poll=True))
    controls = [
        asyncio.create_task(transport.submit(write(3, 0x082A, value)))
        for value in [20, 30, 40, 80]
    ]
    await asyncio.sleep(0)
    gateway.release.set()
    try:
        await asyncio.gather(first, poll, *controls)
        assert [(r[0], r[1]) for r in gateway.requests] == [(1, 3), (3, 6), (2, 3)]
        assert gateway.requests[1][5] == 80
        assert len([e for e in trace.snapshot() if e["direction"] == "TX"]) == 3
    finally:
        await transport.close()
