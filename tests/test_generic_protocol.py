"""Generic RTU builder/parser tests independent of any product register map."""

import asyncio

import pytest

from custom_components.coselig_modbus_hub.profiles import ModbusData, PollSpec
from custom_components.coselig_modbus_hub.protocol import (
    CRCError,
    DeviceException,
    ProtocolError,
    frame,
    read_coils,
    read_discrete_inputs,
    read_holding_registers,
    read_input_registers,
    receive,
    validate,
    write_single_coil,
    write_single_register,
)
from custom_components.coselig_modbus_hub.transport import Transport

READERS = [
    read_coils,
    read_discrete_inputs,
    read_holding_registers,
    read_input_registers,
]


@pytest.mark.parametrize("function,builder", enumerate(READERS, 1))
async def test_read_build_fragment_coalesce_decode(function, builder):
    # Exercise unaligned bit addresses, multiple packed bytes, and arbitrary UINT16.
    count = 10 if function < 3 else 2
    payload = bytes([0xA5, 0x03]) if function < 3 else bytes.fromhex("1234ffff")
    request = builder(51, 400, count)
    assert request == frame(
        bytes([51, function]) + (400).to_bytes(2, "big") + count.to_bytes(2, "big")
    )
    packet = frame(bytes([51, function, len(payload)]) + payload)
    reader = asyncio.StreamReader()
    task = asyncio.create_task(receive(reader))
    reader.feed_data(packet[:2])
    await asyncio.sleep(0)
    assert not task.done()
    reader.feed_data(packet[2:] + packet)
    assert validate(request, await task) == packet
    assert validate(request, await receive(reader)) == packet
    data = ModbusData()
    PollSpec(function, 400, count).decode(packet, data)
    target = [
        data.coils,
        data.discrete_inputs,
        data.holding_registers,
        data.input_registers,
    ][function - 1]
    assert target == (
        {400 + i: bool(payload[i // 8] & (1 << (i % 8))) for i in range(count)}
        if function < 3
        else {400: 0x1234, 401: 65535}
    )
    assert data.populated


@pytest.mark.parametrize("builder", READERS)
@pytest.mark.parametrize(
    "args",
    [
        (0, 0, 1),
        (248, 0, 1),
        (True, 0, 1),
        (1, -1, 1),
        (1, 65536, 1),
        (1, 0, 0),
        (1, 65535, 2),
        (1, 0, True),
        (1, 0, 2001),
        (1, 0, 1.0),
    ],
)
def test_invalid_generic_read(builder, args):
    with pytest.raises(ValueError):
        builder(*args)


@pytest.mark.parametrize("builder", READERS)
def test_read_limits(builder):
    maximum = 2000 if builder in READERS[:2] else 125
    builder(247, 0, maximum)
    with pytest.raises(ValueError):
        builder(1, 0, maximum + 1)


@pytest.mark.parametrize("function,builder", enumerate(READERS, 1))
@pytest.mark.parametrize("kind", ["crc", "slave", "function", "length", "exception"])
async def test_read_rejections(function, builder, kind):
    request = builder(1, 0, 4)
    payload = bytes([5]) if function < 3 else bytes(8)
    response = frame(bytes([1, function, len(payload)]) + payload)
    if kind == "crc":
        response = response[:-1] + bytes([response[-1] ^ 1])
        error = CRCError
    elif kind == "slave":
        response = frame(bytes([2]) + response[1:-2])
        error = ProtocolError
    elif kind == "function":
        response = frame(bytes([1, 6]) + response[2:-2])
        error = ProtocolError
    elif kind == "length":
        response = frame(bytes([1, function, 2, 0, 0]))
        error = ProtocolError
    else:
        response = frame(bytes([1, function | 0x80, 1]))
        error = DeviceException
        reader = asyncio.StreamReader()
        reader.feed_data(response)
        assert await receive(reader) == response
    with pytest.raises(error):
        validate(request, response)


@pytest.mark.parametrize(
    "function,value,encoded",
    [
        (5, True, 0xFF00),
        (5, False, 0),
        (6, 0x0564, 0x0564),
        (6, 65535, 65535),
        (6, 0, 0),
    ],
)
async def test_write_raw_echo(function, value, encoded):
    builder = write_single_coil if function == 5 else write_single_register
    request = builder(70, 65535, value)
    assert request == frame(
        bytes([70, function, 255, 255]) + encoded.to_bytes(2, "big")
    )
    reader = asyncio.StreamReader()
    reader.feed_data(request)
    assert validate(request, await receive(reader)) == request
    with pytest.raises(ProtocolError, match="echo"):
        validate(request, frame(request[:3] + b"\x00" + request[4:-2]))
    with pytest.raises(CRCError):
        validate(request, request[:-1] + bytes([request[-1] ^ 1]))
    exception = frame(bytes([70, function | 0x80, 2]))
    reader.feed_data(exception)
    with pytest.raises(DeviceException):
        validate(request, await receive(reader))


@pytest.mark.parametrize(
    "builder,args",
    [
        (write_single_coil, (1, 0, 1)),
        (write_single_coil, (1, 0, None)),
        (write_single_register, (1, 0, -1)),
        (write_single_register, (1, 0, 65536)),
        (write_single_register, (1, 0, True)),
        (write_single_register, (1, True, 1)),
    ],
)
def test_invalid_generic_write(builder, args):
    with pytest.raises(ValueError):
        builder(*args)


@pytest.mark.parametrize("function", [1, 2, 3, 4])
@pytest.mark.parametrize("count", [0, 251])
async def test_parser_invalid_byte_count(function, count):
    reader = asyncio.StreamReader()
    reader.feed_data(bytes([1, function, count]))
    with pytest.raises(ProtocolError):
        await receive(reader)


async def test_fc05_timeout_and_recovery(gateway):
    transport = Transport("127.0.0.1", gateway.port, timeout=0.02)
    try:
        gateway.modes[70] = "timeout"
        with pytest.raises(TimeoutError):
            await transport.submit(write_single_coil(70, 0, True))
        gateway.modes.clear()
        request = write_single_coil(70, 0, False)
        assert await transport.submit(request) == request
    finally:
        await transport.close()
