"""One config entry owns one bus and one coordinator."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from homeassistant.core import HomeAssistant

from .coordinator import Coordinator
from .models import Device
from .s402 import S402Event
from .scanner import ScanResult, scan
from .trace import ProtocolTraceBuffer
from .transport import GatewayError, Transport


@dataclass
class Hub:
    transport: Transport
    coordinator: Coordinator
    s402_ids: tuple[int, ...] = ()
    _s402_listeners: dict[tuple[int, int], set[Callable[[S402Event], None]]] = field(
        default_factory=dict, init=False, repr=False
    )

    def __post_init__(self) -> None:
        self.transport.on_s402_event = self._dispatch_s402_event
        self.transport.on_connection_lost = self.coordinator.gateway_lost

    @classmethod
    def create(
        cls,
        hass: HomeAssistant,
        host: str,
        port: int,
        devices: tuple[Device, ...],
        interval: float,
        s402_ids: tuple[int, ...] = (),
    ) -> Hub:
        transport = Transport(host, port, trace=ProtocolTraceBuffer())
        return cls(
            transport,
            Coordinator(hass, transport, devices, interval),
            s402_ids,
        )

    def subscribe_s402(
        self, device_id: int, di: int, listener: Callable[[S402Event], None]
    ) -> Callable[[], None]:
        """Subscribe one HA entity to exactly one configured input event."""
        if device_id not in self.s402_ids:
            raise ValueError("S402 device is not configured")
        key = (device_id, di)
        listeners = self._s402_listeners.setdefault(key, set())
        listeners.add(listener)

        def unsubscribe() -> None:
            current = self._s402_listeners.get(key)
            if current is not None:
                current.discard(listener)
                if not current:
                    self._s402_listeners.pop(key, None)

        return unsubscribe

    def _dispatch_s402_event(self, event: S402Event) -> None:
        """Fan out a valid configured event without awaiting HA automations."""
        if event.device_id not in self.s402_ids:
            return
        for listener in tuple(
            self._s402_listeners.get((event.device_id, event.di), ())
        ):
            listener(event)

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
