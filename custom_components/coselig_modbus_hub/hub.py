"""One config entry owns one bus and one coordinator."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.core import HomeAssistant

from .coordinator import Coordinator
from .models import Device
from .scanner import ScanResult, scan
from .trace import ProtocolTraceBuffer
from .transport import GatewayError, Transport


@dataclass
class Hub:
    transport: Transport
    coordinator: Coordinator

    @classmethod
    def create(
        cls,
        hass: HomeAssistant,
        host: str,
        port: int,
        devices: tuple[Device, ...],
        interval: float,
    ) -> Hub:
        transport = Transport(host, port, trace=ProtocolTraceBuffer())
        return cls(transport, Coordinator(hass, transport, devices, interval))

    async def start(self) -> None:
        await self.transport.connect()
        await self.coordinator.async_poll_round()
        if not self.coordinator.gateway_available:
            raise ConnectionError("Gateway lost during initial poll")
        self.coordinator.start()

    async def scan(
        self,
        start: int,
        end: int,
        progress: Callable[[int, int, int], None] | None = None,
    ) -> list[ScanResult]:
        async with self.coordinator.pause():
            try:
                return await scan(self.transport, start, end, progress)
            except GatewayError:
                self.coordinator.gateway_lost()
                raise
            finally:
                # Wake the existing scheduler; Poll=0 continues to wait without I/O.
                self.coordinator.set_poll_interval(self.coordinator.poll_interval)

    async def stop(self) -> None:
        try:
            await self.coordinator.stop()
        finally:
            await self.transport.close()
