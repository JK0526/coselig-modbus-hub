"""A single RTU stream with bounded stateful and action control scheduling."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from .const import (
    ACTION_CONTROL_CAPACITY,
    DEFAULT_TIMEOUT,
    STATEFUL_CONTROL_CAPACITY,
)
from .protocol import (
    CRCError,
    DeviceException,
    ProtocolError,
    receive,
    validate,
)
from .s402 import S402Event, is_s402_candidate, parse_s402_event
from .trace import ProtocolTraceBuffer

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
    source: str


@dataclass(frozen=True)
class ActionSlot:
    """Transport-owned FIFO identity for one non-coalescing action."""

    sequence: int


S402Callback = Callable[[S402Event], None]
ConnectionLostCallback = Callable[[], None]


class Transport:
    """The worker is the only owner allowed to exchange packets."""

    def __init__(
        self,
        host: str,
        port: int,
        timeout: float = DEFAULT_TIMEOUT,
        stateful_capacity: int = STATEFUL_CONTROL_CAPACITY,
        action_capacity: int = ACTION_CONTROL_CAPACITY,
        trace: ProtocolTraceBuffer | None = None,
        on_s402_event: S402Callback | None = None,
        on_connection_lost: ConnectionLostCallback | None = None,
    ) -> None:
        if (
            not host
            or not 1 <= port <= 65535
            or timeout <= 0
            or stateful_capacity < 1
            or action_capacity < 1
        ):
            raise ValueError("Invalid transport configuration")
        self.host, self.port, self.timeout = host, port, timeout
        self.stateful_capacity = stateful_capacity
        self.action_capacity = action_capacity
        self.statistics = Statistics()
        self.trace = trace
        self.on_s402_event = on_s402_event
        self.on_connection_lost = on_connection_lost
        self._controls: OrderedDict[CommandKey | ActionSlot, Pending] = OrderedDict()
        self._next_action_sequence = 0
        self._polls: deque[Pending] = deque()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._pending_response: asyncio.Future[bytes] | None = None
        self._pending_source = "response"
        self._worker: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._gate = asyncio.Lock()
        self._closed = False
        self._connections = 0
        self._disconnecting = False
        self._loss_reported = False

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    @property
    def queue_depth(self) -> int:
        return self.control_depth + self.poll_depth

    @property
    def control_depth(self) -> int:
        """Number of pending controls in the shared ordered schedule."""
        return len(self._controls)

    @property
    def stateful_depth(self) -> int:
        """Number of distinct pending CommandKeys, excluding in-flight work."""
        return sum(not isinstance(key, ActionSlot) for key in self._controls)

    @property
    def action_depth(self) -> int:
        """Number of pending non-coalescing ActionSlots."""
        return sum(isinstance(key, ActionSlot) for key in self._controls)

    @property
    def poll_depth(self) -> int:
        """Number of queued polls; the in-flight request is not counted."""
        return len(self._polls)

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
        self._loss_reported = False
        reader = self._reader
        self._reader_task = asyncio.create_task(
            self._read_loop(reader), name="coselig-rx"
        )

    async def disconnect(self) -> None:
        self._disconnecting = True
        try:
            reader_task, self._reader_task = self._reader_task, None
            if (
                reader_task is not None
                and reader_task is not asyncio.current_task()
                and not reader_task.done()
            ):
                reader_task.cancel()
                await asyncio.gather(reader_task, return_exceptions=True)
            pending, self._pending_response = self._pending_response, None
            if pending is not None and not pending.done():
                pending.set_exception(GatewayError("Gateway disconnected"))
            writer, self._writer = self._writer, None
            self._reader = None
            if writer is not None:
                writer.close()
                try:
                    async with asyncio.timeout(self.timeout):
                        await writer.wait_closed()
                except OSError, TimeoutError:
                    pass
        finally:
            self._disconnecting = False

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        """Continuously own the socket reader and route complete RTU frames."""
        try:
            while reader is self._reader and not self._disconnecting:
                packet = await receive(reader)
                if is_s402_candidate(packet):
                    event = parse_s402_event(packet)
                    if event is None:
                        _LOGGER.debug("Consumed invalid S402 event candidate")
                        continue
                    self._trace("EVENT", packet, source="s402_event")
                    if self.on_s402_event is not None:
                        try:
                            self.on_s402_event(event)
                        except Exception:
                            _LOGGER.debug("S402 event listener failed", exc_info=True)
                    continue

                pending = self._pending_response
                if pending is None or pending.done():
                    _LOGGER.debug("Consumed RTU frame without a pending request")
                    continue
                self._trace("RX", packet, source=self._pending_source)
                pending.set_result(packet)
        except asyncio.CancelledError:
            raise
        except (OSError, asyncio.IncompleteReadError) as error:
            if reader is self._reader and not self._disconnecting:
                self._mark_connection_lost(error)
        except ProtocolError as error:
            pending = self._pending_response
            if pending is not None and not pending.done():
                pending.set_exception(error)
            elif reader is self._reader and not self._disconnecting:
                # Framing can no longer be trusted when there is no request to own
                # the malformed frame, so reconnect through the Coordinator.
                self._mark_connection_lost(error)
        finally:
            if self._reader_task is asyncio.current_task():
                self._reader_task = None

    def _mark_connection_lost(self, error: BaseException) -> None:
        """Report unexpected stream loss once; Coordinator owns reconnect policy."""
        if self._disconnecting or self._loss_reported:
            return
        self._loss_reported = True
        self._reader = None
        writer, self._writer = self._writer, None
        pending, self._pending_response = self._pending_response, None
        gateway_error = GatewayError("Gateway connection lost")
        if pending is not None and not pending.done():
            pending.set_exception(gateway_error)
        if writer is not None:
            writer.close()
        _LOGGER.debug("Gateway RX stream ended", exc_info=error)
        if self.on_connection_lost is not None:
            try:
                self.on_connection_lost()
            except Exception:
                _LOGGER.exception("Gateway loss listener failed")

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
        source = "scan" if exclusive else "poll" if poll else "control"
        if exclusive:
            future = self._enqueue(request, poll, key, source)
        else:
            async with self._gate:
                future = self._enqueue(request, poll, key, source)
        return await future

    async def submit_action(self, request: bytes) -> bytes | None:
        """Submit a FIFO action that is never coalesced with another command."""
        async with self._gate:
            future = self._enqueue(request, False, None, "control", action=True)
        return await future

    def _enqueue(
        self,
        request: bytes,
        poll: bool,
        key: CommandKey | None,
        source: str,
        *,
        action: bool = False,
    ) -> asyncio.Future[bytes | None]:
        if self._closed:
            raise OfflineError("Transport closed")
        future = asyncio.get_running_loop().create_future()
        item = Pending(request, future, source)
        if poll:
            self._polls.append(item)
        elif action:
            if self.action_depth >= self.action_capacity:
                _LOGGER.warning("Action queue full; new action rejected")
                raise asyncio.QueueFull
            self._next_action_sequence += 1
            self._controls[ActionSlot(self._next_action_sequence)] = item
        else:
            key = key or (request[0], request[2:4].hex(), "write")
            if key in self._controls:
                old = self._controls[key]
                if not old.future.done():
                    old.future.set_result(None)  # Explicitly superseded; no write sent.
            elif self.stateful_depth >= self.stateful_capacity:
                _LOGGER.warning("Stateful control queue full; new command rejected")
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

    def _trace(
        self,
        direction: str,
        raw: bytes = b"",
        *,
        source: str,
        latency_ms: float | None = None,
    ) -> None:
        if self.trace is not None:
            try:
                self.trace.record(direction, raw, source=source, latency_ms=latency_ms)
            except Exception:
                # Observation must never turn a valid exchange into a failed one.
                _LOGGER.debug("Modbus trace observation failed", exc_info=True)

    async def _exchange(self, request: bytes, source: str) -> bytes:
        await self.connect()
        started = asyncio.get_running_loop().time()
        self._trace("TX", request, source=source)
        reader = self._reader
        writer = self._writer
        if reader is None or writer is None:
            raise GatewayError("Gateway is not connected")
        pending = asyncio.get_running_loop().create_future()
        self._pending_response = pending
        self._pending_source = source
        try:
            async with asyncio.timeout(self.timeout):
                writer.write(request)
                await writer.drain()
                response = await pending
                result = validate(request, response)
            self._trace(
                "OK",
                source=source,
                latency_ms=(asyncio.get_running_loop().time() - started) * 1000,
            )
            return result
        except TimeoutError:
            self._trace(
                "TIMEOUT",
                source=source,
                latency_ms=(asyncio.get_running_loop().time() - started) * 1000,
            )
            raise
        except ProtocolError as error:
            self._trace(
                "CRC ERROR" if isinstance(error, CRCError) else "ERROR",
                source=source,
                latency_ms=(asyncio.get_running_loop().time() - started) * 1000,
            )
            raise
        except (OSError, asyncio.IncompleteReadError) as error:
            self._trace(
                "ERROR",
                source=source,
                latency_ms=(asyncio.get_running_loop().time() - started) * 1000,
            )
            raise GatewayError("Gateway connection lost") from error
        finally:
            if self._pending_response is pending:
                self._pending_response = None
            if not pending.done():
                pending.cancel()

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
                result = await self._exchange(item.request, item.source)
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
