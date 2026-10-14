"""Find responsive slaves with a shared two-register probe, never a model guess."""

from __future__ import annotations

import logging
from collections.abc import Callable

from .const import DEFAULT_SCAN_END, DEFAULT_SCAN_START
from .protocol import ProtocolError, read
from .transport import Transport

_LOGGER = logging.getLogger(__name__)


async def scan(
    transport: Transport,
    start: int = DEFAULT_SCAN_START,
    end: int = DEFAULT_SCAN_END,
    progress: Callable[[int, int, int], None] | None = None,
) -> list[int]:
    if type(start) is not int or type(end) is not int or not 1 <= start <= end <= 247:
        raise ValueError("Invalid scan range")
    found: list[int] = []
    async with transport.exclusive():
        for index, slave in enumerate(range(start, end + 1), 1):
            _LOGGER.debug("Scan slave %s", slave)
            try:
                await transport.submit(read(slave, 2), poll=True, exclusive=True)
            except TimeoutError, ProtocolError:
                pass
            else:
                found.append(slave)
            if progress is not None:
                progress(index, end - start + 1, len(found))
    return found
