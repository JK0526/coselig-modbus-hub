"""Common discovery, read-only family identification, then user confirmation."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from .const import DEFAULT_SCAN_END, DEFAULT_SCAN_START
from .models import Model
from .profiles import DISCOVERY, FAMILY_PROBES, DeviceFamily, PollSpec
from .protocol import ProtocolError
from .transport import Transport

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScanResult:
    slave: int
    family: DeviceFamily = DeviceFamily.UNKNOWN
    candidates: tuple[Model, ...] = tuple(Model)
    recommended: Model | None = None


async def _probe(transport: Transport, slave: int, spec: PollSpec) -> bool:
    try:
        await transport.submit(spec.request(slave), poll=True, exclusive=True)
    except (TimeoutError, ProtocolError) as error:
        _LOGGER.debug(
            "Probe slave %s FC%02X not matched: %s", slave, spec.function, error
        )
        return False
    return True


async def scan(
    transport: Transport,
    start: int = DEFAULT_SCAN_START,
    end: int = DEFAULT_SCAN_END,
    progress: Callable[[int, int, int], None] | None = None,
) -> list[ScanResult]:
    if type(start) is not int or type(end) is not int or not 1 <= start <= end <= 247:
        raise ValueError("Invalid scan range")
    found = []
    async with transport.exclusive():
        for index, slave in enumerate(range(start, end + 1), 1):
            if await _probe(transport, slave, DISCOVERY):
                result = ScanResult(slave)
                for spec, family, candidates in FAMILY_PROBES:
                    if await _probe(transport, slave, spec):
                        result = ScanResult(slave, family, candidates, candidates[0])
                        break
                found.append(result)
            if progress is not None:
                progress(index, end - start + 1, len(found))
    return found
