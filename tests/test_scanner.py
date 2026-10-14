import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.coselig_modbus_hub.protocol import read
from custom_components.coselig_modbus_hub.scanner import scan
from custom_components.coselig_modbus_hub.transport import GatewayError, Transport


async def test_scan_found_only_valid(gateway):
    transport = Transport("127.0.0.1", gateway.port, timeout=0.02)
    gateway.modes.update({1: "timeout", 3: "timeout"})
    progress = []
    try:
        assert await scan(transport, 1, 4, lambda *args: progress.append(args)) == [
            2,
            4,
        ]
        assert all(request == read(request[0], 2) for request in gateway.requests)
        assert progress[-1] == (4, 4, 2)
        assert await transport.submit(read(2, 4), poll=True)
    finally:
        await transport.close()


async def test_default_range():
    transport = Transport("test", 502)
    with patch.object(
        transport, "submit", new=AsyncMock(return_value=b"valid")
    ) as submit:
        assert await scan(transport) == list(range(1, 248))
        assert [call.args[0][0] for call in submit.call_args_list] == list(
            range(1, 248)
        )
    await transport.close()


@pytest.mark.parametrize("mode", ["crc", "slave", "function", "malformed", "exception"])
async def test_scan_invalid_response(gateway, mode):
    transport = Transport("127.0.0.1", gateway.port, timeout=0.02)
    gateway.modes[1] = mode
    try:
        assert await scan(transport, 1, 2) == [2]
    finally:
        await transport.close()


async def test_disconnect_during_scan(gateway):
    gateway.modes[1] = "disconnect"
    transport = Transport("127.0.0.1", gateway.port)
    try:
        with pytest.raises(GatewayError):
            await scan(transport, 1, 2)
    finally:
        await transport.close()


async def test_cancellation_releases_scan(gateway):
    gateway.block = True
    transport = Transport("127.0.0.1", gateway.port)
    task = asyncio.create_task(scan(transport, 1, 2))
    await gateway.arrived.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    gateway.release.set()
    try:
        assert await transport.submit(read(3, 4), poll=True)
        assert [r[0] for r in gateway.requests] == [1, 3]
    finally:
        await transport.close()


@pytest.mark.parametrize("start,end", [(0, 1), (2, 1), (1, 248), (True, 32)])
async def test_invalid_range(start, end):
    transport = Transport("test", 502)
    with pytest.raises(ValueError):
        await scan(transport, start, end)
    await transport.close()
