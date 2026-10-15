import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.coselig_modbus_hub.scanner import scan
from custom_components.coselig_modbus_hub.transport import GatewayError, Transport
from tests.dimmer_packets import read


async def test_scan_found_only_valid(gateway):
    transport = Transport("127.0.0.1", gateway.port, timeout=0.02)
    gateway.modes.update({1: "timeout", 3: "timeout"})
    progress = []
    try:
        assert [
            r.slave
            for r in await scan(transport, 1, 4, lambda *args: progress.append(args))
        ] == [2, 4]
        assert [
            int.from_bytes(request[2:4], "big") for request in gateway.requests
        ] == [0, 0, 0x082A, 0, 0, 0x082A]
        assert progress[-1] == (4, 4, 2)
        assert await transport.submit(read(2, 4), poll=True)
    finally:
        await transport.close()


async def test_default_range():
    transport = Transport("test", 502)
    with patch.object(
        transport, "submit", new=AsyncMock(return_value=b"valid")
    ) as submit:
        assert [r.slave for r in await scan(transport)] == list(range(1, 248))
        assert [call.args[0][0] for call in submit.call_args_list] == [
            slave for slave in range(1, 248) for _ in range(2)
        ]
    await transport.close()


@pytest.mark.parametrize("mode", ["crc", "slave", "function", "malformed", "exception"])
async def test_scan_invalid_response(gateway, mode):
    transport = Transport("127.0.0.1", gateway.port, timeout=0.02)
    gateway.modes[1] = mode
    try:
        assert [r.slave for r in await scan(transport, 1, 2)] == [2]
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


async def test_discovery_and_families_custom_range(gateway):
    from custom_components.coselig_modbus_hub.models import Model
    from custom_components.coselig_modbus_hub.profiles import DeviceFamily

    gateway.families.update({51: "dimmer", 52: "relay", 53: "unknown"})
    transport = Transport("127.0.0.1", gateway.port, timeout=0.02)
    try:
        results = await scan(transport, 51, 53)
        assert [(r.slave, r.family, r.candidates, r.recommended) for r in results] == [
            (51, DeviceFamily.DIMMER, (Model.P404, Model.P210), Model.P404),
            (52, DeviceFamily.RELAY, (Model.R410,), Model.R410),
            (53, DeviceFamily.UNKNOWN, tuple(Model), None),
        ]
        assert [
            (r[0], r[1], int.from_bytes(r[2:4], "big"), int.from_bytes(r[4:6], "big"))
            for r in gateway.requests
        ] == [
            (51, 3, 0, 1),
            (51, 3, 0x082A, 2),
            (52, 3, 0, 1),
            (52, 3, 0x082A, 2),
            (52, 2, 0, 4),
            (53, 3, 0, 1),
            (53, 3, 0x082A, 2),
            (53, 2, 0, 4),
        ]
        assert transport.connected
    finally:
        await transport.close()


@pytest.mark.parametrize("mode", ["timeout", "crc", "exception"])
async def test_family_probe_failure_retains_discovered_unknown(gateway, mode):
    from custom_components.coselig_modbus_hub.profiles import DeviceFamily

    gateway.probe_modes.update({(80, 3, 0x082A): mode, (80, 2, 0): mode})
    transport = Transport("127.0.0.1", gateway.port, timeout=0.02)
    try:
        results = await scan(transport, 80, 80)
        assert len(results) == 1
        assert results[0].family is DeviceFamily.UNKNOWN
    finally:
        await transport.close()
