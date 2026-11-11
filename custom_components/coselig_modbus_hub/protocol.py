"""Self-contained RTU-over-TCP framing used by the integration."""

from __future__ import annotations

import asyncio
import struct


class ProtocolError(ValueError):
    """A malformed or mismatched Modbus response."""


class CRCError(ProtocolError):
    """A response with an invalid checksum, retained for scan evidence only."""

    def __init__(self, response: bytes):
        self.response = response
        super().__init__("Invalid CRC")


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


def _uint16(value: int) -> bool:
    return type(value) is int and 0 <= value <= 0xFFFF


def _request(slave: int, function: int, address: int, value: int) -> bytes:
    if type(slave) is not int or not 1 <= slave <= 247:
        raise ValueError("Invalid slave")
    if not _uint16(address) or not _uint16(value):
        raise ValueError("Invalid address or value")
    return frame(struct.pack(">BBHH", slave, function, address, value))


def _read(slave: int, function: int, start: int, count: int) -> bytes:
    maximum = 2000 if function in (1, 2) else 125
    if type(count) is not int or not 1 <= count <= maximum:
        raise ValueError("Invalid read count")
    if not _uint16(start) or start + count > 0x10000:
        raise ValueError("Invalid read range")
    return _request(slave, function, start, count)


def read_coils(slave: int, start: int, count: int) -> bytes:
    return _read(slave, 1, start, count)


def read_discrete_inputs(slave: int, start: int, count: int) -> bytes:
    return _read(slave, 2, start, count)


def read_holding_registers(slave: int, start: int, count: int) -> bytes:
    return _read(slave, 3, start, count)


def read_input_registers(slave: int, start: int, count: int) -> bytes:
    return _read(slave, 4, start, count)


def write_single_coil(slave: int, address: int, value: bool) -> bytes:
    if type(value) is not bool:
        raise ValueError("Coil value must be bool")
    return _request(slave, 5, address, 0xFF00 if value else 0)


def write_single_register(slave: int, address: int, value: int) -> bytes:
    return _request(slave, 6, address, value)


def manual_request(hex_text: str, auto_crc: bool) -> bytes:
    """Parse one structurally verifiable unicast request."""
    if len(hex_text) > 1024:
        raise ValueError("HEX input is too long")
    packet = bytes.fromhex(hex_text)
    body = packet if auto_crc else packet[:-2]
    if len(body) < 6:
        raise ValueError("Incomplete RTU request")
    slave, function, start, value = struct.unpack(">BBHH", body[:6])
    if not 1 <= slave <= 247:
        raise ValueError("Slave must be 1–247")
    if function in (1, 2, 3, 4, 5, 6):
        if len(body) != 6:
            raise ValueError("Expected exactly one fixed-length RTU request")
        if function < 5:
            _read(slave, function, start, value)
        elif function == 5 and value not in (0, 0xFF00):
            raise ValueError("Invalid coil value")
    elif function in (15, 16):
        limit = 1968 if function == 15 else 123
        byte_count = (value + 7) // 8 if function == 15 else value * 2
        if (
            not 1 <= value <= limit
            or start + value > 0x10000
            or len(body) != 7 + byte_count
            or body[6] != byte_count
        ):
            raise ValueError("Invalid quantity, byte count or RTU length")
    else:
        raise ValueError(f"Unsupported function FC{function:02X}")
    return frame(body) if auto_crc else packet


def validate(request: bytes, response: bytes) -> bytes:
    if len(response) < 5:
        raise ProtocolError("Invalid length")
    if frame(response[:-2]) != response:
        raise CRCError(response)
    if response[0] != request[0]:
        raise ProtocolError("Wrong slave")
    if response[1] == request[1] | 0x80 and len(response) == 5:
        raise DeviceException(response[2])
    if response[1] != request[1]:
        raise ProtocolError("Wrong function")
    if request[1] in (5, 6):
        if response != request:
            raise ProtocolError("Write echo mismatch")
    elif request[1] in (15, 16):
        if len(response) != 8 or response[:6] != request[:6]:
            raise ProtocolError("Multiple-write echo mismatch")
    elif request[1] in (1, 2, 3, 4):
        quantity = int.from_bytes(request[4:6], "big")
        count = (quantity + 7) // 8 if request[1] in (1, 2) else quantity * 2
        if response[2] != count or len(response) != count + 5:
            raise ProtocolError("Read length mismatch")
    else:
        raise ProtocolError("Unsupported function")
    return response


async def receive(reader: asyncio.StreamReader) -> bytes:
    """Read one ADU from a TCP stream, including fragmented frames."""
    header = await reader.readexactly(2)
    if header[1] in (0x81, 0x82, 0x83, 0x84, 0x85, 0x86, 0x8F, 0x90):
        return header + await reader.readexactly(3)
    if header[1] in (5, 6, 15, 16):
        return header + await reader.readexactly(6)
    if header[1] in (1, 2, 3, 4):
        count = await reader.readexactly(1)
        if not 1 <= count[0] <= 250 or (header[1] in (3, 4) and count[0] % 2):
            raise ProtocolError("Unsupported read size")
        return header + count + await reader.readexactly(count[0] + 2)
    raise ProtocolError("Unsupported response")
