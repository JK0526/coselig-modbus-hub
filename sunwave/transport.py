"""One in-flight RTU request per connection, bounded priority queue."""
import asyncio
import itertools
from .protocol import validate, receive, DeviceException


class Transport:
    def __init__(self, host, port=502, timeout=0.5, capacity=100):
        if timeout <= 0 or capacity < 1 or not 1 <= port <= 65535:
            raise ValueError('Invalid transport configuration')
        self.host, self.port, self.timeout = host, port, timeout
        self.queue = asyncio.PriorityQueue(capacity)
        self.sequence = itertools.count()
        self.polls = set()
        self.reader = self.writer = self.worker = None
        self.closed = False

    async def submit(self, request, *, poll=False):
        if self.closed:
            raise RuntimeError('Transport closed')
        if poll and request in self.polls:
            return None
        future = asyncio.get_running_loop().create_future()
        # QueueFull is explicit: never silently discard an accepted control.
        self.queue.put_nowait((1 if poll else 0, next(self.sequence), request, future, poll))
        if poll:
            self.polls.add(request)
        if self.worker is None:
            self.worker = asyncio.create_task(self._run())
        return await future

    async def _disconnect(self):
        writer, self.writer = self.writer, None
        self.reader = None
        if writer:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def _exchange(self, request):
        if self.writer is None:
            self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
        self.writer.write(request)
        await self.writer.drain()
        return validate(request, await receive(self.reader))

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
                # On timeout/malformed response abandon this connection. RTU has
                # no transaction ID: never reuse its pending bytes for a new command.
                if not isinstance(error, DeviceException):
                    await self._disconnect()
                if not future.done():
                    future.set_exception(error)
            finally:
                if poll:
                    self.polls.discard(request)
                self.queue.task_done()

    async def close(self):
        self.closed = True
        if self.worker:
            self.worker.cancel()
            try:
                await self.worker
            except asyncio.CancelledError:
                pass
        while not self.queue.empty():
            _, _, request, future, poll = self.queue.get_nowait()
            future.cancel()
            self.queue.task_done()
        self.polls.clear()
        await self._disconnect()
