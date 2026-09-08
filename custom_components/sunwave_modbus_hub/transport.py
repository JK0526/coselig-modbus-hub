"""Serialized, bounded RTU-over-TCP transport."""

from __future__ import annotations

import asyncio
import itertools

from .protocol import DeviceException, receive, validate


class Transport:
    """One in-flight request, with control commands ahead of polls."""

    def __init__(self, host: str, port: int, timeout: float, capacity: int = 100):
        if not timeout > 0 or capacity < 1 or not 1 <= port <= 65535:
            raise ValueError("Invalid transport configuration")
        self.host = host
        self.port = port
        self.timeout = timeout
        self.queue: asyncio.PriorityQueue = asyncio.PriorityQueue(capacity)
        self._sequence = itertools.count()
        self._polls: set[bytes] = set()
        self._reader = None
        self._writer = None
        self._worker = None
        self._closed = False

    async def submit(self, request: bytes, *, poll: bool = False):
        if self._closed:
            raise RuntimeError("Transport closed")
        if poll and request in self._polls:
            return None
        future = asyncio.get_running_loop().create_future()
        self.queue.put_nowait((1 if poll else 0, next(self._sequence), request, future, poll))
        if poll:
            self._polls.add(request)
        if self._worker is None:
            self._worker = asyncio.create_task(self._run())
        return await future

    async def _disconnect(self):
        writer, self._writer = self._writer, None
        self._reader = None
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def _exchange(self, request: bytes):
        if self._writer is None:
            self._reader, self._writer = await asyncio.open_connection(self.host, self.port)
        self._writer.write(request)
        await self._writer.drain()
        return validate(request, await receive(self._reader))

    async def _run(self):
        while True:
            _, _, request, future, poll = await self.queue.get()
            try:
                if future.cancelled():
                    continue
                async with asyncio.timeout(self.timeout):
                    result = await self._exchange(request)
                if not future.done():
                    future.set_result(result)
            except asyncio.CancelledError:
                if not future.done():
                    future.cancel()
                raise
            except Exception as error:
                if not isinstance(error, DeviceException):
                    await self._disconnect()
                if not future.done():
                    future.set_exception(error)
            finally:
                if poll:
                    self._polls.discard(request)
                self.queue.task_done()

    async def close(self):
        self._closed = True
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None
        while not self.queue.empty():
            _, _, _, future, _ = self.queue.get_nowait()
            future.cancel()
            self.queue.task_done()
        self._polls.clear()
        await self._disconnect()
