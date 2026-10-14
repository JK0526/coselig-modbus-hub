"""Actual HA fixtures and a loopback RTU gateway; no现场 endpoints."""

import asyncio
from dataclasses import dataclass, field

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.coselig_modbus_hub.const import DOMAIN
from custom_components.coselig_modbus_hub.protocol import frame


@pytest.fixture(autouse=True)
def custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
def entry(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=1,
        data={"host": "gateway.local", "port": 502},
        options={
            "poll_interval": 2.0,
            "scan_start": 1,
            "scan_end": 32,
            "devices": [{"slave": 1, "model": "p404"}, {"slave": 3, "model": "p210"}],
        },
    )
    entry.add_to_hass(hass)
    return entry


@dataclass
class Gateway:
    port: int = 0
    connections: int = 0
    requests: list[bytes] = field(default_factory=list)
    modes: dict[int, str] = field(default_factory=dict)
    registers: dict[int, list[int]] = field(default_factory=dict)
    handlers: set = field(default_factory=set)
    writers: set = field(default_factory=set)
    arrived: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    block: bool = False

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.handlers.add(task)
        self.writers.add(writer)
        self.connections += 1
        try:
            while True:
                request = await reader.readexactly(8)
                self.requests.append(request)
                self.arrived.set()
                if self.block:
                    await self.release.wait()
                mode = self.modes.get(request[0], "normal")
                if mode == "disconnect":
                    return
                regs = self.registers.setdefault(request[0], [20, 25, 50, 75])
                if request[1] == 6:
                    regs[int.from_bytes(request[2:4], "big") - 0x082A] = request[5]
                    response = request
                else:
                    count = int.from_bytes(request[4:6], "big")
                    response = frame(
                        bytes([request[0], 3, count * 2])
                        + b"".join(value.to_bytes(2, "big") for value in regs[:count])
                    )
                if mode == "timeout":
                    continue
                if mode == "crc":
                    response = response[:-1] + bytes([response[-1] ^ 1])
                elif mode == "slave":
                    response = frame(bytes([request[0] + 1]) + response[1:-2])
                elif mode == "function":
                    response = frame(bytes([request[0], 7]) + response[2:-2])
                elif mode == "malformed":
                    response = frame(bytes([request[0], 3, 3, 0, 0, 0]))
                elif mode == "exception":
                    response = frame(bytes([request[0], request[1] | 0x80, 2]))
                elif mode == "echo" and request[1] == 6:
                    response = frame(request[:5] + bytes([request[5] + 1]))
                writer.write(response[:1])
                await writer.drain()
                await asyncio.sleep(0)
                writer.write(response[1:])
                await writer.drain()
        except asyncio.IncompleteReadError, ConnectionError:
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            self.handlers.discard(task)
            self.writers.discard(writer)


@pytest.fixture
async def gateway(socket_enabled):
    gateway = Gateway()
    server = await asyncio.start_server(gateway.handle, "127.0.0.1", 0)
    gateway.port = server.sockets[0].getsockname()[1]
    yield gateway
    server.close()
    tasks = list(gateway.handlers)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await server.wait_closed()
