import asyncio

import pytest

from custom_components.coselig_modbus_hub.protocol import (
    CRCError,
    DeviceException,
    ProtocolError,
    crc16,
    frame,
    read,
    receive,
    validate,
    write,
)


def test_crc_and_fixed_packets():
    assert crc16(bytes.fromhex("01030000000a")) == 0xCDC5
    assert read(1, 2) == frame(bytes.fromhex("0103082a0002"))
    assert read(1, 4) == frame(bytes.fromhex("0103082a0004"))
    request = write(51, 0x082A, 50)
    assert request[:6] == bytes.fromhex("3306082a0532")
    assert validate(request, request) == request


@pytest.mark.parametrize("slave,count", [(0, 2), (248, 2), (True, 2), (1, 3)])
def test_invalid_read(slave, count):
    with pytest.raises(ValueError):
        read(slave, count)


@pytest.mark.parametrize(
    "slave,register,value",
    [(0, 0x082A, 2), (True, 0x082A, 2), (1, 0, 2), (1, 0x082A, 101), (1, 0x082A, True)],
)
def test_invalid_write(slave, register, value):
    with pytest.raises(ValueError):
        write(slave, register, value)


@pytest.mark.parametrize(
    "response",
    [
        b"",
        frame(bytes([2, 6, 8, 42, 5, 20])),
        frame(bytes([1, 3, 4, 0, 0, 0, 0])),
        write(1, 0x082A, 21),
    ],
)
def test_rejected_echo(response):
    with pytest.raises(ProtocolError):
        validate(write(1, 0x082A, 20), response)


def test_crc_exception_and_read_shape():
    request = read(1, 4)
    valid = frame(bytes([1, 3, 8, 0, 20, 0, 30, 0, 40, 0, 50]))
    assert validate(request, valid) == valid
    with pytest.raises(CRCError):
        validate(request, valid[:-1] + bytes([valid[-1] ^ 1]))
    with pytest.raises(DeviceException) as error:
        validate(request, frame(bytes([1, 0x83, 2])))
    assert error.value.code == 2
    with pytest.raises(ProtocolError):
        validate(request, frame(bytes([1, 3, 4, 0, 1, 0, 2])))
    with pytest.raises(ProtocolError):
        validate(
            frame(bytes([1, 4, 8, 42, 0, 2])),
            valid[:1] + frame(bytes([4, 4, 0, 1, 0, 2])),
        )


async def test_coalesced_fragmented_frames():
    reader = asyncio.StreamReader()
    packets = [write(1, 0x082A, 20), write(2, 0x082A, 30), frame(bytes([1, 0x83, 2]))]
    task = asyncio.create_task(receive(reader))
    reader.feed_data(packets[0][:1])
    await asyncio.sleep(0)
    assert not task.done()
    reader.feed_data(packets[0][1:] + b"".join(packets[1:]))
    assert await task == packets[0]
    assert await receive(reader) == packets[1]
    assert await receive(reader) == packets[2]


@pytest.mark.parametrize("packet", [bytes([1, 7]), bytes([1, 3, 3])])
async def test_receive_invalid(packet):
    reader = asyncio.StreamReader()
    reader.feed_data(packet)
    with pytest.raises(ProtocolError):
        await receive(reader)


def test_unsupported_function():
    with pytest.raises(ProtocolError, match="Unsupported function"):
        validate(frame(bytes([1, 7, 0, 0])), frame(bytes([1, 7, 0, 0])))
