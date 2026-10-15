"""Sequential polling, per-slave health, and gateway reconnection."""

from __future__ import annotations

import asyncio
import logging
import math
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import BACKOFF, DOMAIN
from .models import Device
from .profiles import ModbusData
from .protocol import ProtocolError
from .transport import CommandKey, GatewayError, OfflineError, Transport

_LOGGER = logging.getLogger(__name__)


def valid_poll_interval(interval: float) -> bool:
    return math.isfinite(interval) and (interval == 0 or interval >= 0.5)


@dataclass
class DeviceState(ModbusData):
    available: bool = False
    consecutive_failures: int = 0
    poll_started: float = 0


class Coordinator(DataUpdateCoordinator[dict[int, DeviceState]]):
    """HA listener delivery with one explicit target-period polling loop."""

    def __init__(
        self,
        hass: HomeAssistant,
        transport: Transport,
        devices: tuple[Device, ...],
        poll_interval: float,
    ) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN)
        if not valid_poll_interval(poll_interval):
            raise ValueError("Poll interval must be 0 or >= 0.5 seconds")
        self.transport, self.devices, self.poll_interval = (
            transport,
            devices,
            poll_interval,
        )
        self.states = {device.slave: DeviceState() for device in devices}
        self.gateway_available = True
        self.scanning = False
        self._round_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._backoff_index = 0
        self._closed = False
        self._schedule_wake = asyncio.Event()
        self._round_times: deque[float] = deque(maxlen=20)
        self.async_set_updated_data(self.states)

    @property
    def polling_paused(self) -> bool:
        return self.poll_interval == 0

    @property
    def last_poll_round_ms(self) -> float:
        return self._round_times[-1] if self._round_times else 0.0

    @property
    def average_poll_round_ms(self) -> float:
        return (
            sum(self._round_times) / len(self._round_times)
            if self._round_times
            else 0.0
        )

    def set_poll_interval(self, interval: float) -> None:
        if not valid_poll_interval(interval):
            raise ValueError("Poll interval must be 0 or >= 0.5 seconds")
        self.poll_interval = interval
        self._schedule_wake.set()

    async def _wait_for_schedule(self, timeout: float | None = None) -> None:
        try:
            async with asyncio.timeout(timeout):
                await self._schedule_wake.wait()
        except TimeoutError:
            pass
        self._schedule_wake.clear()

    def next_backoff(self) -> int:
        delay = BACKOFF[min(self._backoff_index, len(BACKOFF) - 1)]
        self._backoff_index += 1
        return delay

    def gateway_lost(self) -> None:
        if self.gateway_available:
            _LOGGER.warning("Gateway lost")
        self.gateway_available = False
        self._schedule_wake.set()
        self.transport.discard_controls()
        for state in self.states.values():
            state.available = False
        self.async_set_updated_data(self.states)

    async def _async_update_data(self) -> dict[int, DeviceState]:
        """Support explicit HA refreshes through the same serialized scheduler."""
        await self.async_poll_round()
        return self.states

    async def async_poll_round(self, *, background: bool = False) -> None:
        async with self._round_lock:
            started = asyncio.get_running_loop().time()
            await self._poll_devices(background=background)
            if self.devices:
                self._round_times.append(
                    (asyncio.get_running_loop().time() - started) * 1000
                )

    async def _poll_devices(self, *, background: bool) -> None:
        successes = 0
        for device in self.devices:
            if background and self.polling_paused:
                return
            state = self.states[device.slave]
            started = asyncio.get_running_loop().time()
            _LOGGER.debug("Poll slave %s", device.slave)
            try:
                data = ModbusData()
                for spec in device.profile.poll_specs(device):
                    response = await self.transport.submit(
                        spec.request(device.slave), poll=True
                    )
                    device.profile.decode(spec, response, data)
            except GatewayError:
                self.gateway_lost()
                return
            except TimeoutError, ProtocolError:
                state.consecutive_failures += 1
                if state.consecutive_failures >= 3 and state.available:
                    _LOGGER.warning("Slave #%s unavailable", device.slave)
                    state.available = False
            else:
                successes += 1
                if not state.available and state.consecutive_failures >= 3:
                    _LOGGER.info("Slave #%s restored", device.slave)
                state.holding_registers = data.holding_registers
                state.input_registers = data.input_registers
                state.coils = data.coils
                state.discrete_inputs = data.discrete_inputs
                state.poll_started = started
                state.consecutive_failures = 0
                state.available = True
            self.async_set_updated_data(self.states)
        if self.devices and not successes:
            # Not proof of TCP outage: retain per-slave failure thresholds.
            await self.transport.disconnect()

    async def async_command(self, slave: int, request: bytes, key: CommandKey) -> None:
        if (
            self.scanning
            or not self.gateway_available
            or not self.states[slave].available
        ):
            raise OfflineError("Device unavailable or scan in progress")
        try:
            await self.transport.submit(request, key=key)
        except GatewayError:
            self.gateway_lost()
            raise
        except TimeoutError, ProtocolError:
            # Transport counters record the error. Poll is the only state authority.
            _LOGGER.debug("Write failed for slave %s; awaiting normal poll", slave)
            raise

    async def _loop(self) -> None:
        while not self._closed:
            if not self.gateway_available:
                await asyncio.sleep(self.next_backoff())
                try:
                    async with self._round_lock:
                        await self.transport.connect()
                except GatewayError:
                    continue
                self.gateway_available = True
                self._backoff_index = 0
                _LOGGER.info("Gateway restored")
                if self.polling_paused:
                    # No reconnect FC03 while paused. Restore previously confirmed health.
                    for state in self.states.values():
                        state.available = (
                            state.populated and state.consecutive_failures < 3
                        )
                    self.async_set_updated_data(self.states)
            if self.polling_paused:
                await self._wait_for_schedule()
                continue
            self._schedule_wake.clear()
            started = asyncio.get_running_loop().time()
            await self.async_poll_round(background=True)
            if not self.gateway_available:
                continue
            remaining = self.poll_interval - (
                asyncio.get_running_loop().time() - started
            )
            if remaining > 0:
                await self._wait_for_schedule(remaining)
            else:
                # Yield after slow rounds without building another poll task.
                await asyncio.sleep(0)

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="coselig-poll")
        _LOGGER.info("Hub started; configured %s devices", len(self.devices))

    @asynccontextmanager
    async def pause(self) -> AsyncIterator[None]:
        async with self._round_lock:
            self.scanning = True
            try:
                yield
            finally:
                self.scanning = False

    async def stop(self) -> None:
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
