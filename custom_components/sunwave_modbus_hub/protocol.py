"""Self-contained RTU-over-TCP framing used by the integration."""

from __future__ import annotations

import asyncio
import struct


class ProtocolError(ValueError):
    """A malformed or mismatched Modbus response."""


class DeviceException(ProtocolError):
    """A valid Modbus exception response."""

    def __init__(self, code: int):
        self.code = code
        super().__init__(f"Modbus exception {code}")


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
    return crc


def frame(data: bytes) -> bytes:
    return data + struct.pack("<H", crc16(data))


def read(slave: int, count: int) -> bytes:
    if type(slave) is not int or not 1 <= slave <= 247 or count not in (2, 4):
        raise ValueError("Invalid slave or register count")
    return frame(struct.pack(">BBHH", slave, 3, 0x082A, count))


def write(slave: int, register: int, value: int) -> bytes:
    if type(slave) is not int or not 1 <= slave <= 247:
        raise ValueError("Invalid slave")
    if register not in range(0x082A, 0x082E) or type(value) is not int or not 0 <= value <= 100:
        raise ValueError("Invalid register or value")
    return frame(struct.pack(">BBHBB", slave, 6, register, 5, value))


def validate(request: bytes, response: bytes) -> bytes:
    if len(response) < 5 or frame(response[:-2]) != response:
        raise ProtocolError("Invalid CRC or length")
    if response[0] != request[0]:
        raise ProtocolError("Wrong slave")
    if response[1] == request[1] | 0x80 and len(response) == 5:
        raise DeviceException(response[2])
    if response[1] != request[1]:
        raise ProtocolError("Wrong function")
    if request[1] == 6:
        if response != request:
            raise ProtocolError("Write echo mismatch")
    elif request[1] == 3:
        count = int.from_bytes(request[4:6], "big") * 2
        if response[2] != count or len(response) != count + 5:
            raise ProtocolError("Read length mismatch")
    else:
        raise ProtocolError("Unsupported function")
    return response


async def receive(reader: asyncio.StreamReader) -> bytes:
    """Read one ADU from a TCP stream, including fragmented frames."""
    header = await reader.readexactly(2)
    if header[1] in (0x83, 0x86):
        return header + await reader.readexactly(3)
    if header[1] == 6:
        return header + await reader.readexactly(6)
    if header[1] == 3:
        count = await reader.readexactly(1)
        if count[0] not in (4, 8):
            raise ProtocolError("Unsupported read size")
        return header + count + await reader.readexactly(count[0] + 2)
    raise ProtocolError("Unsupported response")
