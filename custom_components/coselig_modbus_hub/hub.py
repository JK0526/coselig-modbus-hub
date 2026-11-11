"""One config entry owns one bus and one coordinator."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field

from homeassistant.core import HomeAssistant

from .coordinator import Coordinator
from .models import Device
from .r410_cover import R410CoverConfig
from .s402 import S402Event
from .scanner import ScanResult, scan
from .trace import ProtocolTraceBuffer
from .transport import GatewayError, GatewayExchangeError, OfflineError, Transport


class HubScanInterrupted(OfflineError):
    """The borrowed Hub stopped during a scan."""


@dataclass
class Hub:
    transport: Transport
    coordinator: Coordinator
    s402_ids: tuple[int, ...] = ()
    r410_covers: tuple[R410CoverConfig, ...] = ()
    stopping: bool = field(default=False, init=False)
    _s402_listeners: dict[tuple[int, int], set[Callable[[S402Event], None]]] = field(
        default_factory=dict, init=False, repr=False
    )

    def __post_init__(self) -> None:
        self.transport.on_s402_event = self._dispatch_s402_event

    @classmethod
    def create(
        cls,
        hass: HomeAssistant,
        host: str,
        port: int,
        devices: tuple[Device, ...],
        interval: float,
        s402_ids: tuple[int, ...] = (),
        r410_covers: tuple[R410CoverConfig, ...] = (),
    ) -> Hub:
        transport = Transport(host, port, trace=ProtocolTraceBuffer())
        return cls(
            transport,
            Coordinator(hass, transport, devices, interval),
            s402_ids,
            r410_covers,
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
        error = await self.coordinator.async_poll_round()
        # Keep the cursor and failure counts if a slave interrupted a multi-device
        # bootstrap. Retrying the entire HA entry would always start at slave one.
        can_resume = (
            isinstance(error, GatewayExchangeError)
            and len(self.coordinator.devices) > 1
        )
        if self.coordinator.gateway_failed and not can_resume:
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
                if self.stopping:
                    raise HubScanInterrupted
                return await scan(self.transport, start, end, progress)
            except asyncio.CancelledError as error:
                # A cancelled request Future is different from cancelling this scan task.
                if not asyncio.current_task().cancelling():
                    if self.stopping:
                        raise HubScanInterrupted from error
                    if self.transport.closed:
                        raise OfflineError("Transport closed") from error
                raise
            except OfflineError as error:
                if self.stopping:
                    raise HubScanInterrupted from error
                raise
            except GatewayError:
                self.coordinator.gateway_lost()
                raise
            finally:
                # Wake the existing scheduler; Poll=0 continues to wait without I/O.
                self.coordinator.set_poll_interval(self.coordinator.poll_interval)

    async def stop(self) -> None:
        self.stopping = True
        try:
            await self.coordinator.stop()
        finally:
            await self.transport.close()
