"""Track pending writes independently for each optimistic entity field."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from .transport import Pending


class OptimisticValue[T]:
    def __init__(self, value: T) -> None:
        self._base = value
        self._pending: dict[int, T] = {}
        self._sequence = 0

    @property
    def value(self) -> T:
        return (
            self._pending[next(reversed(self._pending))]
            if self._pending
            else self._base
        )

    def observe(self, value: T) -> None:
        self._base = value
        self._pending.clear()

    def stage(self, value: T, item: Pending, publish: Callable[[], None]) -> None:
        self._sequence += 1
        sequence = self._sequence
        self._pending[sequence] = value

        def settle(_future: asyncio.Future[bytes | None]) -> None:
            if sequence not in self._pending:
                return
            before = self.value
            del self._pending[sequence]
            # A validated echo confirms the write. A sent write with no valid echo
            # is uncertain, so keep its estimate until a poll can reconcile it.
            # Superseded, rejected and cancelled-before-send writes never reach TX.
            if item.sent:
                self._base = value
                self._pending = {
                    token: pending
                    for token, pending in self._pending.items()
                    if token > sequence
                }
            if self.value != before:
                publish()

        item.future.add_done_callback(settle)


async def wait_controls(items: tuple[Pending, ...]) -> None:
    results = await asyncio.gather(
        *(item.future for item in items), return_exceptions=True
    )
    for result in results:
        if isinstance(result, BaseException):
            raise result
