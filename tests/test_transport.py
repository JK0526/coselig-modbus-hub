import asyncio

import pytest

from custom_components.coselig_modbus_hub.protocol import (
    CRCError,
    DeviceException,
    ProtocolError,
)
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
