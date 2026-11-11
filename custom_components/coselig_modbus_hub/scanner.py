"""Common discovery, read-only capability probes, then user confirmation."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from .const import DEFAULT_SCAN_END, DEFAULT_SCAN_START
from .models import Model
from .profiles import (
    DIMMER_NARROW_PROBE,
    DIMMER_WIDE_PROBE,
    DISCOVERY,
    RELAY_PROBE,
    DeviceFamily,
    PollSpec,
)
from .protocol import CRCError, DeviceException, ProtocolError
from .transport import Transport

_LOGGER = logging.getLogger(__name__)

_DIMMER_MODELS = (Model.P404, Model.P210)


class Confidence(StrEnum):
    LOW = "low"
    HIGH = "high"


@dataclass(frozen=True)
class ScanResult:
    slave: int
    family: DeviceFamily = DeviceFamily.UNKNOWN
    candidates: tuple[Model, ...] = tuple(Model)
    recommended: Model | None = None
    evidence: str = ""
    confidence: Confidence | None = None


async def _probe(
    transport: Transport, slave: int, spec: PollSpec
) -> TimeoutError | ProtocolError | None:
    try:
        await transport.submit(spec.request(slave), poll=True, exclusive=True)
    except (TimeoutError, ProtocolError) as error:
        _LOGGER.debug(
            "Probe slave %s FC%02X count %s not matched: %s",
            slave,
            spec.function,
            spec.count,
            error,
        )
        return error
    # GatewayError propagates so a TCP failure cannot become model evidence.
    return None


def _known_bad_crc(error: TimeoutError | ProtocolError | None, slave: int) -> bool:
    return isinstance(error, CRCError) and error.response == bytes(
        (slave, 0x83, 0x03, 0x00, 0x3D)
    )


async def _identify(transport: Transport, slave: int) -> ScanResult:
    wide = await _probe(transport, slave, DIMMER_WIDE_PROBE)
    if wide is None:
        return ScanResult(
            slave,
            DeviceFamily.DIMMER,
            _DIMMER_MODELS,
            Model.P404,
            "FC03 0x082A：4 個暫存器回覆有效。",
            Confidence.HIGH,
        )

    if await _probe(transport, slave, DIMMER_NARROW_PROBE) is None:
        confidence = Confidence.LOW
        if isinstance(wide, DeviceException):
            reason = f"4 個暫存器收到有效 Modbus Exception 0x{wide.code:02X}"
            if wide.code in (2, 3):
                confidence = Confidence.HIGH
        elif _known_bad_crc(wide, slave):
            # Recheck both sides of the pattern. Never accept the invalid frame as data.
            repeat = await _probe(transport, slave, DIMMER_WIDE_PROBE)
            if repeat is None:
                return ScanResult(
                    slave,
                    DeviceFamily.DIMMER,
                    _DIMMER_MODELS,
                    Model.P404,
                    "FC03 0x082A：重試時收到有效的 4 暫存器回覆。",
                    Confidence.LOW,
                )
            reason = "4 個暫存器出現特徵性 FC83/03 CRC 錯誤"
            if await _probe(
                transport, slave, DIMMER_NARROW_PROBE
            ) is None and _known_bad_crc(repeat, slave):
                confidence = Confidence.HIGH
                reason = "兩次重現 4 個暫存器的特徵性 FC83/03 CRC 錯誤"
        elif isinstance(wide, CRCError):
            reason = "4 個暫存器回覆 CRC 錯誤"
        elif isinstance(wide, TimeoutError):
            reason = "4 個暫存器讀取逾時"
        else:
            reason = "4 個暫存器回覆未通過協定驗證"
        return ScanResult(
            slave,
            DeviceFamily.DIMMER,
            _DIMMER_MODELS,
            Model.P210,
            f"FC03 0x082A：{reason}，2 個暫存器回覆有效。",
            confidence,
        )

    if await _probe(transport, slave, RELAY_PROBE) is None:
        return ScanResult(slave, DeviceFamily.RELAY, (Model.R410,), Model.R410)
    return ScanResult(slave)


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
            if await _probe(transport, slave, DISCOVERY) is None:
                found.append(await _identify(transport, slave))
            if progress is not None:
                progress(index, end - start + 1, len(found))
    return found
