"""A single RTU stream with bounded, latest-wins control scheduling."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from .const import CONTROL_CAPACITY, DEFAULT_TIMEOUT
from .protocol import CRCError, DeviceException, receive, validate

_LOGGER = logging.getLogger(__name__)
CommandKey = tuple[int, str, str]


class GatewayError(ConnectionError):
    """A TCP failure, distinct from a slave transaction timeout."""


class OfflineError(ConnectionError):
    """A control was rejected or discarded; it will never be replayed."""


@dataclass
class Statistics:
    successful_requests: int = 0
    failed_requests: int = 0
    timeout_count: int = 0
    crc_error_count: int = 0
    reconnect_count: int = 0
    queue_peak: int = 0


@dataclass
class Pending:
    request: bytes
    future: asyncio.Future[bytes | None]


class Transport:
    """The worker is the only owner allowed to exchange packets."""

    def __init__(
        self,
        host: str,
        port: int,
        timeout: float = DEFAULT_TIMEOUT,
        capacity: int = CONTROL_CAPACITY,
    ) -> None:
        if not host or not 1 <= port <= 65535 or timeout <= 0 or capacity < 1:
            raise ValueError("Invalid transport configuration")
        self.host, self.port, self.timeout, self.capacity = (
            host,
            port,
            timeout,
            capacity,
        )
        self.statistics = Statistics()
        self._controls: OrderedDict[CommandKey, Pending] = OrderedDict()
        self._polls: deque[Pending] = deque()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._worker: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._gate = asyncio.Lock()
        self._closed = False
        self._connections = 0

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    @property
    def queue_depth(self) -> int:
        return len(self._controls) + len(self._polls)

    async def connect(self) -> None:
        if self._closed:
            raise OfflineError("Transport closed")
        if self.connected:
            return
        try:
            async with asyncio.timeout(self.timeout):
                self._reader, self._writer = await asyncio.open_connection(
                    self.host, self.port
                )
        except (OSError, TimeoutError) as error:
            raise GatewayError("Cannot connect to gateway") from error
        if self._connections:
            self.statistics.reconnect_count += 1
        self._connections += 1

    async def disconnect(self) -> None:
        writer, self._writer = self._writer, None
        self._reader = None
        if writer is not None:
            writer.close()
            try:
                async with asyncio.timeout(self.timeout):
                    await writer.wait_closed()
            except OSError, TimeoutError:
                pass

    def discard_controls(self) -> None:
        for item in self._controls.values():
            if not item.future.done():
                item.future.set_exception(
                    OfflineError("Pending command discarded after bus failure")
                )
        self._controls.clear()

    async def submit(
        self,
        request: bytes,
        *,
        poll: bool = False,
        key: CommandKey | None = None,
        exclusive: bool = False,
    ) -> bytes | None:
        if exclusive:
            future = self._enqueue(request, poll, key)
        else:
            async with self._gate:
                future = self._enqueue(request, poll, key)
        return await future

    def _enqueue(
        self, request: bytes, poll: bool, key: CommandKey | None
    ) -> asyncio.Future[bytes | None]:
        if self._closed:
            raise OfflineError("Transport closed")
        future = asyncio.get_running_loop().create_future()
        item = Pending(request, future)
        if poll:
            self._polls.append(item)
        else:
            key = key or (request[0], request[2:4].hex(), "write")
            if key in self._controls:
                old = self._controls[key]
                if not old.future.done():
                    old.future.set_result(None)  # Explicitly superseded; no write sent.
            elif len(self._controls) >= self.capacity:
                _LOGGER.warning("Control queue full; new command rejected")
                raise asyncio.QueueFull
            self._controls[key] = item
        self.statistics.queue_peak = max(self.statistics.queue_peak, self.queue_depth)
        self._idle.clear()
        self._wake.set()
        if self._worker is None:
            self._worker = asyncio.create_task(self._run(), name="coselig-transport")
        return future

    @asynccontextmanager
    async def exclusive(self) -> AsyncIterator[None]:
        """Drain earlier transactions and keep scan traffic alone on the bus."""
        async with self._gate:
            await self._idle.wait()
            yield

    async def _exchange(self, request: bytes) -> bytes:
        await self.connect()
        try:
            async with asyncio.timeout(self.timeout):
                self._writer.write(request)
                await self._writer.drain()
                return validate(request, await receive(self._reader))
        except TimeoutError:
            raise
        except (OSError, asyncio.IncompleteReadError) as error:
            raise GatewayError("Gateway connection lost") from error

    async def _run(self) -> None:
        while True:
            await self._wake.wait()
            if self._controls:
                _, item = self._controls.popitem(last=False)
            elif self._polls:
                item = self._polls.popleft()
            else:
                self._wake.clear()
                self._idle.set()
                continue
            try:
                if item.future.cancelled():
                    continue
                result = await self._exchange(item.request)
                self.statistics.successful_requests += 1
                if not item.future.done():
                    item.future.set_result(result)
            except asyncio.CancelledError:
                item.future.cancel()
                raise
            except Exception as error:
                self.statistics.failed_requests += 1
                if isinstance(error, TimeoutError):
                    self.statistics.timeout_count += 1
                if isinstance(error, CRCError):
                    self.statistics.crc_error_count += 1
                if not isinstance(error, DeviceException):
                    self.discard_controls()
                    await self.disconnect()
                if not item.future.done():
                    item.future.set_exception(error)

    async def close(self) -> None:
        self._closed = True
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
        for item in [*self._controls.values(), *self._polls]:
            item.future.cancel()
        self._controls.clear()
        self._polls.clear()
        self._idle.set()
        await self.disconnect()
